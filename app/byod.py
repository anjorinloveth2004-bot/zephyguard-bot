"""BYOD: a group can point its own data (group memory, wiki) at its own Postgres database.

Bot-critical tables (groups, keys, dm_state, points, warnings, locks, etc.) ALWAYS stay on the main
DATABASE_URL. Only per-group content tables route to the group's own DB.

Lifecycle:
  connect  -> validate DSN, try a migration in a transaction that is ROLLED BACK, then store the DSN encrypted
  route    -> pool_for(chat) returns a capped, short-timeout Database for that group, or None if unavailable
  degrade  -> if the BYOD DB dies, the group drops to paused content features with a notice; the bot keeps running
Only enabled when BYOD_ENABLED=true (super-admin env).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import text

from app.crypto import KeyBox
from app.db import Database
from app.features import mask_dsn, validate_dsn

log = logging.getLogger(__name__)

BYOD_TABLES = ["group_memory", "wiki"]
TRIAL_SQL = [
    "CREATE TABLE IF NOT EXISTS zephy_byod_probe (id INTEGER PRIMARY KEY, v TEXT)",
    "INSERT INTO zephy_byod_probe (id, v) VALUES (1, 'ok')",
    "SELECT v FROM zephy_byod_probe WHERE id = 1",
]


async def trial_migrate(dsn: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Connect to the group's DB and run a trial in a transaction that is always rolled back.
    Returns (ok, message). Nothing is persisted on the group's database."""
    from sqlalchemy.ext.asyncio import create_async_engine
    url = dsn.replace("postgresql://", "postgresql+asyncpg://", 1) if dsn.startswith("postgresql://") else dsn
    try:
        eng = create_async_engine(url, pool_size=1, max_overflow=0, connect_args={"timeout": timeout, "command_timeout": 10})
    except Exception as exc:
        return False, f"couldn't open that database: {exc.__class__.__name__}"
    try:
        async with eng.connect() as conn:
            trans = await conn.begin()
            try:
                for stmt in TRIAL_SQL:
                    await conn.execute(text(stmt))
                await trans.rollback()
            except Exception as exc:
                await trans.rollback()
                return False, f"migration trial failed: {exc.__class__.__name__}. nothing saved."
        return True, "ok"
    except Exception as exc:
        return False, f"couldn't connect: {exc.__class__.__name__}. check the host, port and password."
    finally:
        await eng.dispose()


async def connect_group(db: Database, keys: KeyBox, chat_id: int, dsn: str, main_url: str, enabled: bool) -> tuple[bool, str]:
    if not enabled:
        return False, "bring-your-own-db is off on this bot right now."
    ok, msg = validate_dsn(dsn, main_url)
    if not ok:
        return False, msg
    ok, msg = await trial_migrate(dsn.strip())
    if not ok:
        return False, msg
    await db.update_group(chat_id, byod_dsn_enc=keys.encrypt(dsn.strip()), byod_hint=mask_dsn(dsn.strip()), byod_status="ok")
    POOLS.pop(chat_id, None)
    return True, f"connected. group data now lives in {mask_dsn(dsn.strip())}"


@dataclass
class GroupPool:
    url: str
    db: Database | None = None
    failures: int = 0
    last_fail: float = 0.0
    paused: bool = False
    notices: list = field(default_factory=list)


POOLS: dict[int, GroupPool] = {}
MAX_FAILURES = 3
COOLDOWN_S = 120


async def pool_for(db: Database, keys: KeyBox, chat_id: int, max_pool: int = 3) -> Database | None:
    """Return the group's BYOD database, or None so callers use the main DB or a paused feature."""
    grp = await db.get_group(chat_id)
    if not grp or not grp.get("byod_dsn_enc") or grp.get("byod_status") != "ok":
        return None
    entry = POOLS.get(chat_id)
    if entry and entry.paused and time.time() - entry.last_fail < COOLDOWN_S:
        return None
    if entry is None or entry.db is None:
        try:
            url = keys.decrypt(grp["byod_dsn_enc"])
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            d = Database(url, pool_size=max_pool, connect_timeout=5)
            entry = GroupPool(url=url, db=d)
            POOLS[chat_id] = entry
        except Exception as exc:
            log.warning("byod pool for %s failed to start: %s", chat_id, exc.__class__.__name__)
            return None
    return entry.db


async def report_failure(chat_id: int, bot, exc: Exception) -> None:
    """Called when a BYOD query fails. After MAX_FAILURES the group's content features pause with one notice."""
    entry = POOLS.get(chat_id)
    if entry is None:
        return
    entry.failures += 1
    entry.last_fail = time.time()
    if entry.failures >= MAX_FAILURES and not entry.paused:
        entry.paused = True
        try:
            await bot.send_message(
                chat_id,
                "heads up: this group's own database isn't responding, so group memory and wiki are paused for now. "
                "the rest of the bot keeps working and this retries automatically.",
            )
        except Exception:
            pass


def reset_pool(chat_id: int) -> None:
    POOLS.pop(chat_id, None)
