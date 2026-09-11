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
from telegram import BotCommandScopeAllGroupChats, BotCommandScopeChat
from telegram.constants import ChatType, MediaGroupLimit, ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import ApplicationHandlerStop

import gemini_tg_bot.telegram.handlers as handlers_module
from gemini_tg_bot.__main__ import (
    _initialize_and_start_polling,
    _log_cookie_location,
    _send_admin_notification,
    _start_application,
)
from gemini_tg_bot.config import RUNTIME_CREDENTIALS_FILENAME
from gemini_tg_bot.gemini.service import DegradedReason, ServiceState
from gemini_tg_bot.i18n import (
    LANGUAGE_CHINESE,
    LANGUAGE_ENGLISH,
    translate,
)
from gemini_tg_bot.queue import RequestQueue
from gemini_tg_bot.storage.db import Database
from gemini_tg_bot.storage.models import (
    AdminNotificationDAO,
    UsageLog,
    UsageLogDAO,
)
from gemini_tg_bot.telegram.handlers import (
    CALLBACK_DATA_LIMIT,
    GEM_LIST_UNAVAILABLE,
    IMAGE_GENERATION_PREFIX,
    IMAGE_USAGE,
    MODEL_LIST_UNAVAILABLE,
    GROUP_COMMANDS,
    GROUP_MENU_COMMANDS,
    HELP_COMMAND,
    PUBLIC_BOT_COMMANDS,
    PUBLIC_MENU_COMMANDS,
    QUOTED_CONTEXT_MAX_CHARS,
    QUOTED_CONTEXT_TRUNCATION_SUFFIX,
    QUOTED_DEFAULT_IMAGE_REQUEST,
    QUOTED_DEFAULT_QUESTION,
    QUOTED_QUESTION_LABEL,
    QUOTED_REQUEST_LABEL,
    EgressMeter,
    TelegramHandlers,
    _help_text,
    register_handlers,
)
from gemini_tg_bot.telegram.media import MediaHandler
from gemini_tg_bot.telegram.sending import MAX_FLOOD_WAIT_SECONDS, SERVICE_BUSY
from gemini_tg_bot.telegram.streaming import EDIT_CHARACTER_THRESHOLD, PLACEHOLDER_TEXT

try:
    from gemini_tg_bot.gemini.sessions import ChatSessionRegistry as _RegistrySpec
except ModuleNotFoundError:
    # T2.3 is landing independently in the shared worktree.  Keep this task's
    # mechanical DoD runnable until that module is present; once it lands the
    # exact production class above automatically becomes the mock spec.
    class _RegistrySpec:
        async def get_state(self, chat_id: int) -> Any: ...
        async def get_or_create(self, chat_id: int) -> Any: ...
        async def start_new(self, chat_id: int) -> Any: ...
        async def start_group_session(
            self, chat_id: int, bot_message_id: int | None = None
        ) -> tuple[Any, bool]: ...
        async def persist_group_session(
            self, chat_id: int, bot_message_id: int, session: Any
        ) -> None: ...
        async def reset(self, chat_id: int) -> None: ...
        async def set_model(self, chat_id: int, model: str | None) -> None: ...
        async def set_gem(self, chat_id: int, gem_id: str | None) -> None: ...
        async def set_temporary(self, chat_id: int, temporary: bool) -> None: ...
        async def set_extended_thinking(
            self, chat_id: int, enabled: bool
        ) -> None: ...
        async def set_language(self, chat_id: int, language: str) -> None: ...
        async def persist(self, chat_id: int, session: Any) -> None: ...
        async def restore_all(self) -> None: ...


NOW = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)
BOT_ID = 999

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
    gem_id: str | None = None,
    temporary: bool = False,
    extended_thinking: bool = False,
    language: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        chat_id=chat_id,
        cid=cid,
        model=model,
        gem_id=gem_id,
        temporary=temporary,
        extended_thinking=extended_thinking,
        language=language,
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
    chat_type: str = ChatType.PRIVATE,
    language_code: str | None = None,
    message_id: int = 301,
    reply_to_message_id: int | None = None,
    reply_to_user_id: int = BOT_ID,
    reply_to_user_is_bot: bool = True,
    reply_to_text: str | None = None,
    reply_to_caption: str | None = None,
    reply_to_sender_name: str | None = None,
) -> SimpleNamespace:
    placeholder = SimpleNamespace(
        message_id=message_id + 1,
        edit_text=AsyncMock(),
        delete=AsyncMock(),
    )
    message = SimpleNamespace(
        message_id=message_id,
        text=text,
        reply_to_message=(
            SimpleNamespace(
                message_id=reply_to_message_id,
                text=reply_to_text,
                caption=reply_to_caption,
                from_user=SimpleNamespace(
                    id=reply_to_user_id,
                    is_bot=reply_to_user_is_bot,
                    full_name=reply_to_sender_name,
                ),
            )
            if reply_to_message_id is not None
            else None
        ),
        delete=AsyncMock(),
        reply_text=AsyncMock(return_value=placeholder),
        reply_photo=AsyncMock(),
        reply_document=AsyncMock(),
        reply_media_group=AsyncMock(),
        placeholder=placeholder,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, language_code=language_code),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_message=message,
        callback_query=None,
    )


def _bot_context() -> SimpleNamespace:
    return SimpleNamespace(bot=SimpleNamespace(id=BOT_ID))


def _callback_update(
    data: str,
    *,
    user_id: int = 101,
    chat_id: int = 202,
    language_code: str | None = None,
) -> SimpleNamespace:
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, language_code=language_code),
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


class _StreamingSession:
    def __init__(self, outputs: list[Any]) -> None:
        self._outputs = outputs
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _generate(self) -> Any:
        for output in self._outputs:
            yield output

    def send_message_stream(self, prompt: str, **kwargs: Any) -> Any:
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
    """The English default must cover both discovery and conversation reset."""

    handlers, _ = handlers_factory()
    update = _update()

    await handlers.start(update, SimpleNamespace())
    help_text = update.effective_message.reply_text.await_args.args[0]
    for item in PUBLIC_BOT_COMMANDS:
        assert f"/{item.command} — {item.description}" in help_text

    await handlers.new(update, SimpleNamespace())
    registry.reset.assert_awaited_once_with(202)
    assert update.effective_message.reply_text.await_args.args[0] == translate(
        "new.started",
        LANGUAGE_ENGLISH,
    )


@pytest.mark.parametrize(
    "command",
    [
        "model",
        "gem",
        "temp",
        "think",
        "language",
        "new",
        "status",
        "research",
        "research_status",
    ],
)
async def test_private_only_commands_are_inert_in_groups(
    handlers_factory,
    command: str,
) -> None:
    """A hidden group command must not mutate state or reach Gemini."""

    handlers, service = handlers_factory()
    update = _update(text=f"/{command}", chat_type=ChatType.GROUP)

    await getattr(handlers, command)(update, SimpleNamespace(args=[]))

    update.effective_message.reply_text.assert_awaited_once_with(
        translate("command.private_only", LANGUAGE_ENGLISH)
    )
    service.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "chat_type",
    [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL],
)
async def test_every_non_private_chat_type_uses_the_safe_command_default(
    handlers_factory,
    chat_type: str,
) -> None:
    """Supergroups, channels, and future group forms must not bypass the gate."""

    handlers, _ = handlers_factory()
    update = _update(text="/new", chat_type=chat_type)

    await handlers.new(update, SimpleNamespace())

    update.effective_message.reply_text.assert_awaited_once_with(
        translate("command.private_only", LANGUAGE_ENGLISH)
    )


async def test_group_help_and_image_commands_remain_available(
    handlers_factory,
) -> None:
    """Hiding /help from menus must not break its conventional manual alias."""

    handlers, _ = handlers_factory()
    start_update = _update(text="/start", chat_type=ChatType.GROUP)
    help_update = _update(text="/help", chat_type=ChatType.GROUP)
    image_update = _update(text="/img", chat_type=ChatType.GROUP)

    await handlers.command_gate(start_update, SimpleNamespace())
    await handlers.start(start_update, SimpleNamespace())
    await handlers.command_gate(help_update, SimpleNamespace())
    await handlers.help(help_update, SimpleNamespace())
    await handlers.command_gate(image_update, SimpleNamespace())
    await handlers.img(image_update, SimpleNamespace(args=[]))

    for help_message in (start_update, help_update):
        help_text = help_message.effective_message.reply_text.await_args.args[0]
        assert _help_commands(help_text) == GROUP_COMMANDS
    image_update.effective_message.reply_text.assert_awaited_once_with(
        translate("image.usage", LANGUAGE_ENGLISH)
    )


