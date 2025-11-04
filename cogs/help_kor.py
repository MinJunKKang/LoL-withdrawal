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
    """한국어 도움말(.도움) 전용 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="도움", aliases=["help", "명령어"])
    async def help_command(self, ctx: commands.Context):
        shop_channel = _get_purchase_channel_mention()
        shop_place = shop_channel or "지정 채널"

        embed = discord.Embed(
            title="🧭 명령어 안내",
            description=f"접두사(prefix)는 **`{PREFIX}`** 입니다.",
            color=discord.Color.blurple()
        )

        # 1) Economy (economy.py)
        embed.add_field(
            name="💰 경제(포인트)",
            value=(
                f"• **{PREFIX}지갑 [@유저]** — 포인트 보유량 확인\n"
                f"• **{PREFIX}지급 @유저 양:n** — (관리자) 포인트 지급\n"
                f"• **{PREFIX}회수 @유저 양:n** — (관리자) 포인트 회수\n"
                f"• **{PREFIX}도박 n** — 1≤n≤30, **성공 1/2**, 당첨 시 2배 지급, 유저별 쿨타임 **12시간**\n"
                f"• **{PREFIX}도박 초기화 @유저** — (관리자) 해당 유저 도박 쿨타임 초기화"
            ),
            inline=False
        )

        # 2) Match (match.py)
        embed.add_field(
            name="⚔️ 내전(match)",
            value=(
                f"• **{PREFIX}내전 모집** — 내전 로비 생성(슬롯/대기열)\n"
                f"• **{PREFIX}내전 참여** — 텍스트로 즉시 참여(또는 버튼 사용)\n"
                f"• 로비 버튼: **참여/대기/취소/시작/종료**\n"
                f"• 팀장 선택 → 드래프트 진행 → 팀 구성 완료\n"
                f"• 결과 버튼: **1팀 승리 / 2팀 승리 / 취소**\n"
                f"    - 승리팀 전원 **+5 {CURRENCY}**, 패배팀 전원 **+3 {CURRENCY}** 자동 지급\n"
                f"    - 전적(update_result_dual) 자동 반영\n"
                f"• 팀별 **OP.GG 멀티서치** 버튼 자동 제공"
            ),
            inline=False
        )

        # 3) Role Shop (role_shop.py)
        embed.add_field(
            name="🛎️ 역할 상점(role_shop)",
            value=(
                f"• **{PREFIX}상점** (별칭: 역할목록/칭호목록) — 구매 가능 칭호와 가격 표시\n"
                f"• **{PREFIX}구입 번호** — 순차 구매(하위 칭호 **유지**), 포인트 차감 후 역할 부여\n"
                f"• **{PREFIX}상점-리로드** — (관리자) `config.ini` 변경사항 반영\n"
                f"• 사용 채널: {shop_place}"
            ),
            inline=False
        )

        embed.set_footer(text="궁금한 점은 운영진에게 문의하세요!")
        await ctx.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(HelpKorCog(bot))
