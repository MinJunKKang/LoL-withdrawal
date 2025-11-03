# cogs/economy.py
import random
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands
from typing import Optional

from utils.stats import (
    load_stats, save_stats, ensure_user, format_num,
    spend_points, get_points, add_points,
    get_last_gamble, set_last_gamble,
)

CURRENCY = "Point"

class EconomyCog(commands.Cog):
    """
    .지급 @유저 양:n
    .회수 @유저 양:n
    .도박 n (n<=30, 성공 1/3, 쿨타임 12시간/유저별, 실제 베팅 성공시에만 시작)
    .지갑 [@유저]
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --------- helpers ---------
    @staticmethod
    def _parse_amount(amount: str | int) -> Optional[int]:
        if isinstance(amount, int):
            return amount
        s = str(amount).strip()
        if ":" in s:
            s = s.split(":", 1)[1]
        s = "".join(ch for ch in s if ch.isdigit() or ch == "-")
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None

    # --------- 지갑 ---------
    @commands.command(name="지갑")
    async def wallet(self, ctx: commands.Context, member: discord.Member | None = None):
        target = member or ctx.author
        points = get_points(target.id)  # utils.stats가 기본 레코드 보장
        # 미뮤 스타일 텍스트 응답
        await ctx.send(f"{target.mention} 님은 **{format_num(points)} {CURRENCY}**를 보유하고 있어요!")


    # --------- 지급(관리권한 필요) ---------
    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(name="지급")
    async def grant_points(self, ctx: commands.Context, member: discord.Member, amount: str):
        parsed = self._parse_amount(amount)
        if parsed is None or parsed <= 0:
            await ctx.reply("금액 형식이 올바르지 않아요. 예: `.지급 @유저 5000` 또는 `.지급 @유저 양:5000`")
            return

        stats = load_stats()
        rec = ensure_user(stats, str(member.id))
        rec["포인트"] = int(rec.get("포인트", 0)) + parsed
        save_stats(stats)

        embed = discord.Embed(
            title="포인트 지급 완료",
            description=(f"{member.mention} 님에게 **{format_num(parsed)} {CURRENCY}** 지급했습니다.\n"
                         f"현재 보유: **{format_num(rec['포인트'])} {CURRENCY}**"),
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"지급자: {ctx.author.display_name}")
        await ctx.send(embed=embed)

    @grant_points.error
    async def _grant_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("이 명령은 **서버 관리** 권한이 있어야 사용 가능합니다.", delete_after=6)

    # --------- 회수(관리권한 필요) ---------
    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(name="회수")
    async def revoke_points(self, ctx: commands.Context, member: discord.Member, amount: str):
        parsed = self._parse_amount(amount)
        if parsed is None or parsed <= 0:
            await ctx.reply("금액 형식이 올바르지 않아요. 예: `.회수 @유저 5000` 또는 `.회수 @유저 양:5000`")
            return

        if not spend_points(member.id, parsed):
            await ctx.send(f"❌ {member.mention} 님의 잔액이 부족합니다. (요청: {format_num(parsed)} {CURRENCY})")
            return

        current = get_points(member.id)
        embed = discord.Embed(
            title="포인트 회수 완료",
            description=(f"{member.mention} 님에게서 **{format_num(parsed)} {CURRENCY}** 회수했습니다.\n"
                         f"현재 보유: **{format_num(current)} {CURRENCY}**"),
            color=discord.Color.red()
        )
        embed.set_footer(text=f"회수자: {ctx.author.display_name}")
        await ctx.send(embed=embed)

    @revoke_points.error
    async def _revoke_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("이 명령은 **서버 관리** 권한이 있어야 사용 가능합니다.", delete_after=6)

    # --------- 도박 (수동 쿨타임 관리) ---------
    @commands.command(name="도박")
    async def gamble(self, ctx: commands.Context, amount: int):
        """
        사용법: .도박 n   (1 ≤ n ≤ 30)
        - 성공(1/3) 시 2배 지급(베팅액을 먼저 회수한 뒤 2n 지급 → 순이익 +n)
        - 실패(2/3) 시 베팅액 회수
        - 쿨타임: 유저별 12시간 (실제 베팅이 이루어진 경우에만 시작)
        """
        # 입력 검증 (쿨타임 시작 안 함)
        if amount <= 0 or amount > 30:
            await ctx.reply("베팅 금액은 1 ~ 30 사이여야 합니다.")
            return

        # 유저별 쿨타임 체크
        now = datetime.now(timezone.utc)
        last = get_last_gamble(ctx.author.id)
        cooldown = timedelta(hours=12)
        if last and now - last < cooldown:
            remain = cooldown - (now - last)
            hrs = remain.seconds // 3600 + remain.days * 24
            mins = (remain.seconds % 3600) // 60
            secs = remain.seconds % 60
            msg = "쿨타임입니다. "
            if hrs:
                msg += f"{hrs}시간 "
            if mins:
                msg += f"{mins}분 "
            msg += f"{secs}초 후에 다시 시도하세요."
            await ctx.reply(msg, delete_after=8)
            return

        # 잔액 차감 실패 시 쿨타임 시작하지 않음
        if not spend_points(ctx.author.id, amount):
            await ctx.reply(f"잔액이 부족합니다. (보유: {format_num(get_points(ctx.author.id))} {CURRENCY})")
            return

        set_last_gamble(ctx.author.id, now)

        win = random.random() < (1.0 / 3.0)
        if win:
            new_balance = add_points(ctx.author.id, amount * 2)
            result = f"🎉 성공! **{format_num(amount * 2)} {CURRENCY}** 획득"
            color = discord.Color.green()
        else:
            new_balance = get_points(ctx.author.id)  # 이미 amount 회수됨
            result = f"😵 실패! **{format_num(amount)} {CURRENCY}** 회수"
            color = discord.Color.red()

        embed = discord.Embed(
            title="도박 결과",
            description=(f"{ctx.author.mention}\n{result}\n"
                         f"현재 보유: **{format_num(new_balance)} {CURRENCY}**"),
            color=color
        )
        await ctx.send(embed=embed)
