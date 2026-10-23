"""Background loop: applies scheduled lock windows, expires captchas, and resets the board.

Runs every 30 seconds. Lock state is stored per group so a restart never double-flips.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

from aiogram import Bot
from aiogram.types import ChatPermissions

from app.db import Database
from app.services import lock_is_expired, lock_should_be_active

log = logging.getLogger(__name__)

LOCKED_PERMS = ChatPermissions(can_send_messages=False)
SOFT_PERMS = ChatPermissions(
    can_send_messages=True, can_send_audios=False, can_send_documents=False, can_send_photos=False,
    can_send_videos=False, can_send_video_notes=False, can_send_voice_notes=False,
    can_send_polls=False, can_send_other_messages=False, can_add_web_page_previews=False,
)
UNLOCKED_PERMS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
    can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True,
    can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True,
)
CAPTCHA_TIMEOUT_S = 300


def _perms_dict(p: ChatPermissions) -> dict:
    return p.model_dump(exclude_none=True)


def _unlock_time_txt(lock: dict, tz_name: str) -> str:
    """Human unlock time in the group's timezone. Empty when it can't be worked out."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    if lock["kind"] == "daily":
        end = lock.get("end_hm") or ""
        return f"{end} daily ({tz_name})" if end else ""
    try:
        t = datetime.fromtimestamp(int(lock["once_end"]), tz)
        return t.strftime("%d %b %H:%M")
    except Exception:
        return ""


async def _apply_lock(bot: Bot, db: Database, lock: dict, group_tz: str) -> None:
    chat_id = lock["chat_id"]
    should = lock_should_be_active(lock, group_tz)
    expired = lock_is_expired(lock)
    if should and not lock["applied"]:
        # Remember the current permissions so we can restore them exactly.
        chat = await bot.get_chat(chat_id)
        orig = chat.permissions or UNLOCKED_PERMS
        target = SOFT_PERMS if lock["soft"] else LOCKED_PERMS
        await bot.set_chat_permissions(chat_id, target)
        await db.exec(
            "UPDATE locks SET applied = 1, orig_perms = :o WHERE chat_id = :c",
            o=json.dumps(_perms_dict(orig)), c=chat_id,
        )
        await db.update_group(chat_id, lock_soft=1 if lock["soft"] else 0)
        mode = "soft lock: media and links are blocked, text is open" if lock["soft"] else "full lock"
        unlock_txt = _unlock_time_txt(lock, group_tz)
        await bot.send_message(
            chat_id,
            f"🔒 group locked ({mode})." + (f" unlocks at {unlock_txt}." if unlock_txt else ""),
        )
    elif not should and lock["applied"]:
        orig = json.loads(lock["orig_perms"] or "{}")
        restore = ChatPermissions(**orig) if orig else UNLOCKED_PERMS
        await bot.set_chat_permissions(chat_id, restore)
        await db.exec("UPDATE locks SET applied = 0, orig_perms = '' WHERE chat_id = :c", c=chat_id)
        await db.update_group(chat_id, lock_soft=0)
        await bot.send_message(chat_id, "🔓 group unlocked. welcome back.")
        if expired:
            await db.exec("DELETE FROM locks WHERE chat_id = :c", c=chat_id)


async def _expire_captchas(bot: Bot, db: Database) -> None:
    cutoff = int(time.time()) - CAPTCHA_TIMEOUT_S
    rows = await db.all("SELECT chat_id, user_id FROM captchas WHERE created_ts < :t", t=cutoff)
    for r in rows:
        try:
            await bot.ban_chat_member(r["chat_id"], r["user_id"])
            await bot.unban_chat_member(r["chat_id"], r["user_id"], only_if_banned=True)
        except Exception as exc:
            log.info("captcha expiry failed for %s/%s: %s", r["chat_id"], r["user_id"], exc)
        await db.exec("DELETE FROM captchas WHERE chat_id = :c AND user_id = :u", c=r["chat_id"], u=r["user_id"])


TRIVIA_EVERY_S = 6 * 3600  # one timed round per group every six hours, only where trivia is on


async def _tick_trivia(bot: Bot, db: Database) -> None:
    """Open a timed trivia round in groups where trivia_on and no round is open."""
    from app import store
    from app.features import TRIVIA_BANK
    import random
    now = time.time()
    rows = await db.all("SELECT chat_id, trivia_next_ts FROM groups WHERE trivia_on = 1")
    for r in rows:
        if (r["trivia_next_ts"] or 0) > now:
            continue
        if await store.current_trivia(db, r["chat_id"]):
            continue
        question, answer = random.choice(TRIVIA_BANK)
        await store.open_trivia(db, r["chat_id"], question, answer, now)
        await db.update_group(r["chat_id"], trivia_next_ts=int(now + TRIVIA_EVERY_S))
        try:
            await bot.send_message(r["chat_id"], f"🧠 trivia time: {question}\nreply with your answer. first right answer wins points.")
        except Exception as exc:
            log.info("trivia post failed for %s: %s", r["chat_id"], exc)


async def run_scheduler(bot: Bot, db: Database, stop: asyncio.Event) -> None:
    """Forever loop. Errors are logged per item so one bad group never stops the others."""
    while not stop.is_set():
        try:
            locks = await db.all("SELECT * FROM locks WHERE enabled = 1")
            for lock in locks:
                grp = await db.get_group(lock["chat_id"])
                tz = grp["timezone"] if grp else "UTC"
                try:
                    await _apply_lock(bot, db, lock, tz)
                except Exception as exc:
                    log.warning("lock flip failed for %s: %s", lock["chat_id"], exc)
            await _expire_captchas(bot, db)
        except Exception as exc:
            log.exception("scheduler tick failed: %s", exc)
        try:
            await _tick_trivia(bot, db)
        except Exception as exc:
            log.exception("trivia tick failed: %s", exc)
        try:
            from app.handlers.mod_v2 import reveal_due_capsules
            await reveal_due_capsules(bot, db)
        except Exception as exc:
            log.exception("capsule tick failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
