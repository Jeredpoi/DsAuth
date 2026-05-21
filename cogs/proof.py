import re
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from helpers import (
    RULES, PUNISHMENTS, build_form, fmt_date, date_end,
    get_guild_cfg, get_member_rank_level, build_command,
    APPROVE_MIN_RANK, RANKS,
)
from cogs.servers import get_server_for_member, get_proof_channel, get_log_channel, ensure_server_channels
from stats_db import record_form


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


async def server_autocomplete(interaction: discord.Interaction, current: str):
    cfg = interaction.client.cfg
    from helpers import get_guild_cfg
    guild_cfg = get_guild_cfg(cfg, interaction.guild_id)
    known = list(guild_cfg.get("servers", {}).keys())
    known += list(guild_cfg.get("user_servers", {}).values())
    seen, choices = set(), []
    for s in known:
        if s not in seen and current in s:
            choices.append(app_commands.Choice(name=f"Сервер {s}", value=s))
            seen.add(s)
    return choices[:25]


async def punishment_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=p, value=p)
        for p in PUNISHMENTS
        if current.lower() in p.lower()
    ][:25]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _extract_embed_data(embed: discord.Embed) -> dict:
    data = {}
    for field in embed.fields:
        name_lower = field.name.lower()
        if "нарушитель" in name_lower:
            m = re.search(r"`(\d{15,20})`", field.value)
            if m:
                data["user_id"] = int(m.group(1))
        elif "наказание" in name_lower:
            data["punishment"] = field.value
        elif "пункт" in name_lower:
            m = re.search(r"`?(\d+\.\d+)`?", field.value)
            if m:
                data["rule_id"] = m.group(1)
        elif "модератор" in name_lower:
            m = re.search(r"<@(\d+)>", field.value)
            if m:
                data["mod_id"] = int(m.group(1))
    return data


def _form_type_from_title(title: str) -> str:
    t = title.lower()
    if "глобального" in t:
        return "gbanform"
    if "бана" in t or "бан" in t:
        return "banform"
    return "proof"


async def _post_to_log(guild: discord.Guild, cfg: dict, server: str, embed: discord.Embed):
    log_ch = get_log_channel(guild, cfg, server)
    if not log_ch:
        return
    await log_ch.send(embed=embed.copy())


# ─── ProofView ────────────────────────────────────────────────────────────────

