"""v2.2 Rose-parity handlers: antiflood/antiraid/approval settings, blocklists, auto-reply filters,
notes, pins, reports, log channels, disabling, import/export, languages and the DM toggles.

Group-side handlers live on `router`; DM toggles live on `dm_router`. Business logic sits in
app/rose_store.py (db) and app/moderation.py (punishments). Copy is lowercase, like the rest of zephy.
"""
from __future__ import annotations

import html
import io
import logging
import time
from collections import defaultdict, deque

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from app import rose_store as rs
from app.config import Settings
from app.db import Database
from app.i18n import LANGS, normalize_lang, t
from app.moderation import log_event, pass_captcha, punish, warn_user

log = logging.getLogger(__name__)
router = Router(name="rose")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))
router.callback_query.filter(F.message.chat.type.in_({"group", "supergroup"}))
dm_router = Router(name="rose_dm")

RAID_LOCK_S = 600  # raid mode stays on for ten minutes after a burst
PROTECTED_CMDS = {"enable", "disable", "disabled"}  # can never be switched off, or nobody can switch back on
RULE_VERB = {"mute": "muted", "kick": "kicked", "ban": "banned"}
TOGGLE_FIELDS = {"antiraid_on", "approval_on", "antipin_on", "reports_on"}

# in-memory join timestamps per chat for raid detection
_joins: dict[int, deque] = defaultdict(lambda: deque(maxlen=200))


def _thread(msg: Message) -> int | None:
    return msg.message_thread_id if msg.is_topic_message else None


def _is_admin_status(status: str) -> bool:
    return status in ("creator", "administrator")


async def _admin_or_refuse(message: Message, bot: Bot, cfg: Settings, db: Database) -> bool:
    from app.handlers.group import _is_admin
    if message.from_user and message.from_user.id in cfg.super_admin_ids:
        return True
    if message.from_user and await _is_admin(bot, message.chat.id, message.from_user.id):
        return True
    await message.reply("admins only.")
    return False


async def _grp(db: Database, message: Message) -> dict:
    grp = await db.get_group(message.chat.id)
    if grp is None:
        grp = await db.ensure_group(message.chat.id, message.chat.title or "")
    return grp


# ----------------------------------------------------------------- disabling middleware
class DisabledCommands(BaseMiddleware):
    """Disabled commands answer 'disabled' and never reach their handler."""

    async def __call__(self, handler, event, data):
        text = getattr(event, "text", None) or ""
        chat = getattr(event, "chat", None)
        if chat is not None and chat.type in ("group", "supergroup") and text.startswith("/"):
            cmd = rs.clean_command(text.split()[0])
            if cmd and cmd not in PROTECTED_CMDS:
                db: Database = data["db"]
                grp = await db.get_group(chat.id)
                if grp and cmd in rs.csv_set(grp.get("disabled_cmds", "")):
                    await event.reply(t(grp.get("lang"), "disabled", cmd=f"/{cmd}"))
                    return None
        return await handler(event, data)


# ----------------------------------------------------------------- antiflood / antiraid / approval settings
@router.message(Command("antiflood"))
async def antiflood_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    parts = (command.args or "").split()
    grp = await _grp(db, message)
    if not parts:
        limit = int(grp["antiflood_limit"])
        state = f"{limit} msgs per 5s, action {grp['antiflood_action']}" if limit else "off"
        await message.reply(f"antiflood: {state}\nusage: /antiflood off | /antiflood N [warn|mute|kick|ban]")
        return
    if parts[0].lower() == "off":
        await db.update_group(message.chat.id, antiflood_limit=0)
        await message.reply("antiflood off.")
        return
    if not parts[0].isdigit() or not 2 <= int(parts[0]) <= 50:
        await message.reply("give a number from 2 to 50, or off.")
        return
    action = parts[1].lower() if len(parts) > 1 else "warn"
    if action not in rs.FLOOD_ACTIONS:
        await message.reply("action must be warn, mute, kick or ban.")
        return
    await db.update_group(message.chat.id, antiflood_limit=int(parts[0]), antiflood_action=action)
    await message.reply(f"antiflood on: more than {parts[0]} msgs in 5s = {action}. admins are exempt.")


