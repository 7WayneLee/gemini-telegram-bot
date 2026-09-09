"""SQLite schema, record models, and data-access objects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import aiosqlite


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    chat_id       INTEGER PRIMARY KEY,
    cid           TEXT,
    metadata_json TEXT,
    model         TEXT,
    gem_id        TEXT,
    temporary     INTEGER DEFAULT 0,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_tasks (
    task_id     TEXT PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    cid         TEXT,
    research_id TEXT,
    prompt      TEXT NOT NULL,
    status      TEXT NOT NULL,
    result_path TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    command    TEXT,
    model      TEXT,
    ok         INTEGER NOT NULL,
    error_kind TEXT,
    latency_ms INTEGER,
    created_at TEXT NOT NULL
);
"""

ChatMetadata = list[str | None]


def serialize_metadata(metadata: ChatMetadata | None) -> str | None:
    """Serialize ordered ChatSession metadata without dropping null slots."""

    if metadata is None:
        return None
    if not isinstance(metadata, list) or any(
        item is not None and not isinstance(item, str) for item in metadata
    ):
        raise TypeError("metadata must be a list containing only strings or None")
    return json.dumps(metadata, ensure_ascii=False)


def deserialize_metadata(value: str | None) -> ChatMetadata | None:
    """Deserialize ChatSession metadata while preserving order and null slots."""

    if value is None:
        return None
    metadata = json.loads(value)
    if not isinstance(metadata, list) or any(
        item is not None and not isinstance(item, str) for item in metadata
    ):
        raise ValueError("metadata_json must contain an array of strings or nulls")
    return metadata


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatSession:
    chat_id: int
    cid: str | None = None
    metadata: ChatMetadata | None = None
    model: str | None = None
    gem_id: str | None = None
    temporary: bool = False
    updated_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ResearchTask:
    task_id: str
    chat_id: int
    cid: str | None = None
    research_id: str | None = None
    prompt: str
    status: str
    result_path: str | None = None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageLog:
    id: int | None = None
    user_id: int
    chat_id: int
    command: str | None = None
    model: str | None = None
    ok: bool
    error_kind: str | None = None
    latency_ms: int | None = None
    created_at: str


