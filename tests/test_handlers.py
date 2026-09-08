from __future__ import annotations

import asyncio
import ast
import inspect
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from gemini_webapi import ModelOutput
from gemini_webapi.constants import AccountStatus
from pydantic import SecretStr
from telegram.constants import MediaGroupLimit, ParseMode
from telegram.error import RetryAfter

from gemini_tg_bot.__main__ import _start_application
from gemini_tg_bot.gemini.service import DegradedReason, ServiceState
from gemini_tg_bot.queue import RequestQueue
from gemini_tg_bot.storage.models import UsageLog, UsageLogDAO
from gemini_tg_bot.telegram.handlers import (
    CALLBACK_DATA_LIMIT,
    GEM_LIST_UNAVAILABLE,
    IMAGE_GENERATION_PREFIX,
    IMAGE_USAGE,
    MODEL_LIST_UNAVAILABLE,
    PUBLIC_BOT_COMMANDS,
    EgressMeter,
    TelegramHandlers,
    register_handlers,
)
from gemini_tg_bot.telegram.media import MediaHandler
from gemini_tg_bot.telegram.sending import MAX_FLOOD_WAIT_SECONDS, SERVICE_BUSY
from gemini_tg_bot.telegram.streaming import PLACEHOLDER_TEXT

try:
    from gemini_tg_bot.gemini.sessions import ChatSessionRegistry as _RegistrySpec
except ModuleNotFoundError:
    # T2.3 is landing independently in the shared worktree.  Keep this task's
    # mechanical DoD runnable until that module is present; once it lands the
    # exact production class above automatically becomes the mock spec.
    class _RegistrySpec:
        async def get_state(self, chat_id: int) -> Any: ...
        async def get_or_create(self, chat_id: int) -> Any: ...
        async def reset(self, chat_id: int) -> None: ...
        async def set_model(self, chat_id: int, model: str | None) -> None: ...
        async def set_gem(self, chat_id: int, gem_id: str | None) -> None: ...
        async def set_temporary(self, chat_id: int, temporary: bool) -> None: ...
        async def persist(self, chat_id: int, session: Any) -> None: ...
        async def restore_all(self) -> None: ...


NOW = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)

_TELEGRAM_SEND_METHODS = {
    "answer",
    "delete",
    "edit_message_text",
    "edit_text",
    "reply_document",
    "reply_media_group",
    "reply_photo",
    "reply_text",
}


def _state(
    *,
    chat_id: int = 202,
    cid: str | None = "cid-test",
    model: str | None = "dynamic-model",
    temporary: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        chat_id=chat_id,
        cid=cid,
        model=model,
        gem_id=None,
        temporary=temporary,
        updated_at=NOW.isoformat(),
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "handlers.py",
        "media.py",
        "streaming.py",
    ],
)
def test_telegram_send_sites_use_common_transport(relative_path: str) -> None:
    source_path = (
        Path(__file__).parents[1]
        / "src"
        / "gemini_tg_bot"
        / "telegram"
        / relative_path
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    direct_send_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and (
            (
                isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in _TELEGRAM_SEND_METHODS
            )
            or (
                isinstance(node.value.func, ast.Name)
                and node.value.func.id == "sender"
            )
        )
    ]

    assert direct_send_lines == []


def _update(
    *,
    text: str = "hello",
    user_id: int = 101,
    chat_id: int = 202,
) -> SimpleNamespace:
    placeholder = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
    message = SimpleNamespace(
        text=text,
        delete=AsyncMock(),
        reply_text=AsyncMock(return_value=placeholder),
        reply_photo=AsyncMock(),
        reply_document=AsyncMock(),
        reply_media_group=AsyncMock(),
        placeholder=placeholder,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message,
        callback_query=None,
    )


def _callback_update(
    data: str,
    *,
    user_id: int = 101,
    chat_id: int = 202,
) -> SimpleNamespace:
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=None,
        callback_query=query,
    )


