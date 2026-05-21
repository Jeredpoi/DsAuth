import re

import discord
from discord.ext import commands

# ─── Список серверов — добавляйте сюда новые ─────────────────────────────────
# Имя должно совпадать с именем роли на сервере Discord
SERVERS: list[str] = [
    "49-й сервер",
    "50-й сервер",
]

COMMON_CATEGORY = "🔊 Общие каналы"
_SERVER_ROLE_SET: set[str] = set(SERVERS)


def _slug(name: str) -> str:
    """'49-й сервер' → '49-й-сервер' для имени канала."""
    return re.sub(r"\s+", "-", name.strip().lower())


async def _ensure_common_channels(guild: discord.Guild):
    category = discord.utils.get(guild.categories, name=COMMON_CATEGORY)
    if not category:
        category = await guild.create_category(
            name=COMMON_CATEGORY,
            reason="Общие голосовые каналы",
        )
    for i in (1, 2):
        name = f"🔊 Общий {i}"
        if not discord.utils.get(guild.voice_channels, name=name):
            await guild.create_voice_channel(
                name=name, category=category,
                reason="Автосоздание общего голосового канала",
            )


async def _ensure_server_channels(guild: discord.Guild, server_name: str):
    """Создаёт категорию и каналы для сервера если их нет."""
    cat_name = f"🖥️ {server_name}"
    category = discord.utils.get(guild.categories, name=cat_name)
    if not category:
        category = await guild.create_category(
            name=cat_name,
            reason=f"Автосоздание каналов для {server_name}",
        )

    slug = _slug(server_name)

    # Текстовый канал выдачи наказаний
    text_name = f"📋-выдача-{slug}"
    if not discord.utils.get(guild.text_channels, name=text_name):
        await guild.create_text_channel(
            name=text_name,
            category=category,
            topic=f"Выдача наказаний — {server_name}",
            reason=f"Автосоздание для {server_name}",
        )

    # Два голосовых канала
    for i in (1, 2):
        vc_name = f"🔊 {server_name} {i}"
        if not discord.utils.get(guild.voice_channels, name=vc_name):
            await guild.create_voice_channel(
                name=vc_name, category=category,
                reason=f"Автосоздание для {server_name}",
            )

    await _ensure_common_channels(guild)


class ServersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        new_roles = {r.name for r in after.roles} - {r.name for r in before.roles}
        for role_name in new_roles:
            if role_name in _SERVER_ROLE_SET:
                try:
                    await _ensure_server_channels(after.guild, role_name)
                except discord.Forbidden:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ServersCog(bot))
