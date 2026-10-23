"""Database layer: PostgreSQL by default (any provider via DATABASE_URL), SQLite for local dev.

Uses SQLAlchemy async Core with plain standard SQL (no provider-specific
extensions). Schema is created idempotently on startup.

Supported URL forms:
  postgresql://user:pass@host:5432/db              (Neon, Supabase, Railway, RDS, VPS...)
  postgres://...                                   (normalized to postgresql+asyncpg)
  sqlite+aiosqlite:///data/bot.db                  (local dev fallback)

Query params like ?sslmode=require are translated for asyncpg automatically.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

log = logging.getLogger(__name__)

# Columns of `groups` that the code is allowed to update (allowlist, never built from user input).
GROUP_FIELDS = {
    "welcome_image_id", "welcome_mode", "edit_mode", "fallback_provider", "fallback_model",
    "fallback_key_enc", "fallback_base_url", "spend_cap_cents",
    "trivia_on", "trivia_next_ts",
    "custom_prompt", "tone_note", "slow_threshold",
    "title", "banned", "ai_enabled", "mod_enabled", "ai_mod", "welcome_enabled",
    "captcha_enabled", "points_enabled", "lb_size", "points_per_msg", "points_hour_cap",
    "reset_policy", "warn_limit", "warn_action", "allow_links", "welcome_text",
    "blacklist", "timezone", "personality", "provider", "model", "key_hint", "key_enc",
    "lock_soft", "board_message_id", "last_reset_key", "web_access",
    "byod_dsn_enc", "byod_hint", "byod_status",
}

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS bot_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS groups (
        chat_id BIGINT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        banned INTEGER NOT NULL DEFAULT 0,
        ai_enabled INTEGER NOT NULL DEFAULT 1,
        mod_enabled INTEGER NOT NULL DEFAULT 1,
        ai_mod INTEGER NOT NULL DEFAULT 0,
        welcome_enabled INTEGER NOT NULL DEFAULT 1,
        captcha_enabled INTEGER NOT NULL DEFAULT 1,
        points_enabled INTEGER NOT NULL DEFAULT 0,
        lb_size INTEGER NOT NULL DEFAULT 10,
        points_per_msg INTEGER NOT NULL DEFAULT 1,
        points_hour_cap INTEGER NOT NULL DEFAULT 20,
        reset_policy TEXT NOT NULL DEFAULT 'never',
        warn_limit INTEGER NOT NULL DEFAULT 3,
        warn_action TEXT NOT NULL DEFAULT 'ban',
        allow_links INTEGER NOT NULL DEFAULT 1,
        welcome_text TEXT NOT NULL DEFAULT 'welcome {name} to {group}',
        blacklist TEXT NOT NULL DEFAULT '',
        timezone TEXT NOT NULL DEFAULT 'Africa/Lagos',
        personality TEXT NOT NULL DEFAULT 'helpful, concise, friendly',
        provider TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        key_hint TEXT NOT NULL DEFAULT '',
        key_enc TEXT NOT NULL DEFAULT '',
        lock_soft INTEGER NOT NULL DEFAULT 0,
        web_access INTEGER NOT NULL DEFAULT 0,
        board_message_id BIGINT,
        last_reset_key TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS group_admins (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        username TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (chat_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS dm_state (
        user_id BIGINT PRIMARY KEY,
        state TEXT NOT NULL DEFAULT '',
        chat_id BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS usage (
        chat_id BIGINT NOT NULL,
        month TEXT NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, month)
    )""",
    """CREATE TABLE IF NOT EXISTS pool_usage (
        day TEXT PRIMARY KEY,
        calls INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS pool_group_usage (
        chat_id BIGINT NOT NULL,
        day TEXT NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, day)
    )""",
    # One lock schedule per group. kind = 'daily' (start_hm/end_hm) or 'once' (once_start/once_end unix ts).
    """CREATE TABLE IF NOT EXISTS locks (
        chat_id BIGINT PRIMARY KEY,
        kind TEXT NOT NULL,
        start_hm TEXT NOT NULL DEFAULT '',
        end_hm TEXT NOT NULL DEFAULT '',
        once_start BIGINT NOT NULL DEFAULT 0,
        once_end BIGINT NOT NULL DEFAULT 0,
        soft INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        applied INTEGER NOT NULL DEFAULT 0,
        orig_perms TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS points (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        username TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        score INTEGER NOT NULL DEFAULT 0,
        hour_key TEXT NOT NULL DEFAULT '',
        hour_pts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS point_log (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        delta INTEGER NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        ts BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS warns (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS captchas (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        created_ts BIGINT NOT NULL,
        PRIMARY KEY (chat_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS group_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id BIGINT NOT NULL,
        kind TEXT NOT NULL,
        body TEXT NOT NULL,
        pinned INTEGER NOT NULL DEFAULT 0,
        ts BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS wiki (
        chat_id BIGINT NOT NULL,
        slug TEXT NOT NULL,
        body TEXT NOT NULL,
        PRIMARY KEY (chat_id, slug)
    )""",
    """CREATE TABLE IF NOT EXISTS faqs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id BIGINT NOT NULL,
        question TEXT NOT NULL,
        answer TEXT NOT NULL DEFAULT '',
        hits INTEGER NOT NULL DEFAULT 0,
        pinned INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS trivia_rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id BIGINT NOT NULL,
        question TEXT NOT NULL,
        answer TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        winner_id BIGINT NOT NULL DEFAULT 0,
        created_ts BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS capsules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        body TEXT NOT NULL,
        reveal_ts BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
    )""",
    """CREATE TABLE IF NOT EXISTS federation (
        fed_id TEXT NOT NULL,
        chat_id BIGINT NOT NULL,
        PRIMARY KEY (fed_id, chat_id)
    )""",
    """CREATE TABLE IF NOT EXISTS shadow_flags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        text TEXT NOT NULL,
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        ts BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS appeals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        text TEXT NOT NULL,
        draft TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open',
        ts BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS spend (
        chat_id BIGINT NOT NULL,
        month TEXT NOT NULL,
        cents DOUBLE PRECISION NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, month)
    )""",
    """CREATE TABLE IF NOT EXISTS ai_heat (
        chat_id BIGINT NOT NULL,
        day TEXT NOT NULL,
        hour INTEGER NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, day, hour)
    )""",
    """CREATE TABLE IF NOT EXISTS battles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL DEFAULT 'trivia',
        chat_a BIGINT NOT NULL,
        chat_b BIGINT NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'open',
        qi INTEGER NOT NULL DEFAULT 0,
        questions TEXT NOT NULL DEFAULT '[]',
        score_a INTEGER NOT NULL DEFAULT 0,
        score_b INTEGER NOT NULL DEFAULT 0,
        q_deadline BIGINT NOT NULL DEFAULT 0,
        deadline BIGINT NOT NULL DEFAULT 0,
        created_ts BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS battle_claims (
        battle_id BIGINT NOT NULL,
        qi INTEGER NOT NULL,
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        ts BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (battle_id, qi, chat_id)
    )""",
    """CREATE TABLE IF NOT EXISTS battle_votes (
        battle_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        option TEXT NOT NULL,
        PRIMARY KEY (battle_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS fed_invites (
        code TEXT PRIMARY KEY,
        fed_id TEXT NOT NULL,
        from_chat BIGINT NOT NULL,
        ts BIGINT NOT NULL DEFAULT 0
    )""",
]


