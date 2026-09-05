import discord
from discord import app_commands
from discord.ext import commands

from helpers import save_config, get_guild_cfg

INFO_CATEGORY  = "📋 Информация"
INFO_COLOR     = 0x2F3136
ANNOUNCE_COLOR = 0x5865F2


# ─── Embed builders ───────────────────────────────────────────────────────────

SERVER_RULES: list[tuple[str, str, str]] = [
    ("🤝", "Уважение к коллегам",
     "Оскорбления, унижения и провокации в адрес участников команды недопустимы."),
    ("📋", "Соблюдение регламента",
     "Изучите регламент выдачи наказаний. Формы заполняются строго по установленному шаблону."),
    ("🔐", "Конфиденциальность",
     "Разглашение внутренних данных, обсуждений и решений команды третьим лицам запрещено."),
    ("⚡", "Активность",
     "Своевременно обрабатывайте заявки. При длительном отсутствии уведомите руководство заранее."),
    ("📢", "Порядок в каналах",
     "Флуд, спам и оффтоп недопустимы. Придерживайтесь тематики каждого канала."),
    ("🛡️", "Честность",
     "Злоупотребление полномочиями и фальсификация доказательств влекут немедленное исключение."),
    ("🚫", "Запрет дискредитации",
     "Публичная критика команды, руководства или проекта в любых каналах недопустима."),
    ("📱", "Связь с руководством",
     "Отвечайте на сообщения руководства в течение 24 часов. Систематическое игнорирование = исключение."),
    ("🎯", "Соответствие должности",
     "Выполняйте обязанности строго в рамках своего звания и полученных полномочий."),
    ("🔇", "Субординация",
     "Соблюдайте субординацию. Решения вышестоящих исполняются без промедления."),
    ("📸", "Качество доказательств",
     "Принимаются только чёткие скриншоты или видеозаписи. Сомнительные материалы недействительны."),
    ("⏰", "Сроки оформления",
     "Формы наказаний оформляются не позднее 30 минут с момента фиксации нарушения."),
    ("🎮", "Поведение на сервере",
     "Поведение модератора в игре должно соответствовать статусу. Нарушения правил в игре фиксируются."),
    ("🔄", "Передача дел",
     "При уходе обязательно передайте незакрытые дела и доступы руководству или преемнику."),
    ("📊", "Выполнение нормы",
     "Систематическое невыполнение нормы по формам влечёт предупреждение и понижение."),
    ("🚨", "Сообщение о нарушениях",
     "Любое нарушение со стороны коллеги обязательно сообщается руководству лично."),
    ("💬", "Культура общения",
     "Общайтесь без мата и агрессии — как внутри команды, так и с игроками проекта."),
    ("🔒", "Использование инструментов",
     "Командные инструменты (бот, каналы, роли) используются исключительно по назначению."),
    ("⚖️", "Равенство",
     "Правила одинаковы для всех участников команды, независимо от звания и выслуги лет."),
    ("📝", "Оформление наказаний",
     "Все наказания фиксируются через бота. Ручные записи без формы считаются недействительными."),
]


def _build_rules_view() -> discord.ui.LayoutView:
    items: list = [
        discord.ui.TextDisplay(
            "## 📖 Правила команды модерации\n"
            "-# Незнание правил не освобождает от ответственности за их нарушение."
        ),
        discord.ui.Separator(),
    ]
    block: list[str] = []
    for i, (emoji, title, desc) in enumerate(SERVER_RULES, 1):
        block.append(f"**{i}. {emoji} {title}**\n-# {desc}")
        if i % 5 == 0 or i == len(SERVER_RULES):
            items.append(discord.ui.TextDisplay("\n\n".join(block)))
            items.append(discord.ui.Separator())
            block = []
    items.append(discord.ui.TextDisplay(
        "### ⚠️ Нарушение правил влечёт взыскание или исключение из команды."
    ))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_color=0x5865F2))
    return view


def _build_commands_view() -> discord.ui.LayoutView:
    items: list = [
        discord.ui.TextDisplay(
            "## 🤖 Команды бота\n"
            "-# Все slash-команды доступны участникам команды модерации."
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "**📋 Формы наказаний**\n"
            "`/proof` — форма обычного наказания\n"
            "`/banform` — форма бана (7–15 дней)\n"
            "`/gbanform` — форма глобального бана\n"
            "`/activforms` — незакрытые формы банов"
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "**📊 Статистика**\n"
            "`/stats` — ваша статистика форм\n"
            "`/listmods` — список модераторов по серверам"
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "**👥 Управление составом**\n"
            "`/promote` — изменить звание модератора `(ЗГМ+)`\n"
            "`/dismiss` — исключить модератора из команды `(ЗГМ+)`"
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "-# Параметры команд смотрите в подсказке при вводе /"
        ),
    ]
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_color=0x2ECC71))
    return view


# ─── Announce modal ───────────────────────────────────────────────────────────

class AnnounceModal(discord.ui.Modal, title="Новое объявление"):
    heading = discord.ui.TextInput(
        label="Заголовок",
        placeholder="Например: Обновление правил",
        max_length=100,
        required=True,
    )
    body = discord.ui.TextInput(
        label="Текст объявления",
        style=discord.TextStyle.paragraph,
        placeholder="Подробный текст...",
        max_length=2000,
        required=True,
    )

    def __init__(self, announce_channel: discord.TextChannel):
        super().__init__()
        self.announce_channel = announce_channel

    async def on_submit(self, interaction: discord.Interaction):
        ts = int(discord.utils.utcnow().timestamp())
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"## 📢 {self.heading.value}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(self.body.value),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"-# {interaction.user.display_name} • <t:{ts}:f>"
            ),
            accent_color=ANNOUNCE_COLOR,
        ))
        try:
            await self.announce_channel.send(view=view)
            await interaction.response.send_message(
                f"✅ Объявление опубликовано в {self.announce_channel.mention}.", ephemeral=True
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "❌ Канал объявлений недоступен или был удалён.", ephemeral=True
            )


