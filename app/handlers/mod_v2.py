"""Item 4 handlers: shadow mod approval, appeals, roast opt-in, time capsules, wiki, smart slow mode.

Admin-only actions go through DMs or group admin checks. Every callback is answered promptly.
Anything that calls the AI goes through app.ai.ask_ai (spend cap, BYOK, free tier, fallback).
"""
from __future__ import annotations

import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import store
from app.ai import ask_ai
from app.config import Settings
from app.crypto import KeyBox
from app.db import Database
from app.features import APPEAL_PROMPT, parse_capsule_when, parse_verdict, slow_decision, slow_score

log = logging.getLogger(__name__)
router = Router(name="mod_v2")

ROAST_SYSTEM = (
    "you write a short playful roast of one group member. punchy, no slurs, no protected-group "
    "jokes, nothing about appearance, health, or family. one or two lines, lowercase."
)


def _kb(rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, data in rows:
        kb.button(text=text, callback_data=data)
    kb.adjust(2)
    return kb.as_markup()


# ---------------------------------------------------------------- shadow mod (admin approval)
def flag_text(flag: dict) -> str:
    return (
        f"flagged message from {flag['user_name']}\n"
        f"reason: {flag['reason']}\n\n"
        f"{(flag['text'] or '')[:400]}"
    )


async def send_flag_to_admins(bot: Bot, db: Database, chat_id: int, flag_id: int, admin_ids: list[int]) -> None:
    flag = await store.get_flag(db, flag_id)
    if not flag:
        return
    kb = _kb([("✅ remove", f"sm:{flag_id}:rm"), ("👍 ignore", f"sm:{flag_id}:ok")])
    for uid in admin_ids:
        try:
            await bot.send_message(uid, flag_text(flag), reply_markup=kb)
        except Exception as exc:  # admin never started the bot in DM
            log.info("shadow flag not delivered to %s: %s", uid, exc)


@router.callback_query(F.data.regex(r"^sm:\d+:(rm|ok)$"))
async def shadow_decide(cb: CallbackQuery, bot: Bot, db: Database) -> None:
    _, fid, action = cb.data.split(":")
    flag = await store.get_flag(db, int(fid))
    if not flag or flag["status"] != "open":
        await cb.answer("already handled", show_alert=True)
        return
    if action == "rm":
        try:
            await bot.delete_message(flag["chat_id"], flag["msg_id"])
        except Exception as exc:
            log.info("shadow remove failed: %s", exc)
        await store.set_flag_status(db, flag["id"], "removed")
        out = "removed."
    else:
        await store.set_flag_status(db, flag["id"], "ignored")
        out = "left it."
    await cb.answer()
    await cb.message.edit_text(f"{out}\n\n{flag_text(flag)}")


# ---------------------------------------------------------------- appeals
async def submit_appeal(db: Database, cfg: Settings, keys: KeyBox, chat_id: int, user_id: int, user_name: str, text: str) -> int:
    """Store the appeal and draft an AI verdict for admins. Returns the appeal id."""
    aid = await store.add_appeal(db, chat_id, user_id, user_name, text)
    res = await ask_ai(db, cfg, keys, chat_id, system=APPEAL_PROMPT, user=text, max_tokens=120)
    verdict, reason = parse_verdict(res.text or "")  # unclear output defaults to uphold (safe side)
    draft = f"{verdict.upper()}: {reason}" if res.text else f"no ai draft ({res.reason or 'unavailable'}), upholding by default"
    await store.set_appeal_draft(db, aid, draft[:500])
    return aid


@router.callback_query(F.data.regex(r"^ap:\d+:(approve|deny)$"))
async def appeal_decide(cb: CallbackQuery, bot: Bot, db: Database) -> None:
    _, aid, action = cb.data.split(":")
    ap = await store.get_appeal(db, int(aid))
    if not ap or ap["status"] != "open":
        await cb.answer("already handled", show_alert=True)
        return
    if action == "approve":
        try:
            await bot.unban_chat_member(ap["chat_id"], ap["user_id"], only_if_banned=True)
        except Exception as exc:
            log.info("appeal unban failed: %s", exc)
        await store.set_appeal_status(db, ap["id"], "approved")
        out = "approved, user unbanned."
    else:
        await store.set_appeal_status(db, ap["id"], "denied")
        out = "denied."
    await cb.answer()
    await cb.message.edit_text(f"appeal from {ap['user_name']}: {out}")


@router.message(Command("appeal"), F.chat.type == "private")
async def appeal_cmd(message: Message, command: CommandObject, db: Database, cfg: Settings, keys: KeyBox) -> None:
    """A banned user DMs the bot: /appeal <chat_id> <why>."""
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[0].lstrip("-").isdigit():
        await message.answer("send: /appeal <group id> <why you should be let back in>")
        return
    chat_id = int(parts[0])
    grp = await db.get_group(chat_id)
    if not grp or not grp.get("appeals_on"):
        await message.answer("appeals are not open in that group.")
        return
    aid = await submit_appeal(db, cfg, keys, chat_id, message.from_user.id, message.from_user.first_name or "member", parts[1][:500])
    await message.answer(f"appeal #{aid} sent to the admins.")


# ---------------------------------------------------------------- roast (opt-in)
@router.message(Command("roast"), F.chat.type.in_({"group", "supergroup"}))
async def roast(message: Message, db: Database, cfg: Settings, keys: KeyBox) -> None:
    grp = await db.get_group(message.chat.id)
    if not grp or not grp.get("roast_on"):
        await message.reply("roast is off in this group. an admin can turn it on in dm settings.")
        return
    target = message.reply_to_message.from_user if message.reply_to_message and message.reply_to_message.from_user else None
    if target is None:
        await message.reply("reply to someone's message with /roast.")
        return
    if target.id == message.from_user.id:
        await message.reply("roasting yourself is a cry for help. try someone else.")
        return
    res = await ask_ai(db, cfg, keys, message.chat.id, ROAST_SYSTEM, f"target: {target.first_name}", max_tokens=120)
    await message.reply(res.text or f"roast unavailable: {res.reason}")


# ---------------------------------------------------------------- time capsules
@router.message(Command("capsule"), F.chat.type.in_({"group", "supergroup"}))
async def capsule(message: Message, command: CommandObject, db: Database) -> None:
    """/capsule <when> | <message>   e.g. /capsule in 2 days | see you at the reunion"""
    grp = await db.get_group(message.chat.id)
    tz = (grp or {}).get("timezone") or "UTC"
    raw = command.args or ""
    if "|" not in raw:
        await message.reply("format: /capsule <when> | <message>. e.g. /capsule +2d | good luck")
        return
    when_text, body = [s.strip() for s in raw.split("|", 1)]
    reveal = parse_capsule_when(when_text, tz, time.time())
    if reveal is None:
        await message.reply("couldn't read that time. use +3h, +2d, or YYYY-MM-DD HH:MM (group time).")
        return
    await store.add_capsule(db, message.chat.id, message.from_user.id, body[:500], int(reveal))
    await message.reply(f"capsule sealed. it opens at {store.fmt_ts(int(reveal), tz)}.")


async def reveal_due_capsules(bot: Bot, db: Database, now: float | None = None) -> int:
    """Called from the scheduler. Returns how many capsules opened."""
    due = await store.due_capsules(db, now)
    opened = 0
    for c in due:
        try:
            await bot.send_message(c["chat_id"], f"📦 time capsule opened: {c['body']}")
            await store.mark_capsule(db, c["id"], "opened")
            opened += 1
        except Exception as exc:
            log.info("capsule reveal failed for %s: %s", c["id"], exc)
    return opened


# ---------------------------------------------------------------- wiki
@router.message(Command("wiki"), F.chat.type.in_({"group", "supergroup"}))
async def wiki(message: Message, command: CommandObject, db: Database, cfg: Settings, keys: KeyBox) -> None:
    """/wiki             list pages
    /wiki <slug>      show a page
    /wiki build       admin: build pages from recent chat via ai"""
    grp = await db.get_group(message.chat.id)
    if not grp or not grp.get("wiki_on"):
        await message.reply("wiki is off in this group.")
        return
    arg = (command.args or "").strip()
    if not arg:
        slugs = await store.list_wiki(db, message.chat.id)
        await message.reply("wiki pages: " + (", ".join(slugs) if slugs else "none yet. admins: /wiki build"))
        return
    if arg == "build":
        member = await message.chat.get_member(message.from_user.id)
        if member.status not in ("creator", "administrator"):
            await message.reply("only admins can build the wiki.")
            return
        recent = await store.recall_block(db, message.chat.id, limit=80)
        res = await ask_ai(
            db, cfg, keys, message.chat.id,
            "write a short group wiki from these notes: 'slug: body' lines, 3 pages max, one per line, no names.",
            recent or "nothing yet", max_tokens=500,
        )
        built = 0
        for line in (res.text or "").splitlines():
            if ":" in line:
                slug, body = line.split(":", 1)
                slug = slug.strip().lower().replace(" ", "-")[:40]
                if slug and body.strip():
                    await store.set_wiki(db, message.chat.id, slug, body.strip()[:800])
                    built += 1
        await message.reply(f"wiki built: {built} page(s)." if built else f"wiki build gave nothing ({res.reason or 'no output'}).")
        return
    page = await store.get_wiki(db, message.chat.id, arg.lower())
    await message.reply(page["body"] if page else f"no wiki page '{arg}'.")


# ---------------------------------------------------------------- smart slow mode
class SlowState:
    """Per-group rolling state: last text and a burst counter."""
    def __init__(self) -> None:
        self.last: dict[tuple[int, int], str] = {}
        self.burst: dict[int, list[float]] = {}


SLOW = SlowState()


def slow_check(chat_id: int, user_id: int, text: str, now: float | None = None) -> str:
    """Return 'ok', 'ai' (borderline, AI-assist if configured) or 'delete'. Pure heuristic first."""
    now = now or time.time()
    times = [t for t in SLOW.burst.get(chat_id, []) if now - t < 10] + [now]
    SLOW.burst[chat_id] = times
    same = SLOW.last.get((chat_id, user_id)) == text
    SLOW.last[(chat_id, user_id)] = text
    score = slow_score(text, same_as_last=same, burst=len(times))
    return slow_decision(score)
