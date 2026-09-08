"""Mock-only tests for persistent Deep Research background work."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from gemini_tg_bot.gemini import research as research_module
from gemini_tg_bot.gemini.research import ResearchManager, ResearchStatus
from gemini_tg_bot.storage.db import Database


class FakePlan(BaseModel):
    research_id: str
    cid: str
    title: str = "Test research"


class FakeService:
    """Exercise service-bound operations without constructing GeminiClient."""

    def __init__(self, client: SimpleNamespace) -> None:
        self.client = client

    async def execute(self, operation):
        result = operation(self.client)
        if inspect.isawaitable(result):
            return await result
        return result


@pytest.fixture(autouse=True)
def fake_plan_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research_module, "DeepResearchPlan", FakePlan)


def _client(
    *,
    create: AsyncMock | None = None,
    start: AsyncMock | None = None,
    wait: AsyncMock | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        create_deep_research_plan=create or AsyncMock(),
        start_deep_research=start or AsyncMock(),
        wait_for_deep_research=wait or AsyncMock(),
    )


async def _wait_for_status(
    manager: ResearchManager,
    chat_id: int,
    expected: ResearchStatus,
    *,
    attempts: int = 100,
) -> None:
    for _ in range(attempts):
        tasks = await manager.status(chat_id)
        if tasks and tasks[0].status is expected:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"task did not reach {expected.value}")


@pytest.mark.asyncio
async def test_submit_returns_id_while_upstream_work_remains_pending(tmp_path) -> None:
    create_started = asyncio.Event()
    release_create = asyncio.Event()

    async def create_plan(prompt: str) -> FakePlan:
        assert prompt == "Research this"
        create_started.set()
        await release_create.wait()
        return FakePlan(research_id="research-1", cid="cid-1")

    client = _client(create=AsyncMock(side_effect=create_plan))
    database = Database(tmp_path / "bot.sqlite3")
    manager = ResearchManager(
        FakeService(client),  # type: ignore[arg-type]
        database,
        timeout_sec=30,
    )

    task_id = await manager.submit(202, "  Research this  ")

    assert len(task_id) == 32
    await asyncio.wait_for(create_started.wait(), timeout=1)
    [task] = await manager.status(202)
    assert task.task_id == task_id
    assert task.prompt == "Research this"
    assert task.status is ResearchStatus.PENDING
    assert task.cid is None
    assert task.research_id is None

    await manager.close()
    await database.close()


@pytest.mark.asyncio
async def test_completed_task_is_persisted_and_proactively_notified(tmp_path) -> None:
    plan = FakePlan(research_id="research-2", cid="cid-2")
    result = SimpleNamespace(
        done=True,
        plan=plan,
        final_output=SimpleNamespace(text="Finished report"),
    )
    create = AsyncMock(return_value=plan)
    start = AsyncMock(return_value=SimpleNamespace())
    wait = AsyncMock(return_value=result)
    client = _client(create=create, start=start, wait=wait)
    notify = AsyncMock()
    database = Database(tmp_path / "bot.sqlite3")
    manager = ResearchManager(
        FakeService(client),  # type: ignore[arg-type]
        database,
        timeout_sec=30,
        poll_interval=0.25,
        notify=notify,
    )

    task_id = await manager.submit(202, "A topic")
    await _wait_for_status(manager, 202, ResearchStatus.DONE)

    create.assert_awaited_once_with("A topic")
    start.assert_awaited_once_with(plan)
    wait.assert_awaited_once()
    assert wait.await_args.args == (plan,)
    assert wait.await_args.kwargs["poll_interval"] == 0.25
    assert 0 < wait.await_args.kwargs["timeout"] <= 30
    [task] = await manager.status(202)
    assert task.cid == "cid-2"
    assert task.research_id == "research-2"
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == 202
    assert task_id in notify.await_args.args[1]
    assert "Finished report" in notify.await_args.args[1]

    async with database.connection.execute(
        "SELECT plan_json FROM research_tasks WHERE task_id = ?",
        (task_id,),
    ) as cursor:
        stored = await cursor.fetchone()
    assert stored is not None
    assert FakePlan.model_validate_json(stored["plan_json"]) == plan

    await manager.close()
    await database.close()


@pytest.mark.asyncio
async def test_task_exceeding_timeout_keeps_cid_and_marks_timeout(tmp_path) -> None:
    plan = FakePlan(research_id="research-timeout", cid="cid-timeout")
    never_finishes = asyncio.Event()

    async def wait_for_research(*args, **kwargs):
        del args, kwargs
        await never_finishes.wait()

    notify = AsyncMock()
    client = _client(
        create=AsyncMock(return_value=plan),
        start=AsyncMock(return_value=SimpleNamespace()),
        wait=AsyncMock(side_effect=wait_for_research),
    )
    database = Database(tmp_path / "bot.sqlite3")
    manager = ResearchManager(
        FakeService(client),  # type: ignore[arg-type]
        database,
        timeout_sec=0.03,
        poll_interval=0.01,
        notify=notify,
    )

    task_id = await manager.submit(303, "Slow topic")
    await _wait_for_status(manager, 303, ResearchStatus.TIMEOUT)

    [task] = await manager.status(303)
    assert task.cid == "cid-timeout"
    assert task.research_id == "research-timeout"
    notify.assert_awaited_once()
    assert task_id in notify.await_args.args[1]
    assert "/research_status" in notify.await_args.args[1]

    await manager.close()
    await database.close()


@pytest.mark.asyncio
async def test_incomplete_upstream_result_is_timeout(tmp_path) -> None:
    plan = FakePlan(research_id="research-incomplete", cid="cid-incomplete")
    result = SimpleNamespace(done=False, plan=plan, final_output=None)
    client = _client(
        create=AsyncMock(return_value=plan),
        start=AsyncMock(return_value=SimpleNamespace()),
        wait=AsyncMock(return_value=result),
    )
    database = Database(tmp_path / "bot.sqlite3")
    manager = ResearchManager(
        FakeService(client),  # type: ignore[arg-type]
        database,
        timeout_sec=30,
    )

    await manager.submit(404, "Incomplete topic")
    await _wait_for_status(manager, 404, ResearchStatus.TIMEOUT)

    await manager.close()
    await database.close()


@pytest.mark.asyncio
async def test_failure_is_persisted_without_exposing_error_text(tmp_path) -> None:
    notify = AsyncMock()
    client = _client(
        create=AsyncMock(side_effect=RuntimeError("sensitive upstream detail")),
    )
    database = Database(tmp_path / "bot.sqlite3")
    manager = ResearchManager(
        FakeService(client),  # type: ignore[arg-type]
        database,
        timeout_sec=30,
        notify=notify,
    )

    task_id = await manager.submit(505, "Failing topic")
    await _wait_for_status(manager, 505, ResearchStatus.FAILED)

    notify.assert_awaited_once()
    notification = notify.await_args.args[1]
    assert task_id in notification
    assert "sensitive upstream detail" not in notification

    await manager.close()
    await database.close()


@pytest.mark.asyncio
async def test_restart_restores_running_task_polling(tmp_path) -> None:
    """The DoD-required restart case resumes only the polling stage."""

    database_path = tmp_path / "bot.sqlite3"
    plan = FakePlan(research_id="research-restart", cid="cid-restart")
    first_wait_started = asyncio.Event()
    hold_first_process = asyncio.Event()

    async def first_wait(*args, **kwargs):
        del args, kwargs
        first_wait_started.set()
        await hold_first_process.wait()

    database = Database(database_path)
    first_client = _client(
        create=AsyncMock(return_value=plan),
        start=AsyncMock(return_value=SimpleNamespace()),
        wait=AsyncMock(side_effect=first_wait),
    )
    first_manager = ResearchManager(
        FakeService(first_client),  # type: ignore[arg-type]
        database,
        timeout_sec=30,
    )
    task_id = await first_manager.submit(606, "Survive restart")
    await asyncio.wait_for(first_wait_started.wait(), timeout=1)
    await _wait_for_status(first_manager, 606, ResearchStatus.RUNNING)

    await first_manager.close()
    interrupted = await _raw_status(database, task_id)
    assert interrupted == ResearchStatus.RUNNING.value
    await database.close()

    completed = SimpleNamespace(
        done=True,
        plan=plan,
        final_output=SimpleNamespace(text="Recovered report"),
    )
    second_create = AsyncMock()
    second_start = AsyncMock()
    second_wait = AsyncMock(return_value=completed)
    notify = AsyncMock()
    second_client = _client(
        create=second_create,
        start=second_start,
        wait=second_wait,
    )
    second_database = Database(database_path)
    second_manager = ResearchManager(
        FakeService(second_client),  # type: ignore[arg-type]
        second_database,
        timeout_sec=30,
        notify=notify,
    )

    assert await second_manager.restore_running() == 1
    await _wait_for_status(second_manager, 606, ResearchStatus.DONE)

    second_create.assert_not_awaited()
    second_start.assert_not_awaited()
    second_wait.assert_awaited_once()
    restored_plan = second_wait.await_args.args[0]
    assert isinstance(restored_plan, FakePlan)
    assert restored_plan == plan
    notify.assert_awaited_once()
    assert "Recovered report" in notify.await_args.args[1]

    await second_manager.close()
    await second_manager.close()
    await second_database.close()


async def _raw_status(database: Database, task_id: str) -> str:
    async with database.connection.execute(
        "SELECT status FROM research_tasks WHERE task_id = ?",
        (task_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return row["status"]