def _client_service(client: Any) -> MagicMock:
    async def execute(operation: Any) -> Any:
        result = operation(client)
        return await result if inspect.isawaitable(result) else result

    service = MagicMock()
    service.execute = AsyncMock(side_effect=execute)
    service.health = SimpleNamespace(
        state=ServiceState.HEALTHY,
        accepting_requests=True,
        degraded_reason=None,
        account_status=AccountStatus.AVAILABLE,
        last_error_kind=None,
        last_error_type=None,
    )
    return service


class _StreamingClient:
    def __init__(self, outputs: list[Any]) -> None:
        self._outputs = outputs
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _generate(self) -> Any:
        for output in self._outputs:
            yield output

    def generate_content_stream(self, prompt: str, **kwargs: Any) -> Any:
        self.calls.append((prompt, kwargs))
        return self._generate()


@pytest.fixture
def registry() -> AsyncMock:
    mocked = AsyncMock(spec=_RegistrySpec)
    mocked.get_state.return_value = _state()
    return mocked


@pytest.fixture
def handlers_factory(tmp_path: Path, registry: AsyncMock):
    def make(
        client: Any | None = None,
        *,
        usage_dao: UsageLogDAO | AsyncMock | None = None,
        egress_meter: EgressMeter | None = None,
        cookie_path: Path | None = None,
        research: AsyncMock | None = None,
        request_queue: RequestQueue | None = None,
        media_handler: MediaHandler | None = None,
    ) -> tuple[TelegramHandlers, MagicMock]:
        service = _client_service(client or MagicMock())
        handlers = TelegramHandlers(
            service=service,
            sessions=registry,
            request_queue=request_queue
            or RequestQueue(
                max_concurrency=1,
                user_rate_limit_per_min=10,
            ),
            usage_dao=usage_dao,
            egress_meter=egress_meter or EgressMeter(now=lambda: NOW),
            cookie_path=cookie_path or tmp_path,
            secure_1psid=SecretStr("FAKE_1PSID_FOR_TEST"),
            research=research,  # type: ignore[arg-type]
            media_handler=media_handler,
            now=lambda: NOW,
        )
        return handlers, service

    return make


