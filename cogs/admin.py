import time

import discord
from discord import app_commands
from discord.ext import commands

from helpers import save_config

START_TIME = time.time()


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─── !sync (скрытая команда, только для владельца) ────────────────────
    @commands.command(name="sync", hidden=True)
    async def sync_cmd(self, ctx: commands.Context):
        """Мгновенная синхронизация slash-команд на текущем сервере."""
        owner_id = getattr(self.bot, "owner_id_cfg", 0)
        is_owner = ctx.author.id == owner_id or await self.bot.is_owner(ctx.author)
        if not is_owner:
            return
        synced = await self.bot.tree.sync(guild=ctx.guild)
        await ctx.send(f"✅ Синхронизировано {len(synced)} команд на этом сервере.", delete_after=5)

    # ─── /setup ───────────────────────────────────────────────────────────
    @app_commands.command(name="setup", description="Настройка бота (только для владельца)")
    @app_commands.describe(
        proof_channel="Канал куда постить доказательства",
        review_role="Роль, которая получает уведомление и может одобрять",
        moderator_nick="Ваш ник для форм отчётов",
    )
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        proof_channel: discord.TextChannel | None = None,
        review_role: discord.Role | None = None,
        moderator_nick: str | None = None,
    ):
        owner_id = getattr(self.bot, "owner_id_cfg", 0)
        is_owner = interaction.user.id == owner_id or await self.bot.is_owner(interaction.user)
        if not is_owner:
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return

        cfg = self.bot.cfg
        if proof_channel:
            cfg["proof_channel_id"] = proof_channel.id
        if review_role:
            cfg["review_role_id"] = review_role.id
        if moderator_nick:
            cfg["moderator_nick"] = moderator_nick
        save_config(cfg)

        ch = interaction.guild.get_channel(cfg.get("proof_channel_id", 0))
        role_id = cfg.get("review_role_id", 0)
        lines = [
            "✅ **Настройки сохранены:**",
            f"• Канал доказательств: {ch.mention if ch else 'не задан'}",
            f"• Роль проверяющих: {'<@&' + str(role_id) + '>' if role_id else 'не задана'}",
            f"• Ник модератора: `{cfg.get('moderator_nick', 'не задан')}`",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ─── /uptime ──────────────────────────────────────────────────────────
    @app_commands.command(name="uptime", description="Время работы бота")
    async def uptime_cmd(self, interaction: discord.Interaction):
        elapsed = int(time.time() - START_TIME)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        await interaction.response.send_message(
            f"⏱️ Бот работает: **{h}ч {m}м {s}с**", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