def _help_commands(help_text: str) -> set[str]:
    return {
        line.partition(" ")[0].removeprefix("/")
        for line in help_text.splitlines()
        if line.startswith("/")
    }


@pytest.mark.parametrize("language", [LANGUAGE_ENGLISH, LANGUAGE_CHINESE])
@pytest.mark.parametrize("group_only", [False, True])
def test_help_lists_exact_commands_for_chat_scope(
    language: str,
    group_only: bool,
) -> None:
    """Accurate discovery prevents users from invoking unavailable commands."""

    help_text = _help_text(language, group_only=group_only)
    expected = (
        GROUP_COMMANDS
        if group_only
        else PUBLIC_MENU_COMMANDS | {HELP_COMMAND}
    )

    assert _help_commands(help_text) == expected
    for command in expected:
        assert (
            f"/{command} — "
            f"{translate(f'command.{command}.description', language)}"
        ) in help_text


@pytest.mark.parametrize(
    ("language", "expected_footer", "private_footer"),
    [
        (
            LANGUAGE_ENGLISH,
            "Reply to the bot's message to continue the current conversation.",
            "Send text directly to continue the current conversation.",
        ),
        (
            LANGUAGE_CHINESE,
            "回覆機器人的訊息即可延續目前對話。",
            "直接傳送文字即可延續目前對話。",
        ),
    ],
)
def test_group_help_explains_how_to_continue_a_conversation(
    language: str,
    expected_footer: str,
    private_footer: str,
) -> None:
    """Privacy-mode users need reply guidance that Telegram can deliver."""

    help_text = _help_text(language, group_only=True)

    assert help_text.endswith(expected_footer)
    assert private_footer not in help_text


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            LANGUAGE_ENGLISH,
            "Available commands:\n"
            "/start — Show usage instructions\n"
            "/help — Show usage instructions\n"
            "/gemini — Ask Gemini in a new conversation\n"
            "/new — Start a new conversation\n"
            "/model — Choose a Gemini model\n"
            "/gem — Choose a Gem\n"
            "/temp — Toggle temporary chat mode\n"
            "/think — Toggle Extended Thinking\n"
            "/language — Choose interface language\n"
            "/img — Generate an image\n"
            "/research — Submit a Deep Research task\n"
            "/research_status — View Deep Research task status\n"
            "/status — View current status\n\n"
            "Send text directly to continue the current conversation.",
        ),
        (
            LANGUAGE_CHINESE,
            "可用指令：\n"
            "/start — 顯示使用說明\n"
            "/help — 顯示使用說明\n"
            "/gemini — 以新對話詢問 Gemini\n"
            "/new — 開始新的對話\n"
            "/model — 選擇 Gemini 模型\n"
            "/gem — 選擇 Gem\n"
            "/temp — 切換暫時對話模式\n"
            "/think — 切換 Extended Thinking\n"
            "/language — 選擇介面語言\n"
            "/img — 生成圖片\n"
            "/research — 提交 Deep Research 任務\n"
            "/research_status — 查看 Deep Research 任務狀態\n"
            "/status — 查看目前狀態\n\n"
            "直接傳送文字即可延續目前對話。",
        ),
    ],
)
def test_private_help_output_remains_unchanged(
    language: str,
    expected: str,
) -> None:
    """Group-specific guidance must not alter established private-chat help."""

    assert _help_text(language) == expected


