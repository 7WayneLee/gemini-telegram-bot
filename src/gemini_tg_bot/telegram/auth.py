"""Telegram authorization middleware with persistent allowlist overrides."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

import aiosqlite
from telegram import Update
from telegram.ext import ApplicationHandlerStop, CallbackContext


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


def _validate_user_id(user_id: int) -> int:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id must be a positive integer")
    return user_id


class SQLiteAccessOverrides:
    """Persist administrator allow/deny decisions in SQLite.

    A stored decision overrides the configured ``ALLOWED_USER_IDS`` value. This
    lets an administrator revoke a configured user immediately without waiting
    for a settings change and process restart.
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

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await self._connection.execute(_CREATE_ACCESS_TABLE_SQL)
            await self._connection.commit()
            self._initialized = True


class AuthMiddleware:
    """Reject Telegram updates unless their sender is explicitly authorized."""

    def __init__(
        self,
        *,
        admin_user_id: int,
        allowed_user_ids: Iterable[int] = (),
        access_overrides: SQLiteAccessOverrides,
    ) -> None:
        self._admin_user_id = _validate_user_id(admin_user_id)
        self._configured_user_ids = frozenset(
            _validate_user_id(user_id) for user_id in allowed_user_ids
        )
        self._access_overrides = access_overrides

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

    async def __call__(self, update: Update, context: CallbackContext) -> None:
        """PTB callback that blocks all handlers after an unauthorized update.

        Register this callback in a handler group that runs before ordinary
        command and message handlers. ``ApplicationHandlerStop`` ensures the
        rejected update is not processed by later handlers.
        """

        del context
        user = update.effective_user
        user_id = user.id if user is not None else None
        if user_id is not None and await self.is_allowed(user_id):
            return

        LOGGER.warning("Unauthorized Telegram access attempt user_id=%s", user_id)
        message = update.effective_message
        if message is not None:
            await message.reply_text(UNAUTHORIZED_MESSAGE)
        raise ApplicationHandlerStop
