from __future__ import annotations
import os, asyncio, discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    gid = os.getenv("GUILD_ID")
    if gid:
        guild_obj = discord.Object(id=int(gid))
        await bot.tree.sync(guild=guild_obj)
    else:
        await bot.tree.sync()

async def main():
    async with bot:
        await bot.load_extension("bot.cogs.levels")
        await bot.load_extension("bot.cogs.quests")
        await bot.load_extension("bot.cogs.admin")
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
