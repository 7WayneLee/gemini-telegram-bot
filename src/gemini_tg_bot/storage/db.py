"""Async SQLite connection management and schema migration."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Awaitable, Callable

import aiosqlite

from .models import (
    ADMIN_NOTIFICATION_SCHEMA_SQL,
    CHAT_SESSION_THINKING_COLUMN,
    SCHEMA_SQL,
)


LATEST_SCHEMA_VERSION = 5

_TELEGRAM_ACCESS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS telegram_user_access (
    user_id INTEGER PRIMARY KEY,
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1))
);
"""


async def migrate(connection: aiosqlite.Connection) -> None:
    """Upgrade ``connection`` through every versioned schema migration."""

    current_version = await _user_version(connection)
    if current_version > LATEST_SCHEMA_VERSION:
        raise RuntimeError(
            "database schema version "
            f"{current_version} is newer than supported version "
            f"{LATEST_SCHEMA_VERSION}"
        )

    migrations: dict[int, Callable[[aiosqlite.Connection], Awaitable[None]]] = {
        1: _migrate_to_v1,
        2: _migrate_to_v2,
        3: _migrate_to_v3,
        4: _migrate_to_v4,
        5: _migrate_to_v5,
    }
    for version in range(current_version + 1, LATEST_SCHEMA_VERSION + 1):
        await migrations[version](connection)


async def _migrate_to_v1(connection: aiosqlite.Connection) -> None:
    await _run_schema_migration(connection, SCHEMA_SQL, version=1)


async def _migrate_to_v2(connection: aiosqlite.Connection) -> None:
    await connection.execute("BEGIN IMMEDIATE")
    try:
        if not await _column_exists(connection, "research_tasks", "plan_json"):
            await connection.execute(
                "ALTER TABLE research_tasks ADD COLUMN plan_json TEXT"
            )
        await connection.execute("PRAGMA user_version = 2")
        await connection.commit()
    except BaseException:
        await connection.rollback()
        raise


async def _migrate_to_v3(connection: aiosqlite.Connection) -> None:
    await _run_schema_migration(
        connection,
        _TELEGRAM_ACCESS_SCHEMA_SQL,
        version=3,
    )


async def _migrate_to_v4(connection: aiosqlite.Connection) -> None:
    await _run_schema_migration(
        connection,
        ADMIN_NOTIFICATION_SCHEMA_SQL,
        version=4,
    )


async def _migrate_to_v5(connection: aiosqlite.Connection) -> None:
    await connection.execute("BEGIN IMMEDIATE")
    try:
        if not await _column_exists(
            connection,
            "chat_sessions",
            CHAT_SESSION_THINKING_COLUMN,
        ):
            await connection.execute(
                "ALTER TABLE chat_sessions "
                f"ADD COLUMN {CHAT_SESSION_THINKING_COLUMN} INTEGER DEFAULT 0"
            )
        await connection.execute("PRAGMA user_version = 5")
        await connection.commit()
    except BaseException:
        await connection.rollback()
        raise


async def _run_schema_migration(
    connection: aiosqlite.Connection,
    schema_sql: str,
    *,
    version: int,
) -> None:
    try:
        await connection.executescript(
            f"BEGIN IMMEDIATE;\n{schema_sql}\n"
            f"PRAGMA user_version = {version};\nCOMMIT;"
        )
    except BaseException:
        if connection.in_transaction:
            await connection.rollback()
        raise


async def _user_version(connection: aiosqlite.Connection) -> int:
    async with connection.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("SQLite did not return PRAGMA user_version")
    return int(row[0])


async def _column_exists(
    connection: aiosqlite.Connection,
    table: str,
    column: str,
) -> bool:
    async with connection.execute(f"PRAGMA table_info({table})") as cursor:
        rows = await cursor.fetchall()
    return any(row[1] == column for row in rows)


class Database:
    """Own one aiosqlite connection and migrate it when opened."""

    def __init__(self, path: str | Path) -> None:
        self.path = path
        self._connection: aiosqlite.Connection | None = None

    @property
    def connection(self) -> aiosqlite.Connection:
        """Return the open connection, or fail clearly before ``connect``."""

        if self._connection is None:
            raise RuntimeError("database is not connected")
        return self._connection

    async def connect(self) -> aiosqlite.Connection:
        """Open the database and run its idempotent migration."""

        if self._connection is not None:
            return self._connection

        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        try:
            await migrate(connection)
        except BaseException:
            await connection.close()
            raise

        self._connection = connection
        return connection

    async def migrate(self) -> None:
        """Run migrations explicitly, opening the database if necessary."""

        if self._connection is None:
            await self.connect()
            return
        await migrate(self._connection)

    async def close(self) -> None:
        """Close the connection; repeated calls are harmless."""

        if self._connection is None:
            return
        connection = self._connection
        self._connection = None
        await connection.close()

    async def __aenter__(self) -> Database:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()
