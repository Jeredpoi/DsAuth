import asyncio
import os

from aiohttp import web
import aiohttp


def _public_url() -> str:
    """Return the public health-check URL on Replit, or localhost as fallback.

    Replit only counts EXTERNAL requests as activity, so we must ping the
    public domain rather than localhost when possible.
    """
    domain = os.getenv("REPLIT_DEV_DOMAIN")
    if not domain:
        slug = os.getenv("REPL_SLUG")
        owner = os.getenv("REPL_OWNER")
        if slug and owner:
            domain = f"{slug}.{owner}.repl.co"
    if domain:
        return f"https://{domain}/health"
    return "http://localhost:8080/health"

_bot_ref = None
_last_ok_ping = 0.0  # время последнего успешного self-ping (time.time())


async def _health_handler(request):
    import time as _time
    if _bot_ref and _bot_ref.is_ready():
        ping = round(_bot_ref.latency * 1000)
        guilds = len(_bot_ref.guilds)
        name = str(_bot_ref.user) if _bot_ref.user else "..."
        ago = int(_time.time() - _last_ok_ping) if _last_ok_ping else -1
        text = f"OK | {name} | guilds={guilds} | ping={ping}ms | selfping={ago}s ago"
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


PING_INTERVAL = 60   # Replit засыпает через ~5 мин без внешней активности — пингуем чаще
LOCAL_URL = "http://localhost:8080/health"


async def self_ping_loop():
    """Ping own server every minute to keep Replit alive.

    The loop must NEVER die: every iteration is fully wrapped, and the HTTP
    session is recreated after failures. Consecutive failures are logged so
    the cause is visible in the console when UptimeRobot reports downtime.
    """
    await asyncio.sleep(30)
    url = _public_url()
    print("   [keepalive] Self-ping loop запущен")
    print(f"   [keepalive] Self-ping → {url} (каждые {PING_INTERVAL}с)")

    timeout = aiohttp.ClientTimeout(total=15)
    session: aiohttp.ClientSession | None = None
    fail_streak = 0

    while True:
        try:
            if session is None or session.closed:
                session = aiohttp.ClientSession(timeout=timeout)

            ok = False
            try:
                async with session.get(url) as resp:
                    ok = resp.status < 500
            except Exception as e:
                # Public URL failed — check the local server so we know whether
                # the web server itself or the external route is the problem
                try:
                    async with session.get(LOCAL_URL) as resp:
                        local_ok = resp.status < 500
                except Exception:
                    local_ok = False
                if fail_streak in (0, 4, 9):  # log 1st, 5th, 10th failure
                    print(f"   [keepalive] ⚠️ Пинг {url} не прошёл ({type(e).__name__}); "
                          f"локальный сервер: {'жив' if local_ok else 'НЕ ОТВЕЧАЕТ'}")
                # Recreate session — a broken connector can poison all later requests
                try:
                    await session.close()
                except Exception:
                    pass
                session = None

            if ok:
                global _last_ok_ping
                import time as _time
                _last_ok_ping = _time.time()
                if fail_streak >= 5:
                    print(f"   [keepalive] ✅ Пинг восстановлен после {fail_streak} ошибок")
                fail_streak = 0
            else:
                fail_streak += 1
        except Exception:
            # Absolute safety net — the loop itself must survive anything
            fail_streak += 1
            session = None
        try:
            await asyncio.sleep(PING_INTERVAL)
        except asyncio.CancelledError:
            raise
