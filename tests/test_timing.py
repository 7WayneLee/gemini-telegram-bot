"""Deterministic, offline coverage for content-free request diagnostics."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from gemini_webapi.constants import AccountStatus
from pydantic import SecretStr
from telegram.constants import ChatType
from telegram.error import NetworkError, RetryAfter

from gemini_tg_bot.config import Settings
from gemini_tg_bot.gemini import service as service_module
from gemini_tg_bot.gemini.service import GeminiService
from gemini_tg_bot.gemini.thinking import ThinkingLevel
from gemini_tg_bot.queue import QueueAcquireTimeout, RateLimitExceeded
from gemini_tg_bot.telegram import handlers as handlers_module
from gemini_tg_bot.telegram.handlers import EgressMeter, TelegramHandlers
from gemini_tg_bot.telegram.media import MediaHandler
from gemini_tg_bot.telegram.streaming import stream_response
from gemini_tg_bot.telegram.timing import CURRENT_TIMING


PROMPT = "FAKE_PROMPT_DISTINCTIVE_SENTINEL"
ANSWER = "**FAKE_ANSWER_DISTINCTIVE_SENTINEL**"
THOUGHTS = "FAKE_THOUGHTS_DISTINCTIVE_SENTINEL"
CHAT_ID = 827364918273
USER_ID = 918273645819


@dataclass
class FakeClock:
    current: float = 0.0

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


class FakeQueue:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.error: Exception | None = None
        self.waits: list[float] = []

    @asynccontextmanager
    async def request(self, user_id: int):
        if self.error is not None:
            raise self.error
        self.clock.advance(0.5)
        yield self

    async def wait_for_flood_control(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.advance(seconds)
        await asyncio.sleep(0)


class FakeSession:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.hang = False
        self.empty = False
        self.error: Exception | None = None
        self.cancelled = False

    def send_message_stream(self, prompt: str, **kwargs):
        return self.generate()

    async def generate(self):
        if self.hang:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
        if self.error is not None:
            raise self.error
        if not self.empty:
            self.clock.advance(2)
            yield SimpleNamespace(text="", text_delta="", thoughts=THOUGHTS, images=())
            self.clock.advance(3)
            yield SimpleNamespace(text=ANSWER, text_delta=ANSWER, thoughts=THOUGHTS, images=())
        self.clock.advance(1)

    async def send_message(self, prompt: str, **kwargs):
        self.clock.advance(4)
        return SimpleNamespace(text=ANSWER, thoughts=THOUGHTS, images=())


class Harness:
    def __init__(self, tmp_path: Path, caplog, *, timeout: float = 600) -> None:
        self.clock = FakeClock()
        self.queue = FakeQueue(self.clock)
        self.session = FakeSession(self.clock)
        self.usage = []
        self.usage_record_counts = []
        self.edits = []
        self.edit_error: Exception | None = None
        self.state = SimpleNamespace(
            model="FAKE_MODEL", language="en", temporary=False,
            thinking_level=ThinkingLevel.LOW,
        )

        async def get_state(chat_id):
            self.clock.advance(0.5)
            return self.state

        async def get_session(*args):
            self.clock.advance(0.5)
            return self.session

        async def group_session(*args):
            return await get_session(), False

        async def persist(*args):
            self.clock.advance(0.5)

        async def execute(operation):
            return await operation(None)

        async def record_usage(value):
            self.usage_record_counts.append(len(timing_lines(caplog)))
            self.usage.append(value)
            # The timing total must use the same endpoint as usage latency,
            # even though the diagnostic is emitted after the database await.
            self.clock.advance(10)

        async def edit(text, **kwargs):
            self.clock.advance(0.5)
            self.edits.append((text, kwargs))
            if self.edit_error is not None:
                error, self.edit_error = self.edit_error, None
                raise error

        self.placeholder = SimpleNamespace(message_id=300, edit_text=edit, delete=AsyncMock())

        async def reply(text, **kwargs):
            self.clock.advance(0.5)
            return self.placeholder

        self.message = SimpleNamespace(
            text=PROMPT, caption=PROMPT, photo=[], document=None,
            reply_to_message=None, reply_text=reply,
        )
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=USER_ID, language_code="en"),
            effective_chat=SimpleNamespace(id=CHAT_ID, type=ChatType.PRIVATE),
            effective_message=self.message,
        )
        self.context = SimpleNamespace(args=[PROMPT], bot=SimpleNamespace(id=99))
        self.sessions = SimpleNamespace(
            get_state=get_state, get_or_create=get_session, start_new=get_session,
            start_group_session=group_session, persist=persist,
            persist_group_session=persist, group_model_name="FAKE_GROUP_MODEL",
            group_thinking_level=ThinkingLevel.LOW,
        )

        @asynccontextmanager
        async def prepare_upload(message, **kwargs):
            yield SimpleNamespace(prompt=PROMPT, files=["FAKE_UPLOAD"], num_bytes=10)

        self.handlers = TelegramHandlers(
            service=SimpleNamespace(accepting_requests=True, execute=execute, health=SimpleNamespace(accepting_requests=True)),
            sessions=self.sessions, request_queue=self.queue,
            usage_dao=SimpleNamespace(add=record_usage), egress_meter=EgressMeter(),
            cookie_path=tmp_path, secure_1psid=SecretStr("FAKE_1PSID_FOR_TEST"),
            media_handler=SimpleNamespace(prepare_upload=prepare_upload, record_upload=MagicMock()),
            request_timeout_sec=timeout, clock=self.clock,
        )

    def group(self) -> None:
        self.update.effective_chat.type = ChatType.GROUP
        self.message.reply_to_message = SimpleNamespace(message_id=200, from_user=SimpleNamespace(id=99))

    def upload(self) -> None:
        self.message.photo = [SimpleNamespace(file_size=10)]


@pytest.fixture(autouse=True)
def diagnostics_logging(caplog, monkeypatch):
    caplog.set_level("DEBUG")

    async def final_edit_only(*args, **kwargs):
        return await stream_response(*args, edit_interval=1000, edit_character_threshold=10000, **kwargs)

    monkeypatch.setattr(handlers_module, "stream_response", final_edit_only)


def timing_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "gemini_tg_bot.timing"]


def timing_fields(caplog) -> dict[str, str]:
    lines = timing_lines(caplog)
    assert len(lines) == 1
    assert lines[0].startswith("Request timing ")
    fields = dict(pair.split("=", 1) for pair in lines[0].split()[2:])
    assert list(fields) == [
        "chat", "command", "model", "level", "outcome", "slot_wait", "first_output",
        "generation", "upstream", "telegram", "flood_wait", "network_retries", "total",
        "thought_chars", "text_chars",
    ]
    assert next(record for record in caplog.records if record.name == "gemini_tg_bot.timing").levelname == "INFO"
    return fields


async def test_streamed_private_request_reports_phases_after_usage(tmp_path, caplog):
    """Known thought/text arrival times distinguish upstream latency from Telegram and persistence."""
    h = Harness(tmp_path, caplog)
    await h.handlers.user_message(h.update, h.context)

    assert timing_fields(caplog) == {
        "chat": "private", "command": "message", "model": "FAKE_MODEL", "level": "low",
        "outcome": "ok", "slot_wait": "0.5", "first_output": "2.0", "generation": "4.0",
        "upstream": "6.0", "telegram": "1.0", "flood_wait": "0.0", "network_retries": "0",
        "total": "9.0", "thought_chars": str(len(THOUGHTS)), "text_chars": str(len(ANSWER)),
    }
    assert h.usage[0].latency_ms == 9000
    assert h.usage_record_counts == [0]
    assert CURRENT_TIMING.get() is None


async def test_retry_after_is_counted_without_changing_edit_retry(tmp_path, caplog):
    """Silent Telegram flood waits must become visible while retrying the same edit as before."""
    h = Harness(tmp_path, caplog)
    h.edit_error = RetryAfter(3)
    await h.handlers.user_message(h.update, h.context)

    fields = timing_fields(caplog)
    assert fields["flood_wait"] == "3.0"
    assert fields["telegram"] == "4.5"
    assert fields["total"] == "12.5"
    assert fields["upstream"] == "6.0"
    assert h.queue.waits == [3.0]
    assert len(h.edits) == 2 and h.edits[0] == h.edits[1]
    assert fields["outcome"] == "ok"


async def test_request_deadline_logs_timeout(tmp_path, caplog):
    """A total request deadline must record its usage outcome even before upstream emits content."""
    h = Harness(tmp_path, caplog, timeout=0.01)
    h.session.hang = True
    await h.handlers.user_message(h.update, h.context)

    fields = timing_fields(caplog)
    assert fields["outcome"] == "timeout"
    assert fields["slot_wait"] == "0.5"
    assert fields["first_output"] == fields["generation"] == "-"
    assert fields["upstream"] == "0.0"
    assert h.usage[0].error_kind == "timeout"
    assert h.session.cancelled
    assert CURRENT_TIMING.get() is None


@pytest.mark.parametrize("error,outcome", [
    (RateLimitExceeded(USER_ID, 12), "rate_limit"),
    (QueueAcquireTimeout(USER_ID, 0.01), "queue_timeout"),
])
async def test_requests_without_a_slot_leave_unreached_phases_blank(tmp_path, caplog, error, outcome):
    """Rate limiting and queue timeout must not invent slot acquisition or generation durations."""
    h = Harness(tmp_path, caplog)
    h.queue.error = error
    await h.handlers.user_message(h.update, h.context)

    fields = timing_fields(caplog)
    assert fields["outcome"] == outcome
    assert all(fields[key] == "-" for key in ("slot_wait", "first_output", "generation", "upstream"))
    assert fields["telegram"] == "0.5"
    assert fields["thought_chars"] == fields["text_chars"] == "0"


@pytest.mark.parametrize("group,upload", [(True, False), (False, True), (True, True)])
async def test_group_and_upload_requests_report_their_route(tmp_path, caplog, group, upload):
    """Group requests need a chat label and uploads need the duration of their non-streaming call."""
    h = Harness(tmp_path, caplog)
    if group:
        h.group()
    if upload:
        h.upload()
    await h.handlers.user_message(h.update, h.context)

    fields = timing_fields(caplog)
    assert fields["chat"] == ("group" if group else "private")
    assert fields["command"] == ("upload" if upload else "message")
    assert fields["upstream"] == ("4.0" if upload else "6.0")
    assert fields["first_output"] == ("-" if upload else "2.0")
    assert fields["generation"] == ("-" if upload else "4.0")
    assert fields["thought_chars"] == str(len(THOUGHTS))
    assert fields["text_chars"] == str(len(ANSWER))


@pytest.mark.parametrize("command,attachment", [("gemini", False), ("img", False), ("gemini", True), ("img", True)])
async def test_command_timing_keeps_its_command_with_attachments(tmp_path, caplog, command, attachment):
    """Explicit commands use streaming even with files and must retain the command that triggered them."""
    h = Harness(tmp_path, caplog)
    if attachment:
        h.upload()
    await getattr(h.handlers, command)(h.update, h.context)

    fields = timing_fields(caplog)
    assert fields["command"] == command
    assert fields["upstream"] == "6.0"
    assert fields["outcome"] == "ok"


@pytest.mark.parametrize("upload,fails", [(False, False), (True, False), (False, True)])
async def test_diagnostics_never_log_content_or_identity(tmp_path, caplog, upload, fails):
    """DEBUG logs and error handling must not leak distinctive prompts, responses, thoughts, or IDs."""
    h = Harness(tmp_path, caplog)
    if upload:
        h.upload()
    if fails:
        h.session.error = RuntimeError(f"{PROMPT} {ANSWER} {THOUGHTS} {CHAT_ID} {USER_ID}")
    await h.handlers.user_message(h.update, h.context)

    timing_fields(caplog)
    for value in (PROMPT, ANSWER, ANSWER.strip("*"), THOUGHTS, str(CHAT_ID), str(USER_ID), "FAKE_1PSID_FOR_TEST"):
        assert value not in caplog.text


async def test_empty_stream_has_no_first_output_or_generation(tmp_path, caplog):
    """An exhausted stream without text or thoughts has upstream time but no content phases."""
    h = Harness(tmp_path, caplog)
    h.session.empty = True
    h.state.model = None
    await h.handlers.user_message(h.update, h.context)

    fields = timing_fields(caplog)
    assert fields["model"] == "default"
    assert fields["upstream"] == "1.0"
    assert fields["first_output"] == fields["generation"] == "-"
    assert fields["thought_chars"] == fields["text_chars"] == "0"


async def test_network_retry_is_counted_and_keeps_existing_retry_policy(tmp_path, caplog):
    """Only retried Telegram network failures belong in the counter, without changing retry delays."""
    h = Harness(tmp_path, caplog)
    h.edit_error = NetworkError("FAKE_NETWORK_ERROR")
    await h.handlers.user_message(h.update, h.context)

    fields = timing_fields(caplog)
    assert fields["network_retries"] == "1"
    assert fields["flood_wait"] == "0.0"
    assert h.queue.waits == [1.0]
    assert len(h.edits) == 2 and h.edits[0] == h.edits[1]


async def test_concurrent_requests_keep_retry_counts_separate(tmp_path, caplog):
    """Inherited task contexts must isolate diagnostics when independent requests overlap during retries."""
    first, second = Harness(tmp_path, caplog), Harness(tmp_path, caplog)
    first.state.model = "FAKE_MODEL_FIRST"
    second.state.model = "FAKE_MODEL_SECOND"
    first.edit_error = RetryAfter(3)
    await asyncio.gather(
        first.handlers.user_message(first.update, first.context),
        second.handlers.user_message(second.update, second.context),
    )

    lines = timing_lines(caplog)
    assert len(lines) == 2
    by_model = {fields["model"]: fields for fields in (
        dict(pair.split("=", 1) for pair in line.split()[2:]) for line in lines
    )}
    assert by_model["FAKE_MODEL_FIRST"]["flood_wait"] == "3.0"
    assert by_model["FAKE_MODEL_FIRST"]["telegram"] == "4.5"
    assert by_model["FAKE_MODEL_SECOND"]["flood_wait"] == "0.0"
    assert by_model["FAKE_MODEL_SECOND"]["telegram"] == "1.0"
    assert CURRENT_TIMING.get() is None


async def test_upload_counts_get_file_without_adding_retries(tmp_path, caplog):
    """Upload metadata retrieval is a Telegram API await outside the common sending helper."""
    h = Harness(tmp_path, caplog)

    async def download_to_drive(*, custom_path):
        custom_path.write_bytes(b"FAKE_FILE")
        return custom_path

    async def get_file():
        h.clock.advance(2)
        return SimpleNamespace(download_to_drive=download_to_drive)

    h.message.photo = [SimpleNamespace(file_size=9, file_unique_id="FAKE_FILE_ID", get_file=get_file)]
    h.handlers._media = MediaHandler(egress_meter=EgressMeter(), temp_root=tmp_path)
    await h.handlers.user_message(h.update, h.context)

    fields = timing_fields(caplog)
    assert fields["command"] == "upload"
    assert fields["upstream"] == "4.0"
    assert fields["telegram"] == "2.5"
    assert fields["outcome"] == "ok"


def fake_client(models, status=AccountStatus.AVAILABLE):
    return SimpleNamespace(
        init=AsyncMock(), close=AsyncMock(), account_status=status,
        list_models=MagicMock(return_value=models),
    )


def fake_settings(tmp_path):
    return Settings(
        _env_file=None, TELEGRAM_BOT_TOKEN="FAKE_TELEGRAM_BOT_TOKEN_FOR_TEST", ADMIN_USER_ID=123456789,
        GEMINI_SECURE_1PSID="FAKE_1PSID_FOR_TEST", GEMINI_SECURE_1PSIDTS="FAKE_1PSIDTS_FOR_TEST",
        GEMINI_COOKIE_PATH=tmp_path,
    )


async def test_registry_logs_models_after_init_and_reinit(tmp_path, caplog, monkeypatch):
    """Every successful service initialization must expose the dynamic model numbers and capacity."""
    models = [
        SimpleNamespace(model_name="gemini-flash", display_name="3.8 Flash", model_number=1, capacity=2, capacity_field=12),
        SimpleNamespace(model_name="gemini-argon", display_name="4 Argon", model_number=8, capacity=1, capacity_field=12),
    ]
    first, second = fake_client(models), fake_client(models)
    monkeypatch.setattr(service_module, "GeminiClient", MagicMock(side_effect=[first, second]))
    service = GeminiService(fake_settings(tmp_path))
    try:
        await service.init()
        await service.reinit()
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Model registry:")]
        expected = "Model registry: gemini-flash(3.8 Flash, number=1, capacity=2/12), gemini-argon(4 Argon, number=8, capacity=1/12)"
        assert lines == [expected, expected]
        first.list_models.assert_called_once_with()
        second.list_models.assert_called_once_with()
        assert all(r.levelname == "INFO" for r in caplog.records if r.getMessage().startswith("Model registry:"))
        assert "FAKE_1PSID" not in caplog.text
    finally:
        await service.close()


async def test_registry_none_is_unavailable(tmp_path, caplog, monkeypatch):
    """An unavailable upstream registry must be reported without fabricating model details."""
    client = fake_client(None)
    monkeypatch.setattr(service_module, "GeminiClient", MagicMock(return_value=client))
    service = GeminiService(fake_settings(tmp_path))
    try:
        await service.init()
        assert "Model registry: unavailable" in caplog.messages
    finally:
        await service.close()


async def test_half_open_reinit_logs_recovered_registry(tmp_path, caplog, monkeypatch):
    """Temporary-account recovery also initializes at the service layer and needs a registry record."""
    first = fake_client(None, AccountStatus.ACCESS_TEMPORARILY_UNAVAILABLE)
    second = fake_client(None)
    monkeypatch.setattr(service_module, "GeminiClient", MagicMock(side_effect=[first, second]))
    clock = FakeClock()
    service = GeminiService(fake_settings(tmp_path), time_source=clock, blocked_cooldown_sec=1)
    try:
        await service.init()
        clock.advance(1)
        await service.execute(AsyncMock())
        assert [message for message in caplog.messages if message.startswith("Model registry:")] == ["Model registry: unavailable"]
        second.list_models.assert_called_once_with()
    finally:
        await service.close()
