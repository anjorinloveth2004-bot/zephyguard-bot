"""Optional live web search for /ask answers.

Shared operator-owned keys (env): TAVILY_API_KEY, SERPER_API_KEY, BRAVE_API_KEY.
Priority order is Tavily, then Serper, then Brave. If none is set, web access
degrades to "no web" and the bot answers from the model alone.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


@dataclass
class Source:
    title: str
    url: str
    snippet: str


def provider_name(cfg: Settings) -> str:
    if cfg.tavily_api_key:
        return "tavily"
    if cfg.serper_api_key:
        return "serper"
    if cfg.brave_api_key:
        return "brave"
    return ""


async def search(cfg: Settings, query: str, limit: int = 4) -> list[Source]:
    """Return up to `limit` sources, or [] if no provider is configured or the call fails."""
    name = provider_name(cfg)
    if not name or not query.strip():
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if name == "tavily":
                r = await client.post(
                    "https://api.tavily.com/search",
                    json={"api_key": cfg.tavily_api_key, "query": query[:300], "max_results": limit},
                )
                items = [(x.get("title", ""), x.get("url", ""), x.get("content", "")) for x in r.json().get("results", [])]
            elif name == "serper":
                r = await client.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": cfg.serper_api_key, "Content-Type": "application/json"},
                    json={"q": query[:300], "num": limit},
                )
                items = [(x.get("title", ""), x.get("link", ""), x.get("snippet", "")) for x in r.json().get("organic", [])]
            else:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    headers={"X-Subscription-Token": cfg.brave_api_key, "Accept": "application/json"},
                    params={"q": query[:300], "count": limit},
                )
                items = [(x.get("title", ""), x.get("url", ""), x.get("description", "")) for x in r.json().get("web", {}).get("results", [])]
    except (httpx.HTTPError, ValueError) as exc:
        log.info("web search failed (%s): %s", name, exc.__class__.__name__)
        return []
    return [Source(t, u, s) for t, u, s in items[:limit] if u]


def format_sources(sources: list[Source]) -> str:
    """Numbered source list for the prompt (model cites by number)."""
    return "\n".join(f"[{i}] {s.title} | {s.url}\n    {s.snippet[:300]}" for i, s in enumerate(sources, 1))
