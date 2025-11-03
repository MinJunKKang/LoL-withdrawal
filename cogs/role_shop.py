# cogs/role_shop.py
import configparser
import discord
from discord.ext import commands
from typing import Optional, List, Tuple

from utils.stats import get_points, spend_points, add_points, format_num

CURRENCY = "Point"

# ───────── config.ini 로딩 ─────────
_cfg = configparser.ConfigParser()
try:
    _cfg.read("config.ini", encoding="utf-8")
except Exception:
    pass

def _get_id(section: str, key: str) -> int:
    try:
        val = _cfg.get(section, key, fallback="0")
        return int(val) if str(val).isdigit() else 0
    except Exception:
        return 0

def _section_exists(name: str) -> bool:
    return name in _cfg

def _get(section: str, key: str, default: str = "") -> str:
    try:
        return _cfg.get(section, key, fallback=default)
    except Exception:
        return default

def _load_top_settings() -> tuple[int, int]:
    purchase_channel_id = _get_id("RoleShop", "purchase_channel_id")
    log_channel_id = _get_id("RoleShop", "log_channel_id")
    return purchase_channel_id, log_channel_id

def _load_tiers_from_config() -> List[Tuple[str, int, int]]:
    tiers: List[Tuple[str, int, int]] = []
    i = 1
    while _section_exists(f"RoleShop.Tier{i}"):
        sec = f"RoleShop.Tier{i}"
        name = _get(sec, "name", "").strip() or f"Tier {i}"
        price_s = _get(sec, "price", "0").strip()
        role_s  = _get(sec, "role_id", "0").strip()

        try:
            price = int(price_s)
            role_id = int(role_s)
        except ValueError:
            break  # 형식 이상 → 중단

        if price <= 0 or role_id <= 0:
            break

        tiers.append((name, price, role_id))
        i += 1
    return tiers

def _find_role(guild: discord.Guild, role_id: int) -> Optional[discord.Role]:
    r = guild.get_role(role_id)
    return r if isinstance(r, discord.Role) else None

def _current_tier(member: discord.Member, tiers: List[Tuple[str, int, int]]) -> int:
    """보유 중인 최고 등급 인덱스(1부터), 없으면 0"""
    role_ids = {r.id for r in member.roles}
    for idx in range(len(tiers), 0, -1):
        if tiers[idx-1][2] in role_ids:
            return idx
    return 0

