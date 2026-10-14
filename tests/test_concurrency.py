"""Offline integration coverage for chat ordering and bounded generation."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from gemini_webapi import AvailableModel
from gemini_webapi.constants import AccountStatus
from pydantic import SecretStr, ValidationError
from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User
from telegram.constants import ChatType
from telegram.ext import Application, ExtBot

from gemini_tg_bot.config import Settings
from gemini_tg_bot.gemini import service as service_module
from gemini_tg_bot.gemini.service import GeminiService, ServiceState
from gemini_tg_bot.gemini.sessions import GroupModelSettings
from gemini_tg_bot.gemini.thinking import (
    HIGH_WIRE_VALUE,
    MEDIUM_WIRE_VALUE,
    ThinkingLevel,
    ThinkingLevelGeminiClient,
)
from gemini_tg_bot.i18n import translate
from gemini_tg_bot.queue import RequestQueue
from gemini_tg_bot.telegram.auth import AuthMiddleware, UNAUTHORIZED_MESSAGE
from gemini_tg_bot.telegram.handlers import EgressMeter, TelegramHandlers, register_handlers
from gemini_tg_bot.telegram.update_processor import (
    LOCK_FREE_COMMANDS,
    PerChatUpdateProcessor,
)
from tests.test_thinking import FakeSession, MODEL_HEADER, _capture


class FakeBot(ExtBot):
    """Exercise PTB's real handler groups without contacting Telegram."""

    def __init__(self) -> None:
        super().__init__("123:FAKE_TELEGRAM_BOT_TOKEN_FOR_TEST")
        object.__setattr__(self, "sent", [])
        object.__setattr__(self, "edited", [])
        object.__setattr__(self, "answered", [])

    async def initialize(self) -> None:
        self._bot_user = User(999, "Bot", True, username="fake_bot")

    async def shutdown(self) -> None:
        pass

    def message(self, chat_id: int, text: str, *, message_id: int = 1) -> Message:
        message = Message(
            message_id, datetime.now(UTC),
            Chat(chat_id, ChatType.PRIVATE if chat_id > 0 else ChatType.GROUP),
            from_user=self._bot_user, text=text,
        )
        message.set_bot(self)
        return message

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Message:
        self.sent.append((chat_id, text))
        return self.message(chat_id, text, message_id=1000 + len(self.sent))

    async def edit_message_text(
        self, text: str, chat_id: int, message_id: int, **kwargs: Any
    ) -> Message:
        self.edited.append((chat_id, text))
        return self.message(chat_id, text, message_id=message_id)

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> bool:
        self.answered.append(callback_query_id)
        return True

    async def delete_message(self, **kwargs: Any) -> bool:
        return True


class GenerationGate:
    """Hold fake requests independently so overlap and cancellation are observable."""

    def __init__(self) -> None:
        self.releases: dict[int, asyncio.Event] = defaultdict(asyncio.Event)
        self.started: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        self.active: set[int] = set()
        self.finished: list[tuple[int, str]] = []
        self.cancelled: list[int] = []

    async def generate(self, chat_id: int, prompt: str) -> Any:
        assert chat_id not in self.active, "Same-chat generations overlapped"
        self.active.add(chat_id)
        self.started.put_nowait((chat_id, prompt))
        try:
            await self.releases[chat_id].wait()
            self.finished.append((chat_id, prompt))
            return SimpleNamespace(text="offline answer", text_delta="offline answer", images=())
        except asyncio.CancelledError:
            self.cancelled.append(chat_id)
            raise
        finally:
            self.active.remove(chat_id)

    async def next_start(self) -> tuple[int, str]:
        return await asyncio.wait_for(self.started.get(), 1)


class FakeChatSession:
    def __init__(self, chat_id: int, gate: GenerationGate) -> None:
        self.chat_id = chat_id
        self.gate = gate

    async def send_message_stream(self, prompt: str, **kwargs: Any) -> Any:
        yield await self.gate.generate(self.chat_id, prompt)

    async def send_message(self, prompt: str, **kwargs: Any) -> Any:
        return await self.gate.generate(self.chat_id, prompt)