@router.message(Command("antiraid"))
async def antiraid_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    parts = (command.args or "").split()
    if not parts:
        grp = await _grp(db, message)
        state = "on" if grp["antiraid_on"] else "off"
        await message.reply(
            f"antiraid is {state}: {grp['antiraid_threshold']} joins in {grp['antiraid_window']}s triggers raid mode.\n"
            "usage: /antiraid on | off | N [window_seconds]"
        )
        return
    head = parts[0].lower()
    if head in ("on", "off"):
        await db.update_group(message.chat.id, antiraid_on=1 if head == "on" else 0)
        await message.reply(f"antiraid {head}.")
        return
    if not head.isdigit() or not 2 <= int(head) <= 200:
        await message.reply("threshold must be a number from 2 to 200, or on/off.")
        return
    fields = {"antiraid_on": 1, "antiraid_threshold": int(head)}
    if len(parts) > 1:
        if not parts[1].isdigit() or not 10 <= int(parts[1]) <= 3600:
            await message.reply("window must be 10 to 3600 seconds.")
            return
        fields["antiraid_window"] = int(parts[1])
    await db.update_group(message.chat.id, **fields)
    await message.reply(f"antiraid on: {fields['antiraid_threshold']} joins in {fields.get('antiraid_window', 60)}s triggers raid mode.")


async def note_join_and_check_raid(bot: Bot, db: Database, grp: dict, chat_id: int, now: float | None = None) -> tuple[bool, bool]:
    """Record one join. Returns (raid_just_triggered, raid_active)."""
    now = now or time.time()
    raid_until = int(grp.get("raid_until_ts") or 0)
    raid_active = raid_until > now
    if not grp.get("antiraid_on"):
        return False, raid_active
    window = int(grp.get("antiraid_window") or 60)
    threshold = int(grp.get("antiraid_threshold") or 5)
    q = _joins[chat_id]
    q.append(now)
    recent = [t_ for t_ in q if now - t_ <= window]
    if len(recent) >= threshold and not raid_active:
        await db.update_group(chat_id, raid_until_ts=int(now + RAID_LOCK_S))
        q.clear()
        return True, True
    return False, raid_active


@router.message(Command("approval"))
async def approval_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        grp = await _grp(db, message)
        await message.reply(f"join approval is {'on' if grp['approval_on'] else 'off'}. usage: /approval on | off")
        return
    await db.update_group(message.chat.id, approval_on=1 if arg == "on" else 0)
    await message.reply(
        "join approval on: new members must tap the button within 10 minutes or get kicked."
        if arg == "on" else "join approval off."
    )


async def start_approval(bot: Bot, db: Database, grp: dict, chat_id: int, user_id: int, name: str, thread_id: int | None) -> None:
    await bot.restrict_chat_member(chat_id, user_id, ChatPermissions(can_send_messages=False))
    await rs.add_pending_join(db, chat_id, user_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ approve me", callback_data=f"apv:{chat_id}:{user_id}")]]
    )
    await bot.send_message(
        chat_id, t(grp.get("lang"), "approve_prompt", name=html.escape(name)),
        reply_markup=kb, message_thread_id=thread_id,
    )


@router.callback_query(F.data.startswith("apv:"))
async def approve_tap(cb, bot: Bot, db: Database) -> None:
    _, chat_s, user_s = cb.data.split(":")
    if cb.from_user.id != int(user_s):
        await cb.answer("this button is for someone else", show_alert=True)
        return
    chat_id, user_id = int(chat_s), int(user_s)
    if not await rs.remove_pending_join(db, chat_id, user_id):
        await cb.answer("already handled")
        return
    await pass_captcha(bot, db, chat_id, user_id)
    await cb.answer("approved ✅")
    grp = await db.get_group(chat_id)
    try:
        await cb.message.edit_text(t(grp.get("lang") if grp else "en", "approved", name=html.escape(cb.from_user.first_name or "member")))
    except Exception:
        pass


