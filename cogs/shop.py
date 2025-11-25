# cogs/shop.py
import configparser
import discord
from discord.ext import commands
from typing import Optional, List, Tuple

from utils.stats import (
    get_points,
    spend_points,
    add_points,
    format_num,
    load_stats,
    save_stats,
)

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
    """config.ini의 RoleShop.Tier1 ~ N을 읽어 티어 목록 생성"""
    tiers: List[Tuple[str, int, int]] = []
    i = 1
    while _section_exists(f"RoleShop.Tier{i}"):
        sec = f"RoleShop.Tier{i}"
        name = _get(sec, "name", "").strip() or f"Tier {i}"
        price_s = _get(sec, "price", "0").strip()
        role_s = _get(sec, "role_id", "0").strip()

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
        if tiers[idx - 1][2] in role_ids:
            return idx
    return 0


# ───────── 포인트 상점 관련 상수 ─────────
POINT_SHOP_KEY = "point_shop"

# index: (표시 이름, 내부 코드, 가격[Point])
POINT_SHOP_ITEMS = {
    1: ("배달의 민족 10,000원 쿠폰", "baemin", 33000),
    2: ("GS25 10,000원 기프티콘", "gs25", 33000),
    3: ("스타벅스 10,000원 기프티콘", "starbucks", 33000),
}

POINT_SHOP_DEFAULT_STOCK = {
    "baemin": 0,
    "gs25": 0,
    "starbucks": 0,
}

POINT_SHOP_ALIAS = {
    # 배민
    "배민": "baemin",
    "배달의민족": "baemin",
    "배달의 민족": "baemin",
    "baemin": "baemin",
    "bm": "baemin",
    # GS25
    "gs": "gs25",
    "gs25": "gs25",
    "지에스": "gs25",
    "지에스25": "gs25",
    "gs편의점": "gs25",
    # 스타벅스
    "스벅": "starbucks",
    "스타벅스": "starbucks",
    "starbucks": "starbucks",
}


