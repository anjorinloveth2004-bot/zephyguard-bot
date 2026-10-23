"""v2.2b slices 2 and 3: goodbyes, fillings, filter/disable extras, log categories, notes extras, privacy
data (dm buttons), misc commands, purges, pins extras, rules, topics, warns extras, clean service and
clean commands, anon admin, and the outer middleware that cleans service and command messages.

Copy is lowercase, like the rest of zephy. Group settings only change through db.update_group
(GROUP_FIELDS allowlist). Column names come from V22B_COLUMNS in app/db.py.
"""
from __future__ import annotations

import html
import logging

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from app import rose_store as rs
from app.config import Settings
from app.db import Database
from app.fillings import SERVICE_CATS, render_text_with_buttons, runs_line, service_category
from app.handlers.rose_b import _admin_or_refuse

log = logging.getLogger(__name__)
router = Router(name="rose_c")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))
router.callback_query.filter(F.message.chat.type.in_({"group", "supergroup"}))
dm_router = Router(name="rose_c_dm")
dm_router.callback_query.filter(F.message.chat.type == "private")

DEFAULT_GOODBYE = "goodbye {first}"
LOG_CATS = ("bans", "mutes", "warns", "notes", "settings", "messages")
WARN_ACTIONS = {"ban", "kick", "mute", "tban", "tmute"}
PURGE_CONFIRM_WORD = "yes"


def _thread(msg: Message) -> int | None:
    return msg.message_thread_id if msg.is_topic_message else None


async def _grp(db: Database, chat_id: int, title: str = "") -> dict:
    return await db.get_group(chat_id) or await db.ensure_group(chat_id, title)


def _onoff(arg: str) -> int | None:
    arg = (arg or "").strip().lower()
    return 1 if arg in ("on", "yes") else 0 if arg in ("off", "no") else None


def _name_of(user) -> str:
    return (user.first_name or "member") if user else "member"


def _kb(buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b, url=u)] for b, u in buttons])


async def send_rich(bot: Bot, chat_id: int, template: str, thread: int | None = None, **fill) -> int | None:
    """Send user-authored text with {fillings} and [label](buttonurl:...) buttons. Returns message id."""
    body, buttons = render_text_with_buttons(template, **fill)
    try:
        sent = await bot.send_message(chat_id, body, message_thread_id=thread, reply_markup=_kb(buttons))
        return sent.message_id
    except Exception as exc:
        log.info("rich send failed in %s: %s", chat_id, exc)
        return None


# ----------------------------------------------------------------- goodbyes + fillings
async def send_goodbye(bot: Bot, db: Database, chat_id: int, user, title: str = "") -> None:
    """Called from the left_chat_member handler. Off by default; clean_goodbye removes the last one."""
    grp = await db.get_group(chat_id)
    if not grp or not grp.get("goodbye_on"):
        return
    if user is None or user.is_bot:
        return
    template = grp.get("goodbye_text") or DEFAULT_GOODBYE
    if grp.get("clean_goodbye") and grp.get("last_goodbye_id"):
        try:
            await bot.delete_message(chat_id, int(grp["last_goodbye_id"]))
        except Exception:
            pass
    mid = await send_rich(
        bot, chat_id, template, first=user.first_name or "", last=user.last_name or "",
        user_id=user.id, username=user.username or "", chat_title=title or grp.get("title", ""),
    )
    if mid:
        await db.update_group(chat_id, last_goodbye_id=mid)


@router.message(Command("goodbye"))
async def goodbye_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    val = _onoff(command.args or "")
    if val is None:
        grp = await _grp(db, message.chat.id, message.chat.title or "")
        await message.reply(f"goodbye is {'on' if grp.get('goodbye_on') else 'off'}. usage: /goodbye on | off")
        return
    await db.update_group(message.chat.id, goodbye_on=val)
    await message.reply("goodbye messages on." if val else "goodbye messages off.")


