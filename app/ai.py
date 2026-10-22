"""Single guarded entry point for every LLM call in the bot.

Order of checks, all enforced here so no call path can skip them:
1. the group's monthly spend cap (cents) from the group row; 0 means unlimited
2. the group's own BYOK key -> then the operator free tier (capped) -> then nothing
3. an optional per-group fallback model (OpenAI-compatible base_url or Gemini) if the primary errors
4. spend and heat accounting after a successful call

Degrades gracefully: any failure returns (None, reason) instead of raising.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app import store
from app.config import Settings
from app.crypto import KeyBox
from app.db import Database
from app.features import est_cost_cents, within_cap
from app.llm import LLMError, LLMTarget, complete
from app.services import FREE_TIER_NOTE, record_ai_call, resolve_llm

log = logging.getLogger(__name__)

CENTS_PER_1K_TOKENS = 0.1  # rough planning estimate for the cap check, not billing


@dataclass
class AIResult:
    text: str | None
    reason: str = ""      # why there is no text, shown to the user only when text is None
    note: str = ""        # e.g. free-tier note


async def fallback_target(db: Database, keys: KeyBox, chat_id: int) -> LLMTarget | None:
    grp = await db.get_group(chat_id)
    if not grp or not grp.get("fallback_provider") or not grp.get("fallback_key_enc"):
        return None
    api_key = keys.decrypt(grp["fallback_key_enc"])
    return LLMTarget(
        provider=grp["fallback_provider"],
        model=grp.get("fallback_model") or "",
        api_key=api_key,
        base_url=grp.get("fallback_base_url") or "https://api.openai.com/v1",
    )


async def ask_ai(
    db: Database, cfg: Settings, keys: KeyBox, chat_id: int,
    system: str, user: str, max_tokens: int = 400,
) -> AIResult:
    """Run one guarded completion. Never raises for provider or cap problems."""
    grp = await db.get_group(chat_id)
    if grp is None:
        return AIResult(None, "this group is not registered yet")
    custom = (grp.get("custom_prompt") or "").strip()
    if custom:
        system = f"{system}\nadmin instructions (follow unless unsafe): {custom[:1200]}"

    est = est_cost_cents(system, user, max_tokens, CENTS_PER_1K_TOKENS)
    spent = await store.get_spend(db, chat_id)
    cap = float(grp.get("spend_cap_cents") or 0)
    if not within_cap(spent, est, cap):
        return AIResult(None, "this group hit its monthly ai spend cap. an admin can raise it in dm.")

    target, note = await resolve_llm(db, cfg, keys, chat_id)
    if target is None:
        return AIResult(None, note)

    used_pool = note == FREE_TIER_NOTE
    errors: list[str] = []
    out = None
    try:
        out = await complete(target, system, user, max_tokens=max_tokens)
    except LLMError as exc:
        errors.append(str(exc))
        log.info("primary model failed for %s: %s", chat_id, exc)

    if out is None:
        fb = await fallback_target(db, keys, chat_id)
        if fb is not None:
            try:
                out = await complete(fb, system, user, max_tokens=max_tokens)
                used_pool = False
            except LLMError as exc:
                errors.append(str(exc))

    if out is None:
        return AIResult(None, "ai error: " + "; ".join(errors) if errors else "ai returned nothing", note)

    await record_ai_call(db, cfg, chat_id, used_pool=used_pool)
    await store.add_spend(db, chat_id, est)
    await store.bump_heat(db, chat_id, _hour(grp.get("tz")))
    return AIResult(out or "(no answer)", "", note)


def _hour(tz_name: str | None = None) -> int:
    """Local hour (0-23) in the group's timezone, falling back to UTC for unknown zones."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    now = datetime.now(tz=timezone.utc)
    try:
        return now.astimezone(ZoneInfo(tz_name)).hour if tz_name else now.hour
    except (ZoneInfoNotFoundError, ValueError):
        return now.hour
