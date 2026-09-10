from __future__ import annotations

import logging
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from gemini_tg_bot.config import (
    RUNTIME_CREDENTIALS_FILENAME,
    Settings,
    persist_runtime_credentials,
)


REQUIRED_SETTINGS = {
    "TELEGRAM_BOT_TOKEN": "FAKE_TELEGRAM_BOT_TOKEN_FOR_TEST",
    "ADMIN_USER_ID": 123456789,
    "GEMINI_SECURE_1PSID": "FAKE_1PSID_FOR_TEST",
    "GEMINI_SECURE_1PSIDTS": "FAKE_1PSIDTS_FOR_TEST",
}

ENVIRONMENT_VARIABLES = {
    "TELEGRAM_BOT_TOKEN",
    "ADMIN_USER_ID",
    "ALLOWED_USER_IDS",
    "ALLOWED_CHAT_IDS",
    "GEMINI_SECURE_1PSID",
    "GEMINI_SECURE_1PSIDTS",
    "GEMINI_COOKIE_PATH",
    "GEMINI_PROXY",
    "DEFAULT_MODEL",
    "GROUP_MODEL",
    "GROUP_THREAD_RETENTION_DAYS",
    "GROUP_THREAD_MAX_PER_CHAT",
    "DEFAULT_LANGUAGE",
    "MAX_CONCURRENCY",
    "USER_RATE_LIMIT_PER_MIN",
    "RESEARCH_TIMEOUT_SEC",
    "LOG_LEVEL",
    "ENABLE_VIDEO_GENERATION",
    "ENABLE_AUDIO_GENERATION",
}


@pytest.fixture(autouse=True)
def clear_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def make_settings(**overrides: object) -> Settings:
    values = REQUIRED_SETTINGS | overrides
    return Settings(_env_file=None, **values)


def test_required_values_and_safe_defaults() -> None:
    settings = make_settings()

    assert (
        settings.telegram_bot_token.get_secret_value()
        == REQUIRED_SETTINGS["TELEGRAM_BOT_TOKEN"]
    )
    assert settings.admin_user_id == REQUIRED_SETTINGS["ADMIN_USER_ID"]
    assert settings.gemini_secure_1psid.get_secret_value() == "FAKE_1PSID_FOR_TEST"
    assert settings.gemini_secure_1psidts.get_secret_value() == "FAKE_1PSIDTS_FOR_TEST"
    assert settings.allowed_user_ids == set()
    assert settings.allowed_chat_ids == set()
    assert settings.max_concurrency == 1
    assert settings.enable_video_generation is False
    assert settings.enable_audio_generation is False
    assert settings.default_model is None
    assert settings.group_model == "flash"
    assert settings.group_thread_retention_days == 30
    assert settings.group_thread_max_per_chat == 200
    assert settings.default_language == "en"
    assert settings.gemini_proxy is None


def test_group_conversation_settings_are_validated() -> None:
    """Invalid cleanup or model values must fail before losing thread state."""

    settings = make_settings(
        GROUP_MODEL="dynamic-group-model",
        GROUP_THREAD_RETENTION_DAYS=14,
        GROUP_THREAD_MAX_PER_CHAT=75,
    )
    assert settings.group_model == "dynamic-group-model"
    assert settings.group_thread_retention_days == 14
    assert settings.group_thread_max_per_chat == 75

    for key, value in (
        ("GROUP_MODEL", ""),
        ("GROUP_THREAD_RETENTION_DAYS", 0),
        ("GROUP_THREAD_MAX_PER_CHAT", 0),
    ):
        with pytest.raises(ValidationError):
            make_settings(**{key: value})


def test_default_language_accepts_only_supported_catalogs() -> None:
    """Non-chat notifications must never select an unavailable catalog."""

    assert make_settings(DEFAULT_LANGUAGE="zh-hant").default_language == "zh-hant"
    with pytest.raises(ValidationError):
        make_settings(DEFAULT_LANGUAGE="fr")


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("123, 456,123", {123, 456}),
        ('[123, "456"]', {123, 456}),
        ("-100123", {-100123}),
        ("", set()),
    ],
)
def test_allowed_user_ids_parsing(raw_value: str, expected: set[int]) -> None:
    settings = make_settings(ALLOWED_USER_IDS=raw_value)

    assert settings.allowed_user_ids == expected


@pytest.mark.parametrize("raw_value", ["123,not-an-id", "[123", "0", "true"])
def test_allowed_user_ids_reject_invalid_values(raw_value: str) -> None:
    with pytest.raises(ValidationError, match="user IDs|comma-separated"):
        make_settings(ALLOWED_USER_IDS=raw_value)


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("-1001, -1002,-1001", {-1001, -1002}),
        ('[-1001, "-1002"]', {-1001, -1002}),
        ("", set()),
    ],
)
def test_allowed_chat_ids_accept_negative_group_ids(
    raw_value: str,
    expected: set[int],
) -> None:
    """Both supported env syntaxes must preserve Telegram's negative group IDs."""

    settings = make_settings(ALLOWED_CHAT_IDS=raw_value)

    assert settings.allowed_chat_ids == expected


