from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from helpers import RULES, PUNISHMENTS, build_form, fmt_date


# ─── Autocomplete ─────────────────────────────────────────────────────────────

async def rule_autocomplete(interaction: discord.Interaction, current: str):
    results = []
    for rule_id, rule_text in RULES.items():
        label = f"{rule_id} — {rule_text}"
        if current.lower() in label.lower():
            results.append(app_commands.Choice(name=label[:100], value=rule_id))
        if len(results) >= 25:
            break
    return results


async def punishment_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=p, value=p)
        for p in PUNISHMENTS
        if current.lower() in p.lower()
    ][:25]


# ─── Proof buttons ────────────────────────────────────────────────────────────

class ProofView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Одобрить", style=discord.ButtonStyle.success, custom_id="proof:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.set_footer(text=f"✅ Одобрено: {interaction.user} ({interaction.user.id})")
        mid = interaction.message.id
        done = discord.ui.View()
        done.add_item(discord.ui.Button(label="✅ Одобрено", style=discord.ButtonStyle.success,
                                        disabled=True, custom_id=f"done:a:{mid}"))
        done.add_item(discord.ui.Button(label="❌ Отклонить", style=discord.ButtonStyle.danger,
                                        disabled=True, custom_id=f"done:r:{mid}"))
        await interaction.message.edit(embed=embed, view=done)
        await interaction.response.send_message("✅ Доказательство одобрено.", ephemeral=True)

    @discord.ui.button(label="❌ Отклонить", style=discord.ButtonStyle.danger, custom_id="proof:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.set_footer(text=f"❌ Отклонено: {interaction.user} ({interaction.user.id})")
        mid = interaction.message.id
        done = discord.ui.View()
        done.add_item(discord.ui.Button(label="✅ Одобрить", style=discord.ButtonStyle.success,
                                        disabled=True, custom_id=f"done:a:{mid}"))
        done.add_item(discord.ui.Button(label="❌ Отклонено", style=discord.ButtonStyle.danger,
                                        disabled=True, custom_id=f"done:r:{mid}"))
        await interaction.message.edit(embed=embed, view=done)
        await interaction.response.send_message("❌ Доказательство отклонено.", ephemeral=True)


# ─── Cog ─────────────────────────────────────────────────────────────────────

class ProofCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="proof", description="Отправить доказательство нарушения")
    @app_commands.describe(
        user="Нарушитель",
        rule="Пункт правил (2.1, 3.1 и т.д.)",
        punishment="Выданное наказание",
        evidence="Скриншот доказательства (необязательно)",
    )
    @app_commands.autocomplete(rule=rule_autocomplete, punishment=punishment_autocomplete)
    async def proof_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        punishment: str,
        evidence: discord.Attachment | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        cfg = self.bot.cfg
        proof_ch_id = cfg.get("proof_channel_id", 0)
        review_role_id = cfg.get("review_role_id", 0)

        if not proof_ch_id:
            await interaction.followup.send(
                "❌ Канал доказательств не настроен. Используйте `/setup`.", ephemeral=True
            )
            return

        proof_channel = interaction.guild.get_channel(proof_ch_id)
        if not proof_channel:
            await interaction.followup.send(
                "❌ Канал не найден. Проверьте `/setup`.", ephemeral=True
            )
            return

        rule_text = RULES.get(rule, rule)
        embed = discord.Embed(
            title="📋 Доказательство нарушения",
            color=0x3498DB,
            timestamp=datetime.now(),
        )
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="Нарушитель", value=f"{user.mention}\n`{user.id}`", inline=True)
        embed.add_field(name="Пункт правил", value=f"`{rule}` — {rule_text}", inline=True)
        embed.add_field(name="Наказание", value=punishment, inline=True)
        embed.add_field(name="Модератор", value=interaction.user.mention, inline=True)
        embed.add_field(name="Дата", value=fmt_date(datetime.now()), inline=True)
        if evidence:
            embed.set_image(url=evidence.url)
        embed.set_footer(text="Ожидает проверки...")

        mention = f"<@&{review_role_id}>" if review_role_id else None
        await proof_channel.send(content=mention, embed=embed, view=ProofView())

        form_text = build_form(
            cfg, user, rule, punishment,
            evidence_url=evidence.url if evidence else "",
        )
        try:
            await interaction.user.send(f"📝 **Форма для отчёта:**\n```\n{form_text}\n```")
        except discord.Forbidden:
            pass

        await interaction.followup.send(
            f"✅ Доказательство отправлено в {proof_channel.mention}!", ephemeral=True
        )

    @app_commands.command(name="banform", description="Сгенерировать форму бана")
    @app_commands.describe(
        user="Нарушитель",
        rule="Пункт правил",
        punishment="Наказание (по умолчанию: Бан 7-15 дней)",
        evidence="Скриншот доказательства (необязательно)",
    )
    @app_commands.autocomplete(rule=rule_autocomplete, punishment=punishment_autocomplete)
    async def banform_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        punishment: str = "Бан 7-15 дней",
        evidence: discord.Attachment | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        cfg = self.bot.cfg
        form_text = build_form(
            cfg, user, rule, punishment,
            evidence_url=evidence.url if evidence else "",
        )
        embed = discord.Embed(title="🔨 Форма бана", color=0xE74C3C, timestamp=datetime.now())
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="Нарушитель", value=f"{user.mention} (`{user.id}`)", inline=False)
        embed.add_field(name="Пункт правил", value=f"`{rule}` — {RULES.get(rule, rule)}", inline=False)
        embed.add_field(name="Наказание", value=punishment, inline=False)
        if evidence:
            embed.set_image(url=evidence.url)

        try:
            await interaction.user.send(
                content=f"📝 **Форма для отчёта:**\n```\n{form_text}\n```", embed=embed
            )
            await interaction.followup.send("✅ Форма бана отправлена в личку!", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                content=f"📝 **Форма для отчёта:**\n```\n{form_text}\n```",
                embed=embed, ephemeral=True,
            )

    @app_commands.command(name="gbanform", description="Сгенерировать форму глобального бана")
    @app_commands.describe(
        user="Нарушитель",
        rule="Пункт правил",
        evidence="Скриншот доказательства (необязательно)",
    )
    @app_commands.autocomplete(rule=rule_autocomplete)
    async def gbanform_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        evidence: discord.Attachment | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        cfg = self.bot.cfg
        form_text = build_form(
            cfg.get("moderator_nick", "Ваш_Nick_Name"),
            user, rule, "Глобальная блокировка",
            evidence_url=evidence.url if evidence else "",
        )
        embed = discord.Embed(title="🌐 Форма глобального бана", color=0x8B0000, timestamp=datetime.now())
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="Нарушитель", value=f"{user.mention} (`{user.id}`)", inline=False)
        embed.add_field(name="Пункт правил", value=f"`{rule}` — {RULES.get(rule, rule)}", inline=False)
        embed.add_field(name="Наказание", value="Глобальная блокировка", inline=False)
        if evidence:
            embed.set_image(url=evidence.url)

        try:
            await interaction.user.send(
                content=f"📝 **Форма для отчёта:**\n```\n{form_text}\n```", embed=embed
            )
            await interaction.followup.send("✅ Форма G-бана отправлена в личку!", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                content=f"📝 **Форма для отчёта:**\n```\n{form_text}\n```",
                embed=embed, ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(ProofCog(bot))
    bot.add_view(ProofView())
