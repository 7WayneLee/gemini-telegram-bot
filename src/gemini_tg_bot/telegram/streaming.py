"""Throttle Gemini streaming updates sent through the Telegram Bot API.

Streaming edits intentionally use plain text.  Gemini Markdown is rendered only
after the upstream async generator is exhausted, so an incomplete Markdown tag
or code fence can never be submitted to Telegram's HTML parser.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
import time
from typing import Any

from telegram.constants import ParseMode

from gemini_tg_bot.i18n import DEFAULT_LANGUAGE, translate

from .rendering import (
    MAX_MESSAGE_LENGTH,
    render_markdown_chunks,
    render_thoughts_blockquote,
)
from .sending import (
    FloodControlExceeded,
    MAX_FLOOD_RETRIES,
    MAX_FLOOD_WAIT_SECONDS,
    edit_text,
    send_text,
)


PLACEHOLDER_TEXT = translate("stream.placeholder", DEFAULT_LANGUAGE)
EMPTY_RESPONSE_TEXT = translate("stream.empty", DEFAULT_LANGUAGE)
EDIT_INTERVAL_SECONDS = 1.5
EDIT_CHARACTER_THRESHOLD = 200

_Sleep = Callable[[float], Awaitable[None]]
_Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class StreamResult:
    """The final text and last complete upstream streaming output."""

    text: str
    output: Any | None
    thoughts: str = ""


async def _cancel(task: asyncio.Future[Any] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def stream_response(
    message: Any,
    session: Any,
    prompt: str,
    *,
    language: str = DEFAULT_LANGUAGE,
    placeholder_text: str | None = None,
    edit_interval: float = EDIT_INTERVAL_SECONDS,
    edit_character_threshold: int = EDIT_CHARACTER_THRESHOLD,
    sleep: _Sleep = asyncio.sleep,
    flood_wait: _Sleep | None = None,
    clock: _Clock = time.monotonic,
    **generate_kwargs: Any,
) -> StreamResult:
    """Stream a Gemini response into a Telegram placeholder message.

    ``session`` is the current upstream ``ChatSession``.  Its
    ``send_message_stream`` method forwards session-owned model and Gem state;
    each yielded item has incremental ``text_delta`` and the complete
    response-so-far in ``text``.

    An intermediate edit happens after either ``edit_interval`` seconds or
    ``edit_character_threshold`` accumulated delta characters, whichever
    occurs first.  Content beyond Telegram's safe single-message limit is not
    edited mid-stream; final rendering splits it into independently valid HTML
    messages instead.

    The complete unrendered Gemini text and the final ``ModelOutput`` are
    returned for callers that need to persist response metadata or deliver
    generated media after the stream finishes.
    """

    if edit_interval <= 0:
        raise ValueError("edit_interval must be positive")
    if edit_character_threshold <= 0:
        raise ValueError("edit_character_threshold must be positive")

    resolved_placeholder = (
        translate("stream.placeholder", language)
        if placeholder_text is None
        else placeholder_text
    )
    placeholder = await send_text(
        message,
        resolved_placeholder,
        sleep=sleep,
        flood_wait=flood_wait,
    )
    last_edit_at = clock()
    pending_characters = 0
    latest_text = ""
    latest_thoughts = ""
    latest_output: Any | None = None
    last_sent_text = resolved_placeholder
    extended_thinking = bool(generate_kwargs.get("extended_thinking", False))
    thoughts_started_at: float | None = None
    answer_started_at: float | None = None

    stream = session.send_message_stream(prompt, **generate_kwargs)
    iterator = stream.__aiter__()
    next_chunk: asyncio.Future[Any] | None = asyncio.ensure_future(anext(iterator))
    timer: asyncio.Future[Any] | None = None

    async def edit_plain_text() -> None:
        nonlocal last_edit_at, last_sent_text, pending_characters
        if latest_text:
            edit_content = latest_text
        elif (
            extended_thinking
            and latest_thoughts
            and thoughts_started_at is not None
        ):
            elapsed_seconds = int(max(0.0, clock() - thoughts_started_at))
            edit_content = translate(
                (
                    "stream.thinking_elapsed.one"
                    if elapsed_seconds == 1
                    else "stream.thinking_elapsed.many"
                ),
                language,
                placeholder=resolved_placeholder,
                seconds=elapsed_seconds,
            )
        else:
            edit_content = resolved_placeholder
        if edit_content != last_sent_text:
            await edit_text(
                placeholder,
                edit_content,
                parse_mode=None,
                sleep=sleep,
                flood_wait=flood_wait,
            )
            last_sent_text = edit_content
        pending_characters = 0
        last_edit_at = clock()

    try:
        while next_chunk is not None:
            can_edit = bool(latest_text) and len(latest_text) <= MAX_MESSAGE_LENGTH
            showing_thoughts = (
                extended_thinking
                and bool(latest_thoughts)
                and not latest_text
                and thoughts_started_at is not None
            )
            if (can_edit and pending_characters) or showing_thoughts:
                remaining = edit_interval - (clock() - last_edit_at)
                if remaining <= 0:
                    await edit_plain_text()
                    continue

                timer = asyncio.ensure_future(sleep(remaining))
                done, _ = await asyncio.wait(
                    {next_chunk, timer},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if next_chunk not in done:
                    timer.result()
                    timer = None
                    await edit_plain_text()
                    continue
                await _cancel(timer)
                timer = None

            try:
                chunk = await next_chunk
            except StopAsyncIteration:
                next_chunk = None
                break

            next_chunk = asyncio.ensure_future(anext(iterator))
            latest_output = chunk
            latest_text = chunk.text
            # Reasoning arrives before the answer and is absent on models that
            # do not support it, so read it defensively and keep the last value.
            chunk_thoughts = getattr(chunk, "thoughts", None)
            observed_at = clock()
            if chunk_thoughts and thoughts_started_at is None:
                thoughts_started_at = observed_at
            latest_thoughts = chunk_thoughts or latest_thoughts
            if latest_text and answer_started_at is None:
                answer_started_at = observed_at
            pending_characters += len(chunk.text_delta)

            can_edit = bool(latest_text) and len(latest_text) <= MAX_MESSAGE_LENGTH
            interval_elapsed = clock() - last_edit_at >= edit_interval
            if can_edit and (
                pending_characters >= edit_character_threshold or interval_elapsed
            ):
                await edit_plain_text()
    finally:
        await _cancel(timer)
        await _cancel(next_chunk)

    thought_seconds = (
        max(
            0.0,
            (answer_started_at if answer_started_at is not None else clock())
            - thoughts_started_at,
        )
        if extended_thinking and thoughts_started_at is not None
        else None
    )
    rendered_chunks = render_markdown_chunks(latest_text)
    if not rendered_chunks:
        images = (
            getattr(latest_output, "images", ())
            if latest_output is not None
            else ()
        )
        if latest_thoughts and thought_seconds is not None:
            suffix = (
                ""
                if images
                else f"\n\n{translate('stream.empty', language)}"
            )
            quote = render_thoughts_blockquote(
                latest_thoughts,
                budget=MAX_MESSAGE_LENGTH - len(suffix),
                seconds=thought_seconds,
                language=language,
            )
            if quote:
                await edit_text(
                    placeholder,
                    f"{quote}{suffix}",
                    parse_mode=ParseMode.HTML,
                    sleep=sleep,
                    flood_wait=flood_wait,
                )
                return StreamResult(
                    text=latest_text,
                    output=latest_output,
                    thoughts=latest_thoughts,
                )
        if images:
            return StreamResult(
                text=latest_text,
                output=latest_output,
                thoughts=latest_thoughts,
            )
        await edit_text(
            placeholder,
            translate("stream.empty", language),
            parse_mode=None,
            sleep=sleep,
            flood_wait=flood_wait,
        )
        return StreamResult(
            text=latest_text,
            output=latest_output,
            thoughts=latest_thoughts,
        )

    if latest_thoughts:
        # Share one message with the answer so nothing can arrive between them.
        # The reasoning is supplementary, so it yields the space rather than
        # pushing the answer into a second message.
        separator = "\n\n"
        quote = render_thoughts_blockquote(
            latest_thoughts,
            budget=MAX_MESSAGE_LENGTH - len(rendered_chunks[0]) - len(separator),
            seconds=thought_seconds,
            language=language,
        )
        if quote:
            rendered_chunks[0] = f"{quote}{separator}{rendered_chunks[0]}"

    if rendered_chunks[0] != last_sent_text:
        await edit_text(
            placeholder,
            rendered_chunks[0],
            parse_mode=ParseMode.HTML,
            sleep=sleep,
            flood_wait=flood_wait,
        )
    for rendered_chunk in rendered_chunks[1:]:
        await send_text(
            message,
            rendered_chunk,
            parse_mode=ParseMode.HTML,
            sleep=sleep,
            flood_wait=flood_wait,
        )
    return StreamResult(
        text=latest_text,
        output=latest_output,
        thoughts=latest_thoughts,
    )


__all__ = [
    "EDIT_CHARACTER_THRESHOLD",
    "EDIT_INTERVAL_SECONDS",
    "EMPTY_RESPONSE_TEXT",
    "FloodControlExceeded",
    "MAX_FLOOD_RETRIES",
    "MAX_FLOOD_WAIT_SECONDS",
    "PLACEHOLDER_TEXT",
    "StreamResult",
    "stream_response",
]
