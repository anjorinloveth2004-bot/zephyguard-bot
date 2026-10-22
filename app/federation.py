"""Federation + cross-group /battle trivia.

Two groups link through a short invite code (issued by one admin, accepted by the other).
A battle runs the same trivia questions in both groups. The first correct answer for a
question in EACH group scores that group's points (via the normal points store), and the
battle total is the sum of both groups' battle scores. Everything is per-group toggled and
never crashes the bot if one side is missing.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass

from app import store
from app.db import Database
from app.features import TRIVIA_BANK, answer_matches
from app.services import award

BATTLE_POINTS = 5
BATTLE_QUESTIONS = 3
BATTLE_Q_SECONDS = 90


# ----------------------------------------------------------------- federation links
def new_invite_code() -> str:
    return "fed-" + secrets.token_hex(3)


async def create_invite(db: Database, from_chat: int) -> str:
    grp = await db.get_group(from_chat)
    fed_id = (grp or {}).get("federation_id") or f"f{from_chat}"
    if not (grp or {}).get("federation_id"):
        await store.fed_join(db, fed_id, from_chat)
    code = new_invite_code()
    await db.exec(
        "INSERT INTO fed_invites (code, fed_id, from_chat, ts) VALUES (:c, :f, :g, :t)",
        c=code, f=fed_id, g=from_chat, t=int(time.time()),
    )
    return code


async def accept_invite(db: Database, code: str, to_chat: int) -> tuple[bool, str]:
    row = await db.one("SELECT code, fed_id, from_chat FROM fed_invites WHERE code = :c", c=code.strip().lower())
    if not row:
        return False, "that invite code doesn't exist or was already used."
    if int(row["from_chat"]) == to_chat:
        return False, "that code came from this group. send it to the other group instead."
    await store.fed_join(db, row["fed_id"], to_chat)
    await db.exec("DELETE FROM fed_invites WHERE code = :c", c=row["code"])
    return True, row["fed_id"]


async def shared_board(db: Database, chat_id: int, limit: int = 10) -> list[dict]:
    """Leaderboard across every group in this group's federation (sum of points per user, grouped by chat)."""
    grp = await db.get_group(chat_id)
    if not grp or not grp.get("federation_id"):
        return []
    members = await store.fed_members(db, grp["federation_id"])
    if not members:
        return []
    rows = []
    for c in members:
        rows += await db.all(
            "SELECT chat_id, user_id, name, username, score FROM points WHERE chat_id = :c ORDER BY score DESC LIMIT :n",
            c=c, n=limit,
        )
    rows.sort(key=lambda r: -int(r["score"]))
    return rows[:limit]


# ----------------------------------------------------------------- battles
async def fed_peer(db: Database, chat_id: int) -> int | None:
    grp = await db.get_group(chat_id)
    if not grp or not grp.get("federation_id"):
        return None
    peers = [c for c in await store.fed_members(db, grp["federation_id"]) if c != chat_id]
    return peers[0] if peers else None


async def start_battle(db: Database, chat_a: int, chat_b: int, now: float | None = None) -> int | None:
    """Create a battle between two linked groups. Returns the battle id or None if not linked."""
    now = now or time.time()
    qs = []
    for q, a in TRIVIA_BANK[: max(BATTLE_QUESTIONS, 1)]:
        qs.append({"q": q, "a": a})
    # rotate question set per battle so repeats are rare
    import random
    random.shuffle(qs)
    qs = qs[:BATTLE_QUESTIONS]
    await db.exec(
        "INSERT INTO battles (kind, chat_a, chat_b, status, qi, questions, score_a, score_b, q_deadline, deadline, created_ts) "
        "VALUES ('trivia', :a, :b, 'open', 0, :qs, 0, 0, :qd, :d, :t)",
        a=chat_a, b=chat_b, qs=json.dumps(qs), qd=int(now + BATTLE_Q_SECONDS),
        d=int(now + BATTLE_Q_SECONDS * BATTLE_QUESTIONS + 60), t=int(now),
    )
    row = await db.one(
        "SELECT id FROM battles WHERE chat_a = :a AND chat_b = :b AND status = 'open' ORDER BY id DESC LIMIT 1",
        a=chat_a, b=chat_b,
    )
    return int(row["id"]) if row else None


async def current_question(db: Database, battle_id: int) -> dict | None:
    b = await db.one("SELECT * FROM battles WHERE id = :i", i=battle_id)
    if not b or b["status"] != "open":
        return None
    qs = json.loads(b["questions"])
    if b["qi"] >= len(qs):
        return None
    return {"qi": int(b["qi"]), "q": qs[b["qi"]]["q"], "a": qs[b["qi"]]["a"], "battle": b}


async def claim_battle_answer(db: Database, battle_id: int, chat_id: int, user_id: int, text: str, qi: int, answer: str) -> bool:
    """Return True if this message is the first correct answer for question qi in chat_id."""
    if not answer_matches(text, answer):
        return False
    # Atomic first-claim per (battle, question, group). The primary key keeps this to one winner per side.
    try:
        await db.exec(
            "INSERT INTO battle_claims (battle_id, qi, chat_id, user_id, ts) VALUES (:b, :q, :c, :u, :t)",
            b=battle_id, q=qi, c=chat_id, u=user_id, t=int(time.time()),
        )
    except Exception:
        return False
    return True


async def record_battle_point(db: Database, battle_id: int, chat_id: int, user_id: int, username: str, name: str) -> None:
    """Score a battle point for the group that answered first, both in the battle and on the points store."""
    b = await db.one("SELECT * FROM battles WHERE id = :i", i=battle_id)
    if not b:
        return
    col = "score_a" if int(b["chat_a"]) == chat_id else "score_b"
    await db.exec(f"UPDATE battles SET {col} = {col} + :p WHERE id = :i", p=BATTLE_POINTS, i=battle_id)
    await award(db, chat_id, user_id, BATTLE_POINTS, f"battle:{battle_id}", username, name)


async def advance_question(db: Database, battle_id: int) -> None:
    b = await db.one("SELECT qi FROM battles WHERE id = :i", i=battle_id)
    if not b:
        return
    await db.exec(
        "UPDATE battles SET qi = qi + 1, q_deadline = :d WHERE id = :i",
        i=battle_id, d=int(time.time()) + BATTLE_Q_SECONDS,
    )


async def finish_battle(db: Database, battle_id: int) -> dict | None:
    b = await db.one("SELECT * FROM battles WHERE id = :i", i=battle_id)
    if not b or b["status"] != "open":
        return None
    await db.exec("UPDATE battles SET status = 'done' WHERE id = :i", i=battle_id)
    a, s_a, s_b = int(b["chat_a"]), int(b["score_a"]), int(b["score_b"])
    winner = "tie" if s_a == s_b else ("a" if s_a > s_b else "b")
    return {"id": battle_id, "chat_a": a, "chat_b": int(b["chat_b"]), "score_a": s_a, "score_b": s_b, "winner": winner}


def results_text(result: dict) -> str:
    if result["winner"] == "tie":
        head = "battle over: it's a tie"
    else:
        head = f"battle over: group {'a' if result['winner'] == 'a' else 'b'} wins"
    return f"{head}\n\nscores: a {result['score_a']} | b {result['score_b']} (points go to each group's own board)"
