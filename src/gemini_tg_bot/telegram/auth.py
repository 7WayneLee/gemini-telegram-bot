"""Telegram authorization middleware with persistent allowlist overrides."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Iterable
from html import escape
from typing import Protocol

import aiosqlite
from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ApplicationHandlerStop, CallbackContext

from gemini_tg_bot.i18n import DEFAULT_LANGUAGE, translate
from gemini_tg_bot.storage.models import TELEGRAM_CHAT_ACCESS_SCHEMA_SQL


LOGGER = logging.getLogger(__name__)

UNAUTHORIZED_MESSAGE = (
    "This bot is private. Ask the administrator to add your Telegram user ID "
    "to the allowlist."
)

_CREATE_ACCESS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS telegram_user_access (
    user_id INTEGER PRIMARY KEY,
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1))
)
"""


class AdminNotifier(Protocol):
    """Deliver admin text with an optional Telegram parse mode."""

    def __call__(
        self,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> Awaitable[None]: ...


def _validate_user_id(user_id: int) -> int:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id must be a positive integer")
    return user_id


def _validate_chat_id(chat_id: int) -> int:
    if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id >= 0:
        raise ValueError("chat_id must be a negative integer")
    return chat_id


class SQLiteAccessOverrides:
    """Persist user and group-chat allow/deny decisions in SQLite.

    Stored decisions override ``ALLOWED_USER_IDS`` and ``ALLOWED_CHAT_IDS``.
    This lets an administrator revoke configured access immediately without
    waiting for a settings change and process restart.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def get(self, user_id: int) -> bool | None:
        """Return a stored access decision, or ``None`` when none exists."""

        user_id = _validate_user_id(user_id)
        await self._ensure_initialized()
        async with self._connection.execute(
            "SELECT allowed FROM telegram_user_access WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else bool(row[0])

    async def allow(self, user_id: int) -> None:
        """Persist an immediate allow decision."""

        await self._set(user_id, allowed=True)

    async def deny(self, user_id: int) -> None:
        """Persist an immediate deny decision."""

        await self._set(user_id, allowed=False)

    async def get_chat(self, chat_id: int) -> bool | None:
        """Return a stored group-chat decision, or ``None`` when absent."""

        chat_id = _validate_chat_id(chat_id)
        await self._ensure_initialized()
        async with self._connection.execute(
            "SELECT allowed FROM telegram_chat_access WHERE chat_id = ?",
            (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else bool(row[0])

    async def allow_chat(self, chat_id: int) -> None:
        """Persist an immediate group-chat allow decision."""

        await self._set_chat(chat_id, allowed=True)

    async def deny_chat(self, chat_id: int) -> None:
        """Persist an immediate group-chat deny decision."""

        await self._set_chat(chat_id, allowed=False)

    async def _set(self, user_id: int, *, allowed: bool) -> None:
        user_id = _validate_user_id(user_id)
        await self._ensure_initialized()
        await self._connection.execute(
            """
            INSERT INTO telegram_user_access (user_id, allowed)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET allowed = excluded.allowed
            """,
            (user_id, int(allowed)),
        )
        await self._connection.commit()

    async def _set_chat(self, chat_id: int, *, allowed: bool) -> None:
        chat_id = _validate_chat_id(chat_id)
        await self._ensure_initialized()
        await self._connection.execute(
            """
            INSERT INTO telegram_chat_access (chat_id, allowed)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET allowed = excluded.allowed
            """,
            (chat_id, int(allowed)),
        )
        await self._connection.commit()

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await self._connection.execute(_CREATE_ACCESS_TABLE_SQL)
            await self._connection.execute(TELEGRAM_CHAT_ACCESS_SCHEMA_SQL)
            await self._connection.commit()
            self._initialized = True


class AuthMiddleware:
    """Authorize private users and group chats through separate allowlists."""

    def __init__(
        self,
        *,
        admin_user_id: int,
        allowed_user_ids: Iterable[int] = (),
        allowed_chat_ids: Iterable[int] = (),
        access_overrides: SQLiteAccessOverrides,
        notify_admin: AdminNotifier | None = None,
        notification_language: str = DEFAULT_LANGUAGE,
    ) -> None:
        self._admin_user_id = _validate_user_id(admin_user_id)
        self._configured_user_ids = frozenset(
            _validate_user_id(user_id) for user_id in allowed_user_ids
        )
        self._configured_chat_ids = frozenset(
            _validate_chat_id(chat_id) for chat_id in allowed_chat_ids
        )
        self._access_overrides = access_overrides
        self._notify_admin = notify_admin
        self._notification_language = notification_language

    def is_admin(self, user_id: int) -> bool:
        """Return whether ``user_id`` has administrator privileges."""

        return _validate_user_id(user_id) == self._admin_user_id

    async def is_allowed(self, user_id: int) -> bool:
        """Resolve access from admin status, DB overrides, then settings."""

        user_id = _validate_user_id(user_id)
        if self.is_admin(user_id):
            return True
        override = await self._access_overrides.get(user_id)
        if override is not None:
            return override
        return user_id in self._configured_user_ids

    async def allow(self, user_id: int) -> None:
        """Allow a user immediately and persist the decision."""

        await self._access_overrides.allow(user_id)

    async def deny(self, user_id: int) -> None:
        """Deny a user immediately and persist the decision."""

        await self._access_overrides.deny(user_id)

    async def is_chat_allowed(self, chat_id: int) -> bool:
        """Resolve group access from DB overrides, then configuration."""

        chat_id = _validate_chat_id(chat_id)
        override = await self._access_overrides.get_chat(chat_id)
        if override is not None:
            return override
        return chat_id in self._configured_chat_ids

    async def allow_chat(self, chat_id: int) -> None:
        """Allow a group chat immediately and persist the decision."""

        await self._access_overrides.allow_chat(chat_id)

    async def deny_chat(self, chat_id: int) -> None:
        """Deny a group chat immediately and persist the decision."""

        await self._access_overrides.deny_chat(chat_id)

    async def __call__(self, update: Update, context: CallbackContext) -> None:
        """PTB callback that blocks all handlers after an unauthorized update.

        Register this callback in a handler group that runs before ordinary
        command and message handlers. ``ApplicationHandlerStop`` ensures the
        rejected update is not processed by later handlers.
        """

        del context
        chat = update.effective_chat
        chat_id = getattr(chat, "id", None)
        chat_type = getattr(chat, "type", None)
        user = update.effective_user
        user_id = user.id if user is not None else None
        if chat_type == ChatType.PRIVATE:
            if user_id is not None and await self.is_allowed(user_id):
                return
            LOGGER.warning(
                "Unauthorized Telegram private access attempt user_id=%s chat_id=%s",
                user_id,
                chat_id,
            )
            message = update.effective_message
            if message is not None:
                await message.reply_text(UNAUTHORIZED_MESSAGE)
            raise ApplicationHandlerStop

        if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
            if isinstance(chat_id, int) and not isinstance(chat_id, bool):
                try:
                    if await self.is_chat_allowed(chat_id):
                        return
                except ValueError:
                    pass
            LOGGER.warning(
                "Unauthorized Telegram group access attempt chat_id=%s",
                chat_id,
            )
            if isinstance(chat_id, int) and chat_id < 0:
                title = getattr(chat, "title", None)
                await self._notify_unauthorized_group(chat_id, title)
            raise ApplicationHandlerStop

        LOGGER.warning(
            "Unsupported Telegram chat access attempt chat_id=%s chat_type=%s",
            chat_id,
            chat_type,
        )
        raise ApplicationHandlerStop

    async def _notify_unauthorized_group(
        self,
        chat_id: int,
        title: object,
    ) -> None:
        if self._notify_admin is None:
            return
        raw_group_name = (
            title.strip()
            if isinstance(title, str) and title.strip()
            else translate(
                "auth.group_title_unknown",
                self._notification_language,
            )
        )
        group_name = escape(raw_group_name, quote=False)
        notification = translate(
            "auth.group_access_request",
            self._notification_language,
            chat_id=chat_id,
            title=group_name,
        )
        try:
            await self._notify_admin(
                notification,
                parse_mode=ParseMode.HTML,
            )
        except Exception as error:
            LOGGER.error(
                "Unable to notify administrator about group access (%s)",
                type(error).__name__,
            )
