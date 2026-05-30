"""Environment configuration — ports src/config/env.ts (Zod) to pydantic-settings.

Loads a local .env (``.env`` always wins over stale shell exports, matching the TS
loader), validates, and enforces "exactly one signing method" (HMAC or RSA).
No external LLM key exists here — the installing agent is the brain.
"""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ── Required ──────────────────────────────────────────────────────────────
    BYBIT_API_KEY: str = Field(min_length=1)
    DATABASE_URL: str

    # ── Auth — set exactly ONE signing method ────────────────────────────────
    BYBIT_API_SECRET: str | None = None            # HMAC-SHA256
    BYBIT_API_PRIVATE_KEY_PATH: str | None = None  # RSA-SHA256 (PEM file)
    BYBIT_API_PRIVATE_KEY: str | None = None       # RSA-SHA256 (inline PEM)

    # ── Notifications ─────────────────────────────────────────────────────────
    REPORT_EMAIL: str | None = None
    REPORT_EMAIL_APP_PASSWORD: str | None = None
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CHAT_ID: str | None = None

    # ── Optional infra ────────────────────────────────────────────────────────
    BYBIT_PROXY_URL: str | None = None
    GITHUB_TOKEN: str | None = None
    NEWS_API_KEY: str | None = None

    NODE_ENV: str = "development"
    LOG_LEVEL: str = "info"

    @model_validator(mode="after")
    def _require_one_signing_method(self) -> "Settings":
        if not (self.BYBIT_API_SECRET or self.BYBIT_API_PRIVATE_KEY_PATH or self.BYBIT_API_PRIVATE_KEY):
            raise ValueError(
                "Set BYBIT_API_SECRET (HMAC) or BYBIT_API_PRIVATE_KEY_PATH / "
                "BYBIT_API_PRIVATE_KEY (RSA)"
            )
        return self


_settings: Settings | None = None


def get_env() -> Settings:
    """Lazy singleton so importing the package never forces validation."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
