# cogs/minigames.py
import random
from typing import Optional, List
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands

from utils.stats import (
    load_stats, save_stats, ensure_user,  # ⬅️ per-game 쿨타임 저장/로드
    get_points, spend_points, add_points, format_num
)

CURRENCY = "Point"

# ── 쿨타임(각 미니게임별로 적용) ───────────────────────────────────────────────
MINIGAME_COOLDOWN_HOURS = 3

# ── 동전던지기 (스케일 반영) ────────────────────────────────────────────────
COIN_ENTRY_COST = 60
COIN_REWARD_ON_HIT = 270
COIN_EXTRA_LOSS_ON_MISS = 180
COIN_MIN_BALANCE_REQUIRED = COIN_ENTRY_COST + COIN_EXTRA_LOSS_ON_MISS  # 240

# ── 주사위 1회 (5배) ────────────────────────────────────────────────────────
DICE1_ENTRY_COST = 10
DICE1_MIN_BALANCE_REQUIRED = 10
DICE1_REWARD_1HIT = 60

# ── 주사위 2회 (4배) ────────────────────────────────────────────────────────
DICE2_ENTRY_COST = 40
DICE2_FAIL_TOTAL_LOSS = 40
DICE2_MIN_BALANCE_REQUIRED = 40
DICE2_REWARD_ANY = 72
DICE2_REWARD_BOTH = 720

# ── 주사위 3회 (지정값) ─────────────────────────────────────────────────────
DICE3_ENTRY_COST = 100
DICE3_FAIL_TOTAL_LOSS = 1000
DICE3_MIN_BALANCE_REQUIRED = 1000
DICE3_REWARD_1 = 0       # ✅ 1개 성공 보상 0
DICE3_REWARD_2 = 1000
DICE3_REWARD_3 = 2500

DICE_CHOICES = ["1", "2", "3", "4", "5", "6"]


