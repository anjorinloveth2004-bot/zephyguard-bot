"""Moderation primitives: warns, punishments, flood control, blacklist/link checks, AI mod, and
the captcha-on-join flow. Handlers in group.py call into these helpers."""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from aiogram import Bot
from aiogram.types import ChatPermissions, Message

from app.crypto import KeyBox
from app.db import Database
from app.config import Settings
from app.llm import LLMError, complete
from app.services import blacklist_hit, has_link, record_ai_call, resolve_llm

log = logging.getLogger(__name__)

# In-memory flood tracking: chat_id -> user_id -> recent message timestamps.
_flood: dict[int, dict[int, deque]] = defaultdict(lambda: defaultdict(lambda: deque(maxlen=20)))

FLOOD_WINDOW_S = 5
FLOOD_MAX_MSGS = 6


def is_flooding(chat_id: int, user_id: int, limit: int = FLOOD_MAX_MSGS, now: float | None = None) -> bool:
    """True when the user sent more than `limit` messages inside FLOOD_WINDOW_S seconds."""
    now = time.time() if now is None else now
    q = _flood[chat_id][user_id]
    q.append(now)
    recent = [t for t in q if now - t <= FLOOD_WINDOW_S]
    return len(recent) > limit


async def punish(bot: Bot, chat_id: int, user_id: int, action: str) -> None:
    """Apply ban / kick / mute. Kick = ban then unban, so they can rejoin."""
    if action == "ban":
        await bot.ban_chat_member(chat_id, user_id)
    elif action == "kick":
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    elif action == "mute":
        await bot.restrict_chat_member(
            chat_id, user_id, ChatPermissions(can_send_messages=False), until_date=int(time.time()) + 3600
        )


async def log_event(bot: Bot, db: Database, chat_id: int, text: str) -> None:
    """Post to the group's log channel, if one is set. Never raises."""
    try:
        grp = await db.get_group(chat_id)
        target = int(grp.get("log_chat_id") or 0) if grp else 0
        if target:
            await bot.send_message(target, f"📝 {text}")
    except Exception as exc:
        log.info("log channel post failed for %s: %s", chat_id, exc)


async def add_warn(db: Database, chat_id: int, user_id: int) -> int:
    await db.exec(
        "INSERT INTO warns (chat_id, user_id, count) VALUES (:c, :u, 1) "
        "ON CONFLICT (chat_id, user_id) DO UPDATE SET count = warns.count + 1",
        c=chat_id, u=user_id,
    )
    row = await db.one("SELECT count FROM warns WHERE chat_id = :c AND user_id = :u", c=chat_id, u=user_id)
    return int(row["count"]) if row else 0


async def warn_user(bot: Bot, db: Database, chat_id: int, user_id: int, name: str, reason: str) -> str:
    """Add a warning; if the group limit is reached, apply the group's warn action and reset."""
    grp = await db.get_group(chat_id)
    limit = int(grp["warn_limit"]) if grp else 3
    action = grp["warn_action"] if grp else "ban"
    count = await add_warn(db, chat_id, user_id)
    if count >= limit:
        await punish(bot, chat_id, user_id, action)
        await db.exec("DELETE FROM warns WHERE chat_id = :c AND user_id = :u", c=chat_id, u=user_id)
        return f"⛔ {name} hit {limit}/{limit} warnings ({reason}). action: {action}."
    return f"⚠️ {name} warned ({count}/{limit}): {reason}"


