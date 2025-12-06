# cogs/moderation.py
import configparser
from typing import Optional

import discord
from discord.ext import commands

from utils.stats import load_stats, save_stats, ensure_user

# ───────── config.ini 로딩 ─────────
_cfg = configparser.ConfigParser()
try:
    _cfg.read("config.ini", encoding="utf-8")
except Exception:
    pass


def _get_id(section: str, key: str) -> int:
    try:
        v = _cfg.get(section, key, fallback="0")
        return int(v) if str(v).isdigit() else 0
    except Exception:
        return 0


# 기본 로그 채널 ID (요구사항)
DEFAULT_WARN_LOG_CHANNEL_ID = 1417536646597054596

# config.ini 에서 [Moderation] warn_log_channel_id 를 읽어오고,
# 없으면 DEFAULT_WARN_LOG_CHANNEL_ID 사용
WARN_LOG_CHANNEL_ID = _get_id("Moderation", "warn_log_channel_id") or DEFAULT_WARN_LOG_CHANNEL_ID


class ModerationCog(commands.Cog):
    """
    경고 / 차감 등 제재 기록 관리용 Cog
    - .경고 @유저 n 사유:...
    - .차감 @유저 n 사유:...
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ───────── 내부 유틸 ─────────
    def _get_warn_log_channel(
        self, guild: Optional[discord.Guild]
    ) -> Optional[discord.TextChannel]:
        """제재 로그 채널 반환 (없으면 None)"""
        if guild is None or not WARN_LOG_CHANNEL_ID:
            return None
        ch = guild.get_channel(WARN_LOG_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
            return ch
        return None

    def _parse_count(self, raw: str) -> Optional[int]:
        """
        'n' 형태의 경고 개수를 정수로 파싱
        예: '1', '3', '양:2' 등 간단한 포맷 허용
        """
        s = str(raw).strip()
        # '양:3', '개:3', '경고:3' 같은 형식도 허용
        if ":" in s:
            head, tail = s.split(":", 1)
            if head in ("양", "개", "경고"):
                s = tail.strip()
        s = s.replace(",", "")

        if not s.isdigit():
            return None

        value = int(s)
        return value if value > 0 else None

    # ───────── .경고 ─────────
    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(name="경고")
    async def give_warning(
        self,
        ctx: commands.Context,
        member: discord.Member,
        count_raw: str,
        *,
        reason: str = "사유 미기재",
    ):
        """
        사용법:
          .경고 @유저 1 사유:욕설
          .경고 @유저 2 사유:경고 누적 등
        - n은 부여할 경고 개수
        - 경고 4회 누적 시 자동 서버 차단 시도
        """
        count = self._parse_count(count_raw)
        if count is None:
            await ctx.reply(
                "경고 개수는 **1 이상의 정수**로 입력해 주세요.\n"
                "예) `.경고 @유저 1 사유:욕설`",
                delete_after=8,
            )
            return

        stats = load_stats()
        rec = ensure_user(stats, str(member.id))

        old_warn = int(rec.get("경고", 0))
        new_warn = old_warn + count
        rec["경고"] = new_warn
        save_stats(stats)

        # 4회 도달 시 자동 차단 시도
        banned = False
        extra_line = ""
        guild = ctx.guild

        if new_warn >= 4 and old_warn < 4 and guild and guild.me.guild_permissions.ban_members:
            try:
                await member.ban(
                    reason=f"경고 {new_warn}회 누적(자동 차단): {reason}"
                )
                banned = True
                extra_line = "\n⚠️ **경고 4회 누적으로 자동 서버 차단이 수행되었습니다.**"
            except discord.Forbidden:
                extra_line = "\n⚠️ 경고 4회지만, 봇에 차단 권한이 없어 자동 차단에 실패했습니다."
            except Exception:
                extra_line = "\n⚠️ 경고 4회지만, 예기치 않은 오류로 자동 차단에 실패했습니다."

        # 현재 채널 알림
        embed = discord.Embed(
            title="⚠️ 경고 부여",
            description=(
                f"{member.mention} 님에게 **경고 {count}회**를 부여했습니다.\n"
                f"누적 경고: **{old_warn}회 → {new_warn}회**\n"
                f"경고 4회 누적 시 서버 차단입니다.{extra_line}"
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(name="사유", value=reason or "사유 미기재", inline=False)
        embed.set_footer(text=f"처리자: {ctx.author.display_name}")
        await ctx.send(embed=embed)

        # 로그 채널 기록
        log_ch = self._get_warn_log_channel(guild)
        if log_ch:
            log_embed = discord.Embed(
                title="🚫 경고 기록",
                color=discord.Color.dark_orange(),
            )
            log_embed.add_field(name="대상", value=member.mention, inline=False)
            log_embed.add_field(name="처리자", value=ctx.author.mention, inline=False)
            log_embed.add_field(name="변동", value=f"+{count}회", inline=False)
            log_embed.add_field(
                name="누적 경고", value=f"{old_warn}회 → {new_warn}회", inline=False
            )
            log_embed.add_field(name="채널", value=ctx.channel.mention, inline=False)
            if banned:
                log_embed.add_field(
                    name="조치", value="경고 4회 누적으로 **자동 서버 차단**", inline=False
                )
            # 👇 사유는 맨 마지막 필드
            log_embed.add_field(name="사유", value=reason or "사유 미기재", inline=False)
            await log_ch.send(embed=log_embed)

    # ───────── .차감 ─────────
    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(name="차감")
    async def reduce_warning(
        self,
        ctx: commands.Context,
        member: discord.Member,
        count_raw: str,
        *,
        reason: str = "사유 미기재",
    ):
        """
        사용법:
          .차감 @유저 1 사유:오해 해소
          .차감 @유저 2 사유:기간 경과 등
        - n은 차감할 경고 개수
        """
        count = self._parse_count(count_raw)
        if count is None:
            await ctx.reply(
                "차감할 경고 개수는 **1 이상의 정수**로 입력해 주세요.\n"
                "예) `.차감 @유저 1 사유:오해 해소`",
                delete_after=8,
            )
            return

        stats = load_stats()
        rec = ensure_user(stats, str(member.id))

        old_warn = int(rec.get("경고", 0))
        new_warn = max(0, old_warn - count)
        diff = old_warn - new_warn  # 실제로 차감된 양
        rec["경고"] = new_warn
        save_stats(stats)

        if diff <= 0:
            note = "⚠️ 차감할 경고가 없어 실제로 차감된 횟수는 0회입니다."
        else:
            note = ""

        # 현재 채널 알림
        embed = discord.Embed(
            title="✅ 경고 차감",
            description=(
                f"{member.mention} 님의 경고를 **{diff}회** 차감했습니다.\n"
                f"누적 경고: **{old_warn}회 → {new_warn}회**\n"
                f"{note}".strip()
            ),
            color=discord.Color.green(),
        )
        embed.add_field(name="사유", value=reason or "사유 미기재", inline=False)
        embed.set_footer(text=f"처리자: {ctx.author.display_name}")
        await ctx.send(embed=embed)

        # 로그 채널 기록
        log_ch = self._get_warn_log_channel(ctx.guild)
        if log_ch:
            log_embed = discord.Embed(
                title="📘 경고 차감 기록",
                color=discord.Color.blue(),
            )
            log_embed.add_field(name="대상", value=member.mention, inline=False)
            log_embed.add_field(name="처리자", value=ctx.author.mention, inline=False)
            log_embed.add_field(
                name="변동", value=f"-{diff}회 (요청: {count}회)", inline=False
            )
            log_embed.add_field(
                name="누적 경고", value=f"{old_warn}회 → {new_warn}회", inline=False
            )
            log_embed.add_field(name="채널", value=ctx.channel.mention, inline=False)
            # 👇 맨 마지막에 사유
            log_embed.add_field(name="사유", value=reason or "사유 미기재", inline=False)
            await log_ch.send(embed=log_embed)

    # ───────── 에러 핸들러 ─────────
    @give_warning.error
    async def _warn_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply(
                "이 명령은 **서버 관리** 권한이 있어야 사용 가능합니다.", delete_after=6
            )

    @reduce_warning.error
    async def _reduce_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply(
                "이 명령은 **서버 관리** 권한이 있어야 사용 가능합니다.", delete_after=6
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
