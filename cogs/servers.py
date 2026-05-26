import asyncio
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from helpers import save_config, get_guild_cfg, LEADERSHIP_RANKS, RANKS, RANK_LEVELS

_BOT_START_TIME = time.time()
_SERVER_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_presence_index = 0


def _get_server_lock(guild_id: int, server: str) -> asyncio.Lock:
    key = (guild_id, server)
    lock = _SERVER_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SERVER_LOCKS[key] = lock
    return lock

COMMON_CATEGORY     = "🌐 Общие каналы"
MONITORING_CATEGORY = "🖥️ Мониторинг"


def is_server_role(name: str) -> bool:
    return name.isdigit() and 1 <= int(name) <= 90


def get_server_for_member(member, cfg: dict | None = None) -> str | None:
    server_roles = [r.name for r in getattr(member, "roles", []) if is_server_role(r.name)]
    if len(server_roles) == 1:
        return server_roles[0]
    if not server_roles:
        from db import get_user_server
        return get_user_server(member.id)
    return None  # multiple server roles — ambiguous


def server_for_channel(guild: discord.Guild, cfg: dict, channel_id: int) -> str | None:
    """Return the server number that owns this channel, or None if not found."""
    from db import get_all_server_channels
    guild_servers = get_guild_cfg(cfg, guild.id).get("servers", {})
    all_servers = set(guild_servers.keys())
    # also check any server that has this channel in DB
    for cat in guild.categories:
        if is_server_role(cat.name):
            all_servers.add(cat.name)
    for server in all_servers:
        sc = get_all_server_channels(guild.id, server)
        if channel_id in (sc.get("proof"), sc.get("banform"), sc.get("logs"), sc.get("auth")):
            return server
    # fallback: check channel's category name
    ch = guild.get_channel(channel_id)
    if ch and ch.category and is_server_role(ch.category.name):
        return ch.category.name
    return None


def _ch_by_id_or_name(
    guild: discord.Guild,
    cfg: dict,
    server: str,
    id_key: str,
    ch_name: str,
) -> discord.TextChannel | None:
    """Look up a server channel: DB first, then category-name fallback, then guild-wide."""
    from db import get_server_channel, clear_server_channel, set_server_channel
    ch_id = get_server_channel(guild.id, server, id_key)
    if ch_id:
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel):
            return ch
        # Stale ID — channel deleted; clear so it gets recreated next time
        clear_server_channel(guild.id, server, id_key)

    # Fallback 1: find by category object
    category = discord.utils.get(guild.categories, name=str(server))
    if category:
        ch = discord.utils.get(category.channels, name=ch_name)
        if isinstance(ch, discord.TextChannel):
            set_server_channel(guild.id, server, id_key, ch.id)
            return ch

    # Fallback 2: guild-wide search — channel has name ch_name in a category named server
    for ch in guild.text_channels:
        if ch.name == ch_name and ch.category and ch.category.name == str(server):
            set_server_channel(guild.id, server, id_key, ch.id)
            return ch
    return None


def get_proof_channel(guild: discord.Guild, cfg: dict, server: str) -> discord.TextChannel | None:
    return _ch_by_id_or_name(guild, cfg, server, "proof", "📋-выдача-наказаний")


def get_banform_channel(guild: discord.Guild, cfg: dict, server: str) -> discord.TextChannel | None:
    return _ch_by_id_or_name(guild, cfg, server, "banform", "⚖️-формы-банов")


def get_monitoring_channel(guild: discord.Guild, cfg: dict, name: str) -> discord.TextChannel | None:
    guild_cfg = get_guild_cfg(cfg, guild.id)
    ch_id = guild_cfg.get("monitoring", {}).get(name, 0)
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch:
        return ch
    category = discord.utils.get(guild.categories, name=MONITORING_CATEGORY)
    if category:
        return discord.utils.get(guild.text_channels, name=name, category=category)
    return None


class StatusRefreshView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        btn = discord.ui.Button(
            label="🔄 Обновить",
            style=discord.ButtonStyle.secondary,
            custom_id="status:refresh",
        )
        btn.callback = _status_refresh_callback
        self.add_item(btn)


async def _status_refresh_callback(interaction: discord.Interaction):
    await interaction.response.defer()
    await post_monitoring_status(interaction.guild, interaction.client.cfg, interaction.client)