def pg_compat(stmt: str, dialect: str) -> str:
    """Postgres has no AUTOINCREMENT keyword; SERIAL is the equivalent. SQLite keeps it."""
    if dialect == "postgresql":
        return stmt.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    return stmt


GROUP_V2_COLUMNS = [
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS edit_mode INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS roast_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS shadow_mod INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS slow_mode INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS slow_ai INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS faq_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS wiki_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS memory_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS twin_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS appeals_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS spend_cap_cents INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS persona_preset TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS system_prompt TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS welcome_image_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS welcome_mode TEXT NOT NULL DEFAULT 'group'",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS fallback_provider TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS fallback_model TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS fallback_key_enc TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS fallback_base_url TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS byod_dsn_enc TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS byod_hint TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS byod_status TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS custom_base_url TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS federation_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS byod_dsn_enc TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS byod_dsn_hint TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS byod_status TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS menu_message_id BIGINT",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS custom_prompt TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS tone_note TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS slow_threshold INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS trivia_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS trivia_next_ts INTEGER NOT NULL DEFAULT 0",
]


def normalize_database_url(url: str) -> str:
    """Turn a provider URL into the SQLAlchemy async form the app expects."""
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


def _engine_args(url: str) -> tuple[str, dict[str, Any]]:
    """Strip libpq-style query params that asyncpg does not accept, map them to connect_args."""
    connect_args: dict[str, Any] = {}
    if url.startswith("postgresql+asyncpg://"):
        parts = urlsplit(url)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        sslmode = params.pop("sslmode", None)
        if sslmode and sslmode != "disable":
            connect_args["ssl"] = True
        new_query = urlencode(params)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    return url, connect_args


