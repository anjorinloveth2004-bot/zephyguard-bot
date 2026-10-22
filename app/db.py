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
import time
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

log = logging.getLogger(__name__)

# Columns of `groups` that the code is allowed to update (allowlist, never built from user input).
GROUP_FIELDS = {
    "title", "banned", "ai_enabled", "mod_enabled", "ai_mod", "welcome_enabled",
    "captcha_enabled", "points_enabled", "lb_size", "points_per_msg", "points_hour_cap",
    "reset_policy", "warn_limit", "warn_action", "allow_links", "welcome_text",
    "blacklist", "timezone", "personality", "provider", "model", "key_hint", "key_enc",
    "lock_soft", "board_message_id", "last_reset_key", "web_access",
}

SCHEMA = [
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
        timezone TEXT NOT NULL DEFAULT 'UTC',
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
    def __init__(self, url: str):
        target, connect_args = _engine_args(url)
        if target.startswith("sqlite"):
            # Make sure the folder for the sqlite file exists (e.g. data/).
            path = target.split("///", 1)[-1]
            if "/" in path:
                import os
                os.makedirs(os.path.dirname(path), exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(
            target, pool_pre_ping=True, connect_args=connect_args
        )

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            for stmt in SCHEMA:
                await conn.execute(text(stmt))
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

    async def admins_count(self, chat_id: int) -> int:
        row = await self.one("SELECT COUNT(*) AS n FROM group_admins WHERE chat_id = :c", c=chat_id)
        return int(row["n"]) if row else 0

    async def remove_admin(self, chat_id: int, user_id: int) -> None:
        await self.exec("DELETE FROM group_admins WHERE chat_id = :c AND user_id = :u", c=chat_id, u=user_id)

    async def groups_admined_by(self, user_id: int) -> list[dict]:
        return await self.all(
            "SELECT g.chat_id, g.title FROM groups g "
            "JOIN group_admins a ON a.chat_id = g.chat_id "
            "WHERE a.user_id = :u AND g.banned = 0 ORDER BY g.title",
            u=user_id,
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
