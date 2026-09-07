"""Concurrency and per-user rate limiting for Gemini requests."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator


class RateLimitExceeded(Exception):
    """Raised when a user has exhausted their request tokens."""

    def __init__(self, user_id: int, retry_after: float) -> None:
        self.user_id = user_id
        self.retry_after = retry_after
        super().__init__(
            f"Rate limit exceeded for user {user_id}; retry after "
            f"{retry_after:.2f} seconds"
        )


@dataclass
class _TokenBucket:
    tokens: float
    updated_at: float


class RequestQueue:
    """Limit global concurrency and request rate independently per user.

    One queue instance owns one semaphore shared by all of its users. Each
    user's token bucket has a one-minute burst capacity and refills
    continuously at ``user_rate_limit_per_min / 60`` tokens per second.
    """

    def __init__(
        self,
        max_concurrency: int = 1,
        user_rate_limit_per_min: int = 10,
    ) -> None:
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency <= 0
        ):
            raise ValueError("max_concurrency must be a positive integer")
        if (
            isinstance(user_rate_limit_per_min, bool)
            or not isinstance(user_rate_limit_per_min, int)
            or user_rate_limit_per_min <= 0
        ):
            raise ValueError("user_rate_limit_per_min must be a positive integer")

        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._rate_limit = user_rate_limit_per_min
        self._refill_rate = user_rate_limit_per_min / 60.0
        self._buckets: dict[int, _TokenBucket] = {}
        self._queue_depth = 0

    @property
    def queue_depth(self) -> int:
        """Return the number of requests waiting for a concurrency slot."""

        return self._queue_depth

    @asynccontextmanager
    async def request(self, user_id: int) -> AsyncIterator[None]:
        """Consume a user token and hold a global concurrency slot.

        Rate-limited requests fail immediately instead of entering the queue.
        The semaphore is always released when the context exits, including
        when the request is cancelled or raises an exception.
        """

        self._consume_token(user_id)

        acquired = False
        self._queue_depth += 1
        try:
            await self._semaphore.acquire()
            acquired = True
        finally:
            self._queue_depth -= 1

        try:
            yield
        finally:
            if acquired:
                self._semaphore.release()

    def _consume_token(self, user_id: int) -> None:
        now = time.monotonic()
        bucket = self._buckets.get(user_id)

        if bucket is None:
            bucket = _TokenBucket(tokens=float(self._rate_limit), updated_at=now)
            self._buckets[user_id] = bucket
        elif now > bucket.updated_at:
            elapsed = now - bucket.updated_at
            bucket.tokens = min(
                float(self._rate_limit),
                bucket.tokens + elapsed * self._refill_rate,
            )
            bucket.updated_at = now

        if bucket.tokens < 1.0:
            retry_after = (1.0 - bucket.tokens) / self._refill_rate
            raise RateLimitExceeded(user_id=user_id, retry_after=retry_after)

        bucket.tokens -= 1.0


__all__ = ["RateLimitExceeded", "RequestQueue"]