@router.message(Command("setgoodbye"))
async def setgoodbye_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    text = (command.args or "").strip()
    if not text:
        await message.reply(
            "usage: /setgoodbye your text. fillings: {first} {last} {fullname} {username} {id} {chatname} "
            "{count}. buttons: [label](buttonurl:https://...)"
        )
        return
    await db.update_group(message.chat.id, goodbye_text=text[:2000])
    await message.reply("goodbye text saved.")


@router.message(Command("resetgoodbye"))
async def resetgoodbye_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    await db.update_group(message.chat.id, goodbye_text="")
    await message.reply("goodbye text reset to the default.")


@router.message(Command("cleangoodbye"))
async def cleangoodbye_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    val = _onoff(command.args or "")
    if val is None:
        grp = await _grp(db, message.chat.id, message.chat.title or "")
        await message.reply(f"clean goodbye is {'on' if grp.get('clean_goodbye') else 'off'}. usage: /cleangoodbye on | off")
        return
    await db.update_group(message.chat.id, clean_goodbye=val)
    await message.reply("clean goodbye on: the last goodbye is removed when a new one is sent." if val else "clean goodbye off.")


# ----------------------------------------------------------------- stop all, disable admin/all
@router.message(Command("stopall"))
async def stopall_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    await db.exec("DELETE FROM chat_filters WHERE chat_id = :c", c=message.chat.id)
    await message.reply("all filters removed.")


@router.message(Command("disableadmin"))
async def disableadmin_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    val = _onoff(command.args or "")
    if val is None:
        await message.reply("usage: /disableadmin on | off (admin commands answer 'disabled' for everyone)")
        return
    await db.update_group(message.chat.id, disable_admin=val)
    await message.reply("admin commands disabled." if val else "admin commands enabled.")


@router.message(Command("disableall"))
async def disableall_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    val = _onoff(command.args or "")
    if val is None:
        await message.reply("usage: /disableall on | off (non-admins can only use the protected commands)")
        return
    await db.update_group(message.chat.id, disable_all=val)
    await message.reply("all commands disabled for members." if val else "member commands enabled.")


# ----------------------------------------------------------------- log categories
def _cats(grp: dict) -> list[str]:
    return sorted(rs.csv_set(grp.get("log_cats") or ""))


@router.message(Command("logcategories"))
async def logcategories_cmd(message: Message, db: Database) -> None:
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    on = _cats(grp)
    lines = [f"{c}: {'on' if c in on else 'off'}" for c in LOG_CATS]
    await message.reply("log categories\n" + "\n".join(lines) + "\nusage: /log <cat> | /nolog <cat>")


async def _set_cat(message: Message, command: CommandObject, db: Database, cfg: Settings, bot: Bot, add: bool) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    cat = (command.args or "").strip().lower()
    if cat not in LOG_CATS:
        await message.reply("categories: " + ", ".join(LOG_CATS))
        return
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    cur = rs.csv_set(grp.get("log_cats") or "")
    cur.add(cat) if add else cur.discard(cat)
    await db.update_group(message.chat.id, log_cats=rs.csv_join(cur))
    await message.reply(f"{cat} will {'be logged' if add else 'not be logged'}.")


@router.message(Command("log"))
async def log_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    await _set_cat(message, command, db, cfg, bot, add=True)


@router.message(Command("nolog"))
async def nolog_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    await _set_cat(message, command, db, cfg, bot, add=False)


# ----------------------------------------------------------------- notes extras
@router.message(Command("clearall"))
async def clearall_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    if (command.args or "").strip().lower() != PURGE_CONFIRM_WORD:
        await message.reply("this deletes every note in the group. send /clearall yes to confirm.")
        return
    await db.exec("DELETE FROM chat_notes WHERE chat_id = :c", c=message.chat.id)
    await message.reply("all notes deleted.")


@router.message(Command("privatenotes"))
async def privatenotes_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    val = _onoff(command.args or "")
    if val is None:
        await message.reply("usage: /privatenotes on | off (on: notes go to the asker's dm, not the group)")
        return
    await db.update_group(message.chat.id, privatenotes_on=val)
    await message.reply("notes will go to dm." if val else "notes post in the group.")


# ----------------------------------------------------------------- privacy (dm)
def _privacy_kb(chat_id: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👀 view my data", callback_data="pd:view"),
        InlineKeyboardButton(text="🗑 delete my data", callback_data="pd:del"),
    ]])


