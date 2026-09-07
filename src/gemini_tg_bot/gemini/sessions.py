"""Per-Telegram-chat Gemini session registry backed by SQLite."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from gemini_webapi.client import ChatSession

from gemini_tg_bot.gemini.service import GeminiService
from gemini_tg_bot.storage.db import Database
from gemini_tg_bot.storage.models import (
    ChatSession as StoredChatSession,
    ChatSessionDAO,
)


_UNSET = object()


@dataclass(frozen=True)
class ChatState:
    """A read-only snapshot of one Telegram chat's persisted settings."""

    chat_id: int
    cid: str | None
    model: str | None
    gem_id: str | None
    temporary: bool
    updated_at: str | None


class ChatSessionRegistry:
    """Map Telegram chat IDs to Gemini sessions and persist their metadata."""

    def __init__(self, service: GeminiService, database: Database) -> None:
        self._service = service
        self._dao = ChatSessionDAO(database.connection)
        self._sessions: dict[int, ChatSession] = {}
        self._lock = asyncio.Lock()

    async def get_state(self, chat_id: int) -> ChatState:
        """Return persisted state, or defaults for a chat not seen before."""

        async with self._lock:
            record = await self._dao.get(chat_id)
            return _to_state(chat_id, record)

    async def get_or_create(self, chat_id: int) -> ChatSession:
        """Return the current session, restoring persisted context if needed."""

        async with self._lock:
            session = self._sessions.get(chat_id)
            if session is not None:
                return session

            record = await self._dao.get(chat_id)
            session = self._start_chat(record)
            self._sessions[chat_id] = session
            return session

    async def reset(self, chat_id: int) -> None:
        """Forget conversation context while preserving the chat's settings."""

        async with self._lock:
            current = await self._dao.get(chat_id)
            await self._dao.upsert(
                StoredChatSession(
                    chat_id=chat_id,
                    model=current.model if current is not None else None,
                    gem_id=current.gem_id if current is not None else None,
                    temporary=(
                        current.temporary if current is not None else False
                    ),
                    updated_at=_timestamp(),
                )
            )
            self._sessions.pop(chat_id, None)

    async def set_model(self, chat_id: int, model: str | None) -> None:
        """Set the model name, using ``None`` for the account default."""

        async with self._lock:
            current = await self._dao.get(chat_id)
            await self._dao.upsert(
                _updated_record(chat_id, current, model=model)
            )
            # ChatSession accepts its model only at construction time.
            self._sessions.pop(chat_id, None)

    async def set_gem(self, chat_id: int, gem_id: str | None) -> None:
        """Set the Gem ID, using ``None`` to disable Gems for this chat."""

        async with self._lock:
            current = await self._dao.get(chat_id)
            await self._dao.upsert(
                _updated_record(chat_id, current, gem_id=gem_id)
            )
            # ChatSession accepts its Gem only at construction time.
            self._sessions.pop(chat_id, None)

    async def set_temporary(self, chat_id: int, temporary: bool) -> None:
        """Persist whether subsequent messages should be temporary."""

        async with self._lock:
            current = await self._dao.get(chat_id)
            await self._dao.upsert(
                _updated_record(chat_id, current, temporary=temporary)
            )

    async def persist(self, chat_id: int, session: ChatSession) -> None:
        """Persist the conversation identifiers from a successful response."""

        # Take a positional copy before awaiting SQLite so later mutations of
        # the upstream metadata list cannot change the snapshot being written.
        metadata = session.metadata
        metadata_snapshot = None if metadata is None else list(metadata)
        cid = session.cid

        async with self._lock:
            current = await self._dao.get(chat_id)
            await self._dao.upsert(
                StoredChatSession(
                    chat_id=chat_id,
                    cid=cid,
                    metadata=metadata_snapshot,
                    model=current.model if current is not None else None,
                    gem_id=current.gem_id if current is not None else None,
                    temporary=(
                        current.temporary if current is not None else False
                    ),
                    updated_at=_timestamp(),
                )
            )

    async def restore_all(self) -> None:
        """Rebuild all in-memory sessions from their persisted metadata."""

        async with self._lock:
            records = await self._dao.list_all()
            restored = {
                record.chat_id: self._start_chat(record) for record in records
            }
            self._sessions = restored

    def _start_chat(self, record: StoredChatSession | None) -> ChatSession:
        """Construct a ChatSession through the service's sole client."""

        return self._service.client.start_chat(
            metadata=record.metadata if record is not None else None,
            cid=(record.cid or "") if record is not None else "",
            model=record.model if record is not None else None,
            gem=record.gem_id if record is not None else None,
        )


def _to_state(
    chat_id: int,
    record: StoredChatSession | None,
) -> ChatState:
    if record is None:
        return ChatState(
            chat_id=chat_id,
            cid=None,
            model=None,
            gem_id=None,
            temporary=False,
            updated_at=None,
        )
    return ChatState(
        chat_id=record.chat_id,
        cid=record.cid,
        model=record.model,
        gem_id=record.gem_id,
        temporary=record.temporary,
        updated_at=record.updated_at,
    )


def _updated_record(
    chat_id: int,
    current: StoredChatSession | None,
    *,
    model: str | None | object = _UNSET,
    gem_id: str | None | object = _UNSET,
    temporary: bool | object = _UNSET,
) -> StoredChatSession:
    previous_model = current.model if current is not None else None
    previous_gem_id = current.gem_id if current is not None else None
    previous_temporary = current.temporary if current is not None else False
    return StoredChatSession(
        chat_id=chat_id,
        cid=current.cid if current is not None else None,
        metadata=current.metadata if current is not None else None,
        model=(
            previous_model
            if model is _UNSET
            else cast(str | None, model)
        ),
        gem_id=(
            previous_gem_id
            if gem_id is _UNSET
            else cast(str | None, gem_id)
        ),
        temporary=(
            previous_temporary
            if temporary is _UNSET
            else cast(bool, temporary)
        ),
        updated_at=_timestamp(),
    )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()
