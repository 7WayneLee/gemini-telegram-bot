"""Concurrency and per-user rate limiting for Gemini requests."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable


SEMAPHORE_ACQUIRE_TIMEOUT_SECONDS = 30.0

_Sleep = Callable[[float], Awaitable[None]]


class RateLimitExceeded(Exception):
    """Raised when a user has exhausted their request tokens."""

    def __init__(self, user_id: int, retry_after: float) -> None:
        self.user_id = user_id
        self.retry_after = retry_after
        super().__init__(
            f"Rate limit exceeded for user {user_id}; retry after "
            f"{retry_after:.2f} seconds"
        )


class QueueAcquireTimeout(TimeoutError):
    """Raised when a request cannot obtain a concurrency slot in time."""

    def __init__(self, user_id: int, timeout: float) -> None:
        self.user_id = user_id
        self.timeout = timeout
        super().__init__(
            f"Request queue timed out for user {user_id} after {timeout:.2f} seconds"
        )


@dataclass
class _TokenBucket:
    tokens: float
    updated_at: float


class _RequestPermit:
    """Own one queue slot and allow flood-control sleeps without holding it."""

    def __init__(self, queue: RequestQueue, user_id: int) -> None:
        self._queue = queue
        self._user_id = user_id
        self._acquired = False

    async def acquire(self) -> None:
        if self._acquired:
            return
        await self._queue._acquire(self._user_id)
        self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        self._queue._semaphore.release()
        self._acquired = False

    async def wait_for_flood_control(
        self,
        delay: float,
        *,
        sleep: _Sleep = asyncio.sleep,
    ) -> None:
        """Release the slot while waiting, then reacquire it with a timeout."""

        self.release()
        await sleep(delay)
        await self.acquire()


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
        acquire_timeout: float = SEMAPHORE_ACQUIRE_TIMEOUT_SECONDS,
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
        if (
            isinstance(acquire_timeout, bool)
            or not isinstance(acquire_timeout, (int, float))
            or acquire_timeout <= 0
        ):
            raise ValueError("acquire_timeout must be positive")

        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._acquire_timeout = float(acquire_timeout)
        self._rate_limit = user_rate_limit_per_min
        self._refill_rate = user_rate_limit_per_min / 60.0
        self._buckets: dict[int, _TokenBucket] = {}
        self._queue_depth = 0

    @property
    def queue_depth(self) -> int:
        """Return the number of requests waiting for a concurrency slot."""

        return self._queue_depth

    @asynccontextmanager
    async def request(self, user_id: int) -> AsyncIterator[_RequestPermit]:
        """Consume a user token and hold a global concurrency slot.

        Rate-limited requests fail immediately instead of entering the queue.
        The semaphore is always released when the context exits, including
        when the request is cancelled or raises an exception.
        """

        self._consume_token(user_id)

        permit = _RequestPermit(self, user_id)
        await permit.acquire()
        try:
            yield permit
        finally:
            permit.release()

    async def _acquire(self, user_id: int) -> None:
        self._queue_depth += 1
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._acquire_timeout,
            )
        except TimeoutError as error:
            raise QueueAcquireTimeout(user_id, self._acquire_timeout) from error
        finally:
            self._queue_depth -= 1

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


__all__ = [
    "QueueAcquireTimeout",
    "RateLimitExceeded",
    "RequestQueue",
    "SEMAPHORE_ACQUIRE_TIMEOUT_SECONDS",
]