def test_group_help_reads_group_commands_at_render_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One live source prevents the menu, gate, and help text from drifting."""

    changed_commands = frozenset({"help", "img"})
    monkeypatch.setattr(handlers_module, "GROUP_COMMANDS", changed_commands)

    assert _help_commands(
        _help_text(LANGUAGE_ENGLISH, group_only=True)
    ) == changed_commands


async def test_gemini_without_question_returns_quoted_group_usage(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """A malformed group trigger needs actionable English guidance in-thread."""

    handlers, _ = handlers_factory()
    registry.get_state.return_value = _state(language=LANGUAGE_CHINESE)
    update = _update(text="/gemini", chat_id=-1001, chat_type=ChatType.GROUP)

    await handlers.gemini(update, SimpleNamespace(args=[]))

    update.effective_message.reply_text.assert_awaited_once_with(
        translate("gemini.usage", LANGUAGE_ENGLISH),
        do_quote=True,
    )
    registry.get_state.assert_not_awaited()


async def test_each_group_gemini_command_starts_and_records_a_fresh_thread(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """Independent /gemini questions must never leak context into each other."""

    output = SimpleNamespace(text="answer", text_delta="answer", images=())
    sessions = [_StreamingSession([output]), _StreamingSession([output])]
    registry.start_group_session.side_effect = [
        (sessions[0], False),
        (sessions[1], False),
    ]
    handlers, _ = handlers_factory()
    first = _update(
        text="/gemini first",
        chat_id=-1001,
        chat_type=ChatType.SUPERGROUP,
        message_id=100,
    )
    second = _update(
        text="/gemini second",
        chat_id=-1001,
        chat_type=ChatType.SUPERGROUP,
        message_id=200,
    )

    await handlers.gemini(first, SimpleNamespace(args=["first"]))
    await handlers.gemini(second, SimpleNamespace(args=["second"]))

    assert registry.start_group_session.await_args_list == [
        call(-1001, None),
        call(-1001, None),
    ]
    assert sessions[0].calls == [
        ("first", {"temporary": False, "extended_thinking": False})
    ]
    assert sessions[1].calls == [
        ("second", {"temporary": False, "extended_thinking": False})
    ]
    assert registry.persist_group_session.await_args_list == [
        call(-1001, 101, sessions[0]),
        call(-1001, 201, sessions[1]),
    ]
    assert first.effective_message.reply_text.await_args.kwargs["do_quote"] is True
    assert second.effective_message.reply_text.await_args.kwargs["do_quote"] is True
    registry.get_state.assert_not_awaited()
    registry.persist.assert_not_awaited()


async def test_group_img_uses_fixed_fresh_sessions_and_records_reply_threads(
    handlers_factory,
    tmp_path: Path,
) -> None:
    """Image requests must stay isolated yet remain continuable by reply."""

    from gemini_tg_bot.gemini.sessions import ChatSessionRegistry

    image = SimpleNamespace(
        url="https://example.test/generated.png",
        title="Generated",
        alt="Generated image",
    )
    image_output = SimpleNamespace(
        text="",
        text_delta="",
        images=[image],
        candidates=[SimpleNamespace(web_images=[], generated_images=[image])],
        chosen=0,
    )
    continued_output = SimpleNamespace(
        text="continued",
        text_delta="continued",
        images=(),
    )
    first_session = _StreamingSession([image_output])
    first_session.cid = "first-image-cid"
    first_session.metadata = ["first-image", None]
    second_session = _StreamingSession([image_output])
    second_session.cid = "second-image-cid"
    second_session.metadata = ["second-image", None]
    continued_session = _StreamingSession([continued_output])
    continued_session.cid = "continued-image-cid"
    continued_session.metadata = ["continued-image", None]
    resolved_group_model = object()
    client = MagicMock()
    client.resolve_model.return_value = resolved_group_model
    client.start_chat.side_effect = [
        first_session,
        second_session,
        continued_session,
    ]
    handlers, service = handlers_factory(client)
    service.client = client

    first = _update(
        text="/img first",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        message_id=100,
    )
    first.effective_message.reply_photo.return_value = SimpleNamespace(
        message_id=150
    )
    second = _update(
        text="/img second",
        chat_id=-1001,
        chat_type=ChatType.SUPERGROUP,
        message_id=200,
    )
    second.effective_message.reply_photo.return_value = SimpleNamespace(
        message_id=250
    )
    follow_up = _update(
        text="continue the first image",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        message_id=300,
        reply_to_message_id=150,
    )

    async with Database(tmp_path / "group-img.sqlite3") as database:
        registry = ChatSessionRegistry(  # type: ignore[arg-type]
            service,
            database,
            group_model="configured-group-model",
            group_thread_retention_days=30,
            group_thread_max_per_chat=100,
        )
        handlers._sessions = registry
        await registry.set_model(-1001, "saved-private-model")
        await registry.set_gem(-1001, "saved-private-gem")
        await registry.set_temporary(-1001, True)
        await registry.set_extended_thinking(-1001, True)
        await registry.set_language(-1001, LANGUAGE_CHINESE)

        await handlers.img(first, SimpleNamespace(args=["first"]))
        await handlers.img(second, SimpleNamespace(args=["second"]))
        await handlers.text_message(follow_up, _bot_context())

    assert client.resolve_model.call_args_list == [
        call("configured-group-model"),
        call("configured-group-model"),
        call("configured-group-model"),
    ]
    assert client.start_chat.call_args_list == [
        call(metadata=None, cid="", model=resolved_group_model, gem=None),
        call(metadata=None, cid="", model=resolved_group_model, gem=None),
        call(
            metadata=["first-image", None],
            cid="first-image-cid",
            model=resolved_group_model,
            gem=None,
        ),
    ]
    assert first_session.calls == [
        (
            f"{IMAGE_GENERATION_PREFIX}\n\nfirst",
            {"temporary": False, "extended_thinking": False},
        )
    ]
    assert second_session.calls == [
        (
            f"{IMAGE_GENERATION_PREFIX}\n\nsecond",
            {"temporary": False, "extended_thinking": False},
        )
    ]
    assert continued_session.calls == [
        (
            "continue the first image",
            {"temporary": False, "extended_thinking": False},
        )
    ]
    assert first.effective_message.reply_photo.await_args.kwargs["do_quote"] is True
    assert second.effective_message.reply_photo.await_args.kwargs["do_quote"] is True
    assert follow_up.effective_message.reply_text.await_args.kwargs == {
        "do_quote": True
    }


async def test_group_reply_restores_and_extends_the_referenced_thread(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """A reply authored by this bot must still extend its referenced thread."""

    output = SimpleNamespace(text="answer", text_delta="answer", images=())
    first_session = _StreamingSession([output])
    continued_session = _StreamingSession([output])
    registry.start_group_session.side_effect = [
        (first_session, False),
        (continued_session, True),
    ]
    handlers, _ = handlers_factory()
    first = _update(
        text="/gemini begin",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        message_id=300,
    )
    follow_up = _update(
        text="continue",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        message_id=400,
        reply_to_message_id=301,
    )

    await handlers.gemini(first, SimpleNamespace(args=["begin"]))
    await handlers.text_message(follow_up, _bot_context())

    assert registry.start_group_session.await_args_list == [
        call(-1001, None),
        call(-1001, 301),
    ]
    registry.persist_group_session.assert_has_awaits(
        [
            call(-1001, 301, first_session),
            call(-1001, 401, continued_session),
        ]
    )
    assert continued_session.calls == [
        ("continue", {"temporary": False, "extended_thinking": False})
    ]
    assert follow_up.effective_message.reply_text.await_args.kwargs == {
        "do_quote": True
    }


async def test_unknown_group_reply_starts_a_new_conversation_without_notice(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """Replies to untracked old messages should remain useful after cleanup."""

    output = SimpleNamespace(text="new answer", text_delta="new answer", images=())
    session = _StreamingSession([output])
    registry.start_group_session.return_value = (session, False)
    handlers, _ = handlers_factory()
    update = _update(
        text="old thread follow-up",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        reply_to_message_id=999,
    )

    await handlers.text_message(update, _bot_context())

    registry.start_group_session.assert_awaited_once_with(-1001, 999)
    assert [
        call_.args[0]
        for call_ in update.effective_message.reply_text.await_args_list
    ] == [PLACEHOLDER_TEXT]
    registry.persist_group_session.assert_awaited_once_with(-1001, 302, session)


async def test_failed_group_restore_notifies_then_retries_fresh(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """Expired upstream context must degrade to a new answer, not an error."""

    class FailingSession:
        def send_message_stream(self, prompt: str, **kwargs: Any) -> Any:
            del prompt, kwargs

            async def fail() -> Any:
                raise RuntimeError("synthetic expired thread")
                yield

            return fail()

    output = SimpleNamespace(text="recovered", text_delta="recovered", images=())
    fresh_session = _StreamingSession([output])
    registry.start_group_session.side_effect = [
        (FailingSession(), True),
        (fresh_session, False),
    ]
    failed_placeholder = SimpleNamespace(
        message_id=501,
        edit_text=AsyncMock(),
        delete=AsyncMock(),
    )
    notice = SimpleNamespace(message_id=502)
    fresh_placeholder = SimpleNamespace(
        message_id=503,
        edit_text=AsyncMock(),
        delete=AsyncMock(),
    )
    handlers, _ = handlers_factory()
    update = _update(
        text="continue",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        reply_to_message_id=450,
    )
    update.effective_message.reply_text.side_effect = [
        failed_placeholder,
        notice,
        fresh_placeholder,
    ]

    await handlers.text_message(update, _bot_context())

    assert registry.start_group_session.await_args_list == [
        call(-1001, 450),
        call(-1001),
    ]
    failed_placeholder.delete.assert_awaited_once_with()
    assert update.effective_message.reply_text.await_args_list[1] == call(
        translate("gemini.thread_expired", LANGUAGE_ENGLISH),
        do_quote=True,
    )
    registry.persist_group_session.assert_awaited_once_with(
        -1001,
        503,
        fresh_session,
    )


@pytest.mark.parametrize(
    ("reply_to_user_id", "reply_to_user_is_bot"),
    [(202, False), (BOT_ID + 1, True)],
    ids=["other-user", "other-bot"],
)
async def test_group_text_reply_to_another_sender_is_completely_ignored(
    handlers_factory,
    registry: AsyncMock,
    reply_to_user_id: int,
    reply_to_user_is_bot: bool,
) -> None:
    """Admin-mode visibility must not make the bot interrupt people or bots."""

    usage_dao = AsyncMock(spec=UsageLogDAO)
    handlers, service = handlers_factory(usage_dao=usage_dao)
    update = _update(
        text="not addressed to this bot",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        reply_to_message_id=700,
        reply_to_user_id=reply_to_user_id,
        reply_to_user_is_bot=reply_to_user_is_bot,
    )

    await handlers.text_message(update, _bot_context())

    registry.start_group_session.assert_not_awaited()
    service.execute.assert_not_awaited()
    usage_dao.add.assert_not_awaited()
    update.effective_message.reply_text.assert_not_awaited()


async def test_unthreaded_group_text_is_ignored(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """Privacy-mode semantics expose only explicit commands and bot replies."""

    usage_dao = AsyncMock(spec=UsageLogDAO)
    handlers, service = handlers_factory(usage_dao=usage_dao)
    update = _update(
        text="ambient group message",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
    )

    await handlers.text_message(update, SimpleNamespace())

    registry.start_group_session.assert_not_awaited()
    service.execute.assert_not_awaited()
    usage_dao.add.assert_not_awaited()
    update.effective_message.reply_text.assert_not_awaited()


async def test_private_gemini_starts_fresh_with_private_settings(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """The new command must not replace normal private chat preferences."""

    output = SimpleNamespace(text="answer", text_delta="answer", images=())
    first_session = _StreamingSession([output])
    second_session = _StreamingSession([output])
    registry.get_state.return_value = _state(
        model="private-model",
        temporary=True,
        extended_thinking=True,
        language=LANGUAGE_CHINESE,
    )
    registry.start_new.side_effect = [first_session, second_session]
    handlers, _ = handlers_factory()

    await handlers.gemini(
        _update(text="/gemini first"),
        SimpleNamespace(args=["first"]),
    )
    await handlers.gemini(
        _update(text="/gemini second"),
        SimpleNamespace(args=["second"]),
    )

    assert registry.start_new.await_args_list == [call(202), call(202)]
    assert first_session.calls == [
        ("first", {"temporary": True, "extended_thinking": True})
    ]
    assert second_session.calls == [
        ("second", {"temporary": True, "extended_thinking": True})
    ]
    assert registry.persist.await_args_list == [
        call(202, first_session),
        call(202, second_session),
    ]
    registry.start_group_session.assert_not_awaited()


async def test_group_media_reply_uses_fixed_settings_and_extends_thread(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """File follow-ups must not bypass the group's fixed privacy-safe setup."""

    upload = SimpleNamespace(prompt="analyze", files=["prepared-file"])
    prepared = MagicMock()
    prepared.__aenter__ = AsyncMock(return_value=upload)
    prepared.__aexit__ = AsyncMock(return_value=None)
    media_handler = MagicMock(spec=MediaHandler)
    media_handler.prepare_upload.return_value = prepared
    output = SimpleNamespace(text="analysis", images=())
    session = SimpleNamespace(
        send_message=AsyncMock(return_value=output),
        cid="media-cid",
        metadata=["media", None],
    )
    registry.start_group_session.return_value = (session, True)
    registry.get_state.return_value = _state(
        temporary=True,
        extended_thinking=True,
        language=LANGUAGE_CHINESE,
    )
    handlers, _ = handlers_factory(media_handler=media_handler)
    update = _update(
        text="caption",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        reply_to_message_id=600,
    )

    await handlers.media_message(update, _bot_context())

    registry.start_group_session.assert_awaited_once_with(-1001, 600)
    session.send_message.assert_awaited_once_with(
        "analyze",
        files=["prepared-file"],
        temporary=False,
        extended_thinking=False,
    )
    update.effective_message.reply_text.assert_awaited_once_with(
        "analysis",
        parse_mode=ParseMode.HTML,
        do_quote=True,
    )
    registry.persist_group_session.assert_awaited_once_with(-1001, 302, session)
    registry.get_state.assert_not_awaited()