@dm_router.callback_query(F.data.in_({"pd:view", "pd:del"}))
async def privacy_data_cb(cb: CallbackQuery, db: Database) -> None:
    uid = cb.from_user.id
    if cb.data == "pd:view":
        pts = await db.all("SELECT chat_id, score FROM points WHERE user_id = :u", u=uid)
        warns = await db.all("SELECT chat_id, count FROM warns WHERE user_id = :u", u=uid)
        lines = [f"points in {r['chat_id']}: {r['score']}" for r in pts]
        lines += [f"warnings in {r['chat_id']}: {r['count']}" for r in warns]
        body = "\n".join(lines) if lines else "i have nothing stored about you."
        await cb.message.answer("your data\n" + body)
    else:
        await db.exec("DELETE FROM points WHERE user_id = :u", u=uid)
        await db.exec("DELETE FROM point_log WHERE user_id = :u", u=uid)
        await db.exec("DELETE FROM warns WHERE user_id = :u", u=uid)
        await db.exec("DELETE FROM dm_state WHERE user_id = :u", u=uid)
        await cb.message.answer("done. your points, warnings and dm state are deleted.")
    await cb.answer()


# ----------------------------------------------------------------- misc
@router.message(Command("runs"))
async def runs_cmd(message: Message) -> None:
    await message.reply(runs_line(message.message_id))


@router.message(Command("id"))
async def id_cmd(message: Message) -> None:
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        await message.reply(f"user id: {u.id}\nchat id: {message.chat.id}")
        return
    await message.reply(f"chat id: {message.chat.id}\nyour id: {message.from_user.id if message.from_user else 'unknown'}")


@router.message(Command("info"))
async def info_cmd(message: Message, db: Database) -> None:
    u = message.reply_to_message.from_user if message.reply_to_message and message.reply_to_message.from_user else message.from_user
    if u is None:
        return
    row = await db.one("SELECT score FROM points WHERE chat_id = :c AND user_id = :u", c=message.chat.id, u=u.id)
    warns = await db.one("SELECT count FROM warns WHERE chat_id = :c AND user_id = :u", c=message.chat.id, u=u.id)
    await message.reply(
        f"user: {html.escape(_name_of(u))}\nid: {u.id}\nusername: @{u.username or 'none'}\n"
        f"points: {row['score'] if row else 0}\nwarnings: {warns['count'] if warns else 0}"
    )


@router.message(Command("limits"))
async def limits_cmd(message: Message) -> None:
    await message.reply(
        "limits: antiflood 2 to 50 msgs per 5s, warnings 2 to 20, purge up to 100 per call, "
        "timed actions up to 366 days, captcha kick window 1 to 3600s."
    )


@router.message(Command("markdownhelp"))
async def markdownhelp_cmd(message: Message) -> None:
    await message.reply(
        "markdown and fillings in notes, welcome and goodbye: {first} {last} {fullname} {username} {id} "
        "{chatname} {count}. buttons: [label](buttonurl:https://example.com). send /markdownhelp in dm for more."
    )


# ----------------------------------------------------------------- purges
@router.message(Command("purgey"))
async def purgey_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    parts = (command.args or "").split()
    if not parts or not parts[0].isdigit() or not 1 <= int(parts[0]) <= 100:
        await message.reply("usage: /purgey N (1 to 100). deletes the N messages before this command.")
        return
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    if grp.get("purge_confirm") and (len(parts) < 2 or parts[1].lower() != PURGE_CONFIRM_WORD):
        await message.reply(f"purge confirm is on. send /purgey {parts[0]} {PURGE_CONFIRM_WORD} to run it.")
        return
    n = int(parts[0])
    ids = list(range(max(1, message.message_id - n), message.message_id + 1))
    deleted = 0
    for i in range(0, len(ids), 100):
        try:
            await bot.delete_messages(message.chat.id, ids[i:i + 100])
            deleted += len(ids[i:i + 100])
        except Exception:
            for mid in ids[i:i + 100]:
                try:
                    await bot.delete_message(message.chat.id, mid)
                    deleted += 1
                except Exception:
                    pass


