"""Offline regression tests for startup with unavailable Gemini credentials."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from pydantic import BaseModel, SecretStr
from telegram import Update

from gemini_tg_bot.__main__ import _initialize_and_start_polling, _start_application
from gemini_tg_bot.gemini import research as research_module
from gemini_tg_bot.gemini.research import ResearchManager
from gemini_tg_bot.gemini.service import (
    DegradedReason,
    ServiceState,
    ServiceUnavailableError,
)
from gemini_tg_bot.gemini.sessions import ChatSessionRegistry
from gemini_tg_bot.queue import RequestQueue
from gemini_tg_bot.storage.db import Database
from gemini_tg_bot.storage.models import ChatSession as StoredChatSession, ChatSessionDAO
from gemini_tg_bot.telegram.handlers import EgressMeter, TelegramHandlers


DEFERRED_WARNING = (
    "Gemini is unavailable; chat sessions will be restored on first use"
)


class FakeService:
    def __init__(self, *, accepting_requests=False, degrade_after_reads=None):
        self.accepting_requests = accepting_requests
        self.degrade_after_reads = degrade_after_reads
        self.client_reads = 0
        self._client = MagicMock()
        self._client.start_chat.side_effect = lambda **kwargs: SimpleNamespace(**kwargs)
        self.init = AsyncMock()

    @property
    def client(self):
        self.client_reads += 1
        if (
            self.degrade_after_reads is not None
            and self.client_reads > self.degrade_after_reads
        ):
            self.accepting_requests = False
        if not self.accepting_requests:
            raise ServiceUnavailableError(ServiceState.DEGRADED, DegradedReason.AUTH)
        return self._client

    async def execute(self, operation):
        if not self.accepting_requests:
            raise ServiceUnavailableError(ServiceState.DEGRADED, DegradedReason.AUTH)
        return await operation(self.client)


async def seed_sessions(database):
    records = [
        StoredChatSession(
            chat_id=chat_id,
            cid=f"FAKE_CID_{chat_id}",
            model=f"FAKE_MODEL_{chat_id}",
            metadata=[f"FAKE_METADATA_{chat_id}", None],
            language="en",
            updated_at="test",
        )
        for chat_id in (101, 102)
    ]
    dao = ChatSessionDAO(database.connection)
    for record in records:
        await dao.upsert(record)
    return records


async def test_degraded_restore_keeps_recovery_reachable(tmp_path: Path, caplog):
    """Dead cookies must not prevent polling, cleanup, or safe deferred restore."""

    service = FakeService()
    async with Database(tmp_path / "bot.sqlite3") as database:
        await seed_sessions(database)
        registry = ChatSessionRegistry(service, database)
        registry.cleanup_group_threads = AsyncMock(wraps=registry.cleanup_group_threads)

        with caplog.at_level("WARNING"):
            await registry.restore_all()

        assert registry._sessions == {}
        registry.cleanup_group_threads.assert_awaited_once_with()
        assert service.client_reads == 0
        service._client.start_chat.assert_not_called()
        assert caplog.messages == [DEFERRED_WARNING]


@pytest.mark.parametrize("degrade_after_reads", [0, 1])
async def test_restore_race_discards_partial_sessions(
    tmp_path: Path, caplog, degrade_after_reads: int,
):
    """Losing readiness mid-restore must leave no partial map or startup crash."""

    service = FakeService(
        accepting_requests=True, degrade_after_reads=degrade_after_reads,
    )
    async with Database(tmp_path / "bot.sqlite3") as database:
        await seed_sessions(database)
        registry = ChatSessionRegistry(service, database)
        registry._sessions[999] = object()
        with caplog.at_level("WARNING"):
            await registry.restore_all()

        assert registry._sessions == {}
        assert service.client_reads == degrade_after_reads + 1
        assert caplog.messages == [DEFERRED_WARNING]


async def test_healthy_restore_keeps_every_stored_session(tmp_path: Path):
    """Healthy restarts must preserve eager restoration of all stored contexts."""

    service = FakeService(accepting_requests=True)
    async with Database(tmp_path / "bot.sqlite3") as database:
        records = await seed_sessions(database)
        registry = ChatSessionRegistry(service, database)

        await registry.restore_all()

        assert set(registry._sessions) == {record.chat_id for record in records}
        for record in records:
            session = await registry.get_or_create(record.chat_id)
            assert session.cid == record.cid
            assert session.model == record.model
            assert session.metadata == record.metadata
        assert service._client.start_chat.call_args_list == [
            call(metadata=record.metadata, cid=record.cid, model=record.model, gem=None)
            for record in records
        ]


async def test_recovery_lazily_rebuilds_stored_context(tmp_path: Path):
    """Cookie recovery must resume the saved conversation and model on first use."""

    service = FakeService()
    async with Database(tmp_path / "bot.sqlite3") as database:
        records = await seed_sessions(database)
        registry = ChatSessionRegistry(service, database)
        await registry.restore_all()
        service.accepting_requests = True

        session = await registry.get_or_create(records[0].chat_id)

        assert session.cid == records[0].cid
        assert session.model == records[0].model
        assert session.metadata == records[0].metadata
        assert await registry.get_or_create(records[0].chat_id) is session
        service._client.start_chat.assert_called_once_with(
            metadata=records[0].metadata, cid=records[0].cid,
            model=records[0].model, gem=None,
        )


class FakePlan(BaseModel):
    research_id: str
    cid: str


async def test_degraded_startup_reaches_polling_and_republishes_menus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Stored chats and research must allow polling and menus for cookie recovery."""

    monkeypatch.setattr(research_module, "DeepResearchPlan", FakePlan)
    service = FakeService()
    updater = SimpleNamespace(start_polling=AsyncMock())
    application = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=AsyncMock()), start=AsyncMock(),
    )
    async with Database(tmp_path / "bot.sqlite3") as database:
        await seed_sessions(database)
        sessions = ChatSessionRegistry(service, database)
        research = ResearchManager(service, database, timeout_sec=60)
        await research._ensure_schema()
        timestamp = datetime.now(UTC).isoformat()
        await database.connection.execute(
            """INSERT INTO research_tasks
               (task_id, chat_id, prompt, status, created_at, updated_at, plan_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "FAKE_TASK", 101, "FAKE_TOPIC", "running", timestamp, timestamp,
                FakePlan(research_id="FAKE_RESEARCH", cid="FAKE_CID").model_dump_json(),
            ),
        )
        await database.connection.commit()
        research.restore_running = AsyncMock(wraps=research.restore_running)
        handlers = TelegramHandlers(
            service=service, sessions=sessions,
            request_queue=RequestQueue(max_concurrency=1, user_rate_limit_per_min=1),
            usage_dao=None, egress_meter=EgressMeter(),
            cookie_path=tmp_path / "FAKE_COOKIE_CACHE",
            secure_1psid=SecretStr("FAKE_1PSID_FOR_TEST"),
            database=database, research=research,
        )
        try:
            await _initialize_and_start_polling(service, sessions, research, updater)
            await _start_application(application, handlers=handlers)

            service.init.assert_awaited_once_with()
            research.restore_running.assert_awaited_once_with()
            updater.start_polling.assert_awaited_once_with(allowed_updates=Update.ALL_TYPES)
            application.start.assert_awaited_once_with()
            scoped_calls = application.bot.set_my_commands.await_args_list[2:]
            assert [item.kwargs["scope"].chat_id for item in scoped_calls] == [101, 102]
            assert sessions._sessions == {}
            assert service.client_reads == 0
        finally:
            await research.close()
