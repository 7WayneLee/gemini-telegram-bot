"""Redact Telegram credentials from complete log lines and tracebacks."""

import logging
import re


_BOT_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")


class TokenRedactingFormatter(logging.Formatter):
    """Remove bot tokens after formatting the message and exception details."""

    def format(self, record: logging.LogRecord) -> str:
        return _BOT_TOKEN.sub("bot<redacted>", super().format(record))