def _build_status_embed(bot) -> discord.Embed:
    from db import all_user_servers, get_global_form_counts, _conn
    elapsed = int(time.time() - _BOT_START_TIME)
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    total_forms, approved_forms = get_global_form_counts()
    mod_count = len(all_user_servers())
    ping_ms = round(bot.latency * 1000)
    status_icon = "🟢" if ping_ms < 200 else "🟡" if ping_ms < 500 else "🔴"

    with _conn() as c:
        rejected_forms = c.execute(
            "SELECT COUNT(*) FROM form_stats WHERE status='rejected'"
        ).fetchone()[0]
    pending_forms = total_forms - approved_forms - rejected_forms
    today_ts = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with _conn() as c:
        today_forms = c.execute(
            "SELECT COUNT(*) FROM form_stats WHERE ts >= ?", (today_ts,)
        ).fetchone()[0]

    embed = discord.Embed(
        title="📡 Статус бота",
        color=0x2ECC71 if ping_ms < 200 else 0xF1C40F if ping_ms < 500 else 0xE74C3C,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="🕐 Аптайм",           value=f"{h}ч {m}м {s}с",              inline=True)
    embed.add_field(name=f"{status_icon} Пинг", value=f"{ping_ms} мс",                inline=True)
    embed.add_field(name="🌐 Серверов",          value=str(len(bot.guilds)),            inline=True)
    embed.add_field(name="👥 Модераторов в БД",  value=str(mod_count),                 inline=True)
    embed.add_field(name="📋 Форм всего",        value=str(total_forms),               inline=True)
    embed.add_field(name="✅ Одобрено",          value=str(approved_forms),            inline=True)
    embed.add_field(name="📊 Отклонено",         value=str(rejected_forms),            inline=True)
    embed.add_field(name="⏳ На рассмотрении",   value=str(max(pending_forms, 0)),     inline=True)
    embed.add_field(name="🗓️ Форм сегодня",      value=str(today_forms),               inline=True)
    embed.set_footer(text="Обновлено")
    return embed


async def post_monitoring_status(guild: discord.Guild, cfg: dict, bot) -> None:
    ch = get_monitoring_channel(guild, cfg, "📡-статус-бота")
    if not ch:
        return

    embed = _build_status_embed(bot)
    view  = StatusRefreshView()

    guild_cfg = get_guild_cfg(cfg, guild.id)
    msg_id = guild_cfg.get("status_message_id", 0)

    if msg_id:
        try:
            msg = await ch.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            guild_cfg["status_message_id"] = 0
            save_config(cfg)

    try:
        msg = await ch.send(embed=embed, view=view)
        guild_cfg["status_message_id"] = msg.id
        save_config(cfg)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def post_monitoring_stats(guild: discord.Guild, cfg: dict, bot) -> None:
    """Post per-server form stats to 📊-статистика-форм channel."""
    ch = get_monitoring_channel(guild, cfg, "📊-статистика-форм")
    if not ch:
        return
    from db import _conn, all_user_servers
    guild_cfg = get_guild_cfg(cfg, guild.id)

    with _conn() as c:
        rows = c.execute(
            "SELECT mod_id, COUNT(*) as total, "
            "SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) as approved, "
            "SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) as rejected "
            "FROM form_stats GROUP BY mod_id ORDER BY total DESC LIMIT 15"
        ).fetchall()

    if not rows:
        return

    embed = discord.Embed(
        title="📊 Статистика форм по модераторам",
        color=0x3498DB,
        timestamp=discord.utils.utcnow(),
    )
    lines = []
    for i, row in enumerate(rows, 1):
        lines.append(
            f"`{i:2}.` <@{row['mod_id']}> — "
            f"✅{row['approved']} ❌{row['rejected']} 📋{row['total']}"
        )
    embed.description = "\n".join(lines)
    embed.set_footer(text="Обновлено")

    msg_id = guild_cfg.get("stats_message_id", 0)
    if msg_id:
        try:
            msg = await ch.fetch_message(msg_id)
            await msg.edit(embed=embed)
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            guild_cfg["stats_message_id"] = 0
    try:
        msg = await ch.send(embed=embed)
        guild_cfg["stats_message_id"] = msg.id
        save_config(cfg)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def post_monitoring_activity(guild: discord.Guild, cfg: dict, bot) -> None:
    """Post today's moderator activity to 👥-активность-модов channel."""
    ch = get_monitoring_channel(guild, cfg, "👥-активность-модов")
    if not ch:
        return
    from db import _conn
    today_ts = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    with _conn() as c:
        rows = c.execute(
            "SELECT mod_id, COUNT(*) as total, "
            "SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) as approved "
            "FROM form_stats WHERE ts >= ? GROUP BY mod_id ORDER BY total DESC",
            (today_ts,)
        ).fetchall()

    embed = discord.Embed(
        title="👥 Активность модераторов сегодня",
        color=0x9B59B6,
        timestamp=discord.utils.utcnow(),
    )
    if rows:
        lines = [f"<@{r['mod_id']}> — 📋{r['total']} (✅{r['approved']})" for r in rows]
        embed.description = "\n".join(lines)
    else:
        embed.description = "_Сегодня форм ещё не было._"
    embed.set_footer(text="Обновлено")

    guild_cfg = get_guild_cfg(cfg, guild.id)
    msg_id = guild_cfg.get("activity_message_id", 0)
    if msg_id:
        try:
            msg = await ch.fetch_message(msg_id)
            await msg.edit(embed=embed)
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            guild_cfg["activity_message_id"] = 0
    try:
        msg = await ch.send(embed=embed)
        guild_cfg["activity_message_id"] = msg.id
        save_config(cfg)
    except (discord.Forbidden, discord.HTTPException):
        pass