@router.message(Command("purgeconfirm"))
async def purgeconfirm_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    val = _onoff(command.args or "")
    if val is None:
        await message.reply("usage: /purgeconfirm on | off")
        return
    await db.update_group(message.chat.id, purge_confirm=val)
    await message.reply("purge confirm on." if val else "purge confirm off.")


@router.message(Command("purgefrom"))
async def purgefrom_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    if not message.reply_to_message:
        await message.reply("reply to the first message with /purgefrom, then reply to the last with /purgeto.")
        return
    await db.update_group(message.chat.id, purge_from_id=message.reply_to_message.message_id)
    await message.reply("start marked. reply to the last message with /purgeto.")


@router.message(Command("purgeto"))
async def purgeto_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    start = int(grp.get("purge_from_id") or 0)
    if not start or not message.reply_to_message:
        await message.reply("first reply to the start message with /purgefrom.")
        return
    lo, hi = sorted((start, message.reply_to_message.message_id))
    ids = list(range(lo, hi + 1))
    for i in range(0, len(ids), 100):
        try:
            await bot.delete_messages(message.chat.id, ids[i:i + 100])
        except Exception:
            for mid in ids[i:i + 100]:
                try:
                    await bot.delete_message(message.chat.id, mid)
                except Exception:
                    pass
    await db.update_group(message.chat.id, purge_from_id=0)


# ----------------------------------------------------------------- pins extras
@router.message(Command("pinloud"))
async def pinloud_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    val = _onoff(command.args or "")
    if val is None:
        await message.reply("usage: /pinloud on | off (on: pins notify members)")
        return
    await db.update_group(message.chat.id, pin_loud=val)
    await message.reply("pins will notify members." if val else "pins are silent.")


@router.message(Command("pinmute"))
async def pinmute_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    await db.update_group(message.chat.id, pin_loud=0)
    await message.reply("pins are silent now.")


@router.message(Command("unpinall"))
async def unpinall_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    try:
        await bot.unpin_all_chat_messages(message.chat.id)
    except Exception as exc:
        await message.reply(f"couldn't unpin all: {exc.__class__.__name__}")
        return
    await message.reply("all pins removed.")


@router.message(Command("antichannelpin"))
async def antichannelpin_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    val = _onoff(command.args or "")
    if val is None:
        await message.reply("usage: /antichannelpin yes | no (yes: unpin pins sent from a linked channel)")
        return
    await db.update_group(message.chat.id, antichannelpin_on=val)
    await message.reply("channel pins will be undone." if val else "channel pins allowed.")


@router.message(Command("channelsettings"))
async def channelsettings_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    await message.reply(
        f"antipin: {'on' if grp.get('antipin_on') else 'off'}\n"
        f"antichannelpin: {'on' if grp.get('antichannelpin_on') else 'off'}\n"
        f"pin notifications: {'on' if grp.get('pin_loud') else 'off'}\n"
        f"banned deleted accounts (dbanned): {'on' if grp.get('dbanned_on') else 'off'}"
    )


@router.message(Command("dbanned"))
async def dbanned_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    val = _onoff(command.args or "")
    if val is None:
        await message.reply("usage: /dbanned on | off (on: deleted accounts are banned when they join)")
        return
    await db.update_group(message.chat.id, dbanned_on=val)
    await message.reply("deleted accounts will be banned on join." if val else "deleted accounts allowed.")


# ----------------------------------------------------------------- rules
@router.message(Command("setrules"))
async def setrules_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    text = (command.args or "").strip()
    if not text:
        await message.reply("usage: /setrules your rules text")
        return
    await db.update_group(message.chat.id, rules_text=text[:3500])
    await message.reply("rules saved.")


@router.message(Command("resetrules"))
async def resetrules_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    await db.update_group(message.chat.id, rules_text="")
    await message.reply("rules cleared.")


