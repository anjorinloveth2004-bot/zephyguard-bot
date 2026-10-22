"""DM screen for the fallback model chain: primary + fallback model, optional OpenAI-compatible base_url.

Flow (all in the admin's private chat):
  fbk:<chat>            -> screen showing current primary/fallback, with buttons
  fbk:<chat>:set        -> ask for the fallback model name (stored as the next DM state)
  fbk:<chat>:url        -> ask for a custom OpenAI-compatible base_url (https only)
  fbk:<chat>:key        -> ask for the fallback key (validated, encrypted, the message is deleted)
  fbk:<chat>:clear      -> remove the fallback entirely
Keys are never shown back; only a masked hint.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import Settings
from app.crypto import KeyBox
from app.db import Database
from app.llm import LLMError, validate_key
from app.services import looks_like_key, mask_key

router = Router(name="fallback")
router.callback_query.filter(F.data.startswith("fbk:"))

ALLOWED_BASE_HOSTS_HINT = "https:// only, e.g. https://api.groq.com/openai/v1 or http://localhost:11434/v1"


def valid_base_url(url: str) -> tuple[bool, str]:
    u = (url or "").strip()
    if not u or len(u) > 200:
        return False, "send one base url (up to 200 chars)"
    parts = urlsplit(u)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return False, "it must be an http(s) url. " + ALLOWED_BASE_HOSTS_HINT
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1"):
        return False, "plain http is only allowed for localhost. " + ALLOWED_BASE_HOSTS_HINT
    return True, u.rstrip("/")


def _screen(chat_id: int, grp: dict) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    has_fb = bool(grp.get("fallback_key_enc"))
    kb.button(text="✏️ set fallback model", callback_data=f"fbk:{chat_id}:set")
    kb.button(text="🌐 set custom base url", callback_data=f"fbk:{chat_id}:url")
    kb.button(text="🔑 set fallback key", callback_data=f"fbk:{chat_id}:key")
    if has_fb:
        kb.button(text="🗑 remove fallback", callback_data=f"fbk:{chat_id}:clear")
    kb.button(text="« back", callback_data=f"m:{chat_id}")
    kb.adjust(1)
    text = (
        "🔁 fallback model\n\n"
        f"primary: {grp.get('provider') or 'auto'} / {grp.get('model') or 'default'}\n"
        f"fallback: {(grp.get('fallback_provider') or 'none')} / {(grp.get('fallback_model') or 'default') if has_fb else 'none'}\n"
        f"base url: {grp.get('fallback_base_url') or 'provider default'}\n\n"
        "if the primary model errors, zephy tries the fallback. "
        "the fallback key is stored encrypted and never shown back."
    )
    return text, kb.as_markup()


async def show_fallback(cb: CallbackQuery, db: Database, chat_id: int) -> None:
    grp = await db.get_group(chat_id)
    text, kb = _screen(chat_id, grp)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.regex(r"^fbk:-?\d+(:[a-z]+)?$"))
async def fallback_cb(cb: CallbackQuery, bot: Bot, db: Database, cfg: Settings) -> None:
    parts = cb.data.split(":")
    chat_id = int(parts[1])
    action = parts[2] if len(parts) > 2 else ""
    from app.handlers.dm import _authorized_group
    if not await _authorized_group(bot, db, cfg, chat_id, cb.from_user.id):
        await cb.answer("not allowed", show_alert=True)
        return
    if action == "":
        await show_fallback(cb, db, chat_id)
        return
    if action == "clear":
        await db.update_group(
            chat_id, fallback_provider="", fallback_model="", fallback_key_enc="", fallback_base_url="",
        )
        await show_fallback(cb, db, chat_id)
        return
    prompts = {
        "set": ("fbk:model", "send the fallback model name, e.g. gemini-flash-latest or gpt-4o-mini."),
        "url": ("fbk:url", "send the base url for an openai-compatible endpoint. " + ALLOWED_BASE_HOSTS_HINT),
        "key": ("fbk:key", "send the fallback api key. it's checked, encrypted, and your message is deleted."),
    }
    if action not in prompts:
        await cb.answer("unknown option", show_alert=True)
        return
    state, prompt = prompts[action]
    await db.set_dm_state(cb.from_user.id, f"fbk:{state.split(':', 1)[1]}", chat_id=chat_id)
    await cb.message.answer(prompt)
    await cb.answer()


async def receive_fallback_input(
    message: Message, bot: Bot, db: Database, cfg: Settings, keys: KeyBox,
    uid: int, chat_id: int, step: str,
) -> None:
    raw = (message.text or "").strip()
    if step == "model":
        await db.update_group(chat_id, fallback_model=raw[:80].replace(" ", ""))
    elif step == "url":
        ok, val = valid_base_url(raw)
        if not ok:
            await message.answer(val)
            return
        await db.update_group(chat_id, fallback_base_url=val)
    elif step == "key":
        try:
            await message.delete()
        except Exception:
            pass
        if not looks_like_key(raw):
            await message.answer("that doesn't look like an api key. paste the key itself.")
            return
        provider_check = raw
        grp = await db.get_group(chat_id) or {}
        provider = "gemini" if raw.startswith(("AIza", "AQ.")) else "openai"
        model = cfg.default_gemini_model if provider == "gemini" else cfg.default_openai_model
        base = grp.get("fallback_base_url") or cfg.openai_base_url
        try:
            await validate_key(provider, raw, model, base)
        except LLMError as exc:
            await message.answer(f"fallback key test failed: {exc}. nothing saved.")
            return
        except Exception:
            await message.answer("couldn't reach the fallback provider to test the key. nothing saved.")
            return
        await db.update_group(
            chat_id, fallback_provider=provider, fallback_key_enc=keys.encrypt(raw),
        )
        await message.answer(f"fallback key saved: {mask_key(raw)}")
    await db.set_dm_state(uid, "")
    grp = await db.get_group(chat_id)
    text, kb = _screen(chat_id, grp)
    await message.answer(text, reply_markup=kb)
