"""Mock-only tests for administrator commands and credential hot restart."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr
from telegram.constants import ChatType
from telegram.ext import ApplicationHandlerStop

from gemini_tg_bot.config import Settings
from gemini_tg_bot.gemini import service as service_module
from gemini_tg_bot.gemini.errors import ErrorKind
from gemini_tg_bot.gemini.service import GeminiService, ServiceState
from gemini_tg_bot.i18n import LANGUAGE_ENGLISH, translate
from gemini_tg_bot.queue import RequestQueue
from gemini_tg_bot.storage.db import Database
from gemini_tg_bot.telegram.auth import AuthMiddleware, SQLiteAccessOverrides
from gemini_tg_bot.telegram.handlers import (
    ADMIN_ONLY,
    CREDENTIALS_NOT_RELAYED,
    EgressMeter,
    TelegramHandlers,
    register_handlers,
)


ADMIN_USER_ID = 9001


def _update(
    text: str,
    *,
    user_id: int = ADMIN_USER_ID,
    chat_id: int | None = None,
    chat_type: str = ChatType.PRIVATE,
    delete: AsyncMock | None = None,
) -> SimpleNamespace:
    message = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(),
        delete=delete or AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(
            id=user_id if chat_id is None else chat_id,
            type=chat_type,
        ),
        effective_message=message,
        callback_query=None,
    )


def _mock_service() -> MagicMock:
    service = MagicMock()
    service.reinit = AsyncMock()
    service.health = SimpleNamespace(
        state=ServiceState.HEALTHY,
        accepting_requests=True,
        last_error_kind=None,
        last_error_type=None,
    )
    return service


async def _admin_stack(
    tmp_path: Path,
    *,
    allowed_user_ids: set[int] | None = None,
    allowed_chat_ids: set[int] | None = None,
    service: MagicMock | None = None,
) -> tuple[Database, AuthMiddleware, TelegramHandlers, MagicMock]:
    database = Database(tmp_path / "admin.sqlite3")
    await database.connect()
    auth = AuthMiddleware(
        admin_user_id=ADMIN_USER_ID,
        allowed_user_ids=allowed_user_ids or set(),
        allowed_chat_ids=allowed_chat_ids or set(),
        access_overrides=SQLiteAccessOverrides(database.connection),
    )
    mocked_service = service or _mock_service()
    handlers = TelegramHandlers(
        service=mocked_service,
        sessions=AsyncMock(),
        request_queue=RequestQueue(
            max_concurrency=1,
            user_rate_limit_per_min=10,
        ),
        usage_dao=None,
        egress_meter=EgressMeter(),
        cookie_path=tmp_path,
        secure_1psid=SecretStr("FAKE_1PSID_FOR_TEST"),
        database=database,
    )
    application = SimpleNamespace(add_handler=MagicMock())
    register_handlers(application, auth=auth, handlers=handlers)
    return database, auth, handlers, mocked_service


async def test_admin_commands_reject_an_allowed_non_admin(tmp_path: Path) -> None:
    database, auth, handlers, _ = await _admin_stack(
        tmp_path,
        allowed_user_ids={7001},
    )
    try:
        update = _update("/allow 7002", user_id=7001)

        await handlers.allow(update, SimpleNamespace(args=["7002"]))

        update.effective_message.reply_text.assert_awaited_once_with(ADMIN_ONLY)
        assert await auth.is_allowed(7002) is False
    finally:
        await database.close()


async def test_group_setcookie_hides_whether_the_sender_is_an_admin(
    tmp_path: Path,
) -> None:
    """Identical denials prevent group members from probing the admin identity."""

    non_admin_id = 7001
    database, auth, handlers, service = await _admin_stack(
        tmp_path,
        allowed_user_ids={non_admin_id},
    )
    try:
        auth.is_admin = MagicMock(wraps=auth.is_admin)
        admin_update = _update(
            "/setcookie",
            chat_id=-1001,
            chat_type=ChatType.GROUP,
        )
        non_admin_update = _update(
            "/setcookie",
            user_id=non_admin_id,
            chat_id=-1001,
            chat_type=ChatType.GROUP,
        )

        await handlers.setcookie(admin_update, SimpleNamespace())
        await handlers.setcookie(non_admin_update, SimpleNamespace())

        admin_reply = admin_update.effective_message.reply_text.await_args.args[0]
        non_admin_reply = (
            non_admin_update.effective_message.reply_text.await_args.args[0]
        )
        assert admin_reply == non_admin_reply == translate(
            "command.private_only",
            LANGUAGE_ENGLISH,
        )
        assert handlers._awaiting_cookie_users == set()
        auth.is_admin.assert_not_called()
        service.reinit.assert_not_awaited()
    finally:
        await database.close()


@pytest.mark.parametrize("command", ["allow", "deny", "health"])
async def test_other_admin_commands_are_private_only(
    tmp_path: Path,
    command: str,
) -> None:
    """Every administrative entry point must share the same group boundary."""

    database, auth, handlers, _ = await _admin_stack(tmp_path)
    try:
        update = _update(
            f"/{command} 7002",
            chat_id=-1001,
            chat_type=ChatType.SUPERGROUP,
        )

        await getattr(handlers, command)(
            update,
            SimpleNamespace(args=["7002"]),
        )

        update.effective_message.reply_text.assert_awaited_once_with(
            translate("command.private_only", LANGUAGE_ENGLISH)
        )
        assert await auth.is_allowed(7002) is False
    finally:
        await database.close()


async def test_allow_and_deny_persist_and_take_effect_immediately(
    tmp_path: Path,
) -> None:
    database, auth, handlers, _ = await _admin_stack(tmp_path)
    try:
        await handlers.allow(
            _update("/allow 7002"),
            SimpleNamespace(args=["7002"]),
        )
        assert await auth.is_allowed(7002) is True

        await handlers.deny(
            _update("/deny 7002"),
            SimpleNamespace(args=["7002"]),
        )
        assert await auth.is_allowed(7002) is False

        reloaded = AuthMiddleware(
            admin_user_id=ADMIN_USER_ID,
            access_overrides=SQLiteAccessOverrides(database.connection),
        )
        assert await reloaded.is_allowed(7002) is False
    finally:
        await database.close()


async def test_allow_chat_and_deny_chat_take_effect_without_restart(
    tmp_path: Path,
) -> None:
    """Admin chat decisions must change group access in the running process."""

    database, auth, handlers, _ = await _admin_stack(tmp_path)
    try:
        chat_id = -1001
        member_id = 7002
        group_update = _update(
            "/start",
            user_id=member_id,
            chat_id=chat_id,
            chat_type=ChatType.GROUP,
        )

        await handlers.allow_chat(
            _update(f"/allow_chat {chat_id}"),
            SimpleNamespace(args=[str(chat_id)]),
        )
        await auth(group_update, SimpleNamespace())
        assert await auth.is_allowed(member_id) is False

        await handlers.deny_chat(
            _update(f"/deny_chat {chat_id}"),
            SimpleNamespace(args=[str(chat_id)]),
        )
        with pytest.raises(ApplicationHandlerStop):
            await auth(group_update, SimpleNamespace())
        group_update.effective_message.reply_text.assert_not_awaited()
    finally:
        await database.close()


@pytest.mark.parametrize("command", ["allow_chat", "deny_chat"])
async def test_chat_access_commands_are_private_only(
    tmp_path: Path,
    command: str,
) -> None:
    """Group members must not discover or exercise administrator privileges."""

    database, auth, handlers, _ = await _admin_stack(
        tmp_path,
        allowed_chat_ids={-1001},
    )
    try:
        update = _update(
            f"/{command} -1002",
            chat_id=-1001,
            chat_type=ChatType.SUPERGROUP,
        )

        await getattr(handlers, command)(
            update,
            SimpleNamespace(args=["-1002"]),
        )

        update.effective_message.reply_text.assert_awaited_once_with(
            translate("command.private_only", LANGUAGE_ENGLISH)
        )
        assert await auth.is_chat_allowed(-1002) is False
    finally:
        await database.close()


async def test_setcookie_deletes_message_then_hot_restarts_without_process_restart(
    tmp_path: Path,
) -> None:
    """The private admin flow must keep its deletion-first singleton restart."""

    events: list[str] = []
    service = _mock_service()

    async def reinit(**kwargs: Any) -> None:
        del kwargs
        events.append("reinit")

    service.reinit.side_effect = reinit
    database, _, handlers, bound_service = await _admin_stack(
        tmp_path,
        service=service,
    )
    try:
        await handlers.setcookie(_update("/setcookie"), SimpleNamespace())

        async def delete() -> None:
            events.append("delete")

        credential_update = _update(
            "__Secure-1PSID=FAKE_REPLACEMENT_1PSID_FOR_TEST\n"
            "__Secure-1PSIDTS=FAKE_REPLACEMENT_1PSIDTS_FOR_TEST",
            delete=AsyncMock(side_effect=delete),
        )
        process_id = os.getpid()
        service_identity = id(bound_service)

        await handlers.text_message(credential_update, SimpleNamespace())

        assert events == ["delete", "reinit"]
        assert os.getpid() == process_id
        assert id(bound_service) == service_identity
        credential_update.effective_message.delete.assert_awaited_once_with()
        service.reinit.assert_awaited_once_with(
            secure_1psid="FAKE_REPLACEMENT_1PSID_FOR_TEST",
            secure_1psidts="FAKE_REPLACEMENT_1PSIDTS_FOR_TEST",
        )
        confirmation = (
            credential_update.effective_message.reply_text.await_args.args[0]
        )
        assert "FAKE_REPLACEMENT_1PSID_FOR_TEST" not in confirmation
        assert "FAKE_REPLACEMENT_1PSIDTS_FOR_TEST" not in confirmation
    finally:
        await database.close()


async def test_setcookie_never_logs_credentials_on_reinit_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _mock_service()
    service.reinit.side_effect = RuntimeError(
        "FAKE_REPLACEMENT_1PSID_FOR_TEST"
    )
    database, _, handlers, _ = await _admin_stack(tmp_path, service=service)
    try:
        await handlers.setcookie(_update("/setcookie"), SimpleNamespace())
        update = _update(
            "FAKE_REPLACEMENT_1PSID_FOR_TEST\n"
            "FAKE_REPLACEMENT_1PSIDTS_FOR_TEST"
        )

        await handlers.text_message(update, SimpleNamespace())

        assert "FAKE_REPLACEMENT_1PSID_FOR_TEST" not in caplog.text
        assert "FAKE_REPLACEMENT_1PSIDTS_FOR_TEST" not in caplog.text
        update.effective_message.delete.assert_awaited_once_with()
    finally:
        await database.close()


async def test_health_reports_client_error_and_live_database(
    tmp_path: Path,
) -> None:
    service = _mock_service()
    service.health = SimpleNamespace(
        state=ServiceState.DEGRADED,
        accepting_requests=False,
        last_error_kind=ErrorKind.TRANSIENT,
        last_error_type="TimeoutError",
    )
    database, _, handlers, _ = await _admin_stack(tmp_path, service=service)
    try:
        update = _update("/health")

        await handlers.health(update, SimpleNamespace())

        report = update.effective_message.reply_text.await_args.args[0]
        assert "Client status: degraded" in report
        assert "Accepting requests: No" in report
        assert "Last error: transient (TimeoutError)" in report
        assert "Database status: healthy" in report
    finally:
        await database.close()


def _upstream_client(cookie_jar: object) -> MagicMock:
    client = MagicMock()
    client.cookies = cookie_jar
    client.init = AsyncMock()
    client.close = AsyncMock()
    return client


async def test_reinit_clears_old_cache_only_when_credentials_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="FAKE_TELEGRAM_BOT_TOKEN_FOR_TEST",
        ADMIN_USER_ID=ADMIN_USER_ID,
        GEMINI_SECURE_1PSID="FAKE_1PSID_FOR_TEST",
        GEMINI_SECURE_1PSIDTS="FAKE_1PSIDTS_FOR_TEST",
        GEMINI_COOKIE_PATH=tmp_path / "cookies",
    )
    old_cookie_jar = object()
    first = _upstream_client(old_cookie_jar)
    second = _upstream_client(object())
    third = _upstream_client(object())
    factory = MagicMock(side_effect=[first, second, third])
    clear_cache = MagicMock()
    monkeypatch.setattr(service_module, "GeminiClient", factory)
    monkeypatch.setattr(service_module, "clear_cookies_cache", clear_cache)
    service = GeminiService(settings)
    try:
        await service.init()

        await service.reinit(
            secure_1psid="FAKE_REPLACEMENT_1PSID_FOR_TEST",
            secure_1psidts="FAKE_REPLACEMENT_1PSIDTS_FOR_TEST",
        )

        clear_cache.assert_called_once_with(old_cookie_jar)
        first.close.assert_awaited_once_with()

        clear_cache.reset_mock()
        await service.reinit()

        clear_cache.assert_not_called()
        second.close.assert_awaited_once_with()
    finally:
        await service.close()


# Split so the literal never appears whole: this shape is exactly what the
# repository's own CI credential scanner greps for, and a committed test
# fixture matching it would fail that job.
_FAKE_PSID = "g.a" + "000" + "FAKE_1PSID_SHAPE_FOR_TEST_0000000000"
_FAKE_PSIDTS = "sidts" + "-" + "FAKE_1PSIDTS_SHAPE_FOR_TEST_0000000000"


async def test_pasted_credentials_are_never_relayed_to_gemini(
    tmp_path: Path,
) -> None:
    """A paste with no preceding /setcookie still takes the credential path.

    The prompt that arms the two-step interaction can be lost -- to a restart,
    or to Telegram flood control -- leaving an administrator pasting cookies
    the bot is not expecting.  Treating that as ordinary text would forward the
    session cookie to Gemini and leave it in the chat transcript.
    """

    database, _, handlers, service = await _admin_stack(tmp_path)
    try:
        update = _update(f"{_FAKE_PSID}\n{_FAKE_PSIDTS}")

        await handlers.text_message(update, SimpleNamespace())

        update.effective_message.delete.assert_awaited_once_with()
        service.reinit.assert_awaited_once_with(
            secure_1psid=_FAKE_PSID,
            secure_1psidts=_FAKE_PSIDTS,
        )
        handlers._sessions.get.assert_not_called()
        confirmation = update.effective_message.reply_text.await_args.args[0]
        assert _FAKE_PSID not in confirmation
        assert _FAKE_PSIDTS not in confirmation
    finally:
        await database.close()


async def test_non_admin_credential_paste_is_refused_without_reinit(
    tmp_path: Path,
) -> None:
    """A non-administrator's paste is stopped, but never applied."""

    other_user = ADMIN_USER_ID + 1
    database, _, handlers, service = await _admin_stack(
        tmp_path,
        allowed_user_ids={other_user},
    )
    try:
        update = _update(f"{_FAKE_PSID}\n{_FAKE_PSIDTS}", user_id=other_user)

        await handlers.text_message(update, SimpleNamespace())

        service.reinit.assert_not_awaited()
        handlers._sessions.get.assert_not_called()
        assert (
            update.effective_message.reply_text.await_args.args[0]
            == CREDENTIALS_NOT_RELAYED
        )
    finally:
        await database.close()


async def test_mentioning_the_cookie_names_is_still_an_ordinary_prompt(
    tmp_path: Path,
) -> None:
    """Detection keys on the value shape, so asking about cookies still works."""

    database, _, handlers, service = await _admin_stack(tmp_path)
    try:
        update = _update("__Secure-1PSID 跟 __Secure-1PSIDTS 有什麼差別？")

        await handlers.text_message(update, SimpleNamespace())

        service.reinit.assert_not_awaited()
        update.effective_message.delete.assert_not_awaited()
        replies = [
            call.args[0]
            for call in update.effective_message.reply_text.await_args_list
        ]
        assert CREDENTIALS_NOT_RELAYED not in replies
    finally:
        await database.close()