# ----------------------------------------------------------------- blocklists
@router.message(Command("blocklist"))
async def blocklist_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    grp = await _grp(db, message)
    parts = (command.args or "").split(maxsplit=1)
    words = rs.csv_set(grp["blacklist"])
    if not parts or parts[0].lower() == "list":
        await message.reply(
            "blocklist: " + (", ".join(sorted(words)) if words else "empty")
            + f"\npunishment: {grp.get('blocklist_action', 'warn')}\n"
            "usage: /blocklist add word | remove word | list"
        )
        return
    op = parts[0].lower()
    if op not in ("add", "remove") or len(parts) < 2:
        await message.reply("usage: /blocklist add word | remove word | list")
        return
    new_words = {w.strip().lower() for w in parts[1].replace("\n", ",").split(",") if w.strip()}
    if op == "add":
        words |= new_words
        await db.update_group(message.chat.id, blacklist=rs.csv_join(words))
        await message.reply(f"added {len(new_words)} to the blocklist.")
    else:
        words -= new_words
        await db.update_group(message.chat.id, blacklist=rs.csv_join(words))
        await message.reply(f"removed {len(new_words)} from the blocklist.")


@router.message(Command("blocklistaction"))
async def blocklist_action_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    action = (command.args or "").strip().lower()
    if action not in rs.FLOOD_ACTIONS | {"delete"}:
        await message.reply("usage: /blocklistaction warn | mute | kick | ban | delete")
        return
    await db.update_group(message.chat.id, blocklist_action=action)
    await message.reply(f"blocklist action set to {action}.")


# ----------------------------------------------------------------- filters
@router.message(Command("filter"))
async def filter_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    parts = (command.args or "").split(maxsplit=1)
    reply_text = parts[1] if len(parts) > 1 else ""
    if not reply_text and message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
    if not parts or not reply_text:
        await message.reply("usage: /filter keyword reply text (or reply to a message with /filter keyword)")
        return
    keyword = parts[0].lower()
    await rs.set_filter(db, message.chat.id, keyword, reply_text)
    await message.reply(f"filter saved for '{html.escape(keyword)}'.")


@router.message(Command("filters"))
async def filters_cmd(message: Message, db: Database) -> None:
    rows = await rs.list_filters(db, message.chat.id)
    if not rows:
        await message.reply("no filters yet. add one with /filter keyword reply")
        return
    await message.reply("filters: " + ", ".join(html.escape(r["keyword"]) for r in rows))


@router.message(Command("stop"))
async def stop_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    keyword = (command.args or "").strip().lower()
    if not keyword:
        await message.reply("usage: /stop keyword")
        return
    ok = await rs.del_filter(db, message.chat.id, keyword)
    await message.reply(f"filter '{html.escape(keyword)}' removed." if ok else "no such filter.")


# ----------------------------------------------------------------- notes
@router.message(Command("save"))
async def save_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    parts = (command.args or "").split(maxsplit=1)
    name = rs.clean_note_name(parts[0]) if parts else None
    if not name:
        await message.reply("usage: /save name text, or reply to a message with /save name (names: letters, numbers, - _)")
        return
    body = parts[1] if len(parts) > 1 else ""
    if not body and message.reply_to_message:
        body = message.reply_to_message.text or message.reply_to_message.caption or ""
    if not body:
        await message.reply("the note needs text, or reply to a message.")
        return
    await rs.save_note(db, message.chat.id, name, body)
    await message.reply(f"saved note #{name}. get it with /get {name} or #{name}")


@router.message(Command("notes"))
async def notes_cmd(message: Message, db: Database) -> None:
    names = await rs.list_notes(db, message.chat.id)
    await message.reply("notes: " + (", ".join(f"#{n}" for n in names) if names else "none yet"))


@router.message(Command("get"))
async def get_cmd(message: Message, command: CommandObject, db: Database) -> None:
    name = rs.clean_note_name((command.args or "").split()[0] if command.args else "")
    if not name:
        await message.reply("usage: /get name")
        return
    body = await rs.get_note(db, message.chat.id, name)
    await message.reply(html.escape(body) if body else f"no note called #{name}.")


