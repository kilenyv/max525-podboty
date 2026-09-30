"""Настройки приложения, читаются из .env и переменных окружения."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Настройки приложения. Источник — .env и переменные окружения."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- MAX -------------------------------------------------------------
    max_bot_token: str = Field(default="", alias="MAX_BOT_TOKEN")
    bot_username: str = Field(default="", alias="BOT_USERNAME")

    # --- Записи ----------------------------------------------------------
    default_timezone: str = Field(default="Europe/Moscow", alias="DEFAULT_TIMEZONE")
    max_subbots_per_user: int = Field(default=5, alias="MAX_SUBBOTS_PER_USER")
    max_subbots_per_day: int = Field(default=10, alias="MAX_SUBBOTS_PER_DAY")
    jobs_poll_interval: int = Field(default=15, alias="JOBS_POLL_INTERVAL")

    # --- Инфраструктура --------------------------------------------------
    database_path: Path = Field(default=BASE_DIR / "data" / "bot.db", alias="DATABASE_PATH")
    api_rate_limit: int = Field(default=25, alias="API_RATE_LIMIT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("bot_username")
    @classmethod
    def _strip_username(cls, value: str) -> str:
        return value.strip().lstrip("@").strip()

    @property
    def is_configured(self) -> bool:
        return bool(self.max_bot_token and self.bot_username)

    def require(self) -> None:
        missing = [
            name
            for name, value in (
                ("MAX_BOT_TOKEN", self.max_bot_token),
                ("BOT_USERNAME", self.bot_username),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Не заполнены переменные окружения: "
                + ", ".join(missing)
                + ". Скопируйте .env.example в .env и укажите значения."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