class Harness:
    def __init__(
        self, tmp_path: Path, *, concurrency: int = 3, timeout: float = 600,
        slot_wait: float = 0.05, service: Any = None,
    ) -> None:
        self.bot = FakeBot()
        self.processor = PerChatUpdateProcessor()
        self.gate = GenerationGate()
        self.sessions = AsyncMock()
        self.levels: dict[int, ThinkingLevel] = {}
        self.languages: dict[int, str] = {}
        self.sessions.group_settings = GroupModelSettings("fake-model", ThinkingLevel.MEDIUM)
        self.chat_sessions: dict[int, Any] = {}

        async def state(chat_id: int) -> Any:
            return SimpleNamespace(
                model="fake-model", gem_id=None, cid="fake-cid", temporary=False,
                thinking_level=self.levels.get(chat_id, ThinkingLevel.MEDIUM),
                language=self.languages.get(chat_id),
            )

        async def session(chat_id: int) -> Any:
            return self.chat_sessions.setdefault(chat_id, FakeChatSession(chat_id, self.gate))

        async def group_session(chat_id: int, parent: Any = None, *, settings: Any = None) -> Any:
            return await session(chat_id), False

        self.sessions.get_state.side_effect = state
        self.sessions.get_or_create.side_effect = session
        self.sessions.start_new.side_effect = session
        self.sessions.start_group_session.side_effect = group_session
        self.usage = AsyncMock()
        self.usage.list_for_chat.return_value = []
        self.client = MagicMock()
        self.client.list_models.return_value = [SimpleNamespace(
            is_available=True, model_name="fake-model", display_name="Fake model",
        )]
        self.client.resolve_model.return_value = self.client.list_models.return_value[0]

        async def execute(operation: Any) -> Any:
            result = operation(self.client)
            return await result if inspect.isawaitable(result) else result

        self.service = service or SimpleNamespace(
            execute=AsyncMock(side_effect=execute),
            health=SimpleNamespace(
                state=ServiceState.HEALTHY, accepting_requests=True,
                degraded_reason=None, account_status=AccountStatus.AVAILABLE,
            ),
        )
        self.queue = RequestQueue(concurrency, 100, acquire_timeout=slot_wait)
        self.handlers = TelegramHandlers(
            service=self.service, sessions=self.sessions, request_queue=self.queue,
            usage_dao=self.usage, egress_meter=EgressMeter(), cookie_path=tmp_path,
            secure_1psid=SecretStr("FAKE_1PSID_FOR_TEST"), request_timeout_sec=timeout,
        )
        self.application = (
            Application.builder().bot(self.bot).concurrent_updates(self.processor).build()
        )
        access = AsyncMock()
        access.get.return_value = None
        access.get_chat.return_value = None
        register_handlers(self.application, auth=AuthMiddleware(
            admin_user_id=1, allowed_user_ids={1, 10, 20, 30, 40},
            allowed_chat_ids={-100},
            access_overrides=access,
        ), handlers=self.handlers)
        self.errors: list[Exception] = []

        async def error_handler(update: Any, context: Any) -> None:
            self.errors.append(context.error)

        self.application.add_error_handler(error_handler)
        self.tasks: list[asyncio.Task[Any]] = []
        self.sequence = 0

    def update(
        self, chat_id: int, text: str = "hello", *, user_id: int = 10,
        callback: str | None = None, photo: bool = False,
    ) -> Update:
        self.sequence += 1
        user = User(user_id, "Test", False)
        command = text.split(maxsplit=1)[0] if text.startswith("/") else None
        message = Message(
            self.sequence, datetime.now(UTC),
            Chat(chat_id, ChatType.PRIVATE if chat_id > 0 else ChatType.GROUP),
            from_user=user, text=None if photo else text,
            entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, len(command))] if command else [],
        )
        message.set_bot(self.bot)
        if photo:
            # Upload preparation is replaced by a fake in the media timeout test.
            from telegram import PhotoSize
            message = Message(
                self.sequence, datetime.now(UTC), message.chat, from_user=user,
                photo=[PhotoSize("fake-file", "fake-unique", 1, 1)],
            )
            message.set_bot(self.bot)
        if callback is not None:
            query = CallbackQuery(str(self.sequence), user, "fake-instance", message=message, data=callback)
            query.set_bot(self.bot)
            update = Update(self.sequence, callback_query=query)
        else:
            update = Update(self.sequence, message=message)
        update.set_bot(self.bot)
        return update

    def submit(self, update: Update) -> asyncio.Task[Any]:
        task = asyncio.create_task(self.processor.process_update(
            update, self.application.process_update(update),
        ))
        self.tasks.append(task)
        return task

    async def finish(self, *tasks: asyncio.Task[Any]) -> None:
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert self.errors == []


