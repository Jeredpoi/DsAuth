"""
Единая SQLite база данных.
Заменяет stats.json (form_stats) и cfg["user_servers"] (user_servers).
Также хранит cooldown'ы заявок на авторизацию.
"""
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = "bot_data.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute("PRAGMA journal_mode=WAL").fetchone()
        # Merge any leftover WAL into the main DB on every startup so the
        # .db file is always consistent even if .shm/.wal were deleted.
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS user_servers (
            user_id   TEXT PRIMARY KEY,
            server    TEXT NOT NULL,
            rank      TEXT NOT NULL DEFAULT '',
            auth_at   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS auth_cooldowns (
            user_id     TEXT PRIMARY KEY,
            rejected_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS form_stats (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            mod_id    INTEGER NOT NULL,
            form_type TEXT NOT NULL,
            status    TEXT NOT NULL,
            ts        INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );

        CREATE INDEX IF NOT EXISTS idx_form_stats_mod ON form_stats(mod_id, ts);

        CREATE TABLE IF NOT EXISTS server_channels (
            guild_id   TEXT NOT NULL,
            server     TEXT NOT NULL,
            key        TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, server, key)
        );
        """)


# ─── user_servers ─────────────────────────────────────────────────────────────

def get_user_server(user_id: int) -> str | None:
    with _conn() as c:
        row = c.execute(
            "SELECT server FROM user_servers WHERE user_id=?", (str(user_id),)
        ).fetchone()
        return row["server"] if row else None


def set_user_server(user_id: int, server: str, rank: str | None = None):
    with _conn() as c:
        if rank is not None:
            c.execute(
                """INSERT INTO user_servers (user_id, server, rank, auth_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE
                   SET server=excluded.server,
                       rank=excluded.rank,
                       auth_at=excluded.auth_at""",
                (str(user_id), server, rank, int(time.time())),
            )
        else:
            c.execute(
                """INSERT INTO user_servers (user_id, server, auth_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE
                   SET server=excluded.server,
                       auth_at=excluded.auth_at""",
                (str(user_id), server, int(time.time())),
            )


def remove_user_server(user_id: int):
    with _conn() as c:
        c.execute("DELETE FROM user_servers WHERE user_id=?", (str(user_id),))


def all_user_servers() -> dict[str, str]:
    """Returns {user_id_str: server_str} — used for autocomplete."""
    with _conn() as c:
        rows = c.execute("SELECT user_id, server FROM user_servers").fetchall()
        return {r["user_id"]: r["server"] for r in rows}


# ─── auth cooldowns ────────────────────────────────────────────────────────────

def get_auth_cooldown_remaining(user_id: int) -> int:
    """Секунды до окончания cooldown. 0 = нет cooldown."""
    with _conn() as c:
        row = c.execute(
            "SELECT rejected_at FROM auth_cooldowns WHERE user_id=?", (str(user_id),)
        ).fetchone()
    if not row:
        return 0
    expires = row["rejected_at"] + 86400  # 24 часа
    remaining = expires - int(time.time())
    return max(0, remaining)


def set_auth_cooldown(user_id: int):
    with _conn() as c:
        c.execute(
            """INSERT INTO auth_cooldowns (user_id, rejected_at) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET rejected_at=excluded.rejected_at""",
            (str(user_id), int(time.time())),
        )


def clear_auth_cooldown(user_id: int):
    with _conn() as c:
        c.execute("DELETE FROM auth_cooldowns WHERE user_id=?", (str(user_id),))


# ─── server_channels ──────────────────────────────────────────────────────────

def get_server_channel(guild_id: int, server: str, key: str) -> int:
    """Return channel_id for (guild, server, key), or 0 if not set."""
    with _conn() as c:
        row = c.execute(
            "SELECT channel_id FROM server_channels WHERE guild_id=? AND server=? AND key=?",
            (str(guild_id), str(server), key),
        ).fetchone()
        return row["channel_id"] if row else 0


def set_server_channel(guild_id: int, server: str, key: str, channel_id: int) -> bool:
    """
    Save (guild, server, key) → channel_id.
    Returns True if the value changed, False if it was already the same.
    """
    existing = get_server_channel(guild_id, server, key)
    if existing == channel_id:
        return False
    with _conn() as c:
        c.execute(
            """INSERT INTO server_channels (guild_id, server, key, channel_id)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, server, key) DO UPDATE SET channel_id=excluded.channel_id""",
            (str(guild_id), str(server), key, channel_id),
        )
    return True


def get_all_server_channels(guild_id: int, server: str) -> dict[str, int]:
    """Return {key: channel_id} for a server."""
    with _conn() as c:
        rows = c.execute(
            "SELECT key, channel_id FROM server_channels WHERE guild_id=? AND server=?",
            (str(guild_id), str(server)),
        ).fetchall()
        return {r["key"]: r["channel_id"] for r in rows}


def clear_server_channel(guild_id: int, server: str, key: str):
    with _conn() as c:
        c.execute(
            "DELETE FROM server_channels WHERE guild_id=? AND server=? AND key=?",
            (str(guild_id), str(server), key),
        )


# ─── form stats ────────────────────────────────────────────────────────────────

def get_global_form_counts() -> tuple[int, int]:
    """Returns (total, approved) form counts across all moderators."""
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM form_stats").fetchone()[0]
        approved = c.execute(
            "SELECT COUNT(*) FROM form_stats WHERE status='approved'"
        ).fetchone()[0]
    return total, approved


def record_form(mod_id: int, form_type: str, status: str):
    if not mod_id:
        return
    with _conn() as c:
        c.execute(
            "INSERT INTO form_stats (mod_id, form_type, status) VALUES (?, ?, ?)",
            (mod_id, form_type, status),
        )


def get_stats(mod_id: int, since_ts: int = 0) -> dict:
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) as cnt FROM form_stats "
            "WHERE mod_id=? AND ts>=? GROUP BY status",
            (mod_id, since_ts),
        ).fetchall()
    result = {"sent": 0, "approved": 0, "rejected": 0}
    for r in rows:
        if r["status"] in result:
            result[r["status"]] = r["cnt"]
    return result


# ─── migration ────────────────────────────────────────────────────────────────

def migrate_from_json(cfg: dict):
    """Переносит user_servers из config.json и форм-статистику из stats.json в SQLite.
    Вызывается один раз при старте; повторный вызов безопасен (ON CONFLICT IGNORE)."""
    import json, os

    # user_servers из config
    for uid, srv in cfg.pop("user_servers", {}).items():
        with _conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO user_servers (user_id, server) VALUES (?, ?)",
                (str(uid), str(srv)),
            )

    # form stats из stats.json
    if os.path.exists("stats.json"):
        try:
            with open("stats.json", encoding="utf-8") as f:
                data = json.load(f)
            with _conn() as c:
                for entry in data.get("forms", []):
                    c.execute(
                        "INSERT OR IGNORE INTO form_stats (mod_id, form_type, status, ts) "
                        "VALUES (?, ?, ?, ?)",
                        (entry.get("mod_id", 0), entry.get("type", ""), entry.get("status", ""), entry.get("ts", 0)),
                    )
            os.rename("stats.json", "stats.json.bak")
        except Exception:
            pass
