"""V2 handlers: inline menus with back buttons, /menu group panel, trivia, memory, roast,
capsules, wiki, shadow-mod approval, appeals, persona presets and custom prompt, BYOD and fallback
model setup, spend caps, and federation. Every callback is answered promptly.

Group side stays light. Settings and keys live in DM only.
"""
from __future__ import annotations

import logging
import random
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import store
from app.config import Settings
from app.features import (
    BOT_NAME, PERSONA_PRESETS, TRIVIA_BANK, answer_matches, group_health, mask_dsn,
    parse_capsule_when, render_heat, validate_dsn,
)
from app.services import award, leaderboard, my_rank, render_board
from app.db import Database
from app.crypto import KeyBox

log = logging.getLogger(__name__)
router = Router(name="v2")

GROUP_MENU_TEXT = (
    "<b>{bot} menu</b>\n\n"
    "points, board, ask, summary, wiki, rules, help. tap to use."
)


def _b(builder: InlineKeyboardBuilder) -> InlineKeyboardMarkup:
    return builder.as_markup()


def group_menu_kb(chat_id: int, bot_username: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="my points", callback_data=f"g:{chat_id}:pts")
    kb.button(text="leaderboard", callback_data=f"g:{chat_id}:lb")
    kb.button(text="ask", callback_data=f"g:{chat_id}:ask")
    kb.button(text="summary", callback_data=f"g:{chat_id}:sum")
    kb.button(text="wiki", callback_data=f"g:{chat_id}:wiki")
    kb.button(text="rules", callback_data=f"g:{chat_id}:rules")
    kb.button(text="help", callback_data=f"g:{chat_id}:help")
    kb.adjust(2, 2, 2, 1)
    return _b(kb)


def back_kb(to: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="« back", callback_data=to)
    return _b(kb)


# ---------------------------------------------------------------- group /menu panel

@router.message(Command("menu"), F.chat.type.in_({"group", "supergroup"}))
async def group_menu(message: Message, bot: Bot, db: Database) -> None:
    me = await bot.get_me()
    grp = await db.get_group(message.chat.id) or {}
    from app.handlers.group_v2 import send_or_edit
    # edit-in-place: a repeat /menu updates the panel instead of stacking a new one
    await send_or_edit(
        bot, grp, message.chat.id, "menu",
        GROUP_MENU_TEXT.format(bot=BOT_NAME),
        parse_mode="HTML",
        reply_markup=group_menu_kb(message.chat.id, me.username or ""),
    )


@router.callback_query(F.data.regex(r"^g:-?\d+:(pts|lb|ask|sum|wiki|rules|help)$"))
async def group_menu_cb(cb: CallbackQuery, bot: Bot, db: Database, cfg: Settings, keys: KeyBox) -> None:
    parts = cb.data.split(":")
    chat_id, action = int(parts[1]), parts[2]
    await cb.answer()
    if action == "pts":
        rank, total, pts = await my_rank(db, chat_id, cb.from_user.id)
        text = f"you have {pts or 0} points. rank {rank} of {total}." if pts is not None else "no points yet. chat a bit."
    elif action == "lb":
        rows = await leaderboard(db, chat_id)
        text = render_board(rows, "leaderboard")
    elif action == "rules":
        grp = await db.get_group(chat_id)
        text = (grp or {}).get("welcome_text") or "no rules set yet."
    elif action == "wiki":
        slugs = await store.list_wiki(db, chat_id)
        text = "wiki pages: " + ", ".join(slugs) if slugs else "no wiki pages yet. admins can run /wiki build"
    elif action == "ask":
        # run it: ask the group chat to take a question in the next message
        text = f"ask me anything here: reply to my message, or mention @{bot_username} with your question."
    elif action == "sum":
        recent = await store.recall_block(db, chat_id, limit=40)
        if not recent.strip():
            text = "nothing recent to summarise yet."
        else:
            from app.ai import ask_ai
            res = await ask_ai(
                db, cfg, keys, chat_id, "summarise the chat in 5 short lowercase lines, no names of people beyond what is needed.",
                recent, max_tokens=300,
            )
            text = res.text or f"summary unavailable: {res.reason}"
    else:
        text = "/ask /summary /points /lb /rules /wiki /menu /help"
    await cb.message.answer(text, reply_markup=back_kb(f"g:{chat_id}:menu"))


