"""v2.2 tests: pure helpers, i18n, and the store/db helpers (SQLite, no network)."""
import asyncio
import json
import os
import tempfile

import pytest

from app import rose_store as rs
from app.db import Database
from app.i18n import LANGS, STRINGS, normalize_lang, t


@pytest.fixture()
def db():
    path = os.path.join(tempfile.mkdtemp(), "v22.db")
    d = Database(f"sqlite+aiosqlite:///{path}")
    asyncio.run(d.init())
    asyncio.run(d.ensure_group(-200, "v22 test"))
    yield d
    asyncio.run(d.close())


def run(coro):
    return asyncio.run(coro)


# ---- pure helpers
def test_clean_note_name_rules():
    assert rs.clean_note_name("#Rules") == "rules"
    assert rs.clean_note_name("bad name!") is None
    assert rs.clean_note_name("") is None
    assert rs.clean_note_name("x" * 33) is None


def test_clean_command_strips_slash_and_bot_suffix():
    assert rs.clean_command("/Ban@zephyguardbot") == "ban"
    assert rs.clean_command("ban") == "ban"
    assert rs.clean_command("ba n") is None


def test_csv_set_roundtrip():
    assert rs.csv_set("a, b,,a") == {"a", "b"}
    assert rs.csv_join(["b", "a", "a"]) == "a,b"
    assert rs.csv_join([]) == ""


def test_filter_match_is_whole_word():
    assert rs.filter_match("well HELLO friend", "hello")
    assert not rs.filter_match("concatenate it", "cat")
    assert rs.filter_match("my cat is cute", "cat")


def test_lang_normalization_and_coverage():
    assert normalize_lang("ES") == "es"
    assert normalize_lang("xx") == "en"
    assert normalize_lang(None) == "en"
    assert set(LANGS) == {"en", "es", "fr", "pt", "hi", "ar"}


def test_i18n_translates_and_falls_back():
    assert t("fr", "warn", name="x", count=1, limit=3, reason="r").startswith("⚠️ x")
    assert t(None, "done", action="ban", name="bob") == "done: ban bob."
    for lang in LANGS:
        assert t(lang, "help")


def test_every_language_covers_core_strings():
    core = {"help", "warn", "done", "flood"}
    for lang, table in STRINGS.items():
        assert core <= set(table), (lang, core - set(table))


def test_is_flooding_respects_limit():
    from app.moderation import is_flooding, _flood
    _flood.clear()
    results = [is_flooding(-300, 5, limit=2, now=1000.0 + i * 0.1) for i in range(4)]
    assert results == [False, False, True, True]
    _flood.clear()


def test_parse_import_rejects_bad_json_and_version():
    assert rs.parse_import("not json")[0] is None
    assert rs.parse_import(json.dumps({"version": 99}))[0] is None


def test_parse_import_drops_unknown_keys():
    raw = json.dumps({"version": rs.EXPORT_VERSION, "settings": {"evil": 1, "lang": "es", "antiflood_limit": 9}})
    payload, err = rs.parse_import(raw)
    assert err == ""
    assert "evil" not in payload["settings"]
    assert payload["settings"]["lang"] == "es"
    assert payload["settings"]["antiflood_limit"] == 9


def test_parse_import_rejects_bad_lang_and_action():
    raw = json.dumps({"version": rs.EXPORT_VERSION,
                      "settings": {"lang": "klingon", "antiflood_action": "explode"}})
    payload, _ = rs.parse_import(raw)
    assert "lang" not in payload["settings"]
    assert "antiflood_action" not in payload["settings"]


# ---- store helpers
def test_filter_set_list_delete(db):
    run(rs.set_filter(db, -200, "hello", "hi there"))
    run(rs.set_filter(db, -200, "bye", "see ya"))
    rows = run(rs.list_filters(db, -200))
    assert [r["keyword"] for r in rows] == ["bye", "hello"]
    assert run(rs.find_filter_reply(db, -200, "well HELLO friend")) == "hi there"
    assert run(rs.find_filter_reply(db, -200, "nothing here")) is None
    assert run(rs.del_filter(db, -200, "hello")) is True
    assert run(rs.del_filter(db, -200, "hello")) is False


def test_notes_save_get_list_delete(db):
    run(rs.save_note(db, -200, "rules", "be nice"))
    run(rs.save_note(db, -200, "faq", "ask away"))
    assert run(rs.get_note(db, -200, "rules")) == "be nice"
    assert run(rs.list_notes(db, -200)) == ["faq", "rules"]
    assert run(rs.del_note(db, -200, "rules")) is True
    assert run(rs.get_note(db, -200, "rules")) is None


def test_disabled_commands_roundtrip(db):
    run(db.update_group(-200, disabled_cmds=""))
    assert rs.csv_set((run(db.get_group(-200)) or {}).get("disabled_cmds", "")) == set()
    run(db.update_group(-200, disabled_cmds=rs.csv_join(["ask", "lb"])))
    assert rs.csv_set(run(db.get_group(-200))["disabled_cmds"]) == {"ask", "lb"}


def test_export_import_roundtrip(db):
    run(db.update_group(-200, lang="fr", antiflood_limit=9))
    run(rs.set_filter(db, -200, "hello", "hi"))
    run(rs.save_note(db, -200, "rules", "be nice"))
    blob = run(rs.export_settings(db, -200))
    assert isinstance(blob, (dict, str))
    text = blob if isinstance(blob, str) else json.dumps(blob)
    data = json.loads(text)
    assert data["settings"]["lang"] == "fr"
    assert data["settings"]["antiflood_limit"] == 9
    assert "api_key" not in text.lower() and "dsn" not in text.lower()
    run(db.update_group(-200, lang="en", antiflood_limit=6))
    payload, err = rs.parse_import(text)
    assert err == ""
    run(rs.import_settings(db, -200, payload))
    assert run(db.get_group(-200))["lang"] == "fr"
    assert run(db.get_group(-200))["antiflood_limit"] == 9


def test_log_channel_roundtrip(db):
    run(db.update_group(-200, log_chat_id=-100555))
    assert run(db.get_group(-200))["log_chat_id"] == -100555
    run(db.update_group(-200, log_chat_id=0))
    assert run(db.get_group(-200))["log_chat_id"] == 0


def test_pending_approval_expiry(db):
    run(rs.add_pending_join(db, -200, 77, now=1000))
    assert run(rs.expired_pending_joins(db, now=1000 + 100)) == []
    expired = run(rs.expired_pending_joins(db, now=1000 + rs.APPROVAL_TIMEOUT_S + 1))
    assert [(r["chat_id"], r["user_id"]) for r in expired] == [(-200, 77)]
    assert run(rs.remove_pending_join(db, -200, 77)) is True
    assert run(rs.remove_pending_join(db, -200, 77)) is False
