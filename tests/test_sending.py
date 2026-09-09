from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from gemini_tg_bot.telegram.sending import (
    MAX_FLOOD_WAIT_SECONDS,
    MAX_NETWORK_RETRIES,
    NETWORK_RETRY_DELAY_SECONDS,
    edit_text,
    send_text,
)


async def test_edit_retries_network_error_then_succeeds() -> None:
    """A transient return-path failure must not discard a generated answer."""

    message = SimpleNamespace(
        edit_text=AsyncMock(side_effect=[NetworkError("offline"), "sent"])
    )
    sleep = AsyncMock()

    result = await edit_text(message, "answer", sleep=sleep)

    assert result == "sent"
    assert message.edit_text.await_args_list == [call("answer"), call("answer")]
    sleep.assert_awaited_once_with(NETWORK_RETRY_DELAY_SECONDS)


async def test_edit_retries_timed_out_error() -> None:
    """Telegram timeouts are transient and must retain the safe edit retry path."""

    message = SimpleNamespace(
        edit_text=AsyncMock(side_effect=[TimedOut("slow response"), "sent"])
    )
    sleep = AsyncMock()

    result = await edit_text(message, "answer", sleep=sleep)

    assert result == "sent"
    assert message.edit_text.await_count == 2
    sleep.assert_awaited_once_with(NETWORK_RETRY_DELAY_SECONDS)


async def test_edit_does_not_retry_bad_request() -> None:
    """Invalid requests must fail immediately despite inheriting NetworkError."""

    error = BadRequest("invalid HTML")
    message = SimpleNamespace(edit_text=AsyncMock(side_effect=error))
    sleep = AsyncMock()

    with pytest.raises(BadRequest) as exc_info:
        await edit_text(message, "<broken>", sleep=sleep)

    assert exc_info.value is error
    assert message.edit_text.await_count == 1
    sleep.assert_not_awaited()


async def test_new_message_does_not_retry_network_error() -> None:
    """A lost success response must not cause a duplicate newly-created message."""

    error = NetworkError("response lost")
    message = SimpleNamespace(reply_text=AsyncMock(side_effect=error))
    sleep = AsyncMock()

    with pytest.raises(NetworkError) as exc_info:
        await send_text(message, "answer", sleep=sleep)

    assert exc_info.value is error
    assert message.reply_text.await_count == 1
    sleep.assert_not_awaited()


async def test_exhausted_network_retries_raise_original_error() -> None:
    """Callers need Telegram's original exception identity for classification."""

    error = NetworkError("still offline")
    message = SimpleNamespace(edit_text=AsyncMock(side_effect=error))
    sleep = AsyncMock()

    with pytest.raises(NetworkError) as exc_info:
        await edit_text(message, "answer", sleep=sleep)

    assert exc_info.value is error
    assert message.edit_text.await_count == MAX_NETWORK_RETRIES + 1
    assert sleep.await_args_list == [
        call(NETWORK_RETRY_DELAY_SECONDS),
        call(NETWORK_RETRY_DELAY_SECONDS),
    ]


async def test_network_backoff_does_not_consume_flood_wait_budget() -> None:
    """Network recovery must leave the full 30-second flood-control allowance."""

    message = SimpleNamespace(
        edit_text=AsyncMock(
            side_effect=[
                NetworkError("offline"),
                RetryAfter(MAX_FLOOD_WAIT_SECONDS),
                "sent",
            ]
        )
    )
    sleep = AsyncMock()

    result = await edit_text(message, "answer", sleep=sleep)

    assert result == "sent"
    assert message.edit_text.await_count == 3
    assert sleep.await_args_list == [
        call(NETWORK_RETRY_DELAY_SECONDS),
        call(MAX_FLOOD_WAIT_SECONDS),
    ]


async def test_retry_after_still_retries_new_message() -> None:
    """Disabling network retries must not weaken established flood handling."""

    message = SimpleNamespace(
        reply_text=AsyncMock(side_effect=[RetryAfter(2), "sent"])
    )
    sleep = AsyncMock()

    result = await send_text(message, "answer", sleep=sleep)

    assert result == "sent"
    assert message.reply_text.await_args_list == [call("answer"), call("answer")]
    sleep.assert_awaited_once_with(2.0)
