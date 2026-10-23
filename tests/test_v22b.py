"""v2.2b slice 1 tests: timed-action store, expiry lifts, captcha kick window. SQLite, no network."""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from app.db import Database
from app.fillings import parse_duration
from app.handlers import rose_b as rb


class FakeBot:
    def __init__(self):
        self.calls = []

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=False):
        self.calls.append(("unban", chat_id, user_id))

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self.calls.append(("ban", chat_id, user_id))

    async def restrict_chat_member(self, chat_id, user_id, perms, **kw):
        self.calls.append(("restrict", chat_id, user_id))


@pytest.fixture()
def db():
    path = os.path.join(tempfile.mkdtemp(), "b1.db")
    d = Database(f"sqlite+aiosqlite:///{path}")
    asyncio.run(d.init())
    asyncio.run(d.ensure_group(-300, "b1 test"))
    yield d
    asyncio.run(d.close())


def run(coro):
    return asyncio.run(coro)


def test_parse_duration_shapes():
    assert parse_duration("4m") == 240
    assert parse_duration("3h") == 3 * 3600
    assert parse_duration("6d") == 6 * 86400
    assert parse_duration("5w") == 5 * 604800
    assert parse_duration("0m") is None
    assert parse_duration("99x") is None
    assert parse_duration("") is None


def test_timed_store_roundtrip(db):
    run(rb.add_timed(db, -300, 7, "tban", 1000))
    assert run(rb.due_timed(db, now=999)) == []
    assert run(rb.due_timed(db, now=1000)) == [{"chat_id": -300, "user_id": 7, "kind": "tban"}]
    run(rb.remove_timed(db, -300, 7, "tban"))
    assert run(rb.due_timed(db, now=2000)) == []


def test_expire_lifts_due_ban_and_mute(db):
    bot = FakeBot()
    run(rb.add_timed(db, -300, 1, "tban", 100))
    run(rb.add_timed(db, -300, 2, "tmute", 100))
    run(rb.add_timed(db, -300, 3, "tban", 9999))
    done = run(rb.expire_timed_actions(bot, db, now=500))
    assert done == 2
    assert ("unban", -300, 1) in bot.calls
    assert ("restrict", -300, 2) in bot.calls
    assert ("unban", -300, 3) not in bot.calls
    # the one not yet due is still stored
    assert run(rb.due_timed(db, now=10000)) == [{"chat_id": -300, "user_id": 3, "kind": "tban"}]


def test_captcha_kick_respects_group_window(db):
    bot = FakeBot()
    run(db.exec("INSERT INTO captchas (chat_id, user_id, created_ts) VALUES (-300, 5, 1000)"))
    run(db.update_group(-300, captcha_kick_secs=600))
    assert run(rb.expire_unsolved_captchas(bot, db, now=1500)) == 0  # 500s in, window 600
    assert run(rb.expire_unsolved_captchas(bot, db, now=1700)) == 1  # 700s in
    assert ("ban", -300, 5) in bot.calls
    assert run(db.one("SELECT user_id FROM captchas WHERE user_id = 5")) is None
