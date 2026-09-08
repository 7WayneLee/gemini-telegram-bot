import asyncio
from unittest.mock import patch

import pytest

from gemini_tg_bot.queue import QueueAcquireTimeout, RateLimitExceeded, RequestQueue


async def test_second_request_waits_and_queue_depth_tracks_waiters() -> None:
    queue = RequestQueue(max_concurrency=1, user_rate_limit_per_min=10)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_request() -> None:
        async with queue.request(user_id=1):
            first_entered.set()
            await release_first.wait()

    async def second_request() -> None:
        async with queue.request(user_id=2):
            second_entered.set()

    first_task = asyncio.create_task(first_request())
    await first_entered.wait()
    assert queue.queue_depth == 0

    second_task = asyncio.create_task(second_request())
    await asyncio.sleep(0)

    assert not second_entered.is_set()
    assert queue.queue_depth == 1

    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert second_entered.is_set()
    assert queue.queue_depth == 0


async def test_token_bucket_refills_continuously() -> None:
    queue = RequestQueue(max_concurrency=1, user_rate_limit_per_min=2)

    with patch("gemini_tg_bot.queue.time.monotonic", return_value=100.0):
        async with queue.request(user_id=1):
            pass
        async with queue.request(user_id=1):
            pass

        with pytest.raises(RateLimitExceeded) as exc_info:
            async with queue.request(user_id=1):
                pass

    assert exc_info.value.user_id == 1
    assert exc_info.value.retry_after == pytest.approx(30.0)

    with patch("gemini_tg_bot.queue.time.monotonic", return_value=130.0):
        async with queue.request(user_id=1):
            pass


async def test_token_buckets_are_isolated_per_user() -> None:
    queue = RequestQueue(max_concurrency=1, user_rate_limit_per_min=1)

    with patch("gemini_tg_bot.queue.time.monotonic", return_value=100.0):
        async with queue.request(user_id=1):
            pass

        with pytest.raises(RateLimitExceeded):
            async with queue.request(user_id=1):
                pass

        async with queue.request(user_id=2):
            pass


async def test_concurrency_slot_is_released_after_error() -> None:
    queue = RequestQueue(max_concurrency=1, user_rate_limit_per_min=10)

    with pytest.raises(RuntimeError):
        async with queue.request(user_id=1):
            raise RuntimeError("request failed")

    async with queue.request(user_id=2):
        pass


async def test_semaphore_acquire_times_out_and_leaves_queue_usable() -> None:
    queue = RequestQueue(
        max_concurrency=1,
        user_rate_limit_per_min=10,
        acquire_timeout=0.01,
    )

    async with queue.request(user_id=1):
        with pytest.raises(QueueAcquireTimeout) as exc_info:
            async with queue.request(user_id=2):
                pass

    assert exc_info.value.user_id == 2
    assert exc_info.value.timeout == pytest.approx(0.01)
    assert queue.queue_depth == 0
    async with queue.request(user_id=3):
        pass


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("max_concurrency", 0),
        ("max_concurrency", True),
        ("user_rate_limit_per_min", 0),
        ("user_rate_limit_per_min", True),
        ("acquire_timeout", 0),
        ("acquire_timeout", True),
    ],
)
def test_configuration_must_be_positive_integers(keyword: str, value: object) -> None:
    with pytest.raises(ValueError):
        RequestQueue(**{keyword: value})  # type: ignore[arg-type]
