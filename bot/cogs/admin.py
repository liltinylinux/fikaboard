from __future__ import annotations
import os, discord
from discord import app_commands
from discord.ext import commands
from shared.util import open_db

DB_PATH = os.getenv("DB_PATH", "./fika.db")
ADMIN_ROLE = os.getenv("ADMIN_ROLE", "Admin")

class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _is_admin(self, inter: discord.Interaction) -> bool:
        if inter.user.guild_permissions.administrator:
            return True
        if ADMIN_ROLE and isinstance(inter.user, discord.Member):
            return any(r.name == ADMIN_ROLE for r in inter.user.roles)
        return False

    @app_commands.command(name="quest_rotate_now", description="Force-rotate weekly quests NOW")
    async def quest_rotate_now(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message("Nope.", ephemeral=True)
            return
        async with open_db(DB_PATH) as db:
            await db.execute("UPDATE quests SET active=0 WHERE active=1")
            await db.execute(
                "INSERT INTO quests(key, title, type, event_type, target, start_ts, end_ts, active) VALUES(?,?,?,?,?,datetime('now'),datetime('now','+7 days'),1)",
                ("dogtags_week", "Collect 5 dog tags", "count_event", "DOGTAG", 5),
            )
            await db.execute(
                "INSERT INTO quests(key, title, type, event_type, target, start_ts, end_ts, active) VALUES(?,?,?,?,?,datetime('now'),datetime('now','+7 days'),1)",
                ("survive_week", "Survive 5 raids", "count_event", "SURVIVE", 5),
            )
            await db.commit()
        await interaction.response.send_message("Rotated.")

async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
