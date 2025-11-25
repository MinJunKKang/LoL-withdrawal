# cogs/economy.py
import random
from datetime import datetime, timezone, timedelta, date
import discord
from discord.ext import commands
from typing import Optional
import configparser

# ─────────────────────────────────────────────
# config.ini에서 Economy 관련 설정 읽기
# ─────────────────────────────────────────────
_cfg = configparser.ConfigParser()
_cfg.read("config.ini", encoding="utf-8")


def _parse_id_list(raw: str) -> set[int]:
    ids: set[int] = set()
    for token in raw.replace("\n", ",").split(","):
        token = token.strip()
        if token.isdigit():
            ids.add(int(token))
    return ids


# 도박 쿨타임 초기화 허용 ID 목록
GAMBLE_RESET_ALLOWED_IDS = _parse_id_list(
    _cfg.get("Economy", "gamble_reset_allow", fallback="")
)

# 전체 포인트 초기화 허용 ID 목록
POINT_RESET_ALLOWED_IDS = _parse_id_list(
    _cfg.get("Economy", "point_reset_allow", fallback="")
)

# 포인트 지급 로그 채널 ID
try:
    POINT_LOG_CHANNEL_ID = int(
        _cfg.get("Economy", "point_log_channel_id", fallback="0").strip() or "0"
    )
except Exception:
    POINT_LOG_CHANNEL_ID = 0


from utils.stats import (
    load_stats,
    save_stats,
    ensure_user,
    format_num,
    spend_points,
    get_points,
    add_points,
    get_last_gamble,
    set_last_gamble,
)

