import discord
from discord import app_commands
from discord.ext import commands

from helpers import save_config, get_guild_cfg, LEADERSHIP_RANKS, RANKS, RANK_LEVELS

COMMON_CATEGORY     = "🌐 Общие каналы"
MONITORING_CATEGORY = "🖥️ Мониторинг"


def is_server_role(name: str) -> bool:
    return name.isdigit() and 1 <= int(name) <= 90


def get_server_for_member(member, cfg: dict | None = None) -> str | None:
    for role in getattr(member, "roles", []):
        if is_server_role(role.name):
            return role.name
    from db import get_user_server
    return get_user_server(member.id)


def get_proof_channel(guild: discord.Guild, cfg: dict, server: str) -> discord.TextChannel | None:
    guild_cfg = get_guild_cfg(cfg, guild.id)
    ch_id = guild_cfg.get("servers", {}).get(server, {}).get("proof")
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch:
        return ch
    category = discord.utils.get(guild.categories, name=str(server))
    if category:
        return discord.utils.get(guild.text_channels, name="📋-выдача-наказаний", category=category)
    return None


def get_banform_channel(guild: discord.Guild, cfg: dict, server: str) -> discord.TextChannel | None:
    guild_cfg = get_guild_cfg(cfg, guild.id)
    ch_id = guild_cfg.get("servers", {}).get(server, {}).get("banform")
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch:
        return ch
    category = discord.utils.get(guild.categories, name=str(server))
    if category:
        return discord.utils.get(guild.text_channels, name="⚖️-формы-банов", category=category)
    return None


def get_log_channel(guild: discord.Guild, cfg: dict, server: str) -> discord.TextChannel | None:
    guild_cfg = get_guild_cfg(cfg, guild.id)
    ch_id = guild_cfg.get("servers", {}).get(server, {}).get("logs")
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch:
        return ch
    category = discord.utils.get(guild.categories, name=str(server))
    if category:
        return discord.utils.get(guild.text_channels, name="📊-логи", category=category)
    return None


