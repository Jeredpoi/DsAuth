from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from stats_db import get_stats


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="stats", description="Моя статистика форм")
    @app_commands.describe(period="За какой период")
    @app_commands.choices(period=[
        app_commands.Choice(name="7 дней", value="7d"),
        app_commands.Choice(name="30 дней", value="30d"),
        app_commands.Choice(name="Всё время", value="all"),
    ])
    async def stats_cmd(self, interaction: discord.Interaction, period: str = "all"):
        if period == "7d":
            label, since = "7 дней", int((datetime.now() - timedelta(days=7)).timestamp())
        elif period == "30d":
            label, since = "30 дней", int((datetime.now() - timedelta(days=30)).timestamp())
        else:
            label, since = "Всё время", 0

        s = get_stats(interaction.user.id, since)

        embed = discord.Embed(
            title="📊 Ваша статистика",
            description=f"Период: **{label}**",
            color=0x3498DB,
        )
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="📤 Отправлено", value=str(s["sent"]), inline=True)
        embed.add_field(name="✅ Одобрено", value=str(s["approved"]), inline=True)
        embed.add_field(name="❌ Отклонено", value=str(s["rejected"]), inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