def get_log_channel(guild: discord.Guild, cfg: dict, server: str) -> discord.TextChannel | None:
    return _ch_by_id_or_name(guild, cfg, server, "logs", "📊-логи")


def get_auth_applications_channel(guild: discord.Guild, cfg: dict, server: str) -> discord.TextChannel | None:
    return _ch_by_id_or_name(guild, cfg, server, "auth", "📋-заявки-авт")


def _save_channel_to_db(guild_id: int, server: str, key: str, ch: discord.TextChannel):
    from db import set_server_channel
    set_server_channel(guild_id, server, key, ch.id)


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


async def _ensure_monitoring_category(guild: discord.Guild, cfg: dict):
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
        ("📡-статус-бота",        "Статус и аптайм бота",                      readonly_lead),
        ("🔔-авторизации",        "Журнал авторизаций, кандидатов и кик-логов", readonly_all),
        ("📊-статистика-форм",    "Статистика форм по серверам",                readonly_lead),
        ("👥-активность-модов",   "Активность модераторов за сегодня",          readonly_lead),
    ]
    guild_cfg = get_guild_cfg(cfg, guild.id)
    monitoring_ids = guild_cfg.setdefault("monitoring", {})
    for name, topic, ow in channels:
        ch = discord.utils.get(guild.text_channels, name=name, category=category)
        if not ch:
            ch = await guild.create_text_channel(name=name, category=category, topic=topic, overwrites=ow)
        monitoring_ids[name] = ch.id
    save_config(cfg)


async def ensure_server_channels(guild: discord.Guild, server: str, cfg: dict) -> dict:
    async with _get_server_lock(guild.id, server):
        return await _ensure_server_channels_locked(guild, server, cfg)


