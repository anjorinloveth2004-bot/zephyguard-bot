"""v2.2 data access: filters, notes, pending join approvals, report log, and export/import.

Every query is parameterised standard SQL through Database helpers. Nothing here talks to Telegram.
"""
from __future__ import annotations

import json
import re
import time

from app.db import Database

# Group settings that /export emits and /import accepts. Secrets (keys, DSNs) are never in this list.
EXPORT_FIELDS = (
    "welcome_text", "welcome_enabled", "captcha_enabled", "warn_limit", "warn_action", "allow_links",
    "blacklist", "blocklist_action", "antiflood_limit", "antiflood_action", "antiraid_on",
    "antiraid_threshold", "antiraid_window", "approval_on", "antipin_on", "reports_on",
    "lang", "disabled_cmds", "log_chat_id", "personality", "timezone",
)
EXPORT_VERSION = 1
FLOOD_ACTIONS = {"warn", "mute", "kick", "ban"}
LANG_CODES = {"en", "es", "fr", "pt", "hi", "ar"}
NOTE_NAME_RE = re.compile(r"^[a-z0-9_\-]{1,32}$")
CMD_RE = re.compile(r"^[a-z0-9_]{1,32}$")
APPROVAL_TIMEOUT_S = 600


def clean_note_name(raw: str) -> str | None:
    name = (raw or "").strip().lower().lstrip("#")
    return name if NOTE_NAME_RE.match(name) else None


def clean_command(raw: str) -> str | None:
    cmd = (raw or "").strip().lower().lstrip("/").split("@")[0]
    return cmd if CMD_RE.match(cmd) else None


def csv_set(value: str) -> set[str]:
    return {p.strip() for p in (value or "").split(",") if p.strip()}


def csv_join(items) -> str:
    return ",".join(sorted({i for i in items if i}))


# ----------------------------------------------------------------- filters (auto-reply)
async def set_filter(db: Database, chat_id: int, keyword: str, reply: str) -> None:
    await db.exec(
        "INSERT INTO chat_filters (chat_id, keyword, reply) VALUES (:c, :k, :r) "
        "ON CONFLICT (chat_id, keyword) DO UPDATE SET reply = :r",
        c=chat_id, k=keyword[:50], r=reply[:1000],
    )


async def del_filter(db: Database, chat_id: int, keyword: str) -> bool:
    row = await db.one("SELECT keyword FROM chat_filters WHERE chat_id = :c AND keyword = :k", c=chat_id, k=keyword)
    if not row:
        return False
    await db.exec("DELETE FROM chat_filters WHERE chat_id = :c AND keyword = :k", c=chat_id, k=keyword)
    return True


async def list_filters(db: Database, chat_id: int) -> list[dict]:
    return await db.all("SELECT keyword, reply FROM chat_filters WHERE chat_id = :c ORDER BY keyword", c=chat_id)


def filter_match(text: str, keyword: str) -> bool:
    """Whole-word, case-insensitive match. 'cat' does not match 'category'."""
    if not text or not keyword:
        return False
    return re.search(r"(?<!\w)" + re.escape(keyword.lower()) + r"(?!\w)", text.lower()) is not None


async def find_filter_reply(db: Database, chat_id: int, text: str) -> str | None:
    for row in await list_filters(db, chat_id):
        if filter_match(text, row["keyword"]):
            return row["reply"]
    return None


# ----------------------------------------------------------------- notes
async def save_note(db: Database, chat_id: int, name: str, body: str) -> None:
    await db.exec(
        "INSERT INTO chat_notes (chat_id, name, body) VALUES (:c, :n, :b) "
        "ON CONFLICT (chat_id, name) DO UPDATE SET body = :b",
        c=chat_id, n=name, b=body[:3500],
    )


async def get_note(db: Database, chat_id: int, name: str) -> str | None:
    row = await db.one("SELECT body FROM chat_notes WHERE chat_id = :c AND name = :n", c=chat_id, n=name)
    return row["body"] if row else None


async def del_note(db: Database, chat_id: int, name: str) -> bool:
    if await get_note(db, chat_id, name) is None:
        return False
    await db.exec("DELETE FROM chat_notes WHERE chat_id = :c AND name = :n", c=chat_id, n=name)
    return True


