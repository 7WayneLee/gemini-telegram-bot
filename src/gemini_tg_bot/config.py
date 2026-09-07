"""Application configuration loaded from environment variables and ``.env``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


LogLevel = Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]


class Settings(BaseSettings):
    """Validated runtime settings for the Telegram bot."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    telegram_bot_token: SecretStr = Field(
        validation_alias="TELEGRAM_BOT_TOKEN",
        min_length=1,
    )
    admin_user_id: int = Field(
        validation_alias="ADMIN_USER_ID",
        gt=0,
    )
    allowed_user_ids: Annotated[set[int], NoDecode] = Field(
        default_factory=set,
        validation_alias="ALLOWED_USER_IDS",
    )
    gemini_secure_1psid: SecretStr = Field(
        validation_alias="GEMINI_SECURE_1PSID",
        min_length=1,
    )
    gemini_secure_1psidts: SecretStr = Field(
        validation_alias="GEMINI_SECURE_1PSIDTS",
        min_length=1,
    )
    gemini_cookie_path: Path = Field(
        default=Path("data/cookies"),
        validation_alias="GEMINI_COOKIE_PATH",
    )
    gemini_proxy: str | None = Field(
        default=None,
        validation_alias="GEMINI_PROXY",
    )
    default_model: str | None = Field(
        default=None,
        validation_alias="DEFAULT_MODEL",
    )
    max_concurrency: int = Field(
        default=1,
        validation_alias="MAX_CONCURRENCY",
        gt=0,
    )
    user_rate_limit_per_min: int = Field(
        default=10,
        validation_alias="USER_RATE_LIMIT_PER_MIN",
        gt=0,
    )
    research_timeout_sec: int = Field(
        default=900,
        validation_alias="RESEARCH_TIMEOUT_SEC",
        gt=0,
    )
    log_level: LogLevel = Field(
        default="INFO",
        validation_alias="LOG_LEVEL",
    )
    enable_video_generation: bool = Field(
        default=False,
        validation_alias="ENABLE_VIDEO_GENERATION",
    )
    enable_audio_generation: bool = Field(
        default=False,
        validation_alias="ENABLE_AUDIO_GENERATION",
    )

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def parse_allowed_user_ids(cls, value: Any) -> set[int]:
        """Accept a comma-separated string or JSON array of Telegram user IDs."""

        if value is None:
            return set()
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return set()
            if value.startswith("["):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        "must be a comma-separated list or JSON array"
                    ) from error
            else:
                value = [item.strip() for item in value.split(",")]

        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("must be a comma-separated list or JSON array")

        parsed: set[int] = set()
        for item in value:
            if isinstance(item, bool):
                raise ValueError("user IDs must be non-zero integers")
            if isinstance(item, int):
                user_id = item
            elif isinstance(item, str):
                try:
                    user_id = int(item)
                except ValueError as error:
                    raise ValueError("user IDs must be non-zero integers") from error
            else:
                raise ValueError("user IDs must be non-zero integers")
            if user_id == 0:
                raise ValueError("user IDs must be non-zero integers")
            parsed.add(user_id)
        return parsed

    @field_validator("gemini_proxy", "default_model", mode="before")
    @classmethod
    def empty_string_to_none(cls, value: Any) -> Any:
        """Treat blank optional environment variables as unset."""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: Any) -> Any:
        """Allow conventional case-insensitive log-level input."""

        return value.upper() if isinstance(value, str) else value
