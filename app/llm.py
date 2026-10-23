"""Provider-agnostic LLM client: OpenAI-compatible APIs and Google Gemini.

Each group stores its own provider/model/encrypted key (BYOK). An optional
operator-paid pooled key can serve free-tier groups under daily caps.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

PROVIDERS = {"openai", "gemini"}


class LLMError(Exception):
    pass


@dataclass
class LLMTarget:
    provider: str          # "openai" or "gemini"
    model: str
    api_key: str
    base_url: str = "https://api.openai.com/v1"  # OpenAI-compatible base (also Groq, OpenRouter, Ollama, ...)


def resolve_model(provider: str, model: str) -> str:
    """Map 'auto'/empty to a sensible default per provider."""
    m = (model or "").strip().lower()
    if m and m != "auto":
        return model
    return "gemini-flash-latest" if provider == "gemini" else "gpt-4o-mini"


async def complete(target: LLMTarget, system: str, user: str, max_tokens: int = 400, timeout: float = 45) -> str:
    """Run one chat completion and return plain text. Raises LLMError on failure."""
    model = resolve_model(target.provider, target.model)
    if model != target.model:
        target = LLMTarget(provider=target.provider, model=model, api_key=target.api_key, base_url=target.base_url)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            if target.provider == "gemini":
                return await _gemini(client, target, system, user, max_tokens)
            return await _openai_compat(client, target, system, user, max_tokens)
        except httpx.HTTPError as exc:
            raise LLMError(f"network error talking to {target.provider}: {exc.__class__.__name__}") from exc


async def validate_key(provider: str, api_key: str, model: str, base_url: str = "https://api.openai.com/v1") -> None:
    """Tiny test call used when an admin connects a key. Raises LLMError if the key is bad."""
    target = LLMTarget(provider=provider, model=model, api_key=api_key, base_url=base_url)
    for attempt in range(2):
        try:
            out = await complete(target, "reply with the single word ok", "ping", max_tokens=5, timeout=20)
            if not out:
                raise LLMError("provider returned an empty response")
            return
        except LLMError as exc:
            # one short retry: transient 5xx / overload should not fail the key
            if attempt == 0 and any(c in str(exc) for c in ("500", "502", "503", "504", "busy", "overloaded")):
                import asyncio as _a
                await _a.sleep(3)
                continue
            raise


async def _openai_compat(client: httpx.AsyncClient, t: LLMTarget, system: str, user: str, max_tokens: int) -> str:
    r = await client.post(
        f"{t.base_url}/chat/completions",
        headers={"Authorization": f"Bearer {t.api_key}"},
        json={
            "model": t.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.4,
        },
    )
    if r.status_code == 404 and t.model != "gemini-flash-latest":
        # model name not available for this key; retry the evergreen alias once
        retry = LLMTarget(provider=t.provider, model="gemini-flash-latest", api_key=t.api_key, base_url=t.base_url)
        return await _gemini(client, retry, system, user, max_tokens)
    if r.status_code != 200:
        raise LLMError(_provider_error(r))
    data = r.json()
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("unexpected response shape from provider") from exc


async def _gemini(client: httpx.AsyncClient, t: LLMTarget, system: str, user: str, max_tokens: int) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{t.model}:generateContent"
    r = await client.post(
        url,
        headers={"x-goog-api-key": t.api_key, "Content-Type": "application/json"},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.4},
        },
    )
    if r.status_code == 404 and t.model != "gemini-flash-latest":
        # model name not available for this key; retry the evergreen alias once
        retry = LLMTarget(provider=t.provider, model="gemini-flash-latest", api_key=t.api_key, base_url=t.base_url)
        return await _gemini(client, retry, system, user, max_tokens)
    if r.status_code != 200:
        raise LLMError(_provider_error(r))
    data = r.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("unexpected response shape from Gemini") from exc


def _provider_error(r: httpx.Response) -> str:
    """Short, key-free error text. Never echo request headers or the key."""
    code = r.status_code
    if code in (401, 403):
        return "the provider rejected this key (unauthorized)"
    if code == 429:
        return "the provider rate limit or quota was hit"
    if code == 404:
        return "model or endpoint not found (check the model name)"
    if code in (500, 502, 503, 504):
        return f"provider is busy or overloaded right now (HTTP {code}). this is on the provider, not the key. try again in a few minutes"
    return f"provider error (HTTP {code})"
