import asyncio
import os
import subprocess
import traceback

import discord
from discord.ext import commands
from dotenv import load_dotenv

from helpers import load_config
from keep_alive import keep_alive
import db

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PROXY = os.getenv("PROXY_URL")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

db.init_db()
bot = commands.Bot(
    command_prefix=commands.when_mentioned,
    intents=intents,
    proxy=PROXY,
    member_cache_flags=discord.MemberCacheFlags.all(),
    chunk_guilds_at_startup=True,
)
cfg = load_config()
cfg["home_guild_id"] = 1504395064175099985  # главный сервер где созданы все каналы
db.migrate_from_json(cfg)   # переносит user_servers и stats.json → SQLite (один раз)
bot.cfg = cfg
bot.owner_id_cfg = OWNER_ID


async def main():
    async with bot:
        await bot.load_extension("cogs.proof")
        await bot.load_extension("cogs.admin")
        await bot.load_extension("cogs.auth")
        await bot.load_extension("cogs.servers")
        await bot.load_extension("cogs.stats")
        await bot.load_extension("cogs.info")
        await bot.load_extension("cogs.context_menus")
        await bot.start(TOKEN)


def _git_version() -> str:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
        date = subprocess.check_output(
            ["git", "log", "-1", "--format=%cd", "--date=format:%d.%m.%Y %H:%M"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        return f"{date}  (коммит {commit})"
    except Exception:
        return "неизвестно"


@bot.event
async def on_ready():
    # Ensure member cache is fully populated so guild.me is never None
    for guild in bot.guilds:
        if not guild.chunked:
            try:
                await guild.chunk()
            except Exception:
                pass
    try:
        await bot.user.edit(description="🛡️ Бот для управления модерацией\n📋 Формы наказаний • 🔐 Авторизация • 📊 Статистика")
    except Exception:
        pass
    await bot.tree.sync()
    print(f"✅ {bot.user} (ID: {bot.user.id})")
    print(f"   Версия: {_git_version()}")
    print(f"   Серверов: {len(bot.guilds)}")
    print("   Используйте !sync на сервере для мгновенной синхронизации команд.")


async def _send_error_to_monitoring(title: str, error_text: str):
    """Send error report to the monitoring channel in every guild."""
    from helpers import get_guild_cfg
    for guild in bot.guilds:
        guild_cfg = get_guild_cfg(cfg, guild.id)
        ch_id = guild_cfg.get("monitoring", {}).get("🔔-авторизации", 0)
        ch = guild.get_channel(ch_id) if ch_id else None
        if not ch:
            from cogs.servers import MONITORING_CATEGORY
            cat = discord.utils.get(guild.categories, name=MONITORING_CATEGORY)
            if cat:
                ch = discord.utils.get(guild.text_channels, name="🔔-авторизации", category=cat)
        if not ch:
            continue
        embed = discord.Embed(
            title=f"🚨 {title}",
            description=f"```\n{error_text[:3900]}\n```",
            color=0xE74C3C,
            timestamp=discord.utils.utcnow(),
        )
        try:
            await ch.send(embed=embed)
        except Exception:
            pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    tb = traceback.format_exc()
    print(tb)
    cmd = getattr(interaction.command, "name", "unknown")
    await _send_error_to_monitoring(
        f"Ошибка команды `/{cmd}`",
        f"Пользователь: {interaction.user} ({interaction.user.id})\n{tb}",
    )
    msg = f"❌ Произошла ошибка: `{error}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_error(event: str, *args, **kwargs):
    tb = traceback.format_exc()
    print(tb)
    await _send_error_to_monitoring(f"Ошибка события `{event}`", tb)


keep_alive()
asyncio.run(main())