async def _ensure_server_channels_locked(guild: discord.Guild, server: str, cfg: dict) -> dict:
    # If bot's member not in cache — force-fetch it so guild.me is not None
    if guild.me is None:
        try:
            await guild.chunk()
        except Exception:
            pass

    everyone = guild.default_role
    # manage_messages needed so Discord hides moderator-only slash commands from non-moderators
    mod_perms = discord.Permissions(manage_messages=True)
    server_role = discord.utils.get(guild.roles, name=server)
    if not server_role:
        server_role = await guild.create_role(
            name=server, color=discord.Color.green(),
            hoist=True, permissions=mod_perms,
            reason=f"Роль сервера {server}",
        )
    elif not server_role.permissions.manage_messages:
        try:
            await server_role.edit(
                permissions=discord.Permissions(server_role.permissions.value | mod_perms.value),
                reason="Обновление: добавление manage_messages для видимости команд",
            )
        except discord.Forbidden:
            pass

    leadership_roles = [r for r in guild.roles if r.name in LEADERSHIP_RANKS]
    guild_cfg = get_guild_cfg(cfg, guild.id)

    def _safe_ow(mapping: dict) -> dict:
        """Remove None keys from overwrites dict (guild.me can be None if cache miss)."""
        return {k: v for k, v in mapping.items() if k is not None}

    async def _create_ch(name: str, cat: discord.CategoryChannel, overwrites: dict, **kw) -> discord.TextChannel:
        """Create text channel with overwrites; fall back to no overwrites on Forbidden."""
        ow = _safe_ow(overwrites)
        try:
            return await guild.create_text_channel(name=name, category=cat, overwrites=ow, **kw)
        except discord.Forbidden as fe:
            print(f"[ensure] create_text_channel with overwrites Forbidden: {fe.text!r} code={fe.code}")
            return await guild.create_text_channel(name=name, category=cat, **kw)

    cat_name = str(server)
    category = discord.utils.get(guild.categories, name=cat_name)
    if not category:
        cat_ow = _safe_ow({
            everyone: discord.PermissionOverwrite(view_channel=False),
            server_role: discord.PermissionOverwrite(view_channel=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        })
        try:
            category = await guild.create_category(name=cat_name, overwrites=cat_ow)
        except discord.Forbidden as fe:
            print(f"[ensure] create_category with overwrites Forbidden: {fe.text!r} code={fe.code}")
            category = await guild.create_category(name=cat_name)

    from db import get_server_channel, set_server_channel
    set_server_channel(guild.id, server, "category_id", category.id)

    def _has_linked(key: str) -> bool:
        """True if DB has a channel ID that still points to a real channel."""
        ch_id = get_server_channel(guild.id, server, key)
        if not ch_id:
            return False
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel):
            return True
        # Channel was deleted — clear stale ID so it gets recreated
        from db import clear_server_channel
        clear_server_channel(guild.id, server, key)
        return False

    def _save(key: str, ch: discord.TextChannel):
        set_server_channel(guild.id, server, key, ch.id)

    if not _has_linked("chat"):
        ch = discord.utils.get(guild.text_channels, name="💬-общение", category=category)
        if not ch:
            ch = await _create_ch("💬-общение", category, {},
                                  topic=f"Общение модераторов сервера {server}")
        _save("chat", ch)

    if not _has_linked("proof"):
        ch = discord.utils.get(guild.text_channels, name="📋-выдача-наказаний", category=category)
        if not ch:
            ch = await _create_ch("📋-выдача-наказаний", category, {},
                                  topic=f"Формы наказаний — сервер {server}")
        _save("proof", ch)

    if not _has_linked("banform"):
        ch = discord.utils.get(guild.text_channels, name="⚖️-формы-банов", category=category)
        if not ch:
            ch = await _create_ch("⚖️-формы-банов", category, {},
                                  topic=f"Формы банов и глобальных банов — сервер {server}")
        _save("banform", ch)

    lead_ow = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        server_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    for role in leadership_roles:
        lead_ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    if not _has_linked("leadership"):
        ch = discord.utils.get(guild.text_channels, name="👑-руководство", category=category)
        if not ch:
            ch = await _create_ch("👑-руководство", category, lead_ow,
                                  topic=f"Руководство сервера {server}")
        _save("leadership", ch)

    log_ow = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        server_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    for role in leadership_roles:
        log_ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)

    if not _has_linked("logs"):
        ch = discord.utils.get(guild.text_channels, name="📊-логи", category=category)
        if not ch:
            ch = await _create_ch("📊-логи", category, log_ow,
                                  topic=f"Логи наказаний — сервер {server}")
        else:
            try:
                await ch.edit(overwrites=_safe_ow(log_ow))
            except discord.Forbidden:
                pass
        _save("logs", ch)

    auth_ow = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        server_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    for role in leadership_roles:
        auth_ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)

    if not _has_linked("auth"):
        ch = discord.utils.get(guild.text_channels, name="📋-заявки-авт", category=category)
        if not ch:
            ch = await _create_ch("📋-заявки-авт", category, auth_ow,
                                  topic=f"Заявки на авторизацию — сервер {server}")
        else:
            try:
                await ch.edit(overwrites=_safe_ow(auth_ow))
            except discord.Forbidden:
                pass
        _save("auth", ch)

    for i in (1, 2):
        vc_name = f"🔊 {server} | Голосовой {i}"
        if not discord.utils.get(guild.voice_channels, name=vc_name, category=category):
            await guild.create_voice_channel(name=vc_name, category=category)

    await _ensure_common_channels(guild)

    from db import get_all_server_channels
    return get_all_server_channels(guild.id, server)


