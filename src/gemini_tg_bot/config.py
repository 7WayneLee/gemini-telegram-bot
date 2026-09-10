"""Application configuration loaded from environment variables and ``.env``."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from gemini_tg_bot.i18n import DEFAULT_LANGUAGE


LogLevel = Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]
RUNTIME_CREDENTIALS_FILENAME = "runtime-credentials.json"
DEFAULT_GROUP_MODEL = "flash"
DEFAULT_GROUP_THREAD_RETENTION_DAYS = 30
DEFAULT_GROUP_THREAD_MAX_PER_CHAT = 200

LOGGER = logging.getLogger(__name__)


def load_runtime_credentials(
    cookie_path: Path,
) -> tuple[SecretStr, SecretStr] | None:
    """Load the optional credential override without exposing its contents."""

    override_path = cookie_path / RUNTIME_CREDENTIALS_FILENAME
    try:
        payload = json.loads(override_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("unsupported runtime credential override")
        secure_1psid = _decode_runtime_credential(payload["secure_1psid"])
        secure_1psidts = _decode_runtime_credential(payload["secure_1psidts"])
    except FileNotFoundError:
        return None
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
    ):
        # Do not include the path or exception text: either may be controlled
        # by credential-related runtime state.
        LOGGER.warning(
            "Runtime credential override is unreadable or invalid; "
            "using configured credentials"
        )
        return None
    return SecretStr(secure_1psid), SecretStr(secure_1psidts)


def persist_runtime_credentials(
    cookie_path: Path,
    secure_1psid: str | SecretStr,
    secure_1psidts: str | SecretStr,
) -> None:
    """Atomically persist credentials in a mode-0600 runtime override."""

    first = _secret_value(secure_1psid)
    second = _secret_value(secure_1psidts)
    if not first or not second:
        raise ValueError("runtime credentials must not be empty")

    payload = {
        "version": 1,
        # Encoding prevents accidental plaintext disclosure during generic
        # file inspection.  Confidentiality is enforced by the 0600 mode.
        "secure_1psid": _encode_runtime_credential(first),
        "secure_1psidts": _encode_runtime_credential(second),
    }
    cookie_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    override_path = cookie_path / RUNTIME_CREDENTIALS_FILENAME
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".runtime-credentials-",
            suffix=".tmp",
            dir=cookie_path,
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as override_file:
            descriptor = -1
            json.dump(payload, override_file, separators=(",", ":"))
            override_file.write("\n")
            override_file.flush()
            os.fsync(override_file.fileno())
        os.replace(temporary_path, override_path)
        temporary_path = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _secret_value(value: str | SecretStr) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def _encode_runtime_credential(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _decode_runtime_credential(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("runtime credential must be encoded text")
    decoded = base64.b64decode(
        value.encode("ascii"),
        altchars=b"-_",
        validate=True,
    ).decode("utf-8")
    if not decoded:
        raise ValueError("runtime credential must not be empty")
    return decoded


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
    allowed_chat_ids: Annotated[set[int], NoDecode] = Field(
        default_factory=set,
        validation_alias="ALLOWED_CHAT_IDS",
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
    group_model: str = Field(
        default=DEFAULT_GROUP_MODEL,
        validation_alias="GROUP_MODEL",
        min_length=1,
    )
    group_thread_retention_days: int = Field(
        default=DEFAULT_GROUP_THREAD_RETENTION_DAYS,
        validation_alias="GROUP_THREAD_RETENTION_DAYS",
        gt=0,
    )
    group_thread_max_per_chat: int = Field(
        default=DEFAULT_GROUP_THREAD_MAX_PER_CHAT,
        validation_alias="GROUP_THREAD_MAX_PER_CHAT",
        gt=0,
    )
    default_language: Literal["en", "zh-hant"] = Field(
        default=DEFAULT_LANGUAGE,
        validation_alias="DEFAULT_LANGUAGE",
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

    @model_validator(mode="after")
    def apply_runtime_credentials(self) -> Settings:
        """Prefer a valid runtime override to environment credentials."""

        credentials = load_runtime_credentials(self.gemini_cookie_path)
        if credentials is not None:
            self.gemini_secure_1psid, self.gemini_secure_1psidts = credentials
        return self

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

    @field_validator("allowed_chat_ids", mode="before")
    @classmethod
    def parse_allowed_chat_ids(cls, value: Any) -> set[int]:
        """Accept comma-separated or JSON-array Telegram group chat IDs."""

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
                raise ValueError("chat IDs must be negative integers")
            if isinstance(item, int):
                chat_id = item
            elif isinstance(item, str):
                try:
                    chat_id = int(item)
                except ValueError as error:
                    raise ValueError(
                        "chat IDs must be negative integers"
                    ) from error
            else:
                raise ValueError("chat IDs must be negative integers")
            if chat_id >= 0:
                raise ValueError("chat IDs must be negative integers")
            parsed.add(chat_id)
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
