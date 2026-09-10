"""Mock-only tests for the persistent Gemini ChatSession registry."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

from gemini_tg_bot.gemini.sessions import ChatSessionRegistry, ChatState
from gemini_tg_bot.storage.db import Database
from gemini_tg_bot.storage.models import GroupThread, GroupThreadDAO


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
            extended_thinking=False,
            language=None,
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


async def test_persist_preserves_enabled_extended_thinking(
    tmp_path: Path,
) -> None:
    """A successful response must not turn off the persisted /think switch."""

    registry_client = MagicMock()
    session = SimpleNamespace(cid="response-cid", metadata=["response"])

    async with Database(tmp_path / "bot.sqlite3") as database:
        registry = ChatSessionRegistry(  # type: ignore[arg-type]
            _service_with_client(registry_client),
            database,
        )
        await registry.set_extended_thinking(1, True)

        await registry.persist(1, session)  # type: ignore[arg-type]

        assert (await registry.get_state(1)).extended_thinking is True


async def test_persist_preserves_every_setting_and_updates_context(
    tmp_path: Path,
) -> None:
    """Guard the whole class of bugs where persist omits a settings field.

    Every configurable field is set together so rebuilding a stored record by
    enumerating only some settings cannot silently pass this regression test.
    """

    registry_client = MagicMock()
    session = SimpleNamespace(
        cid="updated-cid",
        metadata=["updated", None, "metadata"],
    )

    async with Database(tmp_path / "bot.sqlite3") as database:
        registry = ChatSessionRegistry(  # type: ignore[arg-type]
            _service_with_client(registry_client),
            database,
        )
        await registry.set_model(2, "configured-model")
        await registry.set_gem(2, "configured-gem")
        await registry.set_temporary(2, True)
        await registry.set_extended_thinking(2, True)
        await registry.set_language(2, "zh-hant")

        await registry.persist(2, session)  # type: ignore[arg-type]

        state = await registry.get_state(2)
        assert state.cid == "updated-cid"
        assert state.model == "configured-model"
        assert state.gem_id == "configured-gem"
        assert state.temporary is True
        assert state.extended_thinking is True
        assert state.language == "zh-hant"

        async with database.connection.execute(
            "SELECT metadata_json FROM chat_sessions WHERE chat_id = 2"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert json.loads(row["metadata_json"]) == [
            "updated",
            None,
            "metadata",
        ]


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
        await registry.set_language(7, "zh-hant")
        assert await registry.get_or_create(7) is old_session
        await registry.persist(7, old_session)  # type: ignore[arg-type]

        await registry.reset(7)

        state = await registry.get_state(7)
        assert state.cid is None
        assert state.model == "chosen-model"
        assert state.gem_id == "chosen-gem"
        assert state.temporary is True
        assert state.language == "zh-hant"
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


async def test_group_sessions_are_fresh_and_ignore_saved_chat_settings(
    tmp_path: Path,
) -> None:
    """Each /gemini thread must be isolated from private-style chat state."""

    resolved_model = object()
    first = SimpleNamespace(cid="first-cid", metadata=["first", None])
    second = SimpleNamespace(cid="second-cid", metadata=["second", None])
    client = MagicMock()
    client.resolve_model.return_value = resolved_model
    client.start_chat.side_effect = [first, second]

    async with Database(tmp_path / "bot.sqlite3") as database:
        registry = ChatSessionRegistry(  # type: ignore[arg-type]
            _service_with_client(client),
            database,
            group_model="configured-group-model",
        )
        await registry.set_model(-1001, "saved-private-model")
        await registry.set_gem(-1001, "saved-private-gem")
        await registry.set_temporary(-1001, True)
        await registry.set_extended_thinking(-1001, True)
        await registry.set_language(-1001, "zh-hant")

        assert await registry.start_group_session(-1001) == (first, False)
        assert await registry.start_group_session(-1001) == (second, False)

    assert client.resolve_model.call_args_list == [
        call("configured-group-model"),
        call("configured-group-model"),
    ]
    assert client.start_chat.call_args_list == [
        call(metadata=None, cid="", model=resolved_model, gem=None),
        call(metadata=None, cid="", model=resolved_model, gem=None),
    ]


async def test_group_model_comes_from_service_settings(
    tmp_path: Path,
) -> None:
    """Production construction must honor env-backed settings without rewiring."""

    resolved_model = object()
    session = SimpleNamespace(cid="", metadata=None)
    client = MagicMock()
    client.resolve_model.return_value = resolved_model
    client.start_chat.return_value = session
    service = SimpleNamespace(
        client=client,
        _settings=SimpleNamespace(
            group_model="settings-group-model",
            group_thread_retention_days=12,
            group_thread_max_per_chat=34,
        ),
    )
    async with Database(tmp_path / "bot.sqlite3") as database:
        registry = ChatSessionRegistry(  # type: ignore[arg-type]
            service,
            database,
        )

        assert registry.group_model_name == "settings-group-model"
        assert await registry.start_group_session(-1001) == (session, False)

    client.resolve_model.assert_called_once_with("settings-group-model")
    client.start_chat.assert_called_once_with(
        metadata=None,
        cid="",
        model=resolved_model,
        gem=None,
    )


async def test_group_reply_context_survives_registry_and_database_restart(
    tmp_path: Path,
) -> None:
    """A service restart must not break replies to already-sent bot answers."""

    path = tmp_path / "bot.sqlite3"
    initial_client = MagicMock()
    initial_client.resolve_model.return_value = object()
    initial_session = SimpleNamespace(
        cid="persisted-group-cid",
        metadata=["slot-0", None, "slot-2", None],
    )
    async with Database(path) as database:
        registry = ChatSessionRegistry(  # type: ignore[arg-type]
            _service_with_client(initial_client),
            database,
        )
        await registry.persist_group_session(-1001, 401, initial_session)

    restored_client = MagicMock()
    restored_model = object()
    restored_session = SimpleNamespace(cid="restored", metadata=None)
    restored_client.resolve_model.return_value = restored_model
    restored_client.start_chat.return_value = restored_session
    async with Database(path) as database:
        registry = ChatSessionRegistry(  # type: ignore[arg-type]
            _service_with_client(restored_client),
            database,
        )
        assert await registry.start_group_session(-1001, 401) == (
            restored_session,
            True,
        )

    restored_client.start_chat.assert_called_once_with(
        metadata=["slot-0", None, "slot-2", None],
        cid="persisted-group-cid",
        model=restored_model,
        gem=None,
    )


async def test_invalid_group_model_falls_back_to_account_default(
    tmp_path: Path,
    caplog,
) -> None:
    """A renamed upstream model must not disable otherwise healthy groups."""

    client = MagicMock()
    client.resolve_model.side_effect = ValueError("synthetic invalid model")
    session = SimpleNamespace(cid="", metadata=None)
    client.start_chat.return_value = session
    async with Database(tmp_path / "bot.sqlite3") as database:
        registry = ChatSessionRegistry(  # type: ignore[arg-type]
            _service_with_client(client),
            database,
            group_model="removed-model",
        )
        with caplog.at_level("WARNING"):
            assert await registry.start_group_session(-1001) == (session, False)

    client.resolve_model.assert_called_once_with("removed-model")
    client.start_chat.assert_called_once_with(
        metadata=None,
        cid="",
        model=None,
        gem=None,
    )
    assert "using account default" in caplog.text


async def test_restore_all_cleans_expired_and_excess_group_threads(
    tmp_path: Path,
) -> None:
    """Startup cleanup bounds old thread state even during quiet deployments."""

    now = datetime(2026, 9, 10, tzinfo=UTC)
    client = MagicMock()
    async with Database(tmp_path / "bot.sqlite3") as database:
        dao = GroupThreadDAO(database.connection)
        for message_id, created_at in (
            (1, "2026-07-01T00:00:00+00:00"),
            (2, "2026-09-01T00:00:00+00:00"),
            (3, "2026-09-02T00:00:00+00:00"),
            (4, "2026-09-03T00:00:00+00:00"),
        ):
            await dao.upsert(
                GroupThread(
                    chat_id=-1001,
                    bot_message_id=message_id,
                    created_at=created_at,
                )
            )
        registry = ChatSessionRegistry(  # type: ignore[arg-type]
            _service_with_client(client),
            database,
            group_thread_retention_days=30,
            group_thread_max_per_chat=2,
            now=lambda: now,
        )

        await registry.restore_all()

        assert [
            row.bot_message_id for row in await dao.list_for_chat(-1001)
        ] == [3, 4]


async def test_start_new_preserves_private_settings_and_replaces_context(
    tmp_path: Path,
) -> None:
    """Adding /gemini must keep private preferences and continuous-chat rules."""

    client = MagicMock()
    fresh = SimpleNamespace(cid="", metadata=None)
    client.start_chat.return_value = fresh
    async with Database(tmp_path / "bot.sqlite3") as database:
        registry = ChatSessionRegistry(  # type: ignore[arg-type]
            _service_with_client(client),
            database,
        )
        await registry.set_model(1, "private-model")
        await registry.set_gem(1, "private-gem")
        await registry.set_temporary(1, True)
        await registry.set_extended_thinking(1, True)
        await registry.set_language(1, "zh-hant")

        assert await registry.start_new(1) is fresh
        state = await registry.get_state(1)
        assert state.cid is None
        assert state.model == "private-model"
        assert state.gem_id == "private-gem"
        assert state.temporary is True
        assert state.extended_thinking is True
        assert state.language == "zh-hant"

    client.start_chat.assert_called_once_with(
        metadata=None,
        cid="",
        model="private-model",
        gem="private-gem",
    )
