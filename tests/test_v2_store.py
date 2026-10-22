"""V2 store and guarded-AI tests (SQLite, no network)."""
import asyncio
import os
import tempfile

import pytest

from app import store
from app.db import Database
from app.features import answer_matches, within_cap, est_cost_cents
from app.ai import _hour


@pytest.fixture()
def db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    d = Database(f"sqlite+aiosqlite:///{path}")
    asyncio.run(d.init())
    asyncio.run(d.ensure_group(-100, "t"))
    yield d
    asyncio.run(d.close())


def run(coro):
    return asyncio.run(coro)


def test_within_cap_zero_is_unlimited():
    assert within_cap(999, 5, 0)


def test_within_cap_blocks_over_limit():
    assert within_cap(90, 5, 100)
    assert not within_cap(98, 5, 100)


def test_est_cost_scales_with_tokens():
    small = est_cost_cents("a", "b", 10, 0.5)
    big = est_cost_cents("a" * 4000, "b", 10, 0.5)
    assert big > small > 0


def test_answer_matches_ignores_articles():
    assert answer_matches("the paris", "Paris")
    assert not answer_matches("london", "Paris")


def test_spend_accumulates(db):
    run(store.add_spend(db, -100, 1.5))
    run(store.add_spend(db, -100, 2.0))
    assert run(store.get_spend(db, -100)) == pytest.approx(3.5)


def test_heat_counts_by_chat(db):
    run(store.bump_heat(db, -100, 3))
    run(store.bump_heat(db, -100, 3))
    counts = run(store.heat_counts(db, -100))
    assert sum(counts.values()) == 2


def test_hour_falls_back_on_bad_zone():
    assert 0 <= _hour("Not/AZone") <= 23
    assert 0 <= _hour(None) <= 23
