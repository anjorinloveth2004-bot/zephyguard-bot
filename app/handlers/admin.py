"""Super-admin dashboard (SUPER_ADMIN_IDS). Only works in private chat with the bot.

Commands:
  /stats                 totals across groups
  /groups                list groups the bot is in (id, title, banned)
  /disconnect <chat_id>  wipe a group's stored AI key (force disconnect)
  /banGroup <chat_id>    stop the bot responding in a group
  /unbanGroup <chat_id>  lift the ban
"""
from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.config import Settings
from app.db import Database

router = Router(name="admin")
router.message.filter(F.chat.type == "private")


@router.message(Command("stats"))
async def stats(message: Message, db: Database, cfg: Settings) -> None:
    if message.from_user.id not in cfg.super_admin_ids:
        return
    g = await db.one("SELECT COUNT(*) AS n, COALESCE(SUM(banned),0) AS b FROM groups")
    k = await db.one("SELECT COUNT(*) AS n FROM groups WHERE key_enc <> ''")
    a = await db.one("SELECT COUNT(*) AS n FROM group_admins")
    month = time.strftime("%Y-%m", time.gmtime())
    u = await db.one("SELECT COALESCE(SUM(calls),0) AS n FROM usage WHERE month = :m", m=month)
    await message.answer(
        f"groups: {g['n']} (banned {g['b']})\n"
        f"groups with own key: {k['n']}\n"
        f"tracked admins: {a['n']}\n"
        f"ai calls this month (utc): {u['n']}"
    )


@router.message(Command("groups"))
async def groups(message: Message, db: Database, cfg: Settings) -> None:
    if message.from_user.id not in cfg.super_admin_ids:
        return
    rows = await db.all("SELECT chat_id, title, banned FROM groups ORDER BY created_at DESC LIMIT 50")
    if not rows:
        await message.answer("no groups yet.")
        return
    lines = [f"{r['chat_id']} | {r['title'] or '-'} | {'BANNED' if r['banned'] else 'ok'}" for r in rows]
    await message.answer("\n".join(lines))


@router.message(Command("disconnect"))
async def force_disconnect(message: Message, command: CommandObject, db: Database, cfg: Settings) -> None:
    if message.from_user.id not in cfg.super_admin_ids:
        return
    chat_id = _parse_id(command.args)
    if chat_id is None:
        await message.answer("usage: /disconnect <chat_id>")
        return
    await db.update_group(chat_id, key_enc="", key_hint="", provider="", model="")
    await message.answer(f"key removed for {chat_id}.")


@router.message(Command("banGroup", "bangroup"))
async def ban_group(message: Message, command: CommandObject, db: Database, cfg: Settings) -> None:
    if message.from_user.id not in cfg.super_admin_ids:
        return
    chat_id = _parse_id(command.args)
    if chat_id is None:
        await message.answer("usage: /banGroup <chat_id>")
        return
    await db.update_group(chat_id, banned=1)
    await message.answer(f"group {chat_id} banned (bot stays silent there).")


@router.message(Command("unbanGroup", "unbangroup"))
async def unban_group(message: Message, command: CommandObject, db: Database, cfg: Settings) -> None:
    if message.from_user.id not in cfg.super_admin_ids:
        return
    chat_id = _parse_id(command.args)
    if chat_id is None:
        await message.answer("usage: /unbanGroup <chat_id>")
        return
    await db.update_group(chat_id, banned=0)
    await message.answer(f"group {chat_id} unbanned.")


def _parse_id(args: str | None) -> int | None:
    try:
        return int((args or "").strip())
    except ValueError:
        return None
