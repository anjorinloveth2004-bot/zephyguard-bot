"""Item 6 tests: heat map, group health, battle tie-break."""
from app.handlers.admin import group_health, heat_bars
from app.handlers.v2 import battle_winner


def test_heat_bars_peak_and_empty():
    assert heat_bars({}) == "no calls yet"
    out = heat_bars({3: 10, 4: 5})
    assert out.startswith("00h ") and "(peak 10)" in out and len(out[4:out.index(" 23h")]) == 24


def test_group_health_states():
    assert group_health({"banned": 1}) == "banned"
    assert group_health({"banned": 0, "key_enc": "", "fallback_key_enc": ""}).startswith("no key")
    assert group_health({"banned": 0, "key_enc": "x"}) == "ok"


def test_battle_winner():
    assert battle_winner(3, 1) == "a"
    assert battle_winner(1, 3) == "b"
    assert battle_winner(2, 2) == "a"