@router.message(Command("clear"))
async def clear_note_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    name = rs.clean_note_name((command.args or "").split()[0] if command.args else "")
    if not name:
        await message.reply("usage: /clear name")
        return
    ok = await rs.del_note(db, message.chat.id, name)
    await message.reply(f"deleted #{name}." if ok else f"no note called #{name}.")


async def send_note_or_filter(message: Message, db: Database, text: str) -> bool:
    """Hook for group.py: '#name' retrieves a note; keyword filters answer normal chat. True if handled."""
    if text.startswith("#"):
        name = rs.clean_note_name(text.split()[0])
        if name:
            body = await rs.get_note(db, message.chat.id, name)
            if body:
                await message.reply(html.escape(body))
                return True
    reply = await rs.find_filter_reply(db, message.chat.id, text)
    if reply:
        await message.reply(html.escape(reply))
        return True
    return False


# ----------------------------------------------------------------- pins
@router.message(Command("pin"))
async def pin_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    if not message.reply_to_message:
        await message.reply("reply to the message you want pinned.")
        return
    try:
        await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id, disable_notification=False)
    except Exception as exc:
        await message.reply(f"couldn't pin: {exc.__class__.__name__}")
        return
    await message.reply("pinned ✅")


@router.message(Command("unpin"))
async def unpin_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    try:
        if message.reply_to_message:
            await bot.unpin_chat_message(message.chat.id, message.reply_to_message.message_id)
        else:
            await bot.unpin_chat_message(message.chat.id)
    except Exception as exc:
        await message.reply(f"couldn't unpin: {exc.__class__.__name__}")
        return
    await message.reply("unpinned.")


@router.message(Command("pinned"))
async def pinned_cmd(message: Message, bot: Bot) -> None:
    chat = await bot.get_chat(message.chat.id)
    pm = getattr(chat, "pinned_message", None)
    if not pm:
        await message.reply("nothing pinned.")
        return
    link = _message_link(message.chat.id, pm.message_id)
    await message.reply(f"pinned message: {link}" if link else "something is pinned (no link available).")


def _message_link(chat_id: int, msg_id: int) -> str | None:
    s = str(chat_id)
    if not s.startswith("-100"):
        return None
    return f"https://t.me/c/{s[4:]}/{msg_id}"


@router.message(Command("antipin"))
async def antipin_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        grp = await _grp(db, message)
        await message.reply(f"antipin is {'on' if grp['antipin_on'] else 'off'}. usage: /antipin on | off")
        return
    await db.update_group(message.chat.id, antipin_on=1 if arg == "on" else 0)
    await message.reply(f"antipin {arg}: member pins are removed automatically." if arg == "on" else "antipin off.")


@router.message(F.pinned_message)
async def auto_unpin(message: Message, bot: Bot, db: Database) -> None:
    """Service message when someone pins. Member pins are undone when antipin is on."""
    if message.from_user is None or message.from_user.is_bot:
        return
    grp = await db.get_group(message.chat.id)
    if not grp or not grp.get("antipin_on"):
        return
    from app.handlers.group import _is_admin
    if await _is_admin(bot, message.chat.id, message.from_user.id):
        return
    try:
        await bot.unpin_chat_message(message.chat.id, message.pinned_message.message_id)
    except Exception as exc:
        log.info("antipin unpin failed in %s: %s", message.chat.id, exc)


# ----------------------------------------------------------------- reports
async def handle_report(message: Message, bot: Bot, db: Database) -> bool:
    """/report or '@admins' as a reply. Pings admins in the group (topic-aware) and logs it."""
    grp = await _grp(db, message)
    if not grp.get("reports_on"):
        return False
    target = message.reply_to_message
    if target is None or target.from_user is None:
        return False
    if message.from_user is None or message.from_user.is_bot or target.from_user.id == message.from_user.id:
        return False
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
    except Exception:
        admins = []
    mentions = []
    for a in admins:
        if a.user.is_bot:
            continue
        mentions.append(f'<a href="tg://user?id={a.user.id}">{html.escape(a.user.first_name or "admin")}</a>')
    reporter = html.escape(message.from_user.first_name or "member")
    offender = html.escape(target.from_user.first_name or "member")
    body = f"🚨 {reporter} reported a message from {offender}."
    if mentions:
        body += " admins: " + " ".join(mentions[:10])
    await message.reply(body, message_thread_id=_thread(message))
    await log_event(bot, db, message.chat.id, f"report by {reporter} about {offender}")
    return True


