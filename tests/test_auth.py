from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ApplicationHandlerStop

from gemini_tg_bot.storage.db import Database
from gemini_tg_bot.telegram.auth import (
    UNAUTHORIZED_MESSAGE,
    AuthMiddleware,
    SQLiteAccessOverrides,
)


def _update(user_id: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=(
            None if user_id is None else SimpleNamespace(id=user_id)
        ),
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
    )


def _middleware(
    database: Database,
    *,
    allowed_user_ids: set[int] | None = None,
    admin_user_id: int = 9001,
) -> AuthMiddleware:
    return AuthMiddleware(
        admin_user_id=admin_user_id,
        allowed_user_ids=allowed_user_ids or set(),
        access_overrides=SQLiteAccessOverrides(database.connection),
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
    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database, allowed_user_ids={101})

        assert await middleware.is_allowed(101) is True
        assert await middleware.is_allowed(102) is False
        assert await middleware.is_allowed(9001) is True
        assert middleware.is_admin(9001) is True
        assert middleware.is_admin(101) is False


@pytest.mark.asyncio
async def test_database_allow_and_deny_take_effect_immediately(tmp_path) -> None:
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
    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database)

        await middleware.deny(9001)

        assert await middleware.is_allowed(9001) is True


@pytest.mark.asyncio
async def test_unauthorized_update_replies_once_logs_id_and_stops(
    tmp_path, caplog
) -> None:
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
    async with Database(tmp_path / "bot.sqlite3") as database:
        middleware = _middleware(database, allowed_user_ids={101})
        update = _update(101)

        await middleware(update, SimpleNamespace())

        update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_without_sender_is_denied(tmp_path, caplog) -> None:
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
    with pytest.raises(ValueError, match="positive integer"):
        AuthMiddleware(
            admin_user_id=9001,
            allowed_user_ids={user_id},
            access_overrides=None,
        )
