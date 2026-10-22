"""DM flow for BYOD: 'bring ur own db' button -> paste a Postgres DSN -> validated, trial-migrated, stored encrypted.

Only reachable when BYOD_ENABLED is on. The DSN message is deleted right after it is read. The group's
data (group memory, wiki) then lives in that database; bot-critical tables stay on the main DATABASE_URL.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from app import byod
from app.config import Settings
from app.crypto import KeyBox
from app.db import Database

router = Router(name="byod_dm")
router.callback_query.filter(F.data.regex(r"^byod:-?\d+$"))


@router.callback_query(F.data.regex(r"^byod:-?\d+$"))
async def byod_button(cb: CallbackQuery, bot: Bot, db: Database, cfg: Settings) -> None:
    chat_id = int(cb.data.split(":")[1])
    from app.handlers.dm import _authorized_group
    if not await _authorized_group(bot, db, cfg, chat_id, cb.from_user.id):
        await cb.answer("not allowed", show_alert=True)
        return
    if not cfg.byod_enabled:
        await cb.answer("bring-your-own-db is off on this bot right now", show_alert=True)
        return
    await db.set_dm_state(cb.from_user.id, "byod", chat_id)
    await cb.message.answer(
        "send your postgres connection string here in this chat (not in the group).\n"
        "format: postgresql://user:password@host:5432/dbname\n"
        "i'll test it, encrypt it, and delete your message right away. only a masked version is shown."
    )
    await cb.answer()


async def receive_dsn(message: Message, bot: Bot, db: Database, cfg: Settings, keys: KeyBox, uid: int, chat_id: int) -> None:
    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    ok, msg = await byod.connect_group(db, keys, chat_id, raw, cfg.database_url, enabled=cfg.byod_enabled)
    await db.set_dm_state(uid, "")
    await message.answer(msg if ok else f"{msg}")
