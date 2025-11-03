# cogs/stats_view.py
import discord
from discord.ext import commands
from utils.stats import load_stats, ensure_user, format_num

def _recent_text(hist: list[int], n: int = 10) -> tuple[str, float]:
    if not hist:
        return "-", 0.0
    last = hist[-n:]
    wins = sum(1 for x in last if x == 1)
    games = len(last)
    rate = round(wins / games * 100, 2) if games else 0.0
    # ✅ / ❌ 표시
    series = " ".join("✅" if x == 1 else "❌" for x in last)
    return series, rate

class StatsCog(commands.Cog):
    """전적 조회 (.전적 [@유저])"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="전적")
    async def show_stats(self, ctx: commands.Context, member: discord.Member | None = None):
        target = member or ctx.author

        stats = load_stats()
        rec = ensure_user(stats, str(target.id))

        total = int(rec.get("참여", 0))
        wins  = int(rec.get("승리", 0))
        losses = int(rec.get("패배", 0))
        winrate = round((wins / total * 100), 2) if total else 0.0

        series, rate10 = _recent_text(rec.get("히스토리", []), n=10)

        embed = discord.Embed(
            title=f"🎮 {target.display_name} 님의 내전 전적",
            color=discord.Color.blurple()
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        embed.add_field(
            name="총 전적",
            value=f"{format_num(total)}전 {format_num(wins)}승 {format_num(losses)}패"
                  f"\n승률 **{winrate}%**",
            inline=False
        )
        embed.add_field(
            name="최근 10경기",
            value=f"{series}\n승률 **{rate10}%**",
            inline=False
        )

        await ctx.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
