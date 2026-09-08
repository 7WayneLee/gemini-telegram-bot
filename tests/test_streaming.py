from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, call

import pytest
from telegram.constants import ParseMode
from telegram.error import RetryAfter

from gemini_tg_bot.queue import RequestQueue
from gemini_tg_bot.telegram.streaming import (
    EDIT_CHARACTER_THRESHOLD,
    EDIT_INTERVAL_SECONDS,
    FloodControlExceeded,
    MAX_FLOOD_WAIT_SECONDS,
    PLACEHOLDER_TEXT,
    stream_response,
)


@dataclass
class FakeClock:
    current: float = 0.0

    def __call__(self) -> float:
        return self.current


class FakeClient:
    def __init__(
        self,
        chunks: Iterable[tuple[float, str]],
        *,
        clock: FakeClock,
    ) -> None:
        self._chunks = list(chunks)
        self._clock = clock
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _generate(self) -> AsyncIterator[SimpleNamespace]:
        full_text = ""
        for timestamp, delta in self._chunks:
            self._clock.current = timestamp
            full_text += delta
            yield SimpleNamespace(text_delta=delta, text=full_text)

    def generate_content_stream(
        self,
        prompt: str,
        **kwargs: Any,
    ) -> AsyncIterator[SimpleNamespace]:
        self.calls.append((prompt, kwargs))
        return self._generate()


def telegram_message() -> tuple[SimpleNamespace, SimpleNamespace]:
    placeholder = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply_text=AsyncMock(return_value=placeholder))
    return message, placeholder


async def test_elapsed_interval_edits_first_and_keeps_partial_markdown_plain() -> None:
    clock = FakeClock()
    client = FakeClient(
        [(0.3, "**open"), (EDIT_INTERVAL_SECONDS, " still"), (1.6, "**")],
        clock=clock,
    )
    message, placeholder = telegram_message()

    result = await stream_response(message, client, "hello", clock=clock)

    assert result.text == "**open still**"
    assert result.output is not None
    assert result.output.text == "**open still**"
    assert client.calls == [("hello", {})]
    message.reply_text.assert_awaited_once_with(PLACEHOLDER_TEXT)
    assert placeholder.edit_text.await_args_list == [
        call("**open still", parse_mode=None),
        call("<b>open still</b>", parse_mode=ParseMode.HTML),
    ]


async def test_character_threshold_edits_before_interval() -> None:
    clock = FakeClock()
    first = "a" * (EDIT_CHARACTER_THRESHOLD - 1)
    client = FakeClient(
        [(0.1, first), (0.2, "b"), (0.3, "c")],
        clock=clock,
    )
    message, placeholder = telegram_message()

    await stream_response(message, client, "hello", clock=clock)

    assert placeholder.edit_text.await_args_list == [
        call(first + "b", parse_mode=None),
        call(first + "bc", parse_mode=ParseMode.HTML),
    ]


async def test_retry_after_waits_then_retries_the_same_edit() -> None:
    clock = FakeClock()
    client = FakeClient([(0.1, "x" * EDIT_CHARACTER_THRESHOLD)], clock=clock)
    message, placeholder = telegram_message()
    placeholder.edit_text.side_effect = [RetryAfter(3), None, None]
    sleep = AsyncMock()

    await stream_response(message, client, "hello", clock=clock, sleep=sleep)

    sleep.assert_awaited_once_with(3.0)
    interim = call("x" * EDIT_CHARACTER_THRESHOLD, parse_mode=None)
    assert placeholder.edit_text.await_args_list == [
        interim,
        interim,
        call("x" * EDIT_CHARACTER_THRESHOLD, parse_mode=ParseMode.HTML),
    ]


async def test_retry_after_over_limit_fails_without_waiting() -> None:
    clock = FakeClock()
    client = FakeClient([], clock=clock)
    message, _ = telegram_message()
    message.reply_text.side_effect = RetryAfter(MAX_FLOOD_WAIT_SECONDS + 1)
    sleep = AsyncMock()

    with pytest.raises(FloodControlExceeded) as exc_info:
        await stream_response(message, client, "hello", clock=clock, sleep=sleep)

    assert exc_info.value.retry_after == MAX_FLOOD_WAIT_SECONDS + 1
    assert exc_info.value.waited_seconds == 0
    sleep.assert_not_awaited()
    assert message.reply_text.await_count == 1
    assert client.calls == []


async def test_flood_control_wait_releases_slot_for_another_user() -> None:
    queue = RequestQueue(
        max_concurrency=1,
        user_rate_limit_per_min=10,
        acquire_timeout=0.5,
    )
    clock = FakeClock()
    client = FakeClient([], clock=clock)
    message, placeholder = telegram_message()
    message.reply_text.side_effect = [RetryAfter(1), placeholder]
    wait_started = asyncio.Event()
    resume_wait = asyncio.Event()
    second_entered = asyncio.Event()
    release_second = asyncio.Event()

    async def controlled_sleep(delay: float) -> None:
        assert delay == 1
        wait_started.set()
        await resume_wait.wait()

    async def first_request() -> None:
        async with queue.request(user_id=1) as permit:
            await stream_response(
                message,
                client,
                "hello",
                clock=clock,
                flood_wait=lambda delay: permit.wait_for_flood_control(
                    delay,
                    sleep=controlled_sleep,
                ),
            )

    async def second_request() -> None:
        async with queue.request(user_id=2):
            second_entered.set()
            await release_second.wait()

    first_task = asyncio.create_task(first_request())
    await wait_started.wait()
    second_task = asyncio.create_task(second_request())

    await asyncio.wait_for(second_entered.wait(), timeout=0.1)
    release_second.set()
    await second_task
    resume_wait.set()
    await first_task

    assert queue.queue_depth == 0
    assert message.reply_text.await_count == 2


async def test_long_final_response_replaces_placeholder_and_sends_more_chunks() -> None:
    clock = FakeClock()
    text = "a" * 3900 + "\n\n" + "b" * 300
    client = FakeClient([(0.1, text)], clock=clock)
    message, placeholder = telegram_message()

    result = await stream_response(message, client, "hello", clock=clock)

    assert result.text == text
    assert result.output is not None
    assert result.output.text == text
    assert placeholder.edit_text.await_count == 1
    first_chunk = placeholder.edit_text.await_args.args[0]
    assert placeholder.edit_text.await_args.kwargs == {"parse_mode": ParseMode.HTML}
    assert len(first_chunk) <= 4000
    assert message.reply_text.await_count == 2
    second_chunk = message.reply_text.await_args.args[0]
    assert message.reply_text.await_args.kwargs == {"parse_mode": ParseMode.HTML}
    assert len(second_chunk) <= 4000
    assert first_chunk + second_chunk == text
