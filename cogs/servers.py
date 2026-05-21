import re

import discord
from discord.ext import commands

from helpers import save_config, get_guild_cfg, LEADERSHIP_RANKS

# ─── Список серверов ─────────────────────────────────────────────────────────
# Имя должно совпадать с именем роли на Discord-сервере.
# Добавляйте новые серверы сюда:
SERVERS: list[str] = [
    "49",
    "50",
]

SERVER_SET: set[str] = set(SERVERS)
COMMON_CATEGORY = "🌐 Общие каналы"


def _find_server_channel(guild: discord.Guild, server: str, channel_key: str) -> discord.TextChannel | None:
    """Ищет канал по сохранённому ID в конфиге."""
    cfg_key = f"server_{server}_{channel_key}"
    for g_cfg in [get_guild_cfg.__wrapped__ if hasattr(get_guild_cfg, '__wrapped__') else None]:
        pass
    return None


async def _ensure_common_channels(guild: discord.Guild):
    category = discord.utils.get(guild.categories, name=COMMON_CATEGORY)
    if not category:
        category = await guild.create_category(name=COMMON_CATEGORY)
    for i in (1, 2):
        name = f"🔊 Общий {i}"
        if not discord.utils.get(guild.voice_channels, name=name):
            await guild.create_voice_channel(name=name, category=category)


async def ensure_server_channels(guild: discord.Guild, server: str, cfg: dict) -> dict:
    """Создаёт все каналы для сервера. Возвращает dict с ID созданных каналов."""
    everyone = guild.default_role
    server_role = discord.utils.get(guild.roles, name=server)
    if not server_role:
        server_role = await guild.create_role(
            name=server, color=discord.Color.blue(),
            hoist=True, reason=f"Роль сервера {server}",
        )

    # Роли руководства
    leadership_roles = [r for r in guild.roles if r.name in LEADERSHIP_RANKS]

    # Категория
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

    # Общение
    ch = discord.utils.get(guild.text_channels, name="💬-общение", category=category)
    if not ch:
        ch = await guild.create_text_channel(
            name="💬-общение", category=category,
            topic=f"Общение модераторов сервера {server}",
        )
    channel_ids["chat"] = ch.id

    # Выдача наказаний
    ch = discord.utils.get(guild.text_channels, name="📋-выдача-наказаний", category=category)
    if not ch:
        ch = await guild.create_text_channel(
            name="📋-выдача-наказаний", category=category,
            topic=f"Формы наказаний — сервер {server}",
        )
    channel_ids["proof"] = ch.id

    # Руководство (Куратор, Зам, Главный)
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

    # Логи (readonly для роли сервера)
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

    # Голосовые
    for i in (1, 2):
        vc_name = f"🔊 {server} | Голосовой {i}"
        if not discord.utils.get(guild.voice_channels, name=vc_name):
            await guild.create_voice_channel(name=vc_name, category=category)

    await _ensure_common_channels(guild)

    # Сохраняем ID каналов в конфиг
    guild_cfg = get_guild_cfg(cfg, guild.id)
    guild_cfg.setdefault("servers", {})[server] = channel_ids
    save_config(cfg)

    return channel_ids


def get_server_for_member(member: discord.Member) -> str | None:
    """Возвращает имя сервера по роли участника."""
    for role in member.roles:
        if role.name in SERVER_SET:
            return role.name
    return None


def get_proof_channel(guild: discord.Guild, cfg: dict, server: str) -> discord.TextChannel | None:
    guild_cfg = get_guild_cfg(cfg, guild.id)
    ch_id = guild_cfg.get("servers", {}).get(server, {}).get("proof")
    return guild.get_channel(ch_id) if ch_id else None


def get_log_channel(guild: discord.Guild, cfg: dict, server: str) -> discord.TextChannel | None:
    guild_cfg = get_guild_cfg(cfg, guild.id)
    ch_id = guild_cfg.get("servers", {}).get(server, {}).get("logs")
    return guild.get_channel(ch_id) if ch_id else None


class ServersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        new_role_names = {r.name for r in after.roles} - {r.name for r in before.roles}
        for name in new_role_names:
            if name in SERVER_SET:
                try:
                    await ensure_server_channels(after.guild, name, self.bot.cfg)
                except discord.Forbidden:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ServersCog(bot))
