"""Private-chat (DM) handlers: the only place settings and API keys are touched.

Flow: admin opens the bot in DM (via the 'settings' deep link from a group, or /start),
picks a group, and manages it from the inline menu. API keys are typed or pasted here,
validated with one tiny call, encrypted, and the admin's message is deleted right away.
"""
from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import Settings
from app.crypto import KeyBox
from app.db import Database
from app.llm import LLMError, validate_key
from app.services import mask_key, looks_like_key, parse_lock_text

log = logging.getLogger(__name__)
router = Router(name="dm")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

NOT_ADMIN_TEXT = "you're not an admin of any group this bot is in. add the bot to your group and make yourself admin, then come back here."


async def is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return m.status in ("creator", "administrator")


async def _authorized_group(bot: Bot, db: Database, cfg: Settings, chat_id: int, user_id: int) -> bool:
    if user_id in cfg.super_admin_ids:
        return True
    return await is_group_admin(bot, chat_id, user_id)


def _main_menu(chat_id: int, grp: dict, byod_enabled: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    has_key = bool(grp["key_enc"])
    kb.button(text=("🔁 replace api key" if has_key else "🔑 connect api key"), callback_data=f"k:{chat_id}:connect")
    if has_key:
        kb.button(text="🗑 disconnect key", callback_data=f"k:{chat_id}:disc")
    kb.button(text="📊 usage this month", callback_data=f"u:{chat_id}")
    kb.button(text="🎭 personality", callback_data=f"pers:{chat_id}")
    kb.button(text="🧠 model", callback_data=f"mdl:{chat_id}")
    kb.button(text="🔁 fallback model", callback_data=f"fbk:{chat_id}")
    kb.button(text="🧩 features", callback_data=f"feat:{chat_id}")
    kb.button(text="🌍 timezone", callback_data=f"tz:{chat_id}")
    kb.button(text="🔒 scheduled lock", callback_data=f"lk:{chat_id}")
    kb.button(text="🏆 points board", callback_data=f"pts:{chat_id}")
    kb.button(text="👮 refresh admins", callback_data=f"adm:{chat_id}")
    kb.button(text="🛡 moderation tools", callback_data=f"rz:{chat_id}")
    if byod_enabled:
        kb.button(text="🗄 bring ur own db", callback_data=f"byod:{chat_id}")
    kb.adjust(1, 1, 1, 2, 1, 1, 1, 1, 1, 1)
    text = (
        f"⚙️ settings: {grp['title'] or chat_id}\n\n"
        f"ai key: {('connected ' + grp['provider'] + ' (' + grp['key_hint'] + ')') if has_key else 'not connected (free tier if available)'}\n"
        f"model: {grp['model'] or 'default'}"
    )
    return text, kb.as_markup()


async def _edit_or_send(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


async def _show_menu(cb_or_msg, db: Database, chat_id: int, byod_enabled: bool | None = None) -> None:
    grp = await db.get_group(chat_id)
    if not grp:
        return
    if byod_enabled is None:
        from app.config import load_settings
        byod_enabled = load_settings().byod_enabled
    text, kb = _main_menu(chat_id, grp, byod_enabled=byod_enabled)
    if isinstance(cb_or_msg, CallbackQuery):
        await _edit_or_send(cb_or_msg, text, kb)
    else:
        await cb_or_msg.answer(text, reply_markup=kb)


# ------------------------------------------------------------------ entry points
@router.message(CommandStart())
async def start(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    uid = message.from_user.id
    # Deep link from a group: /start g-100123 (the group chat id).
    if command.args and command.args.startswith("g-"):
        try:
            chat_id = int(command.args[1:])
        except ValueError:
            chat_id = 0
        if chat_id and await _authorized_group(bot, db, cfg, chat_id, uid):
            await _show_menu(message, db, chat_id)
            return
        await message.answer(NOT_ADMIN_TEXT)
        return
    await _home(message, bot, db, cfg)


@router.message(Command("settings"))
async def settings_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    await _home(message, bot, db, cfg)


async def _home(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    uid = message.from_user.id
    groups = await db.groups_admined_by(uid)
    if not groups:
        me = await bot.me()
        news = await db.get_meta("news_channel")
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ add me to your chat", url=f"https://t.me/{me.username}?startgroup=new")
        if news:
            kb.button(text="📰 news channel", url=news)
        kb.adjust(1)
        await message.answer(
            "hey there! i'm zephy - i help you manage your groups: moderation, points, locks, welcomes and optional ai.\n\n"
            "use /help to see everything i can do.\n\n"
            + (f"join my <a href=\"{news}\">news channel</a> for updates.\n\n" if news else "")
            + "check /privacy to see the privacy policy and how i handle your data.\n\n"
            "ai is optional: connect a key later from a group's settings if u want it."
        )
        await message.answer("pick a button below 👇", reply_markup=kb.as_markup())
        if uid in cfg.super_admin_ids:
            await message.answer("boss tools: /stats, /groups, /disconnect <chat_id>, /banGroup <chat_id>, /unbanGroup <chat_id>, /setnews <url>")
        return
    kb = InlineKeyboardBuilder()
    for g in groups:
        kb.button(text=(g["title"] or str(g["chat_id"]))[:40], callback_data=f"m:{g['chat_id']}")
    kb.adjust(1)
    await message.answer(
        "ur connected groups:\n(pick one for settings. ai key is optional, everything else works without it)"
        if uid not in cfg.super_admin_ids
        else "ur connected groups: (boss tools: /stats, /groups, /banGroup, /unbanGroup)",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("m:"))
async def open_menu(cb: CallbackQuery, bot: Bot, db: Database, cfg: Settings) -> None:
    chat_id = int(cb.data.split(":")[1])
    if not await _authorized_group(bot, db, cfg, chat_id, cb.from_user.id):
        await cb.answer("you're not an admin there", show_alert=True)
        return
    await _show_menu(cb, db, chat_id)


# ------------------------------------------------------------------ API key (BYOK)
@router.callback_query(F.data.startswith("k:"))
async def key_actions(cb: CallbackQuery, bot: Bot, db: Database, cfg: Settings, keys: KeyBox) -> None:
    _, chat_s, action = cb.data.split(":", 2)
    chat_id = int(chat_s)
    if not await _authorized_group(bot, db, cfg, chat_id, cb.from_user.id):
        await cb.answer("not allowed", show_alert=True)
        return
    if action == "connect":
        kb = InlineKeyboardBuilder()
        kb.button(text="openai (or compatible)", callback_data=f"k:{chat_id}:prov:openai")
        kb.button(text="google gemini", callback_data=f"k:{chat_id}:prov:gemini")
        kb.button(text="« back", callback_data=f"m:{chat_id}")
        kb.adjust(1)
        await _edit_or_send(cb, "which provider is this key for?", kb.as_markup())
    elif action.startswith("prov:"):
        provider = action.split(":", 1)[1]
        await db.set_dm_state(cb.from_user.id, f"key:{provider}", chat_id)
        where = (
            "get one at platform.openai.com → api keys (free accounts work, you just need billing or a free grant)"
            if provider == "openai"
            else "get one at aistudio.google.com → get api key (free tier is fine)"
        )
        await _edit_or_send(
            cb,
            f"🔑 connect your {provider} key\n\n"
            f"1. {where}\n"
            "2. copy the key\n"
            "3. paste the key right here in this chat (not in the group)\n\n"
            "i'll test it, encrypt it, and delete your message instantly. only a masked hint (like AIza...9f2x) is ever shown. "
            "nothing is stored in plain text.\n\n"
            "send /cancel to stop.",
        )
    elif action == "disc":
        await db.update_group(chat_id, key_enc="", key_hint="", provider="", model="")
        await cb.answer("key disconnected")
        await _show_menu(cb, db, chat_id)


def _privacy_kb():
    from app.handlers.rose_c import _privacy_kb as kb
    return kb()


@router.message(Command("privacy"))
async def privacy(message: Message) -> None:
    await message.answer(
        "zephy privacy policy\n\n"
        "what i store: group ids and titles, admin ids, settings you choose, points/warns, and api keys admins connect (encrypted, never shown).\n"
        "what i read: messages in groups i'm a member of, only to answer /ask, summaries, moderation and points when triggered. i don't keep message history.\n"
        "what i never do: sell data, show keys, or dm your members first.\n\n"
        "questions: contact the bot owner (super admin). group admins can disconnect their group anytime with /disconnect or by removing me.",
        reply_markup=_privacy_kb(),
    )


@router.message(Command("setnews"))
async def set_news(message: Message, db: Database, cfg: Settings) -> None:
    if message.from_user.id not in cfg.super_admin_ids:
        return
    url = (message.text or "").split(" ", 1)[-1].strip() if message.text else ""
    if not url.startswith("https://t.me/"):
        await message.answer("send the channel link, like /setnews https://t.me/zephynews")
        return
    await db.set_meta("news_channel", url)
    await message.answer(f"news channel set: {url}. it now shows on /start.")


@router.message(Command("help"))
async def dm_help(message: Message) -> None:
    await message.answer(
        "zephy commands\n"
        "groups: /ask (question), /summary, /points, /lb, /rules\n"
        "admin: /warn, /ban, /kick, /mute, /purge, /award N reason (reply to a message)\n"
        "dm: /start (manage groups), /settings (same), /help (this)\n\n"
        "ai is optional: connect a key from /start when u want it. moderation, points, locks and welcomes work without one."
    )


@router.message(F.text)
async def free_text(message: Message, bot: Bot, db: Database, cfg: Settings, keys: KeyBox) -> None:
    """Handles the next free-text answer for whatever the admin is in the middle of setting up."""
    uid = message.from_user.id
    # any new command cancels a half-finished setup instead of being read as its input
    if (message.text or "").startswith("/"):
        st = await db.get_dm_state(uid)
        if st and st["state"]:
            await db.set_dm_state(uid, "")
            await message.answer("ok, cancelled that. send /start to reopen settings.")
            return
    st = await db.get_dm_state(uid)
    state, chat_id = st["state"], int(st["chat_id"])
    if not state or not chat_id:
        await message.answer("send /start to manage your groups.")
        return

    if state.startswith("key:"):
        await _receive_key(message, bot, db, cfg, keys, uid, chat_id, state.split(":", 1)[1])
    elif state == "lock":
        await _receive_lock(message, db, chat_id)
    elif state == "tz":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        name = (message.text or "").strip()
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            await message.answer("unknown timezone. use an IANA name like Africa/Lagos, Europe/London or America/New_York.")
            return
        await db.update_group(chat_id, timezone=name)
        await db.set_dm_state(uid, "")
        await message.answer(f"timezone set to {name}.")
        await _show_menu(message, db, chat_id)
    elif state == "prompt":
        from app.handlers.settings import receive_custom_prompt
        await receive_custom_prompt(message, db, chat_id, uid)
    elif state == "model":
        m = message.text.strip().replace(" ", "")
        await db.update_group(chat_id, model=m[:80])
        await db.set_dm_state(uid, "")
        await message.answer(f"model set to {m}.")
        await _show_menu(message, db, chat_id)
    elif state.startswith("fbk:"):
        from app.handlers.fallback import receive_fallback_input
        await receive_fallback_input(message, bot, db, cfg, keys, uid, chat_id, state.split(":", 1)[1])
    elif state == "byod":
        from app.handlers.byod_dm import receive_dsn
        await receive_dsn(message, bot, db, cfg, keys, uid, chat_id)
    elif state == "pers":
        await db.update_group(chat_id, personality=message.text.strip()[:300])
        await db.set_dm_state(uid, "")
        await message.answer("personality saved.")
        await _show_menu(message, db, chat_id)
    elif state == "points_award_hint":
        await db.set_dm_state(uid, "")
    else:
        await db.set_dm_state(uid, "")


async def _receive_key(
    message: Message, bot: Bot, db: Database, cfg: Settings, keys: KeyBox, uid: int, chat_id: int, provider: str
) -> None:
    raw = (message.text or "").strip()
    # Always remove the message holding the key, whether or not validation succeeds.
    deleted = False
    try:
        await message.delete()
        deleted = True
    except Exception as exc:
        log.info("could not delete key message in dm: %s", exc)

    if not looks_like_key(raw):
        await db.set_dm_state(uid, "")
        await message.answer(
            "that doesn't look like an api key, so i cancelled key setup. send /start to try again, or just use the bot without ai, moderation/points/locks all work keyless."
            + ("" if deleted else " also, please delete the message you just sent.")
        )
        return

    model = cfg.default_openai_model if provider == "openai" else cfg.default_gemini_model
    if provider == "openai" and (raw.startswith("AIza") or raw.startswith("AQ.")):
        provider = "gemini"
        model = cfg.default_gemini_model
    try:
        await validate_key(provider, raw, model, cfg.openai_base_url)
    except LLMError as exc:
        await db.set_dm_state(uid, "")
        await message.answer(
            f"key test failed: {exc}. nothing was saved, and key setup is cancelled.\n"
            "send /start to try again later, everything else in the bot works without a key."
        )
        return

    # if the default model 404'd, validate_key retried gemini-flash-latest; persist what worked
    if provider == "gemini" and model != "gemini-flash-latest":
        try:
            await validate_key(provider, raw, model, cfg.openai_base_url)
        except LLMError:
            model = "gemini-flash-latest"
    await db.update_group(
        chat_id,
        key_enc=keys.encrypt(raw),
        key_hint=mask_key(raw),
        provider=provider,
        model=model,
    )
    await db.set_dm_state(uid, "")
    note = "" if deleted else "\n\nheads up: i couldn't delete your message. please delete it yourself."
    await message.answer(f"✅ {provider} key connected: {mask_key(raw)}\nmodel: {model}{note}")
    await _show_menu(message, db, chat_id)


# ------------------------------------------------------------------ usage / personality / features
@router.callback_query(F.data.startswith("u:"))
async def usage(cb: CallbackQuery, db: Database, bot: Bot, cfg: Settings) -> None:
    chat_id = int(cb.data.split(":")[1])
    if not await _authorized_group(bot, db, cfg, chat_id, cb.from_user.id):
        await cb.answer("not allowed", show_alert=True)
        return
    month = datetime.utcnow().strftime("%Y-%m")
    calls = await db.get_usage(chat_id, month)
    await cb.answer()
    await cb.message.answer(f"ai calls this month (utc {month}): {calls}")


@router.callback_query(F.data.startswith("pers:"))
async def personality(cb: CallbackQuery, db: Database, bot: Bot, cfg: Settings) -> None:
    chat_id = int(cb.data.split(":")[1])
    if not await _authorized_group(bot, db, cfg, chat_id, cb.from_user.id):
        await cb.answer("not allowed", show_alert=True)
        return
    from app.handlers.settings import persona_screen
    grp = await db.get_group(chat_id)
    text, kb = persona_screen(chat_id, grp)
    await _edit_or_send(cb, text, kb)


@router.callback_query(F.data.startswith("feat:"))
async def features(cb: CallbackQuery, db: Database, bot: Bot, cfg: Settings) -> None:
    parts = cb.data.split(":")
    chat_id = int(parts[1])
    if not await _authorized_group(bot, db, cfg, chat_id, cb.from_user.id):
        await cb.answer("not allowed", show_alert=True)
        return
    if len(parts) == 4 and parts[2] == "t":
        flag = parts[3]
        grp = await db.get_group(chat_id)
        await db.update_group(chat_id, **{flag: 0 if grp[flag] else 1})
        await cb.answer("updated")
    grp = await db.get_group(chat_id)
    kb = InlineKeyboardBuilder()
    flags = [
        ("ai_enabled", "ai answers"),
        ("mod_enabled", "auto moderation"),
        ("ai_mod", "ai moderation"),
        ("welcome_enabled", "welcome"),
        ("captcha_enabled", "captcha on join"),
        ("allow_links", "links allowed"),
        ("web_access", "web access for /ask (shared search key)"),
    ]
    for key, label in flags:
        mark = "✅" if grp[key] else "⬜"
        kb.button(text=f"{mark} {label}", callback_data=f"feat:{chat_id}:t:{key}")
    kb.button(text="« back", callback_data=f"m:{chat_id}")
    kb.adjust(1)
    await _edit_or_send(cb, "features (tap to toggle):", kb.as_markup())


COMMON_TZ = [
    "Africa/Lagos", "UTC", "Africa/Nairobi", "Africa/Johannesburg", "Europe/London", "Europe/Paris",
    "Europe/Berlin", "Europe/Moscow", "Asia/Dubai", "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo",
    "Australia/Sydney", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Sao_Paulo",
]


@router.callback_query(F.data.startswith("tz:"))
async def timezone_prompt(cb: CallbackQuery, db: Database, bot: Bot, cfg: Settings) -> None:
    """Common-zone picker. Pick one with tz:<chat>:set:<zone>, or send a custom IANA name."""
    parts = cb.data.split(":")
    chat_id = int(parts[1])
    if not await _authorized_group(bot, db, cfg, chat_id, cb.from_user.id):
        await cb.answer("not allowed", show_alert=True)
        return
    if len(parts) == 4 and parts[2] == "set":
        from zoneinfo import ZoneInfo
        zone = parts[3]
        try:
            ZoneInfo(zone)
        except Exception:
            await cb.answer("unknown zone", show_alert=True)
            return
        await db.update_group(chat_id, timezone=zone)
        await cb.answer(f"timezone: {zone}")
    grp = await db.get_group(chat_id)
    kb = InlineKeyboardBuilder()
    for z in COMMON_TZ:
        mark = "• " if grp["timezone"] == z else ""
        kb.button(text=f"{mark}{z}", callback_data=f"tz:{chat_id}:set:{z}")
    kb.button(text="✏️ type a custom zone", callback_data=f"tz:{chat_id}:custom")
    kb.button(text="« back", callback_data=f"m:{chat_id}")
    kb.adjust(2)
    if len(parts) == 3 and parts[2] == "custom":
        await db.set_dm_state(cb.from_user.id, "tz", chat_id)
        await _edit_or_send(cb, "send the IANA zone name, e.g. Africa/Accra. (or tap back)")
        return
    await _edit_or_send(
        cb,
        f"group timezone: {grp['timezone']}\n"
        "scheduled locks, lock notices and the weekly/monthly leaderboard reset use this. default utc.",
        kb.as_markup(),
    )


# ------------------------------------------------------------------ scheduled lock
@router.callback_query(F.data.startswith("lk:"))
async def lock_menu(cb: CallbackQuery, db: Database, bot: Bot, cfg: Settings) -> None:
    parts = cb.data.split(":")
    chat_id = int(parts[1])
    if not await _authorized_group(bot, db, cfg, chat_id, cb.from_user.id):
        await cb.answer("not allowed", show_alert=True)
        return
    if len(parts) == 3 and parts[2] == "soft":
        row = await db.one("SELECT soft FROM locks WHERE chat_id = :c", c=chat_id)
        if row:
            await db.exec("UPDATE locks SET soft = :s WHERE chat_id = :c", s=0 if row["soft"] else 1, c=chat_id)
    if len(parts) == 3 and parts[2] == "off":
        await db.exec("DELETE FROM locks WHERE chat_id = :c", c=chat_id)
        await cb.answer("lock schedule removed")
    if len(parts) == 3 and parts[2] == "set":
        await db.set_dm_state(cb.from_user.id, "lock", chat_id)
        await _edit_or_send(
            cb,
            "send the window as one message:\n"
            "• daily 22:00 06:00  (repeats every day, crosses midnight ok)\n"
            "• once 2026-10-25 09:00 11:00  (one time, utc)\n"
            "daily times use the group's timezone (set it under 🌍 timezone). once-locks are utc.",
        )
        return
    lock = await db.one("SELECT * FROM locks WHERE chat_id = :c", c=chat_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏰ set / change window", callback_data=f"lk:{chat_id}:set")
    if lock:
        kb.button(text=f"{'🧱 soft lock: on' if lock['soft'] else '🔒 full lock: on'} (tap to switch)", callback_data=f"lk:{chat_id}:soft")
        kb.button(text="🗑 remove schedule", callback_data=f"lk:{chat_id}:off")
    kb.button(text="« back", callback_data=f"m:{chat_id}")
    kb.adjust(1)
    if lock:
        when = (
            f"daily {lock['start_hm']} to {lock['end_hm']}" if lock["kind"] == "daily"
            else f"once {datetime.utcfromtimestamp(lock['once_start']):%Y-%m-%d %H:%M} to {datetime.utcfromtimestamp(lock['once_end']):%H:%M} utc"
        )
        text = f"scheduled lock: {when}\nmode: {'soft (media and links blocked, text open)' if lock['soft'] else 'full lock'}"
    else:
        text = "no lock scheduled."
    await _edit_or_send(cb, text, kb.as_markup())


async def _receive_lock(message: Message, db: Database, chat_id: int) -> None:
    parsed = parse_lock_text(message.text or "")
    if not parsed:
        await message.answer("couldn't read that. use 'daily 22:00 06:00' or 'once 2026-10-25 09:00 11:00'.")
        return
    await db.exec("DELETE FROM locks WHERE chat_id = :c", c=chat_id)
    await db.exec(
        "INSERT INTO locks (chat_id, kind, start_hm, end_hm, once_start, once_end, soft, enabled, applied, orig_perms) "
        "VALUES (:c, :k, :sh, :eh, :os, :oe, 0, 1, 0, '')",
        c=chat_id, k=parsed["kind"], sh=parsed.get("start_hm", ""), eh=parsed.get("end_hm", ""),
        os=parsed.get("once_start", 0), oe=parsed.get("once_end", 0),
    )
    await db.set_dm_state(message.from_user.id, "")
    await message.answer("lock schedule saved. the group gets a notice when it locks and unlocks.")
    await _show_menu(message, db, chat_id)


# ------------------------------------------------------------------ points board
@router.callback_query(F.data.startswith("pts:"))
async def points_menu(cb: CallbackQuery, db: Database, bot: Bot, cfg: Settings) -> None:
    parts = cb.data.split(":")
    chat_id = int(parts[1])
    if not await _authorized_group(bot, db, cfg, chat_id, cb.from_user.id):
        await cb.answer("not allowed", show_alert=True)
        return
    if len(parts) == 4 and parts[2] == "size":
        await db.update_group(chat_id, lb_size=int(parts[3]))
    elif len(parts) == 4 and parts[2] == "reset":
        await db.update_group(chat_id, reset_policy=parts[3], last_reset_key="")
    elif len(parts) == 3 and parts[2] == "toggle":
        grp = await db.get_group(chat_id)
        await db.update_group(chat_id, points_enabled=0 if grp["points_enabled"] else 1)
    grp = await db.get_group(chat_id)
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"{'✅ points on' if grp['points_enabled'] else '⬜ points off'} (tap)",
        callback_data=f"pts:{chat_id}:toggle",
    )
    for n in (5, 10, 20):
        mark = "•" if grp["lb_size"] == n else ""
        kb.button(text=f"{mark}top {n}", callback_data=f"pts:{chat_id}:size:{n}")
    for policy in ("never", "weekly", "monthly"):
        mark = "•" if grp["reset_policy"] == policy else ""
        kb.button(text=f"{mark}reset {policy}", callback_data=f"pts:{chat_id}:reset:{policy}")
    kb.button(text="« back", callback_data=f"m:{chat_id}")
    kb.adjust(1, 3, 3, 1)
    text = (
        f"points: {'on' if grp['points_enabled'] else 'off'}\n"
        f"board size: top {grp['lb_size']}\n"
        f"reset: {grp['reset_policy']}\n"
        f"earning: {grp['points_per_msg']} per message, max {grp['points_hour_cap']} per hour per member.\n"
        "members use /points and /lb in the group. admins can /award @user N reason there."
    )
    await _edit_or_send(cb, text, kb.as_markup())


# ------------------------------------------------------------------ admin refresh
@router.callback_query(F.data.startswith("adm:"))
async def refresh_admins(cb: CallbackQuery, bot: Bot, db: Database, cfg: Settings) -> None:
    chat_id = int(cb.data.split(":")[1])
    if not await _authorized_group(bot, db, cfg, chat_id, cb.from_user.id):
        await cb.answer("not allowed", show_alert=True)
        return
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as exc:
        await cb.answer(f"could not fetch admins: {exc.__class__.__name__}", show_alert=True)
        return
    for a in admins:
        if a.user.is_bot:
            continue
        await db.upsert_admin(chat_id, a.user.id, a.user.username or "", a.user.first_name or "")
    await cb.answer(f"{len(admins)} admins synced")
    await _show_menu(cb, db, chat_id)


@router.callback_query(F.data.startswith("mdl:"))
async def model_pick(cb: CallbackQuery, db: Database, bot: Bot, cfg: Settings) -> None:
    chat_id = int(cb.data.split(":")[1])
    if not await _authorized_group(bot, db, cfg, chat_id, cb.from_user.id):
        await cb.answer("not allowed", show_alert=True)
        return
    await db.set_dm_state(cb.from_user.id, "model", chat_id)
    grp = await db.get_group(chat_id)
    await cb.message.answer(
        f"current model: {grp['model'] or 'default'}\n\n"
        "send the model name to use, e.g. gemini-flash-latest, gemini-2.5-flash, gemini-2.5-pro, gpt-4o-mini. "
        "or send 'auto' to let zephy pick what works."
    )
    await cb.answer()
