"""v2.2b slices 2 and 3 tests: pure helpers, log categories, help grid, store helpers (SQLite, no network)."""
import asyncio
import os
import tempfile

import pytest

from app import rose_store as rs
from app.db import Database
from app.handlers import rose_c as rc


@pytest.fixture()
def db():
    path = os.path.join(tempfile.mkdtemp(), "v22b_rose.db")
    d = Database(f"sqlite+aiosqlite:///{path}")
    asyncio.run(d.init())
    asyncio.run(d.ensure_group(-300, "rose test"))
    yield d
    asyncio.run(d.close())


def run(coro):
    return asyncio.run(coro)


def test_onoff_parses_both_words_and_rejects_junk():
    assert rc._onoff("on") == 1
    assert rc._onoff("OFF") == 0
    assert rc._onoff("maybe") is None
    assert rc._onoff("") is None


def test_help_grid_has_one_button_per_module_and_back_target():
    text, kb = rc.help_grid()
    assert "zephy" in text.lower()
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    for key in rc.HELP_MODULES:
        assert f"hmod:{key}" in datas
    assert all(len(d.encode()) <= 64 for d in datas)


def test_log_category_routes_lines_to_the_right_bucket():
    grp = {"log_cats": "bans,warns"}
    assert rc_log(grp, "ban 123 (spam)") is True
    assert rc_log(grp, "mute 123 for 1h") is False
    assert rc_log(grp, "warn 123: rude") is True
    assert rc_log(grp, "note saved: rules") is False
    # unknown lines always log
    assert rc_log(grp, "something new happened") is True


def rc_log(grp, text):
    from app import moderation
    return moderation._log_cat_enabled(grp, text)


def test_filter_roundtrip(db):
    async def go():
        await rs.set_filter(db, -300, "hello", "hi there")
        assert await rs.find_filter_reply(db, -300, "well hello friend") == "hi there"
        assert await rs.find_filter_reply(db, -300, "hellothere") is None
        assert await rs.del_filter(db, -300, "hello") is True
        assert await rs.find_filter_reply(db, -300, "hello") is None
    run(go())


def test_note_roundtrip(db):
    async def go():
        await rs.save_note(db, -300, "rules", "be nice")
        assert await rs.get_note(db, -300, "rules") == "be nice"
        assert "rules" in await rs.list_notes(db, -300)
        assert await rs.del_note(db, -300, "rules") is True
    run(go())


def test_clean_note_name_and_command_are_strict():
    assert rs.clean_note_name("#Rules") == "rules"
    assert rs.clean_note_name("bad name!!") is None
    assert rs.clean_command("/Start@zephyguardbot") == "start"
    assert rs.clean_command("bad command!!") is None


def test_export_then_parse_import_roundtrip(db):
    async def go():
        import json
        payload = await rs.export_settings(db, -300)
        parsed, err = rs.parse_import(json.dumps(payload))
        assert parsed is not None and err == ""
    run(go())


def test_parse_import_rejects_garbage():
    parsed, err = rs.parse_import("not json at all")
    assert parsed is None and err
