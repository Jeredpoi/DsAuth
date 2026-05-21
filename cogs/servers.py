import discord
from discord import app_commands
from discord.ext import commands

from helpers import save_config, get_guild_cfg, LEADERSHIP_RANKS, RANKS

COMMON_CATEGORY = "🌐 Общие каналы"


def is_server_role(name: str) -> bool:
    return name.isdigit() and 1 <= int(name) <= 90


def get_server_for_member(member: discord.Member) -> str | None:
    for role in member.roles:
        if is_server_role(role.name):
            return role.name
    return None


def get_proof_channel(guild: discord.Guild, cfg: dict, server: str) -> discord.TextChannel | None:
    # Сначала ищем по сохранённому ID
    guild_cfg = get_guild_cfg(cfg, guild.id)
    ch_id = guild_cfg.get("servers", {}).get(server, {}).get("proof")
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch:
        return ch
    # Fallback: ищем по имени канала в категории сервера
    category = discord.utils.get(guild.categories, name=str(server))
    if category:
        return discord.utils.get(guild.text_channels, name="📋-выдача-наказаний", category=category)
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

    # Общий текстовый чат для всех модераторов
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


async def ensure_server_channels(guild: discord.Guild, server: str, cfg: dict) -> dict:
    everyone = guild.default_role
    server_role = discord.utils.get(guild.roles, name=server)
    if not server_role:
        server_role = await guild.create_role(
            name=server, color=discord.Color.blue(),
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

    @app_commands.command(name="setupserver", description="Создать каналы для сервера вручную")
    @app_commands.describe(server="Номер сервера (1–90)")
    async def setupserver_cmd(self, interaction: discord.Interaction, server: str):
        if not (server.isdigit() and 1 <= int(server) <= 90):
            await interaction.response.send_message("❌ Укажите число от 1 до 90.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await ensure_server_channels(interaction.guild, server, self.bot.cfg)
            await interaction.followup.send(f"✅ Каналы для сервера **{server}** созданы/обновлены.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ Нет прав для создания каналов.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServersCog(bot))