# ─── Cog ─────────────────────────────────────────────────────────────────────

class InfoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        uid = interaction.user.id
        return (
            uid == getattr(self.bot, "owner_id_cfg", 0)
            or (interaction.guild and uid == interaction.guild.owner_id)
        )

    async def _get_announce_channel(self, interaction: discord.Interaction) -> discord.TextChannel | None:
        guild_cfg = get_guild_cfg(self.bot.cfg, interaction.guild_id)
        ch_id = guild_cfg.get("info_announce_channel_id", 0)
        return interaction.guild.get_channel(ch_id) if ch_id else None

    # ─── /setup-info ──────────────────────────────────────────────────────
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="setup-info", description="Создать информационную категорию (владелец)")
    async def setup_info_cmd(self, interaction: discord.Interaction):
        if not self._is_owner(interaction):
            await interaction.response.send_message("❌ Только для владельца сервера.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        everyone = guild.default_role
        read_only = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        bot_ow    = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True)

        # Category
        category = discord.utils.get(guild.categories, name=INFO_CATEGORY)
        if not category:
            category = await guild.create_category(
                name=INFO_CATEGORY,
                overwrites={everyone: read_only, guild.me: bot_ow},
            )

        guild_cfg = get_guild_cfg(self.bot.cfg, guild.id)
        ids = guild_cfg.setdefault("info_channels", {})

        # 📌 Правила
        ch_rules = discord.utils.get(guild.text_channels, name="📌-правила", category=category)
        if not ch_rules:
            ch_rules = await guild.create_text_channel(
                name="📌-правила", category=category,
                topic="Правила команды модерации",
                overwrites={everyone: read_only, guild.me: bot_ow},
            )
        await ch_rules.purge(limit=50)
        await ch_rules.send(view=_build_rules_view())
        ids["rules_channel_id"] = ch_rules.id

        # 📢 Объявления
        ch_ann = discord.utils.get(guild.text_channels, name="📢-объявления", category=category)
        if not ch_ann:
            ch_ann = await guild.create_text_channel(
                name="📢-объявления", category=category,
                topic="Объявления руководства",
                overwrites={everyone: read_only, guild.me: bot_ow},
            )
        ids["announce_channel_id"] = ch_ann.id
        guild_cfg["info_announce_channel_id"] = ch_ann.id

        # ℹ️ Команды
        ch_cmd = discord.utils.get(guild.text_channels, name="ℹ️-команды", category=category)
        if not ch_cmd:
            ch_cmd = await guild.create_text_channel(
                name="ℹ️-команды", category=category,
                topic="Список команд бота",
                overwrites={everyone: read_only, guild.me: bot_ow},
            )
        cmd_msg_id = ids.get("commands_msg_id", 0)
        existing_cmd = None
        if cmd_msg_id:
            try:
                existing_cmd = await ch_cmd.fetch_message(cmd_msg_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                existing_cmd = None
        if existing_cmd:
            await existing_cmd.edit(view=_build_commands_view())
        else:
            msg = await ch_cmd.send(view=_build_commands_view())
            ids["commands_msg_id"] = msg.id
        ids["commands_channel_id"] = ch_cmd.id

        save_config(self.bot.cfg)

        await interaction.followup.send(
            f"✅ Информационная категория готова:\n"
            f"• {ch_rules.mention} — правила\n"
            f"• {ch_ann.mention} — объявления\n"
            f"• {ch_cmd.mention} — команды бота",
            ephemeral=True,
        )

    # ─── /update-rules ────────────────────────────────────────────────────
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="update-rules", description="Обновить embed правил в канале (владелец)")
    async def update_rules_cmd(self, interaction: discord.Interaction):
        if not self._is_owner(interaction):
            await interaction.response.send_message("❌ Только для владельца сервера.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild_cfg = get_guild_cfg(self.bot.cfg, interaction.guild_id)
        ids = guild_cfg.get("info_channels", {})
        ch_id = ids.get("rules_channel_id", 0)

        if not ch_id:
            await interaction.followup.send(
                "❌ Канал правил не найден. Сначала запустите `/setup-info`.", ephemeral=True
            )
            return

        ch = interaction.guild.get_channel(ch_id)
        if not ch:
            await interaction.followup.send("❌ Канал правил недоступен.", ephemeral=True)
            return

        await ch.purge(limit=50)
        await ch.send(view=_build_rules_view())

        await interaction.followup.send(f"✅ Правила обновлены в {ch.mention}.", ephemeral=True)

    # ─── /announce ────────────────────────────────────────────────────────
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="announce", description="Отправить объявление в канал объявлений (только владелец)")
    async def announce_cmd(self, interaction: discord.Interaction):
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "❌ Только для владельца сервера.", ephemeral=True
            )
            return

        ch = await self._get_announce_channel(interaction)
        if not ch:
            await interaction.response.send_message(
                "❌ Канал объявлений не найден. Сначала запустите `/setup-info`.", ephemeral=True
            )
            return

        await interaction.response.send_modal(AnnounceModal(ch))


async def setup(bot: commands.Bot):
    await bot.add_cog(InfoCog(bot))
