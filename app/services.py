"""Business logic shared by handlers: AI routing, points, warns, lock windows, leaderboard rendering."""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings
from app.crypto import KeyBox
from app.db import Database
from app.llm import LLMTarget

FREE_TIER_NOTE = "this group is using the shared free tier (limited daily calls). admins can add their own key in dm."


# ---------------------------------------------------------------- AI routing
async def resolve_llm(db: Database, cfg: Settings, keys: KeyBox, chat_id: int) -> tuple[LLMTarget | None, str]:
    """Pick the LLM for a group.

    Priority: the group's own BYOK key -> operator pooled free tier (capped) -> nothing.
    Returns (target, note). note explains free-tier use or why there is no target.
    """
    grp = await db.get_group(chat_id)
    if grp and grp["key_enc"]:
        api_key = keys.decrypt(grp["key_enc"])
        base = cfg.openai_base_url if grp["provider"] == "openai" else ""
        return LLMTarget(provider=grp["provider"], model=grp["model"], api_key=api_key, base_url=base or cfg.openai_base_url), ""

    if cfg.pool_provider and cfg.pool_api_key:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if await db.pool_calls_today(today) >= cfg.pool_daily_calls:
            return None, "the shared free tier is used up for today. admins can connect their own key in dm."
        if await db.pool_group_calls_today(chat_id, today) >= cfg.pool_group_daily_calls:
            return None, "this group hit its daily free-tier limit. admins can connect their own key in dm."
        model = cfg.pool_model or (cfg.default_gemini_model if cfg.pool_provider == "gemini" else cfg.default_openai_model)
        base = cfg.openai_base_url
        return LLMTarget(provider=cfg.pool_provider, model=model, api_key=cfg.pool_api_key, base_url=base), FREE_TIER_NOTE

    return None, "no ai key is connected for this group. a group admin can connect one in the bot's dm."


async def record_ai_call(db: Database, cfg: Settings, chat_id: int, used_pool: bool) -> None:
    month = datetime.utcnow().strftime("%Y-%m")
    await db.bump_usage(chat_id, month)
    if used_pool:
        await db.bump_pool(chat_id, datetime.utcnow().strftime("%Y-%m-%d"))


def mask_key(key: str) -> str:
    """Show only the prefix and last 4 chars, e.g. sk-…abcd. Never the full key."""
    key = key.strip()
    if len(key) <= 8:
        return "••••"
    prefix = "sk-" if key.startswith("sk-") else key[:3]
    return f"{prefix}…{key[-4:]}"


KEY_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),   # OpenAI-style
    re.compile(r"AIza[0-9A-Za-z_.\-]{30,}"),  # Google/Gemini-style (classic)
    re.compile(r"AQ\.[A-Za-z0-9_.\-]{20,}"),  # Google/Gemini-style (new format)
]


def looks_like_key(text: str) -> bool:
    t = text.strip().strip("`" + '"') 
    t = re.sub(r"^[Bb]earer\s+", "", t).strip()
    # accept the key even if the paste carried stray punctuation around it
    m = re.search(r"(sk-[A-Za-z0-9_\-.]{16,}|AIza[0-9A-Za-z_.\-]{30,}|AQ\.[A-Za-z0-9_.\-]{20,})", t)
    return bool(m) and any(p.fullmatch(m.group(1).rstrip(".,"))
                            or p.fullmatch(m.group(1)) for p in KEY_PATTERNS)


# ---------------------------------------------------------------- points
def today_key(tz_name: str) -> str:
    return datetime.now(_tz(tz_name)).strftime("%Y-%m-%d")


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


async def earn_message_points(db: Database, chat_id: int, user_id: int, username: str, name: str) -> int:
    """Award per-message points with an hourly anti-farm cap. Returns points actually granted."""
    grp = await db.get_group(chat_id)
    if not grp or not grp["points_enabled"]:
        return 0
    per_msg = int(grp["points_per_msg"])
    cap = int(grp["points_hour_cap"])
    hour = datetime.utcnow().strftime("%Y-%m-%d %H")
    row = await db.one("SELECT hour_key, hour_pts FROM points WHERE chat_id = :c AND user_id = :u", c=chat_id, u=user_id)
    used = int(row["hour_pts"]) if row and row["hour_key"] == hour else 0
    grant = max(0, min(per_msg, cap - used))
    await db.exec(
        "INSERT INTO points (chat_id, user_id, username, name, score, hour_key, hour_pts) "
        "VALUES (:c, :u, :un, :n, :s, :h, :hp) "
        "ON CONFLICT (chat_id, user_id) DO UPDATE SET username = :un, name = :n, "
        "score = points.score + :s, "
        "hour_pts = CASE WHEN points.hour_key = :h THEN points.hour_pts + :s ELSE :s END, "
        "hour_key = :h",
        c=chat_id, u=user_id, un=username or "", n=name[:64], s=grant, h=hour, hp=grant,
    )
    return grant


