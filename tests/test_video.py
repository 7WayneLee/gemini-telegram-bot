"""Offline video request, lifecycle, delivery, and privacy regressions."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from gemini_webapi import exceptions as gw_exc
from gemini_webapi.constants import Endpoint
from pydantic import SecretStr, ValidationError
from telegram import BotCommandScopeAllGroupChats, BotCommandScopeChat
from telegram.constants import ChatType
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop

from gemini_tg_bot.__main__ import _start_application
from gemini_tg_bot.config import Settings
from gemini_tg_bot.gemini import thinking
from gemini_tg_bot.gemini.thinking import (
    ThinkingLevel,
    ThinkingLevelGeminiClient,
    VideoRequestState,
    rewrite_video_request,
    thinking_request,
    video_request,
)
from gemini_tg_bot.i18n import LANGUAGE_CHINESE, LANGUAGE_ENGLISH, translate
from gemini_tg_bot.queue import RequestQueue
from gemini_tg_bot.storage.db import Database
from gemini_tg_bot.storage.models import UsageLogDAO
from gemini_tg_bot.telegram.handlers import (
    GROUP_COMMANDS,
    EgressMeter,
    TelegramHandlers,
    _help_text,
    register_command_menu,
)
from gemini_tg_bot.telegram.media import MAX_VIDEO_BYTES, MediaHandler
from gemini_tg_bot.telegram.sending import send_video


PROMPT = "FAKE_DISTINCTIVE_VIDEO_PROMPT"
REPLY = "FAKE_DISTINCTIVE_INITIAL_REPLY"
URL = "https://example.invalid/FAKE_DISTINCTIVE_VIDEO_URL"
CID = "FAKE_DISTINCTIVE_CONVERSATION_ID"
USER_ID = 834726195
CHAT_ID = 927361845


def sample_request() -> dict[str, str]:
    inner = [None] * 81
    inner[0] = [PROMPT, 0, None, None, None, None, 0]
    inner[1] = ["en-GB"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    for index, value in {
        6: [1], 7: 1, 10: 1, 11: 0, 17: [[0]], 18: 0, 27: 1,
        30: [4], 41: [1], 53: 0, 59: "FAKE_REQUEST_UUID", 61: [],
        68: 1, 79: 1, 80: 1,
    }.items():
        inner[index] = value
    return {"f.req": json.dumps([None, json.dumps(inner)]), "at": "FAKE_AT_TOKEN"}


def decode(data):
    return json.loads(json.loads(data["f.req"])[1])


class ManualClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = asyncio.Queue()
        self.timeouts = []

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        await self.sleeps.put((delay, future))
        await future

    @asynccontextmanager
    async def timeout(self, delay):
        # Use asyncio's cancellation/TimeoutError handling with a manual deadline.
        async with asyncio.timeout_at(None) as timer:
            entry = (self.now + delay, timer)
            self.timeouts.append(entry)
            try:
                yield timer
            finally:
                self.timeouts.remove(entry)

    def advance(self, delay):
        self.now += delay
        for deadline, timer in self.timeouts:
            if self.now >= deadline and not timer.expired():
                timer.reschedule(asyncio.get_running_loop().time() - 1)

    async def tick(self):
        delay, future = await asyncio.wait_for(self.sleeps.get(), 1)
        self.advance(delay)
        future.set_result(None)
        return delay


class FakeVideo:
    def __init__(self, size=12, *, with_thumbnail=False):
        self.url = URL
        self.size = size
        self.paths = []
        self.thumbnail = "https://example.invalid/FAKE_THUMBNAIL" if with_thumbnail else ""

    async def save(self, *, path):
        thumbnail = None
        if self.thumbnail:
            thumbnail_path = Path(path) / "video.jpg"
            thumbnail_path.write_bytes(b"FAKE_THUMBNAIL_BYTES")
            self.paths.append(thumbnail_path)
            thumbnail = str(thumbnail_path)
        destination = Path(path) / "video.mp4"
        self.paths.append(destination)
        with destination.open("wb") as file:
            file.truncate(self.size)
        return {"video": str(destination), "video_thumbnail": thumbnail}


class Harness:
    def __init__(self, tmp_path, database, *, enabled=True, timeout=300, delivery_timeout=600, malformed=False):
        self.clock = ManualClock()
        self.session = SimpleNamespace(cid=CID, send_message=AsyncMock(side_effect=self.initial_send))
        self.client = SimpleNamespace(
            start_chat=MagicMock(return_value=self.session),
            fetch_latest_chat_response=AsyncMock(),
        )
        self.service = SimpleNamespace(
            execute=AsyncMock(side_effect=self.execute), health=SimpleNamespace(accepting_requests=True),
        )
        self.sessions = AsyncMock()
        self.sessions.get_state.return_value = SimpleNamespace(language=LANGUAGE_ENGLISH)
        self.queue = RequestQueue(max_concurrency=1, user_rate_limit_per_min=20)
        self.egress = EgressMeter()
        self.media = MediaHandler(egress_meter=self.egress, temp_root=tmp_path)
        self.usage = UsageLogDAO(database.connection)
        self.handlers = TelegramHandlers(
            service=self.service, sessions=self.sessions, request_queue=self.queue,
            usage_dao=self.usage, egress_meter=self.egress, cookie_path=tmp_path,
            secure_1psid=SecretStr("FAKE_1PSID_FOR_TEST"), media_handler=self.media,
            clock=self.clock, enable_video_generation=enabled, video_timeout_sec=timeout,
            request_timeout_sec=delivery_timeout,
        )
        self.handlers._video_jobs._sleep = self.clock.sleep
        self.malformed = malformed
        self.status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        self.relay_payloads = []

        async def reply_video(video, **kwargs):
            if isinstance(video, str):
                raise BadRequest("FAKE_REJECTED_URL " + URL)
            self.relay_payloads.append((video.read(), video.name, kwargs))

        self.message = SimpleNamespace(
            text="/video " + PROMPT, reply_to_message=None,
            reply_text=AsyncMock(return_value=self.status), reply_video=AsyncMock(side_effect=reply_video),
        )
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=USER_ID, language_code="en"),
            effective_chat=SimpleNamespace(id=CHAT_ID, type=ChatType.PRIVATE),
            effective_message=self.message,
        )
        self.context = SimpleNamespace(args=[PROMPT])

    async def execute(self, operation):
        return await operation(self.client)

    async def initial_send(self, prompt, **kwargs):
        assert kwargs == {"extended_thinking": False}
        assert thinking._thinking_wire_value.get() is None
        state = thinking._video_request.get()
        assert state is not None
        rewrite_video_request({"f.req": "bad"} if self.malformed else sample_request(), state)
        self.clock.now += 2
        return SimpleNamespace(text=REPLY, videos=[])

    async def start(self):
        await self.handlers.video(self.update, self.context)
        job = self.handlers._video_jobs._jobs.get(CHAT_ID)
        return job.task if job else None

    async def rows(self):
        return await self.usage.list_for_chat(CHAT_ID)


@pytest.fixture
async def harness(tmp_path):
    database = Database(":memory:")
    await database.connect()
    instances = []

    def make(**kwargs):
        instance = Harness(tmp_path, database, **kwargs)
        instances.append(instance)
        return instance

    yield make
    for instance in instances:
        await instance.handlers.close()
    await database.close()


async def test_disabled_has_no_generation_or_listings(harness):
    """The default-off switch must reject typed commands and hide every listing."""

    h = harness(enabled=False)
    await h.start()
    h.message.reply_text.assert_awaited_once_with(translate("video.disabled", "en"))
    h.service.execute.assert_not_awaited()
    h.client.start_chat.assert_not_called()
    bot = SimpleNamespace(delete_my_commands=AsyncMock(), set_my_commands=AsyncMock())
    await register_command_menu(SimpleNamespace(bot=bot))
    for language in (LANGUAGE_ENGLISH, LANGUAGE_CHINESE):
        await h.handlers._set_chat_command_menu(bot, CHAT_ID, language)
        assert "/video" not in _help_text(language)
        assert "/video" not in _help_text(language, group_only=True)
    assert all("video" not in {item.command for item in entry.args[0]} for entry in bot.set_my_commands.await_args_list)
    rows = await h.rows()
    assert len(rows) == 1 and rows[0].command == "video" and rows[0].error_kind == "disabled"


@pytest.mark.parametrize("language", [LANGUAGE_ENGLISH, LANGUAGE_CHINESE])
async def test_enabled_private_menus_and_help_only(harness, language):
    """Both languages must expose the opt-in command exclusively in private scopes."""

    h = harness()
    h.sessions.get_state.return_value.language = language
    bot = SimpleNamespace(delete_my_commands=AsyncMock(), set_my_commands=AsyncMock())
    await _start_application(SimpleNamespace(bot=bot, start=AsyncMock()), handlers=h.handlers)
    await h.handlers._set_chat_command_menu(bot, CHAT_ID, language)
    menus = bot.set_my_commands.await_args_list
    assert "video" in {item.command for item in menus[0].args[0]}
    assert menus[1].kwargs == {"scope": BotCommandScopeAllGroupChats()}
    assert "video" not in {item.command for item in menus[1].args[0]}
    assert menus[2].kwargs == {"scope": BotCommandScopeChat(chat_id=CHAT_ID)}
    assert next(item for item in menus[2].args[0] if item.command == "video").description == translate("command.video.description", language)
    await h.handlers.help(h.update, h.context)
    assert "/video" in h.message.reply_text.await_args.args[0]
    assert "/video" in _help_text(language, enable_video_generation=True)
    assert "/video" not in _help_text(language, group_only=True, enable_video_generation=True)
    await h.handlers._set_chat_command_menu(bot, -100, language)
    assert "video" not in {item.command for item in bot.set_my_commands.await_args.args[0]}


async def test_group_gate_rejects_video(harness):
    """Enabling videos must not expand the deliberately small group command gate."""

    h = harness()
    h.update.effective_chat.type = ChatType.GROUP
    assert "video" not in GROUP_COMMANDS
    with pytest.raises(ApplicationHandlerStop):
        await h.handlers.command_gate(h.update, h.context)
    h.message.reply_text.assert_awaited_once_with(translate("command.private_only", "en"))
    h.service.execute.assert_not_awaited()


def test_adapter_exact_contract_values(monkeypatch):
    """Every captured video field must match the contract without mutating the caller."""

    data = sample_request()
    original = decode(data)
    monkeypatch.setattr(thinking.secrets, "token_urlsafe", lambda count: "FAKE_OPAQUE" if count == 2600 else pytest.fail())
    monkeypatch.setattr(thinking, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    state = VideoRequestState()
    result = rewrite_video_request(data, state)
    expected = original.copy() + [None] * 18
    expected[0] = [PROMPT, 0, None, None, None, None, 0, None, None, [None, None, None, None, None, None, [[None, None, None, 1]]]]
    for index, value in {3: "!FAKE_OPAQUE", 4: "a" * 32, 30: [4, 16, 19], 49: 11, 55: [[16]], 67: 0, 68: 2, 91: 1, 96: 0, 98: 1}.items():
        expected[index] = value
    assert decode(result) == expected
    assert decode(data) == original
    assert result["at"] == data["at"]
    assert state.applied and not state.failed


@pytest.mark.parametrize("bad", [None, {"f.req": "invalid"}, {"f.req": "[]"}, {"f.req": json.dumps([None, "[]"])}])
def test_adapter_preserves_unknown_shapes_and_warns_once(bad, caplog):
    """An upstream shape change must fail closed without revealing content or flooding logs."""

    state = VideoRequestState()
    with caplog.at_level(logging.WARNING):
        assert rewrite_video_request(bad, state) is bad
        assert rewrite_video_request(bad, state) is bad
    assert state.failed and not state.applied
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.WARNING


async def test_adapter_real_upstream_builder_and_scope_reset():
    """The actual session proxy must adapt only video sends and restore Medium afterwards."""

    class Capture:
        def __init__(self):
            self.requests = []

        def stream(self, *args, **kwargs):
            self.requests.append(kwargs)
            raise gw_exc.GeminiError("FAKE_CAPTURE_COMPLETE")

    client = ThinkingLevelGeminiClient(secure_1psid="FAKE_1PSID_FOR_TEST", secure_1psidts="FAKE_1PSIDTS_FOR_TEST")
    transport = Capture()
    client._running = True
    client.client = transport
    assert client._live_client is transport
    with thinking_request(ThinkingLevel.MEDIUM):
        with video_request() as state:
            with pytest.raises(gw_exc.GeminiError, match="FAKE_CAPTURE_COMPLETE"):
                async for _ in client._generate(prompt=PROMPT, extended_thinking=False):
                    pytest.fail("No response parsing is permitted")
            assert state.applied and not state.failed
            assert len(decode(transport.requests[-1]["data"])) == 99
            assert decode(transport.requests[-1]["data"])[80] == 1
        assert thinking._thinking_wire_value.get() == thinking.MEDIUM_WIRE_VALUE
    assert client._live_client is transport
    with pytest.raises(gw_exc.GeminiError):
        async for _ in client._generate(prompt=PROMPT, extended_thinking=False):
            pytest.fail("No response parsing is permitted")
    assert len(decode(transport.requests[-1]["data"])) == 81
    assert thinking._video_request.get() is None


def test_adapter_preserves_other_rpc_requests():
    """The video scope must not alter the read-chat or other non-generation RPCs."""

    transport = SimpleNamespace(stream=MagicMock())
    data, headers = sample_request(), {"FAKE_HEADER": "FAKE_VALUE"}
    with video_request() as state:
        proxy = thinking._ThinkingSessionProxy(transport, None, state)
        proxy.stream("POST", "https://example.invalid/FAKE_OTHER_RPC", data=data, headers=headers)
        assert not state.applied and not state.failed
    transport.stream.assert_called_once_with(
        "POST", "https://example.invalid/FAKE_OTHER_RPC", data=data, headers=headers,
    )
    assert transport.stream.call_args.kwargs["data"] is data


async def test_happy_path_relays_and_releases_queue(harness, caplog):
    """Polling must release interactive capacity and deliver a measured, cleaned-up relay once."""

    h = harness()
    video = FakeVideo()
    h.client.fetch_latest_chat_response.side_effect = [SimpleNamespace(videos=[]), SimpleNamespace(videos=[video])]
    with caplog.at_level(logging.INFO):
        task = await h.start()
        h.status.edit_text.assert_awaited_once_with(translate("video.generating", "en") + "\n\n" + REPLY, parse_mode=None)
        assert await h.rows() == []
        async with h.queue.request(USER_ID + 1):
            assert not task.done()
        assert await h.clock.tick() == 10
        assert await h.clock.tick() == 10
        await asyncio.wait_for(task, 1)
    h.client.start_chat.assert_called_once_with(metadata=None, model=None, gem=None)
    h.session.send_message.assert_awaited_once_with(PROMPT, extended_thinking=False)
    for entry in h.client.fetch_latest_chat_response.await_args_list:
        assert entry.args == (CID,) and entry.kwargs["limit"] == 5
        assert not entry.kwargs["match"](SimpleNamespace(videos=[]))
        assert entry.kwargs["match"](SimpleNamespace(videos=[video]))
    assert h.message.reply_video.await_count == 2
    assert h.message.reply_video.await_args_list[0] == call(URL)
    assert h.relay_payloads[0][0] == bytes(12)
    assert h.relay_payloads[0][2] == {"supports_streaming": True}
    assert h.egress.month_to_date_bytes == 12
    assert video.paths and all(not path.exists() and not path.parent.exists() for path in video.paths)
    h.status.delete.assert_awaited_once_with()
    h.sessions.get_or_create.assert_not_awaited()
    h.sessions.persist.assert_not_awaited()
    rows = await h.rows()
    assert len(rows) == 1 and rows[0].command == "video" and rows[0].ok
    assert rows[0].latency_ms == 22000 and rows[0].error_kind is None
    timing = [r.getMessage() for r in caplog.records if r.name == "gemini_tg_bot.timing"]
    assert len(timing) == 1
    assert "command=video" in timing[0] and "upstream=2.0" in timing[0]
    assert "first_output=- generation=-" in timing[0]


async def test_nonmatching_output_keeps_polling(harness):
    """fetch_latest_chat_response can return nonmatching text and must not complete the job."""

    h = harness()
    h.client.fetch_latest_chat_response.return_value = SimpleNamespace(videos=[], text=REPLY)
    task = await h.start()
    await h.clock.tick()
    await h.clock.tick()
    await asyncio.sleep(0)
    assert not task.done()
    assert h.client.fetch_latest_chat_response.await_count == 2
    h.message.reply_video.assert_not_awaited()
    assert await h.rows() == []


async def test_timeout_removes_job_and_accounts_once(harness, caplog):
    """The video deadline must free the chat and record timeout over the full latency."""

    h = harness(timeout=25)
    h.client.fetch_latest_chat_response.return_value = SimpleNamespace(videos=[])
    with caplog.at_level(logging.INFO):
        task = await h.start()
        assert [await h.clock.tick(), await h.clock.tick(), await h.clock.tick()] == [10, 10, 5]
        await asyncio.wait_for(task, 1)
    assert not h.handlers._video_jobs._jobs
    assert h.status.edit_text.await_args == call(translate("video.timeout", "en"), parse_mode=None)
    rows = await h.rows()
    assert len(rows) == 1 and not rows[0].ok and rows[0].error_kind == "timeout"
    assert rows[0].command == "video" and rows[0].latency_ms == 27000
    assert len([r for r in caplog.records if r.name == "gemini_tg_bot.timing"]) == 1


async def test_second_video_is_rejected_while_running(harness):
    """A duplicate command must preserve the original background job and avoid another send."""

    h = harness()
    task = await h.start()
    await h.handlers.video(h.update, h.context)
    assert h.message.reply_text.await_args == call(translate("video.already_generating", "en"))
    h.session.send_message.assert_awaited_once()
    assert h.handlers._video_jobs._jobs[CHAT_ID].task is task
    rows = await h.rows()
    assert len(rows) == 1 and rows[0].error_kind == "already_generating"


@pytest.mark.parametrize("before_start", [True, False])
async def test_shutdown_cancels_all_jobs(harness, before_start):
    """Shutdown must also account for jobs cancelled before their coroutine first runs."""

    h = harness()
    task = await h.start()
    if not before_start:
        await asyncio.wait_for(h.clock.sleeps.get(), 1)
    await h.handlers.close()
    assert task.cancelled() and not h.handlers._video_jobs._jobs
    rows = await h.rows()
    assert len(rows) == 1 and rows[0].command == "video" and rows[0].error_kind == "cancelled"
    await h.handlers.close()
    assert len(await h.rows()) == 1


async def test_rewrite_failure_is_unavailable_without_polling(harness, caplog):
    """Failed adaptation must report unavailability immediately and never poll a normal chat."""

    h = harness(malformed=True)
    with caplog.at_level(logging.WARNING):
        assert await h.start() is None
    assert h.status.edit_text.await_args == call(translate("video.unavailable", "en"), parse_mode=None)
    h.client.fetch_latest_chat_response.assert_not_awaited()
    assert not h.handlers._video_jobs._jobs
    assert len(caplog.records) == 1
    rows = await h.rows()
    assert len(rows) == 1 and rows[0].error_kind == "unavailable"


async def test_no_content_leaks_to_logs(harness, caplog):
    """Request content, replies, URLs, conversation IDs and Telegram identities must stay out of logs."""

    h = harness()
    # Isolate application logging from aiosqlite's DEBUG SQL parameter tracing.
    h.handlers._usage_dao = AsyncMock()
    h.client.fetch_latest_chat_response.return_value = SimpleNamespace(videos=[FakeVideo()])
    with caplog.at_level(logging.DEBUG):
        task = await h.start()
        await h.clock.tick()
        await asyncio.wait_for(task, 1)
    assert caplog.records
    for value in (PROMPT, REPLY, URL, CID, str(CHAT_ID), str(USER_ID)):
        assert value not in caplog.text


async def test_url_success_skips_relay(harness):
    """Telegram URL delivery should consume no VM media egress or download."""

    h = harness()
    h.message.reply_video.side_effect = None
    video = FakeVideo()
    h.client.fetch_latest_chat_response.return_value = SimpleNamespace(videos=[video])
    task = await h.start()
    await h.clock.tick()
    await asyncio.wait_for(task, 1)
    h.message.reply_video.assert_awaited_once_with(URL)
    assert not video.paths and h.egress.month_to_date_bytes == 0
    assert (await h.rows())[0].ok


async def test_relay_uses_video_path_and_cleans_thumbnail(harness):
    """Real save results include a thumbnail that must never be uploaded or metered."""

    h = harness()
    video = FakeVideo(size=19, with_thumbnail=True)
    h.client.fetch_latest_chat_response.return_value = SimpleNamespace(videos=[video])
    task = await h.start()
    await h.clock.tick()
    await asyncio.wait_for(task, 1)

    assert h.relay_payloads == [(bytes(19), str(video.paths[1]), {"supports_streaming": True})]
    assert video.paths[0].suffix == ".jpg" and video.paths[1].suffix == ".mp4"
    assert h.egress.month_to_date_bytes == 19
    assert all(not path.exists() and not path.parent.exists() for path in video.paths)
    h.status.delete.assert_awaited_once_with()
    rows = await h.rows()
    assert len(rows) == 1 and rows[0].ok


@pytest.mark.parametrize("missing_key", [False, True])
async def test_save_without_video_is_unavailable(harness, missing_key):
    """A missing video path must fail delivery without attempting a file upload."""

    class MissingVideo(FakeVideo):
        async def save(self, *, path):
            result = await super().save(path=path)
            result["video"] = None
            if missing_key:
                del result["video"]
            return result

    h = harness()
    video = MissingVideo(with_thumbnail=True)
    h.client.fetch_latest_chat_response.return_value = SimpleNamespace(videos=[video])
    task = await h.start()
    await h.clock.tick()
    await asyncio.wait_for(task, 1)

    # The optimistic URL attempt was rejected; no subsequent file send occurs.
    h.message.reply_video.assert_awaited_once_with(URL)
    assert h.relay_payloads == [] and h.egress.month_to_date_bytes == 0
    assert h.status.edit_text.await_args == call(translate("video.unavailable", "en"), parse_mode=None)
    h.status.delete.assert_not_awaited()
    assert all(not path.exists() for path in video.paths)
    rows = await h.rows()
    assert len(rows) == 1 and not rows[0].ok and rows[0].error_kind is not None


async def test_late_video_has_a_separate_delivery_deadline(harness, monkeypatch):
    """A video found before polling expires deserves the full delivery budget."""

    h = harness(timeout=25, delivery_timeout=60)
    monkeypatch.setattr(asyncio, "timeout", h.clock.timeout)

    class SlowVideo(FakeVideo):
        async def save(self, *, path):
            await h.clock.sleep(20)
            return await super().save(path=path)

    video = SlowVideo(with_thumbnail=True)
    h.client.fetch_latest_chat_response.side_effect = [SimpleNamespace(videos=[]), SimpleNamespace(videos=[video])]
    task = await h.start()
    assert await h.clock.tick() == 10
    assert await h.clock.tick() == 10
    # Five polling seconds remain, but the relay needs another twenty seconds.
    assert await h.clock.tick() == 20
    await asyncio.wait_for(task, 1)

    assert len(h.relay_payloads) == 1 and h.egress.month_to_date_bytes == 12
    h.status.delete.assert_awaited_once_with()
    rows = await h.rows()
    assert len(rows) == 1 and rows[0].ok and rows[0].latency_ms == 42000


async def test_delivery_deadline_reports_timeout_and_cleans_files(harness, monkeypatch):
    """A relay stuck in upstream save retries must stop at REQUEST_TIMEOUT_SEC."""

    h = harness(timeout=300, delivery_timeout=7)
    monkeypatch.setattr(asyncio, "timeout", h.clock.timeout)

    class SlowVideo(FakeVideo):
        async def save(self, *, path):
            result = await super().save(path=path)
            await h.clock.sleep(8)
            return result

    video = SlowVideo(with_thumbnail=True)
    h.client.fetch_latest_chat_response.return_value = SimpleNamespace(videos=[video])
    task = await h.start()
    assert await h.clock.tick() == 10
    assert await h.clock.tick() == 8
    await asyncio.wait_for(task, 1)

    assert h.status.edit_text.await_args == call(translate("video.timeout", "en"), parse_mode=None)
    h.status.delete.assert_not_awaited()
    h.message.reply_video.assert_awaited_once_with(URL)
    assert not h.relay_payloads and h.egress.month_to_date_bytes == 0
    assert all(not path.exists() and not path.parent.exists() for path in video.paths)
    assert not h.handlers._video_jobs._jobs
    rows = await h.rows()
    assert len(rows) == 1 and not rows[0].ok and rows[0].error_kind == "timeout"


async def test_large_video_is_not_uploaded(harness):
    """The relay must reject a file above 50 MB and still delete temporary files."""

    h = harness()
    video = FakeVideo(size=MAX_VIDEO_BYTES + 1)
    h.client.fetch_latest_chat_response.return_value = SimpleNamespace(videos=[video])
    task = await h.start()
    await h.clock.tick()
    await asyncio.wait_for(task, 1)
    h.message.reply_video.assert_awaited_once_with(URL)
    assert h.status.edit_text.await_args == call(translate("video.too_large", "en"), parse_mode=None)
    assert h.egress.month_to_date_bytes == 0
    assert all(not path.exists() for path in video.paths)
    assert (await h.rows())[0].error_kind == "too_large"


async def test_usage_ignores_attachments_and_uses_quoted_context(harness):
    """Video prompts follow /img quoting but never download an attached reference image."""

    h = harness()
    h.context.args = []
    h.message.photo = [SimpleNamespace(get_file=AsyncMock())]
    assert await h.start() is None
    h.message.reply_text.assert_awaited_once_with(translate("video.usage", "en"))
    h.message.photo[0].get_file.assert_not_awaited()
    h.service.execute.assert_not_awaited()
    h.message.reply_to_message = SimpleNamespace(text="FAKE_QUOTED_CONTEXT", from_user=SimpleNamespace(full_name="FAKE_SENDER"))
    await h.start()
    prompt = h.session.send_message.await_args.args[0]
    assert "FAKE_QUOTED_CONTEXT" in prompt and "FAKE_SENDER" in prompt
    h.message.photo[0].get_file.assert_not_awaited()


async def test_status_is_plain_text_and_fits_telegram(harness):
    """Long refusal text should be visible immediately without markup parsing or length errors."""

    h = harness()

    async def initial(*args, **kwargs):
        await h.initial_send(*args, **kwargs)
        return SimpleNamespace(text="<b>FAKE_REFUSAL</b>" + "😀" * 5000)

    h.session.send_message.side_effect = initial
    await h.start()
    text = h.status.edit_text.await_args.args[0]
    assert "<b>FAKE_REFUSAL</b>" in text
    assert len(text.encode("utf-16-le")) <= 4096 * 2
    assert h.status.edit_text.await_args.kwargs == {"parse_mode": None}


async def test_send_video_uses_common_transport(monkeypatch):
    """Video sends must retain the shared Telegram retry and timing policy."""

    import gemini_tg_bot.telegram.sending as sending

    transport = AsyncMock()
    monkeypatch.setattr(sending, "call_telegram", transport)
    message = SimpleNamespace(reply_video=AsyncMock())
    await send_video(message, URL, supports_streaming=True)
    transport.assert_awaited_once_with(message.reply_video, URL, supports_streaming=True)


def test_video_timeout_setting_is_positive_and_defaults_to_five_minutes():
    """The background deadline must have a safe default and reject nonpositive values."""

    values = dict(TELEGRAM_BOT_TOKEN="FAKE_TELEGRAM_TOKEN", ADMIN_USER_ID=1, GEMINI_SECURE_1PSID="FAKE_1PSID_FOR_TEST", GEMINI_SECURE_1PSIDTS="FAKE_1PSIDTS_FOR_TEST")
    assert Settings(_env_file=None, **values).video_timeout_sec == 300
    assert Settings(_env_file=None, **values, VIDEO_TIMEOUT_SEC=17).video_timeout_sec == 17
    for value in (0, -1):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **values, VIDEO_TIMEOUT_SEC=value)