@pytest.mark.parametrize(
    ("reply_to_user_id", "reply_to_user_is_bot"),
    [(202, False), (BOT_ID + 1, True)],
    ids=["other-user", "other-bot"],
)
async def test_group_media_reply_to_another_sender_is_completely_ignored(
    handlers_factory,
    registry: AsyncMock,
    reply_to_user_id: int,
    reply_to_user_is_bot: bool,
) -> None:
    """Media must use the same exact-bot reply boundary as plain group text."""

    usage_dao = AsyncMock(spec=UsageLogDAO)
    media_handler = MagicMock(spec=MediaHandler)
    handlers, service = handlers_factory(
        usage_dao=usage_dao,
        media_handler=media_handler,
    )
    update = _update(
        text="caption",
        chat_id=-1001,
        chat_type=ChatType.SUPERGROUP,
        reply_to_message_id=800,
        reply_to_user_id=reply_to_user_id,
        reply_to_user_is_bot=reply_to_user_is_bot,
    )
    update.effective_message.document = SimpleNamespace(file_id="document")

    await handlers.media_message(update, _bot_context())

    media_handler.prepare_upload.assert_not_called()
    registry.start_group_session.assert_not_awaited()
    service.execute.assert_not_awaited()
    usage_dao.add.assert_not_awaited()
    update.effective_message.reply_text.assert_not_awaited()


async def test_group_gate_stops_even_an_unregistered_command(
    handlers_factory,
) -> None:
    """A deny-by-default gate keeps future commands private until opted in."""

    handlers, _ = handlers_factory()
    update = _update(text="/future_command", chat_type=ChatType.GROUP)

    with pytest.raises(ApplicationHandlerStop):
        await handlers.command_gate(update, SimpleNamespace())

    update.effective_message.reply_text.assert_awaited_once_with(
        translate("command.private_only", LANGUAGE_ENGLISH)
    )


@pytest.mark.parametrize("command", ["allow_chat", "deny_chat", "access"])
async def test_group_gate_stops_chat_access_admin_commands(
    handlers_factory,
    command: str,
) -> None:
    """Group approval commands must remain behind T9.1's private-only gate."""

    handlers, _ = handlers_factory()
    update = _update(
        text=f"/{command} -1001",
        chat_id=-1002,
        chat_type=ChatType.SUPERGROUP,
    )

    with pytest.raises(ApplicationHandlerStop):
        await handlers.command_gate(update, SimpleNamespace())

    update.effective_message.reply_text.assert_awaited_once_with(
        translate("command.private_only", LANGUAGE_ENGLISH)
    )


@pytest.mark.parametrize("language", [LANGUAGE_ENGLISH, LANGUAGE_CHINESE])
async def test_help_is_generated_for_the_stored_chat_language(
    handlers_factory,
    registry: AsyncMock,
    language: str,
) -> None:
    """Help must match the same per-chat choice used by ordinary replies."""

    handlers, _ = handlers_factory()
    registry.get_state.return_value = _state(language=language)
    update = _update(text="/help")

    await handlers.help(update, SimpleNamespace())

    help_text = update.effective_message.reply_text.await_args.args[0]
    assert help_text.startswith(translate("help.heading", language))
    assert (
        f"/language — {translate('command.language.description', language)}"
        in help_text
    )
    assert help_text.endswith(translate("help.footer", language))


@pytest.mark.parametrize(
    ("language", "telegram_code"),
    [
        (LANGUAGE_ENGLISH, "zh-tw"),
        (LANGUAGE_CHINESE, "en"),
    ],
)
async def test_new_uses_the_stored_chat_language(
    handlers_factory,
    registry: AsyncMock,
    language: str,
    telegram_code: str,
) -> None:
    """A persisted choice must localize /new even if Telegram disagrees."""

    handlers, _ = handlers_factory()
    registry.get_state.return_value = _state(language=language)
    update = _update(text="/new", language_code=telegram_code)

    await handlers.new(update, SimpleNamespace())

    update.effective_message.reply_text.assert_awaited_once_with(
        translate("new.started", language)
    )


