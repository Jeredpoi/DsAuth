import asyncio
from aiohttp import web
import aiohttp

_bot_ref = None


async def _health_handler(request):
    if _bot_ref and _bot_ref.is_ready():
        ping = round(_bot_ref.latency * 1000)
        guilds = len(_bot_ref.guilds)
        name = str(_bot_ref.user) if _bot_ref.user else "..."
        text = f"OK | {name} | guilds={guilds} | ping={ping}ms"
    else:
        text = "STARTING"
    return web.Response(text=text, content_type="text/plain")


async def start_webserver(bot=None):
    global _bot_ref
    _bot_ref = bot
    app = web.Application()
    app.router.add_get("/", _health_handler)
    app.router.add_get("/health", _health_handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    print("   [web] HTTP сервер запущен на :8080")
    return runner


async def self_ping_loop():
    """Ping own server every 4 min to keep Replit alive."""
    await asyncio.sleep(60)
    print("   [keepalive] Self-ping loop запущен")
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get("http://localhost:8080/health") as resp:
                    pass
            except Exception:
                pass
            await asyncio.sleep(240)
