# cogs/minigames.py
import random
from typing import Optional, List

import discord
from discord.ext import commands

from utils.stats import (
    get_points, spend_points, add_points, format_num
)

CURRENCY = "Point"

COIN_MIN_BALANCE_REQUIRED = 9   # 동전: 실패 시 총 -9이므로 최소 9 보유 요구
COIN_ENTRY_COST = 3
COIN_REWARD_ON_HIT = 9
COIN_EXTRA_LOSS_ON_MISS = 6

DICE1_ENTRY_COST = 2
DICE1_MIN_BALANCE_REQUIRED = 2
DICE1_REWARD_1HIT = 12

DICE2_ENTRY_COST = 5
DICE2_FAIL_TOTAL_LOSS = 10      # 둘 다 실패 시 총 -10 → 시작에 5 차감했으니 추가 -5
DICE2_MIN_BALANCE_REQUIRED = 10
DICE2_REWARD_ANY = 18           # 하나라도 성공
DICE2_REWARD_BOTH = 180         # 둘 다 성공

DICE3_ENTRY_COST = 25
DICE3_FAIL_TOTAL_LOSS = 50      # 전부 실패 시 총 -50 → 시작에 25 차감했으니 추가 -25
DICE3_MIN_BALANCE_REQUIRED = 50
DICE3_REWARD_1 = 72
DICE3_REWARD_2 = 360
DICE3_REWARD_3 = 5400

DICE_CHOICES = ["1", "2", "3", "4", "5", "6"]