@router.message(Command("privaterules"))
async def privaterules_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    val = _onoff(command.args or "")
    if val is None:
        await message.reply("usage: /privaterules yes | no (yes: /rules is sent to the asker's dm)")
        return
    await db.update_group(message.chat.id, rules_private=val)
    await message.reply("rules go to dm." if val else "rules show in the group.")


@router.message(Command("setrulesbutton"))
async def setrulesbutton_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    text = (command.args or "").strip()
    if not text:
        await message.reply("usage: /setrulesbutton button text")
        return
    await db.update_group(message.chat.id, rules_button=text[:40])
    await message.reply("rules button saved.")


@router.message(Command("resetrulesbutton"))
async def resetrulesbutton_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    await db.update_group(message.chat.id, rules_button="")
    await message.reply("rules button removed.")


@router.callback_query(F.data.startswith("rules:"))
async def rules_button_cb(cb: CallbackQuery, db: Database) -> None:
    chat_id = int(cb.data.split(":", 1)[1])
    grp = await db.get_group(chat_id)
    text = (grp or {}).get("rules_text") or "no rules set yet."
    await cb.answer()
    await cb.message.answer(html.escape(text))


async def rules_reply(message: Message, bot: Bot, db: Database, noformat: bool = False) -> None:
    """Shared by /rules: shows the group's rules (or the default), privately when rules_private is on."""
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    text = grp.get("rules_text") or "no rules set yet. admins: /setrules your rules text"
    shown = text if noformat else html.escape(text)
    if grp.get("rules_private") and message.from_user:
        try:
            await bot.send_message(message.from_user.id, shown)
            await message.reply("rules sent to your dm.")
            return
        except Exception:
            await message.reply("i can't dm you yet. start a chat with me, then run /rules again.")
            return
    button = grp.get("rules_button") or ""
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button, callback_data=f"rules:{message.chat.id}")]]) if button else None
    await message.reply(shown, reply_markup=kb)


# ----------------------------------------------------------------- topics
@router.message(Command("settopic"))
async def settopic_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    tid = _thread(message)
    if not tid:
        await message.reply("run /settopic inside the topic you want as the default.")
        return
    await db.update_group(message.chat.id, topic_default=tid)
    await message.reply("default topic saved.")


@router.message(Command("gettopic"))
async def gettopic_cmd(message: Message, db: Database) -> None:
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    tid = int(grp.get("topic_default") or 0)
    await message.reply(f"default topic id: {tid}" if tid else "no default topic set. admins: /settopic inside a topic.")


@router.message(Command("newtopic"))
async def newtopic_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    name = (command.args or "").strip()[:128]
    if not name:
        await message.reply("usage: /newtopic topic name")
        return
    try:
        topic = await bot.create_forum_topic(message.chat.id, name)
    except Exception as exc:
        await message.reply(f"couldn't create the topic: {exc.__class__.__name__}. is this a forum group?")
        return
    await message.reply(f"topic created: {html.escape(topic.name)} (id {topic.message_thread_id}).")


async def _topic_action(message: Message, bot: Bot, cfg: Settings, db: Database, action: str, name: str | None = None) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    tid = _thread(message)
    if not tid:
        await message.reply(f"run /{action}topic inside the topic you want to change.")
        return
    try:
        if action == "rename":
            if not name:
                await message.reply("usage: /renametopic new name")
                return
            await bot.edit_forum_topic(message.chat.id, tid, name=name[:128])
            await message.reply("topic renamed.")
        elif action == "close":
            await bot.close_forum_topic(message.chat.id, tid)
            await message.reply("topic closed.")
        elif action == "reopen":
            await bot.reopen_forum_topic(message.chat.id, tid)
            await message.reply("topic reopened.")
        elif action == "delete":
            await bot.delete_forum_topic(message.chat.id, tid)
    except Exception as exc:
        await message.reply(f"couldn't {action} the topic: {exc.__class__.__name__}")


@router.message(Command("renametopic"))
async def renametopic_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    await _topic_action(message, bot, cfg, db, "rename", (command.args or "").strip() or None)


@router.message(Command("closetopic"))
async def closetopic_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    await _topic_action(message, bot, cfg, db, "close")


@router.message(Command("reopentopic"))
async def reopentopic_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    await _topic_action(message, bot, cfg, db, "reopen")