class RoleShopCog(commands.Cog):
    """포인트로 칭호(역할) 순차 구매: .상점 / .구입 n / .상점-리로드(관리자) — 하위 칭호 스택 유지"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.purchase_channel_id, self.log_channel_id = _load_top_settings()
        self.role_tiers: List[Tuple[str, int, int]] = _load_tiers_from_config()  # 설정만 사용

    # ───────── 내부 유틸 ─────────
    def _check_channel(self, ctx: commands.Context) -> bool:
        return self.purchase_channel_id == 0 or ctx.channel.id == self.purchase_channel_id

    def _log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        if not guild or not self.log_channel_id:
            return None
        ch = guild.get_channel(self.log_channel_id)
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
            return ch
        return None

    def _tiers_ready(self) -> bool:
        return len(self.role_tiers) > 0

    # ───────── 관리자: 설정 리로드 ─────────
    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(name="상점-리로드", aliases=["역할상점-리로드", "칭호-리로드"])
    async def reload_shop(self, ctx: commands.Context):
        try:
            _cfg.read("config.ini", encoding="utf-8")
            self.purchase_channel_id, self.log_channel_id = _load_top_settings()
            self.role_tiers = _load_tiers_from_config()
            if self.role_tiers:
                await ctx.reply("역할 상점 설정을 리로드했습니다. (config.ini 기반)")
            else:
                await ctx.reply("역할 티어가 없습니다. config.ini의 [RoleShop.Tier1]부터 정의해 주세요.")
        except Exception:
            await ctx.reply("리로드 중 오류가 발생했습니다. config.ini 형식을 확인하세요.", delete_after=8)

    # ───────── 상점 보기 ─────────
    @commands.command(name="상점", aliases=["역할목록", "칭호목록"])
    async def shop(self, ctx: commands.Context):
        if not self._check_channel(ctx):
            mention = f"<#{self.purchase_channel_id}>" if self.purchase_channel_id else "지정 채널"
            await ctx.reply(f"이 명령은 {mention}에서만 사용할 수 있어요.", delete_after=6)
            return

        if not self._tiers_ready():
            await ctx.reply("역할 상점이 아직 설정되지 않았어요. 관리자가 config.ini에 티어를 등록해야 합니다.")
            return

        member = ctx.author
        tiers = self.role_tiers
        tier_idx = _current_tier(member, tiers)
        balance = get_points(member.id)
        role_ids = {r.id for r in member.roles}

        lines = []
        for i, (name, price, role_id) in enumerate(tiers, start=1):
            owned   = "✅ 보유" if role_id in role_ids else ""
            nexttag = "🛒 다음 구매" if i == tier_idx + 1 else ""
            afford  = "💰 가능" if (i == tier_idx + 1 and balance >= price) else ("❌ 부족" if i == tier_idx + 1 else "")
            lines.append(f"**{i}. {name}** — {format_num(price)} {CURRENCY} {owned} {nexttag} {afford}".strip())

        embed = discord.Embed(
            title="🛎️ 역할 상점",
            description="\n".join(lines) if lines else "-",
            color=discord.Color.gold()
        )
        embed.set_footer(text="구매 방법: .구입 번호 (예: .구입 2)")
        embed.add_field(name="내 보유 포인트", value=f"{format_num(balance)} {CURRENCY}", inline=False)
        await ctx.send(embed=embed)

    # ───────── 구매 ─────────
    @commands.command(name="구입")
    async def buy(self, ctx: commands.Context, index: int):
        if not self._check_channel(ctx):
            mention = f"<#{self.purchase_channel_id}>" if self.purchase_channel_id else "지정 채널"
            await ctx.reply(f"이 명령은 {mention}에서만 사용할 수 있어요.", delete_after=6)
            return

        if not self._tiers_ready():
            await ctx.reply("역할 상점이 비활성화 상태입니다. 관리자가 config.ini에 티어를 등록해야 합니다.")
            return

        tiers = self.role_tiers
        max_tier = len(tiers)
        if index < 1 or index > max_tier:
            await ctx.reply(f"잘못된 번호입니다. 1 ~ {max_tier} 사이로 입력하세요.")
            return

        guild = ctx.guild
        member = ctx.author
        assert guild is not None

        if not guild.me.guild_permissions.manage_roles:
            await ctx.reply("역할을 관리할 권한이 없어요. 봇에 **Manage Roles** 권한을 부여해 주세요.")
            return

        current = _current_tier(member, tiers)
        if current >= max_tier:
            await ctx.reply("이미 최고 등급 칭호를 보유하고 있어요. 👑")
            return

        required = current + 1
        if index != required:
            await ctx.reply(f"순차 구매만 가능합니다. 현재 등급: {current} → **다음 구매 가능: {required}번**")
            return

        name, price, role_id = tiers[index - 1]
        role = _find_role(guild, role_id)
        if not role:
            await ctx.reply(f"서버에 해당 역할(ID: {role_id})이 없습니다. 관리자에게 문의해 주세요.")
            return

        # 이미 그 역할을 가진 경우 중복 결제 방지
        if role in member.roles:
            await ctx.reply("이미 해당 칭호를 보유하고 있습니다.")
            return

        if guild.me.top_role.position <= role.position:
            await ctx.reply("역할 계층이 낮아 부여할 수 없어요. 봇 최상위 역할을 구매 대상 역할들보다 **위로** 올려주세요.")
            return

        if not spend_points(member.id, price):
            await ctx.reply(
                f"포인트가 부족합니다. 필요: {format_num(price)} {CURRENCY} / "
                f"보유: {format_num(get_points(member.id))} {CURRENCY}"
            )
            return

        try:
            # 새 역할 부여(스택 유지: 하위 칭호 제거하지 않음)
            await member.add_roles(role, reason="칭호 구매(스택 유지)")
        except discord.Forbidden:
            add_points(member.id, price)  # 자동 환불
            await ctx.reply("역할 부여 실패(권한/계층 문제). 결제는 자동 환불되었습니다.")
            return
        except Exception:
            add_points(member.id, price)  # 자동 환불
            await ctx.reply("역할 부여 중 오류가 발생했습니다. 결제는 자동 환불되었습니다.")
            return

        balance = get_points(member.id)
        embed = discord.Embed(
            title="구매 완료",
            description=(f"{member.mention} 님, **{name}** 칭호를 획득했습니다!\n"
                         f"차감: **{format_num(price)} {CURRENCY}**"),
            color=discord.Color.green()
        )
        embed.add_field(name="현재 보유 포인트", value=f"{format_num(balance)} {CURRENCY}", inline=False)
        await ctx.send(embed=embed)

        ch = self._log_channel(guild)
        if ch:
            log = discord.Embed(
                title="🧾 역할 구매 로그",
                description=(f"**구매자:** {member.mention}\n"
                             f"**칭호:** {name}\n"
                             f"**가격:** {format_num(price)} {CURRENCY}\n"
                             f"**채널:** {ctx.channel.mention}"),
                color=discord.Color.blurple()
            )
            await ch.send(embed=log)

async def setup(bot: commands.Bot):
    await bot.add_cog(RoleShopCog(bot))