@router.message(Command("report"))
async def report_cmd(message: Message, bot: Bot, db: Database) -> None:
    if not await handle_report(message, bot, db):
        await message.reply("reply to the message you want to report with /report (reports may be off).")


@router.message(Command("reports"))
async def reports_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        grp = await _grp(db, message)
        await message.reply(f"reports are {'on' if grp['reports_on'] else 'off'}. usage: /reports on | off")
        return
    await db.update_group(message.chat.id, reports_on=1 if arg == "on" else 0)
    await message.reply(f"reports {arg}.")


# ----------------------------------------------------------------- log channel
@router.message(Command("logchannel"))
async def logchannel_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    parts = (command.args or "").split()
    if not parts:
        grp = await _grp(db, message)
        lc = int(grp.get("log_chat_id") or 0)
        await message.reply(f"log channel: {lc}" if lc else "no log channel set.\nusage: /logchannel set chat_id | off")
        return
    if parts[0].lower() == "off":
        await db.update_group(message.chat.id, log_chat_id=0)
        await message.reply("log channel off.")
        return
    if parts[0].lower() == "set" and len(parts) > 1:
        raw = parts[1]
    else:
        raw = parts[0]
    if not raw.lstrip("-").isdigit():
        await message.reply("usage: /logchannel set -100123456789 (the channel or group id) | off")
        return
    lc = int(raw)
    try:
        await bot.send_message(lc, f"zephy log channel connected for {html.escape(message.chat.title or str(message.chat.id))}.")
    except Exception:
        await message.reply("i can't post in that chat. add me there as admin first, then try again.")
        return
    await db.update_group(message.chat.id, log_chat_id=lc)
    await message.reply("log channel set ✅ bans, mutes, warns and reports go there.")


# ----------------------------------------------------------------- disabling
@router.message(Command("disable"))
async def disable_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    cmd = rs.clean_command(command.args or "")
    if not cmd:
        await message.reply("usage: /disable command (without the slash)")
        return
    if cmd in PROTECTED_CMDS:
        await message.reply(f"/{cmd} can't be disabled.")
        return
    grp = await _grp(db, message)
    cmds = rs.csv_set(grp["disabled_cmds"]) | {cmd}
    await db.update_group(message.chat.id, disabled_cmds=rs.csv_join(cmds))
    await message.reply(f"/{cmd} disabled in this group.")


@router.message(Command("enable"))
async def enable_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    cmd = rs.clean_command(command.args or "")
    if not cmd:
        await message.reply("usage: /enable command (without the slash)")
        return
    grp = await _grp(db, message)
    cmds = rs.csv_set(grp["disabled_cmds"]) - {cmd}
    await db.update_group(message.chat.id, disabled_cmds=rs.csv_join(cmds))
    await message.reply(f"/{cmd} enabled.")


@router.message(Command("disabled"))
async def disabled_list_cmd(message: Message, db: Database) -> None:
    grp = await _grp(db, message)
    cmds = sorted(rs.csv_set(grp["disabled_cmds"]))
    await message.reply("disabled here: " + (", ".join(f"/{c}" for c in cmds) if cmds else "nothing"))


# ----------------------------------------------------------------- languages
@router.message(Command("lang"))
async def lang_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    code = (command.args or "").strip().lower()
    if not code:
        grp = await _grp(db, message)
        await message.reply(
            f"language: {grp.get('lang') or 'en'}. options: " + ", ".join(f"{c} ({n})" for c, n in LANGS.items())
        )
        return
    if code not in LANGS:
        await message.reply(t("en", "lang_bad", langs=", ".join(LANGS)))
        return
    await db.update_group(message.chat.id, lang=code)
    await message.reply(t(code, "lang_set", lang=LANGS[code]))