@router.message(Command("deletetopic"))
async def deletetopic_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    await _topic_action(message, bot, cfg, db, "delete")


# ----------------------------------------------------------------- warns extras
async def _target(message: Message):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    return None


@router.message(Command("warns"))
async def warns_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    u = await _target(message) or message.from_user
    row = await db.one("SELECT count FROM warns WHERE chat_id = :c AND user_id = :u", c=message.chat.id, u=u.id)
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    await message.reply(f"{_name_of(u)} has {row['count'] if row else 0}/{grp.get('warn_limit', 3)} warnings.")


@router.message(Command("swarn"))
async def swarn_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    u = await _target(message)
    if not u:
        await message.reply("reply to someone's message with /swarn [reason]")
        return
    from app.moderation import warn_user
    await warn_user(bot, db, message.chat.id, u.id, _name_of(u), (command.args or "no reason").strip())
    try:
        await message.delete()
    except Exception:
        pass


@router.message(Command("rwarn"))
async def rwarn_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    u = await _target(message)
    if not u:
        await message.reply("reply to someone's message with /rwarn to remove one warning.")
        return
    await db.exec(
        "UPDATE warns SET count = CASE WHEN count > 0 THEN count - 1 ELSE 0 END WHERE chat_id = :c AND user_id = :u",
        c=message.chat.id, u=u.id,
    )
    await message.reply(f"removed one warning from {_name_of(u)}.")


@router.message(Command("resetwarns"))
async def resetwarns_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    u = await _target(message)
    if not u:
        await message.reply("reply to someone's message with /resetwarns")
        return
    await db.exec("DELETE FROM warns WHERE chat_id = :c AND user_id = :u", c=message.chat.id, u=u.id)
    await message.reply(f"warnings reset for {_name_of(u)}.")


@router.message(Command("resetallwarns"))
async def resetallwarns_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    if (command.args or "").strip().lower() != PURGE_CONFIRM_WORD:
        await message.reply("this resets every warning in the group. send /resetallwarns yes to confirm.")
        return
    await db.exec("DELETE FROM warns WHERE chat_id = :c", c=message.chat.id)
    await message.reply("all warnings reset.")


@router.message(Command("setwarnaction"))
async def setwarnaction_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    act = (command.args or "").strip().lower()
    if act not in WARN_ACTIONS:
        await message.reply("action must be one of: ban, kick, mute, tban, tmute.")
        return
    await db.update_group(message.chat.id, warn_action=act)
    await message.reply(f"warn action set to {act}.")


@router.message(Command("warnlimit"))
async def warnlimit_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    raw = (command.args or "").strip()
    if not raw.isdigit() or not 2 <= int(raw) <= 20:
        await message.reply("give a number from 2 to 20.")
        return
    await db.update_group(message.chat.id, warn_limit=int(raw))
    await message.reply(f"warn limit set to {raw}.")


@router.message(Command("warntime"))
async def warntime_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    raw = (command.args or "").strip().lower()
    if raw in ("off", "0", ""):
        await db.update_group(message.chat.id, warn_time=0)
        await message.reply("warnings do not expire.")
        return
    from app.fillings import parse_duration
    secs = parse_duration(raw)
    if not secs:
        await message.reply("usage: /warntime 3d | 12h | off (warnings older than this reset).")
        return
    await db.update_group(message.chat.id, warn_time=secs)
    await message.reply(f"warnings expire after {raw}.")


# ----------------------------------------------------------------- clean service / commands
@router.message(Command("cleanservice"))
async def cleanservice_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    cat = (command.args or "").strip().lower()
    if cat not in SERVICE_CATS:
        await message.reply("categories: " + ", ".join(sorted(SERVICE_CATS)) + ". usage: /cleanservice <cat>")
        return
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    cur = rs.csv_set(grp.get("clean_service") or "")
    cur.add(cat)
    await db.update_group(message.chat.id, clean_service=rs.csv_join(cur))
    await message.reply(f"{cat} service messages will be deleted.")


