"""Unit tests for the pure logic: encryption, key masking, lock windows, anti-farm, link/blacklist checks."""
from datetime import datetime

import pytest

from app.crypto import KeyBox
from app.db import normalize_database_url
from app.services import (
    blacklist_hit,
    has_link,
    in_daily_window,
    lock_should_be_active,
    looks_like_key,
    mask_key,
    parse_lock_text,
)


def test_encrypt_roundtrip_and_no_plaintext():
    box = KeyBox("x" * 32)
    token = box.encrypt("sk-abcdefghijklmnopqrstuvwxyz1234")
    assert "sk-abcdef" not in token
    assert box.decrypt(token) == "sk-abcdefghijklmnopqrstuvwxyz1234"


def test_wrong_secret_cannot_decrypt():
    token = KeyBox("a" * 32).encrypt("sk-abcdefghijklmnopqrstuvwx")
    with pytest.raises(ValueError):
        KeyBox("b" * 32).decrypt(token)


def test_mask_shows_only_prefix_and_last4():
    assert mask_key("sk-abcdefghijklmnopqrst1234") == "sk-…1234"


def test_key_shape_detection():
    assert looks_like_key("sk-abcdefghijklmnopqrstuv")
    assert not looks_like_key("hello there")


def test_daily_window_crosses_midnight():
    n = lambda h, m=0: datetime(2026, 10, 22, h, m)
    assert in_daily_window(n(23), "22:00", "06:00")
    assert in_daily_window(n(3), "22:00", "06:00")
    assert not in_daily_window(n(12), "22:00", "06:00")


def test_daily_window_same_day():
    assert in_daily_window(datetime(2026, 1, 1, 10, 30), "10:00", "11:00")
    assert not in_daily_window(datetime(2026, 1, 1, 11, 0), "10:00", "11:00")


def test_parse_lock_text_daily_and_once():
    assert parse_lock_text("daily 22:00 06:00") == {"kind": "daily", "start_hm": "22:00", "end_hm": "06:00"}
    once = parse_lock_text("once 2026-10-25 09:00 11:00")
    assert once["kind"] == "once" and once["once_end"] > once["once_start"]
    assert parse_lock_text("once 2026-10-25 11:00 09:00") is None
    assert parse_lock_text("sometimes") is None


def test_once_lock_is_active_only_inside_window():
    lock = {"kind": "once", "enabled": 1, "once_start": 100, "once_end": 200}
    assert lock_should_be_active(lock, "UTC", now_ts=150)
    assert not lock_should_be_active(lock, "UTC", now_ts=250)


def test_link_detection():
    assert has_link("check https://spam.example")
    assert has_link("join t.me/somegroup")
    assert not has_link("i love python")


def test_blacklist_whole_word_only():
    assert blacklist_hit("this is a scam", "scam,spam") == "scam"
    assert blacklist_hit("scampi for dinner", "scam") is None


def test_database_url_normalisation():
    assert normalize_database_url("postgres://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert normalize_database_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert normalize_database_url("sqlite+aiosqlite:///x.db").startswith("sqlite+aiosqlite")
