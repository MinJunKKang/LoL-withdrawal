# cogs/economy.py
import random
from datetime import datetime, timezone, timedelta, date
import discord
from discord.ext import commands
from typing import Optional

from utils.stats import (
    load_stats, save_stats, ensure_user, format_num,
    spend_points, get_points, add_points,
    get_last_gamble, set_last_gamble,
)

# ─────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────
CURRENCY = "Point"
COOLDOWN_MINUTES = 3          # 도박 쿨타임: 3분
MAX_BET = 50
SUCCESS_PROB = 0.5            # 1/2 확률

DAILY_REWARD = 30             # 출석 보상
ATTEND_KEY = "출석_최근"        # 유저 레코드에 저장할 키(YYYY-MM-DD)

# ─────────────────────────────────────────────────────────
# Timezone: Asia/Seoul (fallback: UTC+9 fixed offset)
# ─────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    # Python < 3.9 환경 대비
    KST = timezone(timedelta(hours=9))


class EconomyCog(commands.Cog):
    """
    .지급 @유저1 [@유저2 ...] 금액
    .회수 @유저 양:n
    .지갑 [@유저]
    .출석                     (KST 자정 초기화, 1일 1회, 보상 30P)
    .도박 n                   (1 ≤ n ≤ 50, 성공 1/2, 당첨 시 2배, 유저별 쿨타임 3분, 베팅 성공시에만 쿨 시작)
    .도박 초기화 @유저         (관리자 전용, 해당 유저의 도박 쿨타임 초기화)
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
        await ctx.send(f"{target.mention} 님은 **{format_num(points)} {CURRENCY}**를 보유하고 있어요!")

    # --------- 출석 (하루 1회, KST 자정 초기화) ---------
    @commands.command(name="출석")
    async def attendance(self, ctx: commands.Context):
        """
        사용법: .출석
        - Asia/Seoul(UTC+9) 기준 하루 1회, 자정 이후 초기화
        - 보상: 30 Point
        """
        user_id = str(ctx.author.id)
        now_kst = datetime.now(tz=KST)
        today_str = now_kst.date().isoformat()          # 'YYYY-MM-DD'
        next_reset = (now_kst + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        # 만약 지금이 이미 00:00 이후라면 위 계산으로 다음날 00:00이 됨

        stats = load_stats()
        rec = ensure_user(stats, user_id)
        last_attend_str = rec.get(ATTEND_KEY)

        if last_attend_str == today_str:
            # 이미 오늘 출석 완료
            ts = next_reset.strftime("%Y-%m-%d %H:%M KST")
            embed = discord.Embed(
                title="📅 출석 체크",
                description=(
                    f"{ctx.author.mention} 님은 이미 오늘 출석을 완료하셨어요.\n"
                    f"다음 출석 가능 시각: **{ts}**"
                ),
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)
            return

        # 출석 처리
        rec["포인트"] = int(rec.get("포인트", 0)) + DAILY_REWARD
        rec[ATTEND_KEY] = today_str
        save_stats(stats)

        current = rec["포인트"]
        embed = discord.Embed(
            title="✅ 출석 체크 완료",
            description=(
                f"{ctx.author.mention} 님에게 출석 보상 **{format_num(DAILY_REWARD)} {CURRENCY}**가 지급되었습니다!\n"
                f"현재 보유: **{format_num(current)} {CURRENCY}**"
            ),
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    # --------- 지급(관리권한 필요, 여러 명/한 명 모두 지원) ---------
    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(name="지급")
    async def grant_points(
        self,
        ctx: commands.Context,
        members: commands.Greedy[discord.Member],  # 여러 멤버(1명 포함) 멘션을 리스트로 받음
        *,
        amount: str                                 # 멤버들 뒤의 마지막 토큰 전체를 금액으로 파싱
    ):
        """
        사용법:
          .지급 @유저1 5000
          .지급 @유저1 @유저2 ... 5000
          .지급 @유저1 @유저2 ... 양:5000
        - 멘션된 모든 유저에게 동일 금액 지급 (1명만 멘션해도 동작)
        """
        if not members:
            await ctx.reply("지급할 **유저를 1명 이상 멘션**해 주세요. 예) `.지급 @사용자1 5000` 또는 `.지급 @사용자1 @사용자2 5000`")
            return

        parsed = self._parse_amount(amount)
        if parsed is None or parsed <= 0:
            await ctx.reply("금액 형식이 올바르지 않아요. 예: `.지급 @유저1 5000` 또는 `.지급 @유저1 @유저2 양:5000`")
            return

        # 중복 멘션 제거
        unique_members = []
        seen_ids = set()
        for m in members:
            if m.id not in seen_ids:
                unique_members.append(m)
                seen_ids.add(m.id)

        # 일괄 지급
        stats = load_stats()
        for member in unique_members:
            rec = ensure_user(stats, str(member.id))
            rec["포인트"] = int(rec.get("포인트", 0)) + parsed
        save_stats(stats)

        # 결과 메시지
        mentions = ", ".join(m.mention for m in unique_members[:10])
        more = len(unique_members) - 10
        if more > 0:
            mentions += f" 외 {more}명"

        total = parsed * len(unique_members)
        embed = discord.Embed(
            title="포인트 지급 완료",
            description=(
                f"수신자: {mentions}\n"
                f"지급 금액(1인당): **{format_num(parsed)} {CURRENCY}**\n"
                f"총 지급: **{format_num(total)} {CURRENCY}**"
            ),
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"지급자: {ctx.author.display_name}")
        await ctx.send(embed=embed)

    @grant_points.error
    async def _grant_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("이 명령은 **서버 관리** 권한이 있어야 사용 가능합니다.", delete_after=6)
        elif isinstance(error, commands.BadArgument):
            await ctx.reply("대상 유저 멘션 뒤에 **금액**을 입력하세요. 예) `.지급 @사용자1 5000`", delete_after=6)

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

    # --------- 도박(그룹: 본명령 + 초기화) ---------
    @commands.group(name="도박", invoke_without_command=True)
    async def gamble(self, ctx: commands.Context, amount: int):
        """
        사용법: .도박 n   (1 ≤ n ≤ 50)
        - 성공: 확률 1/2, 2배 지급(베팅액 선차감 → 당첨 시 2n 지급, 순이익 +n)
        - 실패: 베팅액 회수
        - 쿨타임: 유저별 3분 (베팅이 실제로 진행된 경우에만 시작)
        """
        # 입력 검증 (쿨타임 시작 안 함)
        if amount <= 0 or amount > MAX_BET:
            await ctx.reply(f"베팅 금액은 1 ~ {MAX_BET} 사이여야 합니다.")
            return

        # 유저별 쿨타임 체크
        now = datetime.now(timezone.utc)
        last = get_last_gamble(ctx.author.id)
        cooldown = timedelta(minutes=COOLDOWN_MINUTES)   # ✅ 분 단위 쿨타임
        if last and now - last < cooldown:
            remain = cooldown - (now - last)
            hrs_total = remain.days * 24 + remain.seconds // 3600
            mins = (remain.seconds % 3600) // 60
            secs = remain.seconds % 60
            msg = "쿨타임입니다. "
            if hrs_total:
                msg += f"{hrs_total}시간 "
            if mins:
                msg += f"{mins}분 "
            msg += f"{secs}초 후에 다시 시도하세요."
            await ctx.reply(msg, delete_after=8)
            return

        # 잔액 차감 실패 시 쿨타임 시작하지 않음
        if not spend_points(ctx.author.id, amount):
            await ctx.reply(f"잔액이 부족합니다. (보유: {format_num(get_points(ctx.author.id))} {CURRENCY})")
            return

        # 베팅이 진행된 시점에 쿨타임 기록
        set_last_gamble(ctx.author.id, now)

        win = random.random() < SUCCESS_PROB  # 1/2
        if win:
            # 총 2n 지급 → 직전에 n 차감했으므로 순이익 +n
            new_balance = add_points(ctx.author.id, amount * 2)
            result = f"🎉 성공! **{format_num(amount * 2)} {CURRENCY}** 획득"
            color = discord.Color.green()
        else:
            new_balance = get_points(ctx.author.id)  # 이미 n 회수됨
            result = f"😵 실패! **{format_num(amount)} {CURRENCY}** 회수"
            color = discord.Color.red()

        embed = discord.Embed(
            title="도박 결과",
            description=(f"{ctx.author.mention}\n{result}\n"
                         f"현재 보유: **{format_num(new_balance)} {CURRENCY}**"),
            color=color
        )
        await ctx.send(embed=embed)

    # 관리자 전용: 유저 도박 쿨타임 초기화
    @gamble.command(name="초기화")
    @commands.has_guild_permissions(manage_guild=True)
    async def gamble_reset(self, ctx: commands.Context, member: discord.Member):
        """
        사용법: .도박 초기화 @유저
        해당 유저의 도박 쿨타임(최근 베팅 시각)을 제거합니다.
        """
        last = get_last_gamble(member.id)
        # set_last_gamble(..., None) 은 utils.stats 에서 키 제거/None 처리
        set_last_gamble(member.id, None)

        if last:
            await ctx.reply(f"{member.mention} 님의 도박 쿨타임을 초기화했어요. 지금 바로 도박이 가능합니다.")
        else:
            await ctx.reply(f"{member.mention} 님은 이미 도박 쿨타임이 없어요.")

    @gamble_reset.error
    async def _gamble_reset_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("이 명령은 **서버 관리** 권한이 있어야 사용 가능합니다.", delete_after=6)
