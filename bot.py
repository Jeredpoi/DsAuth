import asyncio
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from helpers import load_config
from keep_alive import keep_alive

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PROXY = os.getenv("PROXY_URL")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, proxy=PROXY)
bot.cfg = load_config()
bot.owner_id_cfg = OWNER_ID


async def main():
    async with bot:
        await bot.load_extension("cogs.proof")
        await bot.load_extension("cogs.admin")
        await bot.load_extension("cogs.auth")
        await bot.load_extension("cogs.servers")
        await bot.start(TOKEN)


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ {bot.user} (ID: {bot.user.id})")
    print(f"   Серверов: {len(bot.guilds)}")
    print("   Используйте !sync на сервере для мгновенной синхронизации команд.")


keep_alive()
asyncio.run(main())