class MinigamesCog(commands.Cog):
    """
    .미니게임  → 버튼으로 선택
      - 동전던지기: 시작 시 3P 차감, 맞추면 +9P, 틀리면 추가 -6P (총 -9)
      - 주사위 눈 맞추기
          1회: 2P 차감, 맞추면 +12P (틀리면 추가 차감 없음)
          2회: 5P 차감, 순서대로 두 번 예측
               - 하나라도 성공: +18P
               - 둘 다 성공: +180P
               - 둘 다 실패: 총 -10P (추가 -5)
          3회: 25P 차감, 순서대로 세 번 예측
               - 1개 성공: +72P
               - 2개 성공: +360P
               - 3개 성공: +5400P
               - 전부 실패: 총 -50P (추가 -25)
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ──────────────────────────────────────────
    # 공통: 뷰 유틸
    # ──────────────────────────────────────────
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

    # ──────────────────────────────────────────
    # 엔트리 메뉴
    # ──────────────────────────────────────────
    @commands.command(name="미니게임")
    async def minigames_command(self, ctx: commands.Context):
        desc = (
            "아래에서 미니게임을 선택하세요!\n\n"
            "• **동전던지기** — 시작 3P, 맞추면 +9P, 틀리면 추가 -6P (총 -9)\n"
            "• **주사위(1회)** — 시작 2P, 맞추면 +12P\n"
            "• **주사위(2회)** — 시작 5P, 1개 성공 +18P / 2개 성공 +180P / 모두 실패 총 -10P\n"
            "• **주사위(3회)** — 시작 25P, 1개 +72P / 2개 +360P / 3개 +5400P / 모두 실패 총 -50P\n"
        )
        embed = discord.Embed(title="🎲 미니게임", description=desc, color=discord.Color.blurple())
        await ctx.send(embed=embed, view=self.MenuView(author_id=ctx.author.id, cog=self))

    class MenuView(BaseView):
        def __init__(self, author_id: int, cog: "MinigamesCog"):
            super().__init__(author_id=author_id, timeout=120)
            self.cog = cog

        @discord.ui.button(label="동전던지기", style=discord.ButtonStyle.primary)
        async def coin_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            user = interaction.user
            # 사전 잔액 체크(총 -9 가능)
            if get_points(user.id) < COIN_MIN_BALANCE_REQUIRED:
                await interaction.response.send_message(
                    f"잔액이 부족합니다. 최소 **{COIN_MIN_BALANCE_REQUIRED} {CURRENCY}** 필요해요.",
                    ephemeral=True
                )
                return

            # 시작 비용 3P
            if not spend_points(user.id, COIN_ENTRY_COST):
                await interaction.response.send_message("잔액이 부족해요.", ephemeral=True)
                return

            bal = get_points(user.id)
            desc = (
                f"**동전던지기 시작!** (현재 보유: {format_num(bal)} {CURRENCY})\n\n"
                f"규칙:\n"
                f"• 시작 시 **{COIN_ENTRY_COST} {CURRENCY}** 차감\n"
                f"• 맞추면 **+{COIN_REWARD_ON_HIT} {CURRENCY}**\n"
                f"• 틀리면 추가 **-{COIN_EXTRA_LOSS_ON_MISS} {CURRENCY}** (총 -{COIN_ENTRY_COST + COIN_EXTRA_LOSS_ON_MISS})\n\n"
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
                # 추가 -6 차감(사전 보유 체크로 실패 가능성은 낮음)
                spend_points(interaction.user.id, COIN_EXTRA_LOSS_ON_MISS)
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
            desc = (
                f"**주사위(2회)** 시작! (현재 보유: {format_num(bal)} {CURRENCY})\n"
                f"• 시작 시 **{cost} {CURRENCY}** 차감\n"
                f"• 하나라도 성공: **+{DICE2_REWARD_ANY} {CURRENCY}**\n"
                f"• 둘 다 성공: **+{DICE2_REWARD_BOTH} {CURRENCY}**\n"
                f"• 둘 다 실패: 총 **-{DICE2_FAIL_TOTAL_LOSS} {CURRENCY}** (추가 -{DICE2_FAIL_TOTAL_LOSS - cost})\n\n"
                "첫 번째로 나올 눈을 선택하세요."
            )
            view = self.DiceView(author_id=user.id, mode=2, cog=self)
        else:
            desc = (
                f"**주사위(3회)** 시작! (현재 보유: {format_num(bal)} {CURRENCY})\n"
                f"• 시작 시 **{cost} {CURRENCY}** 차감\n"
                f"• 1개 성공: **+{DICE3_REWARD_1} {CURRENCY}**\n"
                f"• 2개 성공: **+{DICE3_REWARD_2} {CURRENCY}**\n"
                f"• 3개 성공: **+{DICE3_REWARD_3} {CURRENCY}**\n"
                f"• 전부 실패: 총 **-{DICE3_FAIL_TOTAL_LOSS} {CURRENCY}** (추가 -{DICE3_FAIL_TOTAL_LOSS - cost})\n\n"
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
                # 다음 선택 유도
                nth = ["첫", "두", "세"][len(self.guesses)] if len(self.guesses) < 3 else f"{len(self.guesses)+1}"
                await interaction.response.edit_message(
                    embed=self._progress_embed(interaction, prompt=f"{nth}번쨰 로 나올 눈을 선택하세요."),
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
                    # 추가 차감해서 총 -10 되도록
                    extra = DICE2_FAIL_TOTAL_LOSS - DICE2_ENTRY_COST  # 5
                    spend_points(user_id, extra)
                    new_bal = get_points(user_id)
                    reward_text = f"둘 다 틀렸어요. 추가 **-{extra} {CURRENCY}** (총 -{DICE2_FAIL_TOTAL_LOSS})"
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
                    extra = DICE3_FAIL_TOTAL_LOSS - DICE3_ENTRY_COST  # 25
                    spend_points(user_id, extra)
                    new_bal = get_points(user_id)
                    reward_text = f"모두 틀렸어요. 추가 **-{extra} {CURRENCY}** (총 -{DICE3_FAIL_TOTAL_LOSS})"
                    color = discord.Color.red()
                elif success_count == 1:
                    new_bal = add_points(user_id, DICE3_REWARD_1)
                    reward_text = f"1개 성공! **+{DICE3_REWARD_1} {CURRENCY}**"
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
            # 권한 체크는 View.interaction_check에서 이미 처리
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
