"""DM settings navigation: a back-button tree on top of the existing DM menu.

Callbacks: st:<chat_id>:<screen>  where screen is one of home | persona | preset:<name> | prompt | prompt_clear
Every screen has a back button to its parent. Persona presets and the custom system prompt are saved
through db.update_group, which only accepts allowlisted column names.
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import Settings
from app.crypto import KeyBox
from app.db import Database
from app.features import PERSONA_PRESETS

log = logging.getLogger(__name__)
router = Router(name="settings")
router.callback_query.filter(F.message.chat.type == "private")

PROMPT_MAX = 1200  # keep custom prompts short enough to stay cheap per call


def _kb(rows: list[tuple[str, str]], back: str | None = None, row_width: int = 1) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, data in rows:
        kb.button(text=text, callback_data=data)
    if back:
        kb.button(text="« back", callback_data=back)
    kb.adjust(row_width)
    return kb.as_markup()


def welcome_screen(chat_id: int, grp: dict) -> tuple[str, InlineKeyboardMarkup]:
    has_img = bool(grp.get("welcome_image_id"))
    mode = grp.get("welcome_mode") or "group"
    text = (
        "welcome image\n\n"
        f"image: {'set' if has_img else 'none'}\n"
        f"sent to: {'the group' if mode == 'group' else 'the new member in dm (falls back to group)'}\n\n"
        "send a photo here in dm to set it. the caption is the welcome text."
    )
    rows = [
        ("🖼 send to group" if mode != "group" else "✅ send to group", f"st:{chat_id}:wmode:group"),
        ("✉️ send in dm" if mode != "dm" else "✅ send in dm", f"st:{chat_id}:wmode:dm"),
        ("📷 set image (send a photo next)", f"st:{chat_id}:wimg_set"),
        ("🗑 remove image", f"st:{chat_id}:wimg_clear"),
    ]
    return text, _kb(rows, back=f"st:{chat_id}:home")


def persona_screen(chat_id: int, grp: dict) -> tuple[str, InlineKeyboardMarkup]:
    current = grp.get("personality") or "default"
    text = f"personality\n\ncurrent: {current[:160]}\n\npick a preset or write your own."
    rows = [(f"🎓 {name}", f"st:{chat_id}:preset:{name}") for name in PERSONA_PRESETS]
    rows.append(("✏️ custom prompt", f"st:{chat_id}:prompt"))
    return text, _kb(rows, back=f"st:{chat_id}:home")


def prompt_screen(chat_id: int, grp: dict) -> tuple[str, InlineKeyboardMarkup]:
    custom = grp.get("custom_prompt") or ""
    shown = custom[:300] + ("…" if len(custom) > 300 else "") if custom else "none set"
    text = (
        "custom system prompt\n\n"
        f"current: {shown}\n\n"
        f"send the new prompt in one message (max {PROMPT_MAX} chars). it is added to every ai reply."
    )
    rows = [("🗑 clear custom prompt", f"st:{chat_id}:prompt_clear")]
    return text, _kb(rows, back=f"st:{chat_id}:persona")


@router.callback_query(F.data.regex(r"^st:-?\d+:(persona|prompt|prompt_clear|preset:\w+|welcome|wmode:(group|dm)|wimg_clear|wimg_set)$"))
async def settings_nav(cb: CallbackQuery, db: Database) -> None:
    parts = cb.data.split(":")
    chat_id = int(parts[1])
    action = parts[2]
    grp = await db.get_group(chat_id)
    if not grp:
        await cb.answer("group not found", show_alert=True)
        return
    if action == "persona":
        text, kb = persona_screen(chat_id, grp)
    elif action == "prompt":
        await db.set_dm_state(cb.from_user.id, "prompt", chat_id)
        text, kb = prompt_screen(chat_id, grp)
    elif action == "prompt_clear":
        await db.update_group(chat_id, custom_prompt="")
        await db.set_dm_state(cb.from_user.id, "", chat_id)
        grp = await db.get_group(chat_id)
        text, kb = prompt_screen(chat_id, grp)
    elif action == "welcome":
        text, kb = welcome_screen(chat_id, grp)
    elif action.startswith("wmode:"):
        await db.update_group(chat_id, welcome_mode=action.split(":", 1)[1])
        grp = await db.get_group(chat_id)
        text, kb = welcome_screen(chat_id, grp)
    elif action == "wimg_set":
        await db.set_dm_state(cb.from_user.id, "welcome_img", chat_id)
        grp = await db.get_group(chat_id)
        text, kb = welcome_screen(chat_id, grp)
        text = "send the photo now (its caption becomes the welcome text).\n\n" + text
    elif action == "wimg_clear":
        await db.update_group(chat_id, welcome_image_id="")
        grp = await db.get_group(chat_id)
        text, kb = welcome_screen(chat_id, grp)
    elif action.startswith("preset:"):
        name = action.split(":", 1)[1]
        if name not in PERSONA_PRESETS:
            await cb.answer("unknown preset", show_alert=True)
            return
        await db.update_group(chat_id, personality=PERSONA_PRESETS[name][:300])
        grp = await db.get_group(chat_id)
        text, kb = persona_screen(chat_id, grp)
        text = f"preset {name} saved.\n\n" + text
    else:
        await cb.answer()
        return
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


async def receive_custom_prompt(message: Message, db: Database, chat_id: int, uid: int) -> None:
    """Called from dm.free_text when the state is 'prompt'."""
    text = (message.text or "").strip()
    if not text:
        await message.answer("send a non-empty prompt, or tap back.")
        return
    if len(text) > PROMPT_MAX:
        await message.answer(f"too long ({len(text)} chars). keep it under {PROMPT_MAX}.")
        return
    await db.update_group(chat_id, custom_prompt=text)
    await db.set_dm_state(uid, "", chat_id)
    await message.answer("custom prompt saved. it applies to the next ai reply.")


@router.message(F.photo, F.chat.type == "private")
async def welcome_photo(message: Message, db: Database) -> None:
    """Admin sends a photo in DM while on the welcome screen: store file_id and caption as the welcome."""
    uid = message.from_user.id
    st = await db.get_dm_state(uid)
    if st["state"] != "welcome_img" or not st["chat_id"]:
        return
    chat_id = int(st["chat_id"])
    file_id = message.photo[-1].file_id
    await db.update_group(chat_id, welcome_image_id=file_id)
    if message.caption:
        await db.update_group(chat_id, welcome_text=message.caption[:800])
    await db.set_dm_state(uid, "", chat_id)
    await message.answer("welcome image saved.")