# ─────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────
CURRENCY = "Point"
COOLDOWN_MINUTES = 3          # 도박 쿨타임: 3분
SUCCESS_PROB = 0.4            # 0.4 확률

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
    .출석
    .전달 @유저 n
    .도박 n                 (성공 0.4, 2배 지급, 유저별 쿨타임 3분)
    .도박 초기화 @유저       (허용 ID만 사용 가능)
    .순위 [@유저]
    .초기화                  (허용 ID만 사용 가능, 전체 포인트 0)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ───────────────── helpers ─────────────────
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

    def _get_point_log_channel(
        self, guild: discord.Guild | None
    ) -> Optional[discord.TextChannel]:
        """포인트 지급 로그 채널 반환 (없으면 None)"""
        if not guild or not POINT_LOG_CHANNEL_ID:
            return None
        ch = guild.get_channel(POINT_LOG_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
            return ch
        return None

    # ───────────────── 지갑 ─────────────────
    @commands.command(name="지갑")
    async def wallet(self, ctx: commands.Context, member: discord.Member | None = None):
        target = member or ctx.author
        points = get_points(target.id)
        await ctx.send(
            f"{target.mention} 님은 **{format_num(points)} {CURRENCY}**를 보유하고 있어요!"
        )

    # ───────────────── 출석 ─────────────────
    @commands.command(name="출석")
    async def attendance(self, ctx: commands.Context):
        """
        사용법: .출석
        - Asia/Seoul 기준 하루 1회, 자정 이후 초기화
        - 보상: 30 Point
        """
        user_id = str(ctx.author.id)
        now_kst = datetime.now(tz=KST)
        today_str = now_kst.date().isoformat()          # 'YYYY-MM-DD'
        next_reset = (now_kst + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        stats = load_stats()
        rec = ensure_user(stats, user_id)
        last_attend_str = rec.get(ATTEND_KEY)

        if last_attend_str == today_str:
            ts = next_reset.strftime("%Y-%m-%d %H:%M KST")
            embed = discord.Embed(
                title="📅 출석 체크",
                description=(
                    f"{ctx.author.mention} 님은 이미 오늘 출석을 완료하셨어요.\n"
                    f"다음 출석 가능 시각: **{ts}**"
                ),
                color=discord.Color.orange(),
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
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    # ───────────────── 전달 ─────────────────
    @commands.command(name="전달")
    async def transfer_points(self, ctx: commands.Context, member: discord.Member, amount: str):
        """
        사용법: .전달 @유저 n
        - 자신의 포인트 중 n 포인트를 대상 유저에게 전달(송금)
        """
        if member.id == ctx.author.id:
            await ctx.reply("자기 자신에게는 전달할 수 없어요.")
            return
        if member.bot:
            await ctx.reply("봇에게는 전달할 수 없어요.")
            return

        parsed = self._parse_amount(amount)
        if parsed is None or parsed <= 0:
            await ctx.reply(
                "전달 금액을 올바르게 입력해 주세요. 예) `.전달 @유저 500` 또는 `.전달 @유저 양:500`"
            )
            return

        if not spend_points(ctx.author.id, parsed):
            await ctx.reply(
                f"잔액이 부족합니다. (보유: {format_num(get_points(ctx.author.id))} {CURRENCY})"
            )
            return

        new_receiver_bal = add_points(member.id, parsed)

        embed = discord.Embed(
            title="💸 포인트 전달 완료",
            description=(
                f"보내는 사람: {ctx.author.mention}\n"
                f"받는 사람: {member.mention}\n"
                f"전달 금액: **{format_num(parsed)} {CURRENCY}**\n\n"
                f"받는 사람 현재 보유: **{format_num(new_receiver_bal)} {CURRENCY}**"
            ),
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    # ───────────────── 지급 (관리자) ─────────────────
    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(name="지급")
    async def grant_points(
        self,
        ctx: commands.Context,
        members: commands.Greedy[discord.Member],
        *,
        amount: str,
    ):
        """
        사용법:
          .지급 @유저1 5000
          .지급 @유저1 @유저2 ... 5000
          .지급 @유저1 @유저2 ... 양:5000
        - 멘션된 모든 유저에게 동일 금액 지급
        """
        if not members:
            await ctx.reply(
                "지급할 **유저를 1명 이상 멘션**해 주세요. 예) "
                "`.지급 @사용자1 5000` 또는 `.지급 @사용자1 @사용자2 5000`"
            )
            return

        parsed = self._parse_amount(amount)
        if parsed is None or parsed <= 0:
            await ctx.reply(
                "금액 형식이 올바르지 않아요. 예: "
                "`.지급 @유저1 5000` 또는 `.지급 @유저1 @유저2 양:5000`"
            )
            return

        # 중복 멘션 제거
        unique_members: list[discord.Member] = []
        seen_ids: set[int] = set()
        for m in members:
            if m.id not in seen_ids:
                unique_members.append(m)
                seen_ids.add(m.id)

        # 일괄 지급 + 각 대상의 새 잔액 기록
        stats = load_stats()
        new_balances: dict[int, int] = {}
        for member in unique_members:
            rec = ensure_user(stats, str(member.id))
            rec["포인트"] = int(rec.get("포인트", 0)) + parsed
            new_balances[member.id] = rec["포인트"]
        save_stats(stats)

        # 결과 메시지 (현재 채널)
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
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"지급자: {ctx.author.display_name}")
        await ctx.send(embed=embed)

        # 포인트 지급 로그 채널로 로그 전송
        log_ch = self._get_point_log_channel(ctx.guild)
        if log_ch:
            for member in unique_members:
                bal = new_balances.get(member.id, get_points(member.id))
                log_embed = discord.Embed(
                    title="💰 지급 로그",
                    color=discord.Color.gold(),
                )
                log_embed.add_field(name="지급자", value=ctx.author.mention, inline=False)
                log_embed.add_field(name="대상", value=member.mention, inline=False)
                log_embed.add_field(
                    name="금액", value=f"{format_num(parsed)} P", inline=False
                )
                log_embed.add_field(
                    name="채널", value=ctx.channel.mention, inline=False
                )
                log_embed.add_field(
                    name="대상 잔액", value=f"{format_num(bal)} P", inline=False
                )
                await log_ch.send(embed=log_embed)

    @grant_points.error
    async def _grant_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("이 명령은 **서버 관리** 권한이 있어야 사용 가능합니다.", delete_after=6)
        elif isinstance(error, commands.BadArgument):
            await ctx.reply(
                "대상 유저 멘션 뒤에 **금액**을 입력하세요. 예) `.지급 @사용자1 5000`",
                delete_after=6,
            )

    # ───────────────── 회수 (관리자) ─────────────────
    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(name="회수")
    async def revoke_points(
        self, ctx: commands.Context, member: discord.Member, amount: str
    ):
        parsed = self._parse_amount(amount)
        if parsed is None or parsed <= 0:
            await ctx.reply(
                "금액 형식이 올바르지 않아요. 예: `.회수 @유저 5000` 또는 `.회수 @유저 양:5000`"
            )
            return

        if not spend_points(member.id, parsed):
            await ctx.send(
                f"❌ {member.mention} 님의 잔액이 부족합니다. (요청: {format_num(parsed)} {CURRENCY})"
            )
            return

        current = get_points(member.id)
        embed = discord.Embed(
            title="포인트 회수 완료",
            description=(
                f"{member.mention} 님에게서 **{format_num(parsed)} {CURRENCY}** 회수했습니다.\n"
                f"현재 보유: **{format_num(current)} {CURRENCY}**"
            ),
            color=discord.Color.red(),
        )
        embed.set_footer(text=f"회수자: {ctx.author.display_name}")
        await ctx.send(embed=embed)

    @revoke_points.error
    async def _revoke_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("이 명령은 **서버 관리** 권한이 있어야 사용 가능합니다.", delete_after=6)

    # ───────────────── 도박 ─────────────────
    @commands.group(name="도박", invoke_without_command=True)
    async def gamble(self, ctx: commands.Context, amount: int):
        """
        사용법: .도박 n
        - 성공: 0.4 확률, 2배 지급
        - 실패: 베팅액 회수
        - 유저별 쿨타임: 3분
        """
        if amount <= 0:
            await ctx.reply("베팅 금액은 1 이상이어야 합니다.")
            return

        now = datetime.now(timezone.utc)
        last = get_last_gamble(ctx.author.id)
        cooldown = timedelta(minutes=COOLDOWN_MINUTES)
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

        if not spend_points(ctx.author.id, amount):
            await ctx.reply(
                f"잔액이 부족합니다. (보유: {format_num(get_points(ctx.author.id))} {CURRENCY})"
            )
            return

        set_last_gamble(ctx.author.id, now)

        win = random.random() < SUCCESS_PROB
        if win:
            new_balance = add_points(ctx.author.id, amount * 2)
            result = f"🎉 성공! **{format_num(amount * 2)} {CURRENCY}** 획득"
            color = discord.Color.green()
        else:
            new_balance = get_points(ctx.author.id)
            result = f"😵 실패! **{format_num(amount)} {CURRENCY}** 회수"
            color = discord.Color.red()

        embed = discord.Embed(
            title="도박 결과",
            description=(
                f"{ctx.author.mention}\n{result}\n"
                f"현재 보유: **{format_num(new_balance)} {CURRENCY}**"
            ),
            color=color,
        )
        await ctx.send(embed=embed)

    # ───── 도박 쿨타임 초기화 (허용 ID 전용) ─────
    @gamble.command(name="초기화")
    async def gamble_reset(self, ctx: commands.Context, member: discord.Member):
        """
        사용법: .도박 초기화 @유저
        - config.ini 의 gamble_reset_allow 에 포함된 ID만 사용 가능
        """
        if ctx.author.id not in GAMBLE_RESET_ALLOWED_IDS:
            await ctx.reply("이 명령은 사용할 수 없습니다. (권한 없음)", delete_after=6)
            return

        last = get_last_gamble(member.id)
        set_last_gamble(member.id, None)

        if last:
            await ctx.reply(
                f"{member.mention} 님의 도박 쿨타임을 초기화했어요. 지금 바로 도박이 가능합니다."
            )
        else:
            await ctx.reply(f"{member.mention} 님은 이미 도박 쿨타임이 없어요.")

    # ───────────────── 순위 조회 (.순위) ─────────────────
    @commands.command(name="순위")
    async def ranking(self, ctx: commands.Context, member: discord.Member | None = None):
        """
        사용법:
          .순위         → 포인트 기준 상위 10명
          .순위 @유저   → 멘션한 유저의 전체 순위 확인
        """
        stats = load_stats()
        guild = ctx.guild

        ranking_list: list[tuple[int, int]] = []

        for uid, rec in stats.items():
            # 숫자 UID만 허용
            if not str(uid).isdigit():
                continue

            uid_int = int(uid)

            # 서버에 실제 존재하는 멤버만 포함
            user = guild.get_member(uid_int) if guild else None
            if user is None:
                continue

            if isinstance(rec, dict):
                point = int(rec.get("포인트", 0))
                ranking_list.append((uid_int, point))

        ranking_list.sort(key=lambda x: x[1], reverse=True)

        # 개별 유저 조회
        if member:
            target_id = member.id
            total_users = len(ranking_list)

            user_rank = None
            user_points = 0

            for idx, (uid, p) in enumerate(ranking_list, start=1):
                if uid == target_id:
                    user_rank = idx
                    user_points = p
                    break

            if user_rank is None:
                await ctx.reply(
                    "해당 유저는 순위에 없습니다. (기록 없음 또는 서버 미참여)"
                )
                return

            embed = discord.Embed(
                title="📊 개인 순위 조회",
                description=(
                    f"**{member.mention}** 님의 순위는\n"
                    f"**{user_rank}위 / {total_users}명** 입니다.\n\n"
                    f"보유 포인트: **{format_num(user_points)} {CURRENCY}**"
                ),
                color=discord.Color.gold(),
            )
            await ctx.send(embed=embed)
            return

        # 상위 10명 출력
        top10 = ranking_list[:10]
        description_lines: list[str] = []

        for i, (uid, point) in enumerate(top10, start=1):
            user = guild.get_member(uid) if guild else None
            if user is None:
                continue
            description_lines.append(
                f"**{i}위 — {user.display_name}** : {format_num(point)} {CURRENCY}"
            )

        if not description_lines:
            description_lines.append("아직 순위에 포함될 유저가 없습니다.")

        embed = discord.Embed(
            title="🏆 포인트 상위 10위 (서버 내 실제 사용자를 기준으로)",
            description="\n".join(description_lines),
            color=discord.Color.blue(),
        )
        await ctx.send(embed=embed)

    # ───────────────── 전체 포인트 초기화 (.초기화) ─────────────────
    @commands.command(name="초기화", aliases=["@초기화", "포인트초기화"])
    async def reset_all_points(self, ctx: commands.Context):
        """
        사용법: .초기화
        - config.ini 의 point_reset_allow 에 포함된 ID만 사용 가능
        - 모든 유저의 포인트를 0으로 초기화
        """
        if ctx.author.id not in POINT_RESET_ALLOWED_IDS:
            await ctx.reply("이 명령은 사용할 수 없습니다. (권한 없음)", delete_after=6)
            return

        stats = load_stats()
        count = 0
        for uid, rec in list(stats.items()):
            if not isinstance(rec, dict):
                continue
            rec["포인트"] = 0
            count += 1
        save_stats(stats)

        await ctx.reply(f"모든 유저의 포인트를 0으로 초기화했습니다. (대상: {count}명)")


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