@router.message(Command("keepservice"))
async def keepservice_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    cat = (command.args or "").strip().lower()
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    cur = rs.csv_set(grp.get("clean_service") or "")
    cur.discard(cat)
    await db.update_group(message.chat.id, clean_service=rs.csv_join(cur))
    await message.reply(f"{cat} service messages will be kept.")


@router.message(Command("cleanservicetypes"))
async def cleanservicetypes_cmd(message: Message, db: Database) -> None:
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    on = rs.csv_set(grp.get("clean_service") or "")
    await message.reply("service messages cleaned: " + (", ".join(sorted(on)) if on else "none"))


@router.message(Command("cleancommand"))
async def cleancommand_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    cmd = rs.clean_command(command.args or "")
    if not cmd:
        await message.reply("usage: /cleancommand <command name without the slash>")
        return
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    cur = rs.csv_set(grp.get("clean_commands") or "")
    cur.add(cmd)
    await db.update_group(message.chat.id, clean_commands=rs.csv_join(cur))
    await message.reply(f"/{cmd} messages will be deleted.")


@router.message(Command("keepcommand"))
async def keepcommand_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    cmd = rs.clean_command(command.args or "") or ""
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    cur = rs.csv_set(grp.get("clean_commands") or "")
    cur.discard(cmd)
    await db.update_group(message.chat.id, clean_commands=rs.csv_join(cur))
    await message.reply(f"/{cmd} messages will be kept.")


@router.message(Command("cleancommandtypes"))
async def cleancommandtypes_cmd(message: Message, db: Database) -> None:
    grp = await _grp(db, message.chat.id, message.chat.title or "")
    on = rs.csv_set(grp.get("clean_commands") or "")
    await message.reply("commands cleaned: " + (", ".join("/" + c for c in sorted(on)) if on else "none"))


class CleanMiddleware(BaseMiddleware):
    """Outer middleware: deletes service messages and commands the admins chose to clean. Never raises."""

    async def __call__(self, handler, event, data):
        chat = getattr(event, "chat", None)
        if chat is not None and chat.type in ("group", "supergroup"):
            db: Database = data.get("db")
            try:
                if db is not None:
                    grp = await db.get_group(chat.id)
                    if grp:
                        cat = service_category(event)
                        if cat and cat in rs.csv_set(grp.get("clean_service") or ""):
                            await event.delete()
                        text = getattr(event, "text", None) or ""
                        if text.startswith("/"):
                            cmd = rs.clean_command(text.split()[0])
                            if cmd and cmd in rs.csv_set(grp.get("clean_commands") or ""):
                                await event.delete()
            except Exception:
                log.info("clean middleware skipped one message in %s", chat.id)
        return await handler(event, data)


# ----------------------------------------------------------------- filters extras helper
async def stop_one_filter(db: Database, chat_id: int, keyword: str) -> bool:
    return await rs.del_filter(db, chat_id, keyword)


# ----------------------------------------------------------------- reset (everything back to defaults)
@router.message(Command("reset"))
async def reset_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    if (command.args or "").strip().lower() != PURGE_CONFIRM_WORD:
        await message.reply("this clears filters, notes, blocked words, disabled commands and log settings. send /reset yes to confirm.")
        return
    cid = message.chat.id
    await db.exec("DELETE FROM chat_filters WHERE chat_id = :c", c=cid)
    await db.exec("DELETE FROM chat_notes WHERE chat_id = :c", c=cid)
    await db.update_group(cid, blacklist="", disabled_cmds="", log_chat_id=0, log_cats="bans,mutes,warns,notes,settings,messages")
    await message.reply("group reset: filters, notes, blocked words, disabled commands and log settings cleared.")


# ----------------------------------------------------------------- anon admin / admin errors
@router.message(Command("anonadmin"))
async def anonadmin_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    """Anonymous-admin mode: admin commands sent as the group (not a person) are accepted when this is on."""
    if not await _admin_or_refuse(message, bot, cfg):
        return
    val = _onoff(command.args or "")
    if val is None:
        await message.reply("usage: /anonadmin on | off")
        return
    await db.update_group(message.chat.id, anon_admin=val)
    await message.reply("anonymous admins allowed." if val else "anonymous admins blocked.")


