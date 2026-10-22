"""v2.1 tests: DSN validation, PG schema compat, battle/federation logic."""
import asyncio
import json
import os
import tempfile

import pytest

from app import db as dbmod
from app import federation
from app.db import Database, pg_compat, SCHEMA
from app.features import validate_dsn, mask_dsn


@pytest.fixture()
def bdb():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    d = Database(f"sqlite+aiosqlite:///{path}")
    asyncio.run(d.init())
    asyncio.run(d.ensure_group(-100, "a"))
    asyncio.run(d.ensure_group(-200, "b"))
    yield d
    asyncio.run(d.close())


def test_pg_compat_rewrites_only_for_postgres():
    stmt = "CREATE TABLE x (id INTEGER PRIMARY KEY AUTOINCREMENT)"
    assert "SERIAL PRIMARY KEY" in pg_compat(stmt, "postgresql")
    assert "AUTOINCREMENT" in pg_compat(stmt, "sqlite")


def test_schema_has_battle_tables():
    joined = "\n".join(SCHEMA)
    for t in ("battles", "battle_claims", "battle_votes", "fed_invites"):
        assert f"CREATE TABLE IF NOT EXISTS {t}" in joined


def test_validate_dsn_rejects_bad_and_masks():
    ok, _ = validate_dsn("postgresql://u:p@db.example.com:5432/app?sslmode=require")
    assert ok
    ok, msg = validate_dsn("mysql://u:p@h/db")
    assert not ok and "postgresql" in msg
    assert "p@" not in mask_dsn("postgresql://u:secret@db.example.com:5432/app")


def test_invite_accept_links_two_groups(bdb):
    code = asyncio.run(federation.create_invite(bdb, -100))
    ok, fed = asyncio.run(federation.accept_invite(bdb, code, -200))
    assert ok
    assert asyncio.run(bdb.get_group(-200))["federation_id"] == fed


def test_invite_cannot_be_accepted_by_own_group(bdb):
    code = asyncio.run(federation.create_invite(bdb, -100))
    ok, msg = asyncio.run(federation.accept_invite(bdb, code, -100))
    assert not ok


def test_bad_invite_code(bdb):
    ok, _ = asyncio.run(federation.accept_invite(bdb, "fed-nope00", -200))
    assert not ok


def test_battle_scoring_flow(bdb):
    async def go():
        await federation.create_invite(bdb, -100)
        code = (await bdb.one("SELECT code FROM fed_invites LIMIT 1"))["code"]
        await federation.accept_invite(bdb, code, -200)
        bid = await federation.start_battle(bdb, -100, -200)
        assert bid is not None
        cur = await federation.current_question(bdb, bid)
        assert cur["qi"] == 0
        # wrong answer earns nothing
        assert not await federation.claim_battle_answer(bdb, bid, -100, 1, "nonsense xyz", 0, cur["a"])
        # right answer from group a claims and scores for a
        assert await federation.claim_battle_answer(bdb, bid, -100, 1, cur["a"].split("|")[0], 0, cur["a"])
        await federation.record_battle_point(bdb, bid, -100, 1, "u", "alice")
        # second correct answer in the same group for the same question must not claim again
        assert not await federation.claim_battle_answer(bdb, bid, -100, 2, cur["a"].split("|")[0], 0, cur["a"])
        # group b can still claim the same question
        assert await federation.claim_battle_answer(bdb, bid, -200, 3, cur["a"].split("|")[0], 0, cur["a"])
        await federation.record_battle_point(bdb, bid, -200, 3, "v", "bob")
        res = await federation.finish_battle(bdb, bid)
        return res
    res = asyncio.run(go())
    assert res["score_a"] == federation.BATTLE_POINTS and res["score_b"] == federation.BATTLE_POINTS
    assert res["winner"] == "tie"
    pts = asyncio.run(bdb.one("SELECT score FROM points WHERE chat_id = -100 AND user_id = 1"))
    assert int(pts["score"]) == federation.BATTLE_POINTS


def test_battle_result_text_mentions_winner():
    assert "group a wins" in federation.results_text({"winner": "a", "score_a": 10, "score_b": 5})
    assert "tie" in federation.results_text({"winner": "tie", "score_a": 5, "score_b": 5})


def test_fallback_base_url_validation():
    from app.handlers.fallback import valid_base_url
    assert valid_base_url("https://api.groq.com/openai/v1/")[1] == "https://api.groq.com/openai/v1"
    assert not valid_base_url("ftp://x.com")[0]
    assert not valid_base_url("http://example.com/v1")[0]
    assert valid_base_url("http://localhost:11434/v1")[0]
    assert not valid_base_url("")[0]


def test_byod_refuses_when_disabled_and_bad_dsn(bdb):
    from app import byod
    from app.crypto import KeyBox
    keys = KeyBox("test-secret")
    ok, msg = asyncio.run(byod.connect_group(bdb, keys, -100, "postgresql://u:p@h/db", "", enabled=False))
    assert not ok and "off" in msg
    ok, msg = asyncio.run(byod.connect_group(bdb, keys, -100, "mysql://u:p@h/db", "", enabled=True))
    assert not ok


def test_byod_pool_none_without_dsn(bdb):
    from app import byod
    assert asyncio.run(byod.pool_for(bdb, None, -100)) is None


def test_byod_failures_pause_after_threshold():
    from app import byod
    class FakeBot:
        def __init__(self): self.sent = []
        async def send_message(self, chat, text): self.sent.append(text)
    byod.POOLS[-555] = byod.GroupPool(url="x")
    bot = FakeBot()
    for _ in range(byod.MAX_FAILURES):
        asyncio.run(byod.report_failure(-555, bot, RuntimeError("boom")))
    assert byod.POOLS[-555].paused and len(bot.sent) == 1
    byod.reset_pool(-555)


def test_keybox_roundtrip_for_byod_dsn():
    from app.crypto import KeyBox
    kb = KeyBox("test-secret")
    enc = kb.encrypt("postgresql://u:pw@h/db")
    assert "pw" not in enc and kb.decrypt(enc) == "postgresql://u:pw@h/db"


def test_invite_links_then_resolves_peer(bdb):
    code = asyncio.run(federation.create_invite(bdb, -100))
    ok, info = asyncio.run(federation.accept_invite(bdb, code, -200))
    assert ok, info
    assert asyncio.run(federation.fed_peer(bdb, -200)) == -100


def test_battle_scores_once_per_group_and_aggregates(bdb):
    asyncio.run(federation.create_invite(bdb, -100))
    code = asyncio.run(bdb.all("SELECT code FROM fed_invites"))[0]["code"]
    asyncio.run(federation.accept_invite(bdb, code, -200))
    bid = asyncio.run(federation.start_battle(bdb, -100, -200))
    assert bid
    q = asyncio.run(federation.current_question(bdb, bid))
    assert q is not None
    # both groups answer correctly; each group should score once for this question
    assert asyncio.run(federation.claim_battle_answer(bdb, bid, -100, 1, q["a"].split("|")[0], q["qi"], q["a"]))
    assert not asyncio.run(federation.claim_battle_answer(bdb, bid, -100, 2, q["a"].split("|")[0], q["qi"], q["a"]))
    assert asyncio.run(federation.claim_battle_answer(bdb, bid, -200, 3, q["a"].split("|")[0], q["qi"], q["a"]))
