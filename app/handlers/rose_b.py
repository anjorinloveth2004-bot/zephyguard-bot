"""v2.2b handlers, slice 1: captcha settings, timed bans/mutes, delete+ban, and the timed-action store.

Group-side handlers live on `router`. Copy is lowercase, like the rest of zephy. Timed actions are
rows in `timed_actions`; the scheduler lifts them when `until_ts` passes (see expire_timed_actions).
"""
from __future__ import annotations

import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, Message

from app.config import Settings
from app.db import Database
from app.fillings import parse_duration
from app.moderation import punish

log = logging.getLogger(__name__)
router = Router(name="rose_b")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

CAPTCHA_KICK_MAX_S = 3600
TIMED_KINDS = {"tban": "ban", "tmute": "mute"}


async def _admin_or_refuse(message: Message, bot: Bot, cfg: Settings) -> bool:
    from app.handlers.group import _is_admin
    if message.from_user and message.from_user.id in cfg.super_admin_ids:
        return True
    if message.from_user and await _is_admin(bot, message.chat.id, message.from_user.id):
        return True
    await message.reply("admins only.")
    return False


def _target(message: Message):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    return None


# ----------------------------------------------------------------- timed-action store
async def add_timed(db: Database, chat_id: int, user_id: int, kind: str, until_ts: int) -> None:
    await db.exec(
        "INSERT INTO timed_actions (chat_id, user_id, kind, until_ts) VALUES (:c, :u, :k, :t) "
        "ON CONFLICT (chat_id, user_id, kind) DO UPDATE SET until_ts = :t",
        c=chat_id, u=user_id, k=kind, t=int(until_ts),
    )


async def remove_timed(db: Database, chat_id: int, user_id: int, kind: str) -> None:
    await db.exec(
        "DELETE FROM timed_actions WHERE chat_id = :c AND user_id = :u AND kind = :k",
        c=chat_id, u=user_id, k=kind,
    )


async def due_timed(db: Database, now: float | None = None) -> list[dict]:
    return await db.all(
        "SELECT chat_id, user_id, kind FROM timed_actions WHERE until_ts <= :t",
        t=int(now or time.time()),
    )


async def expire_timed_actions(bot: Bot, db: Database, now: float | None = None) -> int:
    """Lift every timed ban or mute that has come due. One bad row never stops the rest."""
    done = 0
    for row in await due_timed(db, now):
        try:
            if row["kind"] == "tban":
                await bot.unban_chat_member(row["chat_id"], row["user_id"], only_if_banned=True)
            elif row["kind"] == "tmute":
                from app.scheduler import UNLOCKED_PERMS
                await bot.restrict_chat_member(row["chat_id"], row["user_id"], UNLOCKED_PERMS)
        except Exception as exc:
            log.info("timed %s lift failed for %s/%s: %s", row["kind"], row["chat_id"], row["user_id"], exc)
        await remove_timed(db, row["chat_id"], row["user_id"], row["kind"])
        done += 1
    return done


# ----------------------------------------------------------------- captcha settings
@router.message(Command("captcha"))
async def captcha_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    parts = (command.args or "").split()
    grp = await db.get_group(message.chat.id) or {}
    if not parts:
        state = "on" if grp.get("captcha_enabled") else "off"
        await message.reply(
            f"captcha is {state}, mode {grp.get('captcha_mode') or 'button'}, "
            f"kick after {grp.get('captcha_kick_secs') or 300}s.\n"
            "usage: /captcha on | off | kick N (seconds, 60 to 3600)"
        )
        return
    head = parts[0].lower()
    if head in ("on", "off"):
        await db.update_group(message.chat.id, captcha_enabled=1 if head == "on" else 0)
        await message.reply(f"captcha {head}.")
        return
    if head == "kick":
        if len(parts) < 2 or not parts[1].isdigit() or not 60 <= int(parts[1]) <= CAPTCHA_KICK_MAX_S:
            await message.reply("kick time must be 60 to 3600 seconds.")
            return
        await db.update_group(message.chat.id, captcha_kick_secs=int(parts[1]))
        await message.reply(f"unsolved captcha members get kicked after {parts[1]}s.")
        return
    await message.reply("usage: /captcha on | off | kick N")


# ----------------------------------------------------------------- timed ban / mute, delete+ban
@router.message(Command("tban", "tmute"))
async def timed_punish_cmd(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Settings) -> None:
    if not await _admin_or_refuse(message, bot, cfg):
        return
    cmd = message.text.split()[0].lstrip("/").split("@")[0].lower()
    kind = cmd  # tban | tmute
    target = _target(message)
    if target is None:
        await message.reply(f"reply to someone's message with /{cmd} 4m|3h|6d|5w")
        return
    secs = parse_duration((command.args or "").split()[0] if command.args else "")
    if secs is None:
        await message.reply(f"give a duration: /{cmd} 4m, 3h, 6d or 5w.")
        return
    until = int(time.time()) + secs
    try:
        if kind == "tban":
            await bot.ban_chat_member(message.chat.id, target.id, until_date=until)
        else:
            await bot.restrict_chat_member(
                message.chat.id, target.id, ChatPermissions(can_send_messages=False), until_date=until,
            )
    except Exception as exc:
        await message.reply(f"couldn't {cmd}: {exc.__class__.__name__}")
        return
    await add_timed(db, message.chat.id, target.id, kind, until)
    verb = "banned" if kind == "tban" else "muted"
    await message.reply(f"{target.first_name or 'member'} {verb} for {command.args.split()[0]}.")


@router.message(Command("dban"))
async def dban_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    """Delete the replied-to message, then ban its author."""
    if not await _admin_or_refuse(message, bot, cfg):
        return
    target = _target(message)
    if target is None:
        await message.reply("reply to the message you want gone and its author banned.")
        return
    try:
        await bot.delete_message(message.chat.id, message.reply_to_message.message_id)
    except Exception:
        pass
    try:
        await punish(bot, message.chat.id, target.id, "ban")
    except Exception as exc:
        await message.reply(f"couldn't ban: {exc.__class__.__name__}")
        return
    await message.reply(f"deleted and banned {target.first_name or 'member'}.")


# ----------------------------------------------------------------- captcha expiry with kick time
async def expire_unsolved_captchas(bot: Bot, db: Database, now: float | None = None) -> int:
    """Kick members who never solved the captcha within their group's kick window."""
    now = int(now or time.time())
    rows = await db.all(
        "SELECT c.chat_id, c.user_id, c.created_ts, g.captcha_kick_secs FROM captchas c "
        "JOIN groups g ON g.chat_id = c.chat_id"
    )
    done = 0
    for r in rows:
        window = int(r["captcha_kick_secs"] or 300)
        if int(r["created_ts"]) + window > now:
            continue
        try:
            await bot.ban_chat_member(r["chat_id"], r["user_id"])
            await bot.unban_chat_member(r["chat_id"], r["user_id"], only_if_banned=True)
        except Exception as exc:
            log.info("captcha kick failed for %s/%s: %s", r["chat_id"], r["user_id"], exc)
        await db.exec("DELETE FROM captchas WHERE chat_id = :c AND user_id = :u", c=r["chat_id"], u=r["user_id"])
        done += 1
    return done