class ProofView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Одобрить", style=discord.ButtonStyle.success, custom_id="proof:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        form_type = _form_type_from_title(embed.title or "")
        min_level = APPROVE_MIN_RANK.get(form_type, 2)
        approver_level = get_member_rank_level(interaction.user)

        if approver_level < min_level:
            needed = RANKS[min_level - 1]
            await interaction.response.send_message(
                f"❌ Недостаточно прав. Требуется минимум: **{needed}**.", ephemeral=True
            )
            return

        data = _extract_embed_data(embed)
        user_id = data.get("user_id", 0)
        punishment = data.get("punishment", "")
        rule_id = data.get("rule_id", "")
        mod_id = data.get("mod_id", 0)

        command = build_command(punishment, user_id, rule_id)

        embed.color = discord.Color.green()
        rank_display = next((r.name for r in reversed(interaction.user.roles)
                             if r.name in RANKS), "—")
        embed.set_footer(text=f"✅ Одобрено: {interaction.user} ({rank_display})")

        if command:
            embed.add_field(name="💻 Команда для исполнения", value=f"```\n{command}\n```", inline=False)

        mid = interaction.message.id
        done = discord.ui.View()
        done.add_item(discord.ui.Button(label="✅ Одобрено", style=discord.ButtonStyle.success,
                                        disabled=True, custom_id=f"done:a:{mid}"))
        done.add_item(discord.ui.Button(label="❌ Отклонить", style=discord.ButtonStyle.danger,
                                        disabled=True, custom_id=f"done:r:{mid}"))
        await interaction.message.edit(embed=embed, view=done)

        record_form(mod_id, form_type, "approved")

        server = get_server_for_member(interaction.user, interaction.client.cfg)
        if server:
            await _post_to_log(interaction.guild, interaction.client.cfg, server, embed)

        await interaction.response.send_message("✅ Форма одобрена.", ephemeral=True)

    @discord.ui.button(label="❌ Отклонить", style=discord.ButtonStyle.danger, custom_id="proof:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        form_type = _form_type_from_title(embed.title or "")
        data = _extract_embed_data(embed)
        mod_id = data.get("mod_id", 0)

        embed.color = discord.Color.red()
        rank_display = next((r.name for r in reversed(interaction.user.roles)
                             if r.name in RANKS), "—")
        embed.set_footer(text=f"❌ Отклонено: {interaction.user} ({rank_display})")

        mid = interaction.message.id
        done = discord.ui.View()
        done.add_item(discord.ui.Button(label="✅ Одобрить", style=discord.ButtonStyle.success,
                                        disabled=True, custom_id=f"done:a:{mid}"))
        done.add_item(discord.ui.Button(label="❌ Отклонено", style=discord.ButtonStyle.danger,
                                        disabled=True, custom_id=f"done:r:{mid}"))
        await interaction.message.edit(embed=embed, view=done)

        record_form(mod_id, form_type, "rejected")

        server = get_server_for_member(interaction.user, interaction.client.cfg)
        if server:
            await _post_to_log(interaction.guild, interaction.client.cfg, server, embed)

        await interaction.response.send_message("❌ Форма отклонена.", ephemeral=True)


# ─── Общая логика постинга формы в канал ─────────────────────────────────────

async def _post_form(
    interaction: discord.Interaction,
    embed: discord.Embed,
    form_text: str,
    evidence: discord.Attachment | None,
    with_buttons: bool = True,
    server_override: str | None = None,
):
    guild = interaction.guild
    cfg = interaction.client.cfg

    # 1. Пробуем канал конкретного сервера (явный параметр или по роли)
    server = server_override or get_server_for_member(interaction.user, cfg)
    proof_ch = None

    if server:
        proof_ch = get_proof_channel(guild, cfg, server)

    # 2. Запасной вариант: глобальный proof_channel_id из /setup
    if not proof_ch:
        guild_cfg_data = get_guild_cfg(cfg, guild.id)
        proof_ch = guild.get_channel(guild_cfg_data.get("proof_channel_id", 0))

    # 3. Ещё нет? Пробуем создать каналы для сервера автоматически
    if not proof_ch and server:
        try:
            await ensure_server_channels(guild, server, cfg)
            proof_ch = get_proof_channel(guild, cfg, server)
        except discord.Forbidden:
            pass

    if not proof_ch:
        if server:
            await interaction.followup.send(
                f"❌ Канал для форм не найден (сервер **{server}**).\n"
                f"Попросите владельца запустить `/setupserver {server}` или настроить `/setup`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "❌ У вас нет роли сервера (1–90). Пройдите авторизацию или попросите выдать роль сервера.",
                ephemeral=True,
            )
        return

    guild_cfg = get_guild_cfg(cfg, guild.id)
    review_role_id = guild_cfg.get("review_role_id", 0)
    mention = f"<@&{review_role_id}>" if review_role_id else None

    view = ProofView() if with_buttons else None
    await proof_ch.send(content=mention, embed=embed, view=view)

    form_type = _form_type_from_title(embed.title or "")
    record_form(interaction.user.id, form_type, "sent")

    try:
        await interaction.user.send(f"📝 **Форма для отчёта:**\n```\n{form_text}\n```")
    except discord.Forbidden:
        pass

    await interaction.followup.send(f"✅ Отправлено в {proof_ch.mention}!", ephemeral=True)


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
        server="Номер сервера (если не определяется автоматически)",
    )
    @app_commands.autocomplete(rule=rule_autocomplete, punishment=punishment_autocomplete, server=server_autocomplete)
    async def proof_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        punishment: str,
        evidence: discord.Attachment | None = None,
        server: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        rule_text = RULES.get(rule, rule)
        now = datetime.now()
        embed = discord.Embed(title="📋 Доказательство нарушения", color=0x3498DB, timestamp=now)
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="Нарушитель", value=f"{user.mention} `{user.id}`", inline=False)
        embed.add_field(name="Пункт правил", value=f"`{rule}` — {rule_text}", inline=False)
        embed.add_field(name="Наказание", value=punishment, inline=False)
        embed.add_field(name="Модератор", value=interaction.user.mention, inline=False)
        if evidence:
            embed.set_image(url=evidence.url)

        form_text = build_form(self.bot.cfg, user, rule, punishment,
                               evidence_url=evidence.url if evidence else "")
        await _post_form(interaction, embed, form_text, evidence, with_buttons=False, server_override=server)

    @app_commands.command(name="banform", description="Сгенерировать форму бана")
    @app_commands.describe(
        user="Нарушитель", rule="Пункт правил",
        punishment="Наказание (по умолчанию: Бан 7-15 дней)",
        evidence="Скриншот (необязательно)",
        server="Номер сервера (если не определяется автоматически)",
    )
    @app_commands.autocomplete(rule=rule_autocomplete, punishment=punishment_autocomplete, server=server_autocomplete)
    async def banform_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        punishment: str = "Бан 7-15 дней",
        evidence: discord.Attachment | None = None,
        server: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if user.id == interaction.user.id:
            await interaction.followup.send("❌ Нельзя выдать наказание самому себе.", ephemeral=True)
            return

        now = datetime.now()
        embed = discord.Embed(title="🔨 Форма бана", color=0xE74C3C, timestamp=now)
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="👤 Нарушитель", value=f"{user.mention}\n`{user.id}`", inline=True)
        embed.add_field(name="⚖️ Наказание", value=punishment, inline=True)
        embed.add_field(name="​", value="​", inline=True)
        embed.add_field(name="📖 Пункт правил", value=f"`{rule}` — {RULES.get(rule, rule)}", inline=False)
        embed.add_field(name="📅 Выдано", value=fmt_date(now), inline=True)
        embed.add_field(name="🗓️ Снятие", value=date_end(punishment), inline=True)
        embed.add_field(name="🛡️ Модератор", value=interaction.user.mention, inline=True)
        if evidence:
            embed.set_image(url=evidence.url)
        embed.set_footer(text="⏳ Требует одобрения: Старший модератор+")

        form_text = build_form(self.bot.cfg, user, rule, punishment,
                               evidence_url=evidence.url if evidence else "")
        await _post_form(interaction, embed, form_text, evidence, with_buttons=True, server_override=server)

    @app_commands.command(name="gbanform", description="Сгенерировать форму глобального бана")
    @app_commands.describe(
        user="Нарушитель", rule="Пункт правил",
        evidence="Скриншот (необязательно)",
        server="Номер сервера (если не определяется автоматически)",
    )
    @app_commands.autocomplete(rule=rule_autocomplete, server=server_autocomplete)
    async def gbanform_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        evidence: discord.Attachment | None = None,
        server: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if user.id == interaction.user.id:
            await interaction.followup.send("❌ Нельзя выдать наказание самому себе.", ephemeral=True)
            return

        now = datetime.now()
        embed = discord.Embed(title="🌐 Форма глобального бана", color=0x8B0000, timestamp=now)
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="👤 Нарушитель", value=f"{user.mention}\n`{user.id}`", inline=True)
        embed.add_field(name="⚖️ Наказание", value="Глобальная блокировка", inline=True)
        embed.add_field(name="​", value="​", inline=True)
        embed.add_field(name="📖 Пункт правил", value=f"`{rule}` — {RULES.get(rule, rule)}", inline=False)
        embed.add_field(name="📅 Выдано", value=fmt_date(now), inline=True)
        embed.add_field(name="🗓️ Снятие", value="Перманентно", inline=True)
        embed.add_field(name="🛡️ Модератор", value=interaction.user.mention, inline=True)
        if evidence:
            embed.set_image(url=evidence.url)
        embed.set_footer(text="⏳ Требует одобрения: Куратор модерации+")

        form_text = build_form(self.bot.cfg, user, rule, "Глобальная блокировка",
                               evidence_url=evidence.url if evidence else "")
        await _post_form(interaction, embed, form_text, evidence, with_buttons=True, server_override=server)


async def setup(bot: commands.Bot):
    await bot.add_cog(ProofCog(bot))
    bot.add_view(ProofView())