class Database:
    def __init__(self, url: str, pool_size: int | None = None, connect_timeout: float | None = None):
        target, connect_args = _engine_args(url)
        extra: dict[str, Any] = {}
        if pool_size is not None:
            # Per-group BYOD pools: capped, short timeouts, never allowed to hang the bot.
            extra.update(pool_size=pool_size, max_overflow=0, pool_timeout=connect_timeout or 5)
            connect_args = {**connect_args, "timeout": connect_timeout or 5, "command_timeout": 10}
        if target.startswith("sqlite"):
            # Make sure the folder for the sqlite file exists (e.g. data/).
            path = target.split("///", 1)[-1]
            if "/" in path:
                import os
                os.makedirs(os.path.dirname(path), exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(
            target, pool_pre_ping=True, connect_args=connect_args, **extra
        )

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            dialect = self.engine.dialect.name
            for stmt in SCHEMA + V22_SCHEMA + V22B_SCHEMA:
                await conn.execute(text(pg_compat(stmt, dialect)))
        # V2 columns on `groups` and extra V2 table columns. Each runs in its own transaction
        # so one failure never aborts the rest. SQLite has no ADD COLUMN IF NOT EXISTS, so we
        # check PRAGMA table_info first.
        is_sqlite = self.engine.dialect.name == "sqlite"
        for col_sql in GROUP_V2_COLUMNS + V2_TABLE_COLUMNS + V22_COLUMNS + V22B_COLUMNS:
            try:
                if is_sqlite:
                    m = re.match(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+) (.*)", col_sql)
                    table, col, rest = m.group(1), m.group(2), m.group(3)
                    async with self.engine.connect() as conn:
                        cols = (await conn.execute(text(f"PRAGMA table_info({table})"))).mappings().all()
                    if any(c["name"] == col for c in cols):
                        continue
                    col_sql = f"ALTER TABLE {table} ADD COLUMN {col} {rest}"
                async with self.engine.begin() as conn:
                    await conn.execute(text(col_sql))
            except Exception as exc:
                log.info("v2 column migration skipped: %s", exc.__class__.__name__)
        log.info("database schema ready")

    async def close(self) -> None:
        await self.engine.dispose()

    # ---- tiny helpers (parameterised, standard SQL) -------------------------
    async def one(self, sql: str, **params: Any) -> dict | None:
        async with self.engine.connect() as conn:
            res = await conn.execute(text(sql), params)
            row = res.mappings().first()
            return dict(row) if row else None

    async def all(self, sql: str, **params: Any) -> list[dict]:
        async with self.engine.connect() as conn:
            res = await conn.execute(text(sql), params)
            return [dict(r) for r in res.mappings().all()]

    async def exec(self, sql: str, **params: Any) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text(sql), params)

    async def exec_many(self, statements: Iterable[tuple[str, dict]]) -> None:
        async with self.engine.begin() as conn:
            for sql, params in statements:
                await conn.execute(text(sql), params)

    # ---- domain helpers ----------------------------------------------------
    async def ensure_group(self, chat_id: int, title: str = "") -> dict:
        await self.exec(
            "INSERT INTO groups (chat_id, title, created_at) VALUES (:c, :t, :ts) "
            "ON CONFLICT (chat_id) DO NOTHING",
            c=chat_id, t=title[:200], ts=int(time.time()),
        )
        if title:
            await self.exec("UPDATE groups SET title = :t WHERE chat_id = :c", t=title[:200], c=chat_id)
        return await self.get_group(chat_id)

    async def get_group(self, chat_id: int) -> dict | None:
        return await self.one("SELECT * FROM groups WHERE chat_id = :c", c=chat_id)

    async def update_group(self, chat_id: int, **fields: Any) -> None:
        bad = set(fields) - GROUP_FIELDS
        if bad:
            raise ValueError(f"disallowed group fields: {bad}")
        if not fields:
            return
        assignments = ", ".join(f"{k} = :{k}" for k in fields)
        await self.exec(f"UPDATE groups SET {assignments} WHERE chat_id = :_chat", _chat=chat_id, **fields)

    async def upsert_admin(self, chat_id: int, user_id: int, username: str, name: str) -> None:
        await self.exec(
            "INSERT INTO group_admins (chat_id, user_id, username, name) VALUES (:c, :u, :un, :n) "
            "ON CONFLICT (chat_id, user_id) DO UPDATE SET username = :un, name = :n",
            c=chat_id, u=user_id, un=username or "", n=name or "",
        )

    async def remove_admin(self, chat_id: int, user_id: int) -> None:
        await self.exec("DELETE FROM group_admins WHERE chat_id = :c AND user_id = :u", c=chat_id, u=user_id)

    async def groups_admined_by(self, user_id: int) -> list[dict]:
        return await self.all(
            "SELECT g.chat_id, g.title FROM groups g "
            "JOIN group_admins a ON a.chat_id = g.chat_id "
            "WHERE a.user_id = :u AND g.banned = 0 ORDER BY g.title",
            u=user_id,
        )

    async def get_meta(self, key: str) -> str | None:
        row = await self.one("SELECT value FROM bot_meta WHERE key = :k", k=key)
        return (row or {}).get("value")

    async def set_meta(self, key: str, value: str) -> None:
        await self.exec(
            "INSERT INTO bot_meta (key, value) VALUES (:k, :v) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            k=key, v=value,
        )

    async def set_dm_state(self, user_id: int, state: str, chat_id: int = 0) -> None:
        await self.exec(
            "INSERT INTO dm_state (user_id, state, chat_id) VALUES (:u, :s, :c) "
            "ON CONFLICT (user_id) DO UPDATE SET state = :s, chat_id = :c",
            u=user_id, s=state, c=chat_id,
        )

    async def get_dm_state(self, user_id: int) -> dict:
        row = await self.one("SELECT state, chat_id FROM dm_state WHERE user_id = :u", u=user_id)
        return row or {"state": "", "chat_id": 0}

    async def bump_usage(self, chat_id: int, month: str) -> None:
        await self.exec(
            "INSERT INTO usage (chat_id, month, calls) VALUES (:c, :m, 1) "
            "ON CONFLICT (chat_id, month) DO UPDATE SET calls = usage.calls + 1",
            c=chat_id, m=month,
        )

    async def get_usage(self, chat_id: int, month: str) -> int:
        row = await self.one("SELECT calls FROM usage WHERE chat_id = :c AND month = :m", c=chat_id, m=month)
        return int(row["calls"]) if row else 0

    async def pool_calls_today(self, day: str) -> int:
        row = await self.one("SELECT calls FROM pool_usage WHERE day = :d", d=day)
        return int(row["calls"]) if row else 0

    async def pool_group_calls_today(self, chat_id: int, day: str) -> int:
        row = await self.one(
            "SELECT calls FROM pool_group_usage WHERE chat_id = :c AND day = :d", c=chat_id, d=day
        )
        return int(row["calls"]) if row else 0

    async def bump_pool(self, chat_id: int, day: str) -> None:
        await self.exec(
            "INSERT INTO pool_usage (day, calls) VALUES (:d, 1) "
            "ON CONFLICT (day) DO UPDATE SET calls = pool_usage.calls + 1",
            d=day,
        )
        await self.exec(
            "INSERT INTO pool_group_usage (chat_id, day, calls) VALUES (:c, :d, 1) "
            "ON CONFLICT (chat_id, day) DO UPDATE SET calls = pool_group_usage.calls + 1",
            c=chat_id, d=day,
        )


