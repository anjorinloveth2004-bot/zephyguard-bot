"""Tests for web-search degradation and group timezone handling."""
import asyncio

from app.config import Settings
from app.websearch import provider_name, search
from app.services import _tz


def _cfg(**kw) -> Settings:
    base = dict(
        bot_token="x", bot_username="b", encryption_key="k", super_admin_ids=frozenset(),
        database_url="sqlite+aiosqlite:///:memory:",
    )
    base.update(kw)
    return Settings(**base)


def test_no_search_key_means_no_provider():
    assert provider_name(_cfg()) == ""
    assert asyncio.run(search(_cfg(), "weather lagos")) == []


def test_provider_priority():
    assert provider_name(_cfg(brave_api_key="b", serper_api_key="s")) == "serper"
    assert provider_name(_cfg(tavily_api_key="t", serper_api_key="s")) == "tavily"


def test_group_timezone_fallback_is_utc():
    assert _tz("").key == "UTC"
    assert _tz("Not/AZone").key == "UTC"
    assert _tz("Africa/Lagos").key == "Africa/Lagos"
