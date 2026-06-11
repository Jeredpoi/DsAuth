import asyncio
import os
import subprocess
import traceback

import discord
from discord.ext import commands
from dotenv import load_dotenv

from helpers import load_config
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
        from keep_alive import start_webserver, self_ping_loop
        await start_webserver(bot)
        _keepalive_task = asyncio.create_task(self_ping_loop())
        bot._keepalive_task = _keepalive_task  # prevent garbage collection
        _exts = [
            "cogs.proof",
            "cogs.admin",
            "cogs.auth",
            "cogs.servers",
            "cogs.stats",
            "cogs.info",
            "cogs.context_menus",
            "cogs.modtools",
        ]
        for _ext in _exts:
            try:
                await bot.load_extension(_ext)
                print(f"   ✅ Загружен: {_ext}")
            except Exception as _e:
                print(f"   ❌ Ошибка загрузки {_ext}: {_e}")
                traceback.print_exc()
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


_ready_once = False


@bot.event
async def on_ready():
    global _ready_once
    if _ready_once:
        print(f"🔁 Переподключение: {bot.user}")
        return
    _ready_once = True
    # Ensure member cache is fully populated so guild.me is never None
    for guild in bot.guilds:
        if not guild.chunked:
            try:
                await guild.chunk()
            except Exception:
                pass
    await bot.tree.sync()
    print(f"✅ {bot.user} (ID: {bot.user.id})")
    print(f"   Версия:   {_git_version()}")
    print(f"   Серверов: {len(bot.guilds)}")
    print(f"   Пинг:     {round(bot.latency * 1000)} мс")
    print("─" * 48)
    print("   ℹ️  Описание профиля бота (About Me) задаётся")
    print("      вручную в Discord Developer Portal:")
    print("      discord.com/developers/applications → выбери бота → Bot → About Me")
    print("   Рекомендуемый текст:")
    print("      🛡️ Система управления модерацией Black Russia")
    print("      📋 Формы наказаний  •  🔐 Авторизация  •  📊 Статистика")
    print("─" * 48)


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
        ts = int(discord.utils.utcnow().timestamp())
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"## 🚨 {title}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"```\n{error_text[:3500]}\n```"),
            discord.ui.TextDisplay(f"-# <t:{ts}:f>"),
            accent_color=0xE74C3C,
        ))
        try:
            await ch.send(view=view)
        except Exception:
            pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    tb = traceback.format_exc()
    print(tb)
    cmd = getattr(interaction.command, "name", "unknown")
    # For autocomplete interactions, respond() and send_message() are invalid —
    # only autocomplete() works, so just log and return.
    if interaction.type == discord.InteractionType.autocomplete:
        print(f"   [autocomplete error] /{cmd}: {error}")
        return
    # Answer the user first — the 3-second initial-response window can expire
    # if we do slow network calls before responding.
    msg = f"❌ Произошла ошибка: `{error}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass
    await _send_error_to_monitoring(
        f"Ошибка команды `/{cmd}`",
        f"Пользователь: {interaction.user} ({interaction.user.id})\n{tb}",
    )


@bot.event
async def on_error(event: str, *args, **kwargs):
    tb = traceback.format_exc()
    print(tb)
    await _send_error_to_monitoring(f"Ошибка события `{event}`", tb)


asyncio.run(main())
