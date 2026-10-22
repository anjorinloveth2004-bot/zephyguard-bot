"""V2 data access: memory, wiki, faqs, trivia, capsules, federation, shadow flags, appeals, spend.

Every query is parameterised standard SQL through Database helpers. Nothing here talks to Telegram.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from app.db import Database

# Feature toggles a group admin may flip from DM. Allowlist, never built from user input.
TOGGLE_FIELDS = {
    "ai_enabled", "mod_enabled", "ai_mod", "welcome_enabled", "captcha_enabled", "allow_links",
    "web_access", "edit_mode", "roast_on", "shadow_mod", "slow_mode", "slow_ai", "faq_on",
    "wiki_on", "memory_on", "twin_on", "appeals_on",
    "trivia_on",
}

TRIVIA_WINDOW_S = 600  # a round stays open for 10 minutes


def month_key(now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time(), tz=timezone.utc).strftime("%Y-%m")


def day_key(now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time(), tz=timezone.utc).strftime("%Y-%m-%d")


# ----------------------------------------------------------------- spend and heat
async def add_spend(db: Database, chat_id: int, cents: float) -> None:
    await db.exec(
        "INSERT INTO spend (chat_id, month, cents) VALUES (:c, :m, :x) "
        "ON CONFLICT (chat_id, month) DO UPDATE SET cents = spend.cents + :x",
        c=chat_id, m=month_key(), x=float(cents),
    )


async def get_spend(db: Database, chat_id: int) -> float:
    row = await db.one("SELECT cents FROM spend WHERE chat_id = :c AND month = :m", c=chat_id, m=month_key())
    return float(row["cents"]) if row else 0.0


async def bump_heat(db: Database, chat_id: int, hour: int) -> None:
    await db.exec(
        "INSERT INTO ai_heat (chat_id, day, hour, calls) VALUES (:c, :d, :h, 1) "
        "ON CONFLICT (chat_id, day, hour) DO UPDATE SET calls = ai_heat.calls + 1",
        c=chat_id, d=day_key(), h=hour,
    )


async def heat_counts(db: Database, chat_id: int | None = None) -> dict[int, int]:
    if chat_id is None:
        rows = await db.all("SELECT hour, SUM(calls) AS n FROM ai_heat GROUP BY hour")
    else:
        rows = await db.all("SELECT hour, SUM(calls) AS n FROM ai_heat WHERE chat_id = :c GROUP BY hour", c=chat_id)
    return {int(r["hour"]): int(r["n"]) for r in rows}


# ----------------------------------------------------------------- group memory
async def add_memory(db: Database, chat_id: int, kind: str, body: str) -> None:
    await db.exec(
        "INSERT INTO group_memory (chat_id, kind, body, ts) VALUES (:c, :k, :b, :t)",
        c=chat_id, k=kind.upper(), b=body[:300], t=int(time.time()),
    )


async def list_memory(db: Database, chat_id: int, limit: int = 40) -> list[dict]:
    return await db.all(
        "SELECT id, kind, body, pinned FROM group_memory WHERE chat_id = :c "
        "ORDER BY pinned DESC, id DESC LIMIT :n",
        c=chat_id, n=limit,
    )


async def forget_memory(db: Database, chat_id: int, mem_id: int) -> None:
    await db.exec("DELETE FROM group_memory WHERE chat_id = :c AND id = :i", c=chat_id, i=mem_id)


async def clear_memory(db: Database, chat_id: int) -> None:
    await db.exec("DELETE FROM group_memory WHERE chat_id = :c", c=chat_id)


async def recall_block(db: Database, chat_id: int, limit: int = 12) -> str:
    """Short text block of what the group has agreed on, for prompts. Empty if nothing stored."""
    rows = await list_memory(db, chat_id, limit)
    if not rows:
        return ""
    return "\n".join(f"{r['kind']}: {r['body']}" for r in rows)


async def slang_tones(db: Database, chat_id: int, limit: int = 8) -> list[str]:
    rows = await db.all(
        "SELECT body FROM group_memory WHERE chat_id = :c AND kind = 'SLANG' ORDER BY id DESC LIMIT :n",
        c=chat_id, n=limit,
    )
    return [r["body"] for r in rows]


# ----------------------------------------------------------------- wiki
async def set_wiki(db: Database, chat_id: int, slug: str, body: str) -> None:
    await db.exec(
        "DELETE FROM wiki WHERE chat_id = :c AND slug = :s", c=chat_id, s=slug,
    )
    await db.exec(
        "INSERT INTO wiki (chat_id, slug, body) VALUES (:c, :s, :b)", c=chat_id, s=slug, b=body[:3000],
    )


async def get_wiki(db: Database, chat_id: int, slug: str) -> str | None:
    row = await db.one("SELECT body FROM wiki WHERE chat_id = :c AND slug = :s", c=chat_id, s=slug)
    return row["body"] if row else None


async def list_wiki(db: Database, chat_id: int) -> list[str]:
    rows = await db.all("SELECT slug FROM wiki WHERE chat_id = :c ORDER BY slug", c=chat_id)
    return [r["slug"] for r in rows]


# ----------------------------------------------------------------- faqs
async def add_faq(db: Database, chat_id: int, question: str, answer: str) -> None:
    await db.exec(
        "INSERT INTO faqs (chat_id, question, answer) VALUES (:c, :q, :a)",
        c=chat_id, q=question[:300], a=answer[:1000],
    )


async def list_faqs(db: Database, chat_id: int) -> list[dict]:
    return await db.all("SELECT id, question, answer, hits FROM faqs WHERE chat_id = :c ORDER BY id", c=chat_id)


async def bump_faq(db: Database, faq_id: int) -> None:
    await db.exec("UPDATE faqs SET hits = hits + 1 WHERE id = :i", i=faq_id)


# ----------------------------------------------------------------- trivia
async def open_trivia(db: Database, chat_id: int, question: str, answer: str, now: float | None = None) -> None:
    # One open round per chat: close any earlier open round first.
    await db.exec(
        "UPDATE trivia_rounds SET status = 'closed' WHERE chat_id = :c AND status = 'open'", c=chat_id,
    )
    await db.exec(
        "INSERT INTO trivia_rounds (chat_id, question, answer, status, created_ts) "
        "VALUES (:c, :q, :a, 'open', :t)",
        c=chat_id, q=question[:300], a=answer[:120], t=int(now or time.time()),
    )


async def current_trivia(db: Database, chat_id: int) -> dict | None:
    return await db.one(
        "SELECT id, question, answer, created_ts FROM trivia_rounds WHERE chat_id = :c AND status = 'open' "
        "ORDER BY id DESC LIMIT 1",
        c=chat_id,
    )


async def claim_trivia(db: Database, round_id: int, user_id: int) -> bool:
    """Atomically mark the round won. Only the first correct answer wins."""
    res = await db.one(
        "UPDATE trivia_rounds SET status = 'won', winner_id = :u WHERE id = :i AND status = 'open' "
        "RETURNING id",
        u=user_id, i=round_id,
    )
    return res is not None


async def expire_trivia(db: Database, now: float | None = None) -> list[dict]:
    cutoff = int(now or time.time()) - TRIVIA_WINDOW_S
    rows = await db.all(
        "SELECT id, chat_id, question, answer FROM trivia_rounds WHERE status = 'open' AND created_ts < :t",
        t=cutoff,
    )
    for r in rows:
        await db.exec("UPDATE trivia_rounds SET status = 'expired' WHERE id = :i AND status = 'open'", i=r["id"])
    return rows


# ----------------------------------------------------------------- capsules
async def add_capsule(db: Database, chat_id: int, user_id: int, body: str, reveal_ts: int) -> None:
    await db.exec(
        "INSERT INTO capsules (chat_id, user_id, body, reveal_ts, status) VALUES (:c, :u, :b, :r, 'pending')",
        c=chat_id, u=user_id, b=body[:1000], r=reveal_ts,
    )


async def due_capsules(db: Database, now: float | None = None) -> list[dict]:
    return await db.all(
        "SELECT id, chat_id, user_id, body FROM capsules WHERE status = 'pending' AND reveal_ts <= :t",
        t=int(now or time.time()),
    )


async def mark_capsule(db: Database, capsule_id: int, status: str) -> None:
    await db.exec("UPDATE capsules SET status = :s WHERE id = :i", s=status, i=capsule_id)


# ----------------------------------------------------------------- federation
async def fed_join(db: Database, fed_id: str, chat_id: int) -> None:
    await db.exec(
        "INSERT INTO federation (fed_id, chat_id) VALUES (:f, :c) ON CONFLICT (fed_id, chat_id) DO NOTHING",
        f=fed_id, c=chat_id,
    )
    await db.exec("UPDATE groups SET federation_id = :f WHERE chat_id = :c", f=fed_id, c=chat_id)


async def fed_leave(db: Database, chat_id: int) -> None:
    grp = await db.get_group(chat_id)
    if grp and grp.get("federation_id"):
        await db.exec("DELETE FROM federation WHERE fed_id = :f AND chat_id = :c", f=grp["federation_id"], c=chat_id)
    await db.exec("UPDATE groups SET federation_id = '' WHERE chat_id = :c", c=chat_id)


async def fed_members(db: Database, fed_id: str) -> list[int]:
    rows = await db.all("SELECT chat_id FROM federation WHERE fed_id = :f", f=fed_id)
    return [int(r["chat_id"]) for r in rows]


# ----------------------------------------------------------------- shadow mod flags
async def add_flag(db: Database, chat_id: int, user_id: int, user_name: str, text: str, reason: str, msg_id: int) -> int:
    await db.exec(
        "INSERT INTO shadow_flags (chat_id, user_id, user_name, text, reason, msg_id, ts) "
        "VALUES (:c, :u, :n, :t, :r, :m, :ts)",
        c=chat_id, u=user_id, n=user_name[:60], t=text[:500], r=reason[:200], m=msg_id, ts=int(time.time()),
    )
    row = await db.one(
        "SELECT id FROM shadow_flags WHERE chat_id = :c ORDER BY id DESC LIMIT 1", c=chat_id,
    )
    return int(row["id"]) if row else 0


async def open_flags(db: Database, chat_id: int, limit: int = 10) -> list[dict]:
    return await db.all(
        "SELECT id, user_id, user_name, text, reason FROM shadow_flags WHERE chat_id = :c AND status = 'open' "
        "ORDER BY id LIMIT :n",
        c=chat_id, n=limit,
    )


async def get_flag(db: Database, flag_id: int) -> dict | None:
    return await db.one("SELECT * FROM shadow_flags WHERE id = :i", i=flag_id)


async def set_flag_status(db: Database, flag_id: int, status: str) -> None:
    await db.exec("UPDATE shadow_flags SET status = :s WHERE id = :i", s=status, i=flag_id)


# ----------------------------------------------------------------- appeals
async def add_appeal(db: Database, chat_id: int, user_id: int, user_name: str, text: str) -> int:
    await db.exec(
        "INSERT INTO appeals (chat_id, user_id, user_name, text, ts) VALUES (:c, :u, :n, :t, :ts)",
        c=chat_id, u=user_id, n=user_name[:60], t=text[:1000], ts=int(time.time()),
    )
    row = await db.one(
        "SELECT id FROM appeals WHERE chat_id = :c AND user_id = :u ORDER BY id DESC LIMIT 1",
        c=chat_id, u=user_id,
    )
    return int(row["id"]) if row else 0


async def get_appeal(db: Database, appeal_id: int) -> dict | None:
    return await db.one("SELECT * FROM appeals WHERE id = :i", i=appeal_id)


async def set_appeal_draft(db: Database, appeal_id: int, draft: str) -> None:
    await db.exec("UPDATE appeals SET draft = :d WHERE id = :i", d=draft[:1000], i=appeal_id)


async def set_appeal_status(db: Database, appeal_id: int, status: str) -> None:
    await db.exec("UPDATE appeals SET status = :s WHERE id = :i", s=status, i=appeal_id)


async def open_appeals(db: Database, chat_id: int, limit: int = 10) -> list[dict]:
    return await db.all(
        "SELECT id, user_id, user_name, text, draft FROM appeals WHERE chat_id = :c AND status = 'open' "
        "ORDER BY id LIMIT :n",
        c=chat_id, n=limit,
    )


def fmt_ts(ts: int, tz_name: str) -> str:
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d %H:%M %Z")

