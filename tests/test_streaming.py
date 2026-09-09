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
from gemini_tg_bot.i18n import LANGUAGE_CHINESE, translate
from gemini_tg_bot.telegram.sending import (
    FloodControlExceeded,
    MAX_FLOOD_WAIT_SECONDS,
)
from gemini_tg_bot.telegram.streaming import (
    EDIT_CHARACTER_THRESHOLD,
    EDIT_INTERVAL_SECONDS,
    EMPTY_RESPONSE_TEXT,
    PLACEHOLDER_TEXT,
    stream_response,
)


@dataclass
class FakeClock:
    current: float = 0.0

    def __call__(self) -> float:
        return self.current


class FakeSession:
    def __init__(
        self,
        chunks: Iterable[tuple[float, str]],
        *,
        clock: FakeClock,
        images: tuple[Any, ...] = (),
        thoughts: str | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._clock = clock
        self._images = images
        self._thoughts = thoughts
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _generate(self) -> AsyncIterator[SimpleNamespace]:
        full_text = ""
        for timestamp, delta in self._chunks:
            self._clock.current = timestamp
            full_text += delta
            chunk: dict[str, Any] = {
                "text_delta": delta,
                "text": full_text,
                "images": self._images,
            }
            if self._thoughts is not None:
                chunk["thoughts"] = self._thoughts
            yield SimpleNamespace(**chunk)

    def send_message_stream(
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


async def test_stream_uses_session_and_forwards_generation_options() -> None:
    clock = FakeClock()
    session = FakeSession([], clock=clock)
    message, _ = telegram_message()

    await stream_response(
        message,
        session,
        "hello",
        clock=clock,
        temporary=True,
        extended_thinking=True,
    )

    assert session.calls == [
        ("hello", {"temporary": True, "extended_thinking": True})
    ]


async def test_elapsed_interval_edits_first_and_keeps_partial_markdown_plain() -> None:
    clock = FakeClock()
    session = FakeSession(
        [(0.3, "**open"), (EDIT_INTERVAL_SECONDS, " still"), (1.6, "**")],
        clock=clock,
    )
    message, placeholder = telegram_message()

    result = await stream_response(message, session, "hello", clock=clock)

    assert result.text == "**open still**"
    assert result.output is not None
    assert result.output.text == "**open still**"
    assert session.calls == [("hello", {})]
    message.reply_text.assert_awaited_once_with(PLACEHOLDER_TEXT)
    assert placeholder.edit_text.await_args_list == [
        call("**open still", parse_mode=None),
        call("<b>open still</b>", parse_mode=ParseMode.HTML),
    ]


async def test_unformatted_final_edit_is_skipped_after_plain_stream_edit() -> None:
    clock = FakeClock()
    session = FakeSession([(EDIT_INTERVAL_SECONDS, "好的")], clock=clock)
    message, placeholder = telegram_message()

    result = await stream_response(message, session, "hello", clock=clock)

    assert result.text == "好的"
    placeholder.edit_text.assert_awaited_once_with("好的", parse_mode=None)


async def test_formatted_final_edit_is_sent_as_html() -> None:
    clock = FakeClock()
    session = FakeSession(
        [(EDIT_INTERVAL_SECONDS, "**粗體** 測試")],
        clock=clock,
    )
    message, placeholder = telegram_message()

    await stream_response(message, session, "hello", clock=clock)

    assert placeholder.edit_text.await_args_list == [
        call("**粗體** 測試", parse_mode=None),
        call("<b>粗體</b> 測試", parse_mode=ParseMode.HTML),
    ]


async def test_unchanged_intermediate_text_is_not_edited_again() -> None:
    clock = FakeClock()

    class RepeatedTextSession:
        async def generate(self) -> AsyncIterator[SimpleNamespace]:
            clock.current = EDIT_INTERVAL_SECONDS
            yield SimpleNamespace(text_delta="好的", text="好的", images=())
            clock.current = EDIT_INTERVAL_SECONDS * 2
            yield SimpleNamespace(text_delta="重送", text="好的", images=())

        def send_message_stream(
            self,
            prompt: str,
            **kwargs: Any,
        ) -> AsyncIterator[SimpleNamespace]:
            del prompt, kwargs
            return self.generate()

    message, placeholder = telegram_message()

    await stream_response(message, RepeatedTextSession(), "hello", clock=clock)

    placeholder.edit_text.assert_awaited_once_with("好的", parse_mode=None)


async def test_character_threshold_edits_before_interval() -> None:
    clock = FakeClock()
    first = "a" * (EDIT_CHARACTER_THRESHOLD - 1)
    session = FakeSession(
        [(0.1, first), (0.2, "b"), (0.3, "c")],
        clock=clock,
    )
    message, placeholder = telegram_message()

    await stream_response(message, session, "hello", clock=clock)

    assert placeholder.edit_text.await_args_list == [
        call(first + "b", parse_mode=None),
        call(first + "bc", parse_mode=ParseMode.HTML),
    ]


async def test_artifact_only_text_with_image_does_not_write_empty_response() -> None:
    clock = FakeClock()
    image = SimpleNamespace(url="https://example.test/generated.png")
    session = FakeSession(
        [(0.1, "\n\n_543\n\n")],
        clock=clock,
        images=(image,),
    )
    message, placeholder = telegram_message()

    result = await stream_response(message, session, "draw", clock=clock)

    assert result.output is not None
    assert result.output.images == (image,)
    placeholder.edit_text.assert_not_awaited()


async def test_artifact_only_text_without_image_writes_empty_response() -> None:
    clock = FakeClock()
    session = FakeSession([(0.1, "\n\n_543\n\n")], clock=clock)
    message, placeholder = telegram_message()

    await stream_response(message, session, "hello", clock=clock)

    placeholder.edit_text.assert_awaited_once_with(
        EMPTY_RESPONSE_TEXT,
        parse_mode=None,
    )


async def test_retry_after_waits_then_retries_the_same_edit() -> None:
    clock = FakeClock()
    session = FakeSession([(0.1, "x" * EDIT_CHARACTER_THRESHOLD)], clock=clock)
    message, placeholder = telegram_message()
    placeholder.edit_text.side_effect = [RetryAfter(3), None, None]
    sleep = AsyncMock()

    await stream_response(message, session, "hello", clock=clock, sleep=sleep)

    sleep.assert_awaited_once_with(3.0)
    interim = call("x" * EDIT_CHARACTER_THRESHOLD, parse_mode=None)
    assert placeholder.edit_text.await_args_list == [
        interim,
        interim,
    ]


async def test_retry_after_over_limit_fails_without_waiting() -> None:
    clock = FakeClock()
    session = FakeSession([], clock=clock)
    message, _ = telegram_message()
    message.reply_text.side_effect = RetryAfter(MAX_FLOOD_WAIT_SECONDS + 1)
    sleep = AsyncMock()

    with pytest.raises(FloodControlExceeded) as exc_info:
        await stream_response(message, session, "hello", clock=clock, sleep=sleep)

    assert exc_info.value.retry_after == MAX_FLOOD_WAIT_SECONDS + 1
    assert exc_info.value.waited_seconds == 0
    sleep.assert_not_awaited()
    assert message.reply_text.await_count == 1
    assert session.calls == []


async def test_flood_control_wait_releases_slot_for_another_user() -> None:
    queue = RequestQueue(
        max_concurrency=1,
        user_rate_limit_per_min=10,
        acquire_timeout=0.5,
    )
    clock = FakeClock()
    session = FakeSession([], clock=clock)
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
                session,
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
    session = FakeSession([(0.1, text)], clock=clock)
    message, placeholder = telegram_message()

    result = await stream_response(message, session, "hello", clock=clock)

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


async def test_thoughts_are_prepended_as_an_expandable_blockquote() -> None:
    """Reasoning shares the answer's message so nothing can arrive between them."""

    clock = FakeClock()
    session = FakeSession(
        [(0.1, "**答案**")],
        clock=clock,
        thoughts="先看資料規模，再確認迴圈層數",
    )
    message, placeholder = telegram_message()

    result = await stream_response(
        message,
        session,
        "question",
        clock=clock,
        sleep=AsyncMock(),
    )

    assert result.thoughts == "先看資料規模，再確認迴圈層數"
    final = placeholder.edit_text.await_args.args[0]
    assert final.startswith("<blockquote expandable>先看資料規模，再確認迴圈層數</blockquote>")
    assert final.endswith("<b>答案</b>")
    assert placeholder.edit_text.await_args.kwargs["parse_mode"] == ParseMode.HTML


async def test_thinking_elapsed_placeholder_is_throttled_and_finally_reported() -> None:
    clock = FakeClock()
    answer_ready = asyncio.Event()
    sleep_calls: list[float] = []

    class ThinkingSession:
        async def generate(self) -> AsyncIterator[SimpleNamespace]:
            yield SimpleNamespace(
                text_delta="",
                text="",
                thoughts="先分析問題",
                images=(),
            )
            await answer_ready.wait()
            yield SimpleNamespace(
                text_delta="答案",
                text="答案",
                thoughts="先分析問題",
                images=(),
            )

        def send_message_stream(
            self,
            prompt: str,
            **kwargs: Any,
        ) -> AsyncIterator[SimpleNamespace]:
            assert prompt == "question"
            assert kwargs == {"extended_thinking": True}
            return self.generate()

    async def advancing_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        clock.current += delay
        if len(sleep_calls) == 3:
            answer_ready.set()
        await asyncio.sleep(0)

    message, placeholder = telegram_message()

    await stream_response(
        message,
        ThinkingSession(),
        "question",
        language=LANGUAGE_CHINESE,
        clock=clock,
        sleep=advancing_sleep,
        extended_thinking=True,
    )

    chinese_placeholder = translate("stream.placeholder", LANGUAGE_CHINESE)
    thinking_edits = [
        edit.args[0]
        for edit in placeholder.edit_text.await_args_list
        if edit.args[0].startswith(chinese_placeholder)
    ]
    assert thinking_edits == ["思考中… 1 秒", "思考中… 3 秒"]
    assert len(thinking_edits) < int(clock.current)
    final = placeholder.edit_text.await_args_list[-1].args[0]
    assert final.startswith(
        "<blockquote expandable>思考過程（4 秒）\n先分析問題</blockquote>"
    )


async def test_disabled_thinking_does_not_add_placeholder_edits() -> None:
    clock = FakeClock()
    session = FakeSession(
        [(0.0, ""), (5.0, "答案")],
        clock=clock,
        thoughts="上游意外附帶的思考",
    )
    message, placeholder = telegram_message()

    await stream_response(
        message,
        session,
        "question",
        clock=clock,
        extended_thinking=False,
    )

    assert placeholder.edit_text.await_args_list == [
        call("答案", parse_mode=None),
        call(
            "<blockquote expandable>上游意外附帶的思考</blockquote>\n\n答案",
            parse_mode=ParseMode.HTML,
        ),
    ]


async def test_thinking_without_answer_measures_until_stream_end() -> None:
    clock = FakeClock(current=2.0)

    class ThoughtsOnlySession:
        async def generate(self) -> AsyncIterator[SimpleNamespace]:
            yield SimpleNamespace(
                text_delta="",
                text="",
                thoughts="只有思考內容",
                images=(),
            )
            clock.current = 8.8

        def send_message_stream(
            self,
            prompt: str,
            **kwargs: Any,
        ) -> AsyncIterator[SimpleNamespace]:
            del prompt, kwargs
            return self.generate()

    message, placeholder = telegram_message()

    await stream_response(
        message,
        ThoughtsOnlySession(),
        "question",
        language=LANGUAGE_CHINESE,
        clock=clock,
        extended_thinking=True,
    )

    assert placeholder.edit_text.await_args_list[-1] == call(
        "<blockquote expandable>思考過程（6 秒）\n只有思考內容</blockquote>"
            "\n\nGemini 未回傳文字。",
        parse_mode=ParseMode.HTML,
    )


async def test_long_thoughts_are_truncated_to_keep_one_message() -> None:
    """The answer must not be pushed into a second message by its own reasoning."""

    clock = FakeClock()
    session = FakeSession(
        [(0.1, "答案")],
        clock=clock,
        thoughts="思" * 8000,
    )
    message, placeholder = telegram_message()

    await stream_response(
        message,
        session,
        "question",
        language=LANGUAGE_CHINESE,
        clock=clock,
        sleep=AsyncMock(),
    )

    final = placeholder.edit_text.await_args.args[0]
    assert len(final) <= 4096
    assert "已截斷" in final
    assert final.endswith("答案")


async def test_missing_thoughts_leave_the_answer_untouched() -> None:
    """Models without reasoning must render exactly as before."""

    clock = FakeClock()
    session = FakeSession([(0.1, "**答案**")], clock=clock)
    message, placeholder = telegram_message()

    result = await stream_response(
        message,
        session,
        "question",
        clock=clock,
        sleep=AsyncMock(),
    )

    assert result.thoughts == ""
    assert placeholder.edit_text.await_args.args[0] == "<b>答案</b>"