async def list_notes(db: Database, chat_id: int) -> list[str]:
    rows = await db.all("SELECT name FROM chat_notes WHERE chat_id = :c ORDER BY name", c=chat_id)
    return [r["name"] for r in rows]


# ----------------------------------------------------------------- join approval
async def add_pending_join(db: Database, chat_id: int, user_id: int, now: float | None = None) -> None:
    await db.exec(
        "INSERT INTO pending_joins (chat_id, user_id, created_ts) VALUES (:c, :u, :t) "
        "ON CONFLICT (chat_id, user_id) DO UPDATE SET created_ts = :t",
        c=chat_id, u=user_id, t=int(now or time.time()),
    )


async def remove_pending_join(db: Database, chat_id: int, user_id: int) -> bool:
    row = await db.one("SELECT user_id FROM pending_joins WHERE chat_id = :c AND user_id = :u", c=chat_id, u=user_id)
    if not row:
        return False
    await db.exec("DELETE FROM pending_joins WHERE chat_id = :c AND user_id = :u", c=chat_id, u=user_id)
    return True


async def expired_pending_joins(db: Database, now: float | None = None) -> list[dict]:
    cutoff = int((now or time.time()) - APPROVAL_TIMEOUT_S)
    return await db.all("SELECT chat_id, user_id FROM pending_joins WHERE created_ts < :t", t=cutoff)


# ----------------------------------------------------------------- export / import
async def export_settings(db: Database, chat_id: int) -> dict:
    grp = await db.get_group(chat_id) or {}
    data = {
        "version": EXPORT_VERSION,
        "settings": {k: grp.get(k) for k in EXPORT_FIELDS},
        "filters": [dict(r) for r in await list_filters(db, chat_id)],
        "notes": [{"name": n, "body": (await get_note(db, chat_id, n)) or ""} for n in await list_notes(db, chat_id)],
    }
    return data


def parse_import(raw: str) -> tuple[dict | None, str]:
    """Validate an export blob. Returns (clean_payload, error). Only whitelisted, typed fields survive."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None, "that isn't valid json."
    if not isinstance(data, dict) or data.get("version") != EXPORT_VERSION:
        return None, "unsupported export version."
    settings_in = data.get("settings") or {}
    if not isinstance(settings_in, dict):
        return None, "settings must be an object."
    clean: dict = {}
    for key in EXPORT_FIELDS:
        if key not in settings_in:
            continue
        val = settings_in[key]
        if key in ("welcome_text", "blacklist", "disabled_cmds", "personality", "timezone"):
            if isinstance(val, str):
                clean[key] = val[:2000]
        elif key == "lang":
            if isinstance(val, str) and val in LANG_CODES:
                clean[key] = val
        elif key == "antiflood_action":
            if val in FLOOD_ACTIONS:
                clean[key] = val
        elif key in ("warn_action", "blocklist_action"):
            if val in FLOOD_ACTIONS | {"delete"}:
                clean[key] = val
        elif key in ("warn_limit", "antiflood_limit", "antiraid_threshold", "antiraid_window", "log_chat_id"):
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                clean[key] = val
        elif isinstance(val, (bool, int)):
            clean[key] = int(val)
    filters = []
    for f in data.get("filters") or []:
        if isinstance(f, dict) and isinstance(f.get("keyword"), str) and isinstance(f.get("reply"), str):
            kw = f["keyword"].strip().lower()[:50]
            if kw:
                filters.append((kw, f["reply"][:1000]))
    notes = []
    for n in data.get("notes") or []:
        if isinstance(n, dict) and clean_note_name(str(n.get("name", ""))) and isinstance(n.get("body"), str):
            notes.append((clean_note_name(n["name"]), n["body"][:3500]))
    return {"settings": clean, "filters": filters, "notes": notes}, ""


async def import_settings(db: Database, chat_id: int, payload: dict) -> int:
    if payload["settings"]:
        await db.update_group(chat_id, **payload["settings"])
    for kw, reply in payload["filters"]:
        await set_filter(db, chat_id, kw, reply)
    for name, body in payload["notes"]:
        await save_note(db, chat_id, name, body)
    return len(payload["settings"])
