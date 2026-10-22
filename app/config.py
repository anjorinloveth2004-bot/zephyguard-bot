"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from app.db import normalize_database_url

load_dotenv()


def _csv_ints(raw: str) -> frozenset[int]:
    out: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return frozenset(out)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    bot_username: str
    encryption_key: str
    super_admin_ids: frozenset[int]
    database_url: str
    # Optional pooled free-tier key (operator-paid), with daily caps.
    pool_provider: str = ""
    pool_api_key: str = ""
    pool_model: str = ""
    pool_daily_calls: int = 500
    pool_group_daily_calls: int = 30
    openai_base_url: str = "https://api.openai.com/v1"
    default_openai_model: str = "gpt-4o-mini"
    default_gemini_model: str = "gemini-2.5-flash"
    timezone_default: str = "UTC"
    # Webhook mode is used when WEBHOOK_BASE_URL is set; otherwise long polling.
    webhook_base_url: str = ""
    webhook_secret: str = ""
    # Shared web-search keys (operator-owned). Web access is used only when a group opts in.
    tavily_api_key: str = ""
    serper_api_key: str = ""
    brave_api_key: str = ""
    port: int = 8080
    log_level: str = "INFO"
    extra: dict = field(default_factory=dict)


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN is required (get it from @BotFather).")

    enc = os.getenv("ENCRYPTION_KEY", "").strip()
    if len(enc) < 24:
        raise SystemExit(
            "ENCRYPTION_KEY must be a long random secret (24+ chars). Generate one with:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )

    pool_provider = os.getenv("POOL_PROVIDER", "").strip().lower()
    if pool_provider not in ("", "openai", "gemini"):
        raise SystemExit("POOL_PROVIDER must be 'openai', 'gemini' or empty.")

    return Settings(
        bot_token=token,
        bot_username=os.getenv("BOT_USERNAME", "YourBotUsername").strip().lstrip("@"),
        encryption_key=enc,
        super_admin_ids=_csv_ints(os.getenv("SUPER_ADMIN_IDS", "")),
        database_url=normalize_database_url(
            os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/bot.db")
        ),
        pool_provider=pool_provider,
        pool_api_key=os.getenv("POOL_API_KEY", "").strip(),
        pool_model=os.getenv("POOL_MODEL", "").strip(),
        pool_daily_calls=int(os.getenv("POOL_DAILY_CALLS", "500")),
        pool_group_daily_calls=int(os.getenv("POOL_GROUP_DAILY_CALLS", "30")),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        default_openai_model=os.getenv("DEFAULT_OPENAI_MODEL", "gpt-4o-mini"),
        default_gemini_model=os.getenv("DEFAULT_GEMINI_MODEL", "gemini-2.5-flash"),
        timezone_default=os.getenv("DEFAULT_TIMEZONE", "UTC"),
        webhook_base_url=os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/"),
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        tavily_api_key=os.getenv("TAVILY_API_KEY", "").strip(),
        serper_api_key=os.getenv("SERPER_API_KEY", "").strip(),
        brave_api_key=os.getenv("BRAVE_API_KEY", "").strip(),
        port=int(os.getenv("PORT", "8080")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
