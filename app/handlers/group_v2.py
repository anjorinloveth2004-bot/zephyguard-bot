"""Item 5: edit-in-place, image welcomes, auto-FAQ, and group twin tone steering.

- edit_mode (default on): the bot's own status messages (lock notice, menu, trivia reveal) are edited
  in place instead of posting a new message. We remember the last message id per (chat, kind).
- welcome image: groups set a photo via file_id; welcome_mode 'group' posts it to the group, 'dm' sends
  the same photo caption to the new member privately (only if they already started the bot).
- auto-FAQ: when faq_on is set, a group question that matches a saved FAQ is answered from the store.
- twin: when twin_on is set, the group's tone_note is learned from recent messages and steered into replies.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import Message

from app import store
from app.ai import ask_ai
from app.config import Settings
from app.crypto import KeyBox
from app.db import Database
from app.features import faq_best_match

log = logging.getLogger(__name__)

# (chat_id, kind) -> message_id, for edit-in-place. Bounded in size by kinds x groups in use.
_LAST_MSG: dict[tuple[int, str], int] = {}


async def send_or_edit(bot: Bot, grp: dict, chat_id: int, kind: str, text: str, **kw) -> int | None:
    """Post a bot status message, or edit the previous one of this kind when edit_mode is on."""
    if grp.get("edit_mode"):
        prev = _LAST_MSG.get((chat_id, kind))
        if prev:
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=prev, **kw)
                return prev
            except Exception:
                # message gone or too old to edit: fall through and post a fresh one
                pass
    sent = await bot.send_message(chat_id, text, **kw)
    _LAST_MSG[(chat_id, kind)] = sent.message_id
    return sent.message_id


async def send_welcome(bot: Bot, db: Database, grp: dict, chat_id: int, user_id: int, caption: str) -> None:
    """Send the welcome as a photo (file_id) when one is set; otherwise as text."""
    file_id = grp.get("welcome_image_id") or ""
    mode = grp.get("welcome_mode") or "group"
    if not file_id:
        await bot.send_message(chat_id, caption)
        return
    if mode == "dm":
        try:
            await bot.send_photo(user_id, file_id, caption=caption)
        except Exception as exc:
            # the member has not started the bot in DM yet; fall back to the group
            log.info("dm welcome not possible for %s: %s", user_id, exc)
            await bot.send_photo(chat_id, file_id, caption=caption)
        return
    await bot.send_photo(chat_id, file_id, caption=caption)


async def try_faq(db: Database, cfg: Settings, keys: KeyBox, msg: Message, grp: dict) -> bool:
    """Auto-answer a question from the saved FAQ. Returns True if it answered."""
    if not grp.get("faq_on") or not msg.text:
        return False
    faqs = await store.list_faqs(db, msg.chat.id)
    hit = faq_best_match(msg.text, faqs)
    if not hit:
        return False
    await store.bump_faq(db, hit["id"])
    await msg.reply(hit["answer"][:800], disable_web_page_preview=True)
    return True


async def learn_tone(db: Database, cfg: Settings, keys: KeyBox, chat_id: int) -> str:
    """Summarise the group's recent slang and tone into one short note, cached on the group row."""
    tones = await store.slang_tones(db, chat_id, limit=8)
    if not tones:
        return ""
    res = await ask_ai(
        db, cfg, keys, chat_id,
        system="describe this group's tone in under 12 words, lowercase, no names, no slurs.",
        user="\n".join(tones), max_tokens=40,
    )
    note = (res.text or "").strip().lower()[:160]
    if note:
        await db.update_group(chat_id, tone_note=note)
    return note
