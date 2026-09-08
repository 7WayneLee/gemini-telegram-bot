"""Bounded flood-control handling for every Telegram API operation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, TypeVar

from telegram.error import RetryAfter


MAX_FLOOD_WAIT_SECONDS = 30.0
MAX_FLOOD_RETRIES = 3
SERVICE_BUSY = "服務忙碌，請稍後再試。"

_ResultT = TypeVar("_ResultT")
_Sleep = Callable[[float], Awaitable[None]]


class FloodControlExceeded(RuntimeError):
    """Raised when a Telegram flood-control wait exceeds the bounded budget."""

    def __init__(self, retry_after: float, waited_seconds: float) -> None:
        self.retry_after = retry_after
        self.waited_seconds = waited_seconds
        super().__init__(
            "Telegram flood-control retry budget exceeded "
            f"after waiting {waited_seconds:.2f} seconds"
        )


def _retry_delay(error: RetryAfter) -> float:
    retry_after = error.retry_after
    if isinstance(retry_after, timedelta):
        return retry_after.total_seconds()
    return float(retry_after)


async def call_telegram(
    operation: Callable[..., Awaitable[_ResultT]],
    *args: Any,
    sleep: _Sleep | None = None,
    flood_wait: _Sleep | None = None,
    **kwargs: Any,
) -> _ResultT:
    """Call one Telegram operation with a bounded flood-control budget."""

    waited_seconds = 0.0
    wait = flood_wait or sleep or asyncio.sleep
    for attempt in range(MAX_FLOOD_RETRIES + 1):
        try:
            return await operation(*args, **kwargs)
        except RetryAfter as error:
            delay = max(0.0, _retry_delay(error))
            remaining = MAX_FLOOD_WAIT_SECONDS - waited_seconds
            if attempt == MAX_FLOOD_RETRIES or delay > remaining:
                raise FloodControlExceeded(delay, waited_seconds) from error
            await wait(delay)
            waited_seconds += delay

    raise AssertionError("unreachable")


async def send_text(message: Any, text: str, **kwargs: Any) -> Any:
    """Send a text reply through the protected transport."""

    return await call_telegram(message.reply_text, text, **kwargs)


async def send_text_or_busy(message: Any, text: str, **kwargs: Any) -> Any:
    """Send text, replacing an excessive flood wait with a busy response."""

    try:
        return await send_text(message, text, **kwargs)
    except FloodControlExceeded:
        if text == SERVICE_BUSY:
            raise
        return await send_text(message, SERVICE_BUSY)


async def send_photo(message: Any, photo: Any, **kwargs: Any) -> Any:
    """Send a photo reply through the protected transport."""

    return await call_telegram(message.reply_photo, photo, **kwargs)


async def send_document(message: Any, document: Any, **kwargs: Any) -> Any:
    """Send a document reply through the protected transport."""

    return await call_telegram(message.reply_document, document, **kwargs)


async def send_media_group(message: Any, media: Any, **kwargs: Any) -> Any:
    """Send a media group reply through the protected transport."""

    return await call_telegram(message.reply_media_group, media, **kwargs)


async def edit_text(message: Any, text: str, **kwargs: Any) -> Any:
    """Edit a message through the protected transport."""

    return await call_telegram(message.edit_text, text, **kwargs)


async def edit_message_text(query: Any, text: str, **kwargs: Any) -> Any:
    """Edit a callback query's message through the protected transport."""

    return await call_telegram(query.edit_message_text, text, **kwargs)


async def edit_message_text_or_busy(query: Any, text: str, **kwargs: Any) -> Any:
    """Edit callback text, replacing an excessive flood wait with busy text."""

    try:
        return await edit_message_text(query, text, **kwargs)
    except FloodControlExceeded:
        if text == SERVICE_BUSY:
            raise
        return await edit_message_text(query, SERVICE_BUSY)


async def answer_callback(query: Any, **kwargs: Any) -> Any:
    """Answer a callback query through the protected transport."""

    return await call_telegram(query.answer, **kwargs)


async def delete_message(message: Any, **kwargs: Any) -> Any:
    """Delete a message through the protected transport."""

    return await call_telegram(message.delete, **kwargs)


__all__ = [
    "FloodControlExceeded",
    "MAX_FLOOD_RETRIES",
    "MAX_FLOOD_WAIT_SECONDS",
    "SERVICE_BUSY",
    "answer_callback",
    "call_telegram",
    "delete_message",
    "edit_message_text",
    "edit_message_text_or_busy",
    "edit_text",
    "send_document",
    "send_media_group",
    "send_photo",
    "send_text",
    "send_text_or_busy",
]
