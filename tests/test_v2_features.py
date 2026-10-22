"""Tests for the V2 pure logic (features.py): trivia, slow mode, FAQ, memory, appeals, capsules, DSN, heat map."""
from datetime import datetime, timezone

from app.features import (
    answer_matches, parse_trivia_output, slow_score, slow_decision, faq_best_match,
    is_question, parse_memory_output, parse_verdict, parse_capsule_when, validate_dsn,
    mask_dsn, render_heat, within_cap, est_cost_cents, PERSONA_PRESETS, group_health,
)


def test_trivia_answer_matching():
    assert answer_matches("Jupiter!", "jupiter")
    assert answer_matches("the moon", "the moon")
    assert answer_matches("it was in 1969", "1969")
    assert answer_matches("six", "six|6")
    assert not answer_matches("mars", "jupiter")
    assert not answer_matches("", "jupiter")


def test_trivia_parse_output():
    assert parse_trivia_output("Q: What is 2+2?\nA: 4") == ("What is 2+2?", "4")
    assert parse_trivia_output("no format here") is None


def test_slow_mode_flags_noise_not_conversation():
    assert slow_decision(slow_score("k")) == "ai"  # borderline goes to the AI-assist flag, never straight delete
    assert slow_decision(slow_score("AAAAAAAAAAAAAA!!!!!", same_as_last=True)) == "delete"
    long_chat = "i think we should move the meeting to thursday because the venue is free then and most people said yes"
    assert slow_decision(slow_score(long_chat)) == "ok"


def test_faq_detection_and_match():
    assert is_question("what time does the event start tonight?")
    assert not is_question("lol nice")
    faqs = [{"question": "what time does the event start", "answer": "7pm utc"}]
    hit = faq_best_match("when does the event start, what time?", faqs)
    assert hit and hit["answer"] == "7pm utc"
    assert faq_best_match("how do i bake bread", faqs) is None
    assert faq_best_match("what time does the event start", [{"question": "what time does the event start", "answer": ""}]) is None


def test_memory_parsing_dedupes_and_limits():
    out = "DECISION: meet thursdays\nFACT: venue is the library\nDECISION: meet thursdays\nrandom chatter\nSLANG: 'bread' means money"
    assert parse_memory_output(out) == [("decision", "meet thursdays"), ("fact", "venue is the library"), ("slang", "'bread' means money")]
    assert parse_memory_output("NONE") == []


def test_verdict_defaults_to_uphold_when_unclear():
    assert parse_verdict("OVERTURN: the ban was a mix up")[0] == "overturn"
    assert parse_verdict("UPHOLD: repeated spam")[0] == "uphold"
    assert parse_verdict("maybe?") [0] == "uphold"
    assert parse_verdict("")[0] == "uphold"


def test_capsule_timezone_and_past_rejection():
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc).timestamp()
    ts = parse_capsule_when("2026-10-25 09:00", "Africa/Lagos", now)
    assert ts == int(datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc).timestamp())
    assert parse_capsule_when("2026-10-20 09:00", "UTC", now) is None
    assert parse_capsule_when("+3h", "UTC", now) == int(now) + 3 * 3600
    assert parse_capsule_when("nonsense", "UTC", now) is None


def test_dsn_validation_and_masking():
    ok, _ = validate_dsn("postgresql://u:pw@db.example.com:5432/mine?sslmode=require")
    assert ok
    assert not validate_dsn("mysql://u:p@h/db")[0]
    assert not validate_dsn("postgresql://u:p@h/")[0]
    main = "postgresql+asyncpg://u:p@neon.host/neondb"
    assert not validate_dsn("postgresql://u:p@neon.host/neondb", main)[0]
    masked = mask_dsn("postgresql://alice:s3cret@db.example.com:5432/mine?sslmode=require")
    assert "s3cret" not in masked and "***" in masked and "alice" in masked


def test_spend_cap_math():
    assert within_cap(100, 5, 0)  # 0 means unlimited
    assert within_cap(90, 5, 100)
    assert not within_cap(98, 5, 100)
    assert est_cost_cents("a" * 400, "b" * 400, 100, 0.1) > 0


def test_heat_map_and_health_and_presets():
    assert render_heat({}).startswith("no activity")
    assert "09:00" in render_heat({9: 4})
    assert group_health({"key_enc": "x"}, "expired").startswith("EXPIRED")
    assert set(PERSONA_PRESETS) == {"study", "trading", "gaming"}