async def moderate_message(
    bot: Bot, db: Database, cfg: Settings, keys: KeyBox, msg: Message, grp: dict, is_admin: bool
) -> bool:
    """Run automatic checks on a group message. Returns True if the message was removed."""
    text = msg.text or msg.caption or ""
    if is_admin or msg.from_user is None:
        return False
    uid = msg.from_user.id
    name = msg.from_user.first_name or "member"
    cid = msg.chat.id

    if not grp["mod_enabled"]:
        return False

    reason = None
    flood_limit = int(grp.get("antiflood_limit") or 0)
    if flood_limit and is_flooding(cid, uid, flood_limit):
        from app.i18n import t
        action = grp.get("antiflood_action") or "warn"
        try:
            await msg.delete()
        except Exception:
            pass
        if action == "warn":
            notice = await warn_user(bot, db, cid, uid, name, "flooding")
        else:
            await punish(bot, cid, uid, action)
            notice = t(grp.get("lang"), "flood", name=name, action=action)
        await log_event(bot, db, cid, f"antiflood {action}: {name}")
        try:
            await bot.send_message(cid, notice, message_thread_id=msg.message_thread_id if msg.is_topic_message else None)
        except Exception:
            pass
        return True
    if text and (hit := blacklist_hit(text, grp["blacklist"])):
        reason = f"blacklisted word ({hit})"
    elif text and (not grp["allow_links"] or _soft_locked(grp)) and has_link(text):
        reason = "links not allowed here"
    elif text and grp["ai_mod"] and len(text) >= 3:
        verdict = await _ai_classify(db, cfg, keys, cid, text)
        if verdict:
            reason = verdict

    if not reason:
        return False

    try:
        await msg.delete()
    except Exception as exc:  # message may already be gone or bot lacks rights
        log.info("could not delete message in %s: %s", cid, exc)
    if reason.startswith("blacklisted word") and (grp.get("blocklist_action") or "warn") != "warn":
        blk = grp.get("blocklist_action")
        if blk in ("mute", "kick", "ban"):
            await punish(bot, cid, uid, blk)
        await log_event(bot, db, cid, f"blocklist {blk}: {name}")
        try:
            await bot.send_message(cid, f"🚫 {name}: {reason}. action: {blk}.")
        except Exception:
            pass
        return True
    notice = await warn_user(bot, db, cid, uid, name, reason)
    await log_event(bot, db, cid, f"warn {name}: {reason}")
    try:
        await bot.send_message(cid, notice)
    except Exception:
        pass
    return True


def _soft_locked(grp: dict) -> bool:
    # Soft lock is applied by the scheduler; the flag on groups.lock_soft mirrors the active soft lock.
    return bool(grp.get("lock_soft"))


async def _ai_classify(db: Database, cfg: Settings, keys: KeyBox, chat_id: int, text: str) -> str | None:
    from app.ai import ask_ai  # local import keeps the guarded path the only route to the LLM
    res = await ask_ai(
        db, cfg, keys, chat_id,
        "You are a group moderator. Reply with exactly one word: REMOVE if the message is spam, "
        "scam, harassment, hate, or explicit sexual content; otherwise KEEP.",
        text[:1000], max_tokens=3,
    )
    if res.text is None:
        return None
    return "ai moderation: spam or abuse" if res.text.strip().upper().startswith("REMOVE") else None


# ------------------------------------------------------------------ captcha
async def start_captcha(bot: Bot, db: Database, chat_id: int, user_id: int, name: str) -> None:
    """Restrict a new member until they tap the button. Timeout is enforced by the scheduler."""
    await bot.restrict_chat_member(chat_id, user_id, ChatPermissions(can_send_messages=False))
    await db.exec(
        "INSERT INTO captchas (chat_id, user_id, created_ts) VALUES (:c, :u, :t) "
        "ON CONFLICT (chat_id, user_id) DO UPDATE SET created_ts = :t",
        c=chat_id, u=user_id, t=int(time.time()),
    )
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ i'm human", callback_data=f"cap:{chat_id}:{user_id}")]]
    )
    await bot.send_message(
        chat_id,
        f"welcome {name}! tap the button within 5 minutes to unlock chat (only you can tap it).",
        reply_markup=kb,
    )


async def pass_captcha(bot: Bot, db: Database, chat_id: int, user_id: int) -> None:
    await bot.restrict_chat_member(
        chat_id,
        user_id,
        ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
            can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True,
            can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True,
        ),
    )
    await db.exec("DELETE FROM captchas WHERE chat_id = :c AND user_id = :u", c=chat_id, u=user_id)
