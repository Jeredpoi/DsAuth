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

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay(f"## 📊 Ваша статистика\n-# Период: **{label}**"),
                accessory=discord.ui.Thumbnail(interaction.user.display_avatar.url),
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"📤 **Отправлено:** {s['sent']}\n"
                f"✅ **Одобрено:** {s['approved']}\n"
                f"❌ **Отклонено:** {s['rejected']}"
            ),
            accent_color=0x3498DB,
        ))

        await interaction.response.send_message(view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