V2_TABLE_COLUMNS = [
    "ALTER TABLE shadow_flags ADD COLUMN IF NOT EXISTS msg_id BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE shadow_flags ADD COLUMN IF NOT EXISTS user_name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE appeals ADD COLUMN IF NOT EXISTS user_name TEXT NOT NULL DEFAULT ''",
]

GROUP_FIELDS |= {re.match(r"ALTER TABLE groups ADD COLUMN IF NOT EXISTS (\w+)", c).group(1) for c in GROUP_V2_COLUMNS}

# ---- v2.2 (Rose-parity moderation, notes, filters, pins, reports, languages) ----
V22_COLUMNS = [
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS antiflood_limit INTEGER NOT NULL DEFAULT 6",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS antiflood_action TEXT NOT NULL DEFAULT 'warn'",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS antiraid_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS antiraid_threshold INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS antiraid_window INTEGER NOT NULL DEFAULT 60",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS raid_until_ts BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS approval_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS antipin_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS reports_on INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS log_chat_id BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS lang TEXT NOT NULL DEFAULT 'en'",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS disabled_cmds TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS blocklist_action TEXT NOT NULL DEFAULT 'warn'",
]

V22_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS chat_filters (
        chat_id BIGINT NOT NULL,
        keyword TEXT NOT NULL,
        reply TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (chat_id, keyword)
    )""",
    """CREATE TABLE IF NOT EXISTS chat_notes (
        chat_id BIGINT NOT NULL,
        name TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (chat_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS pending_joins (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        created_ts BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    )""",
]

GROUP_FIELDS |= {re.match(r"ALTER TABLE groups ADD COLUMN IF NOT EXISTS (\w+)", c).group(1) for c in V22_COLUMNS}

# v2.2b: captcha depth, ban/clean/service/goodbye/log/notes/privacy/purge/pin/rules/topic/warn settings.
V22B_COLUMNS = [
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS captcha_mode TEXT NOT NULL DEFAULT 'button'",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS captcha_kick_secs INTEGER NOT NULL DEFAULT 300",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS captcha_text TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS captcha_rules INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS clean_commands TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS clean_service TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS anonadmin_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS adminerror_on INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS goodbye_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS goodbye_text TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS clean_goodbye INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS last_goodbye_id BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS disable_admin INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS disable_all INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS log_cats TEXT NOT NULL DEFAULT 'bans,mutes,warns,notes,settings,messages'",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS privatenotes_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS purge_confirm INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS purge_from_id BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS pin_loud INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS antichannelpin_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS dbanned_on INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS rules_text TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS rules_private INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS rules_button TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS action_topic INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS topic_default BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS warn_action_secs INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS warn_time INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE captchas ADD COLUMN IF NOT EXISTS captcha_ack INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE warns ADD COLUMN IF NOT EXISTS last_ts BIGINT NOT NULL DEFAULT 0",
]

V22B_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS approved_users (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        PRIMARY KEY (chat_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS timed_actions (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        kind TEXT NOT NULL,
        until_ts BIGINT NOT NULL,
        PRIMARY KEY (chat_id, user_id, kind)
    )""",
    """CREATE TABLE IF NOT EXISTS bot_bans (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        PRIMARY KEY (chat_id, user_id)
    )""",
]

GROUP_FIELDS |= {re.match(r"ALTER TABLE groups ADD COLUMN IF NOT EXISTS (\w+)", c).group(1) for c in V22B_COLUMNS if c.startswith("ALTER TABLE groups")}