@router.message(Command("adminerror"))
async def adminerror_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    val = _onoff(command.args or "")
    if val is None:
        await message.reply("usage: /adminerror on | off (on: tell admins when the bot lacks a permission)")
        return
    await db.update_group(message.chat.id, admin_error_on=val)
    await message.reply("admin errors will be shown." if val else "admin errors hidden.")


# ----------------------------------------------------------------- help grid (rose style)
HELP_MODULES: dict[str, tuple[str, str]] = {
    "admin": ("admin", "/warn /ban /kick /mute /tban /tmute /dban /purge /purgey /purgefrom /purgeto /purgeconfirm\n/pin /unpin /pinned /pinloud /pinmute /unpinall /antipin /antichannelpin /channelsettings /dbanned\n/promote /demote /reset yes"),
    "antiflood": ("antiflood", "/antiflood N [warn|mute|kick|ban] (or off). admins are exempt. approved users are exempt."),
    "antiraid": ("antiraid", "/antiraid on|off|N. a burst of joins triggers captcha mode for new members."),
    "captcha": ("captcha", "/captcha on|off|kick. /captchamode, /captchakicktime, /setcaptchatext, /resetcaptchatext"),
    "locks": ("locks", "/lock <thing> and /unlock <thing>. see /locks for the list. schedule with /lockwindow."),
    "blocklists": ("blocklists", "/blocklist add|remove|list <word>, /blocklistaction <warn|mute|kick|ban>"),
    "filters": ("filters", "/filter keyword reply, /filters, /stop keyword, /stopall"),
    "notes": ("notes", "/save name reply, /notes, /get name or #name, /clear name, /clearall yes, /privatenotes on|off"),
    "warns": ("warns", "/warn, /swarn, /rwarn, /warns, /resetwarns, /resetallwarns yes, /setwarnaction, /warnlimit N, /warntime 3d|off"),
    "reports": ("reports", "/report (reply) or @admins (reply). /reports on|off"),
    "rules": ("rules", "/rules, /setrules text, /resetrules, /privaterules yes|no, /setrulesbutton text, /resetrulesbutton"),
    "goodbyes": ("goodbyes", "/goodbye on|off, /setgoodbye text, /resetgoodbye, /cleangoodbye on|off"),
    "disabling": ("disabling", "/disable cmd, /enable cmd, /disabled, /disableadmin on|off, /disableall on|off"),
    "cleaning": ("cleaning", "/cleanservice cat, /keepservice cat, /cleanservicetypes, /cleancommand cmd, /keepcommand cmd, /cleancommandtypes"),
    "logs": ("logs", "/logchannel chat_id|off, /log cat, /nolog cat, /logcategories"),
    "topics": ("topics", "/settopic, /gettopic, /actiontopic, /newtopic, /renametopic, /closetopic, /reopentopic, /deletetopic"),
    "misc": ("misc", "/runs, /id, /info, /limits, /markdownhelp, /export, /import, /lang, /anonadmin, /adminerror"),
}


def help_grid() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "hey there! i'm zephy, a group helper.\n"
        "tap a module below to see its commands. admins run most of these; members can use the rest."
    )
    rows, row = [], []
    for key, (label, _) in HELP_MODULES.items():
        row.append(InlineKeyboardButton(text=label, callback_data=f"hmod:{key}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("help"))
async def help_grid_cmd(message: Message) -> None:
    text, kb = help_grid()
    await message.reply(text, reply_markup=kb)


@router.callback_query(F.data.startswith("hmod:"))
async def help_module_cb(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 1)[1]
    if key not in HELP_MODULES:
        await cb.answer("unknown module", show_alert=True)
        return
    label, body = HELP_MODULES[key]
    back = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« back", callback_data="hmod:back")]])
    if cb.message:
        await cb.message.edit_text(f"{label}\n\n{body}", reply_markup=back)
    await cb.answer()


@router.callback_query(F.data == "hmod:back")
async def help_back_cb(cb: CallbackQuery) -> None:
    text, kb = help_grid()
    if cb.message:
        await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()
