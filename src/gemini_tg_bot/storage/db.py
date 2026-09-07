"""Async SQLite connection management and schema migration."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

import aiosqlite

from .models import SCHEMA_SQL


async def migrate(connection: aiosqlite.Connection) -> None:
    """Create the current schema on ``connection`` if it does not exist."""

    await connection.executescript(SCHEMA_SQL)
    await connection.commit()


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
