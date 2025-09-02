from __future__ import annotations
import os, discord
from discord import app_commands
from discord.ext import commands, tasks
from shared.util import env_int
from bot.db import top_players, player_card, get_meta, set_meta

DB_PATH = os.getenv("DB_PATH", "./fika.db")
LEADERBOARD_CHANNEL_ID = env_int("LEADERBOARD_CHANNEL_ID")

class Levels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.update_leaderboard.start()

    def cog_unload(self):
        self.update_leaderboard.cancel()

    @app_commands.command(name="level", description="Show a player's level card")
    @app_commands.describe(game_name="SPT/Fika in-game name")
    async def level(self, interaction: discord.Interaction, game_name: str):
        card = await player_card(DB_PATH, game_name)
        if not card:
            await interaction.response.send_message(f"No stats for **{game_name}** yet.")
            return
        emb = discord.Embed(title=f"{card['game_name']} — Lv {card['level']} ({card['xp']} XP)")
        emb.add_field(name="Kills", value=str(card['kills']))
        emb.add_field(name="Deaths", value=str(card['deaths']))
        emb.add_field(name="Extracts", value=str(card['extracts']))
        emb.add_field(name="Survivals", value=str(card['survivals']))
        emb.add_field(name="Dogtags", value=str(card['dogtags']))
        await interaction.response.send_message(embed=emb)

    @app_commands.command(name="leaderboard", description="Show top XP players")
    async def leaderboard(self, interaction: discord.Interaction):
        items = await top_players(DB_PATH, 10)
        emb = discord.Embed(title="FIKA — XP Leaderboard (Top 10)")
        lines = []
        for i, r in enumerate(items, start=1):
            lines.append(f"**{i}. {r['game_name']}** — Lv {r['level']} • {r['xp']} XP • {r['kills']}K/{r['deaths']}D")
        emb.description = "\n".join(lines) if lines else "(No data yet)"
        await interaction.response.send_message(embed=emb)

    async def get_or_create_message(self, channel: discord.TextChannel) -> discord.Message:
        msg_id = await get_meta(DB_PATH, "leaderboard_msg_id")
        if msg_id:
            try:
                return await channel.fetch_message(int(msg_id))
            except Exception:
                pass
        msg = await channel.send("(initializing leaderboard…)")
        await set_meta(DB_PATH, "leaderboard_msg_id", str(msg.id))
        return msg

    @tasks.loop(minutes=5)
    async def update_leaderboard(self):
        if LEADERBOARD_CHANNEL_ID is None:
            return
        channel = self.bot.get_channel(LEADERBOARD_CHANNEL_ID)
        if channel is None:
            return
        msg = await self.get_or_create_message(channel)
        items = await top_players(DB_PATH, 10)
        emb = discord.Embed(title="FIKA — XP Leaderboard (Top 10)")
        lines = []
        for i, r in enumerate(items, start=1):
            lines.append(f"**{i}. {r['game_name']}** — Lv {r['level']} • {r['xp']} XP • {r['kills']}K/{r['deaths']}D")
        emb.description = "\n".join(lines) if lines else "(No data yet)"
        try:
            await msg.edit(embed=emb)
        except Exception:
            new_msg = await channel.send(embed=emb)
            await set_meta(DB_PATH, "leaderboard_msg_id", str(new_msg.id))

    @update_leaderboard.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(Levels(bot))
