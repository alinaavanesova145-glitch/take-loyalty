"""
Centralized configuration for the TAKE coffee&more Loyalty System.

All runtime configuration is loaded from environment variables (via a .env
file in local development, or real environment variables in production).
Never hardcode secrets — this module is the single source of truth for
every other module that needs a config value.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Only used by seed.py (the sole consumer of this module) — kept minimal
    # rather than declaring fields nothing reads.
    DATABASE_URL: str
    BARISTA_PIN: str = "1234"
    ADMIN_PIN: str = "9999"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings accessor. Using lru_cache means the .env file (or
    environment) is parsed exactly once per process, and every call site
    that does `get_settings()` shares the same Settings instance.
    """
    return Settings()  # type: ignore[call-arg]  # fields are populated from env/.env at runtime


settings = get_settings()
