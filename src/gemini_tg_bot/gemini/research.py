"""Persistent background management for Gemini Deep Research tasks."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from gemini_webapi import DeepResearchPlan

from gemini_tg_bot.gemini.service import GeminiService
from gemini_tg_bot.i18n import DEFAULT_LANGUAGE, translate
from gemini_tg_bot.storage.db import Database


LOGGER = logging.getLogger(__name__)

ResearchNotifier = Callable[[int, str], Awaitable[None]]


class ResearchStatus(StrEnum):
    """Persisted states for one Deep Research task."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class ResearchTaskView:
    """Public, serialization-free view used by ``/research_status``."""

    task_id: str
    chat_id: int
    cid: str | None
    research_id: str | None
    prompt: str
    status: ResearchStatus
    result_path: str | None
    created_at: str
    updated_at: str


class ResearchManager:
    """Submit, persist, resume, and monitor Deep Research work.

    Upstream calls use the application-scoped :class:`GeminiService`; this
    class never creates a Gemini client.  Submission persists a ``pending``
    row and schedules the upstream work before returning its task id.
    """

    def __init__(
        self,
        service: GeminiService,
        database: Database,
        *,
        timeout_sec: float,
        poll_interval: float = 10.0,
        notify: ResearchNotifier | None = None,
        default_language: str = DEFAULT_LANGUAGE,
    ) -> None:
        if isinstance(timeout_sec, bool) or timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        if isinstance(poll_interval, bool) or poll_interval <= 0:
            raise ValueError("poll_interval must be positive")

        self._service = service
        self._database = database
        self._timeout_sec = float(timeout_sec)
        self._poll_interval = float(poll_interval)
        self._notify = notify
        self._default_language = default_language
        self._schema_lock = asyncio.Lock()
        self._schema_ready = False
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    async def submit(self, chat_id: int, prompt: str) -> str:
        """Persist a task and immediately return its id without upstream wait."""

        self._ensure_open()
        topic = prompt.strip()
        if not topic:
            raise ValueError("prompt must not be empty")
        await self._ensure_schema()

        task_id = uuid.uuid4().hex
        timestamp = _utc_now()
        connection = self._database.connection
        await connection.execute(
            """
            INSERT INTO research_tasks (
                task_id, chat_id, cid, research_id, prompt, status,
                result_path, created_at, updated_at, plan_json
            ) VALUES (?, ?, NULL, NULL, ?, ?, NULL, ?, ?, NULL)
            """,
            (
                task_id,
                chat_id,
                topic,
                ResearchStatus.PENDING.value,
                timestamp,
                timestamp,
            ),
        )
        await connection.commit()

        self._spawn(
            task_id,
            self._start_and_poll(task_id, chat_id, topic, timestamp),
        )
        return task_id

    async def status(self, chat_id: int) -> list[ResearchTaskView]:
        """Return all persisted tasks for one Telegram chat."""

        self._ensure_open()
        await self._ensure_schema()
        async with self._database.connection.execute(
            """
            SELECT task_id, chat_id, cid, research_id, prompt, status,
                   result_path, created_at, updated_at
            FROM research_tasks
            WHERE chat_id = ?
            ORDER BY created_at, task_id
            """,
            (chat_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_view_from_row(row) for row in rows]

    async def restore_running(self) -> int:
        """Schedule polling for every recoverable persisted ``running`` task."""

        self._ensure_open()
        await self._ensure_schema()
        async with self._database.connection.execute(
            """
            SELECT task_id, chat_id, created_at, plan_json
            FROM research_tasks
            WHERE status = ?
            ORDER BY created_at, task_id
            """,
            (ResearchStatus.RUNNING.value,),
        ) as cursor:
            rows = await cursor.fetchall()

        restored = 0
        for row in rows:
            task_id = row["task_id"]
            if task_id in self._background_tasks:
                continue
            plan_json = row["plan_json"]
            if not plan_json:
                await self._finish(
                    task_id,
                    row["chat_id"],
                    ResearchStatus.FAILED,
                )
                continue
            try:
                plan = DeepResearchPlan.model_validate_json(plan_json)
            except Exception as error:
                _log_failure("restore plan", task_id, error)
                await self._finish(
                    task_id,
                    row["chat_id"],
                    ResearchStatus.FAILED,
                )
                continue

            remaining = self._remaining_seconds(row["created_at"])
            if remaining <= 0:
                await self._finish(
                    task_id,
                    row["chat_id"],
                    ResearchStatus.TIMEOUT,
                )
                continue

            self._spawn(
                task_id,
                self._resume_poll(
                    task_id,
                    row["chat_id"],
                    plan,
                    remaining,
                ),
            )
            restored += 1
        return restored

    async def close(self) -> None:
        """Cancel owned background tasks; repeated calls are harmless."""

        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._background_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            await self._database.connect()
            connection = self._database.connection
            async with connection.execute(
                "PRAGMA table_info(research_tasks)"
            ) as cursor:
                columns = await cursor.fetchall()
            if not any(row["name"] == "plan_json" for row in columns):
                await connection.execute(
                    "ALTER TABLE research_tasks ADD COLUMN plan_json TEXT"
                )
                await connection.commit()
            self._schema_ready = True

    async def _start_and_poll(
        self,
        task_id: str,
        chat_id: int,
        prompt: str,
        created_at: str,
    ) -> None:
        plan: DeepResearchPlan | None = None
        try:
            remaining = self._remaining_seconds(created_at)
            if remaining <= 0:
                await self._finish(task_id, chat_id, ResearchStatus.TIMEOUT)
                return
            async with asyncio.timeout(remaining):
                plan = await self._service.execute(
                    lambda client: client.create_deep_research_plan(prompt)
                )
                await self._service.execute(
                    lambda client: client.start_deep_research(plan)
                )
                await self._mark_running(task_id, plan)
                remaining = self._remaining_seconds(created_at)
                if remaining <= 0:
                    await self._finish(
                        task_id,
                        chat_id,
                        ResearchStatus.TIMEOUT,
                    )
                    return
                await self._poll(task_id, chat_id, plan, remaining)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self._finish(
                task_id,
                chat_id,
                ResearchStatus.TIMEOUT,
                plan=plan,
            )
        except Exception as error:
            _log_failure("research task", task_id, error)
            await self._finish(
                task_id,
                chat_id,
                ResearchStatus.FAILED,
                plan=plan,
            )

    async def _resume_poll(
        self,
        task_id: str,
        chat_id: int,
        plan: DeepResearchPlan,
        remaining: float,
    ) -> None:
        try:
            async with asyncio.timeout(remaining):
                await self._poll(task_id, chat_id, plan, remaining)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self._finish(task_id, chat_id, ResearchStatus.TIMEOUT)
        except Exception as error:
            _log_failure("restored research task", task_id, error)
            await self._finish(task_id, chat_id, ResearchStatus.FAILED)

    async def _poll(
        self,
        task_id: str,
        chat_id: int,
        plan: DeepResearchPlan,
        remaining: float,
    ) -> None:
        result = await self._service.execute(
            lambda client: client.wait_for_deep_research(
                plan,
                poll_interval=self._poll_interval,
                timeout=remaining,
            )
        )
        if not result.done:
            await self._finish(task_id, chat_id, ResearchStatus.TIMEOUT)
            return

        completed_plan = result.plan
        await self._finish(
            task_id,
            chat_id,
            ResearchStatus.DONE,
            plan=completed_plan,
        )
        text = result.final_output.text.strip()
        message = translate(
            "research.completed",
            self._default_language,
            task_id=task_id,
        )
        if text:
            message = f"{message}\n\n{text}"
        await self._send_notification(chat_id, message)

    async def _mark_running(
        self,
        task_id: str,
        plan: DeepResearchPlan,
    ) -> None:
        await self._database.connection.execute(
            """
            UPDATE research_tasks
            SET cid = ?, research_id = ?, status = ?, plan_json = ?,
                updated_at = ?
            WHERE task_id = ?
            """,
            (
                plan.cid,
                plan.research_id,
                ResearchStatus.RUNNING.value,
                plan.model_dump_json(),
                _utc_now(),
                task_id,
            ),
        )
        await self._database.connection.commit()

    async def _finish(
        self,
        task_id: str,
        chat_id: int,
        status: ResearchStatus,
        *,
        plan: DeepResearchPlan | None = None,
    ) -> None:
        if plan is None:
            await self._database.connection.execute(
                """
                UPDATE research_tasks
                SET status = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (status.value, _utc_now(), task_id),
            )
        else:
            await self._database.connection.execute(
                """
                UPDATE research_tasks
                SET cid = ?, research_id = ?, status = ?, plan_json = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    plan.cid,
                    plan.research_id,
                    status.value,
                    plan.model_dump_json(),
                    _utc_now(),
                    task_id,
                ),
            )
        await self._database.connection.commit()

        if status is ResearchStatus.FAILED:
            await self._send_notification(
                chat_id,
                translate(
                    "research.failed",
                    self._default_language,
                    task_id=task_id,
                ),
            )
        elif status is ResearchStatus.TIMEOUT:
            await self._send_notification(
                chat_id,
                translate(
                    "research.timeout",
                    self._default_language,
                    task_id=task_id,
                ),
            )

    async def _send_notification(self, chat_id: int, message: str) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(chat_id, message)
        except Exception as error:
            _log_failure("research notification", "notification", error)

    def _remaining_seconds(self, created_at: str) -> float:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        elapsed = (datetime.now(UTC) - created.astimezone(UTC)).total_seconds()
        return self._timeout_sec - max(0.0, elapsed)

    def _spawn(self, task_id: str, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine, name=f"research:{task_id}")
        self._background_tasks[task_id] = task

        def discard(completed: asyncio.Task[None]) -> None:
            if self._background_tasks.get(task_id) is completed:
                self._background_tasks.pop(task_id, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(discard)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("research manager is closed")


def _view_from_row(row: Any) -> ResearchTaskView:
    return ResearchTaskView(
        task_id=row["task_id"],
        chat_id=row["chat_id"],
        cid=row["cid"],
        research_id=row["research_id"],
        prompt=row["prompt"],
        status=ResearchStatus(row["status"]),
        result_path=row["result_path"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _log_failure(action: str, task_id: str, error: Exception) -> None:
    # Upstream exception strings are intentionally excluded: callers may
    # provide secrets, and no credential value may reach logs.
    LOGGER.warning(
        "%s failed task_id=%s error_type=%s",
        action,
        task_id,
        type(error).__name__,
    )