@pytest.fixture
async def harness_factory(tmp_path: Path):
    harnesses: list[Harness] = []

    async def make(**kwargs: Any) -> Harness:
        harness = Harness(tmp_path, **kwargs)
        harnesses.append(harness)
        await harness.application.initialize()
        return harness

    yield make
    for harness in harnesses:
        for task in harness.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*harness.tasks, return_exceptions=True)
        await harness.application.shutdown()
        assert harness.errors == []
        assert harness.processor._chat_locks == {}


async def test_three_chats_generate_and_fourth_times_out_in_queue(harness_factory) -> None:
    """MAX_CONCURRENCY must allow overlap while preserving the existing busy reply."""
    h = await harness_factory(concurrency=3)
    first = [h.submit(h.update(chat)) for chat in (10, 20, 30)]
    assert { (await h.gate.next_start())[0] for _ in range(3) } == {10, 20, 30}
    fourth = h.submit(h.update(40))
    await h.finish(fourth)
    assert h.gate.active == {10, 20, 30}
    assert (40, translate("generic.service_busy", "en")) in h.bot.sent
    assert h.gate.started.empty()
    assert h.queue.queue_depth == 0
    for chat in (10, 20, 30):
        h.gate.releases[chat].set()
    await h.finish(*first)


async def test_fourth_chat_runs_when_a_slot_is_released(harness_factory) -> None:
    """A waiting chat must begin as soon as another request releases its permit."""
    h = await harness_factory(slot_wait=1)
    first = [h.submit(h.update(chat)) for chat in (10, 20, 30)]
    for _ in range(3):
        await h.gate.next_start()
    fourth = h.submit(h.update(40))
    await asyncio.sleep(0)
    assert h.gate.started.empty()
    h.gate.releases[10].set()
    assert (await h.gate.next_start())[0] == 40
    for chat in (20, 30, 40):
        h.gate.releases[chat].set()
    await h.finish(*first, fourth)


async def test_same_chat_messages_finish_in_arrival_order(harness_factory) -> None:
    """Concurrent PTB dispatch must never overlap one chat's session mutations."""
    h = await harness_factory()
    first = h.submit(h.update(10, "first"))
    assert await h.gate.next_start() == (10, "first")
    second = h.submit(h.update(10, "second"))
    await asyncio.sleep(0)
    assert not second.done()
    assert h.gate.started.empty()
    h.gate.releases[10].set()
    await h.finish(first, second)
    assert h.gate.finished == [(10, "first"), (10, "second")]


async def test_status_and_help_answer_during_generations(harness_factory) -> None:
    """Operational commands must remain responsive during same-chat and other-chat work."""
    h = await harness_factory()
    generations = [h.submit(h.update(chat)) for chat in (10, 20)]
    for _ in generations:
        await h.gate.next_start()
    commands = [h.submit(h.update(10, text)) for text in ("/status@fake_bot", "/help")]
    await h.finish(*commands)
    assert h.gate.active == {10, 20}
    assert any("Queue" in text for chat, text in h.bot.sent if chat == 10)
    assert any("/gemini" in text for chat, text in h.bot.sent if chat == 10)
    for chat in (10, 20):
        h.gate.releases[chat].set()
    await h.finish(*generations)