async def award(db: Database, chat_id: int, user_id: int, delta: int, reason: str, username: str = "", name: str = "") -> int:
    await db.exec(
        "INSERT INTO points (chat_id, user_id, username, name, score) VALUES (:c, :u, :un, :n, :d) "
        "ON CONFLICT (chat_id, user_id) DO UPDATE SET score = points.score + :d",
        c=chat_id, u=user_id, un=username or "", n=name[:64], d=delta,
    )
    await db.exec(
        "INSERT INTO point_log (chat_id, user_id, delta, reason, ts) VALUES (:c, :u, :d, :r, :t)",
        c=chat_id, u=user_id, d=delta, r=reason[:200], t=int(time.time()),
    )
    row = await db.one("SELECT score FROM points WHERE chat_id = :c AND user_id = :u", c=chat_id, u=user_id)
    return int(row["score"]) if row else 0


async def maybe_reset_board(db: Database, chat_id: int, tz_name: str) -> bool:
    """Apply the group's reset policy (never / weekly / monthly). Returns True if a reset ran."""
    grp = await db.get_group(chat_id)
    if not grp or grp["reset_policy"] not in ("weekly", "monthly"):
        return False
    now = datetime.now(_tz(tz_name))
    key = now.strftime("%G-W%V") if grp["reset_policy"] == "weekly" else now.strftime("%Y-%m")
    if grp["last_reset_key"] == key:
        return False
    await db.exec("UPDATE points SET score = 0, hour_pts = 0 WHERE chat_id = :c", c=chat_id)
    await db.update_group(chat_id, last_reset_key=key)
    return True


async def leaderboard(db: Database, chat_id: int) -> list[dict]:
    grp = await db.get_group(chat_id)
    size = int(grp["lb_size"]) if grp else 10
    return await db.all(
        "SELECT user_id, username, name, score FROM points WHERE chat_id = :c AND score > 0 "
        "ORDER BY score DESC, name ASC LIMIT :n",
        c=chat_id, n=size,
    )


async def my_rank(db: Database, chat_id: int, user_id: int) -> tuple[int, int, int | None]:
    """Returns (score, rank or 0, rank_position). Nobody else's breakdown is exposed."""
    row = await db.one("SELECT score FROM points WHERE chat_id = :c AND user_id = :u", c=chat_id, u=user_id)
    score = int(row["score"]) if row else 0
    above = await db.one(
        "SELECT COUNT(*) AS n FROM points WHERE chat_id = :c AND score > :s", c=chat_id, s=score
    )
    rank = int(above["n"]) + 1 if score > 0 else 0
    return score, rank, rank or None


def render_board(rows: list[dict], title: str) -> str:
    if not rows:
        return f"🏆 {title}\n\nno points yet. start chatting!"
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 {title}", ""]
    for i, r in enumerate(rows, 1):
        badge = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{badge} {r['name'] or 'member'}: {r['score']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- lock windows
def parse_hm(hm: str) -> tuple[int, int]:
    h, m = hm.split(":")
    return int(h), int(m)


def in_daily_window(now_local: datetime, start_hm: str, end_hm: str) -> bool:
    """True if now is inside a daily window. Handles windows that cross midnight (10pm to 6am)."""
    sh, sm = parse_hm(start_hm)
    eh, em = parse_hm(end_hm)
    cur = now_local.hour * 60 + now_local.minute
    start = sh * 60 + sm
    end = eh * 60 + em
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def parse_lock_text(text: str) -> dict | None:
    """Parse 'daily 22:00 06:00' or 'once 2026-10-25 09:00 11:00'. Returns dict or None."""
    parts = text.lower().split()
    if len(parts) == 3 and parts[0] == "daily":
        try:
            parse_hm(parts[1]); parse_hm(parts[2])
        except ValueError:
            return None
        return {"kind": "daily", "start_hm": parts[1], "end_hm": parts[2]}
    if len(parts) == 4 and parts[0] == "once":
        try:
            s = datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            e = datetime.strptime(f"{parts[1]} {parts[3]}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        if e <= s:
            return None
        return {"kind": "once", "once_start": int(s.timestamp()), "once_end": int(e.timestamp())}
    return None


def lock_should_be_active(lock: dict, tz_name: str, now_ts: float | None = None) -> bool:
    now_ts = now_ts or time.time()
    if not lock["enabled"]:
        return False
    if lock["kind"] == "daily":
        return in_daily_window(datetime.fromtimestamp(now_ts, _tz(tz_name)), lock["start_hm"], lock["end_hm"])
    return int(lock["once_start"]) <= now_ts < int(lock["once_end"])


def lock_is_expired(lock: dict, now_ts: float | None = None) -> bool:
    now_ts = now_ts or time.time()
    return lock["kind"] == "once" and now_ts >= int(lock["once_end"])


# ---------------------------------------------------------------- moderation helpers
URL_RE = re.compile(r"(https?://|www\.|t\.me/|[a-z0-9-]+\.(com|net|org|io|xyz|ly|gg)\b)", re.I)


def has_link(text: str) -> bool:
    return bool(URL_RE.search(text or ""))


def blacklist_hit(text: str, blacklist_csv: str) -> str | None:
    if not blacklist_csv or not text:
        return None
    low = text.lower()
    for word in (w.strip().lower() for w in blacklist_csv.split(",")):
        if word and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", low):
            return word
    return None


def expand_welcome(template: str, name: str, group: str) -> str:
    return template.replace("{name}", name).replace("{group}", group)
