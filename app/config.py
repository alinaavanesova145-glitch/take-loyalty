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
    # --- Telegram ---
    BOT_TOKEN: str
    BOT_USERNAME: str = "take_loyalty_bot"

    # --- Database ---
    DATABASE_URL: str

    # --- Security ---
    QR_SECRET_KEY: str
    BARISTA_PIN: str = "1234"
    INIT_DATA_MAX_AGE_SECONDS: int = 86400  # 24 hours

    # --- Business rules ---
    APP_NAME: str = "TAKE coffee&more Loyalty"
    CASHBACK_RATE: float = 0.03
    DEFAULT_BRANCH_ID: int = 1
    DEFAULT_BRANCH_NAME: str = "TAKE #1"
    DEFAULT_BRANCH_ADDRESS: str = "Central Branch"

    # --- Web ---
    BASE_URL: str = "http://localhost:8000"
    DEV_MODE: bool = False

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