async def test_new_and_model_level_button_wait_for_generation(harness_factory) -> None:
    """Session reset and model selection must not race an active conversation turn."""
    h = await harness_factory()
    generation = h.submit(h.update(10))
    await h.gate.next_start()
    reset = h.submit(h.update(10, "/new"))
    button_update = h.update(10, callback="mlvl:high:fake-model")
    button = h.submit(button_update)
    await asyncio.sleep(0)
    h.sessions.reset.assert_not_awaited()
    h.sessions.set_model_and_thinking_level.assert_not_awaited()
    assert h.bot.answered == []
    h.gate.releases[10].set()
    await h.finish(generation, reset, button)
    h.sessions.reset.assert_awaited_once_with(10)
    h.sessions.set_model_and_thinking_level.assert_awaited_once_with(10, "fake-model", ThinkingLevel.HIGH)


async def test_authorization_runs_first_under_concurrent_dispatch(harness_factory) -> None:
    """Enabling concurrency must not bypass the group -1 authorization gate."""
    h = await harness_factory()
    allowed = h.submit(h.update(10))
    await h.gate.next_start()
    rejected = h.submit(h.update(20, user_id=777))
    await h.finish(rejected)
    assert (20, UNAUTHORIZED_MESSAGE) in h.bot.sent
    assert h.gate.active == {10}
    assert h.gate.started.empty()
    h.gate.releases[10].set()
    await h.finish(allowed)


@pytest.mark.parametrize("language", ["en", "zh-hant"])
@pytest.mark.parametrize("chat,text", [(10, "hello"), (10, "/gemini hello"), (10, "/img hello"), (-100, "hello"), (-100, "/gemini hello"), (-100, "/img hello")])
async def test_generation_timeout_cancels_records_usage_and_releases_slot(
    harness_factory, caplog, language, chat, text,
) -> None:
    """Every text generation entry point needs a total deadline and a reusable slot."""
    h = await harness_factory(concurrency=1, timeout=0.02)
    h.languages[chat] = language
    with caplog.at_level(logging.WARNING):
        update = h.update(chat, text)
        if chat < 0 and not text.startswith("/"):
            update.message._unfreeze()
            update.message.reply_to_message = h.bot.message(chat, "offline previous answer")
            update.message._freeze()
        request = h.submit(update)
        await h.finish(request)
    expected_language = "en" if chat < 0 else language
    assert (chat, translate("gemini.request_timeout", expected_language, minutes=1)) in h.bot.sent
    assert h.gate.cancelled == [chat]
    usage = h.usage.add.await_args.args[0]
    assert usage.ok == 0 and usage.error_kind == "timeout"
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "hello" not in warnings[0].getMessage()
    assert "offline answer" not in warnings[0].getMessage()
    h.gate.releases[20].set()
    await h.finish(h.submit(h.update(20, "next")))
    assert h.gate.finished == [(20, "next")]


async def test_media_generation_has_total_timeout(harness_factory) -> None:
    """user_message uploads must receive the same cancellation and usage accounting."""
    h = await harness_factory(timeout=0.02)

    @asynccontextmanager
    async def prepare(*args: Any, **kwargs: Any):
        yield SimpleNamespace(prompt="offline upload", files=["fake-file"])

    h.handlers._media = SimpleNamespace(prepare_upload=prepare, record_upload=MagicMock())
    await h.finish(h.submit(h.update(10, photo=True)))
    assert h.gate.cancelled == [10]
    usage = h.usage.add.await_args.args[0]
    assert usage.ok == 0 and usage.error_kind == "timeout"
    assert (10, translate("gemini.request_timeout", "en", minutes=1)) in h.bot.sent


async def test_queue_wait_is_excluded_from_generation_deadline(harness_factory) -> None:
    """A request waiting for a permit must receive its full generation time budget."""
    h = await harness_factory(concurrency=1, timeout=0.03, slot_wait=1)
    async with h.queue.request(30):
        request = h.submit(h.update(10))
        await asyncio.sleep(0.05)
        assert not request.done()
        assert h.gate.started.empty()
    await h.gate.next_start()
    h.gate.releases[10].set()
    await h.finish(request)
    assert h.usage.add.await_args.args[0].ok == 1


async def test_timeout_minutes_round_up(harness_factory) -> None:
    """Non-whole-minute limits must describe the configured deadline rounded up."""
    h = await harness_factory(timeout=61)
    await h.handlers._reply_request_timeout(h.bot.message(10, ""), "en")
    assert (10, translate("gemini.request_timeout", "en", minutes=2)) in h.bot.sent


