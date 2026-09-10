from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType
from telegram.ext import ApplicationHandlerStop

from gemini_tg_bot.storage.db import Database
from gemini_tg_bot.storage.models import AdminNotificationDAO
from gemini_tg_bot.telegram.auth import (
    UNAUTHORIZED_MESSAGE,
    AuthMiddleware,
    SQLiteAccessOverrides,
)


def _update(
    user_id: int | None,
    *,
    chat_id: int | None = None,
    chat_type: str = ChatType.PRIVATE,
    title: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=(
            None if user_id is None else SimpleNamespace(id=user_id)
        ),
        effective_chat=SimpleNamespace(
            id=chat_id if chat_id is not None else (user_id or 5001),
            type=chat_type,
            title=title,
        ),
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
    )


def _middleware(
    database: Database,
    *,
    allowed_user_ids: set[int] | None = None,
    allowed_chat_ids: set[int] | None = None,
    admin_user_id: int = 9001,
    notify_admin: AsyncMock | None = None,
) -> AuthMiddleware:
    return AuthMiddleware(
        admin_user_id=admin_user_id,
        allowed_user_ids=allowed_user_ids or set(),
        allowed_chat_ids=allowed_chat_ids or set(),
        access_overrides=SQLiteAccessOverrides(database.connection),
        notify_admin=notify_admin,
    )


@pytest.mark.asyncio
async def test_empty_allowlist_rejects_every_non_admin_user(tmp_path) -> None:
    """An empty ALLOWED_USER_IDS value must default to denying all users."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database)

        assert await middleware.is_allowed(101) is False
        assert await middleware.is_allowed(202) is False


@pytest.mark.asyncio
async def test_configured_user_and_admin_are_allowed(tmp_path) -> None:
    """Private-chat authorization must retain the pre-group user semantics."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database, allowed_user_ids={101})

        assert await middleware.is_allowed(101) is True
        assert await middleware.is_allowed(102) is False
        assert await middleware.is_allowed(9001) is True
        assert middleware.is_admin(9001) is True
        assert middleware.is_admin(101) is False


@pytest.mark.asyncio
async def test_database_allow_and_deny_take_effect_immediately(tmp_path) -> None:
    """Persistent user decisions must retain their restart-free precedence."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database, allowed_user_ids={101})

        await middleware.allow(202)
        assert await middleware.is_allowed(202) is True

        await middleware.deny(101)
        assert await middleware.is_allowed(101) is False

        reloaded = _middleware(database, allowed_user_ids={101})
        assert await reloaded.is_allowed(202) is True
        assert await reloaded.is_allowed(101) is False


@pytest.mark.asyncio
async def test_admin_cannot_be_locked_out_by_database_override(tmp_path) -> None:
    """The private administrator path must remain reachable after a bad deny."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database)

        await middleware.deny(9001)

        assert await middleware.is_allowed(9001) is True


@pytest.mark.asyncio
async def test_unauthorized_update_replies_once_logs_id_and_stops(
    tmp_path, caplog
) -> None:
    """An unauthorized private user must receive the unchanged denial message."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database)
        update = _update(404)

        with caplog.at_level(logging.WARNING), pytest.raises(
            ApplicationHandlerStop
        ):
            await middleware(update, SimpleNamespace())

        update.effective_message.reply_text.assert_awaited_once_with(
            UNAUTHORIZED_MESSAGE
        )
        assert "user_id=404" in caplog.text


@pytest.mark.asyncio
async def test_authorized_update_passes_without_reply(tmp_path) -> None:
    """A configured private user must keep passing without middleware noise."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database, allowed_user_ids={101})
        update = _update(101)

        await middleware(update, SimpleNamespace())

        update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_without_sender_is_denied(tmp_path, caplog) -> None:
    """A private update without a sender cannot bypass the user allowlist."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database, allowed_user_ids={101})
        update = _update(None)

        with caplog.at_level(logging.WARNING), pytest.raises(
            ApplicationHandlerStop
        ):
            await middleware(update, SimpleNamespace())

        update.effective_message.reply_text.assert_awaited_once_with(
            UNAUTHORIZED_MESSAGE
        )
        assert "user_id=None" in caplog.text


@pytest.mark.parametrize("user_id", [0, -1, True, "101"])
def test_invalid_user_ids_are_rejected(user_id) -> None:
    """Strict user identifiers keep malformed private overrides out of SQLite."""

    with pytest.raises(ValueError, match="positive integer"):
        AuthMiddleware(
            admin_user_id=9001,
            allowed_user_ids={user_id},
            access_overrides=None,
        )


@pytest.mark.parametrize("chat_type", [ChatType.GROUP, ChatType.SUPERGROUP])
@pytest.mark.asyncio
async def test_approved_group_ignores_member_allowlist(
    tmp_path,
    chat_type: str,
) -> None:
    """Group approval must grant access even when the member cannot use DMs."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database, allowed_chat_ids={-1001})
        update = _update(404, chat_id=-1001, chat_type=chat_type)

        await middleware(update, SimpleNamespace())

        update.effective_message.reply_text.assert_not_awaited()
        assert await middleware.is_allowed(404) is False


