# cogs/help_kor.py
import configparser
import discord
from discord.ext import commands

PREFIX = "."
CURRENCY = "Point"

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


def _get_purchase_channel_mention() -> str | None:
    ch_id = _get_id("RoleShop", "purchase_channel_id")
    return f"<#{ch_id}>" if ch_id else None


class HelpKorCog(commands.Cog):
    """한국어 도움말(.도움 / .도움 관리자) 전용 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -----------------------
    # 내부: 임베드 빌더들
    # -----------------------
    def _build_user_embed(self) -> discord.Embed:
        shop_channel = _get_purchase_channel_mention()
        shop_place = shop_channel or "지정 채널"

        embed = discord.Embed(
            title="🧭 명령어 안내",
            description=f"접두사(prefix)는 **`{PREFIX}`** 입니다.",
            color=discord.Color.blurple(),
        )

        # Economy (user)
        embed.add_field(
            name="💰 경제(포인트) — 사용자",
            value=(
                f"• **{PREFIX}지갑 [@유저]** — 포인트 보유량 확인\n"
                f"• **{PREFIX}출석** — 하루 1회, KST 자정 초기화, 보상 **30 {CURRENCY}**\n"
                f"• **{PREFIX}전달 @유저 금액** — @유저에게 자신의 포인트를 전달\n"
                f"• **{PREFIX}도박 n** — n ≥ 1 (상한 없음), 당첨 시 2배 지급, 유저별 쿨타임 **3분**\n"
                f"   (💡 베팅이 **진행되면** 쿨타임이 시작됩니다)"
            ),
            inline=False,
        )

        # Minigames (user)
        embed.add_field(
            name="🎮 미니게임(minigames) — **각 미니게임별 쿨타임 3시간**",
            value=(
                f"• **{PREFIX}미니게임** — 버튼으로 게임 선택(동전던지기 / 주사위)\n"
                f"  - **동전던지기(앞/뒤)**: **{60}{CURRENCY}** 베팅 → 맞추면 **+{270}{CURRENCY}**, "
                f"틀리면 추가 **-{180}{CURRENCY}** (실패 시 총 **-{240}{CURRENCY}**)\n"
                f"  - **주사위(1회)**: **{10}{CURRENCY}** 베팅, 적중 **+{60}{CURRENCY}**, 실패 시 총 **-{10}{CURRENCY}**\n"
                f"  - **주사위(2회)**: **{40}{CURRENCY}** 베팅 → 하나 적중 **+{72}{CURRENCY}**, "
                f"둘 다 적중 **+{720}{CURRENCY}**, 둘 다 실패 **-{40}{CURRENCY}** (추가 차감 없음)\n"
                f"  - **주사위(3회)**: **{100}{CURRENCY}** 베팅 → 1개 적중 **+0{CURRENCY}**, "
                f"2개 **+{1000}{CURRENCY}**, 3개 **+{2500}{CURRENCY}**, 모두 실패 **-{1000}{CURRENCY}**"
            ),
            inline=False,
        )

        # Match (user)
        embed.add_field(
            name="⚔️ 내전(match) — 사용자",
            value=(
                f"• **{PREFIX}내전 모집** — 내전 로비 생성(슬롯/대기열)\n"
                f"• **{PREFIX}내전 참여** — 텍스트로 즉시 참여(또는 버튼 사용)\n"
                f"• 로비 버튼: **참여 / 대기 / 취소 / 시작 / 종료**\n"
                f"• **대기열 최대 10명**, 자동 승격 **OFF**\n"
                f"• 팀장 선택 → 드래프트 진행 → 팀 구성 완료 (OP.GG 멀티서치 버튼 자동 제공)\n"
                f"• 결과 버튼: **1팀 승리 / 2팀 승리 / 취소 / 다음판(같은 인원)**\n"
                f"    - 승리팀 전원 **+700 {CURRENCY}**, 패배팀 전원 **+400 {CURRENCY}** 자동 지급\n"
                f"    - 전적 자동 반영"
            ),
            inline=False,
        )

        # Role Shop (user)
        embed.add_field(
            name="🛎️ 역할 상점(role_shop) — 사용자",
            value=(
                f"• **{PREFIX}상점** (별칭: 역할목록/칭호목록) — 구매 가능 칭호와 가격 표시\n"
                f"• **{PREFIX}구입 번호** — 순차 구매(하위 칭호 **유지**), 포인트 차감 후 역할 부여\n"
                f"• 사용 채널: {shop_place}"
            ),
            inline=False,
        )

        embed.set_footer(text="운영진용 명령은 **.도움 관리자** 를 참고하세요.")
        return embed

    # 관리자 임베드
    def _build_admin_embed(self) -> discord.Embed:
        shop_channel = _get_purchase_channel_mention()
        shop_place = shop_channel or "지정 채널"

        embed = discord.Embed(
            title="🛠️ 관리자 명령어 안내",
            description=f"접두사(prefix)는 **`{PREFIX}`** 입니다.",
            color=discord.Color.orange(),
        )

        # Economy (admin)
        embed.add_field(
            name="💰 경제(포인트) — 관리자",
            value=(
                f"• **{PREFIX}지급 @유저1 [@유저2 ...] 금액** — 여러 명/한 명 일괄 지급 (예: `{PREFIX}지급 @A @B 5000`)\n"
                f"• **{PREFIX}회수 @유저 양:n** — 포인트 회수\n"
                f"• **{PREFIX}도박 초기화 @유저** — 해당 유저 도박 쿨타임 초기화"
            ),
            inline=False,
        )

        # Minigame (admin)
        embed.add_field(
            name="🎮 미니게임 — 관리자",
            value=(
                f"• **{PREFIX}미니게임 초기화 @유저** — 해당 유저의 **미니게임 쿨타임** 초기화"
            ),
            inline=False,
        )

        

        # Match (admin)
        embed.add_field(
            name="⚔️ 내전(match) — 관리자",
            value=(
                f"• **텍스트 명령**\n"
                f"  - **{PREFIX}내전 교체 @내보낼사람 @투입할사람** — 스왑/대기열 투입/미배정 투입 지원\n"
                f"  - **{PREFIX}내전 팀장 <1|2> @유저** — 해당 팀의 팀장 변경\n"
                f"• **관리 패널(에페메랄)** — 로비/팀 현황 메시지의 **관리** 버튼으로 진입\n"
                f"  - 기능: **대기열 승격(1명)**, **팀장 변경**, **멤버 제외**, **멤버 교체**, **닫기**\n"
                f"• **드래프트 중 팀원 선택**은 **팀장 외에도 개최자/관리자**가 수행할 수 있습니다."
            ),
            inline=False,
        )

        # Role Shop (admin)
        embed.add_field(
            name="🛎️ 역할 상점(role_shop) — 관리자",
            value=(
                f"• **{PREFIX}상점-리로드** — `config.ini` 변경사항 반영\n"
                f"• 사용자 구매/사용은: {shop_place}"
            ),
            inline=False,
        )

        # Economy (admin)
        embed.add_field(
            name="💰 경제(포인트) — 관리자",
            value=(
                f"• **{PREFIX}지급 @유저1 [@유저2 ...] 금액** — 여러 명/한 명 일괄 지급 (예: `{PREFIX}지급 @A @B 5000`)\n"
                f"• **{PREFIX}회수 @유저 양:n** — 포인트 회수\n"
                f"• **{PREFIX}도박 초기화 @유저** — 해당 유저 도박 쿨타임 초기화\n"
                f"• **{PREFIX}초기화** (별칭: **{PREFIX}@초기화**, **{PREFIX}포인트초기화**) — 현재 서버 유저 전원 포인트를 **0 {CURRENCY}**로 초기화"
            ),
            inline=False,
        )


        embed.set_footer(text="일반 사용자 명령은 **.도움** 을 참고하세요.")
        return embed
    

    # -----------------------
    # 공개 명령: .도움 / .도움 관리자
    # -----------------------
    @commands.group(name="도움", aliases=["help", "명령어"], invoke_without_command=True)
    async def help_group(self, ctx: commands.Context):
        """일반 사용자 도움말"""
        await ctx.send(embed=self._build_user_embed())

    @help_group.command(name="관리자")
    async def help_admin(self, ctx: commands.Context):
        """관리자 도움말"""
        await ctx.send(embed=self._build_admin_embed())


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpKorCog(bot))
