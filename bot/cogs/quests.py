from __future__ import annotations
import os, discord
from discord import app_commands
from discord.ext import commands
from bot.db import active_quests, quest_progress_for_guild

DB_PATH = os.getenv("DB_PATH", "./fika.db")

class Quests(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="quests", description="Show active weekly quests and progress")
    async def quests(self, interaction: discord.Interaction):
        quests = await active_quests(DB_PATH)
        prog = await quest_progress_for_guild(DB_PATH)
        if not quests:
            await interaction.response.send_message("No active quests.")
            return
        emb = discord.Embed(title="Weekly Quests")
        for q in quests:
            lines = [f"**{q['title']}** — target: {q['target']} ({q['start_ts']} → {q['end_ts']})"]
            for row in [r for r in prog if r['title'] == q['title']][:10]:
                lines.append(f"• {row['game_name']}: {row['progress']}/{q['target']}")
            emb.add_field(name=q['title'], value="\n".join(lines), inline=False)
        await interaction.response.send_message(embed=emb)

async def setup(bot: commands.Bot):
    await bot.add_cog(Quests(bot))