async def test_help_and_new_commands(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    handlers, _ = handlers_factory()
    update = _update()

    await handlers.start(update, SimpleNamespace())
    help_text = update.effective_message.reply_text.await_args.args[0]
    for item in PUBLIC_BOT_COMMANDS:
        assert f"/{item.command} — {item.description}" in help_text

    await handlers.new(update, SimpleNamespace())
    registry.reset.assert_awaited_once_with(202)
    assert "新的對話" in update.effective_message.reply_text.await_args.args[0]


async def test_new_retries_bounded_flood_control_and_succeeds(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    handlers, _ = handlers_factory()
    update = _update(text="/new")
    update.effective_message.reply_text.side_effect = [
        RetryAfter(3),
        update.effective_message.placeholder,
    ]
    sleep = AsyncMock()

    with patch("gemini_tg_bot.telegram.sending.asyncio.sleep", sleep):
        await handlers.new(update, SimpleNamespace())

    registry.reset.assert_awaited_once_with(202)
    assert update.effective_message.reply_text.await_args_list == [
        call("已開始新的對話。"),
        call("已開始新的對話。"),
    ]
    sleep.assert_awaited_once_with(3.0)


async def test_new_reports_busy_when_flood_wait_exceeds_limit(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    handlers, _ = handlers_factory()
    update = _update(text="/new")
    update.effective_message.reply_text.side_effect = [
        RetryAfter(MAX_FLOOD_WAIT_SECONDS + 1),
        update.effective_message.placeholder,
    ]
    sleep = AsyncMock()

    with patch("gemini_tg_bot.telegram.sending.asyncio.sleep", sleep):
        await handlers.new(update, SimpleNamespace())

    registry.reset.assert_awaited_once_with(202)
    assert update.effective_message.reply_text.await_args_list == [
        call("已開始新的對話。"),
        call(SERVICE_BUSY),
    ]
    sleep.assert_not_awaited()


async def test_temp_toggles_from_registry_state(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    handlers, _ = handlers_factory()
    update = _update()
    registry.get_state.side_effect = [
        _state(temporary=False),
        _state(temporary=True),
    ]

    await handlers.temp(update, SimpleNamespace())
    await handlers.temp(update, SimpleNamespace())

    assert registry.set_temporary.await_args_list == [
        call(202, True),
        call(202, False),
    ]


async def test_research_submits_topic_and_immediately_replies_with_task_id(
    handlers_factory,
) -> None:
    research = AsyncMock()
    research.submit.return_value = "research-task-123"
    handlers, _ = handlers_factory(research=research)
    update = _update(text="/research orbital solar power")

    await handlers.research(
        update,
        SimpleNamespace(args=["orbital", "solar", "power"]),
    )

    research.submit.assert_awaited_once_with(202, "orbital solar power")
    update.effective_message.reply_text.assert_awaited_once_with(
        "Deep Research 任務已提交：research-task-123"
    )


async def test_research_rejects_an_empty_topic(handlers_factory) -> None:
    research = AsyncMock()
    handlers, _ = handlers_factory(research=research)
    update = _update(text="/research")

    await handlers.research(update, SimpleNamespace(args=[]))

    research.submit.assert_not_awaited()
    update.effective_message.reply_text.assert_awaited_once_with(
        "用法：/research <topic>"
    )


async def test_research_status_lists_only_manager_results_for_chat(
    handlers_factory,
) -> None:
    research = AsyncMock()
    research.status.return_value = [
        SimpleNamespace(
            task_id="research-task-123",
            status=SimpleNamespace(value="running"),
        ),
        SimpleNamespace(
            task_id="research-task-456",
            status=SimpleNamespace(value="done"),
        ),
    ]
    handlers, _ = handlers_factory(research=research)
    update = _update(text="/research_status")

    await handlers.research_status(update, SimpleNamespace())

    research.status.assert_awaited_once_with(202)
    update.effective_message.reply_text.assert_awaited_once_with(
        "Deep Research 任務狀態：\n"
        "research-task-123：running\n"
        "research-task-456：done"
    )


async def test_img_explicitly_requests_generation_and_uses_media_handler(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    image = SimpleNamespace(
        url="https://example.test/generated.png",
        title="Generated",
        alt="Generated image",
    )
    output = SimpleNamespace(
        text="",
        text_delta="",
        images=[image],
        candidates=[
            SimpleNamespace(web_images=[], generated_images=[image]),
        ],
        chosen=0,
    )
    client = _StreamingClient([output])
    media_handler = MagicMock(spec=MediaHandler)
    session = SimpleNamespace()
    registry.get_or_create.return_value = session
    handlers, _ = handlers_factory(client, media_handler=media_handler)
    update = _update(text="/img 台北 101 的水彩畫")
    delivery_order: list[str] = []

    async def delete_placeholder() -> None:
        delivery_order.append("delete-placeholder")

    async def send_images(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        delivery_order.append("send-images")

    update.effective_message.placeholder.delete.side_effect = delete_placeholder
    media_handler.send_output_images.side_effect = send_images

    await handlers.img(
        update,
        SimpleNamespace(args=["台北", "101", "的水彩畫"]),
    )

    assert client.calls == [
        (
            f"{IMAGE_GENERATION_PREFIX}\n\n台北 101 的水彩畫",
            {"chat": session, "temporary": False},
        )
    ]
    media_handler.send_output_images.assert_awaited_once_with(
        update.effective_message,
        output,
        caption=None,
    )
    update.effective_message.placeholder.edit_text.assert_not_awaited()
    update.effective_message.placeholder.delete.assert_awaited_once_with()
    assert delivery_order == ["delete-placeholder", "send-images"]
    registry.persist.assert_awaited_once_with(202, session)


async def test_img_without_prompt_replies_with_usage(handlers_factory) -> None:
    media_handler = MagicMock(spec=MediaHandler)
    handlers, service = handlers_factory(media_handler=media_handler)
    update = _update(text="/img")

    await handlers.img(update, SimpleNamespace(args=[]))

    update.effective_message.reply_text.assert_awaited_once_with(IMAGE_USAGE)
    service.execute.assert_not_awaited()
    media_handler.send_output_images.assert_not_awaited()


async def test_real_generated_image_fixture_deletes_placeholder_before_media(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "image-generated.json"
    output = ModelOutput.model_validate_json(fixture_path.read_text(encoding="utf-8"))
    media_handler = MagicMock(spec=MediaHandler)
    registry.get_or_create.return_value = SimpleNamespace()
    handlers, _ = handlers_factory(
        _StreamingClient([output]),
        media_handler=media_handler,
    )
    update = _update(text="/img cat")
    delivery_order: list[str] = []

    async def delete_placeholder() -> None:
        delivery_order.append("delete-placeholder")

    async def send_images(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        delivery_order.append("send-images")

    update.effective_message.placeholder.delete.side_effect = delete_placeholder
    media_handler.send_output_images.side_effect = send_images

    await handlers.img(update, SimpleNamespace(args=["cat"]))

    assert output.text == "\n\n_543\n\n"
    assert len(output.images) == 1
    update.effective_message.placeholder.edit_text.assert_not_awaited()
    assert delivery_order == ["delete-placeholder", "send-images"]
    media_handler.send_output_images.assert_awaited_once_with(
        update.effective_message,
        output,
        caption=None,
    )


async def test_model_lists_dynamic_available_models_and_skips_oversized_data(
    handlers_factory,
) -> None:
    available = SimpleNamespace(
        model_name="dynamic-model",
        display_name="Dynamic Model",
        is_available=True,
    )
    unavailable = SimpleNamespace(
        model_name="disabled-model",
        display_name="Disabled Model",
        is_available=False,
    )
    oversized = SimpleNamespace(
        model_name="界" * CALLBACK_DATA_LIMIT,
        display_name="Too Large",
        is_available=True,
    )
    client = MagicMock()
    client.list_models.return_value = [available, unavailable, oversized]
    handlers, _ = handlers_factory(client)
    update = _update()

    await handlers.model(update, SimpleNamespace())

    client.list_models.assert_called_once_with()
    reply = update.effective_message.reply_text.await_args
    keyboard = reply.kwargs["reply_markup"].inline_keyboard
    assert len(keyboard) == 1
    assert keyboard[0][0].text == "Dynamic Model"
    assert keyboard[0][0].callback_data == "model:dynamic-model"


async def test_model_handles_upstream_none(handlers_factory) -> None:
    client = MagicMock()
    client.list_models.return_value = None
    handlers, _ = handlers_factory(client)
    update = _update()

    await handlers.model(update, SimpleNamespace())

    update.effective_message.reply_text.assert_awaited_once_with(
        MODEL_LIST_UNAVAILABLE
    )


async def test_model_callback_revalidates_and_resolves_stable_name(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    model = SimpleNamespace(
        model_name="dynamic-model",
        display_name="Dynamic Model",
        is_available=True,
    )
    client = MagicMock()
    client.list_models.return_value = [model]
    client.resolve_model.return_value = model
    handlers, _ = handlers_factory(client)
    update = _callback_update("model:dynamic-model")

    await handlers.callback(update, SimpleNamespace())

    update.callback_query.answer.assert_awaited_once_with()
    client.list_models.assert_called_once_with()
    client.resolve_model.assert_called_once_with("dynamic-model")
    registry.set_model.assert_awaited_once_with(202, "dynamic-model")
    assert "Dynamic Model" in (
        update.callback_query.edit_message_text.await_args.args[0]
    )


async def test_gem_lists_and_selects_from_fresh_gem_jar(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    gem = SimpleNamespace(id="gem-id", name="My Gem")
    gem_jar = MagicMock()
    gem_jar.values.return_value = [gem]
    gem_jar.get.return_value = gem
    client = MagicMock()
    client.fetch_gems = AsyncMock(return_value=gem_jar)
    handlers, _ = handlers_factory(client)
    command_update = _update()

    await handlers.gem(command_update, SimpleNamespace())

    client.fetch_gems.assert_awaited_once_with(include_hidden=False)
    keyboard = command_update.effective_message.reply_text.await_args.kwargs[
        "reply_markup"
    ].inline_keyboard
    assert keyboard[0][0].text == "My Gem"
    assert keyboard[0][0].callback_data == "gem:gem-id"

    callback_update = _callback_update("gem:gem-id")
    await handlers.callback(callback_update, SimpleNamespace())

    assert client.fetch_gems.await_count == 2
    gem_jar.get.assert_called_once_with(id="gem-id")
    registry.set_gem.assert_awaited_once_with(202, "gem-id")
    assert "My Gem" in (
        callback_update.callback_query.edit_message_text.await_args.args[0]
    )


async def test_gem_handles_empty_jar(handlers_factory) -> None:
    gem_jar = MagicMock()
    gem_jar.values.return_value = []
    client = MagicMock()
    client.fetch_gems = AsyncMock(return_value=gem_jar)
    handlers, _ = handlers_factory(client)
    update = _update()

    await handlers.gem(update, SimpleNamespace())

    update.effective_message.reply_text.assert_awaited_once_with(
        GEM_LIST_UNAVAILABLE
    )


async def test_text_uses_current_session_service_renders_and_persists_usage(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    session = SimpleNamespace()
    output = SimpleNamespace(
        text="**hello**\n\nworld",
        text_delta="**hello**\n\nworld",
        images=(),
    )
    client = _StreamingClient([output])
    registry.get_state.return_value = _state(temporary=True)
    registry.get_or_create.return_value = session
    usage_dao = AsyncMock(spec=UsageLogDAO)
    handlers, service = handlers_factory(client, usage_dao=usage_dao)
    update = _update(text="question")

    await handlers.text_message(update, SimpleNamespace())

    registry.get_or_create.assert_awaited_once_with(202)
    service.execute.assert_awaited_once()
    assert client.calls == [
        (
            "question",
            {"chat": session, "temporary": True},
        )
    ]
    registry.persist.assert_awaited_once_with(202, session)
    update.effective_message.reply_text.assert_awaited_once_with(
        PLACEHOLDER_TEXT,
    )
    update.effective_message.placeholder.edit_text.assert_awaited_once_with(
        "<b>hello</b>\n\nworld",
        parse_mode=ParseMode.HTML,
    )
    saved = usage_dao.add.await_args.args[0]
    assert saved.user_id == 101
    assert saved.chat_id == 202
    assert saved.model == "dynamic-model"
    assert saved.ok is True


async def test_text_reports_bounded_flood_control_and_releases_slot(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    queue = RequestQueue(max_concurrency=1, user_rate_limit_per_min=10)
    usage_dao = AsyncMock(spec=UsageLogDAO)
    registry.get_or_create.return_value = SimpleNamespace()
    handlers, _ = handlers_factory(
        _StreamingClient([]),
        usage_dao=usage_dao,
        request_queue=queue,
    )
    update = _update(text="question")
    update.effective_message.reply_text.side_effect = [
        RetryAfter(MAX_FLOOD_WAIT_SECONDS + 1),
        update.effective_message.placeholder,
    ]

    await handlers.text_message(update, SimpleNamespace())

    assert update.effective_message.reply_text.await_args_list == [
        call(PLACEHOLDER_TEXT),
        call(SERVICE_BUSY),
    ]
    saved = usage_dao.add.await_args.args[0]
    assert saved.ok is False
    assert saved.error_kind == "flood_control"
    async with queue.request(user_id=202):
        pass


async def test_text_reports_queue_acquire_timeout(
    handlers_factory,
) -> None:
    queue = RequestQueue(
        max_concurrency=1,
        user_rate_limit_per_min=10,
        acquire_timeout=0.01,
    )
    usage_dao = AsyncMock(spec=UsageLogDAO)
    handlers, service = handlers_factory(
        _StreamingClient([]),
        usage_dao=usage_dao,
        request_queue=queue,
    )
    update = _update(text="question")

    async with queue.request(user_id=999):
        await handlers.text_message(update, SimpleNamespace())

    update.effective_message.reply_text.assert_awaited_once_with(SERVICE_BUSY)
    service.execute.assert_not_awaited()
    saved = usage_dao.add.await_args.args[0]
    assert saved.ok is False
    assert saved.error_kind == "queue_timeout"


async def test_degraded_text_request_fails_before_waiting_for_queue(
    handlers_factory,
) -> None:
    queue = RequestQueue(
        max_concurrency=1,
        user_rate_limit_per_min=10,
        acquire_timeout=30.0,
    )
    handlers, service = handlers_factory(request_queue=queue)
    service.health = SimpleNamespace(
        state=ServiceState.DEGRADED,
        accepting_requests=False,
        degraded_reason=DegradedReason.AUTH,
        account_status=AccountStatus.UNAUTHENTICATED,
    )
    update = _update(text="question")

    async with queue.request(user_id=999):
        await asyncio.wait_for(
            handlers.text_message(update, SimpleNamespace()),
            timeout=0.1,
        )

    service.execute.assert_not_awaited()
    update.effective_message.reply_text.assert_awaited_once_with(
        "Gemini 服務目前處於認證失效狀態，暫時無法接受請求。"
    )


async def test_setcookie_rejects_non_available_account_status(
    handlers_factory,
) -> None:
    handlers, service = handlers_factory()
    service.reinit = AsyncMock()
    service.health = SimpleNamespace(
        state=ServiceState.DEGRADED,
        accepting_requests=False,
        degraded_reason=DegradedReason.AUTH,
        account_status=AccountStatus.UNAUTHENTICATED,
    )
    handlers.bind_auth(SimpleNamespace(is_admin=lambda user_id: user_id == 101))
    handlers._awaiting_cookie_users.add(101)
    update = _update(
        text=(
            "__Secure-1PSID=FAKE_REPLACEMENT_1PSID_FOR_TEST\n"
            "__Secure-1PSIDTS=FAKE_REPLACEMENT_1PSIDTS_FOR_TEST"
        )
    )

    await handlers.setcookie_value(update, SimpleNamespace())

    reply = update.effective_message.reply_text.await_args.args[0]
    assert "Cookie 更新失敗" in reply
    assert AccountStatus.UNAUTHENTICATED.description in reply
    assert "Cookie 已更新" not in reply


@pytest.mark.parametrize(
    ("artifact_text", "expected_text", "parse_mode"),
    [
        ("_551", None, None),
        ("_0", None, None),
        (
            "http://googleusercontent.com/image_generation_content/0_551",
            None,
            None,
        ),
        (
            "台北101是台北的地標…\n_0",
            "台北101是台北的地標…\n",
            ParseMode.HTML,
        ),
    ],
)
async def test_text_stream_cleans_artifacts_and_sends_final_output_images(
    handlers_factory,
    registry: AsyncMock,
    artifact_text: str,
    expected_text: str | None,
    parse_mode: str | None,
) -> None:
    image = SimpleNamespace(
        url="https://example.test/generated.png",
        title="Generated",
        alt="A generated test image",
    )
    output = SimpleNamespace(
        text=artifact_text,
        text_delta=artifact_text,
        images=[image],
        candidates=[
            SimpleNamespace(web_images=[image], generated_images=[]),
        ],
        chosen=0,
    )
    client = _StreamingClient([output])
    session = SimpleNamespace()
    registry.get_state.return_value = _state(temporary=False)
    registry.get_or_create.return_value = session
    handlers, _ = handlers_factory(client)
    update = _update(text="generate an image")
    delivery_order: list[str] = []

    async def delete_placeholder() -> None:
        delivery_order.append("delete-placeholder")

    async def send_photo(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        delivery_order.append("send-photo")

    update.effective_message.placeholder.delete.side_effect = delete_placeholder
    update.effective_message.reply_photo.side_effect = send_photo

    await handlers.text_message(update, SimpleNamespace())

    update.effective_message.reply_text.assert_awaited_once_with(PLACEHOLDER_TEXT)
    if expected_text is None:
        update.effective_message.placeholder.edit_text.assert_not_awaited()
    else:
        update.effective_message.placeholder.edit_text.assert_awaited_once_with(
            expected_text,
            parse_mode=parse_mode,
        )
    expected_caption = expected_text
    expected_photo_kwargs = {"caption": expected_caption}
    if expected_caption is not None:
        expected_photo_kwargs["parse_mode"] = ParseMode.HTML
    update.effective_message.reply_photo.assert_awaited_once_with(
        image.url,
        **expected_photo_kwargs,
    )
    update.effective_message.placeholder.delete.assert_awaited_once_with()
    if expected_text is None:
        assert delivery_order == ["delete-placeholder", "send-photo"]
    assert "_551" not in str(
        update.effective_message.placeholder.edit_text.await_args_list
    )
    assert "_0" not in str(
        update.effective_message.placeholder.edit_text.await_args_list
    )
    registry.persist.assert_awaited_once_with(202, session)


async def test_text_stream_merges_rendered_text_into_image_caption(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    image = SimpleNamespace(
        url="https://example.test/generated.png",
        title="Generated",
        alt="Generated image",
    )
    output = SimpleNamespace(
        text="**short answer**",
        text_delta="**short answer**",
        images=[image],
        candidates=[
            SimpleNamespace(web_images=[], generated_images=[image]),
        ],
        chosen=0,
    )
    registry.get_or_create.return_value = SimpleNamespace()
    handlers, _ = handlers_factory(_StreamingClient([output]))
    update = _update(text="generate")

    await handlers.text_message(update, SimpleNamespace())

    update.effective_message.reply_text.assert_awaited_once_with(PLACEHOLDER_TEXT)
    update.effective_message.reply_photo.assert_awaited_once_with(
        image.url,
        caption="<b>short answer</b>",
        parse_mode=ParseMode.HTML,
    )
    update.effective_message.placeholder.delete.assert_awaited_once_with()


async def test_text_stream_over_caption_limit_keeps_separate_text_message(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    text = "x" * 1025
    images = [
        SimpleNamespace(
            url=f"https://example.test/generated-{index}.png",
            title="Generated",
            alt="Generated image",
        )
        for index in range(int(MediaGroupLimit.MIN_MEDIA_LENGTH) + 1)
    ]
    output = SimpleNamespace(
        text=text,
        text_delta=text,
        images=images,
        candidates=[
            SimpleNamespace(web_images=images, generated_images=[]),
        ],
        chosen=0,
    )
    registry.get_or_create.return_value = SimpleNamespace()
    handlers, _ = handlers_factory(_StreamingClient([output]))
    update = _update(text="generate")

    await handlers.text_message(update, SimpleNamespace())

    assert update.effective_message.placeholder.edit_text.await_args == call(
        text,
        parse_mode=ParseMode.HTML,
    )
    update.effective_message.reply_photo.assert_not_awaited()
    update.effective_message.reply_media_group.assert_awaited_once()
    media_group = update.effective_message.reply_media_group.await_args.args[0]
    assert [item.media for item in media_group] == [image.url for image in images]
    assert all(item.caption is None for item in media_group)
    update.effective_message.placeholder.delete.assert_not_awaited()


async def test_status_reports_required_fields_and_does_not_expose_cookie(
    handlers_factory,
    registry: AsyncMock,
    tmp_path: Path,
) -> None:
    cache_file = tmp_path / ".cached_cookies_FAKE_1PSID_FOR_TEST.json"
    cache_file.write_text("{}", encoding="utf-8")
    refresh_timestamp = datetime(2026, 9, 7, 7, 30, tzinfo=UTC).timestamp()
    os.utime(cache_file, (refresh_timestamp, refresh_timestamp))

    usage_dao = AsyncMock(spec=UsageLogDAO)
    usage_dao.list_for_chat.return_value = [
        UsageLog(
            id=1,
            user_id=101,
            chat_id=202,
            command="message",
            model="dynamic-model",
            ok=True,
            created_at="2026-09-07T01:00:00+00:00",
        ),
        UsageLog(
            id=2,
            user_id=101,
            chat_id=202,
            command="message",
            model="dynamic-model",
            ok=True,
            created_at="2026-09-06T23:59:59+00:00",
        ),
    ]
    egress = EgressMeter(now=lambda: NOW)
    egress.record(2048)
    handlers, _ = handlers_factory(
        usage_dao=usage_dao,
        egress_meter=egress,
        cookie_path=tmp_path,
    )
    update = _update()

    await handlers.status(update, SimpleNamespace())

    text = update.effective_message.reply_text.await_args.args[0]
    assert "目前模型：dynamic-model" in text
    assert "Session CID：cid-test" in text
    assert "Cookie 最後刷新時間：2026-09-07 07:30:00 UTC" in text
    assert "佇列深度：0" in text
    assert "今日用量：1" in text
    assert "本月累計 egress 估算值：2.0 KiB" in text
    assert (
        f"Account status：AVAILABLE — {AccountStatus.AVAILABLE.description}"
        in text
    )
    assert "FAKE_1PSID_FOR_TEST" not in text


async def test_status_reports_cookie_not_refreshed_when_cache_is_missing(
    handlers_factory,
) -> None:
    handlers, _ = handlers_factory()
    update = _update()

    await handlers.status(update, SimpleNamespace())

    assert "Cookie 最後刷新時間：尚未刷新" in (
        update.effective_message.reply_text.await_args.args[0]
    )


async def test_health_reports_account_status_name_and_description(
    handlers_factory,
) -> None:
    handlers, service = handlers_factory()
    service.health.account_status = AccountStatus.LOCATION_REJECTED
    handlers.bind_auth(SimpleNamespace(is_admin=lambda user_id: user_id == 101))
    update = _update(text="/health")

    await handlers.health(update, SimpleNamespace())

    report = update.effective_message.reply_text.await_args.args[0]
    assert (
        "Account status：LOCATION_REJECTED — "
        f"{AccountStatus.LOCATION_REJECTED.description}"
    ) in report
    assert "FAKE_1PSID_FOR_TEST" not in report


def test_egress_meter_resets_on_calendar_month() -> None:
    clock = [datetime(2026, 9, 30, 23, 59, tzinfo=UTC)]
    meter = EgressMeter(now=lambda: clock[0])
    meter.record(100)
    assert meter.month_to_date_bytes == 100

    clock[0] = datetime(2026, 10, 1, tzinfo=UTC)
    assert meter.month_to_date_bytes == 0


def test_registration_places_auth_in_first_group(
    handlers_factory,
) -> None:
    handlers, _ = handlers_factory()
    application = SimpleNamespace(add_handler=MagicMock())
    auth = AsyncMock()

    register_handlers(application, auth=auth, handlers=handlers)

    calls = application.add_handler.call_args_list
    assert calls[0].kwargs == {"group": -1}
    assert len(calls) == 13
    registered_commands = {
        command
        for registered in calls
        for command in getattr(registered.args[0], "commands", ())
    }
    assert {"img", "research", "research_status"} <= registered_commands


async def test_startup_registers_public_command_menu() -> None:
    application = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=AsyncMock()),
        start=AsyncMock(),
    )

    await _start_application(application)

    application.bot.set_my_commands.assert_awaited_once_with(PUBLIC_BOT_COMMANDS)
    registered_commands = {item.command for item in PUBLIC_BOT_COMMANDS}
    assert registered_commands == {
        "start",
        "help",
        "new",
        "model",
        "gem",
        "temp",
        "img",
        "research",
        "research_status",
        "status",
    }
    assert registered_commands.isdisjoint(
        {"setcookie", "allow", "deny", "health"}
    )
    application.start.assert_awaited_once_with()


async def test_command_menu_failure_does_not_prevent_startup(caplog) -> None:
    application = SimpleNamespace(
        bot=SimpleNamespace(
            set_my_commands=AsyncMock(side_effect=RuntimeError("offline"))
        ),
        start=AsyncMock(),
    )

    with caplog.at_level("WARNING"):
        await _start_application(application)

    application.start.assert_awaited_once_with()
    assert "Unable to register Telegram command menu (RuntimeError)" in caplog.text
