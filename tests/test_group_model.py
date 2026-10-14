"""Mock-only runtime group model, migration, and request snapshot regressions."""

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import MessageEntity

from gemini_tg_bot.gemini.service import DegradedReason, ServiceState, ServiceUnavailableError
from gemini_tg_bot.gemini.sessions import ChatSessionRegistry, GroupModelSettings
from gemini_tg_bot.gemini.thinking import MEDIUM_WIRE_VALUE, ThinkingLevel, _thinking_wire_value
from gemini_tg_bot.i18n import LANGUAGE_CHINESE, LANGUAGE_ENGLISH, translate
from gemini_tg_bot.storage.db import Database
from gemini_tg_bot.storage.models import GroupModelOverride, GroupModelOverrideDAO, UsageLogDAO
from gemini_tg_bot.telegram.handlers import (
    GROUP_MODEL_CALLBACK_PREFIX,
    GROUP_MODEL_LEVEL_CALLBACK_PREFIX,
    GROUP_MODEL_RESET_CALLBACK,
    GROUP_MODEL_RESET_CALLBACK_PREFIX,
    LANGUAGE_CALLBACK_PREFIX,
    MODEL_CALLBACK_PREFIX,
    MODEL_LEVEL_CALLBACK_PREFIX,
    PUBLIC_BOT_COMMANDS,
    _help_text,
)
from gemini_tg_bot.telegram.update_processor import LOCK_FREE_COMMANDS
from tests.test_concurrency import Harness


DEFAULT_MODEL = "FAKE_DEFAULT_MODEL"
NEW_MODEL = "FAKE_NEW_MODEL"
DEFAULTS = GroupModelSettings(DEFAULT_MODEL, ThinkingLevel.MEDIUM)