class MinigamesCog(commands.Cog):
    """
    .미니게임  → 버튼으로 선택 (**각 미니게임별 쿨타임 3시간**)
      - 동전던지기: 시작 60P, 맞추면 +270P, 틀리면 추가 -180P (총 -240) / 최소 보유 240P
      - 주사위(1회): 시작 10P, 맞추면 +60P
      - 주사위(2회): 시작 40P, 1개 +72P / 2개 +720P / 둘 다 실패 총 -40P(추가 차감 없음)
      - 주사위(3회): 시작 100P, 0개 실패 총 -1000P(추가 -900) / 1개 +0P / 2개 +1000P / 3개 +2500P

    .미니게임 초기화 @유저  → @유저의 미니게임 쿨타임을 초기화합니다. (관리자만)
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ──────────────────────────────────────────
    # 쿨타임 유틸 (per-game)
    # ──────────────────────────────────────────
    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _get_cd_map(user_id: int) -> dict:
        stats = load_stats()
        rec = ensure_user(stats, str(user_id))
        cdmap = rec.get("minigame_cooldowns")
        if not isinstance(cdmap, dict):
            cdmap = {}
            rec["minigame_cooldowns"] = cdmap
            save_stats(stats)
        return cdmap

    @staticmethod
    def _get_last_minigame(user_id: int, game_key: str) -> Optional[datetime]:
        cdmap = MinigamesCog._get_cd_map(user_id)
        iso = cdmap.get(game_key)
        if not iso:
            return None
        try:
            return datetime.fromisoformat(iso)
        except Exception:
            return None

    @staticmethod
    def _set_last_minigame(user_id: int, game_key: str, when: Optional[datetime] = None) -> None:
        stats = load_stats()
        rec = ensure_user(stats, str(user_id))
        cdmap = rec.get("minigame_cooldowns")
        if not isinstance(cdmap, dict):
            cdmap = {}
            rec["minigame_cooldowns"] = cdmap
        cdmap[game_key] = (when or MinigamesCog._now_utc()).isoformat()
        save_stats(stats)

    @staticmethod
    def _reset_all_cooldowns(user_id: int) -> None:
        """해당 유저의 모든 미니게임 쿨타임 초기화"""
        stats = load_stats()
        rec = ensure_user(stats, str(user_id))
        rec["minigame_cooldowns"] = {}
        save_stats(stats)

    @staticmethod
    def _cooldown_remaining(user_id: int, game_key: str) -> Optional[timedelta]:
        last = MinigamesCog._get_last_minigame(user_id, game_key)
        if not last:
            return None
        cd = timedelta(hours=MINIGAME_COOLDOWN_HOURS)
        now = MinigamesCog._now_utc()
        if now - last >= cd:
            return None
        return cd - (now - last)

    @staticmethod
    def _format_td(remain: timedelta) -> str:
        total_seconds = int(remain.total_seconds())
        hours = total_seconds // 3600
        mins = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        parts = []
        if hours:
            parts.append(f"{hours}시간")
        if mins:
            parts.append(f"{mins}분")
        parts.append(f"{secs}초")
        return " ".join(parts)

    # ──────────────────────────────────────────
    # 명령 그룹: .미니게임
    # ──────────────────────────────────────────
    @commands.group(name="미니게임", invoke_without_command=True)
    async def minigames_command(self, ctx: commands.Context):
        """서브커맨드 없이 호출되면 메뉴를 띄웁니다."""
        uid = ctx.author.id
        # 각 버튼 상태를 미리 보여주기 위한 남은 시간 조회
        r_coin  = self._cooldown_remaining(uid, "coin")
        r_d1    = self._cooldown_remaining(uid, "dice1")
        r_d2    = self._cooldown_remaining(uid, "dice2")
        r_d3    = self._cooldown_remaining(uid, "dice3")

        def stat(remain): return "✅ 가능" if not remain else f"⏳ {self._format_td(remain)} 남음"

        # 동적으로 총손실/추가손실 문구 구성
        d2_extra = DICE2_FAIL_TOTAL_LOSS - DICE2_ENTRY_COST
        d2_fail_str = f"총 -{DICE2_FAIL_TOTAL_LOSS}{CURRENCY}" + (f" (추가 -{d2_extra}{CURRENCY})" if d2_extra > 0 else " (추가 차감 없음)")
        d3_extra = DICE3_FAIL_TOTAL_LOSS - DICE3_ENTRY_COST
        d3_fail_str = f"총 -{DICE3_FAIL_TOTAL_LOSS}{CURRENCY} (추가 -{d3_extra}{CURRENCY})"

        total_coin_loss = COIN_ENTRY_COST + COIN_EXTRA_LOSS_ON_MISS

        desc = (
            "아래에서 미니게임을 선택하세요! (**각 게임별 쿨타임 3시간**)\n\n"
            f"• **동전던지기** — 시작 {COIN_ENTRY_COST}{CURRENCY}, 맞추면 +{COIN_REWARD_ON_HIT}{CURRENCY}, 틀리면 추가 -{COIN_EXTRA_LOSS_ON_MISS}{CURRENCY} (총 -{total_coin_loss}{CURRENCY}) — {stat(r_coin)}\n"
            f"• **주사위(1회)** — 시작 {DICE1_ENTRY_COST}{CURRENCY}, 맞추면 +{DICE1_REWARD_1HIT}{CURRENCY} — {stat(r_d1)}\n"
            f"• **주사위(2회)** — 시작 {DICE2_ENTRY_COST}{CURRENCY}, 1개 +{DICE2_REWARD_ANY}{CURRENCY} / 2개 +{DICE2_REWARD_BOTH}{CURRENCY} / 둘 다 실패 {d2_fail_str} — {stat(r_d2)}\n"
            f"• **주사위(3회)** — 시작 {DICE3_ENTRY_COST}{CURRENCY}, 0개 실패 {d3_fail_str} / 1개 +{DICE3_REWARD_1}{CURRENCY} / 2개 +{DICE3_REWARD_2}{CURRENCY} / 3개 +{DICE3_REWARD_3}{CURRENCY} — {stat(r_d3)}\n"
        )
        embed = discord.Embed(title="🎲 미니게임", description=desc, color=discord.Color.blurple())
        await ctx.send(embed=embed, view=self.MenuView(author_id=uid, cog=self))

    # ──────────────────────────────────────────
    # 관리자 서브커맨드: .미니게임 초기화 @유저
    # ──────────────────────────────────────────
    @minigames_command.command(name="초기화")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def reset_cooldown(self, ctx: commands.Context, member: discord.Member):
        """
        .미니게임 초기화 @유저
        @유저의 미니게임 쿨타임을 초기화합니다. (관리자 전용)
        """
        self._reset_all_cooldowns(member.id)
        await ctx.reply(f"✅ {member.mention}의 **모든 미니게임 쿨타임**을 초기화했습니다.")

    @reset_cooldown.error
    async def reset_cooldown_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("❌ 이 명령은 **관리자만** 사용할 수 있어요.")
        elif isinstance(error, commands.BadArgument):
            await ctx.reply("사용법: `.미니게임 초기화 @유저` (멤버를 멘션해 주세요)")
        else:
            await ctx.reply(f"처리 중 오류가 발생했습니다: {error}")

    # 공통: 뷰 베이스(작성자만 조작)
    class BaseView(discord.ui.View):
        def __init__(self, author_id: int, timeout: Optional[float] = 120):
            super().__init__(timeout=timeout)
            self.author_id = author_id

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.author_id:
                await interaction.response.send_message("이 패널은 해당 사용자만 조작할 수 있어요.", ephemeral=True)
                return False
            return True

        async def on_timeout(self) -> None:
            for item in self.children:
                try:
                    item.disabled = True
                except Exception:
                    pass

    class MenuView(BaseView):
        def __init__(self, author_id: int, cog: "MinigamesCog"):
            super().__init__(author_id=author_id, timeout=120)
            self.cog = cog

        @discord.ui.button(label="동전던지기", style=discord.ButtonStyle.primary)
        async def coin_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            # ⛔ 해당 게임 쿨타임 체크
            remain = self.cog._cooldown_remaining(interaction.user.id, "coin")
            if remain:
                await interaction.response.send_message(
                    f"⏳ 동전던지기는 **{self.cog._format_td(remain)}** 후에 이용 가능해요.",
                    ephemeral=True
                )
                return

            user = interaction.user
            if get_points(user.id) < COIN_MIN_BALANCE_REQUIRED:
                await interaction.response.send_message(
                    f"잔액이 부족합니다. 최소 **{COIN_MIN_BALANCE_REQUIRED} {CURRENCY}** 필요해요.",
                    ephemeral=True
                )
                return

            if not spend_points(user.id, COIN_ENTRY_COST):
                await interaction.response.send_message("잔액이 부족해요.", ephemeral=True)
                return

            # ✅ 해당 게임 쿨타임 시작
            MinigamesCog._set_last_minigame(user.id, "coin")

            bal = get_points(user.id)
            total_loss = COIN_ENTRY_COST + COIN_EXTRA_LOSS_ON_MISS
            desc = (
                f"**동전던지기 시작!** (현재 보유: {format_num(bal)} {CURRENCY})\n\n"
                f"규칙:\n"
                f"• 시작 시 **{COIN_ENTRY_COST} {CURRENCY}** 차감\n"
                f"• 맞추면 **+{COIN_REWARD_ON_HIT} {CURRENCY}**\n"
                f"• 틀리면 추가 **-{COIN_EXTRA_LOSS_ON_MISS} {CURRENCY}** (총 -{total_loss} {CURRENCY})\n\n"
                "아래에서 **앞/뒤** 를 선택하세요."
            )
            embed = discord.Embed(title="🪙 동전던지기", description=desc, color=discord.Color.gold())
            await interaction.response.send_message(embed=embed, view=self.cog.CoinView(author_id=user.id))

        @discord.ui.button(label="주사위(1회)", style=discord.ButtonStyle.success)
        async def dice1_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self.cog._start_dice(interaction, mode=1)

        @discord.ui.button(label="주사위(2회)", style=discord.ButtonStyle.secondary)
        async def dice2_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self.cog._start_dice(interaction, mode=2)

        @discord.ui.button(label="주사위(3회)", style=discord.ButtonStyle.danger)
        async def dice3_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self.cog._start_dice(interaction, mode=3)

    # ──────────────────────────────────────────
    # 동전던지기
    # ──────────────────────────────────────────
    class CoinView(BaseView):
        def __init__(self, author_id: int):
            super().__init__(author_id=author_id, timeout=60)

        @discord.ui.button(label="앞", style=discord.ButtonStyle.primary)
        async def heads(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._resolve(interaction, guess="앞")

        @discord.ui.button(label="뒤", style=discord.ButtonStyle.primary)
        async def tails(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._resolve(interaction, guess="뒤")

        @discord.ui.button(label="포기", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(content="게임을 종료했습니다.", view=self)

        async def _resolve(self, interaction: discord.Interaction, guess: str):
            result = random.choice(["앞", "뒤"])
            won = (guess == result)

            if won:
                new_bal = add_points(interaction.user.id, COIN_REWARD_ON_HIT)
                msg = f"정답은 **{result}**! 🎉 **+{COIN_REWARD_ON_HIT} {CURRENCY}**"
                color = discord.Color.green()
            else:
                spend_points(interaction.user.id, COIN_EXTRA_LOSS_ON_MISS)  # 추가 -180
                new_bal = get_points(interaction.user.id)
                msg = f"정답은 **{result}**! 😵 **-{COIN_EXTRA_LOSS_ON_MISS} {CURRENCY}** 추가 차감"
                color = discord.Color.red()

            for item in self.children:
                item.disabled = True

            desc = (
                f"당신의 선택: **{guess}**\n"
                f"{msg}\n\n"
                f"현재 보유: **{format_num(new_bal)} {CURRENCY}**"
            )
            embed = discord.Embed(title="🪙 동전던지기 결과", description=desc, color=color)
            await interaction.response.edit_message(embed=embed, view=self)

    # ──────────────────────────────────────────
    # 주사위: 공통 시작/진행
    # ──────────────────────────────────────────
    async def _start_dice(self, interaction: discord.Interaction, mode: int):
        game_key = f"dice{mode}"

        # ⛔ 해당 주사위 모드의 쿨타임 체크
        remain = self._cooldown_remaining(interaction.user.id, game_key)
        if remain:
            await interaction.response.send_message(
                f"⏳ 주사위({mode}회)는 **{self._format_td(remain)}** 후에 이용 가능해요.",
                ephemeral=True
            )
            return

        user = interaction.user
        if mode == 1:
            need = DICE1_MIN_BALANCE_REQUIRED
            cost = DICE1_ENTRY_COST
        elif mode == 2:
            need = DICE2_MIN_BALANCE_REQUIRED
            cost = DICE2_ENTRY_COST
        else:
            need = DICE3_MIN_BALANCE_REQUIRED
            cost = DICE3_ENTRY_COST

        if get_points(user.id) < need:
            await interaction.response.send_message(
                f"잔액이 부족합니다. 최소 **{need} {CURRENCY}** 필요해요.", ephemeral=True
            )
            return

        if not spend_points(user.id, cost):
            await interaction.response.send_message("잔액이 부족해요.", ephemeral=True)
            return

        # ✅ 해당 주사위 모드 쿨타임 시작
        self._set_last_minigame(user.id, game_key)

        bal = get_points(user.id)
        if mode == 1:
            desc = (
                f"**주사위(1회)** 시작! (현재 보유: {format_num(bal)} {CURRENCY})\n"
                f"• 시작 시 **{cost} {CURRENCY}** 차감\n"
                f"• 맞추면 **+{DICE1_REWARD_1HIT} {CURRENCY}**\n\n"
                "나올 눈(1~6) 하나를 선택하세요."
            )
            view = self.DiceView(author_id=user.id, mode=1, cog=self)
        elif mode == 2:
            d2_extra = DICE2_FAIL_TOTAL_LOSS - DICE2_ENTRY_COST
            fail_line = f"• 둘 다 실패: 총 **-{DICE2_FAIL_TOTAL_LOSS} {CURRENCY}**" + (f" (추가 -{d2_extra} {CURRENCY})" if d2_extra > 0 else " (추가 차감 없음)")
            desc = (
                f"**주사위(2회)** 시작! (현재 보유: {format_num(bal)} {CURRENCY})\n"
                f"• 시작 시 **{cost} {CURRENCY}** 차감\n"
                f"• 하나라도 성공: **+{DICE2_REWARD_ANY} {CURRENCY}**\n"
                f"• 둘 다 성공: **+{DICE2_REWARD_BOTH} {CURRENCY}**\n"
                f"{fail_line}\n\n"
                "첫 번째로 나올 눈을 선택하세요."
            )
            view = self.DiceView(author_id=user.id, mode=2, cog=self)
        else:
            d3_extra = DICE3_FAIL_TOTAL_LOSS - DICE3_ENTRY_COST
            desc = (
                f"**주사위(3회)** 시작! (현재 보유: {format_num(bal)} {CURRENCY})\n"
                f"• 시작 시 **{cost} {CURRENCY}** 차감\n"
                f"• 0개 성공(실패): 총 **-{DICE3_FAIL_TOTAL_LOSS} {CURRENCY}** (추가 -{d3_extra} {CURRENCY})\n"
                f"• 1개 성공: **+{DICE3_REWARD_1} {CURRENCY}**\n"
                f"• 2개 성공: **+{DICE3_REWARD_2} {CURRENCY}**\n"
                f"• 3개 성공: **+{DICE3_REWARD_3} {CURRENCY}**\n\n"
                "첫 번째로 나올 눈을 선택하세요."
            )
            view = self.DiceView(author_id=user.id, mode=3, cog=self)

        embed = discord.Embed(title="🎲 주사위 미니게임", description=desc, color=discord.Color.dark_purple())
        await interaction.response.send_message(embed=embed, view=view)

    class DiceView(BaseView):
        """
        mode=1 → 한 번 예측
        mode=2 → 두 번 순차 예측
        mode=3 → 세 번 순차 예측
        """
        def __init__(self, author_id: int, mode: int, cog: "MinigamesCog"):
            super().__init__(author_id=author_id, timeout=180)
            self.cog = cog
            self.mode = mode
            self.guesses: List[int] = []  # 선택한 예측들(정수 1~6)
            # 예측 버튼들 생성
            for i, face in enumerate(DICE_CHOICES, start=1):
                style = discord.ButtonStyle.primary if i <= 3 else discord.ButtonStyle.secondary
                self.add_item(MinigamesCog.DiceFaceButton(face_label=face, style=style))
            self.add_item(MinigamesCog.DiceCancelButton())

        async def handle_guess(self, interaction: discord.Interaction, face_value: int):
            # 기록
            self.guesses.append(face_value)

            need = 1 if self.mode == 1 else (2 if self.mode == 2 else 3)
            if len(self.guesses) < need:
                nth_names = ["첫", "두", "세"]
                nth = nth_names[len(self.guesses)] if len(self.guesses) < 3 else f"{len(self.guesses)+1}"
                await interaction.response.edit_message(
                    embed=self._progress_embed(interaction, prompt=f"{nth}번째로 나올 눈을 선택하세요."),
                    view=self
                )
                return

            # 모두 고르면 해석/정산
            await self._resolve(interaction)

        async def _resolve(self, interaction: discord.Interaction):
            rolls = []
            for _ in range(1 if self.mode == 1 else (2 if self.mode == 2 else 3)):
                rolls.append(random.randint(1, 6))

            # 성공 판단(순서 고려)
            success_count = 0
            for i, guess in enumerate(self.guesses):
                if i < len(rolls) and guess == rolls[i]:
                    success_count += 1

            # 정산
            user_id = interaction.user.id
            color = discord.Color.blurple()
            reward_text = ""
            if self.mode == 1:
                if success_count == 1:
                    new_bal = add_points(user_id, DICE1_REWARD_1HIT)
                    reward_text = f"정답! **+{DICE1_REWARD_1HIT} {CURRENCY}**"
                    color = discord.Color.green()
                else:
                    new_bal = get_points(user_id)
                    reward_text = "아쉽네요! 추가 차감은 없습니다."
                    color = discord.Color.red()
            elif self.mode == 2:
                if success_count == 0:
                    extra = DICE2_FAIL_TOTAL_LOSS - DICE2_ENTRY_COST  # 0
                    if extra > 0:
                        spend_points(user_id, extra)
                    new_bal = get_points(user_id)
                    if extra > 0:
                        reward_text = f"둘 다 틀렸어요. 추가 **-{extra} {CURRENCY}** (총 -{DICE2_FAIL_TOTAL_LOSS})"
                    else:
                        reward_text = f"둘 다 틀렸어요. 총 **-{DICE2_FAIL_TOTAL_LOSS} {CURRENCY}** (추가 차감 없음)"
                    color = discord.Color.red()
                elif success_count == 1:
                    new_bal = add_points(user_id, DICE2_REWARD_ANY)
                    reward_text = f"하나 성공! **+{DICE2_REWARD_ANY} {CURRENCY}**"
                    color = discord.Color.green()
                else:  # 2개 성공
                    new_bal = add_points(user_id, DICE2_REWARD_BOTH)
                    reward_text = f"두 개 모두 성공! **+{DICE2_REWARD_BOTH} {CURRENCY}** 🎉"
                    color = discord.Color.green()
            else:  # mode == 3
                if success_count == 0:
                    extra = DICE3_FAIL_TOTAL_LOSS - DICE3_ENTRY_COST  # 900
                    if extra > 0:
                        spend_points(user_id, extra)
                    new_bal = get_points(user_id)
                    reward_text = f"모두 틀렸어요. 추가 **-{extra} {CURRENCY}** (총 -{DICE3_FAIL_TOTAL_LOSS})"
                    color = discord.Color.red()
                elif success_count == 1:
                    if DICE3_REWARD_1 > 0:
                        new_bal = add_points(user_id, DICE3_REWARD_1)
                        reward_text = f"1개 성공! **+{DICE3_REWARD_1} {CURRENCY}**"
                    else:
                        new_bal = get_points(user_id)
                        reward_text = f"1개 성공! 보상 없음 (**+0 {CURRENCY}**)"
                    color = discord.Color.green()
                elif success_count == 2:
                    new_bal = add_points(user_id, DICE3_REWARD_2)
                    reward_text = f"2개 성공! **+{DICE3_REWARD_2} {CURRENCY}** 🎉"
                    color = discord.Color.green()
                else:  # 3개 성공
                    new_bal = add_points(user_id, DICE3_REWARD_3)
                    reward_text = f"3개 모두 성공! **+{DICE3_REWARD_3} {CURRENCY}** 🏆"
                    color = discord.Color.green()

            # 버튼 비활성화
            for item in self.children:
                item.disabled = True

            # 결과 표시
            guess_str = ", ".join(str(g) for g in self.guesses)
            roll_str = ", ".join(str(r) for r in rolls)
            desc = (
                f"당신의 예측: **{guess_str}**\n"
                f"실제 결과: **{roll_str}**\n"
                f"성공 개수: **{success_count}**\n\n"
                f"{reward_text}\n\n"
                f"현재 보유: **{format_num(new_bal)} {CURRENCY}**"
            )
            embed = discord.Embed(title="🎲 주사위 결과", description=desc, color=color)
            await interaction.response.edit_message(embed=embed, view=self)

        def _progress_embed(self, interaction: discord.Interaction, prompt: str) -> discord.Embed:
            chosen = ", ".join(str(x) for x in self.guesses) if self.guesses else "(없음)"
            desc = f"지금까지 선택: **{chosen}**\n\n{prompt}"
            return discord.Embed(title="🎲 주사위 진행 중", description=desc, color=discord.Color.dark_purple())

    # 개별 숫자 버튼(1~6)
    class DiceFaceButton(discord.ui.Button):
        def __init__(self, face_label: str, style: discord.ButtonStyle):
            super().__init__(label=face_label, style=style)

        async def callback(self, interaction: discord.Interaction):
            view = self.view
            if not isinstance(view, MinigamesCog.DiceView):
                await interaction.response.defer()
                return
            val = int(self.label)
            await view.handle_guess(interaction, face_value=val)

    # 취소 버튼
    class DiceCancelButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="포기", style=discord.ButtonStyle.secondary)

        async def callback(self, interaction: discord.Interaction):
            view = self.view
            if isinstance(view, MinigamesCog.DiceView):
                for item in view.children:
                    item.disabled = True
                await interaction.response.edit_message(content="게임을 종료했습니다.", view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(MinigamesCog(bot))