async def test_language_shows_both_choices_in_the_resolved_language(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """The language picker must remain usable before a preference is stored."""

    handlers, _ = handlers_factory()
    registry.get_state.return_value = _state(language=None)
    update = _update(text="/language", language_code="zh-Hant-TW")

    await handlers.language(update, SimpleNamespace())

    reply = update.effective_message.reply_text.await_args
    assert reply.args[0] == translate("lang.choose", LANGUAGE_CHINESE)
    keyboard = reply.kwargs["reply_markup"].inline_keyboard
    assert [row[0].text for row in keyboard] == ["English", "正體中文"]
    assert [row[0].callback_data for row in keyboard] == [
        "lang:en",
        "lang:zh-hant",
    ]


@pytest.mark.parametrize(
    ("language", "label"),
    [
        (LANGUAGE_ENGLISH, "English"),
        (LANGUAGE_CHINESE, "正體中文"),
    ],
)
async def test_language_callback_persists_selection_and_chat_menu(
    handlers_factory,
    registry: AsyncMock,
    language: str,
    label: str,
) -> None:
    """A chat-scoped menu must override Telegram's unrelated app locale."""

    handlers, _ = handlers_factory()
    update = _callback_update(f"lang:{language}")
    bot = SimpleNamespace(set_my_commands=AsyncMock())

    await handlers.callback(update, SimpleNamespace(bot=bot))

    registry.set_language.assert_awaited_once_with(202, language)
    menu_call = bot.set_my_commands.await_args
    assert {item.command for item in menu_call.args[0]} == {
        item.command for item in PUBLIC_BOT_COMMANDS
    }
    assert next(
        item.description
        for item in menu_call.args[0]
        if item.command == "language"
    ) == translate("command.language.description", language)
    assert isinstance(menu_call.kwargs["scope"], BotCommandScopeChat)
    assert menu_call.kwargs["scope"].chat_id == 202
    update.callback_query.edit_message_text.assert_awaited_once_with(
        translate("lang.selected", language, language=label)
    )


async def test_language_callback_keeps_selection_when_chat_menu_fails(
    handlers_factory,
    registry: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An optional Telegram menu failure must not roll back the chat choice."""

    handlers, _ = handlers_factory()
    update = _callback_update(f"lang:{LANGUAGE_CHINESE}")
    bot = SimpleNamespace(
        set_my_commands=AsyncMock(side_effect=RuntimeError("offline"))
    )

    await handlers.callback(update, SimpleNamespace(bot=bot))

    registry.set_language.assert_awaited_once_with(202, LANGUAGE_CHINESE)
    update.callback_query.edit_message_text.assert_awaited_once_with(
        translate(
            "lang.selected",
            LANGUAGE_CHINESE,
            language=translate("language.chinese", LANGUAGE_CHINESE),
        )
    )
    assert "Unable to register Telegram chat command menu" in caplog.text


async def test_new_retries_bounded_flood_control_and_succeeds(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """Localization must preserve the bounded retry behavior of /new."""

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
        call(translate("new.started", LANGUAGE_ENGLISH)),
        call(translate("new.started", LANGUAGE_ENGLISH)),
    ]
    sleep.assert_awaited_once_with(3.0)


async def test_new_reports_busy_when_flood_wait_exceeds_limit(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """Localized reset confirmation must not bypass the flood-wait ceiling."""

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
        call(translate("new.started", LANGUAGE_ENGLISH)),
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
        translate(
            "research.submitted",
            LANGUAGE_ENGLISH,
            task_id="research-task-123",
        )
    )


async def test_research_rejects_an_empty_topic(handlers_factory) -> None:
    research = AsyncMock()
    handlers, _ = handlers_factory(research=research)
    update = _update(text="/research")

    await handlers.research(update, SimpleNamespace(args=[]))

    research.submit.assert_not_awaited()
    update.effective_message.reply_text.assert_awaited_once_with(
        translate("research.usage", LANGUAGE_ENGLISH)
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
        "\n".join(
            (
                translate("research.status_heading", LANGUAGE_ENGLISH),
                translate(
                    "research.status_line",
                    LANGUAGE_ENGLISH,
                    task_id="research-task-123",
                    status="running",
                ),
                translate(
                    "research.status_line",
                    LANGUAGE_ENGLISH,
                    task_id="research-task-456",
                    status="done",
                ),
            )
        )
    )


async def test_img_explicitly_requests_generation_and_uses_media_handler(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """Private image generation must retain each chat's chosen preferences."""

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
    session = _StreamingSession([output])
    media_handler = MagicMock(spec=MediaHandler)
    registry.get_state.return_value = _state(
        model="private-image-model",
        gem_id="private-image-gem",
        temporary=True,
        extended_thinking=True,
        language=LANGUAGE_CHINESE,
    )
    registry.get_or_create.return_value = session
    handlers, _ = handlers_factory(media_handler=media_handler)
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

    assert session.calls == [
        (
            f"{IMAGE_GENERATION_PREFIX}\n\n台北 101 的水彩畫",
            {"temporary": True, "extended_thinking": True},
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
    assert registry.get_state.await_args_list == [call(202), call(202)]
    registry.get_or_create.assert_awaited_once_with(202)
    registry.start_group_session.assert_not_awaited()
    registry.set_model.assert_not_awaited()
    registry.set_gem.assert_not_awaited()
    registry.persist.assert_awaited_once_with(202, session)


async def test_img_without_prompt_replies_with_usage(handlers_factory) -> None:
    media_handler = MagicMock(spec=MediaHandler)
    handlers, service = handlers_factory(media_handler=media_handler)
    update = _update(text="/img")

    await handlers.img(update, SimpleNamespace(args=[]))

    update.effective_message.reply_text.assert_awaited_once_with(IMAGE_USAGE)
    service.execute.assert_not_awaited()
    media_handler.send_output_images.assert_not_awaited()


def _text_session() -> _StreamingSession:
    output = SimpleNamespace(text="answer", text_delta="answer", images=())
    return _StreamingSession([output])


def _sent_texts(update: SimpleNamespace) -> list[Any]:
    return [
        awaited.args[0]
        for awaited in update.effective_message.reply_text.await_args_list
        if awaited.args
    ]


async def test_group_gemini_reply_carries_the_quoted_message_as_context(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """A bare "why" is meaningless unless the quoted claim travels with it;
    without the quote Gemini answers the question in the abstract."""

    session = _text_session()
    registry.start_group_session.return_value = (session, False)
    handlers, _ = handlers_factory()
    update = _update(
        text="/gemini why",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        reply_to_message_id=700,
        reply_to_user_id=555,
        reply_to_user_is_bot=False,
        reply_to_sender_name="Alice",
        reply_to_text="The build only fails on ARM runners.",
    )

    await handlers.gemini(update, SimpleNamespace(args=["why"]))

    assert session.calls == [
        (
            "Quoted message from Alice:\n"
            "The build only fails on ARM runners.\n"
            "\n"
            f"{QUOTED_QUESTION_LABEL} why",
            {"temporary": False, "extended_thinking": False},
        )
    ]


async def test_img_reply_carries_the_quoted_message_as_context(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """Image prompts are refined by quoting a description, so /img must see
    the quoted text as well as the refinement the user typed."""

    session = _text_session()
    registry.get_or_create.return_value = session
    handlers, _ = handlers_factory()
    update = _update(
        text="/img in the same style",
        reply_to_message_id=700,
        reply_to_user_id=555,
        reply_to_user_is_bot=False,
        reply_to_sender_name="Bob",
        reply_to_text="A neon skyline at dusk.",
    )

    await handlers.img(
        update,
        SimpleNamespace(args=["in", "the", "same", "style"]),
    )

    assert session.calls == [
        (
            f"{IMAGE_GENERATION_PREFIX}\n\n"
            "Quoted message from Bob:\n"
            "A neon skyline at dusk.\n"
            "\n"
            f"{QUOTED_REQUEST_LABEL} in the same style",
            {"temporary": False, "extended_thinking": False},
        )
    ]


async def test_gemini_quoting_own_answer_still_opens_a_new_conversation(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """/gemini always means "start over"; quoting our own answer may only
    supply context, never silently reopen the thread it came from."""

    session = _text_session()
    registry.start_group_session.return_value = (session, False)
    handlers, _ = handlers_factory()
    update = _update(
        text="/gemini why",
        chat_id=-1001,
        chat_type=ChatType.SUPERGROUP,
        reply_to_message_id=700,
        reply_to_user_id=BOT_ID,
        reply_to_sender_name="Gemini Bot",
        reply_to_text="Because the cache starts cold.",
    )

    await handlers.gemini(update, SimpleNamespace(args=["why"]))

    registry.start_group_session.assert_awaited_once_with(-1001, None)
    assert session.calls == [
        (
            "Quoted message from Gemini Bot:\n"
            "Because the cache starts cold.\n"
            "\n"
            f"{QUOTED_QUESTION_LABEL} why",
            {"temporary": False, "extended_thinking": False},
        )
    ]


async def test_gemini_reply_to_a_caption_uses_the_caption(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """Photos and documents carry their words in the caption; ignoring it
    would make every reply to an uploaded file lose its context."""

    session = _text_session()
    registry.start_group_session.return_value = (session, False)
    handlers, _ = handlers_factory()
    update = _update(
        text="/gemini summarise this",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        reply_to_message_id=700,
        reply_to_user_id=555,
        reply_to_user_is_bot=False,
        reply_to_caption="Quarterly egress report, September.",
    )

    await handlers.gemini(
        update,
        SimpleNamespace(args=["summarise", "this"]),
    )

    assert session.calls == [
        (
            "Quoted message:\n"
            "Quarterly egress report, September.\n"
            "\n"
            f"{QUOTED_QUESTION_LABEL} summarise this",
            {"temporary": False, "extended_thinking": False},
        )
    ]


async def test_gemini_reply_without_text_or_caption_sends_only_the_question(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """Stickers and voice notes have nothing readable to quote, so the
    scaffolding must not wrap the question around an empty block."""

    session = _text_session()
    registry.start_group_session.return_value = (session, False)
    handlers, _ = handlers_factory()
    update = _update(
        text="/gemini why",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        reply_to_message_id=700,
        reply_to_user_id=555,
        reply_to_user_is_bot=False,
        reply_to_sender_name="Alice",
    )

    await handlers.gemini(update, SimpleNamespace(args=["why"]))

    assert session.calls == [
        ("why", {"temporary": False, "extended_thinking": False})
    ]


async def test_long_quoted_message_is_truncated_before_reaching_upstream(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """One pasted wall of text must not crowd out the user's own question or
    blow past the upstream prompt budget."""

    session = _text_session()
    registry.start_group_session.return_value = (session, False)
    handlers, _ = handlers_factory()
    update = _update(
        text="/gemini why",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        reply_to_message_id=700,
        reply_to_user_id=555,
        reply_to_user_is_bot=False,
        reply_to_sender_name="Alice",
        reply_to_text="y" * (QUOTED_CONTEXT_MAX_CHARS + 500),
    )

    await handlers.gemini(update, SimpleNamespace(args=["why"]))

    sent_prompt = session.calls[0][0]
    assert (
        "y" * QUOTED_CONTEXT_MAX_CHARS + QUOTED_CONTEXT_TRUNCATION_SUFFIX
    ) in sent_prompt
    assert "y" * (QUOTED_CONTEXT_MAX_CHARS + 1) not in sent_prompt
    assert len(sent_prompt) <= (
        QUOTED_CONTEXT_MAX_CHARS + len(QUOTED_CONTEXT_TRUNCATION_SUFFIX) + 100
    )


async def test_gemini_and_img_without_a_reply_send_the_bare_prompt(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """The overwhelmingly common case is an unquoted command; it must reach
    Gemini byte-for-byte as it did before quoting existed."""

    gemini_session = _text_session()
    img_session = _text_session()
    registry.start_new.return_value = gemini_session
    registry.get_or_create.return_value = img_session
    handlers, _ = handlers_factory()

    await handlers.gemini(
        _update(text="/gemini why"),
        SimpleNamespace(args=["why"]),
    )
    await handlers.img(
        _update(text="/img a red bicycle"),
        SimpleNamespace(args=["a", "red", "bicycle"]),
    )

    assert gemini_session.calls == [
        ("why", {"temporary": False, "extended_thinking": False})
    ]
    assert img_session.calls == [
        (
            f"{IMAGE_GENERATION_PREFIX}\n\na red bicycle",
            {"temporary": False, "extended_thinking": False},
        )
    ]


async def test_bare_commands_with_a_quote_treat_the_quote_as_the_request(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """Replying with a bare /gemini clearly means "deal with this"; answering
    with usage text there would be pedantic and useless."""

    gemini_session = _text_session()
    img_session = _text_session()
    registry.start_new.return_value = gemini_session
    registry.get_or_create.return_value = img_session
    handlers, _ = handlers_factory()
    quoted = dict(
        reply_to_message_id=700,
        reply_to_user_id=555,
        reply_to_user_is_bot=False,
        reply_to_sender_name="Cara",
        reply_to_text="Egress stayed under one gigabyte all month.",
    )
    gemini_update = _update(text="/gemini", **quoted)
    img_update = _update(text="/img", **quoted)

    await handlers.gemini(gemini_update, SimpleNamespace(args=[]))
    await handlers.img(img_update, SimpleNamespace(args=[]))

    assert gemini_session.calls == [
        (
            "Quoted message from Cara:\n"
            "Egress stayed under one gigabyte all month.\n"
            "\n"
            f"{QUOTED_QUESTION_LABEL} {QUOTED_DEFAULT_QUESTION}",
            {"temporary": False, "extended_thinking": False},
        )
    ]
    assert img_session.calls == [
        (
            f"{IMAGE_GENERATION_PREFIX}\n\n"
            "Quoted message from Cara:\n"
            "Egress stayed under one gigabyte all month.\n"
            "\n"
            f"{QUOTED_REQUEST_LABEL} {QUOTED_DEFAULT_IMAGE_REQUEST}",
            {"temporary": False, "extended_thinking": False},
        )
    ]
    assert translate("gemini.usage", LANGUAGE_ENGLISH) not in _sent_texts(
        gemini_update
    )
    assert IMAGE_USAGE not in _sent_texts(img_update)


async def test_bare_commands_without_a_quote_still_explain_their_usage(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """With neither a quote nor arguments there is nothing to work with, so
    the help text remains the only sensible answer."""

    handlers, service = handlers_factory()

    gemini_update = _update(text="/gemini")
    img_update = _update(text="/img")
    await handlers.gemini(gemini_update, SimpleNamespace(args=[]))
    await handlers.img(img_update, SimpleNamespace(args=[]))

    assert _sent_texts(gemini_update) == [
        translate("gemini.usage", LANGUAGE_ENGLISH)
    ]
    assert _sent_texts(img_update) == [IMAGE_USAGE]
    service.execute.assert_not_awaited()
    registry.start_new.assert_not_awaited()
    registry.get_or_create.assert_not_awaited()


@pytest.mark.parametrize(
    "language",
    [LANGUAGE_ENGLISH, LANGUAGE_CHINESE],
    ids=["english", "chinese"],
)
async def test_quoting_scaffolding_stays_english_in_every_interface_language(
    handlers_factory,
    registry: AsyncMock,
    language: str,
) -> None:
    """The scaffolding is instruction text for the model, not interface copy;
    letting it follow the chat language would make behaviour drift."""

    session = _text_session()
    registry.get_state.return_value = _state(language=language)
    registry.start_new.return_value = session
    handlers, _ = handlers_factory()
    update = _update(
        text="/gemini why",
        reply_to_message_id=700,
        reply_to_user_id=555,
        reply_to_user_is_bot=False,
        reply_to_sender_name="Alice",
        reply_to_text="The build only fails on ARM runners.",
    )

    await handlers.gemini(update, SimpleNamespace(args=["why"]))

    assert session.calls == [
        (
            "Quoted message from Alice:\n"
            "The build only fails on ARM runners.\n"
            "\n"
            f"{QUOTED_QUESTION_LABEL} why",
            {"temporary": False, "extended_thinking": False},
        )
    ]


async def test_group_text_reply_to_another_sender_stays_ignored_when_quotable(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """Quoting context is opt-in through a command; plain group chatter that
    happens to quote someone must still never wake the bot up."""

    usage_dao = AsyncMock(spec=UsageLogDAO)
    handlers, service = handlers_factory(usage_dao=usage_dao)
    update = _update(
        text="agreed, that is odd",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        reply_to_message_id=700,
        reply_to_user_id=555,
        reply_to_user_is_bot=False,
        reply_to_sender_name="Alice",
        reply_to_text="The build only fails on ARM runners.",
    )

    await handlers.text_message(update, _bot_context())

    registry.start_group_session.assert_not_awaited()
    service.execute.assert_not_awaited()
    usage_dao.add.assert_not_awaited()
    update.effective_message.reply_text.assert_not_awaited()


async def test_group_text_reply_to_this_bot_continues_without_scaffolding(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """Replying to our own answer is an ongoing conversation: the thread is
    resumed and the message travels verbatim, with no quoting scaffolding."""

    session = _text_session()
    registry.start_group_session.return_value = (session, True)
    handlers, _ = handlers_factory()
    update = _update(
        text="and what about the cache?",
        chat_id=-1001,
        chat_type=ChatType.GROUP,
        reply_to_message_id=700,
        reply_to_user_id=BOT_ID,
        reply_to_sender_name="Gemini Bot",
        reply_to_text="Because the cache starts cold.",
    )

    await handlers.text_message(update, _bot_context())

    registry.start_group_session.assert_awaited_once_with(-1001, 700)
    assert session.calls == [
        (
            "and what about the cache?",
            {"temporary": False, "extended_thinking": False},
        )
    ]

async def test_real_generated_image_fixture_deletes_placeholder_before_media(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "image-generated.json"
    output = ModelOutput.model_validate_json(fixture_path.read_text(encoding="utf-8"))
    media_handler = MagicMock(spec=MediaHandler)
    session = _StreamingSession([output])
    registry.get_or_create.return_value = session
    handlers, _ = handlers_factory(media_handler=media_handler)
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
    """Exact group reply checks must leave ordinary private text unchanged."""

    output = SimpleNamespace(
        text="**hello**\n\nworld",
        text_delta="**hello**\n\nworld",
        images=(),
    )
    session = _StreamingSession([output])
    registry.get_state.return_value = _state(temporary=True)
    registry.get_or_create.return_value = session
    usage_dao = AsyncMock(spec=UsageLogDAO)
    handlers, service = handlers_factory(usage_dao=usage_dao)
    update = _update(text="question")

    await handlers.text_message(update, SimpleNamespace())

    registry.get_or_create.assert_awaited_once_with(202)
    service.execute.assert_awaited_once()
    assert session.calls == [
        (
            "question",
            {"temporary": True, "extended_thinking": False},
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


@pytest.mark.parametrize("language", [LANGUAGE_ENGLISH, LANGUAGE_CHINESE])
async def test_generic_failure_uses_the_stored_chat_language(
    handlers_factory,
    registry: AsyncMock,
    language: str,
) -> None:
    """Unexpected failures must remain actionable in the user's chosen UI."""

    handlers, service = handlers_factory()
    registry.get_state.return_value = _state(language=language)
    service.execute.side_effect = RuntimeError("synthetic failure")
    update = _update(text="question")

    await handlers.text_message(update, SimpleNamespace())

    update.effective_message.reply_text.assert_awaited_once_with(
        translate("generic.failure", language)
    )


async def test_unformatted_stream_skips_final_edit_and_records_success(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    text = "好" * EDIT_CHARACTER_THRESHOLD
    output = SimpleNamespace(
        text=text,
        text_delta=text,
        images=(),
    )
    session = _StreamingSession([output])
    registry.get_or_create.return_value = session
    usage_dao = AsyncMock(spec=UsageLogDAO)
    handlers, _ = handlers_factory(usage_dao=usage_dao)
    update = _update(text="question")
    update.effective_message.placeholder.edit_text.side_effect = [
        None,
        BadRequest("message is not modified"),
    ]

    await handlers.text_message(update, SimpleNamespace())

    update.effective_message.placeholder.edit_text.assert_awaited_once_with(
        text,
        parse_mode=None,
    )
    registry.persist.assert_awaited_once_with(202, session)
    saved = usage_dao.add.await_args.args[0]
    assert saved.ok is True
    assert saved.error_kind is None


async def test_text_reports_bounded_flood_control_and_releases_slot(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    queue = RequestQueue(max_concurrency=1, user_rate_limit_per_min=10)
    usage_dao = AsyncMock(spec=UsageLogDAO)
    registry.get_or_create.return_value = SimpleNamespace()
    handlers, _ = handlers_factory(usage_dao=usage_dao, request_queue=queue)
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
        translate("service.unavailable.auth", LANGUAGE_ENGLISH)
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
    assert "Cookie update failed" in reply
    assert AccountStatus.UNAUTHENTICATED.description in reply
    assert translate("admin.cookie_updated", LANGUAGE_ENGLISH) not in reply


async def test_setcookie_success_persists_private_runtime_override(
    handlers_factory,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cookie_path = tmp_path / "cookies"
    handlers, service = handlers_factory(cookie_path=cookie_path)
    service.reinit = AsyncMock()
    handlers.bind_auth(SimpleNamespace(is_admin=lambda user_id: user_id == 101))
    handlers._awaiting_cookie_users.add(101)
    replacement_1psid = "FAKE_REPLACEMENT_1PSID_FOR_TEST"
    replacement_1psidts = "FAKE_REPLACEMENT_1PSIDTS_FOR_TEST"
    update = _update(
        text=(
            f"__Secure-1PSID={replacement_1psid}\n"
            f"__Secure-1PSIDTS={replacement_1psidts}"
        )
    )

    await handlers.setcookie_value(update, SimpleNamespace())

    service.reinit.assert_awaited_once_with(
        secure_1psid=replacement_1psid,
        secure_1psidts=replacement_1psidts,
    )
    override_path = cookie_path / RUNTIME_CREDENTIALS_FILENAME
    assert override_path.stat().st_mode & 0o777 == 0o600
    override_text = override_path.read_text(encoding="utf-8")
    assert replacement_1psid not in override_text
    assert replacement_1psidts not in override_text
    assert replacement_1psid not in caplog.text
    assert replacement_1psidts not in caplog.text
    update.effective_message.reply_text.assert_awaited_once_with(
        translate("admin.cookie_updated", LANGUAGE_ENGLISH)
    )


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
    session = _StreamingSession([output])
    registry.get_state.return_value = _state(temporary=False)
    registry.get_or_create.return_value = session
    handlers, _ = handlers_factory()
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
    session = _StreamingSession([output])
    registry.get_or_create.return_value = session
    handlers, _ = handlers_factory()
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
    session = _StreamingSession([output])
    registry.get_or_create.return_value = session
    handlers, _ = handlers_factory()
    update = _update(text="generate")

    await handlers.text_message(update, SimpleNamespace())

    update.effective_message.placeholder.edit_text.assert_awaited_once_with(
        text,
        parse_mode=None,
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
    """The Chinese status view must keep every operational field secret-free."""

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
    registry.get_state.return_value = _state(language=LANGUAGE_CHINESE)
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
    registry: AsyncMock,
) -> None:
    """A missing cache file needs a localized value instead of a blank field."""

    handlers, _ = handlers_factory()
    registry.get_state.return_value = _state(language=LANGUAGE_CHINESE)
    update = _update()

    await handlers.status(update, SimpleNamespace())

    assert "Cookie 最後刷新時間：尚未刷新" in (
        update.effective_message.reply_text.await_args.args[0]
    )


@pytest.mark.parametrize(
    ("language", "labels"),
    [
        (
            LANGUAGE_ENGLISH,
            (
                "Current model:",
                "Session CID:",
                "Temporary mode:",
                "Extended Thinking:",
                "Service status:",
                "Account status:",
                "Last cookie refresh:",
                "Queue depth:",
                "Today's usage:",
                "Estimated monthly egress:",
            ),
        ),
        (
            LANGUAGE_CHINESE,
            (
                "目前模型：",
                "Session CID：",
                "Temporary mode：",
                "Extended Thinking：",
                "服務狀態：",
                "Account status：",
                "Cookie 最後刷新時間：",
                "佇列深度：",
                "今日用量：",
                "本月累計 egress 估算值：",
            ),
        ),
    ],
)
async def test_status_uses_the_stored_chat_language_for_labels(
    handlers_factory,
    registry: AsyncMock,
    language: str,
    labels: tuple[str, ...],
) -> None:
    """Status labels must follow the chat choice, not Telegram profile hints."""

    handlers, _ = handlers_factory()
    registry.get_state.return_value = _state(language=language)
    update = _update(language_code=("zh-tw" if language == "en" else "en"))

    await handlers.status(update, SimpleNamespace())

    report = update.effective_message.reply_text.await_args.args[0]
    assert all(label in report for label in labels)


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
        "Account status: LOCATION_REJECTED — "
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
    """Only /language should reach the authorized public command surface."""

    handlers, _ = handlers_factory()
    application = SimpleNamespace(add_handler=MagicMock())
    auth = AsyncMock()

    register_handlers(application, auth=auth, handlers=handlers)

    calls = application.add_handler.call_args_list
    assert calls[0].kwargs == {"group": -1}
    assert calls[1].kwargs == {"group": 0}
    assert all(call_.kwargs == {"group": 1} for call_ in calls[2:15])
    assert len(calls) == 17
    registered_commands = {
        command
        for registered in calls
        for command in getattr(registered.args[0], "commands", ())
    }
    assert "language" in registered_commands
    assert {"start", "help"} <= registered_commands
    assert "lang" not in registered_commands
    assert {"img", "research", "research_status"} <= registered_commands
    assert {"allow_chat", "deny_chat", "access"} <= registered_commands


async def test_startup_registers_public_command_menu() -> None:
    """Both Telegram menus must hide /help while preserving the /start entry."""

    application = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=AsyncMock()),
        start=AsyncMock(),
    )

    await _start_application(application)

    menu_calls = application.bot.set_my_commands.await_args_list
    assert len(menu_calls) == 2
    assert menu_calls[0] == call(PUBLIC_BOT_COMMANDS)
    assert next(
        item.description
        for item in menu_calls[0].args[0]
        if item.command == "language"
    ) == translate("command.language.description", LANGUAGE_ENGLISH)
    registered_commands = {item.command for item in PUBLIC_BOT_COMMANDS}
    assert registered_commands == {
        "start",
        "gemini",
        "new",
        "model",
        "gem",
        "temp",
        "think",
        "language",
        "img",
        "research",
        "research_status",
        "status",
    }
    assert "help" not in registered_commands
    assert registered_commands.isdisjoint(
        {
            "setcookie",
            "allow",
            "deny",
            "allow_chat",
            "deny_chat",
            "access",
            "health",
        }
    )
    assert GROUP_COMMANDS == {"start", "help", "img", "gemini"}
    assert GROUP_MENU_COMMANDS == {"start", "img", "gemini"}
    assert {item.command for item in menu_calls[1].args[0]} == GROUP_MENU_COMMANDS
    assert "help" not in {item.command for item in menu_calls[1].args[0]}
    assert isinstance(
        menu_calls[1].kwargs["scope"],
        BotCommandScopeAllGroupChats,
    )
    application.start.assert_awaited_once_with()


@pytest.mark.parametrize("parse_mode", [None, ParseMode.HTML])
async def test_admin_notification_sender_preserves_the_requested_parse_mode(
    parse_mode: str | None,
) -> None:
    """HTML must be opt-in so safe group markup cannot break plain alerts."""

    notifications = AsyncMock(spec=AdminNotificationDAO)
    notifications.claim.return_value = True
    application = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
    )

    await _send_admin_notification(
        application,
        notifications,
        9001,
        "admin alert",
        parse_mode=parse_mode,
    )

    expected_kwargs = {"chat_id": 9001, "text": "admin alert"}
    if parse_mode is not None:
        expected_kwargs["parse_mode"] = parse_mode
    application.bot.send_message.assert_awaited_once_with(**expected_kwargs)


async def test_startup_never_registers_zh_hant_command_menu(caplog) -> None:
    """Avoiding Telegram's invalid zh-hant code prevents noisy startup errors."""

    async def reject_invalid_language_code(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("language_code") == LANGUAGE_CHINESE:
            raise BadRequest("language_code must be two letters")

    set_my_commands = AsyncMock(side_effect=reject_invalid_language_code)
    application = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=set_my_commands),
        start=AsyncMock(),
    )

    with caplog.at_level("WARNING"):
        await _start_application(application)

    assert set_my_commands.await_args_list[0] == call(PUBLIC_BOT_COMMANDS)
    assert set_my_commands.await_count == 2
    assert "BadRequest" not in caplog.text


async def test_startup_sequence_restores_state_before_polling() -> None:
    """Order only.  The service-level guarantee is covered in test_service."""

    service = SimpleNamespace(
        init=AsyncMock(),
        state=ServiceState.DEGRADED,
    )
    sessions = SimpleNamespace(restore_all=AsyncMock())
    research = SimpleNamespace(restore_running=AsyncMock())
    updater = SimpleNamespace(start_polling=AsyncMock())

    await _initialize_and_start_polling(
        service,
        sessions,
        research,
        updater,
    )

    service.init.assert_awaited_once_with()
    sessions.restore_all.assert_awaited_once_with()
    research.restore_running.assert_awaited_once_with()
    updater.start_polling.assert_awaited_once()


async def test_command_menu_failure_does_not_prevent_startup(caplog) -> None:
    """An optional default-menu outage must not prevent update processing."""

    application = SimpleNamespace(
        bot=SimpleNamespace(
            set_my_commands=AsyncMock(side_effect=RuntimeError("offline"))
        ),
        start=AsyncMock(),
    )

    with caplog.at_level("WARNING"):
        await _start_application(application)

    application.start.assert_awaited_once_with()
    assert application.bot.set_my_commands.await_count == 2
    assert "Unable to register default Telegram command menu (RuntimeError)" in (
        caplog.text
    )


async def test_group_command_menu_failure_only_logs_a_warning(caplog) -> None:
    """An optional group menu outage must never keep the bot from starting."""

    application = SimpleNamespace(
        bot=SimpleNamespace(
            set_my_commands=AsyncMock(
                side_effect=[None, RuntimeError("group menu offline")]
            )
        ),
        start=AsyncMock(),
    )

    with caplog.at_level("WARNING"):
        await _start_application(application)

    application.start.assert_awaited_once_with()
    assert "Unable to register group Telegram command menu (RuntimeError)" in (
        caplog.text
    )


def test_cookie_location_is_logged_as_an_absolute_path(
    tmp_path, monkeypatch, caplog
) -> None:
    """A relative cookie path must not be reported as written.

    It resolves against the working directory, so it can land somewhere other
    than the absolute path a service unit names -- and then an empty directory
    at the unit's path looks like a lost session rather than a misconfiguration.
    """

    from gemini_tg_bot.config import Settings

    (tmp_path / "data" / "cookies").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE_TOKEN_FOR_TEST")
    monkeypatch.setenv("ADMIN_USER_ID", "1")
    monkeypatch.setenv("GEMINI_SECURE_1PSID", "FAKE_1PSID_FOR_TEST")
    monkeypatch.setenv("GEMINI_SECURE_1PSIDTS", "FAKE_1PSIDTS_FOR_TEST")
    monkeypatch.setenv("GEMINI_COOKIE_PATH", "./data/cookies")

    with caplog.at_level("INFO"):
        _log_cookie_location(Settings(_env_file=None))

    assert str((tmp_path / "data" / "cookies").resolve()) in caplog.text
    assert "./data/cookies" not in caplog.text
    assert "override=False" in caplog.text
    assert "FAKE_1PSID_FOR_TEST" not in caplog.text
    assert "FAKE_1PSIDTS_FOR_TEST" not in caplog.text


@pytest.mark.parametrize("language", [LANGUAGE_ENGLISH, LANGUAGE_CHINESE])
async def test_think_toggles_from_registry_state(
    handlers_factory,
    registry: AsyncMock,
    language: str,
) -> None:
    """The persisted /think switch must report both states in the chat UI."""

    handlers, _ = handlers_factory()
    update = _update()
    registry.get_state.side_effect = [
        _state(extended_thinking=False, language=language),
        _state(extended_thinking=True, language=language),
    ]

    await handlers.think(update, SimpleNamespace())
    await handlers.think(update, SimpleNamespace())

    assert registry.set_extended_thinking.await_args_list == [
        call(202, True),
        call(202, False),
    ]
    replies = [
        c.args[0] for c in update.effective_message.reply_text.await_args_list
    ]
    assert replies == [
        translate("think.enabled", language),
        translate("think.disabled", language),
    ]


async def test_enabled_thinking_reaches_the_upstream_call(
    handlers_factory,
    registry: AsyncMock,
) -> None:
    """The persisted switch has to arrive as the upstream keyword argument."""

    session = _StreamingSession(
        [SimpleNamespace(text="ok", text_delta="ok", images=())]
    )
    registry.get_state.return_value = _state(extended_thinking=True)
    registry.get_or_create.return_value = session
    handlers, _ = handlers_factory()

    await handlers.text_message(_update(text="question"), SimpleNamespace())

    assert session.calls == [
        ("question", {"temporary": False, "extended_thinking": True})
    ]


async def test_enabled_thinking_logs_model_and_thought_character_count(
    handlers_factory,
    registry: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    thoughts = "SENSITIVE_THOUGHTS_SENTINEL"
    session = _StreamingSession(
        [
            SimpleNamespace(
                text="ok",
                text_delta="ok",
                images=(),
                thoughts=thoughts,
            )
        ]
    )
    registry.get_state.return_value = _state(
        model="thinking-capable-model",
        extended_thinking=True,
    )
    registry.get_or_create.return_value = session
    handlers, _ = handlers_factory()

    with caplog.at_level("INFO", logger="gemini_tg_bot.telegram.handlers"):
        await handlers.text_message(_update(text="question"), SimpleNamespace())

    assert "thinking-capable-model" in caplog.text
    assert f"received {len(thoughts)} thought characters" in caplog.text
    assert thoughts not in caplog.text


async def test_enabled_thinking_logs_when_no_thoughts_are_returned(
    handlers_factory,
    registry: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _StreamingSession(
        [SimpleNamespace(text="ok", text_delta="ok", images=(), thoughts=None)]
    )
    registry.get_state.return_value = _state(
        model=None,
        extended_thinking=True,
    )
    registry.get_or_create.return_value = session
    handlers, _ = handlers_factory()

    with caplog.at_level("INFO", logger="gemini_tg_bot.telegram.handlers"):
        await handlers.text_message(_update(text="question"), SimpleNamespace())

    assert "account default" in caplog.text
    assert "requested but received 0 thought characters" in caplog.text


async def test_disabled_thinking_does_not_log_thought_observability(
    handlers_factory,
    registry: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _StreamingSession(
        [
            SimpleNamespace(
                text="ok",
                text_delta="ok",
                images=(),
                thoughts="UNEXPECTED_THOUGHTS_SENTINEL",
            )
        ]
    )
    registry.get_state.return_value = _state(extended_thinking=False)
    registry.get_or_create.return_value = session
    handlers, _ = handlers_factory()

    with caplog.at_level("INFO", logger="gemini_tg_bot.telegram.handlers"):
        await handlers.text_message(_update(text="question"), SimpleNamespace())

    assert "Extended thinking result" not in caplog.text