async def _ensure_common_channels(guild: discord.Guild):
    everyone = guild.default_role
    category = discord.utils.get(guild.categories, name=COMMON_CATEGORY)

    if not category:
        mod_roles = [r for r in guild.roles if r.name in RANKS]
        cat_ow = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        for r in mod_roles:
            cat_ow[r] = discord.PermissionOverwrite(view_channel=True)
        category = await guild.create_category(name=COMMON_CATEGORY, overwrites=cat_ow)

    if not discord.utils.get(guild.text_channels, name="💬-общий-чат", category=category):
        mod_roles = [r for r in guild.roles if r.name in RANKS]
        ch_ow = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        for r in mod_roles:
            ch_ow[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        await guild.create_text_channel(
            name="💬-общий-чат", category=category, overwrites=ch_ow,
            topic="Общение всех модераторов",
        )

    for i in (1, 2):
        vc_name = f"🔊 Общий {i}"
        if not discord.utils.get(guild.voice_channels, name=vc_name, category=category):
            await guild.create_voice_channel(name=vc_name, category=category)


async def _ensure_monitoring_category(guild: discord.Guild):
    everyone = guild.default_role
    all_mod_roles = [r for r in guild.roles if r.name in RANKS]
    lead_roles    = [r for r in guild.roles if r.name in LEADERSHIP_RANKS]

    category = discord.utils.get(guild.categories, name=MONITORING_CATEGORY)
    if not category:
        cat_ow = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        for r in lead_roles:
            cat_ow[r] = discord.PermissionOverwrite(view_channel=True)
        category = await guild.create_category(name=MONITORING_CATEGORY, overwrites=cat_ow)

    readonly_lead = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    for r in lead_roles:
        readonly_lead[r] = discord.PermissionOverwrite(view_channel=True, send_messages=False)

    readonly_all = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    for r in all_mod_roles:
        readonly_all[r] = discord.PermissionOverwrite(view_channel=True, send_messages=False)

    channels = [
        ("📡-статус-бота",   "Статус и аптайм бота",                          readonly_lead),
        ("🔔-авторизации",   "Журнал авторизаций, кандидатов и кик-логов",     readonly_all),
        ("📋-журнал-команд", "Лог административных действий",                   readonly_lead),
    ]
    for name, topic, ow in channels:
        if not discord.utils.get(guild.text_channels, name=name, category=category):
            await guild.create_text_channel(name=name, category=category, topic=topic, overwrites=ow)


async def ensure_server_channels(guild: discord.Guild, server: str, cfg: dict) -> dict:
    everyone = guild.default_role
    server_role = discord.utils.get(guild.roles, name=server)
    if not server_role:
        server_role = await guild.create_role(
            name=server, color=discord.Color.green(),
            hoist=True, reason=f"Роль сервера {server}",
        )

    leadership_roles = [r for r in guild.roles if r.name in LEADERSHIP_RANKS]

    cat_name = str(server)
    category = discord.utils.get(guild.categories, name=cat_name)
    if not category:
        cat_ow = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            server_role: discord.PermissionOverwrite(view_channel=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        category = await guild.create_category(name=cat_name, overwrites=cat_ow)

    channel_ids = {}

    ch = discord.utils.get(guild.text_channels, name="💬-общение", category=category)
    if not ch:
        ch = await guild.create_text_channel(
            name="💬-общение", category=category,
            topic=f"Общение модераторов сервера {server}",
        )
    channel_ids["chat"] = ch.id

    ch = discord.utils.get(guild.text_channels, name="📋-выдача-наказаний", category=category)
    if not ch:
        ch = await guild.create_text_channel(
            name="📋-выдача-наказаний", category=category,
            topic=f"Формы наказаний — сервер {server}",
        )
    channel_ids["proof"] = ch.id

    ch = discord.utils.get(guild.text_channels, name="⚖️-формы-банов", category=category)
    if not ch:
        ch = await guild.create_text_channel(
            name="⚖️-формы-банов", category=category,
            topic=f"Формы банов и глобальных банов — сервер {server}",
        )
    channel_ids["banform"] = ch.id

    lead_ow = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        server_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    for role in leadership_roles:
        lead_ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    ch = discord.utils.get(guild.text_channels, name="👑-руководство", category=category)
    if not ch:
        ch = await guild.create_text_channel(
            name="👑-руководство", category=category,
            overwrites=lead_ow,
            topic=f"Руководство сервера {server}",
        )
    channel_ids["leadership"] = ch.id

    log_ow = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        server_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    ch = discord.utils.get(guild.text_channels, name="📊-логи", category=category)
    if not ch:
        ch = await guild.create_text_channel(
            name="📊-логи", category=category,
            overwrites=log_ow,
            topic=f"Логи наказаний — сервер {server}",
        )
    channel_ids["logs"] = ch.id

    for i in (1, 2):
        vc_name = f"🔊 {server} | Голосовой {i}"
        if not discord.utils.get(guild.voice_channels, name=vc_name, category=category):
            await guild.create_voice_channel(name=vc_name, category=category)

    await _ensure_common_channels(guild)

    guild_cfg = get_guild_cfg(cfg, guild.id)
    guild_cfg.setdefault("servers", {})[server] = channel_ids
    save_config(cfg)

    return channel_ids


# ─── Delete confirmation view ─────────────────────────────────────────────────

class DeleteCategoryView(discord.ui.View):
    def __init__(self, server: str, cfg: dict):
        super().__init__(timeout=30)
        self.server = server
        self.cfg = cfg

    @discord.ui.button(label="Удалить", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        category = discord.utils.get(guild.categories, name=self.server)
        deleted = 0
        if category:
            for ch in list(category.channels):
                try:
                    await ch.delete(reason=f"manage-category delete: {self.server}")
                    deleted += 1
                except discord.Forbidden:
                    pass
            try:
                await category.delete(reason=f"manage-category delete: {self.server}")
            except discord.Forbidden:
                pass

        # Remove from config
        guild_cfg = get_guild_cfg(self.cfg, guild.id)
        guild_cfg.get("servers", {}).pop(self.server, None)
        save_config(self.cfg)

        self.stop()
        await interaction.followup.send(
            f"🗑️ Категория **{self.server}** удалена ({deleted} каналов).", ephemeral=True
        )

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Отменено.", view=None)


# ─── Cog ─────────────────────────────────────────────────────────────────────

class ServersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        new_role_names = {r.name for r in after.roles} - {r.name for r in before.roles}
        for name in new_role_names:
            if is_server_role(name):
                try:
                    await ensure_server_channels(after.guild, name, self.bot.cfg)
                except discord.Forbidden:
                    pass

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        uid = interaction.user.id
        return uid == interaction.guild.owner_id or uid == getattr(self.bot, "owner_id_cfg", 0)

    @app_commands.command(name="setupserver", description="Создать каналы для сервера вручную")
    @app_commands.describe(server="Номер сервера (1–90)")
    async def setupserver_cmd(self, interaction: discord.Interaction, server: str):
        if not self._is_owner(interaction):
            await interaction.response.send_message("❌ Только для владельца сервера.", ephemeral=True)
            return
        if not (server.isdigit() and 1 <= int(server) <= 90):
            await interaction.response.send_message("❌ Укажите число от 1 до 90.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await ensure_server_channels(interaction.guild, server, self.bot.cfg)
            await interaction.followup.send(f"✅ Каналы для сервера **{server}** созданы/обновлены.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ Нет прав для создания каналов.", ephemeral=True)

    @app_commands.command(name="manage-category", description="Управление категориями серверов")
    @app_commands.describe(
        action="Действие",
        server="Номер сервера (1–90) — для create_missing и delete",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Создать недостающие каналы", value="create_missing"),
        app_commands.Choice(name="Удалить категорию сервера",  value="delete"),
        app_commands.Choice(name="Создать категорию мониторинга", value="monitoring"),
    ])
    async def manage_category_cmd(
        self,
        interaction: discord.Interaction,
        action: str,
        server: str | None = None,
    ):
        if not self._is_owner(interaction):
            await interaction.response.send_message("❌ Только для владельца сервера.", ephemeral=True)
            return

        if action in ("create_missing", "delete"):
            if not server or not (server.isdigit() and 1 <= int(server) <= 90):
                await interaction.response.send_message(
                    "❌ Укажите номер сервера (1–90) для этого действия.", ephemeral=True
                )
                return

        if action == "create_missing":
            await interaction.response.defer(ephemeral=True)
            try:
                await ensure_server_channels(interaction.guild, server, self.bot.cfg)
                await interaction.followup.send(
                    f"✅ Недостающие каналы для сервера **{server}** созданы.", ephemeral=True
                )
            except discord.Forbidden:
                await interaction.followup.send("❌ Нет прав для создания каналов.", ephemeral=True)

        elif action == "delete":
            category = discord.utils.get(interaction.guild.categories, name=server)
            if not category:
                await interaction.response.send_message(
                    f"❌ Категория **{server}** не найдена.", ephemeral=True
                )
                return
            ch_count = len(category.channels)
            await interaction.response.send_message(
                f"⚠️ Удалить категорию **{server}** и все {ch_count} каналов в ней?",
                view=DeleteCategoryView(server, self.bot.cfg),
                ephemeral=True,
            )

        elif action == "monitoring":
            await interaction.response.defer(ephemeral=True)
            try:
                await _ensure_monitoring_category(interaction.guild)
                await interaction.followup.send(
                    f"✅ Категория **{MONITORING_CATEGORY}** создана/обновлена.", ephemeral=True
                )
            except discord.Forbidden:
                await interaction.followup.send("❌ Нет прав для создания каналов.", ephemeral=True)

    @app_commands.command(name="cleanupserver", description="Удалить дублирующиеся каналы сервера")
    @app_commands.describe(server="Номер сервера (1–90)")
    async def cleanupserver_cmd(self, interaction: discord.Interaction, server: str):
        if not self._is_owner(interaction):
            await interaction.response.send_message("❌ Только для владельца сервера.", ephemeral=True)
            return
        if not (server.isdigit() and 1 <= int(server) <= 90):
            await interaction.response.send_message("❌ Укажите число от 1 до 90.", ephemeral=True)
            return

        category = discord.utils.get(interaction.guild.categories, name=str(server))
        if not category:
            await interaction.response.send_message(f"❌ Категория **{server}** не найдена.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        deleted = 0
        seen_names: dict[str, discord.abc.GuildChannel] = {}
        for ch in list(category.channels):
            if ch.name in seen_names:
                try:
                    await ch.delete(reason=f"Cleanup: дубль канала {ch.name}")
                    deleted += 1
                except discord.Forbidden:
                    pass
            else:
                seen_names[ch.name] = ch

        if deleted:
            await interaction.followup.send(f"🗑️ Удалено дублей: **{deleted}** в категории **{server}**.", ephemeral=True)
        else:
            await interaction.followup.send(f"✅ Дублей в категории **{server}** не найдено.", ephemeral=True)

    @app_commands.command(name="listmods", description="Список модераторов по серверам")
    async def listmods_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        servers: dict[str, list[str]] = {}
        for member in guild.members:
            server = get_server_for_member(member)
            if server:
                rank = next(
                    (r.name for r in sorted(member.roles, key=lambda r: RANK_LEVELS.get(r.name, 0), reverse=True)
                     if r.name in RANK_LEVELS),
                    "—",
                )
                servers.setdefault(server, []).append(f"{member.mention} — {rank}")

        if not servers:
            await interaction.followup.send("Нет авторизованных модераторов.", ephemeral=True)
            return

        embed = discord.Embed(title="👥 Модераторы по серверам", color=0x2ECC71)
        for server_num in sorted(servers.keys(), key=int):
            mods_list = servers[server_num]
            embed.add_field(
                name=f"Сервер {server_num} ({len(mods_list)})",
                value="\n".join(mods_list),
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServersCog(bot))
