"""/federate and /battle commands (group chat) plus the answer check used by the group text handler.

Admins link groups with a one-time code: /federate makes a code, /federate join <code> accepts it.
/battle starts a timed trivia duel with the linked group and scores both sides on their own boards.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app import federation
from app.db import Database
from app.features import TRIVIA_BANK  # noqa: F401  (kept for parity with trivia)

log = logging.getLogger(__name__)
router = Router(name="federate")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    from app.handlers.group import _is_admin as admin_check
    return await admin_check(bot, chat_id, user_id)


@router.message(Command("federate"))
async def federate_cmd(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    if not await _is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("only admins can link groups.")
        return
    arg = (command.args or "").strip()
    if arg.lower().startswith("join"):
        code = arg[4:].strip()
        if not code:
            await message.reply("usage: /federate join <code>")
            return
        ok, info = await federation.accept_invite(db, code, message.chat.id)
        await message.reply(f"linked. {info}" if ok else info)
        return
    if arg.lower() == "board":
        rows = await federation.shared_board(db, message.chat.id)
        if not rows:
            await message.reply("no linked groups or no points yet. link with /federate first.")
            return
        lines = [f"{i+1}. {r['name'] or r['username'] or r['user_id']} · {r['score']}" for i, r in enumerate(rows)]
        await message.reply("<b>shared board</b>\n" + "\n".join(lines))
        return
    code = await federation.create_invite(db, message.chat.id)
    await message.reply(
        f"invite code: {code}\nsend `/federate join {code}` in the other group's chat. the code works once."
    )


@router.message(Command("battle"))
async def battle_cmd(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    if not await _is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("only admins can start a battle.")
        return
    peer = await federation.fed_peer(db, message.chat.id)
    if peer is None:
        await message.reply("this group isn't linked to another yet. use /federate first.")
        return
    bid = await federation.start_battle(db, message.chat.id, peer)
    if bid is None:
        await message.reply("couldn't start the battle. try again.")
        return
    await message.reply(
        f"battle #{bid} starts now: {federation.BATTLE_QUESTIONS} questions, "
        f"{federation.BATTLE_Q_SECONDS}s each. first correct answer in each group scores for that group."
    )
    cur = await federation.current_question(db, bid)
    if cur:
        await bot.send_message(message.chat.id, f"q1: {cur['q']}")
        await bot.send_message(peer, f"battle #{bid}, q1: {cur['q']}")


async def check_battle_answer(message: Message, bot: Bot, db: Database) -> bool:
    """Score a correct battle answer. Returns True when the text counted for a battle."""
    grp = await db.get_group(message.chat.id)
    if not grp or not grp.get("federation_id"):
        return False
    row = await db.one(
        "SELECT id FROM battles WHERE status = 'open' AND (chat_a = :c OR chat_b = :c) ORDER BY id DESC LIMIT 1",
        c=message.chat.id,
    )
    if not row:
        return False
    cur = await federation.current_question(db, int(row["id"]))
    if not cur or not message.text:
        return False
    if not await federation.claim_battle_answer(
        db, int(row["id"]), message.chat.id, message.from_user.id, message.text, cur["qi"], cur["a"]
    ):
        return False
    await federation.record_battle_point(
        db, int(row["id"]), message.chat.id, message.from_user.id,
        message.from_user.username or "", message.from_user.first_name or "",
    )
    await message.reply(f"correct. +{federation.BATTLE_POINTS} for this group.")
    return True
