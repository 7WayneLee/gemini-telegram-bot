"""Redact Telegram credentials from complete log lines and tracebacks."""

import logging
import re


_BOT_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")
_BARE_TOKEN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


class TokenRedactingFormatter(logging.Formatter):
    """Remove bot tokens after formatting the message and exception details."""

    def format(self, record: logging.LogRecord) -> str:
        formatted = _BOT_TOKEN.sub("bot<redacted>", super().format(record))
        return _BARE_TOKEN.sub("<redacted-token>", formatted)