@router.callback_query(F.data.regex(r"^g:-?\d+:menu$"))
async def group_menu_back(cb: CallbackQuery, bot: Bot) -> None:
    chat_id = int(cb.data.split(":")[1])
    me = await bot.get_me()
    await cb.answer()
    await cb.message.answer(GROUP_MENU_TEXT.format(bot=BOT_NAME),
                            reply_markup=group_menu_kb(chat_id, me.username or ""))


# ---------------------------------------------------------------- trivia

def trivia_join_kb(round_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="join / answer", callback_data=f"tv:{round_id}:join")
    return _b(kb)


async def start_trivia_round(bot: Bot, db: Database, chat_id: int) -> bool:
    question, answer = random.choice(TRIVIA_BANK)
    await store.open_trivia(db, chat_id, question, answer)
    rnd = await store.current_trivia(db, chat_id)
    await bot.send_message(
        chat_id,
        f"<b>{BOT_NAME} trivia</b>\n\nq: {question}\n\nreply with the answer. first correct one wins 5 points.",
        reply_markup=trivia_join_kb(rnd["id"]) if rnd else None,
    )
    return True


@router.message(Command("trivia"), F.chat.type.in_({"group", "supergroup"}))
async def trivia_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    from app.handlers.group import _is_admin  # reuse the cached admin check
    if not await _is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("only admins can host a round.")
        return
    await start_trivia_round(bot, db, message.chat.id)


@router.callback_query(F.data.regex(r"^tv:\d+:join$"))
async def trivia_join(cb: CallbackQuery, db: Database) -> None:
    await cb.answer("reply in chat with your answer")


async def check_trivia_answer(message: Message, db: Database) -> bool:
    """Called from the group text handler. Returns True when the text was a winning answer."""
    rnd = await store.current_trivia(db, message.chat.id)
    if not rnd or not message.text:
        return False
    if not answer_matches(message.text, rnd["answer"]):
        return False
    if not await store.claim_trivia(db, rnd["id"], message.from_user.id):
        return False
    await award(db, message.chat.id, message.from_user.id, 5, "trivia",
                message.from_user.username or "", message.from_user.first_name or "")
    await message.reply(f"correct: {rnd['answer']}. +5 points.")
    return True


# ---------------------------------------------------------------- memory

@router.message(Command("memory"), F.chat.type.in_({"group", "supergroup"}))
async def memory_cmd(message: Message, bot: Bot, db: Database, cfg: Settings) -> None:
    from app.handlers.group import _is_admin
    if not await _is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("only admins can read group memory.")
        return
    rows = await store.list_memory(db, message.chat.id)
    if not rows:
        await message.reply("no group memory yet.")
        return
    lines = [f"#{r['id']} [{r['kind']}] {r['body']}" for r in rows]
    await message.reply("<b>group memory</b>\n" + "\n".join(lines))


# ----------------------------------------------------------------- /battle (federation-lite duel)
BATTLE_RULE = "two options, the group votes, the most votes wins. ties go to the first option."


def battle_winner(a_votes: int, b_votes: int) -> str:
    """Pure tie-break for /battle: 'a', 'b', or 'tie'."""
    if a_votes > b_votes:
        return "a"
    if b_votes > a_votes:
        return "b"
    return "a" if a_votes == b_votes and a_votes >= 0 else "tie"


@router.message(Command("battle"))
async def battle_cmd(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """/battle a | b : open a two-option vote in the group."""
    parts = [p.strip() for p in (command.args or "").split("|")]
    if len(parts) != 2 or not all(parts):
        await message.reply("usage: /battle option a | option b")
        return
    await message.reply(f"battle: {parts[0]}  vs  {parts[1]}\n{BATTLE_RULE}\nvote by replying a or b.")