async def test_medium_and_high_payloads_are_isolated_between_chats(harness_factory) -> None:
    """Real concurrent handler tasks must keep the Medium ContextVar override local."""
    h = await harness_factory()
    client = ThinkingLevelGeminiClient(
        secure_1psid="FAKE_1PSID_FOR_TEST", secure_1psidts="FAKE_1PSIDTS_FOR_TEST",
    )
    client._running = True
    client.client = FakeSession()
    model = AvailableModel(
        model_id="FAKE_MODEL_ID_FOR_TEST", model_name="fake-model",
        display_name="Fake model", description="Offline fixture", capacity=1,
        model_number=7,
    )
    ready: dict[int, asyncio.Event] = {10: asyncio.Event(), 20: asyncio.Event()}
    captured: dict[int, Any] = {}

    class CapturingSession:
        def __init__(self, chat_id: int) -> None:
            self.chat_id = chat_id

        async def send_message_stream(self, prompt: str, **kwargs: Any):
            ready[self.chat_id].set()
            await ready[20 if self.chat_id == 10 else 10].wait()
            captured[self.chat_id] = await _capture(client, model, kwargs)
            yield SimpleNamespace(text="offline answer", text_delta="offline answer", images=())

    h.levels.update({10: ThinkingLevel.MEDIUM, 20: ThinkingLevel.HIGH})
    h.chat_sessions.update({chat: CapturingSession(chat) for chat in (10, 20)})
    await h.finish(*(h.submit(h.update(chat)) for chat in (10, 20)))
    for chat, expected in ((10, MEDIUM_WIRE_VALUE), (20, HIGH_WIRE_VALUE)):
        inner = json.loads(json.loads(captured[chat].data["f.req"])[1])
        assert inner[80] == expected
        assert json.loads(captured[chat].headers[MODEL_HEADER])[-2] == expected
    medium = json.loads(json.loads(captured[10].data["f.req"])[1])
    high = json.loads(json.loads(captured[20].data["f.req"])[1])
    for index, (medium_value, high_value) in enumerate(zip(medium, high, strict=True)):
        if index not in (59, 80):  # Per-request random token and effort.
            assert medium_value == high_value
    assert client._live_client is client.client


async def test_setcookie_reload_waits_for_all_active_requests(harness_factory, monkeypatch, tmp_path) -> None:
    """Credential reload must drain concurrent requests before closing the shared client."""
    settings = Settings(
        _env_file=None, TELEGRAM_BOT_TOKEN="FAKE_TELEGRAM_BOT_TOKEN_FOR_TEST",
        ADMIN_USER_ID=1, GEMINI_SECURE_1PSID="FAKE_1PSID_FOR_TEST",
        GEMINI_SECURE_1PSIDTS="FAKE_1PSIDTS_FOR_TEST", GEMINI_COOKIE_PATH=tmp_path,
    )
    clients = [MagicMock(account_status=AccountStatus.AVAILABLE) for _ in range(2)]
    for client in clients:
        client.init = AsyncMock()
        client.close = AsyncMock()
    factory = MagicMock(side_effect=clients)
    monkeypatch.setattr(service_module, "GeminiClient", factory)
    monkeypatch.setattr(service_module, "clear_cookies_cache", MagicMock())
    service = GeminiService(settings)
    try:
        await service.init()
        h = await harness_factory(service=service)
        work = [h.submit(h.update(chat)) for chat in (10, 20)]
        for _ in work:
            await h.gate.next_start()
        await h.finish(h.submit(h.update(1, "/setcookie", user_id=1)))
        reload = h.submit(h.update(1, "FAKE_1PSID_FOR_TEST FAKE_1PSIDTS_FOR_TEST", user_id=1))
        async with asyncio.timeout(1):
            while service.health.state is not ServiceState.REINITIALIZING:
                await asyncio.sleep(0)
        assert service.health.active_requests == 2
        clients[0].close.assert_not_awaited()
        h.gate.releases[10].set()
        await h.finish(work[0])
        assert not reload.done()
        clients[0].close.assert_not_awaited()
        h.gate.releases[20].set()
        await h.finish(work[1], reload)
        assert service.health.active_requests == 0
        assert service.health.state is ServiceState.HEALTHY
        clients[0].close.assert_awaited_once()
        clients[1].init.assert_awaited_once()
        assert factory.call_count == 2
        assert all(call.args[0].ok == 1 for call in h.usage.add.await_args_list)
        assert (1, translate("admin.cookie_updated", "en")) in h.bot.sent
    finally:
        if 'h' in locals():
            for event in h.gate.releases.values():
                event.set()
            await asyncio.gather(*h.tasks, return_exceptions=True)
        await service.close()


