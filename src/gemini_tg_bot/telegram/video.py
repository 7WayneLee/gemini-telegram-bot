"""Own disposable video conversations after their initial request completes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from gemini_tg_bot.gemini.errors import classify_error
from gemini_tg_bot.i18n import translate

from .media import MediaHandler, VideoTooLargeError
from .sending import delete_message, edit_text


LOGGER = logging.getLogger(__name__)
Finish = Callable[[bool, str | None], Awaitable[None]]


@dataclass(slots=True)
class _Job:
    finish: Finish
    task: asyncio.Task[None] | None = None
    finalized: bool = False

    async def complete(self, ok: bool, error_kind: str | None) -> None:
        if not self.finalized:
            self.finalized = True
            await self.finish(ok, error_kind)


class VideoJobManager:
    """Allow one video per chat without retaining a queue slot or chat lock."""

    def __init__(
        self,
        *,
        service: Any,
        media: MediaHandler,
        timeout_sec: int = 300,
        delivery_timeout_sec: int = 600,
        poll_interval: float = 10,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._media = media
        self._timeout_sec = timeout_sec
        self._delivery_timeout_sec = delivery_timeout_sec
        self._poll_interval = poll_interval
        self._sleep = sleep
        self._clock = clock
        self._jobs: dict[int, _Job | None] = {}

    def reserve(self, chat_id: int) -> bool:
        """Claim the chat before the initial send, without an intervening await."""

        if chat_id in self._jobs:
            return False
        self._jobs[chat_id] = None
        return True

    def release(self, chat_id: int) -> None:
        self._jobs.pop(chat_id, None)

    def start(
        self,
        chat_id: int,
        *,
        cid: str,
        message: Any,
        status: Any,
        language: str,
        finish: Finish,
    ) -> None:
        """Transfer the reserved chat and final accounting to a background task."""

        job = _Job(finish)
        self._jobs[chat_id] = job
        job.task = asyncio.create_task(
            self._run(chat_id, job, cid, message, status, language),
            name="video-generation",
        )

    async def _run(
        self, chat_id: int, job: _Job, cid: str, message: Any, status: Any, language: str
    ) -> None:
        ok = False
        error_kind: str | None = "cancelled"
        try:
            deadline = self._clock() + self._timeout_sec
            async with asyncio.timeout(self._timeout_sec):
                while True:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise TimeoutError
                    await self._sleep(min(self._poll_interval, remaining))
                    if self._clock() >= deadline:
                        raise TimeoutError
                    output = await self._service.execute(
                        lambda client: client.fetch_latest_chat_response(
                            cid, limit=5, match=lambda output: bool(output.videos)
                        )
                    )
                    # Upstream returns the newest output even if match failed.
                    if output is None or not output.videos:
                        continue
                    break
            async with asyncio.timeout(self._delivery_timeout_sec):
                await self._media.send_video(message, output.videos[0])
            try:
                await delete_message(status)
            except Exception as error:
                LOGGER.warning("Unable to delete video status (%s)", type(error).__name__)
            ok, error_kind = True, None
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            error_kind = "timeout"
            await self._show_error(status, "video.timeout", language)
        except VideoTooLargeError:
            error_kind = "too_large"
            await self._show_error(status, "video.too_large", language)
        except Exception as error:
            error_kind = classify_error(error).value
            LOGGER.warning("Video generation failed (%s)", type(error).__name__)
            await self._show_error(status, "video.unavailable", language)
        finally:
            try:
                await job.complete(ok, error_kind)
            finally:
                self.release(chat_id)

    async def _show_error(self, status: Any, key: str, language: str) -> None:
        try:
            await edit_text(status, translate(key, language), parse_mode=None)
        except Exception as error:
            LOGGER.warning("Unable to edit video status (%s)", type(error).__name__)

    async def close(self) -> None:
        """Cancel and account for every job, including tasks not yet started."""

        jobs = [job for job in self._jobs.values() if job is not None]
        tasks = [job.task for job in jobs if job.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in jobs:
            await job.complete(False, "cancelled")
        self._jobs.clear()
