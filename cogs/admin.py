import time

import discord
from discord import app_commands
from discord.ext import commands

import db
from helpers import save_config, DEFAULT_TEMPLATE, get_guild_cfg, RULES, RANKS, RANK_LEVELS, UNVERIFIED_ROLE_NAME

START_TIME = time.time()

FORM_TYPES = {
    "general": "Общий (fallback)",
    "oral":    "Устное предупреждение",
    "warn":    "Предупреждение",
    "mute":    "Мут",
    "ban":     "Бан / блокировка",
    "gban":    "Глобальный бан",
}

_ADMIN_PERM = app_commands.default_permissions(administrator=True)
_LEAD_PERM  = app_commands.default_permissions(manage_roles=True)


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
    @_ADMIN_PERM
    @app_commands.command(name="sync", description="Синхронизировать команды на этом сервере")
    async def sync_slash(self, interaction: discord.Interaction):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        synced = await self.bot.tree.sync(guild=interaction.guild)
        await interaction.followup.send(f"✅ Синхронизировано {len(synced)} команд.", ephemeral=True)

    # ─── /setup ───────────────────────────────────────────────────────────
    @_ADMIN_PERM
    @app_commands.command(name="setup", description="Настройка бота")
    @app_commands.describe(
        proof_channel="Канал для форм наказаний (proof)",
        banform_channel="Канал для форм банов (banform/gbanform)",
        moderator_nick="Ваш ник для форм",
    )
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        proof_channel: discord.TextChannel | None = None,
        banform_channel: discord.TextChannel | None = None,
        moderator_nick: str | None = None,
    ):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return

        cfg = self.bot.cfg
        guild_cfg = get_guild_cfg(cfg, interaction.guild_id)

        for ch_param, key in ((proof_channel, "proof_channel_id"), (banform_channel, "banform_channel_id")):
            if ch_param:
                if ch_param.guild.id != interaction.guild_id:
                    await interaction.response.send_message(
                        f"❌ Канал {ch_param.mention} должен принадлежать этому серверу.", ephemeral=True
                    )
                    return
                guild_cfg[key] = ch_param.id

        if moderator_nick:
            cfg["moderator_nick"] = moderator_nick
        save_config(cfg)

        proof_ch   = interaction.guild.get_channel(guild_cfg.get("proof_channel_id", 0))
        banform_ch = interaction.guild.get_channel(guild_cfg.get("banform_channel_id", 0))
        lines = [
            "✅ **Настройки сохранены**:",
            f"• Канал proof: {proof_ch.mention if proof_ch else '❌ не задан'}",
            f"• Канал banform: {banform_ch.mention if banform_ch else '❌ не задан'}",
            f"• Ник модератора: `{cfg.get('moderator_nick', 'не задан')}`",
            "",
            "Для шаблонов форм: `/setform` | Для авторизации: `/setup-auth`",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ─── /assignserver ────────────────────────────────────────────────────
    @_ADMIN_PERM
    @app_commands.command(name="assignserver", description="Вручную привязать участника к серверу")
    @app_commands.describe(member="Участник", server="Номер сервера (1–90)")
    async def assignserver_cmd(self, interaction: discord.Interaction, member: discord.Member, server: str):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return
        if not server.isdigit() or not (1 <= int(server) <= 90):
            await interaction.response.send_message("❌ Укажите число от 1 до 90.", ephemeral=True)
            return

        db.set_user_server(member.id, server)
        await interaction.response.send_message(
            f"✅ {member.mention} привязан к серверу **{server}**.", ephemeral=True
        )

    # ─── /setform ─────────────────────────────────────────────────────────
    @_ADMIN_PERM
    @app_commands.command(name="setform", description="Настроить шаблон формы")
    @app_commands.describe(type="Тип наказания")
    @app_commands.choices(type=[
        app_commands.Choice(name=label, value=key)
        for key, label in FORM_TYPES.items()
    ])
    async def setform_cmd(self, interaction: discord.Interaction, type: str):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return
        current = self.bot.cfg.get("templates", {}).get(type, "")
        await interaction.response.send_modal(FormTemplateModal(self.bot, type, current))

    # ─── /showform ────────────────────────────────────────────────────────
    @_ADMIN_PERM
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
    @_ADMIN_PERM
    @app_commands.command(name="editrule", description="Добавить или изменить правило")
    async def editrule_cmd(self, interaction: discord.Interaction):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return
        await interaction.response.send_modal(EditRuleModal(self.bot))

    # ─── /uptime ──────────────────────────────────────────────────────────
    @_ADMIN_PERM
    @app_commands.command(name="uptime", description="Время работы бота")
    async def uptime_cmd(self, interaction: discord.Interaction):
        elapsed = int(time.time() - START_TIME)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        await interaction.response.send_message(
            f"⏱️ Бот работает: **{h}ч {m}м {s}с**", ephemeral=True
        )

    # ─── /debugconfig ─────────────────────────────────────────────────────
    @_ADMIN_PERM
    @app_commands.command(name="debugconfig", description="Показать конфиг и роли участника")
    @app_commands.describe(member="Участник для проверки (по умолчанию — вы)")
    async def debugconfig_cmd(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return

        target = member or interaction.user
        guild_cfg = get_guild_cfg(self.bot.cfg, interaction.guild_id)
        server_roles = [r.name for r in target.roles if r.name.isdigit() and 1 <= int(r.name) <= 90]
        rank_roles   = [r.name for r in target.roles if r.name in (
            "Младший модератор", "Модератор", "Старший модератор",
            "Куратор модерации", "Заместитель главного модератора", "Главный модератор",
        )]
        user_server_db = db.get_user_server(target.id) or "—"
        proof_ch_id    = guild_cfg.get("proof_channel_id", 0)
        banform_ch_id  = guild_cfg.get("banform_channel_id", 0)
        proof_ch       = interaction.guild.get_channel(proof_ch_id)
        banform_ch     = interaction.guild.get_channel(banform_ch_id)
        servers_cfg    = guild_cfg.get("servers", {})
        lines = [
            f"**Участник:** {target.mention} (`{target.id}`)",
            f"**Роли сервера (1-90):** {', '.join(server_roles) or 'нет'}",
            f"**Роли должности:** {', '.join(rank_roles) or 'нет'}",
            f"**user_server в БД:** `{user_server_db}`",
            f"**proof_channel:** {proof_ch.mention if proof_ch else f'не найден (id={proof_ch_id})'}",
            f"**banform_channel:** {banform_ch.mention if banform_ch else f'не найден (id={banform_ch_id})'}",
            f"**Серверные каналы в конфиге:** {', '.join(servers_cfg.keys()) or 'нет'}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


    # ─── /deploy ──────────────────────────────────────────────────────────
    @_ADMIN_PERM
    @app_commands.command(
        name="deploy",
        description="Создать все недостающие каналы, роли и категории (только владелец)",
    )
    async def deploy_cmd(self, interaction: discord.Interaction):
        if not self._is_owner(interaction) and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        cfg   = self.bot.cfg
        done: list[str] = []
        errors: list[str] = []

        async def step(label: str, coro):
            try:
                await coro
                done.append(f"✅ {label}")
            except Exception as e:
                errors.append(f"❌ {label}: {e}")

        # ── 1. Роли авторизации ────────────────────────────────────────────
        async def _create_roles():
            unverified = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
            if not unverified:
                await guild.create_role(
                    name=UNVERIFIED_ROLE_NAME, color=discord.Color.light_grey(),
                    reason="deploy: роль новых участников",
                )
            for rank in RANKS:
                existing = discord.utils.get(guild.roles, name=rank)
                if not existing:
                    await guild.create_role(
                        name=rank, color=discord.Color.blue(), hoist=True,
                        reason="deploy: роль должности",
                    )
                elif not existing.hoist:
                    try:
                        await existing.edit(hoist=True)
                    except discord.Forbidden:
                        pass

        await step("Роли", _create_roles())

        # ── 2. Информационная категория ────────────────────────────────────
        async def _setup_info():
            from cogs.info import _build_rule_embeds, _build_commands_embed, INFO_CATEGORY

            everyone  = guild.default_role
            read_only = discord.PermissionOverwrite(view_channel=True, send_messages=False)
            bot_ow    = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True)

            category = discord.utils.get(guild.categories, name=INFO_CATEGORY)
            if not category:
                category = await guild.create_category(
                    name=INFO_CATEGORY,
                    overwrites={everyone: read_only, guild.me: bot_ow},
                )

            guild_cfg = get_guild_cfg(cfg, guild.id)
            ids = guild_cfg.setdefault("info_channels", {})

            # Правила
            ch_rules = discord.utils.get(guild.text_channels, name="📌-правила", category=category)
            if not ch_rules:
                ch_rules = await guild.create_text_channel(
                    name="📌-правила", category=category,
                    topic="Правила команды модерации",
                    overwrites={everyone: read_only, guild.me: bot_ow},
                )
                await ch_rules.purge(limit=10)
                for embed in _build_rule_embeds():
                    await ch_rules.send(embed=embed)
            ids["rules_channel_id"] = ch_rules.id

            # Объявления
            ch_ann = discord.utils.get(guild.text_channels, name="📢-объявления", category=category)
            if not ch_ann:
                ch_ann = await guild.create_text_channel(
                    name="📢-объявления", category=category,
                    topic="Объявления руководства",
                    overwrites={everyone: read_only, guild.me: bot_ow},
                )
            ids["announce_channel_id"] = ch_ann.id
            guild_cfg["info_announce_channel_id"] = ch_ann.id

            # Команды
            ch_cmd = discord.utils.get(guild.text_channels, name="ℹ️-команды", category=category)
            if not ch_cmd:
                ch_cmd = await guild.create_text_channel(
                    name="ℹ️-команды", category=category,
                    topic="Список команд бота",
                    overwrites={everyone: read_only, guild.me: bot_ow},
                )
                await ch_cmd.send(embed=_build_commands_embed())
            ids["commands_channel_id"] = ch_cmd.id

            save_config(cfg)

        await step("Информационная категория", _setup_info())

        # ── 3. Мониторинг ──────────────────────────────────────────────────
        async def _setup_monitoring():
            from cogs.servers import _ensure_monitoring_category
            await _ensure_monitoring_category(guild, cfg)

        await step("Мониторинг", _setup_monitoring())

        # ── 4. Общие каналы ────────────────────────────────────────────────
        async def _setup_common():
            from cogs.servers import _ensure_common_channels
            await _ensure_common_channels(guild)

        await step("Общие каналы", _setup_common())

        # ── 5. Авторизация ─────────────────────────────────────────────────
        async def _setup_auth():
            from cogs.auth import _get_or_create_forum_tag, AUTH_COOLDOWN_HOURS, AUTOKICK_DAYS

            everyone  = guild.default_role
            guild_cfg = get_guild_cfg(cfg, guild.id)

            # Категория
            cat_ow = {
                everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            }
            unverified = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
            if unverified:
                cat_ow[unverified] = discord.PermissionOverwrite(view_channel=True)
            category = discord.utils.get(guild.categories, name="🔐 Авторизация")
            if not category:
                category = await guild.create_category(name="🔐 Авторизация", overwrites=cat_ow)

            # Канал авторизации (кнопка) — создаём если нет, всегда обновляем сообщение
            from cogs.auth import AuthButtonView
            existing_auth_ch_id = guild_cfg.get("auth_channel_id", 0)
            auth_ch = guild.get_channel(existing_auth_ch_id) if existing_auth_ch_id else None
            if not auth_ch:
                ch_ow = {
                    everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                }
                if unverified:
                    ch_ow[unverified] = discord.PermissionOverwrite(view_channel=True)
                auth_ch = await guild.create_text_channel(
                    name="авторизация", category=category,
                    topic="Нажмите кнопку для подачи заявки",
                    overwrites=ch_ow,
                )
                guild_cfg["auth_channel_id"] = auth_ch.id
            # Always refresh the V2 welcome message
            await auth_ch.purge(limit=10, check=lambda m: m.author == guild.me)
            await auth_ch.send(view=AuthButtonView())

            save_config(cfg)

        await step("Авторизация", _setup_auth())

        # ── 6. Каналы серверов из БД ───────────────────────────────────────
        async def _setup_server_channels():
            from cogs.servers import ensure_server_channels
            servers_in_db = set(db.all_user_servers().values())
            guild_cfg = get_guild_cfg(cfg, guild.id)
            servers_in_cfg = set(guild_cfg.get("servers", {}).keys())
            all_servers = servers_in_db | servers_in_cfg
            created = 0
            for srv in sorted(all_servers, key=lambda x: int(x) if x.isdigit() else 0):
                try:
                    await ensure_server_channels(guild, srv, cfg)
                    created += 1
                except (discord.Forbidden, discord.HTTPException):
                    pass
            save_config(cfg)
            return created

        try:
            n = await _setup_server_channels()
            done.append(f"✅ Каналы серверов ({n} шт.)")
        except Exception as e:
            errors.append(f"❌ Каналы серверов: {e}")

        # ── Итог ───────────────────────────────────────────────────────────
        lines = ["**🚀 Deploy завершён**", ""]
        lines += done
        if errors:
            lines += ["", "**Ошибки:**"] + errors
        lines += ["", f"Всего: **{len(done)}** успешно, **{len(errors)}** с ошибкой."]
        await interaction.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