def test_removed_research_status_is_not_lock_free() -> None:
    """Removing the command must also remove its special ordering exemption."""

    assert "research_status" not in LOCK_FREE_COMMANDS


@pytest.mark.parametrize("command", sorted(LOCK_FREE_COMMANDS))
async def test_lock_free_command_names_and_mentions(command) -> None:
    """Every specified read/admin command must bypass a held chat lock with bot suffixes."""
    processor = PerChatUpdateProcessor()
    entered, release = asyncio.Event(), asyncio.Event()

    async def generation():
        entered.set()
        await release.wait()

    update = SimpleNamespace(effective_chat=SimpleNamespace(id=10), effective_message=SimpleNamespace(text="hello"), callback_query=None)
    work = asyncio.create_task(processor.process_update(update, generation()))
    await entered.wait()
    try:
        for text in (f"/{command}", f"/{command}@fake_bot argument"):
            update.effective_message = SimpleNamespace(text=text)
            reply = AsyncMock()
            await asyncio.wait_for(processor.process_update(update, reply()), 0.1)
            reply.assert_awaited_once()
    finally:
        release.set()
        await work
    assert processor._chat_locks == {}


async def test_lock_registry_cleans_up_failures_and_cancelled_waiters() -> None:
    """Lock cleanup must also cover handler failure and cancellation before acquisition."""
    processor = PerChatUpdateProcessor()
    entered, release = asyncio.Event(), asyncio.Event()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=10), effective_message=None, callback_query=None)

    async def fail():
        entered.set()
        await release.wait()
        raise ValueError("offline failure")

    first = asyncio.create_task(processor.process_update(update, fail()))
    await entered.wait()
    pending = asyncio.create_task(processor.process_update(update, AsyncMock()()))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    release.set()
    with pytest.raises(ValueError):
        await first
    assert processor._chat_locks == {}


async def test_updates_without_chat_are_not_locked() -> None:
    """Chatless updates must still run without allocating a registry entry."""
    processor = PerChatUpdateProcessor()
    operation = AsyncMock()
    await processor.process_update(SimpleNamespace(effective_chat=None), operation())
    operation.assert_awaited_once()
    assert processor._chat_locks == {}
    assert processor.max_concurrent_updates == 64


def test_request_timeout_environment_validation_and_defaults(monkeypatch, tmp_path) -> None:
    """The new timeout must be positive and preserve MAX_CONCURRENCY's safe default."""
    monkeypatch.delenv("REQUEST_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("MAX_CONCURRENCY", raising=False)
    required = dict(
        _env_file=None, TELEGRAM_BOT_TOKEN="FAKE_TELEGRAM_BOT_TOKEN_FOR_TEST",
        ADMIN_USER_ID=1, GEMINI_SECURE_1PSID="FAKE_1PSID_FOR_TEST",
        GEMINI_SECURE_1PSIDTS="FAKE_1PSIDTS_FOR_TEST", GEMINI_COOKIE_PATH=tmp_path,
    )
    settings = Settings(**required)
    assert settings.request_timeout_sec == 600
    assert settings.max_concurrency == 1
    monkeypatch.setenv("REQUEST_TIMEOUT_SEC", "17")
    assert Settings(**required).request_timeout_sec == 17
    monkeypatch.setenv("REQUEST_TIMEOUT_SEC", "0")
    with pytest.raises(ValidationError):
        Settings(**required)
