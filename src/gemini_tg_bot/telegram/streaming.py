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

from .rendering import MAX_MESSAGE_LENGTH, render_markdown_chunks
from .sending import (
    FloodControlExceeded,
    MAX_FLOOD_RETRIES,
    MAX_FLOOD_WAIT_SECONDS,
    edit_text,
    send_text,
)


PLACEHOLDER_TEXT = "思考中…"
EMPTY_RESPONSE_TEXT = "Gemini 未回傳文字。"
EDIT_INTERVAL_SECONDS = 1.5
EDIT_CHARACTER_THRESHOLD = 200

_Sleep = Callable[[float], Awaitable[None]]
_Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class StreamResult:
    """The final text and last complete upstream streaming output."""

    text: str
    output: Any | None


async def _cancel(task: asyncio.Future[Any] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def stream_response(
    message: Any,
    client: Any,
    prompt: str,
    *,
    placeholder_text: str = PLACEHOLDER_TEXT,
    edit_interval: float = EDIT_INTERVAL_SECONDS,
    edit_character_threshold: int = EDIT_CHARACTER_THRESHOLD,
    sleep: _Sleep = asyncio.sleep,
    flood_wait: _Sleep | None = None,
    clock: _Clock = time.monotonic,
    **generate_kwargs: Any,
) -> StreamResult:
    """Stream a Gemini response into a Telegram placeholder message.

    ``client`` is the application's existing singleton Gemini client.  Its
    ``generate_content_stream`` method is consumed directly according to the
    pinned upstream contract: each item has incremental ``text_delta`` and the
    complete response-so-far in ``text``.

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

    placeholder = await send_text(
        message,
        placeholder_text,
        sleep=sleep,
        flood_wait=flood_wait,
    )
    last_edit_at = clock()
    pending_characters = 0
    latest_text = ""
    latest_output: Any | None = None

    stream = client.generate_content_stream(prompt, **generate_kwargs)
    iterator = stream.__aiter__()
    next_chunk: asyncio.Future[Any] | None = asyncio.ensure_future(anext(iterator))
    timer: asyncio.Future[Any] | None = None

    async def edit_plain_text() -> None:
        nonlocal last_edit_at, pending_characters
        await edit_text(
            placeholder,
            latest_text,
            parse_mode=None,
            sleep=sleep,
            flood_wait=flood_wait,
        )
        pending_characters = 0
        last_edit_at = clock()

    try:
        while next_chunk is not None:
            can_edit = bool(latest_text) and len(latest_text) <= MAX_MESSAGE_LENGTH
            if can_edit and pending_characters:
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

    rendered_chunks = render_markdown_chunks(latest_text)
    if not rendered_chunks:
        images = (
            getattr(latest_output, "images", ())
            if latest_output is not None
            else ()
        )
        if images:
            return StreamResult(text=latest_text, output=latest_output)
        await edit_text(
            placeholder,
            EMPTY_RESPONSE_TEXT,
            parse_mode=None,
            sleep=sleep,
            flood_wait=flood_wait,
        )
        return StreamResult(text=latest_text, output=latest_output)

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
    return StreamResult(text=latest_text, output=latest_output)


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
