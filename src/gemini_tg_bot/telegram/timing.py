"""Content-free diagnostics scoped to one interactive request."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


LOGGER = logging.getLogger("gemini_tg_bot.timing")


@dataclass(slots=True)
class RequestTiming:
    """Accumulate only durations and counts, never request content or identities."""

    clock: Callable[[], float]
    slot_wait: float | None = None
    first_output: float | None = None
    generation: float | None = None
    upstream: float | None = None
    telegram: float | None = None
    flood_wait: float | None = None
    network_retries: int | None = None
    thought_chars: int = 0
    text_chars: int = 0

    @contextmanager
    def telegram_call(self) -> Iterator[None]:
        """Count a Telegram await, including transport retry waits when present."""

        started = self.clock()
        if self.telegram is None:
            self.telegram = 0.0
            self.flood_wait = 0.0
            self.network_retries = 0
        try:
            yield
        finally:
            self.telegram += self.clock() - started

    def start_upstream(self, clock: Callable[[], float] | None = None) -> UpstreamTiming:
        """Measure each upstream attempt while retaining request-wide totals."""

        self.thought_chars = self.text_chars = 0
        return UpstreamTiming(self, clock or self.clock)

    def log(
        self,
        *,
        chat: str,
        command: str,
        model: str | None,
        level: str,
        outcome: str,
        total: float,
    ) -> None:
        def seconds(value: float | None) -> str:
            return "-" if value is None else f"{value:.1f}"

        LOGGER.info(
            "Request timing chat=%s command=%s model=%s level=%s outcome=%s "
            "slot_wait=%s first_output=%s generation=%s upstream=%s telegram=%s "
            "flood_wait=%s network_retries=%s total=%.1f thought_chars=%d text_chars=%d",
            chat,
            command,
            model or "default",
            level,
            outcome,
            seconds(self.slot_wait),
            seconds(self.first_output),
            seconds(self.generation),
            seconds(self.upstream),
            seconds(self.telegram),
            seconds(self.flood_wait),
            "-" if self.network_retries is None else self.network_retries,
            total,
            self.thought_chars,
            self.text_chars,
        )


class UpstreamTiming:
    """Observe stream completion in its reader, before Telegram rendering waits."""

    def __init__(self, request: RequestTiming, clock: Callable[[], float]) -> None:
        self.request = request
        self.clock = clock
        self.started = clock()
        self.first_at: float | None = None
        self.finished = False

    def observe(self, text: str, thoughts: str | None) -> None:
        self.request.text_chars = len(text)
        if thoughts:
            self.request.thought_chars = len(thoughts)
        if self.first_at is None and (text or thoughts):
            self.first_at = self.clock()
            if self.request.first_output is None:
                self.request.first_output = self.first_at - self.started

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        ended = self.clock()
        self.request.upstream = (self.request.upstream or 0.0) + ended - self.started
        if self.first_at is not None:
            self.request.generation = (
                (self.request.generation or 0.0) + ended - self.first_at
            )


# Child tasks inherit the current context; independent requests get their own
# object, and handlers reset the token after their usage/timing records finish.
CURRENT_TIMING: ContextVar[RequestTiming | None] = ContextVar(
    "interactive_request_timing", default=None
)
