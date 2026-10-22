"""DM settings navigation and persona/prompt tests (no Telegram)."""
import asyncio
import os
import tempfile

import pytest

from app.db import Database
from app.features import PERSONA_PRESETS
from app.handlers import settings as st


@pytest.fixture()
def db():
    path = os.path.join(tempfile.mkdtemp(), "s.db")
    d = Database(f"sqlite+aiosqlite:///{path}")
    asyncio.run(d.init())
    asyncio.run(d.ensure_group(-200, "s"))
    yield d
    asyncio.run(d.close())


def _grp(db):
    return asyncio.run(db.get_group(-200))


def test_persona_screen_has_every_preset_and_back():
    text, kb = st.persona_screen(-200, {"personality": "dry"})
    labels = [b.text for row in kb.inline_keyboard for b in row]
    for name in PERSONA_PRESETS:
        assert any(name in lbl for lbl in labels)
    assert any(lbl.endswith("back") for lbl in labels)
    assert "dry" in text


def test_prompt_screen_back_goes_to_persona():
    _, kb = st.prompt_screen(-200, {"custom_prompt": ""})
    backs = [b.callback_data for row in kb.inline_keyboard for b in row if "back" in b.text]
    assert backs == ["st:-200:persona"]


def test_custom_prompt_saved_and_capped(db):
    class Msg:
        def __init__(self, text):
            self.text = text
            self.replies = []
        async def answer(self, t):
            self.replies.append(t)
    ok, long = Msg("be terse"), Msg("x" * (st.PROMPT_MAX + 1))
    asyncio.run(db.set_dm_state(1, "prompt", -200))
    asyncio.run(st.receive_custom_prompt(ok, db, -200, 1))
    assert _grp(db)["custom_prompt"] == "be terse"
    asyncio.run(st.receive_custom_prompt(long, db, -200, 1))
    assert _grp(db)["custom_prompt"] == "be terse"  # over-cap text did not overwrite
    assert "too long" in long.replies[0]


def test_preset_names_are_real_keys():
    assert {"study", "trading", "gaming"} <= set(PERSONA_PRESETS)
