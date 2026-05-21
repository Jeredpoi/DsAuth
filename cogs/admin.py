import time

import discord
from discord import app_commands
from discord.ext import commands

from helpers import save_config, DEFAULT_TEMPLATE, get_guild_cfg, RULES

START_TIME = time.time()

FORM_TYPES = {
    "general": "Общий (fallback)",
    "oral":    "Устное предупреждение",
    "warn":    "Предупреждение",
    "mute":    "Мут",
    "ban":     "Бан / блокировка",
    "gban":    "Глобальный бан",
}


# ─── Модальное окно редактирования правила ────────────────────────────────────

class EditRuleModal(discord.ui.Modal, title="Добавить / изменить правило"):
    rule_id_input = discord.ui.TextInput(
        label="ID правила (например 2.1)",
        placeholder="2.1",
        max_length=8,
    )
    rule_text_input = discord.ui.TextInput(
        label="Название правила",
        placeholder="Неадекватное поведение",
        max_length=200,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        rid = self.rule_id_input.value.strip()
        rtext = self.rule_text_input.value.strip()
        self.bot.cfg.setdefault("rules", {})[rid] = rtext
        save_config(self.bot.cfg)
        RULES[rid] = rtext
        await interaction.response.send_message(
            f"✅ Правило `{rid}` → **{rtext}**", ephemeral=True
        )


# ─── Модальное окно редактирования шаблона ───────────────────────────────────

class FormTemplateModal(discord.ui.Modal):
    template_input = discord.ui.TextInput(
        label="Шаблон формы",
        style=discord.TextStyle.paragraph,
        placeholder=(
            "Переменные: {moderatorNick} {userId} {userTag}\n"
            "{ruleId} {ruleText} {punishment}\n"
            "{dateIssued} {dateEnd} {evidence}"
        ),
        max_length=1800,
        required=True,
    )

    def __init__(self, bot: commands.Bot, form_type: str, current_value: str):
        super().__init__(title=f"Шаблон: {FORM_TYPES.get(form_type, form_type)}")
        self.bot = bot
        self.form_type = form_type
        self.template_input.default = current_value or DEFAULT_TEMPLATE

    async def on_submit(self, interaction: discord.Interaction):
        self.bot.cfg.setdefault("templates", {})[self.form_type] = self.template_input.value
        save_config(self.bot.cfg)
        await interaction.response.send_message(
            f"✅ Шаблон **{FORM_TYPES.get(self.form_type)}** сохранён.", ephemeral=True
        )


# ─── Cog ─────────────────────────────────────────────────────────────────────

class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        uid = interaction.user.id
        global_owner = getattr(self.bot, "owner_id_cfg", 0)
        guild_owner = interaction.guild.owner_id if interaction.guild else 0
        return uid in (global_owner, guild_owner)

    # ─── /sync ────────────────────────────────────────────────────────────
    @app_commands.command(name="sync", description="Синхронизировать команды на этом сервере")
    async def sync_slash(self, interaction: discord.Interaction):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        synced = await self.bot.tree.sync(guild=interaction.guild)
        await interaction.followup.send(f"✅ Синхронизировано {len(synced)} команд.", ephemeral=True)

    # ─── /setup ───────────────────────────────────────────────────────────
    @app_commands.command(name="setup", description="Настройка бота (только для владельца)")
    @app_commands.describe(
        proof_channel="Канал для доказательств",
        review_role="Роль проверяющих (получают пинг)",
        moderator_nick="Ваш ник для форм",
    )
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        proof_channel: discord.TextChannel | None = None,
        review_role: discord.Role | None = None,
        moderator_nick: str | None = None,
    ):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return

        cfg = self.bot.cfg
        guild_cfg = get_guild_cfg(cfg, interaction.guild_id)

        if proof_channel:
            guild_cfg["proof_channel_id"] = proof_channel.id
        if review_role:
            guild_cfg["review_role_id"] = review_role.id
        if moderator_nick:
            cfg["moderator_nick"] = moderator_nick
        save_config(cfg)

        ch = interaction.guild.get_channel(guild_cfg.get("proof_channel_id", 0))
        role_id = guild_cfg.get("review_role_id", 0)
        lines = [
            f"✅ **Настройки сохранены** (guild_id: `{interaction.guild_id}`):",
            f"• Канал доказательств: {ch.mention if ch else '❌ не задан — укажи `proof_channel`'}",
            f"• Роль проверяющих: {'<@&' + str(role_id) + '>' if role_id else 'не задана'}",
            f"• Ник модератора: `{cfg.get('moderator_nick', 'не задан')}`",
            "",
            "Для настройки шаблонов форм используйте `/setform`.",
            "Для настройки системы авторизации используйте `/setup-auth`.",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ─── /assignserver ────────────────────────────────────────────────────
    @app_commands.command(name="assignserver", description="Вручную привязать участника к серверу (только владелец)")
    @app_commands.describe(member="Участник", server="Номер сервера (1–90)")
    async def assignserver_cmd(self, interaction: discord.Interaction, member: discord.Member, server: str):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return
        if not server.isdigit() or not (1 <= int(server) <= 90):
            await interaction.response.send_message("❌ Укажите число от 1 до 90.", ephemeral=True)
            return

        guild_cfg = get_guild_cfg(self.bot.cfg, interaction.guild_id)
        guild_cfg.setdefault("user_servers", {})[str(member.id)] = server
        save_config(self.bot.cfg)

        await interaction.response.send_message(
            f"✅ {member.mention} привязан к серверу **{server}** в базе данных.\n"
            f"Теперь `/proof` будет направлять формы в канал сервера {server}.",
            ephemeral=True,
        )

    # ─── /setform ─────────────────────────────────────────────────────────
    @app_commands.command(name="setform", description="Настроить шаблон формы (только для владельца)")
    @app_commands.describe(type="Тип наказания для которого настраивается шаблон")
    @app_commands.choices(type=[
        app_commands.Choice(name=label, value=key)
        for key, label in FORM_TYPES.items()
    ])
    async def setform_cmd(self, interaction: discord.Interaction, type: str):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return

        current = self.bot.cfg.get("templates", {}).get(type, "")
        modal = FormTemplateModal(self.bot, type, current)
        await interaction.response.send_modal(modal)

    # ─── /showform ────────────────────────────────────────────────────────
    @app_commands.command(name="showform", description="Показать текущий шаблон формы")
    @app_commands.describe(type="Тип наказания")
    @app_commands.choices(type=[
        app_commands.Choice(name=label, value=key)
        for key, label in FORM_TYPES.items()
    ])
    async def showform_cmd(self, interaction: discord.Interaction, type: str):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return

        template = self.bot.cfg.get("templates", {}).get(type, "") or DEFAULT_TEMPLATE
        label = FORM_TYPES.get(type, type)
        await interaction.response.send_message(
            f"📝 **Шаблон «{label}»:**\n```\n{template}\n```\n"
            f"Переменные: `{{moderatorNick}}` `{{userId}}` `{{userTag}}` "
            f"`{{ruleId}}` `{{ruleText}}` `{{punishment}}` "
            f"`{{dateIssued}}` `{{dateEnd}}` `{{evidence}}`",
            ephemeral=True,
        )

    # ─── /editrule ────────────────────────────────────────────────────────
    @app_commands.command(name="editrule", description="Добавить или изменить правило (только владелец)")
    async def editrule_cmd(self, interaction: discord.Interaction):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return
        await interaction.response.send_modal(EditRuleModal(self.bot))

    # ─── /uptime ──────────────────────────────────────────────────────────
    @app_commands.command(name="uptime", description="Время работы бота")
    async def uptime_cmd(self, interaction: discord.Interaction):
        elapsed = int(time.time() - START_TIME)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        await interaction.response.send_message(
            f"⏱️ Бот работает: **{h}ч {m}м {s}с**"
        )

    # ─── /debugconfig ─────────────────────────────────────────────────────
    @app_commands.command(name="debugconfig", description="Показать конфиг и роли участника (только владелец)")
    @app_commands.describe(member="Участник для проверки (по умолчанию — вы)")
    async def debugconfig_cmd(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return

        target = member or interaction.user
        guild_cfg = get_guild_cfg(self.bot.cfg, interaction.guild_id)

        server_roles = [r.name for r in target.roles if r.name.isdigit() and 1 <= int(r.name) <= 90]
        rank_roles = [r.name for r in target.roles if r.name in ("Младший модератор", "Модератор", "Старший модератор", "Куратор модерации", "Заместитель главного модератора", "Главный модератор")]

        user_server_db = guild_cfg.get("user_servers", {}).get(str(target.id), "—")
        proof_ch_id = guild_cfg.get("proof_channel_id", 0)
        proof_ch = interaction.guild.get_channel(proof_ch_id)
        servers_cfg = guild_cfg.get("servers", {})

        lines = [
            f"**Участник:** {target.mention} (`{target.id}`)",
            f"**Роли сервера (1-90):** {', '.join(server_roles) or 'нет'}",
            f"**Роли должности:** {', '.join(rank_roles) or 'нет'}",
            f"**user_servers в конфиге:** `{user_server_db}`",
            f"**proof_channel_id:** {proof_ch.mention if proof_ch else f'не найден (id={proof_ch_id})'}",
            f"**Серверные каналы в конфиге:** {', '.join(servers_cfg.keys()) or 'нет'}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