class ChatSessionDAO:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def upsert(self, session: ChatSession) -> None:
        await self._connection.execute(
            """
            INSERT INTO chat_sessions (
                chat_id, cid, metadata_json, model, gem_id, temporary, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                cid = excluded.cid,
                metadata_json = excluded.metadata_json,
                model = excluded.model,
                gem_id = excluded.gem_id,
                temporary = excluded.temporary,
                updated_at = excluded.updated_at
            """,
            (
                session.chat_id,
                session.cid,
                serialize_metadata(session.metadata),
                session.model,
                session.gem_id,
                int(session.temporary),
                session.updated_at,
            ),
        )
        await self._connection.commit()

    async def get(self, chat_id: int) -> ChatSession | None:
        async with self._connection.execute(
            "SELECT * FROM chat_sessions WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _chat_session_from_row(row) if row is not None else None

    async def list_all(self) -> list[ChatSession]:
        async with self._connection.execute(
            "SELECT * FROM chat_sessions ORDER BY chat_id"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_chat_session_from_row(row) for row in rows]

    async def delete(self, chat_id: int) -> bool:
        cursor = await self._connection.execute(
            "DELETE FROM chat_sessions WHERE chat_id = ?", (chat_id,)
        )
        try:
            deleted = cursor.rowcount > 0
        finally:
            await cursor.close()
        await self._connection.commit()
        return deleted


class ResearchTaskDAO:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def upsert(self, task: ResearchTask) -> None:
        await self._connection.execute(
            """
            INSERT INTO research_tasks (
                task_id, chat_id, cid, research_id, prompt, status, result_path,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                cid = excluded.cid,
                research_id = excluded.research_id,
                prompt = excluded.prompt,
                status = excluded.status,
                result_path = excluded.result_path,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (
                task.task_id,
                task.chat_id,
                task.cid,
                task.research_id,
                task.prompt,
                task.status,
                task.result_path,
                task.created_at,
                task.updated_at,
            ),
        )
        await self._connection.commit()

    async def get(self, task_id: str) -> ResearchTask | None:
        async with self._connection.execute(
            "SELECT * FROM research_tasks WHERE task_id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _research_task_from_row(row) if row is not None else None

    async def list_for_chat(self, chat_id: int) -> list[ResearchTask]:
        async with self._connection.execute(
            """
            SELECT * FROM research_tasks
            WHERE chat_id = ?
            ORDER BY created_at, task_id
            """,
            (chat_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_research_task_from_row(row) for row in rows]

    async def list_running(self) -> list[ResearchTask]:
        async with self._connection.execute(
            """
            SELECT * FROM research_tasks
            WHERE status = 'running'
            ORDER BY created_at, task_id
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [_research_task_from_row(row) for row in rows]

    async def delete(self, task_id: str) -> bool:
        cursor = await self._connection.execute(
            "DELETE FROM research_tasks WHERE task_id = ?", (task_id,)
        )
        try:
            deleted = cursor.rowcount > 0
        finally:
            await cursor.close()
        await self._connection.commit()
        return deleted


class UsageLogDAO:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def add(self, entry: UsageLog) -> int:
        cursor = await self._connection.execute(
            """
            INSERT INTO usage_log (
                user_id, chat_id, command, model, ok, error_kind, latency_ms,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.user_id,
                entry.chat_id,
                entry.command,
                entry.model,
                int(entry.ok),
                entry.error_kind,
                entry.latency_ms,
                entry.created_at,
            ),
        )
        try:
            row_id = cursor.lastrowid
        finally:
            await cursor.close()
        await self._connection.commit()
        if row_id is None:
            raise RuntimeError("SQLite did not return an id for the usage log row")
        return row_id

    async def list_for_chat(self, chat_id: int) -> list[UsageLog]:
        async with self._connection.execute(
            """
            SELECT * FROM usage_log
            WHERE chat_id = ?
            ORDER BY id
            """,
            (chat_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_usage_log_from_row(row) for row in rows]


ADMIN_NOTIFICATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS admin_notifications (
    fingerprint TEXT PRIMARY KEY,
    sent_at REAL NOT NULL
);
"""


class AdminNotificationDAO:
    """Suppress an administrator page that a restart would otherwise repeat.

    The service already refuses to page twice for one degraded reason, but that
    memory dies with the process.  A supervisor restarting a process that fails
    the same way every time turns one problem into a stream of identical
    messages, and Telegram rate-limits a chat: the flood buries the
    ``/setcookie`` prompt that is the only way back from AUTH degradation.

    Only the digest of a message is stored, never its text, so nothing an
    administrator may have supplied can reach the database.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def claim(
        self,
        message: str,
        *,
        now: float,
        cooldown_sec: float,
    ) -> bool:
        """Report whether ``message`` should be sent, recording it when so."""

        fingerprint = hashlib.sha256(message.encode("utf-8")).hexdigest()
        async with self._connection.execute(
            "SELECT sent_at FROM admin_notifications WHERE fingerprint = ?",
            (fingerprint,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None and now - float(row[0]) < cooldown_sec:
            return False
        await self._connection.execute(
            """
            INSERT INTO admin_notifications (fingerprint, sent_at)
            VALUES (?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET sent_at = excluded.sent_at
            """,
            (fingerprint, now),
        )
        await self._connection.commit()
        return True


def _chat_session_from_row(row: aiosqlite.Row) -> ChatSession:
    return ChatSession(
        chat_id=row["chat_id"],
        cid=row["cid"],
        metadata=deserialize_metadata(row["metadata_json"]),
        model=row["model"],
        gem_id=row["gem_id"],
        temporary=bool(row["temporary"]),
        updated_at=row["updated_at"],
    )


def _research_task_from_row(row: aiosqlite.Row) -> ResearchTask:
    return ResearchTask(
        task_id=row["task_id"],
        chat_id=row["chat_id"],
        cid=row["cid"],
        research_id=row["research_id"],
        prompt=row["prompt"],
        status=row["status"],
        result_path=row["result_path"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _usage_log_from_row(row: aiosqlite.Row) -> UsageLog:
    return UsageLog(
        id=row["id"],
        user_id=row["user_id"],
        chat_id=row["chat_id"],
        command=row["command"],
        model=row["model"],
        ok=bool(row["ok"]),
        error_kind=row["error_kind"],
        latency_ms=row["latency_ms"],
        created_at=row["created_at"],
    )
