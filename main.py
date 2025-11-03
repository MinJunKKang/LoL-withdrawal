# main.py
import os
import configparser
import discord
from discord.ext import commands

from cogs.match import MatchCog
from cogs.economy import EconomyCog
from cogs.stats_view import StatsCog
from cogs.role_shop import RoleShopCog

config = configparser.ConfigParser()
config.read("config.ini", encoding="utf-8")

def _get_id(section: str, key: str) -> int:
    try:
        v = config.get(section, key, fallback="0")
        return int(v) if str(v).isdigit() else 0
    except Exception:
        return 0

TOKEN = os.getenv("DISCORD_TOKEN") or config.get("Settings", "token", fallback="").strip()

ROLE_IDS = {}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=".", intents=intents)

async def setup_hook():
    await bot.add_cog(MatchCog(bot, role_ids=ROLE_IDS))
    await bot.add_cog(EconomyCog(bot))
    await bot.add_cog(StatsCog(bot))
    await bot.add_cog(RoleShopCog(bot))

bot.setup_hook = setup_hook

@bot.event
async def on_ready():
    print(f"봇 로그인됨: {bot.user} (prefix='.')")

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN이 비어 있습니다. config.ini 또는 환경변수를 확인하세요.")
    bot.run(TOKEN)