@pytest.mark.parametrize("chat_type", [ChatType.GROUP, ChatType.SUPERGROUP])
@pytest.mark.asyncio
async def test_unapproved_group_silently_rejects_every_member(
    tmp_path,
    chat_type: str,
) -> None:
    """Silence avoids both group noise and disclosure of the bot's access state."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database)
        update = _update(404, chat_id=-1001, chat_type=chat_type)

        with pytest.raises(ApplicationHandlerStop):
            await middleware(update, SimpleNamespace())

        update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_is_rejected_silently_in_an_unapproved_group(tmp_path) -> None:
    """No admin exception may reveal which member controls the bot."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database)
        update = _update(9001, chat_id=-1001, chat_type=ChatType.SUPERGROUP)

        with pytest.raises(ApplicationHandlerStop):
            await middleware(update, SimpleNamespace())

        update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_update_is_rejected_without_a_reply(tmp_path) -> None:
    """Unsupported chat types must never inherit private or group privileges."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(
            database,
            allowed_user_ids={404},
            allowed_chat_ids={-1001},
        )
        update = _update(404, chat_id=-1001, chat_type=ChatType.CHANNEL)

        with pytest.raises(ApplicationHandlerStop):
            await middleware(update, SimpleNamespace())

        update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_database_override_precedes_configured_allowlist(tmp_path) -> None:
    """An administrator must be able to revoke configured groups immediately."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database, allowed_chat_ids={-1001})

        await middleware.deny_chat(-1001)
        await middleware.allow_chat(-1002)

        assert await middleware.is_chat_allowed(-1001) is False
        assert await middleware.is_chat_allowed(-1002) is True
        reloaded = _middleware(database, allowed_chat_ids={-1001})
        assert await reloaded.is_chat_allowed(-1001) is False
        assert await reloaded.is_chat_allowed(-1002) is True


@pytest.mark.parametrize(
    ("title", "expected_name"),
    [("Project Crew", "Project Crew"), (None, "Unnamed group")],
)
@pytest.mark.asyncio
async def test_unapproved_group_notifies_admin_with_actionable_identity(
    tmp_path,
    title: str | None,
    expected_name: str,
) -> None:
    """The private page must identify and make even an unnamed group approvable."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        notify_admin = AsyncMock()
        middleware = _middleware(database, notify_admin=notify_admin)
        update = _update(
            404,
            chat_id=-1001,
            chat_type=ChatType.GROUP,
            title=title,
        )

        with pytest.raises(ApplicationHandlerStop):
            await middleware(update, SimpleNamespace())

        notification = notify_admin.await_args.args[0]
        assert expected_name in notification
        assert "-1001" in notification
        assert "/allow_chat -1001" in notification
        update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_notification_reuses_durable_message_deduplication(
    tmp_path,
) -> None:
    """A busy unauthorized group must not flood the administrator's private chat."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        dao = AdminNotificationDAO(database.connection)
        delivered = AsyncMock()

        async def notify_admin(message: str) -> None:
            if await dao.claim(message, now=100.0, cooldown_sec=900.0):
                await delivered(message)

        middleware = AuthMiddleware(
            admin_user_id=9001,
            access_overrides=SQLiteAccessOverrides(database.connection),
            notify_admin=notify_admin,
        )

        for _ in range(2):
            update = _update(
                404,
                chat_id=-1001,
                chat_type=ChatType.GROUP,
                title="Busy group",
            )
            with pytest.raises(ApplicationHandlerStop):
                await middleware(update, SimpleNamespace())
            update.effective_message.reply_text.assert_not_awaited()

        delivered.assert_awaited_once()


@pytest.mark.parametrize("chat_id", [0, 1, True, "-1001"])
def test_invalid_group_chat_ids_are_rejected(chat_id) -> None:
    """Only Telegram's negative group identifiers may enter chat allowlists."""

    with pytest.raises(ValueError, match="negative integer"):
        AuthMiddleware(
            admin_user_id=9001,
            allowed_chat_ids={chat_id},
            access_overrides=None,
        )