class ShopCog(commands.Cog):
    """
    역할 상점(.상점 / .구입) + 포인트 상점(.포인트상점 ...)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.purchase_channel_id, self.log_channel_id = _load_top_settings()
        # 역할 상점 티어
        self.role_tiers: List[Tuple[str, int, int]] = _load_tiers_from_config()

    # ───────── 공통 유틸 ─────────
    def _check_channel(self, ctx: commands.Context) -> bool:
        """역할 상점 사용 채널 제한"""
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

    # ───────── 포인트 상점 유틸 ─────────
    def _load_point_shop(self) -> tuple[dict, dict]:
        """
        stats 전체와 point_shop dict를 함께 반환.
        point_shop 키가 없으면 기본값으로 초기화.
        """
        stats = load_stats()
        data = stats.get(POINT_SHOP_KEY)
        if not isinstance(data, dict):
            data = POINT_SHOP_DEFAULT_STOCK.copy()
            stats[POINT_SHOP_KEY] = data
            save_stats(stats)
        else:
            # 빠진 키 있으면 채워주기
            changed = False
            for k, v in POINT_SHOP_DEFAULT_STOCK.items():
                if k not in data:
                    data[k] = v
                    changed = True
            if changed:
                save_stats(stats)
        return stats, data

    # ───────── 관리자: 설정 리로드 (역할 상점) ─────────
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

    # ───────── 역할 상점 보기 ─────────
    @commands.command(name="상점", aliases=["역할목록", "칭호목록"])
    async def role_shop(self, ctx: commands.Context):
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
            owned = "✅ 보유" if role_id in role_ids else ""
            nexttag = "🛒 다음 구매" if i == tier_idx + 1 else ""
            afford = (
                "💰 가능"
                if (i == tier_idx + 1 and balance >= price)
                else ("❌ 부족" if i == tier_idx + 1 else "")
            )
            lines.append(
                f"**{i}. {name}** — {format_num(price)} {CURRENCY} {owned} {nexttag} {afford}".strip()
            )

        embed = discord.Embed(
            title="🛎️ 역할 상점",
            description="\n".join(lines) if lines else "-",
            color=discord.Color.gold(),
        )
        embed.set_footer(text="구매 방법: .구입 번호 (예: .구입 2)")
        embed.add_field(name="내 보유 포인트", value=f"{format_num(balance)} {CURRENCY}", inline=False)
        await ctx.send(embed=embed)

    # ───────── 역할 구매 ─────────
    @commands.command(name="구입")
    async def buy_role(self, ctx: commands.Context, index: int):
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
            await ctx.reply(
                "역할 계층이 낮아 부여할 수 없어요. 봇 최상위 역할을 구매 대상 역할들보다 **위로** 올려주세요."
            )
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
            description=(
                f"{member.mention} 님, **{name}** 칭호를 획득했습니다!\n"
                f"차감: **{format_num(price)} {CURRENCY}**"
            ),
            color=discord.Color.green(),
        )
        embed.add_field(name="현재 보유 포인트", value=f"{format_num(balance)} {CURRENCY}", inline=False)
        await ctx.send(embed=embed)

        ch = self._log_channel(guild)
        if ch:
            log = discord.Embed(
                title="🧾 역할 구매 로그",
                description=(
                    f"**구매자:** {member.mention}\n"
                    f"**칭호:** {name}\n"
                    f"**가격:** {format_num(price)} {CURRENCY}\n"
                    f"**채널:** {ctx.channel.mention}"
                ),
                color=discord.Color.blurple(),
            )
            await ch.send(embed=log)

    # ───────── 포인트 상점: 메인 (.포인트상점) ─────────
    @commands.group(name="포인트상점", invoke_without_command=True)
    async def point_shop(self, ctx: commands.Context):
        """
        사용법:
          .포인트상점
            → 배민 / GS25 / 스타벅스 재고 및 가격 보기
        """
        _, shop = self._load_point_shop()

        lines: list[str] = []
        for idx in sorted(POINT_SHOP_ITEMS.keys()):
            name, code, price = POINT_SHOP_ITEMS[idx]
            stock = int(shop.get(code, 0))
            lines.append(
                f"**{idx}. {name}** — 재고: **{stock}개** / 가격: **{format_num(price)} {CURRENCY}**"
            )

        embed = discord.Embed(
            title="📦 포인트 상점",
            description="\n".join(lines) if lines else "등록된 상품이 없습니다.",
            color=discord.Color.teal(),
        )
        embed.set_footer(text="구매 방법: .포인트상점 구매 번호 (예: .포인트상점 구매 1)")
        await ctx.send(embed=embed)

    # ───────── 포인트 상점: 입고 (관리자) ─────────
    @point_shop.command(name="입고")
    @commands.has_guild_permissions(manage_guild=True)
    async def point_shop_stock(self, ctx: commands.Context, item: str, qty: int):
        """
        사용법: .포인트상점 입고 (배민/GS/스벅) n
        - n개의 상품을 포인트상점에 추가 (관리자 전용)
        """
        key = POINT_SHOP_ALIAS.get(item.lower())
        if key is None:
            await ctx.reply(
                "상품명을 잘못 입력하셨습니다.\n"
                "사용 가능: **배민 / 배달의민족 / GS / GS25 / 스벅 / 스타벅스**",
                delete_after=8,
            )
            return

        if qty <= 0:
            await ctx.reply("입고 수량은 1 이상이어야 합니다.", delete_after=6)
            return

        stats, shop = self._load_point_shop()
        cur = int(shop.get(key, 0))
        shop[key] = cur + qty
        save_stats(stats)

        # 이름 찾기
        item_name = None
        for _, (disp_name, code, _) in POINT_SHOP_ITEMS.items():
            if code == key:
                item_name = disp_name
                break
        if item_name is None:
            item_name = key

        embed = discord.Embed(
            title="📦 포인트 상점 입고",
            description=(
                f"**{item_name}** 상품을 **{qty}개** 입고했습니다.\n"
                f"현재 재고: **{shop[key]}개**"
            ),
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    # ───────── 포인트 상점: 구매 ─────────
    @point_shop.command(name="구매")
    async def point_shop_buy(self, ctx: commands.Context, index: int):
        """
        사용법: .포인트상점 구매 1|2|3
        - 1개씩만 구매 가능
        """
        if index not in POINT_SHOP_ITEMS:
            await ctx.reply("잘못된 번호입니다. 1 / 2 / 3 중 하나를 선택해 주세요.", delete_after=6)
            return

        name, code, price = POINT_SHOP_ITEMS[index]

        # 1) 현재 재고 확인
        _, shop = self._load_point_shop()
        stock = int(shop.get(code, 0))
        if stock <= 0:
            await ctx.reply(f"현재 **{name}** 재고가 없습니다. 😢", delete_after=6)
            return

        # 2) 포인트 차감 (내부적으로 load/save 실행)
        if not spend_points(ctx.author.id, price):
            await ctx.reply(
                f"포인트가 부족합니다. 필요: {format_num(price)} {CURRENCY} / "
                f"보유: {format_num(get_points(ctx.author.id))} {CURRENCY}",
                delete_after=8,
            )
            return

        # 3) 재고 감소 (포인트 차감 이후 stats를 다시 로드해서,
        #    차감 결과를 덮어쓰지 않도록 함)
        stats2 = load_stats()
        shop2 = stats2.get(POINT_SHOP_KEY)
        if not isinstance(shop2, dict):
            shop2 = POINT_SHOP_DEFAULT_STOCK.copy()
            stats2[POINT_SHOP_KEY] = shop2

        cur_stock = int(shop2.get(code, 0))
        new_stock = cur_stock - 1 if cur_stock > 0 else 0
        shop2[code] = new_stock
        save_stats(stats2)

        balance = get_points(ctx.author.id)

        embed = discord.Embed(
            title="✅ 포인트 상점 구매 완료",
            description=(
                f"{ctx.author.mention} 님이 **{name}** 을(를) 1개 구매했습니다.\n"
                f"차감: **{format_num(price)} {CURRENCY}**\n"
                f"남은 재고: **{new_stock}개**"
            ),
            color=discord.Color.green(),
        )
        embed.add_field(
            name="현재 보유 포인트",
            value=f"{format_num(balance)} {CURRENCY}",
            inline=False,
        )
        embed.set_footer(text="실제 쿠폰 지급은 운영진에게 문의해 주세요.")
        await ctx.send(embed=embed)

        # 로그 채널 기록
        guild = ctx.guild
        if guild:
            ch = self._log_channel(guild)
            if ch:
                log = discord.Embed(
                    title="🧾 포인트 상점 구매 로그",
                    description=(
                        f"**구매자:** {ctx.author.mention}\n"
                        f"**상품:** {name}\n"
                        f"**가격:** {format_num(price)} {CURRENCY}\n"
                        f"**채널:** {ctx.channel.mention}"
                    ),
                    color=discord.Color.dark_teal(),
                )
                await ch.send(embed=log)


async def setup(bot: commands.Bot):
    await bot.add_cog(ShopCog(bot))
