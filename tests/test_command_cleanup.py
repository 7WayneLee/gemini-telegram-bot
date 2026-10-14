"""Offline regressions for removed commands and Telegram's stale English menu."""

import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from telegram import BotCommandScopeAllGroupChats, BotCommandScopeChat
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler

from gemini_tg_bot.__main__ import _start_application
from gemini_tg_bot.i18n import LANGUAGE_CHINESE, LANGUAGE_ENGLISH, translate
from gemini_tg_bot.telegram.handlers import (
    GROUP_MENU_COMMANDS,
    PUBLIC_BOT_COMMANDS,
    _help_text,
    register_command_menu,
)
from tests.test_concurrency import Harness


REMOVED_LISTINGS = {"help", "gem", "research", "research_status"}


@pytest.fixture
async def harness(tmp_path):
    instance = Harness(tmp_path)
    async with instance.application:
        yield instance
    assert instance.errors == []


async def test_stale_english_menu_is_deleted_before_publishing() -> None:
    """Telegram prefers the obsolete English menu until its explicit deletion."""

    bot = MagicMock(delete_my_commands=AsyncMock(), set_my_commands=AsyncMock())

    await register_command_menu(SimpleNamespace(bot=bot))

    bot.delete_my_commands.assert_awaited_once_with(language_code="en")
    assert bot.mock_calls[0] == call.delete_my_commands(language_code="en")
    menus = bot.set_my_commands.await_args_list
    assert len(menus) == 2
    assert menus[0] == call(PUBLIC_BOT_COMMANDS)
    assert {item.command for item in menus[1].args[0]} == GROUP_MENU_COMMANDS
    assert menus[1].kwargs == {"scope": BotCommandScopeAllGroupChats()}


async def test_failed_stale_menu_deletion_does_not_block_startup(caplog) -> None:
    """A cleanup outage must preserve menu publication and log no exception text."""

    bot = SimpleNamespace(
        delete_my_commands=AsyncMock(side_effect=BadRequest("FAKE_PRIVATE_DETAIL")),
        set_my_commands=AsyncMock(),
    )
    application = SimpleNamespace(bot=bot, start=AsyncMock())

    with caplog.at_level(logging.WARNING):
        await _start_application(application)

    bot.delete_my_commands.assert_awaited_once_with(language_code="en")
    assert bot.set_my_commands.await_args_list[0] == call(PUBLIC_BOT_COMMANDS)
    group_menu = bot.set_my_commands.await_args_list[1]
    assert {item.command for item in group_menu.args[0]} == GROUP_MENU_COMMANDS
    assert group_menu.kwargs == {"scope": BotCommandScopeAllGroupChats()}
    application.start.assert_awaited_once_with()
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.WARNING, "Unable to delete Telegram command menu for en (BadRequest)")
    ]
    assert "FAKE_PRIVATE_DETAIL" not in caplog.text


async def test_all_menu_scopes_omit_removed_listings(harness) -> None:
    """Default, group, and Traditional Chinese chat menus must agree on removals."""

    bot = SimpleNamespace(delete_my_commands=AsyncMock(), set_my_commands=AsyncMock())
    await register_command_menu(SimpleNamespace(bot=bot))
    await harness.handlers._set_chat_command_menu(bot, 10, LANGUAGE_CHINESE)

    menus = bot.set_my_commands.await_args_list
    assert len(menus) == 3
    assert menus[0].kwargs == {}
    assert menus[1].kwargs == {"scope": BotCommandScopeAllGroupChats()}
    assert menus[2].kwargs == {"scope": BotCommandScopeChat(chat_id=10)}
    for menu in menus:
        assert REMOVED_LISTINGS.isdisjoint(item.command for item in menu.args[0])
    assert menus[2].args[0][0].description == translate(
        "command.start.description", LANGUAGE_CHINESE
    )


@pytest.mark.parametrize("language", [LANGUAGE_ENGLISH, LANGUAGE_CHINESE])
@pytest.mark.parametrize("group_only", [False, True])
def test_help_text_omits_removed_listings(language, group_only) -> None:
    """Both languages and scopes must hide the alias and deleted feature commands."""

    text = _help_text(language, group_only=group_only)
    commands = set(re.findall(r"^/(\w+)", text, flags=re.MULTILINE))
    assert REMOVED_LISTINGS.isdisjoint(commands)
    assert re.search(r"/(?:help|gem|research)\b", text) is None


@pytest.mark.parametrize("language", [LANGUAGE_ENGLISH, LANGUAGE_CHINESE])
@pytest.mark.parametrize("chat_id", [10, -100])
async def test_typed_help_alias_uses_registered_handler_and_group_gate(
    harness, language, chat_id,
) -> None:
    """Hiding /help must preserve its real private and group command routing."""

    harness.languages[chat_id] = language
    await harness.application.process_update(harness.update(chat_id, "/help"))

    expected_language = LANGUAGE_ENGLISH if chat_id < 0 else language
    assert harness.bot.sent == [
        (chat_id, _help_text(expected_language, group_only=chat_id < 0))
    ]
    harness.service.execute.assert_not_awaited()
    assert harness.errors == []


@pytest.mark.parametrize("command", ["gem", "research", "research_status"])
async def test_typed_removed_private_commands_are_ignored(harness, command) -> None:
    """Deleted private commands must neither generate a request nor raise an error."""

    # Release any accidental generation so a regression fails without hanging.
    harness.gate.releases[10].set()
    await harness.application.process_update(harness.update(10, f"/{command}"))

    harness.service.execute.assert_not_awaited()
    harness.sessions.get_or_create.assert_not_awaited()
    harness.sessions.start_new.assert_not_awaited()
    assert harness.bot.sent == []
    assert harness.errors == []


async def test_gem_callback_is_no_longer_routed(harness) -> None:
    """Old Gem keyboards must not dispatch removed picker callbacks to Gemini."""

    update = harness.update(10, callback="gem:FAKE_GEM")
    routers = [
        handler
        for group in harness.application.handlers.values()
        for handler in group
        if isinstance(handler, CallbackQueryHandler)
    ]
    assert routers
    assert all(not router.check_update(update) for router in routers)

    await harness.application.process_update(update)

    harness.service.execute.assert_not_awaited()
    assert harness.bot.answered == []
    assert harness.bot.edited == []
    assert harness.errors == []
