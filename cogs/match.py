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
        self.team_captains: List[int] = []            # [캡틴1, 캡틴2]
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
    """내전(슬롯 모집/대기/드롭다운 팀장-드래프트/결과 기록/OPGG) 전담 Cog"""

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
            # 캡틴은 별도 표시 (드래프트 진행 중일 때 teams에 이미 포함됨)
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

    # ========= 드롭다운: 팀장 선택 → 드래프트 =========
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

        class CaptainSelectView(View):
            def __init__(self):
                super().__init__(timeout=None)

            @discord.ui.select(placeholder="팀장을 선택하세요 (두 명)", min_values=2, max_values=2, options=options)
            async def select_callback(self, inner_interaction: discord.Interaction, select: Select):
                if inner_interaction.user.id != game.host_id and not inner_interaction.user.guild_permissions.manage_guild:
                    await inner_interaction.response.send_message("팀장 선택은 개최자 또는 관리자만 가능합니다.", ephemeral=True)
                    return

                game.team_captains = [int(uid) for uid in select.values]

                embed = discord.Embed(
                    title="팀장 선택 완료",
                    description="팀장이 선택되었습니다! 팀 구성을 시작합니다.",
                    color=0x2F3136
                )
                await inner_interaction.response.edit_message(embed=embed, view=None)
                await cog.start_draft(inner_interaction, game)

        embed = discord.Embed(
            title="팀장 선택",
            description="티어 순으로 정렬된 명단에서 팀장을 선택해주세요:",
            color=0x2F3136
        )
        await interaction.channel.send(embed=embed, view=CaptainSelectView())

    async def start_draft(self, interaction: discord.Interaction, game: Game):
        players = [uid for uid in game.participants if uid not in game.team_captains]
        random.shuffle(players)
        first = random.choice([1, 2])

        random.shuffle(game.team_captains)
        game.teams[1].append(game.team_captains[0])
        game.teams[2].append(game.team_captains[1])

        game.pick_order = [1, 2, 2, 1, 1, 2, 2, 1] if first == 1 else [2, 1, 1, 2, 2, 1, 1, 2]

        guild = interaction.guild
        assert guild is not None

        c1 = guild.get_member(game.team_captains[0]).display_name
        c2 = guild.get_member(game.team_captains[1]).display_name
        embed = discord.Embed(title=f"내전 #{game.id} 팀 구성 현황", color=0x2F3136)
        embed.add_field(name="1팀", value=f"- {c1}", inline=True)
        embed.add_field(name="2팀", value=f"- {c2}", inline=True)

        game.team_status_message = await interaction.channel.send(embed=embed)
        await self.send_draft_ui(interaction.channel, game, players)

    async def send_draft_ui(self, channel: discord.TextChannel, game: Game, available: List[int]):
        if not available or game.draft_turn >= len(game.pick_order):
            await self.finish_teams(channel, game)
            return

        team_num = game.pick_order[game.draft_turn]
        captain_id = game.team_captains[team_num - 1]
        guild = channel.guild

        def create_team_embed():
            team1_members = [guild.get_member(u).display_name for u in game.teams[1]]
            team2_members = [guild.get_member(u).display_name for u in game.teams[2]]
            embed = discord.Embed(title=f"내전 #{game.id} 팀 구성 현황", color=0x2F3136)
            embed.add_field(name="1팀", value="\n".join(f"- {n}" for n in team1_members) or "-", inline=True)
            embed.add_field(name="2팀", value="\n".join(f"- {n}" for n in team2_members) or "-", inline=True)
            return embed

        cog = self

        class DraftView(View):
            def __init__(self):
                super().__init__(timeout=None)

            @discord.ui.select(
                placeholder=f"{team_num}팀 픽 대상 선택",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(
                        label=guild.get_member(uid).display_name,
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

                await game.team_status_message.edit(embed=create_team_embed())
                await interaction.message.delete()
                await cog.send_draft_ui(channel, game, available)

            @discord.ui.button(label="↩ 되돌리기", style=discord.ButtonStyle.secondary)
            async def undo_pick(self, interaction: discord.Interaction, button: Button):
                if interaction.user.id != game.host_id and not interaction.user.guild_permissions.manage_guild:
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

                await game.team_status_message.edit(embed=create_team_embed())

                try:
                    await interaction.message.delete()
                except:
                    pass
                await cog.send_draft_ui(channel, game, available)

        embed = discord.Embed(
            title=f"{team_num}팀 팀원 선택",
            description=f"{guild.get_member(captain_id).display_name}님, 팀원을 선택하세요:",
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
        """모집 메시지의 기본 컨트롤러"""
        def __init__(self, cog: "MatchCog", game: Game):
            super().__init__(timeout=None)
            self.cog = cog
            self.game = game

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