# ─── Delete confirmation view ─────────────────────────────────────────────────

class ResetServerView(discord.ui.View):
    def __init__(self, server: str, cfg: dict, bot):
        super().__init__(timeout=30)
        self.server = server
        self.cfg = cfg
        self.bot = bot
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Сбросить и пересоздать", style=discord.ButtonStyle.danger, emoji="♻️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        server = self.server

        # 1. Delete all channels in the category
        category = discord.utils.get(guild.categories, name=server)
        deleted = failed = 0
        if category:
            for ch in list(category.channels):
                try:
                    await ch.delete(reason=f"reset-server: {server}")
                    deleted += 1
                except (discord.Forbidden, discord.HTTPException):
                    failed += 1
            try:
                await category.delete(reason=f"reset-server: {server}")
            except (discord.Forbidden, discord.HTTPException):
                pass

        # 2. Clear all DB channel IDs for this server
        from db import clear_server_channel
        for key in ("chat", "proof", "banform", "leadership", "logs", "auth", "category_id"):
            clear_server_channel(guild.id, server, key)

        # 3. Recreate everything
        try:
            await ensure_server_channels(guild, server, self.cfg)
        except discord.Forbidden:
            await interaction.followup.send("❌ Нет прав для создания каналов.", ephemeral=True)
            self.stop()
            return

        self.stop()
        msg = f"♻️ Сервер **{server}** сброшен и пересоздан (удалено каналов: {deleted}"
        if failed:
            msg += f", не удалось удалить: {failed}"
        msg += ")."
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Отменено.", view=None)


class DeleteCategoryView(discord.ui.View):
    def __init__(self, server: str, cfg: dict):
        super().__init__(timeout=30)
        self.server = server
        self.cfg = cfg
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Удалить", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        category = discord.utils.get(guild.categories, name=self.server)
        deleted = failed = 0
        category_deleted = False

        if category:
            for ch in list(category.channels):
                try:
                    await ch.delete(reason=f"manage-category delete: {self.server}")
                    deleted += 1
                except (discord.Forbidden, discord.HTTPException):
                    failed += 1
            try:
                await category.delete(reason=f"manage-category delete: {self.server}")
                category_deleted = True
            except (discord.Forbidden, discord.HTTPException):
                pass

        if category_deleted:
            guild_cfg = get_guild_cfg(self.cfg, guild.id)
            guild_cfg.get("servers", {}).pop(self.server, None)
            save_config(self.cfg)
            from db import clear_server_channel
            for key in ("chat", "proof", "banform", "leadership", "logs", "auth", "category_id"):
                clear_server_channel(guild.id, self.server, key)

        self.stop()
        msg = f"🗑️ Категория **{self.server}** удалена ({deleted} каналов)."
        if failed:
            msg += f"\n⚠️ {failed} каналов не удалены (нет прав)."
        if not category_deleted and category:
            msg = f"❌ Не удалось удалить категорию **{self.server}** (нет прав)."
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Отменено.", view=None)


# ─── Cog ─────────────────────────────────────────────────────────────────────

async def _update_bot_presence(bot: commands.Bot):
    global _presence_index
    from db import all_user_servers, get_global_form_counts
    from datetime import datetime
    mods = len(all_user_servers())
    total_forms, approved = get_global_form_counts()
    ping_ms = round(bot.latency * 1000)

    activities = [
        discord.Activity(type=discord.ActivityType.watching,  name=f"за {mods} модераторами 🛡️"),
        discord.Activity(type=discord.ActivityType.watching,  name=f"{total_forms} форм | ✅ {approved} одобрено"),
        discord.Activity(type=discord.ActivityType.listening, name="/proof • /auth • /banform"),
        discord.Activity(type=discord.ActivityType.watching,  name=f"пинг {ping_ms}мс 📡"),
        discord.Game(name="Black Russia Moderation"),
    ]

    activity = activities[_presence_index % len(activities)]
    _presence_index += 1

    status = discord.Status.online if ping_ms < 500 else discord.Status.idle
    await bot.change_presence(status=status, activity=activity)


class ServersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.status_loop.start()
        self.presence_loop.start()

    def cog_unload(self):
        self.status_loop.cancel()
        self.presence_loop.cancel()

    @tasks.loop(hours=1)
    async def status_loop(self):
        for guild in self.bot.guilds:
            await post_monitoring_status(guild, self.bot.cfg, self.bot)
            await post_monitoring_stats(guild, self.bot.cfg, self.bot)
            await post_monitoring_activity(guild, self.bot.cfg, self.bot)

    @status_loop.before_loop
    async def before_status_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=15)
    async def presence_loop(self):
        await _update_bot_presence(self.bot)

    @presence_loop.before_loop
    async def before_presence_loop(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await post_monitoring_status(guild, self.bot.cfg, self.bot)
            await post_monitoring_stats(guild, self.bot.cfg, self.bot)
            await post_monitoring_activity(guild, self.bot.cfg, self.bot)
            await self._startup_sync(guild)
        await _update_bot_presence(self.bot)

    async def _startup_sync(self, guild: discord.Guild):
        """Sync member DB entries and ensure all known server channels exist."""
        from db import set_user_server, remove_user_server

        # 1. Sync DB from current roles for every member
        # Only SET entries — never delete, removal is handled by on_member_update
        for member in guild.members:
            if member.bot:
                continue
            server_roles = [r.name for r in member.roles if is_server_role(r.name)]
            if len(server_roles) == 1:
                set_user_server(member.id, server_roles[0])


    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        before_servers = {r.name for r in before.roles if is_server_role(r.name)}
        after_servers  = {r.name for r in after.roles  if is_server_role(r.name)}

        # Auto-create channels for newly assigned server role
        for name in after_servers - before_servers:
            try:
                await ensure_server_channels(after.guild, name, self.bot.cfg)
            except discord.Forbidden:
                pass

        # Keep DB in sync: update when server roles changed
        if after_servers != before_servers:
            from db import set_user_server, remove_user_server
            if len(after_servers) == 1:
                set_user_server(after.id, next(iter(after_servers)))
            elif not after_servers:
                remove_user_server(after.id)

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        uid = interaction.user.id
        return uid == interaction.guild.owner_id or uid == getattr(self.bot, "owner_id_cfg", 0)

    @app_commands.default_permissions(administrator=True)
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
            await interaction.followup.send("❌ Нет прав для создания каналов/ролей.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка при создании каналов: `{e}`", ephemeral=True)
            import traceback; traceback.print_exc()

    @app_commands.default_permissions(administrator=True)
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
            view = DeleteCategoryView(server, self.bot.cfg)
            await interaction.response.send_message(
                f"⚠️ Удалить категорию **{server}** и все {ch_count} каналов в ней?",
                view=view,
                ephemeral=True,
            )
            view.message = await interaction.original_response()

        elif action == "monitoring":
            await interaction.response.defer(ephemeral=True)
            try:
                await _ensure_monitoring_category(interaction.guild, self.bot.cfg)
                await post_monitoring_status(interaction.guild, self.bot.cfg, self.bot)
                await interaction.followup.send(
                    f"✅ Категория **{MONITORING_CATEGORY}** создана/обновлена.", ephemeral=True
                )
            except discord.Forbidden:
                await interaction.followup.send("❌ Нет прав для создания каналов.", ephemeral=True)

    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="linkserver", description="Привязать существующие каналы к серверу")
    @app_commands.describe(
        server="Номер сервера (1–90)",
        proof="Канал выдачи наказаний (📋-выдача-наказаний)",
        banform="Канал форм банов (⚖️-формы-банов)",
        logs="Канал логов (📊-логи)",
        auth="Канал заявок авторизации (📋-заявки-авт)",
    )
    async def linkserver_cmd(
        self,
        interaction: discord.Interaction,
        server: str,
        proof: discord.TextChannel | None = None,
        banform: discord.TextChannel | None = None,
        logs: discord.TextChannel | None = None,
        auth: discord.TextChannel | None = None,
    ):
        if not self._is_owner(interaction):
            await interaction.response.send_message("❌ Только для владельца сервера.", ephemeral=True)
            return
        if not (server.isdigit() and 1 <= int(server) <= 90):
            await interaction.response.send_message("❌ Укажите число от 1 до 90.", ephemeral=True)
            return
        if not any([proof, banform, logs, auth]):
            await interaction.response.send_message(
                "❌ Укажите хотя бы один канал.", ephemeral=True
            )
            return

        from db import set_server_channel

        changed_lines = []
        same_lines = []

        for key, ch, label in [
            ("proof",   proof,   "📋 Наказания"),
            ("banform", banform, "⚖️ Баны"),
            ("logs",    logs,    "📊 Логи"),
            ("auth",    auth,    "📋 Авт. заявки"),
        ]:
            if ch is None:
                continue
            updated = set_server_channel(interaction.guild.id, server, key, ch.id)
            if updated:
                changed_lines.append(f"{label} → {ch.mention}")
            else:
                same_lines.append(f"{label} → {ch.mention} (ID идентичен, замена не произошла)")

        parts = []
        if changed_lines:
            parts.append("✅ Привязано:\n" + "\n".join(changed_lines))
        if same_lines:
            parts.append("ℹ️ Без изменений:\n" + "\n".join(same_lines))

        await interaction.response.send_message(
            f"**Сервер {server}**\n" + "\n\n".join(parts),
            ephemeral=True,
        )

    @app_commands.default_permissions(administrator=True)
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
                except (discord.Forbidden, discord.HTTPException):
                    pass
            else:
                seen_names[ch.name] = ch

        if deleted:
            await interaction.followup.send(f"🗑️ Удалено дублей: **{deleted}** в категории **{server}**.", ephemeral=True)
        else:
            await interaction.followup.send(f"✅ Дублей в категории **{server}** не найдено.", ephemeral=True)

    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="resetserver", description="Удалить все каналы сервера и пересоздать их заново")
    @app_commands.describe(server="Номер сервера (1–90)")
    async def resetserver_cmd(self, interaction: discord.Interaction, server: str):
        if not self._is_owner(interaction):
            await interaction.response.send_message("❌ Только для владельца сервера.", ephemeral=True)
            return
        if not (server.isdigit() and 1 <= int(server) <= 90):
            await interaction.response.send_message("❌ Укажите число от 1 до 90.", ephemeral=True)
            return

        category = discord.utils.get(interaction.guild.categories, name=server)
        ch_count = len(category.channels) if category else 0
        view = ResetServerView(server, self.bot.cfg, self.bot)
        await interaction.response.send_message(
            f"⚠️ Все каналы категории **{server}** ({ch_count} шт.) будут **удалены** и пересозданы с нуля.\n"
            f"Все сохранённые ID в базе данных будут сброшены. Продолжить?",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="listmods", description="Список модераторов по серверам")
    async def listmods_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        from db import all_user_servers
        db_servers = all_user_servers()  # batch fetch once

        servers: dict[str, list[str]] = {}
        for member in guild.members:
            server = next(
                (r.name for r in member.roles if is_server_role(r.name)), None
            ) or db_servers.get(str(member.id))
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

        embeds: list[discord.Embed] = []
        embed = discord.Embed(title="👥 Модераторы по серверам", color=0x2ECC71)
        for server_num in sorted(servers.keys(), key=lambda x: int(x) if x.isdigit() else 0):
            mods_list = servers[server_num]
            chunk: list[str] = []
            chunk_len = 0
            part = 1
            for entry in mods_list:
                line = entry + "\n"
                if chunk_len + len(line) > 1024 and chunk:
                    label = f"Сервер {server_num} ({len(mods_list)})" if part == 1 else f"Сервер {server_num} (прод.)"
                    if len(embed.fields) >= 25:
                        embeds.append(embed)
                        embed = discord.Embed(color=0x2ECC71)
                    embed.add_field(name=label, value="\n".join(chunk), inline=False)
                    chunk, chunk_len, part = [], 0, part + 1
                chunk.append(entry)
                chunk_len += len(line)
            if chunk:
                label = f"Сервер {server_num} ({len(mods_list)})" if part == 1 else f"Сервер {server_num} (прод.)"
                if len(embed.fields) >= 25:
                    embeds.append(embed)
                    embed = discord.Embed(color=0x2ECC71)
                embed.add_field(name=label, value="\n".join(chunk), inline=False)

        embeds.append(embed)
        # Discord allows max 10 embeds per message
        for i in range(0, len(embeds), 10):
            await interaction.followup.send(embeds=embeds[i:i+10], ephemeral=True)


async def setup(bot: commands.Bot):
    bot.add_view(StatusRefreshView())
    await bot.add_cog(ServersCog(bot))
