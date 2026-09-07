"""Mock-only tests for the persistent Gemini ChatSession registry."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gemini_tg_bot.gemini.sessions import ChatSessionRegistry, ChatState
from gemini_tg_bot.storage.db import Database


def _service_with_client(client: object) -> SimpleNamespace:
    return SimpleNamespace(client=client)


async def test_unknown_chat_has_default_state_and_creates_one_session(
    tmp_path: Path,
) -> None:
    client = MagicMock()
    session = SimpleNamespace(cid="", metadata=None)
    client.start_chat.return_value = session

    async with Database(tmp_path / "bot.sqlite3") as database:
        registry = ChatSessionRegistry(  # type: ignore[arg-type]
            _service_with_client(client),
            database,
        )

        assert await registry.get_state(-1001) == ChatState(
            chat_id=-1001,
            cid=None,
            model=None,
            gem_id=None,
            temporary=False,
            updated_at=None,
        )
        assert await registry.get_or_create(-1001) is session
        assert await registry.get_or_create(-1001) is session

    client.start_chat.assert_called_once_with(
        metadata=None,
        cid="",
        model=None,
        gem=None,
    )


async def test_settings_are_per_chat_and_rebuild_session_with_context(
    tmp_path: Path,
) -> None:
    sessions = [
        SimpleNamespace(cid="cid-one", metadata=["m0", None, "m2"]),
        SimpleNamespace(cid="cid-one", metadata=["m0", None, "m2"]),
        SimpleNamespace(cid="cid-two", metadata=None),
    ]
    client = MagicMock()
    client.start_chat.side_effect = sessions

    async with Database(tmp_path / "bot.sqlite3") as database:
        registry = ChatSessionRegistry(  # type: ignore[arg-type]
            _service_with_client(client),
            database,
        )
        first = await registry.get_or_create(1)
        await registry.persist(1, first)  # type: ignore[arg-type]

        await registry.set_model(1, "model-selected-at-runtime")
        await registry.set_gem(1, "gem-selected-at-runtime")
        await registry.set_temporary(1, True)

        rebuilt = await registry.get_or_create(1)
        other = await registry.get_or_create(2)

        assert rebuilt is sessions[1]
        assert other is sessions[2]
        state = await registry.get_state(1)
        assert state.cid == "cid-one"
        assert state.model == "model-selected-at-runtime"
        assert state.gem_id == "gem-selected-at-runtime"
        assert state.temporary is True

    assert client.start_chat.call_args_list[1].kwargs == {
        "metadata": ["m0", None, "m2"],
        "cid": "cid-one",
        "model": "model-selected-at-runtime",
        "gem": "gem-selected-at-runtime",
    }
    assert client.start_chat.call_args_list[2].kwargs == {
        "metadata": None,
        "cid": "",
        "model": None,
        "gem": None,
    }


async def test_reset_discards_conversation_and_preserves_settings(
    tmp_path: Path,
) -> None:
    old_session = SimpleNamespace(
        cid="cid-before-reset",
        metadata=["before", None, "reset"],
    )
    new_session = SimpleNamespace(cid="", metadata=None)
    client = MagicMock()
    client.start_chat.side_effect = [old_session, new_session]

    async with Database(tmp_path / "bot.sqlite3") as database:
        registry = ChatSessionRegistry(  # type: ignore[arg-type]
            _service_with_client(client),
            database,
        )
        await registry.set_model(7, "chosen-model")
        await registry.set_gem(7, "chosen-gem")
        await registry.set_temporary(7, True)
        assert await registry.get_or_create(7) is old_session
        await registry.persist(7, old_session)  # type: ignore[arg-type]

        await registry.reset(7)

        state = await registry.get_state(7)
        assert state.cid is None
        assert state.model == "chosen-model"
        assert state.gem_id == "chosen-gem"
        assert state.temporary is True
        assert await registry.get_or_create(7) is new_session

    assert client.start_chat.call_args_list[-1].kwargs == {
        "metadata": None,
        "cid": "",
        "model": "chosen-model",
        "gem": "chosen-gem",
    }


class _ContinuingSession:
    def __init__(
        self,
        *,
        metadata: list[str | None] | None,
        cid: str,
        model: str | None,
        gem: str | None,
    ) -> None:
        self.metadata = metadata
        self.cid = cid
        self.model = model
        self.gem = gem
        self.send_message = AsyncMock(side_effect=self._continue)

    async def _continue(self, prompt: str) -> str:
        assert prompt == "continue after restart"
        assert self.cid == "persisted-cid"
        assert self.metadata == ["slot-0", None, "slot-2", None]
        self.cid = "continued-cid"
        self.metadata = ["slot-0", None, "continued", None]
        return "continued"


async def test_restart_restores_sqlite_metadata_and_continues_conversation(
    tmp_path: Path,
) -> None:
    """A fresh registry resumes the exact ordered upstream session context."""

    database_path = tmp_path / "bot.sqlite3"
    first_client = MagicMock()
    original = SimpleNamespace(
        cid="persisted-cid",
        metadata=["slot-0", None, "slot-2", None],
    )
    first_client.start_chat.return_value = original

    async with Database(database_path) as first_database:
        first_registry = ChatSessionRegistry(  # type: ignore[arg-type]
            _service_with_client(first_client),
            first_database,
        )
        await first_registry.set_model(99, "persisted-model")
        await first_registry.set_gem(99, "persisted-gem")
        await first_registry.set_temporary(99, True)
        assert await first_registry.get_or_create(99) is original
        await first_registry.persist(99, original)  # type: ignore[arg-type]

        async with first_database.connection.execute(
            "SELECT metadata_json FROM chat_sessions WHERE chat_id = 99"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert json.loads(row["metadata_json"]) == [
            "slot-0",
            None,
            "slot-2",
            None,
        ]

    restored_sessions: list[_ContinuingSession] = []

    def start_chat(**kwargs: object) -> _ContinuingSession:
        restored = _ContinuingSession(**kwargs)  # type: ignore[arg-type]
        restored_sessions.append(restored)
        return restored

    restarted_client = MagicMock()
    restarted_client.start_chat.side_effect = start_chat
    async with Database(database_path) as restarted_database:
        restarted_registry = ChatSessionRegistry(  # type: ignore[arg-type]
            _service_with_client(restarted_client),
            restarted_database,
        )

        await restarted_registry.restore_all()
        restored = await restarted_registry.get_or_create(99)
        assert restored is restored_sessions[0]
        assert await restored.send_message("continue after restart") == "continued"
        await restarted_registry.persist(99, restored)  # type: ignore[arg-type]

        state = await restarted_registry.get_state(99)
        assert state.cid == "continued-cid"
        assert state.model == "persisted-model"
        assert state.gem_id == "persisted-gem"
        assert state.temporary is True
        async with restarted_database.connection.execute(
            "SELECT metadata_json FROM chat_sessions WHERE chat_id = 99"
        ) as cursor:
            continued_row = await cursor.fetchone()
        assert continued_row is not None
        assert json.loads(continued_row["metadata_json"]) == [
            "slot-0",
            None,
            "continued",
            None,
        ]

    restarted_client.start_chat.assert_called_once_with(
        metadata=["slot-0", None, "slot-2", None],
        cid="persisted-cid",
        model="persisted-model",
        gem="persisted-gem",
    )
