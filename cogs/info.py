import discord
from discord import app_commands
from discord.ext import commands

from helpers import save_config, get_guild_cfg, RULES, RANKS, PUNISHMENTS, get_member_rank_level

INFO_CATEGORY = "📋 Информация"
INFO_COLOR     = 0x2F3136
ANNOUNCE_COLOR = 0x5865F2

AUTH_MIN_LEVEL = 5  # ЗГМ / ГМ могут отправлять объявления


# ─── Embed builders ───────────────────────────────────────────────────────────

def _build_rules_embed() -> discord.Embed:
    sections: dict[str, list[tuple[str, str]]] = {}
    for rule_id, rule_text in RULES.items():
        section = rule_id.split(".")[0]
        sections.setdefault(section, []).append((rule_id, rule_text))

    section_titles = {
        "2": "🚫 Раздел 2 — Основные запреты",
        "3": "💬 Раздел 3 — Текстовые каналы",
        "4": "🔊 Раздел 4 — Голосовые каналы",
    }

    embed = discord.Embed(
        title="📖 Правила команды модерации",
        description=(
            "Нарушение правил влечёт наказание согласно регламенту.\n"
            "Незнание правил не освобождает от ответственности."
        ),
        color=INFO_COLOR,
    )

    for section_id in sorted(sections):
        title = section_titles.get(section_id, f"Раздел {section_id}")
        rules = sections[section_id]
        lines = [f"`{rid}` — {rtext}" for rid, rtext in rules]
        # Split field if too long (Discord limit: 1024 chars per field)
        chunk, chunks = [], []
        for line in lines:
            if sum(len(l) + 1 for l in chunk) + len(line) > 1000:
                chunks.append(chunk)
                chunk = [line]
            else:
                chunk.append(line)
        if chunk:
            chunks.append(chunk)
        for i, part in enumerate(chunks):
            embed.add_field(
                name=title if i == 0 else f"{title} (продолжение)",
                value="\n".join(part),
                inline=False,
            )

    embed.add_field(
        name="⚖️ Наказания",
        value="\n".join(f"`{i+1}.` {p}" for i, p in enumerate(PUNISHMENTS)),
        inline=False,
    )
    embed.set_footer(text="Правила обновлены автоматически")
    return embed


def _build_commands_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🤖 Команды бота",
        description="Все slash-команды, доступные участникам команды.",
        color=ANNOUNCE_COLOR,
    )
    embed.add_field(
        name="📋 Формы наказаний",
        value=(
            "`/proof` — форма обычного наказания\n"
            "`/banform` — форма бана (7–15 дней)\n"
            "`/gbanform` — форма глобального бана"
        ),
        inline=False,
    )
    embed.add_field(
        name="📊 Статистика",
        value=(
            "`/stats` — ваша статистика форм\n"
            "`/history` — история отправленных форм"
        ),
        inline=False,
    )
    embed.add_field(
        name="👥 Авторизация",
        value=(
            "`/setup-auth` — настроить систему авторизации (владелец)\n"
            "`/dismiss` — исключить модератора из команды (ЗГМ+)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛠️ Серверы",
        value=(
            "`/setupserver` — создать каналы сервера (владелец)\n"
            "`/cleanupserver` — удалить дубли каналов (владелец)\n"
            "`/listmods` — список модераторов по серверам"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Администрирование",
        value=(
            "`/setup` — основные настройки бота\n"
            "`/setform` — шаблон формы наказания\n"
            "`/editrule` — добавить / изменить правило\n"
            "`/assignserver` — вручную привязать модератора к серверу\n"
            "`/announce` — отправить объявление (ЗГМ+)\n"
            "`/update-rules` — обновить embed правил\n"
            "`/uptime` — время работы бота"
        ),
        inline=False,
    )
    embed.set_footer(text="Параметры команд смотрите в описании при вводе /")
    return embed


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
        embed = discord.Embed(
            title=f"📢 {self.heading.value}",
            description=self.body.value,
            color=ANNOUNCE_COLOR,
        )
        embed.set_footer(text=f"Объявление от {interaction.user} • {discord.utils.utcnow().strftime('%d.%m.%Y %H:%M')} UTC")
        await self.announce_channel.send(embed=embed)
        await interaction.response.send_message(
            f"✅ Объявление опубликовано в {self.announce_channel.mention}.", ephemeral=True
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
    @app_commands.command(name="setup-info", description="Создать информационную категорию (владелец)")
    async def setup_info_cmd(self, interaction: discord.Interaction):
        if not self._is_owner(interaction):
            await interaction.response.send_message("❌ Только для владельца сервера.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        everyone = guild.default_role
        read_only = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        hidden    = discord.PermissionOverwrite(view_channel=False)
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
        msg_id = ids.get("rules_msg_id", 0)
        existing = None
        if msg_id:
            try:
                existing = await ch_rules.fetch_message(msg_id)
            except (discord.NotFound, discord.HTTPException):
                existing = None
        if existing:
            await existing.edit(embed=_build_rules_embed())
        else:
            msg = await ch_rules.send(embed=_build_rules_embed())
            ids["rules_msg_id"] = msg.id
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
            except (discord.NotFound, discord.HTTPException):
                existing_cmd = None
        if existing_cmd:
            await existing_cmd.edit(embed=_build_commands_embed())
        else:
            msg = await ch_cmd.send(embed=_build_commands_embed())
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
    @app_commands.command(name="update-rules", description="Обновить embed правил в канале (владелец)")
    async def update_rules_cmd(self, interaction: discord.Interaction):
        if not self._is_owner(interaction):
            await interaction.response.send_message("❌ Только для владельца сервера.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild_cfg = get_guild_cfg(self.bot.cfg, interaction.guild_id)
        ids = guild_cfg.get("info_channels", {})
        ch_id  = ids.get("rules_channel_id", 0)
        msg_id = ids.get("rules_msg_id", 0)

        if not ch_id:
            await interaction.followup.send(
                "❌ Канал правил не найден. Сначала запустите `/setup-info`.", ephemeral=True
            )
            return

        ch = interaction.guild.get_channel(ch_id)
        if not ch:
            await interaction.followup.send("❌ Канал правил недоступен.", ephemeral=True)
            return

        try:
            msg = await ch.fetch_message(msg_id)
            await msg.edit(embed=_build_rules_embed())
        except (discord.NotFound, discord.HTTPException):
            msg = await ch.send(embed=_build_rules_embed())
            guild_cfg["info_channels"]["rules_msg_id"] = msg.id
            save_config(self.bot.cfg)

        await interaction.followup.send(f"✅ Правила обновлены в {ch.mention}.", ephemeral=True)

    # ─── /announce ────────────────────────────────────────────────────────
    @app_commands.command(name="announce", description="Отправить объявление в канал объявлений (ЗГМ+)")
    async def announce_cmd(self, interaction: discord.Interaction):
        if not self._is_owner(interaction) and get_member_rank_level(interaction.user) < AUTH_MIN_LEVEL:
            await interaction.response.send_message(
                "❌ Доступно только **Заместителю главного модератора** и **Главному модератору**.",
                ephemeral=True,
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
