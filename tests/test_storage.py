from __future__ import annotations

import json

import pytest

from gemini_tg_bot.storage.db import Database
from gemini_tg_bot.storage.models import (
    ChatSession,
    ChatSessionDAO,
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


@pytest.mark.asyncio
async def test_migration_is_automatic_complete_and_idempotent(tmp_path) -> None:
    async with Database(tmp_path / "bot.sqlite3") as database:
        first = await _schema_snapshot(database)

        await database.migrate()
        await database.migrate()

        assert await _schema_snapshot(database) == first
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