# ----------------------------------------------------------------- import / export
@router.message(Command("export"))
async def export_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    import json
    data = await rs.export_settings(db, message.chat.id)
    blob = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    doc = BufferedInputFile(blob, filename=f"zephy-{str(message.chat.id).lstrip('-')}.json")
    try:
        await bot.send_document(
            message.from_user.id, doc,
            caption=f"settings export for {html.escape(message.chat.title or str(message.chat.id))}. "
                    "reply to a message in the group with /import to restore it.",
        )
    except Exception:
        await message.reply("i can't dm you yet. open a chat with me first (tap my name, press start), then run /export again.")
        return
    await message.reply("settings exported to your dm ✅")


@router.message(Command("import"))
async def import_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg, db):
        return
    src = message.reply_to_message
    if src is None:
        await message.reply("reply to the exported json file (or a message with its text) with /import")
        return
    raw = None
    if src.document:
        buf = io.BytesIO()
        await bot.download(src.document, destination=buf)
        raw = buf.getvalue().decode("utf-8", errors="replace")
    elif src.text:
        raw = src.text
    if not raw:
        await message.reply("no json found in that message.")
        return
    payload, err = rs.parse_import(raw)
    if payload is None:
        await message.reply(err)
        return
    n = await rs.import_settings(db, message.chat.id, payload)
    await message.reply(f"imported: {n} settings, {len(payload['filters'])} filters, {len(payload['notes'])} notes.")


# ----------------------------------------------------------------- DM toggles
async def _dm_authorized(bot: Bot, db: Database, cfg: Settings, chat_id: int, uid: int) -> bool:
    if uid in cfg.super_admin_ids:
        return True
    row = await db.one("SELECT user_id FROM group_admins WHERE chat_id = :c AND user_id = :u", c=chat_id, u=uid)
    return row is not None


def _rz_menu(chat_id: int, grp: dict) -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    for field, label in (
        ("antiraid_on", "antiraid"), ("approval_on", "join approval"),
        ("antipin_on", "antipin"), ("reports_on", "reports"),
    ):
        mark = "✅" if grp.get(field) else "▫️"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"rz:{chat_id}:t:{field}")])
    rows.append([InlineKeyboardButton(text="📤 export settings", callback_data=f"rz:{chat_id}:export")])
    text = (
        f"🛡 moderation tools: {grp.get('title') or chat_id}\n\n"
        f"antiflood: {int(grp['antiflood_limit'] or 0) and str(grp['antiflood_limit']) + ' msgs/5s, ' + grp['antiflood_action'] or 'off'}\n"
        f"blocklist punishment: {grp.get('blocklist_action', 'warn')}\n"
        f"language: {grp.get('lang') or 'en'}\n\n"
        "the rest is set with commands in the group (see /help)."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dm_router.callback_query(F.data.regex(r"^rz:-?\d+(:t:\w+|:export)?$"))
async def rz_callback(cb, bot: Bot, db: Database, cfg: Settings) -> None:
    parts = cb.data.split(":")
    chat_id = int(parts[1])
    if not await _dm_authorized(bot, db, cfg, chat_id, cb.from_user.id):
        await cb.answer("only group admins can change this", show_alert=True)
        return
    if len(parts) == 4 and parts[2] == "t" and parts[3] in TOGGLE_FIELDS:
        grp = await db.get_group(chat_id)
        if grp:
            await db.update_group(chat_id, **{parts[3]: 0 if grp.get(parts[3]) else 1})
    elif len(parts) == 3 and parts[2] == "export":
        import json
        data = await rs.export_settings(db, chat_id)
        blob = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        await bot.send_document(cb.from_user.id, BufferedInputFile(blob, filename=f"zephy-{str(chat_id).lstrip('-')}.json"))
        await cb.answer("export sent")
        return
    grp = await db.get_group(chat_id)
    if not grp:
        await cb.answer("group not found", show_alert=True)
        return
    text, kb = _rz_menu(chat_id, grp)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


def rz_button(chat_id: int) -> tuple[str, str]:
    """Label and callback data for the DM settings menu entry."""
    return "🛡 moderation tools", f"rz:{chat_id}"
