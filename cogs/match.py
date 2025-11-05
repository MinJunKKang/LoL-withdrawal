# cogs/match.py
import asyncio
import random
import re
import urllib.parse
import configparser
from typing import Dict, Set, List, Optional, Tuple

import discord
from discord.ext import commands
from discord.ui import View, Button, Select

from utils.stats import update_result_dual, add_points

CURRENCY = "Point"
WIN_REWARD = 5
LOSE_REWARD = 3

# ───────── config.ini 로딩 ─────────
_cfg = configparser.ConfigParser()
try:
    _cfg.read("config.ini", encoding="utf-8")
except Exception:
    pass

def _get_id(section: str, key: str) -> int:
    """config.ini에서 정수 ID 읽기 (없거나 잘못되면 0)."""
    try:
        val = _cfg.get(section, key, fallback="0")
        return int(val) if str(val).isdigit() else 0
    except Exception:
        return 0

# 설정 값들 읽기
MATCH_LOG_CHANNEL_ID: int = _get_id("Match", "match_log_channel_id")
MATCH_JOIN_LEAVE_LOG_CHANNEL_ID: int = _get_id("Match", "match_join_leave_log_channel_id")  # 선택 로그 채널

# ===== 도우미 함수 =====
def create_opgg_multisearch_url(summoner_list: List[str]) -> str:
    base_url = "https://op.gg/ko/lol/multisearch/kr?summoners="
    encoded = [urllib.parse.quote(s) for s in summoner_list]
    return base_url + ",".join(encoded)

def clean_opgg_name(name: str) -> str:
    return re.sub(r"[^\w\s가-힣/#]", "", name).split('/')[0].strip()


# ===== 데이터 구조 =====
class Game:
    """슬롯(1~10) 기반 모집 + 대기열(최대 5) + 드롭다운 팀장/드래프트"""
    def __init__(self, game_id: int, host_id: int, channel_id: int):
        self.id = game_id
        self.host_id = host_id
        self.channel_id = channel_id

        # 모집/참여
        self.slots: Dict[int, Optional[int]] = {i: None for i in range(1, 11)}  # 1..10 → user_id or None
        self.user_to_slot: Dict[int, int] = {}  # user_id → slot
        self.waitlist: List[int] = []  # 최대 5
        self.message: Optional[discord.Message] = None  # 모집 메시지

        # 팀 구성 (드래프트)
        self.team_captains: List[int] = []            # [팀장1, 팀장2]  ← 순서 고정(1팀/2팀)
        self.teams: Dict[int, List[int]] = {1: [], 2: []}
        self.pick_order: List[int] = []               # 예: [1,2,2,1,1,2,2,1]
        self.draft_turn: int = 0
        self.pick_history: List[Tuple[int, int]] = [] # (team_no, picked_uid)
        self.team_status_message: Optional[discord.Message] = None

        # 진행/종료
        self.finished = False
        self.result_message: Optional[discord.Message] = None  # 결과 버튼 메시지

    # ---- 편의 ----
    @property
    def participants(self) -> List[int]:
        return [uid for uid in self.slots.values() if uid is not None]

    def is_full(self) -> bool:
        return all(self.slots[i] is not None for i in self.slots)

    def first_free_slot(self) -> Optional[int]:
        for i in range(1, 11):
            if self.slots[i] is None:
                return i
        return None

    def assign_slot(self, user_id: int, slot_no: int) -> Tuple[bool, str]:
        """지정 슬롯 배정. 성공 여부/메시지 반환."""
        if slot_no not in self.slots:
            return False, "존재하지 않는 슬롯입니다."
        if self.slots[slot_no] is not None and self.slots[slot_no] != user_id:
            return False, "이미 사용 중인 번호입니다."

        # 기존 대기열/슬롯 정리
        self.remove_from_waitlist(user_id)
        if user_id in self.user_to_slot:
            old = self.user_to_slot[user_id]
            self.slots[old] = None
            del self.user_to_slot[user_id]

        # 배정
        self.slots[slot_no] = user_id
        self.user_to_slot[user_id] = slot_no
        return True, f"{slot_no}번으로 배정되었습니다."

    def remove_from_slot(self, user_id: int) -> Optional[int]:
        """유저의 슬롯을 비우고, 비워진 슬롯 번호를 리턴."""
        if user_id in self.user_to_slot:
            s = self.user_to_slot[user_id]
            self.slots[s] = None
            del self.user_to_slot[user_id]
            return s
        return None

    def add_waitlist(self, user_id: int) -> Tuple[bool, str]:
        if user_id in self.user_to_slot:
            return False, "이미 참여 중입니다."
        if user_id in self.waitlist:
            return False, "이미 대기 중입니다."
        if len(self.waitlist) >= 5:
            return False, "대기 인원이 가득 찼습니다. (5/5)"
        self.waitlist.append(user_id)
        return True, "대기열에 등록되었습니다."

    def remove_from_waitlist(self, user_id: int) -> bool:
        if user_id in self.waitlist:
            self.waitlist.remove(user_id)
            return True
        return False

    def autopromote_waiter(self, freed_slot: Optional[int]) -> Optional[int]:
        """공석이 있을 때 대기열 선두를 승격. 승격된 유저 id 반환(없으면 None)."""
        if freed_slot is None:
            freed_slot = self.first_free_slot()
        if freed_slot is None or not self.waitlist:
            return None
        uid = self.waitlist.pop(0)
        self.slots[freed_slot] = uid
        self.user_to_slot[uid] = freed_slot
        return uid


