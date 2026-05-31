"""Centralised env config. Every service imports `get_settings()`."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Market data providers (Phase 1)
    polygon_api_key: str | None = None
    alpaca_api_key: str | None = None
    alpaca_api_secret: str | None = None

    # Logging
    log_level: str = Field(default="INFO")
    log_json: bool = Field(default=False, description="Emit JSON logs (prod=true).")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
