"""Per-Telegram-chat Gemini session registry backed by SQLite."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from gemini_webapi import ChatSession

from gemini_tg_bot.config import (
    DEFAULT_GROUP_MODEL,
    DEFAULT_GROUP_THREAD_MAX_PER_CHAT,
    DEFAULT_GROUP_THREAD_RETENTION_DAYS,
)
from gemini_tg_bot.gemini.service import GeminiService
from gemini_tg_bot.storage.db import Database
from gemini_tg_bot.storage.models import (
    ChatSession as StoredChatSession,
    ChatSessionDAO,
    GroupThread,
    GroupThreadDAO,
)


_UNSET = object()
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatState:
    """A read-only snapshot of one Telegram chat's persisted settings."""

    chat_id: int
    cid: str | None
    model: str | None
    gem_id: str | None
    temporary: bool
    extended_thinking: bool
    language: str | None
    updated_at: str | None


class ChatSessionRegistry:
    """Map Telegram chat IDs to Gemini sessions and persist their metadata."""

    def __init__(
        self,
        service: GeminiService,
        database: Database,
        *,
        group_model: str | None = None,
        group_thread_retention_days: int | None = None,
        group_thread_max_per_chat: int | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._dao = ChatSessionDAO(database.connection)
        self._group_threads = GroupThreadDAO(database.connection)
        self._sessions: dict[int, ChatSession] = {}
        self._lock = asyncio.Lock()
        settings = getattr(service, "_settings", None)
        self._group_model = group_model or getattr(
            settings,
            "group_model",
            DEFAULT_GROUP_MODEL,
        )
        self._group_thread_retention_days = (
            group_thread_retention_days
            if group_thread_retention_days is not None
            else getattr(
                settings,
                "group_thread_retention_days",
                DEFAULT_GROUP_THREAD_RETENTION_DAYS,
            )
        )
        self._group_thread_max_per_chat = (
            group_thread_max_per_chat
            if group_thread_max_per_chat is not None
            else getattr(
                settings,
                "group_thread_max_per_chat",
                DEFAULT_GROUP_THREAD_MAX_PER_CHAT,
            )
        )
        if not self._group_model:
            raise ValueError("group_model must not be empty")
        if self._group_thread_retention_days <= 0:
            raise ValueError("group_thread_retention_days must be positive")
        if self._group_thread_max_per_chat <= 0:
            raise ValueError("group_thread_max_per_chat must be positive")
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def group_model_name(self) -> str:
        """Return the configured group model name used for usage records."""

        return self._group_model

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
            if current is None:
                record = StoredChatSession(
                    chat_id=chat_id,
                    updated_at=_timestamp(),
                )
            else:
                record = dataclasses.replace(
                    current,
                    cid=None,
                    metadata=None,
                    updated_at=_timestamp(),
                )
            await self._dao.upsert(record)
            self._sessions.pop(chat_id, None)

    async def start_new(self, chat_id: int) -> ChatSession:
        """Start a fresh private conversation while preserving its settings."""

        async with self._lock:
            current = await self._dao.get(chat_id)
            if current is None:
                record = StoredChatSession(
                    chat_id=chat_id,
                    updated_at=_timestamp(self._now),
                )
            else:
                record = dataclasses.replace(
                    current,
                    cid=None,
                    metadata=None,
                    updated_at=_timestamp(self._now),
                )
            await self._dao.upsert(record)
            session = self._start_chat(record)
            self._sessions[chat_id] = session
            return session

    async def start_group_session(
        self,
        chat_id: int,
        bot_message_id: int | None = None,
    ) -> tuple[ChatSession, bool]:
        """Start a fixed-settings group session, restoring a known reply."""

        async with self._lock:
            thread = (
                await self._group_threads.get(chat_id, bot_message_id)
                if bot_message_id is not None
                else None
            )
            return self._start_group_chat(thread), thread is not None

    async def persist_group_session(
        self,
        chat_id: int,
        bot_message_id: int,
        session: ChatSession,
    ) -> None:
        """Attach a bot answer to the group conversation it continues."""

        metadata = session.metadata
        metadata_snapshot = None if metadata is None else list(metadata)
        thread = GroupThread(
            chat_id=chat_id,
            bot_message_id=bot_message_id,
            cid=session.cid,
            metadata=metadata_snapshot,
            created_at=_timestamp(self._now),
        )
        async with self._lock:
            await self._group_threads.upsert(thread)

    async def cleanup_group_threads(self) -> int:
        """Apply the configured age and per-chat limits to reply contexts."""

        cutoff = self._now().astimezone(UTC) - timedelta(
            days=self._group_thread_retention_days
        )
        async with self._lock:
            return await self._group_threads.cleanup(
                older_than=cutoff.isoformat(),
                max_per_chat=self._group_thread_max_per_chat,
            )

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

    async def set_extended_thinking(self, chat_id: int, enabled: bool) -> None:
        """Persist whether subsequent messages should use extended thinking."""

        async with self._lock:
            current = await self._dao.get(chat_id)
            await self._dao.upsert(
                _updated_record(chat_id, current, extended_thinking=enabled)
            )

    async def set_language(self, chat_id: int, language: str) -> None:
        """Persist an explicit interface language for this chat."""

        async with self._lock:
            current = await self._dao.get(chat_id)
            await self._dao.upsert(
                _updated_record(chat_id, current, language=language)
            )

    async def persist(self, chat_id: int, session: ChatSession) -> None:
        """Persist conversation identifiers without rebuilding chat settings.

        Existing records are derived with :func:`dataclasses.replace` rather
        than enumerating settings fields so newly added settings cannot be
        silently reset to their dataclass defaults during a response persist.
        """

        # Take a positional copy before awaiting SQLite so later mutations of
        # the upstream metadata list cannot change the snapshot being written.
        metadata = session.metadata
        metadata_snapshot = None if metadata is None else list(metadata)
        cid = session.cid

        async with self._lock:
            current = await self._dao.get(chat_id)
            if current is None:
                record = StoredChatSession(
                    chat_id=chat_id,
                    cid=cid,
                    metadata=metadata_snapshot,
                    updated_at=_timestamp(),
                )
            else:
                record = dataclasses.replace(
                    current,
                    cid=cid,
                    metadata=metadata_snapshot,
                    updated_at=_timestamp(),
                )
            await self._dao.upsert(record)

    async def restore_all(self) -> None:
        """Rebuild all in-memory sessions from their persisted metadata."""

        await self.cleanup_group_threads()
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

    def _start_group_chat(self, thread: GroupThread | None) -> ChatSession:
        """Construct one group session without consulting private-chat state."""

        try:
            model: Any = self._service.client.resolve_model(self._group_model)
            if model is None:
                raise ValueError("model resolver returned no model")
        except Exception as error:
            LOGGER.warning(
                "Unable to resolve group model %r (%s); using account default",
                self._group_model,
                type(error).__name__,
            )
            model = None
        return self._service.client.start_chat(
            metadata=thread.metadata if thread is not None else None,
            cid=(thread.cid or "") if thread is not None else "",
            model=model,
            gem=None,
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
            extended_thinking=False,
            language=None,
            updated_at=None,
        )
    return ChatState(
        chat_id=record.chat_id,
        cid=record.cid,
        model=record.model,
        gem_id=record.gem_id,
        temporary=record.temporary,
        extended_thinking=record.extended_thinking,
        language=record.language,
        updated_at=record.updated_at,
    )


def _updated_record(
    chat_id: int,
    current: StoredChatSession | None,
    *,
    model: str | None | object = _UNSET,
    gem_id: str | None | object = _UNSET,
    temporary: bool | object = _UNSET,
    extended_thinking: bool | object = _UNSET,
    language: str | None | object = _UNSET,
) -> StoredChatSession:
    previous_model = current.model if current is not None else None
    previous_gem_id = current.gem_id if current is not None else None
    previous_temporary = current.temporary if current is not None else False
    previous_thinking = (
        current.extended_thinking if current is not None else False
    )
    previous_language = current.language if current is not None else None
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
        extended_thinking=(
            previous_thinking
            if extended_thinking is _UNSET
            else cast(bool, extended_thinking)
        ),
        language=(
            previous_language
            if language is _UNSET
            else cast(str | None, language)
        ),
        updated_at=_timestamp(),
    )


def _timestamp(now: Callable[[], datetime] | None = None) -> str:
    current = now() if now is not None else datetime.now(UTC)
    return current.astimezone(UTC).isoformat()
