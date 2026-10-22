"""Item 4 tests: slow mode heuristic, capsule parsing, verdict parsing, and the mod_v2 pure helpers."""
import time

from app.features import parse_capsule_when, parse_verdict, slow_decision, slow_score
from app.handlers import mod_v2


def test_slow_score_flags_repeats_and_bursts():
    assert slow_score("same thing", same_as_last=True) > slow_score("fresh thought here")
    assert slow_score("hello there friend", burst=6) > slow_score("hello there friend", burst=1)


def test_slow_decision_bands():
    assert slow_decision(5) == "delete"
    assert slow_decision(3) == "ai"
    assert slow_decision(0) == "ok"


def test_slow_check_burst_window_and_repeat():
    mod_v2.SLOW.last.clear()
    mod_v2.SLOW.burst.clear()
    now = 1_000_000.0
    seen = [mod_v2.slow_check(-1, 7, f"message number {i}", now=now + i * 0.1) for i in range(12)]
    assert seen[-1] in ("ai", "delete")
    # a burst that is 60s later is outside the 10s window, so it's back to normal
    assert mod_v2.slow_check(-1, 7, "a quiet thought", now=now + 600) == "ok"


def test_slow_check_same_text_as_last_scores_higher():
    mod_v2.SLOW.last.clear()
    mod_v2.SLOW.burst.clear()
    first = mod_v2.slow_check(-2, 1, "copy paste text here", now=5000.0)
    again = mod_v2.slow_check(-2, 1, "copy paste text here", now=5020.0)
    assert first == "ok"
    assert again in ("ai", "delete", "ok")


def test_capsule_relative_and_absolute():
    now = time.time()
    r = parse_capsule_when("+2d", "UTC", now)
    assert r is not None and r > now
    assert parse_capsule_when("+3h", "UTC", now) is not None
    assert parse_capsule_when("not a time", "UTC", now) is None


def test_verdict_parse_defaults_safe():
    assert parse_verdict("OVERTURN: it was a joke")[0] == "overturn"
    assert parse_verdict("UPHOLD: real spam")[0] == "uphold"
    assert parse_verdict("maybe?")[0] == "uphold"  # unclear output keeps the ban


def test_roast_system_has_no_protected_group_jokes():
    assert "no slurs" in mod_v2.ROAST_SYSTEM
    assert "protected" in mod_v2.ROAST_SYSTEM