class FakeGroupSession:
    def __init__(self, harness, **kwargs):
        self.harness = harness
        self.cid = kwargs["cid"] or "FAKE_CID"
        self.metadata = kwargs["metadata"] or ["FAKE_METADATA"]
        self.model = kwargs["model"]
        self.calls = []
        self.wire_levels = []

    async def send_message_stream(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        self.wire_levels.append(_thinking_wire_value.get())
        self.harness.started.set()
        if self.harness.block_generation:
            await self.harness.release.wait()
        yield SimpleNamespace(text="FAKE_ANSWER", text_delta="FAKE_ANSWER", images=())

    async def send_message(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        self.wire_levels.append(_thinking_wire_value.get())
        return SimpleNamespace(text="FAKE_ANSWER", images=())


@pytest.fixture
async def h(tmp_path):
    async with Database(tmp_path / "FAKE_BOT.sqlite3") as database:
        instance = Harness(tmp_path, concurrency=1, slot_wait=1)
        instance.database = database
        instance.models = [
            SimpleNamespace(model_name=DEFAULT_MODEL, display_name="Default model", is_available=True),
            SimpleNamespace(model_name=NEW_MODEL, display_name="New model", is_available=True),
            SimpleNamespace(model_name="FAKE_UNAVAILABLE", display_name="Unavailable", is_available=False),
        ]
        instance.client.list_models.return_value = instance.models
        instance.client.resolve_model.side_effect = lambda name: next(
            (model for model in instance.models if model.model_name == name), None
        )
        instance.service.client = instance.client
        instance.service.accepting_requests = True
        instance.service._settings = SimpleNamespace(
            group_model=DEFAULT_MODEL, group_thinking_level="medium",
        )
        instance.sessions = ChatSessionRegistry(instance.service, database)
        await instance.sessions.load_group_model_override()
        instance.handlers._sessions = instance.sessions
        instance.handlers._database = database
        instance.handlers._usage_dao = UsageLogDAO(database.connection)
        instance.generated_sessions = []
        instance.started = asyncio.Event()
        instance.release = asyncio.Event()
        instance.block_generation = False

        def start_chat(**kwargs):
            session = FakeGroupSession(instance, **kwargs)
            instance.generated_sessions.append(session)
            return session

        instance.client.start_chat.side_effect = start_chat

        @asynccontextmanager
        async def prepare_upload(message, **kwargs):
            yield SimpleNamespace(prompt="FAKE_UPLOAD_PROMPT", files=["FAKE_UPLOAD"])

        instance.handlers._media = SimpleNamespace(
            prepare_upload=prepare_upload, record_upload=MagicMock(),
        )
        async with instance.application:
            yield instance
            instance.release.set()
            await asyncio.gather(*instance.tasks, return_exceptions=True)
        assert instance.errors == []


async def tap(h, data, *, user_id=1, chat_id=1):
    await h.application.process_update(h.update(chat_id, user_id=user_id, callback=data))
    assert h.errors == []


def group_update(h, path="command"):
    update = h.update(-100, "/gemini FAKE_PROMPT" if path == "command" else "FAKE_PROMPT",
                      photo=path in {"command_upload", "reply_upload"})
    if path == "command_upload":
        object.__setattr__(update.message, "caption", "/gemini FAKE_PROMPT")
        object.__setattr__(update.message, "caption_entities", (
            MessageEntity(MessageEntity.BOT_COMMAND, 0, len("/gemini")),
        ))
    elif path in {"reply", "reply_upload"}:
        object.__setattr__(update.message, "reply_to_message", h.bot.message(-100, "FAKE_PARENT", message_id=999))
    return update


@pytest.mark.parametrize("chat_id,user_id,key", [(10, 10, "admin.only"), (-100, 1, "command.private_only")])
async def test_group_model_access_changes_nothing(h, chat_id, user_id, key):
    """The global setting must be protected by both administrator and private-chat gates."""

    await h.application.process_update(h.update(chat_id, "/group_model", user_id=user_id))

    assert h.bot.sent == [(chat_id, translate(key, LANGUAGE_ENGLISH))]
    assert h.sessions.group_settings == DEFAULTS
    assert await GroupModelOverrideDAO(h.database.connection).get() is None
    h.service.execute.assert_not_awaited()


@pytest.mark.parametrize("language", [LANGUAGE_ENGLISH, LANGUAGE_CHINESE])
async def test_listing_is_dynamic_and_shows_source_and_reset(h, language):
    """Administrators need the current pair and available upstream choices in either language."""

    await h.sessions.set_language(1, language)
    sent = AsyncMock(wraps=h.bot.send_message)
    object.__setattr__(h.bot, "send_message", sent)

    await h.application.process_update(h.update(1, "/group_model", user_id=1))

    h.client.list_models.assert_called_once_with()
    assert h.bot.sent[0][1] == translate(
        "group_model.current", language, model="Default model", level="Medium",
        source=translate("group_model.source_default", language),
    )
    keyboard = sent.await_args.kwargs["reply_markup"].inline_keyboard
    assert [row[0].text for row in keyboard] == [
        "Default model", "New model", translate("group_model.reset", language),
    ]
    assert [row[0].callback_data for row in keyboard] == [
        f"{GROUP_MODEL_CALLBACK_PREFIX}{DEFAULT_MODEL}",
        f"{GROUP_MODEL_CALLBACK_PREFIX}{NEW_MODEL}", GROUP_MODEL_RESET_CALLBACK,
    ]
    await h.sessions.set_group_model(NEW_MODEL, ThinkingLevel.HIGH)
    await h.application.process_update(h.update(1, "/group_model", user_id=1))
    assert h.bot.sent[-1][1] == translate(
        "group_model.current", language, model="New model", level="High",
        source=translate("group_model.source_override", language),
    )


async def test_listing_uses_configured_name_when_model_cannot_resolve(h):
    """A removed upstream model must still identify the current configured setting."""

    h.client.resolve_model.return_value = None
    h.client.resolve_model.side_effect = ValueError("FAKE_RESOLVE_ERROR")
    await h.application.process_update(h.update(1, "/group_model", user_id=1))
    assert DEFAULT_MODEL in h.bot.sent[-1][1]


async def test_picker_answers_with_every_generation_slot_busy(h):
    """Administration must remain usable when the single generation slot is occupied."""

    async with h.queue.request(20):
        await asyncio.wait_for(h.application.process_update(h.update(1, "/group_model", user_id=1)), 0.5)
        await asyncio.wait_for(tap(h, f"{GROUP_MODEL_CALLBACK_PREFIX}{NEW_MODEL}"), 0.5)
        await asyncio.wait_for(tap(h, f"{GROUP_MODEL_LEVEL_CALLBACK_PREFIX}low:{NEW_MODEL}"), 0.5)
    assert len(h.bot.sent) == 1
    assert h.sessions.group_settings == GroupModelSettings(NEW_MODEL, ThinkingLevel.LOW, True)
    assert "group_model" in LOCK_FREE_COMMANDS


async def test_model_tap_marks_current_group_level(h):
    """The second picker step must reflect the group effort rather than private preferences."""

    edited = AsyncMock(wraps=h.bot.edit_message_text)
    object.__setattr__(h.bot, "edit_message_text", edited)
    await tap(h, f"{GROUP_MODEL_CALLBACK_PREFIX}{NEW_MODEL}")
    keyboard = edited.await_args.kwargs["reply_markup"].inline_keyboard
    assert [button.text for button in keyboard[0]] == ["Low", "✓ Medium", "High"]
    assert [button.callback_data for button in keyboard[0]] == [
        f"{GROUP_MODEL_LEVEL_CALLBACK_PREFIX}{level.value}:{NEW_MODEL}" for level in ThinkingLevel
    ]
    assert h.sessions.group_settings == DEFAULTS
    assert await GroupModelOverrideDAO(h.database.connection).get() is None


@pytest.mark.parametrize("model_name", ["FAKE_UNKNOWN", "FAKE_UNAVAILABLE", NEW_MODEL])
@pytest.mark.parametrize("step", ["model", "level"])
async def test_unknown_unavailable_or_unresolvable_model_is_rejected(h, model_name, step):
    """Both steps must revalidate upstream availability before changing global state."""

    if model_name == NEW_MODEL:
        h.client.resolve_model.side_effect = lambda name: None
    prefix = GROUP_MODEL_CALLBACK_PREFIX if step == "model" else f"{GROUP_MODEL_LEVEL_CALLBACK_PREFIX}high:"
    await tap(h, f"{prefix}{model_name}")
    assert h.bot.edited[-1][1] == translate("group_model.unavailable", LANGUAGE_ENGLISH)
    assert h.sessions.group_settings == DEFAULTS
    assert await GroupModelOverrideDAO(h.database.connection).get() is None


@pytest.mark.parametrize("level", list(ThinkingLevel))
async def test_level_tap_persists_and_controls_next_group_request(h, caplog, level):
    """A completed picker must update persistence, session model, effort, usage, and timing together."""

    await tap(h, f"{GROUP_MODEL_LEVEL_CALLBACK_PREFIX}{level.value}:{NEW_MODEL}")
    override = await GroupModelOverrideDAO(h.database.connection).get()
    assert override.model_name == NEW_MODEL
    assert override.thinking_level == level.value
    assert override.updated_at
    assert h.bot.edited[-1][1] == translate(
        "group_model.selected", LANGUAGE_ENGLISH, model="New model",
        level=translate(f"thinking.level.{level.value}", LANGUAGE_ENGLISH),
    )
    h.client.resolve_model.reset_mock()
    with caplog.at_level(logging.INFO, logger="gemini_tg_bot.timing"):
        await h.application.process_update(group_update(h))
    h.client.resolve_model.assert_called_once_with(NEW_MODEL)
    session = h.generated_sessions[-1]
    assert session.model.model_name == NEW_MODEL
    assert session.calls[-1][1]["extended_thinking"] is (level is not ThinkingLevel.LOW)
    assert session.wire_levels == [MEDIUM_WIRE_VALUE if level is ThinkingLevel.MEDIUM else None]
    usage = await UsageLogDAO(h.database.connection).list_for_chat(-100)
    assert len(usage) == 1 and usage[0].model == NEW_MODEL and usage[0].ok
    assert f"model={NEW_MODEL} level={level.value}" in caplog.text


async def test_restart_loads_override_instead_of_env_defaults(h):
    """A runtime choice must survive a new registry and database connection."""

    await tap(h, f"{GROUP_MODEL_LEVEL_CALLBACK_PREFIX}high:{NEW_MODEL}")
    async with Database(h.database.path) as restarted_database:
        restarted = ChatSessionRegistry(h.service, restarted_database)
        assert restarted.group_settings == DEFAULTS
        await restarted.load_group_model_override()
        assert restarted.group_settings == GroupModelSettings(NEW_MODEL, ThinkingLevel.HIGH, True)
        await restarted.start_group_session(-100)
        assert h.client.resolve_model.call_args.args == (NEW_MODEL,)


async def test_reset_removes_override_and_restores_env_pair(h):
    """Reset must durably return every group to the original env model and effort."""

    await h.sessions.set_group_model(NEW_MODEL, ThinkingLevel.HIGH)
    await tap(h, GROUP_MODEL_RESET_CALLBACK)
    assert await GroupModelOverrideDAO(h.database.connection).get() is None
    assert h.sessions.group_settings == DEFAULTS
    assert h.bot.edited[-1][1] == translate(
        "group_model.reset_done", LANGUAGE_ENGLISH, model="Default model", level="Medium",
    )
    await h.sessions.start_group_session(-100)
    assert h.client.resolve_model.call_args.args == (DEFAULT_MODEL,)
    restarted = ChatSessionRegistry(h.service, h.database)
    await restarted.load_group_model_override()
    assert restarted.group_settings == DEFAULTS


async def test_reset_remains_usable_when_service_is_degraded(h):
    """Clearing a saved override must not depend on a working Gemini connection."""

    await h.sessions.set_group_model(NEW_MODEL, ThinkingLevel.HIGH)
    h.service.execute.side_effect = ServiceUnavailableError(ServiceState.DEGRADED, DegradedReason.AUTH)
    await tap(h, GROUP_MODEL_RESET_CALLBACK)
    assert h.sessions.group_settings == DEFAULTS
    assert DEFAULT_MODEL in h.bot.edited[-1][1]
    assert await GroupModelOverrideDAO(h.database.connection).get() is None


@pytest.mark.parametrize("data", [f"{GROUP_MODEL_CALLBACK_PREFIX}{NEW_MODEL}", f"{GROUP_MODEL_LEVEL_CALLBACK_PREFIX}high:{NEW_MODEL}", GROUP_MODEL_RESET_CALLBACK])
async def test_forged_non_admin_callbacks_change_nothing(h, data):
    """Every picker step must recheck administrator access even for copied keyboards."""

    await h.sessions.set_group_model(NEW_MODEL, ThinkingLevel.LOW)
    before = await GroupModelOverrideDAO(h.database.connection).get()
    await tap(h, data, user_id=10, chat_id=10)
    assert h.bot.edited[-1][1] == translate("admin.only", LANGUAGE_ENGLISH)
    assert await GroupModelOverrideDAO(h.database.connection).get() == before
    assert h.sessions.group_settings == GroupModelSettings(NEW_MODEL, ThinkingLevel.LOW, True)
    h.service.execute.assert_not_awaited()


@pytest.mark.parametrize("data", [f"{GROUP_MODEL_CALLBACK_PREFIX}", f"{GROUP_MODEL_LEVEL_CALLBACK_PREFIX}invalid:{NEW_MODEL}", f"{GROUP_MODEL_LEVEL_CALLBACK_PREFIX}high:", f"{GROUP_MODEL_RESET_CALLBACK_PREFIX}forged", f"{GROUP_MODEL_CALLBACK_PREFIX}" + "界" * 30])
async def test_invalid_callbacks_change_nothing(h, data):
    """Malformed or overlong callback payloads must fail before upstream or storage work."""

    await tap(h, data)
    assert h.bot.edited[-1][1] == translate("group_model.invalid", LANGUAGE_ENGLISH)
    h.service.execute.assert_not_awaited()
    assert h.sessions.group_settings == DEFAULTS


async def test_callback_byte_limits_and_prefixes_keep_other_pickers_unchanged(h):
    """The new routes must stay distinct and skip models that cannot fit both callback steps."""

    prefixes = [GROUP_MODEL_CALLBACK_PREFIX, GROUP_MODEL_LEVEL_CALLBACK_PREFIX,
                GROUP_MODEL_RESET_CALLBACK_PREFIX, MODEL_CALLBACK_PREFIX,
                MODEL_LEVEL_CALLBACK_PREFIX, LANGUAGE_CALLBACK_PREFIX]
    assert all(not a.startswith(b) for a in prefixes for b in prefixes if a != b)
    h.models.append(SimpleNamespace(model_name="界" * 20, display_name="Too long", is_available=True))
    sent = AsyncMock(wraps=h.bot.send_message)
    object.__setattr__(h.bot, "send_message", sent)
    await h.application.process_update(h.update(1, "/group_model", user_id=1))
    assert all(row[0].text != "Too long" for row in sent.await_args.kwargs["reply_markup"].inline_keyboard)
    assert "group_model" not in {item.command for item in PUBLIC_BOT_COMMANDS}
    assert all("/group_model" not in _help_text(language, group_only=group)
               for language in [LANGUAGE_ENGLISH, LANGUAGE_CHINESE] for group in [False, True])


@pytest.mark.parametrize("stage", ["listing", "selection"])
@pytest.mark.parametrize("error", [RuntimeError("FAKE_FAILURE"), ServiceUnavailableError(ServiceState.DEGRADED, DegradedReason.AUTH)])
async def test_service_failures_preserve_settings(h, stage, error):
    """Picker outages must use existing model/service messages and leave the group pair intact."""

    h.service.execute.side_effect = error
    if stage == "listing":
        await h.application.process_update(h.update(1, "/group_model", user_id=1))
        text = h.bot.sent[-1][1]
    else:
        await tap(h, f"{GROUP_MODEL_CALLBACK_PREFIX}{NEW_MODEL}")
        text = h.bot.edited[-1][1]
    assert text
    assert h.sessions.group_settings == DEFAULTS
    assert await GroupModelOverrideDAO(h.database.connection).get() is None


async def test_fresh_database_gains_version_ten_override_table():
    """New installations must have the singleton override table immediately after migration."""

    async with Database(":memory:") as database:
        async with database.connection.execute("PRAGMA user_version") as cursor:
            assert (await cursor.fetchone())[0] == 10
        dao = GroupModelOverrideDAO(database.connection)
        assert await dao.get() is None
        await dao.set(GroupModelOverride(model_name=DEFAULT_MODEL, thinking_level="low", updated_at="FAKE_TIME"))
        await dao.set(GroupModelOverride(model_name=NEW_MODEL, thinking_level="high", updated_at="FAKE_NEW_TIME"))
        async with database.connection.execute("SELECT count(*) FROM group_model_override") as cursor:
            assert (await cursor.fetchone())[0] == 1
        assert (await dao.get()).model_name == NEW_MODEL


async def legacy_snapshot(database):
    async with database.connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name != 'group_model_override' ORDER BY name") as cursor:
        tables = await cursor.fetchall()
    snapshot = {}
    for name, schema in tables:
        async with database.connection.execute(f"SELECT * FROM {name}") as cursor:
            snapshot[name] = (schema, [tuple(row) for row in await cursor.fetchall()])
    return snapshot


async def test_version_nine_migration_preserves_all_existing_tables_and_rows(tmp_path):
    """The additive migration must not rewrite existing chats, usage, access, or research state."""

    path = tmp_path / "FAKE_VERSION_NINE.sqlite3"
    async with Database(path) as database:
        await database.connection.executescript("""
            DROP TABLE group_model_override;
            PRAGMA user_version = 9;
            INSERT INTO chat_sessions (chat_id, cid, model, gem_id, updated_at, thinking_level) VALUES (1, 'FAKE_CID', 'FAKE_MODEL', 'FAKE_GEM', 'FAKE_TIME', 'high');
            INSERT INTO research_tasks (task_id, chat_id, prompt, status, created_at, updated_at, plan_json) VALUES ('FAKE_TASK', 1, 'FAKE_PROMPT', 'running', 'FAKE_TIME', 'FAKE_TIME', 'FAKE_PLAN');
            INSERT INTO usage_log (user_id, chat_id, command, ok, created_at) VALUES (1, 1, 'message', 1, 'FAKE_TIME');
            INSERT INTO telegram_user_access VALUES (1, 1);
            INSERT INTO telegram_chat_access VALUES (-100, 1);
            INSERT INTO admin_notifications VALUES ('FAKE_FINGERPRINT', 1.0);
            INSERT INTO group_threads VALUES (-100, 1, 'FAKE_CID', '["FAKE_METADATA"]', 'FAKE_TIME');
        """)
        before = await legacy_snapshot(database)
    async with Database(path) as database:
        assert await legacy_snapshot(database) == before
        assert await GroupModelOverrideDAO(database.connection).get() is None
        async with database.connection.execute("PRAGMA user_version") as cursor:
            assert (await cursor.fetchone())[0] == 10


async def test_invalid_stored_level_is_ignored_without_logging_values(h, caplog):
    """A corrupt persisted effort must preserve env defaults and reveal only the field name."""

    await GroupModelOverrideDAO(h.database.connection).set(GroupModelOverride(
        model_name="FAKE_PRIVATE_MODEL", thinking_level="FAKE_INVALID_LEVEL", updated_at="FAKE_TIME",
    ))
    restarted = ChatSessionRegistry(h.service, h.database)
    with caplog.at_level(logging.WARNING):
        await restarted.load_group_model_override()
    assert restarted.group_settings == DEFAULTS
    assert caplog.messages == ["Ignoring invalid group model override field: thinking_level"]
    assert "FAKE_PRIVATE_MODEL" not in caplog.text and "FAKE_INVALID_LEVEL" not in caplog.text


@pytest.mark.parametrize("path", ["command", "reply", "command_upload", "reply_upload"])
async def test_queued_group_request_keeps_one_snapshot(h, caplog, path):
    """Changes during a queue wait must not mix old effort with a new session model or usage label."""

    async def wait_for_queue():
        while h.queue.queue_depth == 0:
            await asyncio.sleep(0)

    with caplog.at_level(logging.INFO, logger="gemini_tg_bot.timing"):
        async with h.queue.request(20):
            request = h.submit(group_update(h, path))
            await asyncio.wait_for(wait_for_queue(), 0.5)
            await tap(h, f"{GROUP_MODEL_LEVEL_CALLBACK_PREFIX}low:{NEW_MODEL}")
        await h.finish(request)

    first = h.generated_sessions[-1]
    assert first.model.model_name == DEFAULT_MODEL
    assert first.calls[-1][1]["extended_thinking"] is True
    assert first.wire_levels == [MEDIUM_WIRE_VALUE]
    usage = await UsageLogDAO(h.database.connection).list_for_chat(-100)
    assert usage[-1].model == DEFAULT_MODEL and usage[-1].ok
    assert f"model={DEFAULT_MODEL} level=medium" in caplog.text
    await h.application.process_update(group_update(h, path))
    next_session = h.generated_sessions[-1]
    assert next_session.model.model_name == NEW_MODEL
    assert next_session.calls[-1][1]["extended_thinking"] is False


async def test_running_group_request_keeps_one_snapshot(h, caplog):
    """An administrator change during generation must only affect a subsequent group request."""

    h.block_generation = True
    with caplog.at_level(logging.INFO, logger="gemini_tg_bot.timing"):
        request = h.submit(group_update(h))
        await asyncio.wait_for(h.started.wait(), 0.5)
        await tap(h, f"{GROUP_MODEL_LEVEL_CALLBACK_PREFIX}low:{NEW_MODEL}")
        h.release.set()
        await h.finish(request)
    session = h.generated_sessions[0]
    assert session.model.model_name == DEFAULT_MODEL
    assert session.wire_levels == [MEDIUM_WIRE_VALUE]
    assert session.calls[-1][1]["extended_thinking"] is True
    assert (await UsageLogDAO(h.database.connection).list_for_chat(-100))[0].model == DEFAULT_MODEL
    assert f"model={DEFAULT_MODEL} level=medium" in caplog.text
    await h.application.process_update(group_update(h))
    assert h.generated_sessions[-1].model.model_name == NEW_MODEL
