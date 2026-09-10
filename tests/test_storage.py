from __future__ import annotations

import json
import sqlite3

import pytest

from gemini_tg_bot.storage.db import LATEST_SCHEMA_VERSION, Database
from gemini_tg_bot.storage.models import (
    SCHEMA_SQL,
    AdminNotificationDAO,
    ChatSession,
    ChatSessionDAO,
    GroupThread,
    GroupThreadDAO,
    ResearchTask,
    ResearchTaskDAO,
    UsageLog,
    UsageLogDAO,
    deserialize_metadata,
    serialize_metadata,
)


EXPECTED_SCHEMA = {
    "chat_sessions": [
        ("chat_id", "INTEGER", 0, None, 1),
        ("cid", "TEXT", 0, None, 0),
        ("metadata_json", "TEXT", 0, None, 0),
        ("model", "TEXT", 0, None, 0),
        ("gem_id", "TEXT", 0, None, 0),
        ("temporary", "INTEGER", 0, "0", 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("extended_thinking", "INTEGER", 0, "0", 0),
        ("language", "TEXT", 0, None, 0),
    ],
    "research_tasks": [
        ("task_id", "TEXT", 0, None, 1),
        ("chat_id", "INTEGER", 1, None, 0),
        ("cid", "TEXT", 0, None, 0),
        ("research_id", "TEXT", 0, None, 0),
        ("prompt", "TEXT", 1, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("result_path", "TEXT", 0, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("plan_json", "TEXT", 0, None, 0),
    ],
    "admin_notifications": [
        ("fingerprint", "TEXT", 0, None, 1),
        ("sent_at", "REAL", 1, None, 0),
    ],
    "usage_log": [
        ("id", "INTEGER", 0, None, 1),
        ("user_id", "INTEGER", 1, None, 0),
        ("chat_id", "INTEGER", 1, None, 0),
        ("command", "TEXT", 0, None, 0),
        ("model", "TEXT", 0, None, 0),
        ("ok", "INTEGER", 1, None, 0),
        ("error_kind", "TEXT", 0, None, 0),
        ("latency_ms", "INTEGER", 0, None, 0),
        ("created_at", "TEXT", 1, None, 0),
    ],
    "telegram_user_access": [
        ("user_id", "INTEGER", 0, None, 1),
        ("allowed", "INTEGER", 1, None, 0),
    ],
    "telegram_chat_access": [
        ("chat_id", "INTEGER", 0, None, 1),
        ("allowed", "INTEGER", 1, None, 0),
    ],
    "group_threads": [
        ("chat_id", "INTEGER", 1, None, 1),
        ("bot_message_id", "INTEGER", 1, None, 2),
        ("cid", "TEXT", 0, None, 0),
        ("metadata_json", "TEXT", 0, None, 0),
        ("created_at", "TEXT", 1, None, 0),
    ],
}


async def _schema_snapshot(database: Database) -> list[tuple[str, str, str, str]]:
    async with database.connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ) as cursor:
        rows = await cursor.fetchall()
    return [tuple(row) for row in rows]


async def _user_version(database: Database) -> int:
    async with database.connection.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.asyncio
async def test_migration_is_automatic_complete_and_idempotent(tmp_path) -> None:
    async with Database(tmp_path / "bot.sqlite3") as database:
        first = await _schema_snapshot(database)
        assert await _user_version(database) == LATEST_SCHEMA_VERSION

        await database.migrate()
        await database.migrate()

        assert await _schema_snapshot(database) == first
        assert await _user_version(database) == LATEST_SCHEMA_VERSION
        assert {row[1] for row in first if row[0] == "table"} == set(
            EXPECTED_SCHEMA
        )
        for table, expected in EXPECTED_SCHEMA.items():
            async with database.connection.execute(
                f"PRAGMA table_info({table})"
            ) as cursor:
                columns = await cursor.fetchall()
            assert [
                (
                    column["name"],
                    column["type"],
                    column["notnull"],
                    column["dflt_value"],
                    column["pk"],
                )
                for column in columns
            ] == expected


@pytest.mark.asyncio
async def test_migration_preserves_rows_from_version_1_database(
    tmp_path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            """
            INSERT INTO chat_sessions (
                chat_id, cid, metadata_json, model, gem_id, temporary, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                101,
                "legacy-cid",
                '["first", null, "third"]',
                "legacy-model",
                "legacy-gem",
                1,
                "2026-09-07T01:02:03+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO research_tasks (
                task_id, chat_id, cid, research_id, prompt, status,
                result_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-task",
                101,
                "legacy-research-cid",
                "legacy-research-id",
                "preserve this prompt",
                "running",
                None,
                "2026-09-07T02:00:00+00:00",
                "2026-09-07T02:01:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO usage_log (
                user_id, chat_id, command, model, ok, error_kind, latency_ms,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                303,
                101,
                "research",
                "legacy-model",
                1,
                None,
                125,
                "2026-09-07T02:04:00+00:00",
            ),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()

    async with Database(database_path) as database:
        assert await _user_version(database) == LATEST_SCHEMA_VERSION
        async with database.connection.execute(
            "SELECT prompt, status, plan_json FROM research_tasks"
        ) as cursor:
            research_row = await cursor.fetchone()
        assert research_row is not None
        assert tuple(research_row) == ("preserve this prompt", "running", None)

        async with database.connection.execute(
            "SELECT cid, metadata_json, temporary FROM chat_sessions"
        ) as cursor:
            chat_row = await cursor.fetchone()
        assert chat_row is not None
        assert tuple(chat_row) == (
            "legacy-cid",
            '["first", null, "third"]',
            1,
        )

        async with database.connection.execute(
            "SELECT user_id, chat_id, command, ok FROM usage_log"
        ) as cursor:
            usage_row = await cursor.fetchone()
        assert usage_row is not None
        assert tuple(usage_row) == (303, 101, "research", 1)

        first = await _schema_snapshot(database)
        await database.migrate()
        await database.migrate()
        assert await _schema_snapshot(database) == first
        assert await _user_version(database) == LATEST_SCHEMA_VERSION


def test_metadata_json_preserves_order_and_none_slots() -> None:
    metadata = ["conversation", None, "response", None, "tail"]

    encoded = serialize_metadata(metadata)

    assert encoded is not None
    assert json.loads(encoded) == metadata
    assert deserialize_metadata(encoded) == metadata
    assert serialize_metadata(None) is None
    assert deserialize_metadata(None) is None


@pytest.mark.asyncio
async def test_chat_session_dao_round_trip_and_update(tmp_path) -> None:
    async with Database(tmp_path / "bot.sqlite3") as database:
        dao = ChatSessionDAO(database.connection)
        original = ChatSession(
            chat_id=101,
            cid="cid-1",
            metadata=["first", None, "third"],
            model="dynamic-model-name",
            gem_id="gem-1",
            temporary=True,
            updated_at="2026-09-07T01:02:03+00:00",
        )
        await dao.upsert(original)

        assert await dao.get(101) == original
        async with database.connection.execute(
            "SELECT metadata_json FROM chat_sessions WHERE chat_id = 101"
        ) as cursor:
            stored = await cursor.fetchone()
        assert stored is not None
        assert json.loads(stored["metadata_json"]) == ["first", None, "third"]

        updated = ChatSession(
            chat_id=101,
            cid="cid-2",
            metadata=[None, "kept-at-index-one"],
            updated_at="2026-09-07T01:03:00+00:00",
        )
        await dao.upsert(updated)

        assert await dao.list_all() == [updated]
        assert await dao.delete(101) is True
        assert await dao.delete(101) is False
        assert await dao.get(101) is None


@pytest.mark.asyncio
async def test_group_thread_dao_survives_reopen_with_null_metadata_slots(
    tmp_path,
) -> None:
    """Replies after a process restart need the exact positional context."""

    path = tmp_path / "bot.sqlite3"
    original = GroupThread(
        chat_id=-1001,
        bot_message_id=51,
        cid="thread-cid",
        metadata=["first", None, "third", None],
        created_at="2026-09-01T00:00:00+00:00",
    )
    async with Database(path) as database:
        await GroupThreadDAO(database.connection).upsert(original)

    async with Database(path) as database:
        dao = GroupThreadDAO(database.connection)
        assert await dao.get(-1001, 51) == original


@pytest.mark.asyncio
async def test_group_thread_cleanup_enforces_age_and_per_chat_limits(
    tmp_path,
) -> None:
    """Bounded retention prevents busy groups from growing SQLite forever."""

    async with Database(tmp_path / "bot.sqlite3") as database:
        dao = GroupThreadDAO(database.connection)
        rows = (
            GroupThread(
                chat_id=-1001,
                bot_message_id=1,
                created_at="2026-07-01T00:00:00+00:00",
            ),
            GroupThread(
                chat_id=-1001,
                bot_message_id=2,
                created_at="2026-09-01T00:00:00+00:00",
            ),
            GroupThread(
                chat_id=-1001,
                bot_message_id=3,
                created_at="2026-09-02T00:00:00+00:00",
            ),
            GroupThread(
                chat_id=-1001,
                bot_message_id=4,
                created_at="2026-09-03T00:00:00+00:00",
            ),
            GroupThread(
                chat_id=-2002,
                bot_message_id=2,
                created_at="2026-09-01T00:00:00+00:00",
            ),
        )
        for row in rows:
            await dao.upsert(row)

        assert await dao.cleanup(
            older_than="2026-08-11T00:00:00+00:00",
            max_per_chat=2,
        ) == 2
        assert [
            row.bot_message_id for row in await dao.list_for_chat(-1001)
        ] == [3, 4]
        assert [
            row.bot_message_id for row in await dao.list_for_chat(-2002)
        ] == [2]


@pytest.mark.asyncio
async def test_research_and_usage_daos(tmp_path) -> None:
    async with Database(tmp_path / "bot.sqlite3") as database:
        research_dao = ResearchTaskDAO(database.connection)
        usage_dao = UsageLogDAO(database.connection)
        running = ResearchTask(
            task_id="task-1",
            chat_id=202,
            cid="cid-research",
            research_id="research-1",
            prompt="Research a topic",
            status="running",
            created_at="2026-09-07T02:00:00+00:00",
            updated_at="2026-09-07T02:01:00+00:00",
        )
        await research_dao.upsert(running)
        await research_dao.upsert(
            ResearchTask(
                task_id="task-2",
                chat_id=202,
                prompt="Already done",
                status="done",
                result_path="/tmp/result.txt",
                created_at="2026-09-07T02:02:00+00:00",
                updated_at="2026-09-07T02:03:00+00:00",
            )
        )

        assert await research_dao.get("task-1") == running
        assert await research_dao.list_running() == [running]
        assert [task.task_id for task in await research_dao.list_for_chat(202)] == [
            "task-1",
            "task-2",
        ]

        entry = UsageLog(
            user_id=303,
            chat_id=202,
            command="research",
            model="dynamic-model-name",
            ok=False,
            error_kind="rate_limit",
            latency_ms=125,
            created_at="2026-09-07T02:04:00+00:00",
        )
        row_id = await usage_dao.add(entry)
        assert row_id == 1
        assert await usage_dao.list_for_chat(202) == [
            UsageLog(
                id=1,
                user_id=303,
                chat_id=202,
                command="research",
                model="dynamic-model-name",
                ok=False,
                error_kind="rate_limit",
                latency_ms=125,
                created_at="2026-09-07T02:04:00+00:00",
            )
        ]


@pytest.mark.asyncio
async def test_repeated_page_is_suppressed_across_process_restarts(
    tmp_path,
) -> None:
    """A supervisor restarting a process must not re-page the administrator.

    Each DAO here stands for a fresh process: the in-memory guard inside the
    service cannot see the previous run, so suppression has to be durable or a
    crash loop floods the chat and buries the /setcookie prompt.
    """

    message = "認證失效，請 /setcookie"
    path = tmp_path / "bot.sqlite3"
    sent = 0

    for restart in range(21):
        async with Database(path) as database:
            dao = AdminNotificationDAO(database.connection)
            if await dao.claim(message, now=100.0 + restart * 10, cooldown_sec=900):
                sent += 1

    assert sent == 1


@pytest.mark.asyncio
async def test_page_is_sent_again_once_the_cooldown_lapses(tmp_path) -> None:
    """Suppression is bounded, so an unfixed problem is raised again."""

    message = "認證失效，請 /setcookie"
    async with Database(tmp_path / "bot.sqlite3") as database:
        dao = AdminNotificationDAO(database.connection)

        assert await dao.claim(message, now=0.0, cooldown_sec=900)
        assert not await dao.claim(message, now=899.0, cooldown_sec=900)
        assert await dao.claim(message, now=900.0, cooldown_sec=900)
        assert await dao.claim("something else", now=900.0, cooldown_sec=900)


@pytest.mark.asyncio
async def test_notification_text_is_never_stored(tmp_path) -> None:
    """Only a digest is persisted, so administrator input cannot leak here."""

    message = "認證失效，請 /setcookie FAKE_1PSID_FOR_TEST"
    async with Database(tmp_path / "bot.sqlite3") as database:
        dao = AdminNotificationDAO(database.connection)
        await dao.claim(message, now=0.0, cooldown_sec=900)

        async with database.connection.execute(
            "SELECT fingerprint FROM admin_notifications"
        ) as cursor:
            rows = await cursor.fetchall()

    assert len(rows) == 1
    assert "FAKE_1PSID_FOR_TEST" not in rows[0][0]
    assert len(rows[0][0]) == 64


@pytest.mark.asyncio
async def test_migrated_database_matches_a_fresh_one(tmp_path) -> None:
    """Fresh and upgraded databases must expose identical final columns.

    Declaring a migrated column anywhere but last in ``SCHEMA_SQL`` leaves a
    fresh database ordered differently from an upgraded one, which only shows up
    against real data. The v7 chat-access and v8 group-thread tables need the
    same parity guarantee.
    """

    async with Database(tmp_path / "fresh.sqlite3") as database:
        fresh = await _column_names(database, "chat_sessions")
        fresh_chat_access = await _column_names(
            database,
            "telegram_chat_access",
        )
        fresh_group_threads = await _column_names(database, "group_threads")

    legacy = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(legacy)
    connection.executescript(
        """
        CREATE TABLE chat_sessions (
            chat_id       INTEGER PRIMARY KEY,
            cid           TEXT,
            metadata_json TEXT,
            model         TEXT,
            gem_id        TEXT,
            temporary     INTEGER DEFAULT 0,
            updated_at    TEXT NOT NULL
        );
        PRAGMA user_version = 4;
        """
    )
    connection.commit()
    connection.close()

    async with Database(legacy) as database:
        migrated = await _column_names(database, "chat_sessions")
        migrated_chat_access = await _column_names(
            database,
            "telegram_chat_access",
        )
        migrated_group_threads = await _column_names(database, "group_threads")

    assert migrated == fresh
    assert fresh[-1] == "language"
    assert migrated_chat_access == fresh_chat_access
    assert fresh_chat_access == ["chat_id", "allowed"]
    assert migrated_group_threads == fresh_group_threads
    assert fresh_group_threads == [
        "chat_id",
        "bot_message_id",
        "cid",
        "metadata_json",
        "created_at",
    ]


async def _column_names(database: Database, table: str) -> list[str]:
    async with database.connection.execute(
        f"PRAGMA table_info({table})"
    ) as cursor:
        return [row[1] for row in await cursor.fetchall()]