@pytest.mark.parametrize(
    "raw_value",
    ["-1001,not-an-id", "[-1001", "0", "1", "true"],
)
def test_allowed_chat_ids_reject_non_group_values(raw_value: str) -> None:
    """Malformed or non-negative IDs must not accidentally authorize a chat."""

    with pytest.raises(ValidationError, match="chat IDs|comma-separated"):
        make_settings(ALLOWED_CHAT_IDS=raw_value)


def test_loads_dotenv_and_normalizes_optional_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=FAKE_TELEGRAM_BOT_TOKEN_FOR_TEST",
                "ADMIN_USER_ID=123456789",
                "ALLOWED_USER_IDS=101,202",
                "ALLOWED_CHAT_IDS=-1001,-1002",
                "GEMINI_SECURE_1PSID=FAKE_1PSID_FOR_TEST",
                "GEMINI_SECURE_1PSIDTS=FAKE_1PSIDTS_FOR_TEST",
                "GEMINI_COOKIE_PATH=/tmp/gemini-test-cookies",
                "GEMINI_PROXY=",
                "DEFAULT_MODEL=",
                "LOG_LEVEL=debug",
                "ENABLE_VIDEO_GENERATION=true",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.allowed_user_ids == {101, 202}
    assert settings.allowed_chat_ids == {-1001, -1002}
    assert settings.gemini_cookie_path == Path("/tmp/gemini-test-cookies")
    assert settings.gemini_proxy is None
    assert settings.default_model is None
    assert settings.log_level == "DEBUG"
    assert settings.enable_video_generation is True
    assert settings.enable_audio_generation is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ADMIN_USER_ID", 0),
        ("MAX_CONCURRENCY", 0),
        ("USER_RATE_LIMIT_PER_MIN", 0),
        ("RESEARCH_TIMEOUT_SEC", 0),
        ("LOG_LEVEL", "verbose"),
    ],
)
def test_rejects_invalid_scalar_settings(name: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{name: value})


def test_required_settings_are_enforced() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)

    missing_fields = {item["loc"][0] for item in error.value.errors()}
    assert missing_fields == {
        "TELEGRAM_BOT_TOKEN",
        "ADMIN_USER_ID",
        "GEMINI_SECURE_1PSID",
        "GEMINI_SECURE_1PSIDTS",
    }


def test_secrets_are_masked_in_settings_repr() -> None:
    rendered = repr(make_settings())

    assert "FAKE_1PSID_FOR_TEST" not in rendered
    assert "FAKE_1PSIDTS_FOR_TEST" not in rendered


def test_runtime_credentials_are_private_and_override_configured_values(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cookie_path = tmp_path / "cookies"
    replacement_1psid = "FAKE_REPLACEMENT_1PSID_FOR_TEST"
    replacement_1psidts = "FAKE_REPLACEMENT_1PSIDTS_FOR_TEST"

    with caplog.at_level(logging.WARNING):
        persist_runtime_credentials(
            cookie_path,
            replacement_1psid,
            replacement_1psidts,
        )
        restarted = make_settings(GEMINI_COOKIE_PATH=cookie_path)

    override_path = cookie_path / RUNTIME_CREDENTIALS_FILENAME
    assert stat.S_IMODE(cookie_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(override_path.stat().st_mode) == 0o600
    override_text = override_path.read_text(encoding="utf-8")
    assert replacement_1psid not in override_text
    assert replacement_1psidts not in override_text
    assert replacement_1psid not in caplog.text
    assert replacement_1psidts not in caplog.text
    assert (
        restarted.gemini_secure_1psid.get_secret_value()
        == replacement_1psid
    )
    assert (
        restarted.gemini_secure_1psidts.get_secret_value()
        == replacement_1psidts
    )


def test_missing_runtime_credentials_falls_back_without_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        settings = make_settings(GEMINI_COOKIE_PATH=tmp_path / "cookies")

    assert settings.gemini_secure_1psid.get_secret_value() == "FAKE_1PSID_FOR_TEST"
    assert (
        settings.gemini_secure_1psidts.get_secret_value()
        == "FAKE_1PSIDTS_FOR_TEST"
    )
    assert "Runtime credential override" not in caplog.text


def test_invalid_runtime_credentials_warns_and_falls_back(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cookie_path = tmp_path / "cookies"
    cookie_path.mkdir()
    (cookie_path / RUNTIME_CREDENTIALS_FILENAME).write_text(
        "{invalid-json",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        settings = make_settings(GEMINI_COOKIE_PATH=cookie_path)

    assert settings.gemini_secure_1psid.get_secret_value() == "FAKE_1PSID_FOR_TEST"
    assert (
        settings.gemini_secure_1psidts.get_secret_value()
        == "FAKE_1PSIDTS_FOR_TEST"
    )
    assert "Runtime credential override is unreadable or invalid" in caplog.text
    assert "FAKE_1PSID_FOR_TEST" not in caplog.text
    assert "FAKE_1PSIDTS_FOR_TEST" not in caplog.text