# ====== Cog ======
class MatchCog(commands.Cog):
    """내전(슬롯 모집/대기/드롭다운 팀장-드래프트/결과 기록/OPGG + 관리자 패널) 전담 Cog"""

    def __init__(self, bot: commands.Bot, role_ids: Dict[str, int]):
        self.bot = bot
        self.role_ids = role_ids
        self.game_counter: int = 1
        self.games: Dict[int, Game] = {}          # game_id → Game
        self.channel_to_game: Dict[int, int] = {} # channel_id → game_id
        self.active_hosts: Set[int] = set()

    # ---------- 채널/로그 ----------
    def _get_match_log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        if MATCH_LOG_CHANNEL_ID:
            ch = guild.get_channel(MATCH_LOG_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
                return ch
        for c in guild.text_channels:
            if c.permissions_for(guild.me).send_messages:
                return c
        return None

    def _get_join_leave_log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        if MATCH_JOIN_LEAVE_LOG_CHANNEL_ID:
            ch = guild.get_channel(MATCH_JOIN_LEAVE_LOG_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
                return ch
        return None

    def _get_active_game(self, channel_id: int) -> Optional[Game]:
        gid = self.channel_to_game.get(channel_id)
        if gid is None:
            return None
        return self.games.get(gid)

    # ---------- 권한 ----------
    @staticmethod
    def _is_host_or_admin(interaction_or_ctx, game: Game) -> bool:
        user = interaction_or_ctx.user if isinstance(interaction_or_ctx, discord.Interaction) else interaction_or_ctx.author
        if getattr(user, "guild_permissions", None) and user.guild_permissions.manage_guild:
            return True
        return user.id == game.host_id

    # ---------- 임베드 ----------
    def _build_lobby_embed(self, guild: discord.Guild, game: Game) -> discord.Embed:
        def name(uid: Optional[int]) -> str:
            if uid is None:
                return "알 수 없음"
            m = guild.get_member(uid)
            return m.display_name if m else f"유저({uid})"

        # 현재 참여자만 1..N으로 표시
        filled_uids = [game.slots[i] for i in range(1, 11) if game.slots[i] is not None]
        participants_lines = [f"{i}. {name(uid)}" for i, uid in enumerate(filled_uids, start=1)]
        participants_text = "\n".join(participants_lines) if participants_lines else "-"

        wait_names = [name(uid) for uid in game.waitlist]

        def team_list(team_no: int) -> str:
            members = []
            # 팀장은 별도 표시
            if len(game.team_captains) >= team_no and game.team_captains[team_no-1] is not None:
                members.append(f"⭐ {name(game.team_captains[team_no-1])}")
            for uid in game.teams[team_no]:
                if len(game.team_captains) >= team_no and uid == game.team_captains[team_no-1]:
                    continue
                members.append(f"- {name(uid)}")
            return "\n".join(members) or "-"

        embed = discord.Embed(
            title=f"내전 #{game.id} — 모집 중 ({len(filled_uids)}/10)",
            description=participants_text,
            color=0x2F3136
        )
        embed.add_field(name=f"대기 ({len(game.waitlist)}/5)", value=", ".join(wait_names) or "-", inline=False)
        embed.add_field(name="🟦 1팀", value=team_list(1), inline=True)
        embed.add_field(name="🟥 2팀", value=team_list(2), inline=True)
        return embed

    async def _refresh_lobby(self, guild: discord.Guild, game: Game):
        if game.message:
            await game.message.edit(embed=self._build_lobby_embed(guild, game), view=self.LobbyView(self, game))

    def _build_team_embed(self, guild: discord.Guild, game: Game) -> discord.Embed:
        def names(team_no: int) -> List[str]:
            arr = []
            for uid in game.teams[team_no]:
                m = guild.get_member(uid)
                if not m:
                    continue
                tag = "⭐ " if len(game.team_captains) >= team_no and uid == game.team_captains[team_no-1] else "- "
                arr.append(f"{tag}{m.display_name}")
            return arr

        t1 = "\n".join(names(1)) or "-"
        t2 = "\n".join(names(2)) or "-"
        embed = discord.Embed(title=f"내전 #{game.id} 팀 구성 현황", color=0x2F3136)
        embed.add_field(name="🟦 1팀", value=t1, inline=True)
        embed.add_field(name="🟥 2팀", value=t2, inline=True)
        return embed

    async def _refresh_team_status(self, guild: discord.Guild, game: Game):
        if game.team_status_message:
            await game.team_status_message.edit(embed=self._build_team_embed(guild, game), view=self.TeamManageEntryView(self, game))

    # ---------- 티어 정렬(팀장 선택용) ----------
    async def get_sorted_participants_by_tier(self, guild: discord.Guild, user_ids: List[int]) -> List[str]:
        tier_order = {"C": 0, "GM": 1, "M": 2, "D": 3, "E": 4, "P": 5, "G": 6, "S": 7, "B": 8, "I": 9}
        def parse_tier(text: str):
            match = re.search(r"(C|GM|M|D|E|P|G|S|B|I)(\d+)", text.upper())
            if match:
                tier, num = match.groups()
                num = int(num)
                tier_rank = tier_order.get(tier, 999)
                score = -num if tier in ("C", "GM", "M") else num
                return (tier_rank, score)
            return (999, 999)

        entries = []
        for uid in user_ids:
            member = guild.get_member(uid)
            if not member:
                continue
            name = member.display_name
            entries.append((name, parse_tier(name)))

        sorted_entries = sorted(entries, key=lambda x: x[1])
        return [entry[0] for entry in sorted_entries]

    # ========= 명령 그룹: .내전 =========
    @commands.group(name="내전", invoke_without_command=True)
    async def match_group(self, ctx: commands.Context):
        await ctx.send("사용법: `.내전 모집`으로 진행하세요.")

    @match_group.command(name="모집")
    async def start_lobby(self, ctx: commands.Context):
        if self._get_active_game(ctx.channel.id):
            await ctx.send("이미 이 채널에서 진행 중인 내전이 있습니다.")
            return

        game_id = self.game_counter
        self.game_counter += 1

        game = Game(game_id, ctx.author.id, ctx.channel.id)
        # 개최자 자동 배정: 1번 슬롯
        game.assign_slot(ctx.author.id, 1)

        self.games[game_id] = game
        self.channel_to_game[ctx.channel.id] = game_id
        self.active_hosts.add(ctx.author.id)

        role_id = self.role_ids.get("내전")
        role = ctx.guild.get_role(role_id) if role_id else None
        if role is None:
            role = discord.utils.get(ctx.guild.roles, name="내전")
        allowed = discord.AllowedMentions(roles=[role] if role else [])
        content = role.mention if role else None

        embed = self._build_lobby_embed(ctx.guild, game)
        message = await ctx.send(content=content, embed=embed, view=self.LobbyView(self, game), allowed_mentions=allowed)
        game.message = message

    @match_group.command(name="참여")
    async def join_command(self, ctx: commands.Context):
        """텍스트 명령으로도 즉시 참여/대기 처리."""
        game = self._get_active_game(ctx.channel.id)
        if not game:
            await ctx.send("이 채널에 진행 중인 내전이 없습니다. `.내전 모집`으로 시작하세요.")
            return
        user_id = ctx.author.id
        if user_id in game.user_to_slot:
            await ctx.send("이미 참여 중입니다.")
            return
        free = game.first_free_slot()
        if free is not None:
            game.assign_slot(user_id, free)
            ch = self._get_join_leave_log_channel(ctx.guild)
            if ch:
                await ch.send(f"👋 `{ctx.author.display_name}`님이 내전 #{game.id}에 참여했습니다. ({len(game.participants)}/10)")
            await self._refresh_lobby(ctx.guild, game)
            await ctx.message.add_reaction("✅")
        else:
            ok, msg = game.add_waitlist(user_id)
            await ctx.send(msg)

    # ========= 팀장 선택(2단계) → 드래프트 =========
    async def start_team_leader_selection(self, interaction: discord.Interaction, game: Game):
        guild = interaction.guild
        assert guild is not None

        sorted_names = await self.get_sorted_participants_by_tier(guild, game.participants)
        name_to_user = {guild.get_member(uid).display_name: uid for uid in game.participants if guild.get_member(uid)}

        options = []
        for name in sorted_names:
            uid = name_to_user.get(name)
            if uid:
                options.append(discord.SelectOption(label=name, value=str(uid)))

        cog = self

        # 1단계: 1팀장 선택
        class Captain1View(View):
            def __init__(self):
                super().__init__(timeout=None)

            @discord.ui.select(placeholder="1팀장을 선택하세요 (한 명)", min_values=1, max_values=1, options=options)
            async def select_c1(self, inner: discord.Interaction, select: Select):
                if inner.user.id != game.host_id and not inner.user.guild_permissions.manage_guild:
                    await inner.response.send_message("팀장 선택은 개최자 또는 관리자만 가능합니다.", ephemeral=True)
                    return
                c1 = int(select.values[0])

                # 2단계 뷰로 교체 (2팀장은 남은 인원에서 선택)
                remain_opts = [o for o in options if int(o.value) != c1]

                class Captain2View(View):
                    def __init__(self):
                        super().__init__(timeout=None)

                    @discord.ui.select(placeholder="2팀장을 선택하세요 (한 명)", min_values=1, max_values=1, options=remain_opts)
                    async def select_c2(self, inner2: discord.Interaction, select2: Select):
                        if inner2.user.id != game.host_id and not inner2.user.guild_permissions.manage_guild:
                            await inner2.response.send_message("팀장 선택은 개최자 또는 관리자만 가능합니다.", ephemeral=True)
                            return
                        c2 = int(select2.values[0])

                        # 순서 고정: [1팀장, 2팀장]
                        game.team_captains = [c1, c2]

                        embed = discord.Embed(
                            title="팀장 선택 완료",
                            description="팀장이 선택되었습니다! 팀 구성을 시작합니다.",
                            color=0x2F3136
                        )
                        await inner2.response.edit_message(embed=embed, view=None)
                        await cog.start_draft(inner2, game)

                await inner.response.edit_message(
                    embed=discord.Embed(title="팀장 선택 (2/2)", description="2팀장을 선택해주세요.", color=0x2F3136),
                    view=Captain2View()
                )

        embed = discord.Embed(
            title="팀장 선택 (1/2)",
            description="명단에서 1팀장을 선택해주세요:",
            color=0x2F3136
        )
        await interaction.channel.send(embed=embed, view=Captain1View())

    async def start_draft(self, interaction: discord.Interaction, game: Game):
        # 플레이어 풀(팀장 제외)
        players = [uid for uid in game.participants if uid not in game.team_captains]
        random.shuffle(players)

        # 선픽 팀 랜덤
        first = random.choice([1, 2])

        # ❗ 팀장 순서는 고정(1팀, 2팀). 더 이상 셔플하지 않음.
        game.teams[1].append(game.team_captains[0])
        game.teams[2].append(game.team_captains[1])

        game.pick_order = [1, 2, 2, 1, 1, 2, 2, 1] if first == 1 else [2, 1, 1, 2, 2, 1, 1, 2]

        guild = interaction.guild
        assert guild is not None

        embed = self._build_team_embed(guild, game)
        # 팀 현황 메시지에는 항상 관리자 진입 버튼을 붙인다
        game.team_status_message = await interaction.channel.send(embed=embed, view=self.TeamManageEntryView(self, game))
        await self.send_draft_ui(interaction.channel, game, players)

    async def send_draft_ui(self, channel: discord.TextChannel, game: Game, available: List[int]):
        if not available or game.draft_turn >= len(game.pick_order):
            await self.finish_teams(channel, game)
            return

        team_num = game.pick_order[game.draft_turn]
        captain_id = game.team_captains[team_num - 1]
        guild = channel.guild

        cog = self

        class DraftView(View):
            def __init__(self):
                super().__init__(timeout=None)
                # 관리 진입 버튼(드래프트 중에도 관리자가 사용할 수 있도록)
                self.add_item(discord.ui.Button(label="관리", style=discord.ButtonStyle.secondary, custom_id="__manage_entry__"))

            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                # "관리" 버튼 커스텀 처리
                if interaction.data and interaction.data.get("custom_id") == "__manage_entry__":
                    if not cog._is_host_or_admin(interaction, game):
                        await interaction.response.send_message("관리자(Manage Guild) 또는 개최자만 사용할 수 있습니다.", ephemeral=True)
                        return False
                    await interaction.response.send_message("관리 패널을 여셨습니다.", ephemeral=True, view=cog.AdminMenuView(cog, game))
                    return False
                return True

            @discord.ui.select(
                placeholder=f"{team_num}팀 픽 대상 선택",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(
                        label=guild.get_member(uid).display_name if guild.get_member(uid) else f"{uid}",
                        value=str(uid)
                    ) for uid in available
                ]
            )
            async def select_callback(self, interaction: discord.Interaction, select: Select):
                if interaction.user.id != captain_id:
                    await interaction.response.send_message("지금은 다른 팀장의 차례입니다.", ephemeral=True)
                    return

                uid = int(select.values[0])
                if uid not in available:
                    await interaction.response.send_message("이미 선택된 유저입니다.", ephemeral=True)
                    return

                game.teams[team_num].append(uid)
                available.remove(uid)
                game.pick_history.append((team_num, uid))
                game.draft_turn += 1

                await game.team_status_message.edit(embed=cog._build_team_embed(guild, game), view=cog.TeamManageEntryView(cog, game))
                await interaction.message.delete()
                await cog.send_draft_ui(channel, game, available)

            @discord.ui.button(label="↩ 되돌리기", style=discord.ButtonStyle.secondary)
            async def undo_pick(self, interaction: discord.Interaction, button: Button):
                if not cog._is_host_or_admin(interaction, game):
                    await interaction.response.send_message("되돌리기는 개최자 또는 관리자만 가능합니다.", ephemeral=True)
                    return

                if not game.pick_history:
                    await interaction.response.send_message("되돌릴 선택이 없습니다.", ephemeral=True)
                    return

                last_team, last_uid = game.pick_history.pop()

                if last_uid in game.teams[last_team]:
                    game.teams[last_team].remove(last_uid)

                if last_uid not in available:
                    available.append(last_uid)

                if game.draft_turn > 0:
                    game.draft_turn -= 1

                await game.team_status_message.edit(embed=cog._build_team_embed(guild, game), view=cog.TeamManageEntryView(cog, game))

                try:
                    await interaction.message.delete()
                except:
                    pass
                await cog.send_draft_ui(channel, game, available)

        embed = discord.Embed(
            title=f"{team_num}팀 팀원 선택",
            description=f"{guild.get_member(captain_id).display_name if guild.get_member(captain_id) else captain_id}님, 팀원을 선택하세요:",
            color=0x2F3136
        )
        await channel.send(embed=embed, view=DraftView())

    async def finish_teams(self, channel: discord.TextChannel, game: Game):
        guild = channel.guild

        team1_members, team2_members = [], []
        team1_opgg_names, team2_opgg_names = [], []

        for uid in game.teams[1]:
            member = guild.get_member(uid)
            nickname = member.display_name if member else "알 수 없음"
            display = f"⭐ {nickname}" if uid == game.team_captains[0] else f"- {nickname}"
            team1_members.append(display)
            if nickname != "알 수 없음":
                team1_opgg_names.append(clean_opgg_name(nickname))

        for uid in game.teams[2]:
            member = guild.get_member(uid)
            nickname = member.display_name if member else "알 수 없음"
            display = f"⭐ {nickname}" if uid == game.team_captains[1] else f"- {nickname}"
            team2_members.append(display)
            if nickname != "알 수 없음":
                team2_opgg_names.append(clean_opgg_name(nickname))

        t1 = "\n".join(team1_members)
        t2 = "\n".join(team2_members)

        opgg1 = create_opgg_multisearch_url(team1_opgg_names) if team1_opgg_names else None
        opgg2 = create_opgg_multisearch_url(team2_opgg_names) if team2_opgg_names else None

        embed = discord.Embed(title=f"⚔️ 내전 #{game.id} 팀 구성 완료", color=0x2F3136)
        embed.add_field(name="🟦 1팀", value=t1 or "- 없음", inline=True)
        embed.add_field(name="🟥 2팀", value=t2 or "- 없음", inline=True)
        embed.set_footer(text="전적 보기 버튼은 아래에 있습니다 👇")

        result_view = self.ResultView(self, game)
        result_message = await channel.send(embed=embed, view=result_view)
        game.result_message = result_message

        # OPGG 버튼
        if opgg1 or opgg2:
            v = View(timeout=10800)
            if opgg1:
                v.add_item(discord.ui.Button(label="🔎 1팀 전적 보기", url=opgg1, style=discord.ButtonStyle.link))
            if opgg2:
                v.add_item(discord.ui.Button(label="🔎 2팀 전적 보기", url=opgg2, style=discord.ButtonStyle.link))
            await channel.send(view=v)

        # 기록 채널에도 복사
        log_ch = self._get_match_log_channel(guild)
        if log_ch:
            await log_ch.send(embed=embed, view=None)

        # 3시간 뒤 버튼 잠금
        asyncio.create_task(self.disable_buttons_after_timeout(result_message, result_view, 10800))

    async def disable_buttons_after_timeout(self, message: discord.Message, view: View, seconds: int):
        await asyncio.sleep(seconds)
        if hasattr(view, "game") and getattr(view.game, "finished", False):
            return
        for item in view.children:
            item.disabled = True
        try:
            embed = message.embeds[0]
            embed.add_field(name="상태", value="⏱️ 시간 초과로 인해 종료되었습니다.", inline=False)
            await message.edit(embed=embed, view=view)
        except Exception:
            pass

    # ========= View들 =========
    class LobbyView(View):
        """모집 메시지의 기본 컨트롤러 + 관리자 진입"""
        def __init__(self, cog: "MatchCog", game: Game):
            super().__init__(timeout=None)
            self.cog = cog
            self.game = game
            # 관리 버튼(표시는 모두가 보지만, 권한 체크 후 에페메랄로 패널 제공)
            self.add_item(discord.ui.Button(label="관리", style=discord.ButtonStyle.secondary, custom_id="__manage_entry__"))

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.data and interaction.data.get("custom_id") == "__manage_entry__":
                if not self.cog._is_host_or_admin(interaction, self.game):
                    await interaction.response.send_message("관리자(Manage Guild) 또는 개최자만 사용할 수 있습니다.", ephemeral=True)
                    return False
                await interaction.response.send_message("관리 패널을 여셨습니다.", ephemeral=True, view=self.cog.AdminMenuView(self.cog, self.game))
                return False
            return True

        async def _refresh(self, interaction: discord.Interaction):
            await self.cog._refresh_lobby(interaction.guild, self.game)

        @discord.ui.button(label="참여", style=discord.ButtonStyle.success)
        async def join(self, interaction: discord.Interaction, button: Button):
            uid = interaction.user.id
            if uid in self.game.user_to_slot:
                await interaction.response.send_message("이미 참여 중입니다.", ephemeral=True)
                return

            free = self.game.first_free_slot()
            if free is not None:
                self.game.assign_slot(uid, free)
                ch = self.cog._get_join_leave_log_channel(interaction.guild)
                if ch:
                    await ch.send(f"👋 `{interaction.user.display_name}`님이 내전 #{self.game.id}에 참여했습니다. ({len(self.game.participants)}/10)")
                await self._refresh(interaction)
                await interaction.response.send_message("참여 완료!", ephemeral=True)
                return

            ok, msg = self.game.add_waitlist(uid)
            if ok:
                ch = self.cog._get_join_leave_log_channel(interaction.guild)
                if ch:
                    await ch.send(f"🕒 `{interaction.user.display_name}`님이 내전 #{self.game.id} 대기열에 등록했습니다. ({len(self.game.waitlist)}/5)")
                await self._refresh(interaction)
            await interaction.response.send_message(msg, ephemeral=True)

        @discord.ui.button(label="대기", style=discord.ButtonStyle.primary)
        async def wait(self, interaction: discord.Interaction, button: Button):
            ok, msg = self.game.add_waitlist(interaction.user.id)
            if ok:
                ch = self.cog._get_join_leave_log_channel(interaction.guild)
                if ch:
                    await ch.send(f"🕒 `{interaction.user.display_name}`님이 내전 #{self.game.id} 대기열에 등록했습니다. ({len(self.game.waitlist)}/5)")
                await self._refresh(interaction)
            await interaction.response.send_message(msg, ephemeral=True)

        @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: Button):
            freed = self.game.remove_from_slot(interaction.user.id)
            removed_wait = self.game.remove_from_waitlist(interaction.user.id)
            promoted = None
            if freed is not None:
                promoted = self.game.autopromote_waiter(freed)

            ch = self.cog._get_join_leave_log_channel(interaction.guild)
            if ch:
                if freed is not None:
                    await ch.send(f"🚪 `{interaction.user.display_name}`님이 내전 #{self.game.id}에서 슬롯을 비웠습니다.")
                elif removed_wait:
                    await ch.send(f"🚪 `{interaction.user.display_name}`님이 내전 #{self.game.id} 대기열에서 나갔습니다.")

            await self._refresh(interaction)

            if promoted:
                m = interaction.guild.get_member(promoted)
                try_name = m.display_name if m else f"{promoted}"
                await interaction.channel.send(f"📣 대기열 승격: **{try_name}** 님이 공석으로 자동 배정되었습니다.")
            await interaction.response.defer(ephemeral=True)

        @discord.ui.button(label="시작", style=discord.ButtonStyle.secondary)
        async def start(self, interaction: discord.Interaction, button: Button):
            if not self.cog._is_host_or_admin(interaction, self.game):
                await interaction.response.send_message("개최자 또는 관리자만 시작할 수 있습니다.", ephemeral=True)
                return
            if len(self.game.participants) < 2:
                await interaction.response.send_message("참여자가 너무 적습니다. (최소 2명)", ephemeral=True)
                return
            await interaction.response.defer()
            await self.cog.start_team_leader_selection(interaction, self.game)

        @discord.ui.button(label="종료", style=discord.ButtonStyle.danger)
        async def end(self, interaction: discord.Interaction, button: Button):
            if not self.cog._is_host_or_admin(interaction, self.game):
                await interaction.response.send_message("개최자 또는 관리자만 종료할 수 있습니다.", ephemeral=True)
                return
            self.cog.active_hosts.discard(self.game.host_id)
            self.cog.channel_to_game.pop(self.game.channel_id, None)
            self.cog.games.pop(self.game.id, None)
            for child in self.children:
                child.disabled = True
            embed = interaction.message.embeds[0]
            embed.title = f"내전 #{self.game.id} — 종료됨"
            await interaction.response.edit_message(embed=embed, view=self)

    class TeamManageEntryView(View):
        """팀 현황 메시지에 붙는 '관리' 진입 버튼"""
        def __init__(self, cog: "MatchCog", game: Game):
            super().__init__(timeout=None)
            self.cog = cog
            self.game = game
            self.add_item(discord.ui.Button(label="관리", style=discord.ButtonStyle.secondary, custom_id="__manage_entry__"))

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.data and interaction.data.get("custom_id") == "__manage_entry__":
                if not self.cog._is_host_or_admin(interaction, self.game):
                    await interaction.response.send_message("관리자(Manage Guild) 또는 개최자만 사용할 수 있습니다.", ephemeral=True)
                    return False
                await interaction.response.send_message("관리 패널을 여셨습니다.", ephemeral=True, view=self.cog.AdminMenuView(self.cog, self.game))
                return False
            return True

    # ------ 관리자 패널(에페메랄) ------
    class AdminMenuView(View):
        def __init__(self, cog: "MatchCog", game: Game):
            super().__init__(timeout=300)
            self.cog = cog
            self.game = game

        @discord.ui.button(label="멤버 제외", style=discord.ButtonStyle.danger)
        async def kick_member(self, interaction: discord.Interaction, button: Button):
            if not self.cog._is_host_or_admin(interaction, self.game):
                await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
                return
            await interaction.response.edit_message(content="제외할 멤버를 선택하세요.", view=self.cog.KickView(self.cog, self.game))

        @discord.ui.button(label="멤버 교체", style=discord.ButtonStyle.primary)
        async def replace_member(self, interaction: discord.Interaction, button: Button):
            if not self.cog._is_host_or_admin(interaction, self.game):
                await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
                return
            await interaction.response.edit_message(content="교체할 팀을 선택하세요.", view=self.cog.ReplaceTeamPickView(self.cog, self.game))

        @discord.ui.button(label="닫기", style=discord.ButtonStyle.secondary)
        async def close(self, interaction: discord.Interaction, button: Button):
            await interaction.response.edit_message(content="관리 패널을 닫았습니다.", view=None)

    class KickView(View):
        """팀장 제외(킥 불가), 일반 팀원/참가자/대기열 제외"""
        def __init__(self, cog: "MatchCog", game: Game):
            super().__init__(timeout=300)
            self.cog = cog
            self.game = game

            guild = self.cog.bot.get_guild(self.cog.bot.guilds[0].id) if self.cog.bot.guilds else None
            # 옵션 구성
            opts: List[discord.SelectOption] = []
            def add_opt(uid: int, label_prefix: str):
                m = guild.get_member(uid) if guild else None
                label = f"{label_prefix} {m.display_name if m else uid}"
                opts.append(discord.SelectOption(label=label, value=str(uid)))

            # 팀원(팀장 제외)
            for t in (1, 2):
                for uid in self.game.teams[t]:
                    if uid == (self.game.team_captains[t-1] if len(self.game.team_captains) >= t else None):
                        continue  # 팀장은 킥으로 제외하지 않음
                    add_opt(uid, f"[팀{t}]")

            # 슬롯 참가자(팀 미배정)
            assigned = set(self.game.teams[1] + self.game.teams[2])
            for uid in self.game.participants:
                if uid in assigned:
                    continue
                add_opt(uid, "[참여]")

            # 대기열
            for uid in self.game.waitlist:
                add_opt(uid, "[대기]")

            if not opts:
                opts = [discord.SelectOption(label="제외할 대상이 없습니다.", value="-1", description="돌아가기를 누르세요.")]

            self._select = Select(placeholder="제외할 멤버 선택", min_values=1, max_values=1, options=opts)
            self.add_item(self._select)

        @discord.ui.button(label="제외 실행", style=discord.ButtonStyle.danger)
        async def do_kick(self, interaction: discord.Interaction, button: Button):
            if not self.cog._is_host_or_admin(interaction, self.game):
                await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
                return
            if not self._select.values or self._select.values[0] == "-1":
                await interaction.response.send_message("대상을 선택해주세요.", ephemeral=True)
                return
            uid = int(self._select.values[0])

            # 대기열에서 제거 우선
            if uid in self.game.waitlist:
                self.game.remove_from_waitlist(uid)
            # 팀에서 제거(팀장 보호는 옵션 구성에서 제외함)
            for t in (1, 2):
                if uid in self.game.teams[t]:
                    self.game.teams[t].remove(uid)
            # 슬롯에서 제거
            freed = self.game.remove_from_slot(uid)
            if freed is not None:
                self.game.autopromote_waiter(freed)

            await self.cog._refresh_lobby(interaction.guild, self.game)
            await self.cog._refresh_team_status(interaction.guild, self.game)
            await interaction.response.edit_message(content="제외를 완료했습니다.", view=self.cog.AdminMenuView(self.cog, self.game))

        @discord.ui.button(label="뒤로", style=discord.ButtonStyle.secondary)
        async def back(self, interaction: discord.Interaction, button: Button):
            await interaction.response.edit_message(content="관리 메뉴로 돌아갑니다.", view=self.cog.AdminMenuView(self.cog, self.game))

    class ReplaceTeamPickView(View):
        """교체할 팀 선택 → 다음 단계로"""
        def __init__(self, cog: "MatchCog", game: Game):
            super().__init__(timeout=300)
            self.cog = cog
            self.game = game

        @discord.ui.button(label="🟦 1팀", style=discord.ButtonStyle.primary)
        async def pick_t1(self, interaction: discord.Interaction, button: Button):
            await interaction.response.edit_message(content="교체할 1팀 멤버와 투입 멤버를 선택하세요.", view=self.cog.ReplacePickView(self.cog, self.game, team_no=1))

        @discord.ui.button(label="🟥 2팀", style=discord.ButtonStyle.danger)
        async def pick_t2(self, interaction: discord.Interaction, button: Button):
            await interaction.response.edit_message(content="교체할 2팀 멤버와 투입 멤버를 선택하세요.", view=self.cog.ReplacePickView(self.cog, self.game, team_no=2))

        @discord.ui.button(label="뒤로", style=discord.ButtonStyle.secondary)
        async def back(self, interaction: discord.Interaction, button: Button):
            await interaction.response.edit_message(content="관리 메뉴로 돌아갑니다.", view=self.cog.AdminMenuView(self.cog, self.game))

    class ReplacePickView(View):
        """
        팀 내 '내보낼 멤버' 1명 + '투입 멤버' 1명 선택
        - 투입 후보: 상대 팀원(스왑), 대기열, (있다면) 팀 미배정 참가자
        - 팀장은 교체 가능(투입/내보낼 대상에 포함)하지만, '킥'이 아닌 '교체'를 통해 처리하도록 유도
        """
        def __init__(self, cog: "MatchCog", game: Game, team_no: int):
            super().__init__(timeout=300)
            self.cog = cog
            self.game = game
            self.team_no = team_no

            guild = self.cog.bot.get_guild(self.cog.bot.guilds[0].id) if self.cog.bot.guilds else None

            def label_of(uid: int) -> str:
                m = guild.get_member(uid) if guild else None
                return m.display_name if m else str(uid)

            # 내보낼 멤버(팀원 전원 선택 가능, 팀장 포함)
            out_opts = [
                discord.SelectOption(label=label_of(uid), value=str(uid))
                for uid in self.game.teams[self.team_no]
            ] or [discord.SelectOption(label="팀에 멤버가 없습니다.", value="-1")]

            # 투입 멤버 후보
            other = 2 if self.team_no == 1 else 1
            in_opts: List[discord.SelectOption] = []

            # 상대 팀원(스왑)
            for uid in self.game.teams[other]:
                in_opts.append(discord.SelectOption(label=f"[상대팀] {label_of(uid)}", value=f"T{other}:{uid}"))

            # 대기열
            for uid in self.game.waitlist:
                in_opts.append(discord.SelectOption(label=f"[대기] {label_of(uid)}", value=f"W:{uid}"))

            # 팀 미배정(슬롯엔 있으나 팀엔 없는 참가자)
            assigned = set(self.game.teams[1] + self.game.teams[2])
            for uid in self.game.participants:
                if uid not in assigned:
                    in_opts.append(discord.SelectOption(label=f"[미배정] {label_of(uid)}", value=f"P:{uid}"))

            if not in_opts:
                in_opts = [discord.SelectOption(label="투입 가능한 대상이 없습니다.", value="-1")]

            self._out = Select(placeholder="내보낼 멤버", min_values=1, max_values=1, options=out_opts)
            self._in  = Select(placeholder="투입할 멤버", min_values=1, max_values=1, options=in_opts)

            self.add_item(self._out)
            self.add_item(self._in)

        @discord.ui.button(label="교체 실행", style=discord.ButtonStyle.success)
        async def do_replace(self, interaction: discord.Interaction, button: Button):
            if not self.cog._is_host_or_admin(interaction, self.game):
                await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
                return
            if not self._out.values or not self._in.values or self._out.values[0] == "-1" or self._in.values[0] == "-1":
                await interaction.response.send_message("대상을 선택해주세요.", ephemeral=True)
                return

            out_uid = int(self._out.values[0])
            in_token = self._in.values[0]  # 예: "T2:123" / "W:123" / "P:123"
            src, val = in_token.split(":")
            in_uid = int(val)

            # 내보낼 멤버는 현재 팀에서 제거
            if out_uid not in self.game.teams[self.team_no]:
                await interaction.response.send_message("선택한 내보낼 멤버가 팀에 없습니다.", ephemeral=True)
                return
            self.game.teams[self.team_no].remove(out_uid)

            # 투입 후보 처리
            if src.startswith("T"):  # 상대 팀 스왑
                other = int(src[1])
                if in_uid not in self.game.teams[other]:
                    await interaction.response.send_message("상대 팀 멤버가 아닙니다.", ephemeral=True)
                    return
                # 상대 팀에서 빼고 우리 팀에 넣음
                self.game.teams[other].remove(in_uid)
                self.game.teams[self.team_no].append(in_uid)
                # 내보낸 멤버는 상대 팀으로
                self.game.teams[other].append(out_uid)

            elif src == "W":  # 대기열 → 우리 팀, 내보낸 멤버는 대기열 뒤로
                if in_uid not in self.game.waitlist:
                    await interaction.response.send_message("대기열에 없는 사용자입니다.", ephemeral=True)
                    return
                self.game.remove_from_waitlist(in_uid)
                self.game.teams[self.team_no].append(in_uid)
                # out_uid를 대기열로 보냄(꽉 찼으면 제거)
                if len(self.game.waitlist) < 5:
                    self.game.waitlist.append(out_uid)
                else:
                    # 슬롯에서 제거 및 공석 승격
                    freed = self.game.remove_from_slot(out_uid)
                    if freed is not None:
                        self.game.autopromote_waiter(freed)

            elif src == "P":  # 미배정 → 우리 팀, out은 그대로 참가 상태 유지(미배정으로 남김)
                # in_uid가 슬롯에 있는지 보장
                if in_uid not in self.game.participants:
                    await interaction.response.send_message("참가 상태가 아닌 사용자입니다.", ephemeral=True)
                    return
                self.game.teams[self.team_no].append(in_uid)
                # out_uid는 팀에서 빠졌으니 미배정(참가자) 상태로 남음

            else:
                await interaction.response.send_message("알 수 없는 유형입니다.", ephemeral=True)
                return

            # 팀장 마커 유지(별도 처리 없음). 팀장 교체를 원하면 스왑을 사용하거나 별도 로직 확장.
            await self.cog._refresh_lobby(interaction.guild, self.game)
            await self.cog._refresh_team_status(interaction.guild, self.game)
            await interaction.response.edit_message(content="교체를 완료했습니다.", view=self.cog.AdminMenuView(self.cog, self.game))

        @discord.ui.button(label="뒤로", style=discord.ButtonStyle.secondary)
        async def back(self, interaction: discord.Interaction, button: Button):
            await interaction.response.edit_message(content="팀 선택으로 돌아갑니다.", view=self.cog.ReplaceTeamPickView(self.cog, self.game))

    class ResultView(View):
        def __init__(self, cog: "MatchCog", game: Game):
            super().__init__(timeout=None)
            self.cog = cog
            self.game = game

        @discord.ui.button(label="1팀 승리", style=discord.ButtonStyle.primary)
        async def team1_win(self, interaction: discord.Interaction, button: Button):
            if not self.cog._is_host_or_admin(interaction, self.game):
                await interaction.response.send_message("개최자 또는 관리자만 결과를 기록할 수 있습니다.", ephemeral=True)
                return
            if self.game.finished:
                await interaction.response.send_message("이미 결과가 기록되었습니다.", ephemeral=True)
                return

            uids_team1 = list(set([self.game.team_captains[0]] + self.game.teams[1]))
            uids_team2 = list(set([self.game.team_captains[1]] + self.game.teams[2]))

            # 전적 + 포인트
            for uid in uids_team1:
                update_result_dual(str(uid), True)
                add_points(uid, WIN_REWARD)
            for uid in uids_team2:
                update_result_dual(str(uid), False)
                add_points(uid, LOSE_REWARD)

            self._lock_buttons()
            embed = interaction.message.embeds[0]
            embed.add_field(name="결과", value="✅ 1팀 승리!", inline=False)
            embed.add_field(
                name="포인트 지급",
                value=f"🟦 1팀 +{WIN_REWARD} {CURRENCY} / 🟥 2팀 +{LOSE_REWARD} {CURRENCY}",
                inline=False
            )
            await interaction.response.edit_message(embed=embed, view=self)

        @discord.ui.button(label="2팀 승리", style=discord.ButtonStyle.danger)
        async def team2_win(self, interaction: discord.Interaction, button: Button):
            if not self.cog._is_host_or_admin(interaction, self.game):
                await interaction.response.send_message("개최자 또는 관리자만 결과를 기록할 수 있습니다.", ephemeral=True)
                return
            if self.game.finished:
                await interaction.response.send_message("이미 결과가 기록되었습니다.", ephemeral=True)
                return

            uids_team1 = list(set([self.game.team_captains[0]] + self.game.teams[1]))
            uids_team2 = list(set([self.game.team_captains[1]] + self.game.teams[2]))

            # 전적 + 포인트
            for uid in uids_team1:
                update_result_dual(str(uid), False)
                add_points(uid, LOSE_REWARD)
            for uid in uids_team2:
                update_result_dual(str(uid), True)
                add_points(uid, WIN_REWARD)

            self._lock_buttons()
            embed = interaction.message.embeds[0]
            embed.add_field(name="결과", value="✅ 2팀 승리!", inline=False)
            embed.add_field(
                name="포인트 지급",
                value=f"🟦 1팀 +{LOSE_REWARD} {CURRENCY} / 🟥 2팀 +{WIN_REWARD} {CURRENCY}",
                inline=False
            )
            await interaction.response.edit_message(embed=embed, view=self)

        @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
        async def cancel_game(self, interaction: discord.Interaction, button: Button):
            if not self.cog._is_host_or_admin(interaction, self.game):
                await interaction.response.send_message("개최자 또는 관리자만 취소할 수 있습니다.", ephemeral=True)
                return
            if self.game.finished:
                await interaction.response.send_message("이미 결과가 기록되었습니다.", ephemeral=True)
                return

            self._lock_buttons()
            embed = interaction.message.embeds[0]
            embed.add_field(name="결과", value="❌ 게임이 취소되었습니다.", inline=False)
            await interaction.response.edit_message(embed=embed, view=self)

        def _lock_buttons(self):
            self.game.finished = True
            for child in self.children:
                child.disabled = True
