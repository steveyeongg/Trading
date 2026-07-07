"""Centralised env config. Every service imports `get_settings()`."""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import dotenv_values
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def load_env(env_file: str = ".env") -> None:
    """Mirror `.env` into ``os.environ`` — call once at application startup.

    pydantic-settings reads `.env` into the ``Settings`` object but does NOT
    export those values to the process environment. Several components read
    ``os.environ`` directly — the provider-status panel, the FRED client, the
    DeepSeek explanation writer, and the Telegram channel — so without this
    bridge they are blind to `.env` and silently fall back even when the keys
    are set.

    This is an **application-entrypoint** concern (like the conventional
    ``load_dotenv()`` call), NOT a library/import-time one: putting it inside
    ``get_settings()`` would pollute the test suite, which runs from the repo
    root next to a real `.env` and relies on os.environ being clean to exercise
    the "no key → fallback" paths. Call it from service ``main()`` / lifespan
    and CLI entrypoints instead.

    Shell-exported variables win: we only fill in keys that are absent, so an
    explicit ``FRED_API_KEY=… uvicorn …`` still overrides the file.
    """
    try:
        values = dotenv_values(env_file)
    except Exception:
        return
    for key, value in values.items():
        if value is not None and key not in os.environ:
            os.environ[key] = value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: str = Field(default="dev", description="dev|staging|prod")

    # Postgres / TimescaleDB
    postgres_dsn: str = Field(
        default="postgresql+asyncpg://atlas:atlas_dev@localhost:5432/atlas",
        description="SQLAlchemy async DSN.",
    )

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0")

    # Market data providers (Phase 1) — pick one or both; falls back to synthetic.
    polygon_api_key: str | None = None
    alpaca_api_key: str | None = None
    alpaca_api_secret: str | None = None

    # LLM (Explanation engine). DeepSeek via the OpenAI-compatible API.
    deepseek_api_key: str | None = None

    # Logging
    log_level: str = Field(default="INFO")
    log_json: bool = Field(default=False, description="Emit JSON logs (prod=true).")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
