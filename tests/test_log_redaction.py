"""Offline checks that configured logging does not expose credentials."""

import logging
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gemini_tg_bot import __main__ as main_module


FAKE_URL = (
    "https://api.telegram.org/"
    "bot123456789:FAKE_TOKEN_FOR_TEST_abcdefghijkl/getMe"
)
FAKE_TOKEN = "123456789:FAKE_TOKEN_FOR_TEST_abcdefghijklmnop"


@pytest.fixture
def isolated_logging(monkeypatch: pytest.MonkeyPatch):
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    request_loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    original_levels = [logger.level for logger in request_loggers]
    output = StringIO()
    root.handlers = [logging.StreamHandler(output)]
    upstream_level = Mock()
    monkeypatch.setattr(main_module.gemini_webapi, "set_log_level", upstream_level)
    try:
        yield SimpleNamespace(upstream_level=upstream_level, output=output)
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = original_handlers
        root.setLevel(original_level)
        for logger, level in zip(request_loggers, original_levels, strict=True):
            logger.setLevel(level)


def test_configured_handler_redacts_request_tokens(isolated_logging):
    """Request URLs must remain safe even if they reach the root handler."""

    main_module._configure_logging("DEBUG")
    logging.getLogger("test.redaction").warning("HTTP Request: POST %s", FAKE_URL)

    output = isolated_logging.output.getvalue()
    assert "https://api.telegram.org/bot<redacted>/getMe" in output
    assert "FAKE_TOKEN" not in output
    assert "bot123456789:" not in output


def test_configured_handler_redacts_exception_tracebacks(isolated_logging):
    """Exceptions can repeat request URLs after the formatted message."""

    main_module._configure_logging("INFO")
    try:
        raise RuntimeError(FAKE_URL)
    except RuntimeError:
        logging.getLogger("test.redaction").exception("Request failed")

    output = isolated_logging.output.getvalue()
    assert "Traceback (most recent call last)" in output
    assert "RuntimeError: https://api.telegram.org/bot<redacted>/getMe" in output
    assert "FAKE_TOKEN" not in output
    assert "bot123456789:" not in output


def test_configured_handler_redacts_bare_tokens(isolated_logging):
    """Rejected-token messages contain bare credentials without a bot prefix."""

    main_module._configure_logging("INFO")
    logging.getLogger("test.redaction").error(
        "The token `%s` was rejected", FAKE_TOKEN,
    )

    output = isolated_logging.output.getvalue()
    assert "The token `<redacted-token>` was rejected" in output
    assert "FAKE_TOKEN" not in output
    assert "123456789:" not in output


def test_configured_handler_redacts_bare_tokens_in_tracebacks(isolated_logging):
    """Credentials appearing only in exception details also need redaction."""

    main_module._configure_logging("INFO")
    try:
        raise RuntimeError(f"The token `{FAKE_TOKEN}` was rejected")
    except RuntimeError:
        logging.getLogger("test.redaction").exception("Polling failed")

    output = isolated_logging.output.getvalue()
    assert "Traceback (most recent call last)" in output
    assert "RuntimeError: The token `<redacted-token>` was rejected" in output
    assert "FAKE_TOKEN_FOR_TEST" not in output
    assert "123456789:" not in output


@pytest.mark.parametrize("message", ["12:34", "2026-10-11 15:06:54"])
def test_configured_handler_preserves_short_digit_strings(isolated_logging, message):
    """Short colon-separated digits and timestamps are not Telegram credentials."""

    main_module._configure_logging("INFO")
    logging.getLogger("test.redaction").warning("Unrelated value: %s", message)

    output = isolated_logging.output.getvalue()
    assert f"Unrelated value: {message}" in output
    assert "redacted" not in output


def test_main_logs_fatal_polling_errors_before_nonzero_exit(
    isolated_logging, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
):
    """Fatal polling failures must reach stderr through redaction and still fail."""

    monkeypatch.setattr(main_module, "Settings", lambda: SimpleNamespace(
        log_level="INFO",
    ))

    async def fake_polling(settings):
        raise RuntimeError(f"The token `{FAKE_TOKEN}` was rejected")

    monkeypatch.setattr(main_module, "_run_polling", fake_polling)
    logging.getLogger().handlers = []

    with pytest.raises(SystemExit) as exit_info:
        raise SystemExit(main_module.main([]))

    assert exit_info.value.code != 0
    stderr = capsys.readouterr().err
    assert "FAKE_TOKEN_FOR_TEST" not in stderr
    assert "123456789:" not in stderr
    assert "<redacted-token>" in stderr
    assert "Traceback (most recent call last)" in stderr
    assert stderr.count("CRITICAL") == 1


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_main_preserves_control_flow_exceptions(
    isolated_logging, monkeypatch: pytest.MonkeyPatch, exception_type,
):
    """Interrupts and explicit exits must retain their existing propagation."""

    monkeypatch.setattr(main_module, "Settings", lambda: SimpleNamespace(
        log_level="INFO",
    ))

    async def fake_polling(settings):
        raise exception_type()

    monkeypatch.setattr(main_module, "_run_polling", fake_polling)

    with pytest.raises(exception_type):
        main_module.main([])

    assert "CRITICAL" not in isolated_logging.output.getvalue()


def test_request_loggers_suppress_credential_bearing_info(isolated_logging):
    """HTTP request logging must stay suppressed even during DEBUG diagnosis."""

    main_module._configure_logging("DEBUG")

    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


@pytest.mark.parametrize(
    ("configured", "expected"), [("DEBUG", "INFO"), ("WARNING", "WARNING")],
)
def test_upstream_level_blocks_cookie_cache_debug_logs(
    isolated_logging, monkeypatch: pytest.MonkeyPatch, configured, expected,
):
    """Production logging must clamp upstream DEBUG without losing higher levels."""

    monkeypatch.setattr(main_module, "Settings", lambda: SimpleNamespace(
        log_level=configured,
    ))
    polling = Mock()

    async def fake_polling(settings):
        polling(settings)

    monkeypatch.setattr(main_module, "_run_polling", fake_polling)

    assert main_module.main([]) == 0

    isolated_logging.upstream_level.assert_called_once_with(expected)
    polling.assert_called_once()
