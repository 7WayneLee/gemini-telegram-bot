"""Mock-only regressions for showing real model names in model menus."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gemini_tg_bot.gemini.model_label import model_label
from gemini_tg_bot.gemini.thinking import ThinkingLevel
from gemini_tg_bot.i18n import LANGUAGE_ENGLISH, translate
from gemini_tg_bot.telegram.handlers import (
    GROUP_MODEL_CALLBACK_PREFIX,
    GROUP_MODEL_LEVEL_CALLBACK_PREFIX,
    GROUP_MODEL_RESET_CALLBACK,
    MODEL_CALLBACK_PREFIX,
    MODEL_LEVEL_CALLBACK_PREFIX,
    _resolved_model_label,
)

# Shared mocked fixtures; importing them registers them for this module.
from tests.test_group_model import DEFAULT_MODEL, NEW_MODEL, h, tap  # noqa: F401
from tests.test_handlers import (  # noqa: F401
    _callback_update,
    _update,
    handlers_factory,
    registry,
)


def _model(model_name, display_name, description, *, is_available=True):
    return SimpleNamespace(
        model_name=model_name,
        display_name=display_name,
        description=description,
        is_available=is_available,
    )


# 2026-11-15 layout: the model name moved to ``description``.
HYDROGEN = _model("gemini-4-hydrogen", "Everyday help", "4 Hydrogen")
# Pre-2026-11-15 layout: the model name is in ``display_name``.
ARGON = _model("gemini-argon", "4 Argon", "Complex tasks")


@pytest.mark.parametrize(
    ("display_name", "description", "expected"),
    [
        # New layout: the versioned description is the model name.
        ("Everyday help", "4 Hydrogen", "4 Hydrogen"),
        ("Quick answers", "3.5 Flash-Lite", "3.5 Flash-Lite"),
        ("Detailed research", "3.1 Pro", "3.1 Pro"),
        # Old layout: the versioned display name is the model name.
        ("4 Argon", "Complex tasks", "4 Argon"),
        ("3.8 Flash", "All-around help", "3.8 Flash"),
        # Neither or both carry a version: prefer display_name.
        ("Dynamic Model", "Offline fixture", "Dynamic Model"),
        ("4 Hydrogen", "Gemini 4 Hydrogen", "4 Hydrogen"),
        # Digits glued to other characters are not a version token.
        ("Everyday help", "v4 notes", "Everyday help"),
        ("Everyday help", "Flash-4", "Everyday help"),
        # Empty display_name falls back to description.
        ("", "Complex tasks", "Complex tasks"),
        ("   ", "Complex tasks", "Complex tasks"),
        ("", "4 Hydrogen", "4 Hydrogen"),
        # None description keeps display_name.
        ("Fake model", None, "Fake model"),
        ("4 Argon", None, "4 Argon"),
        # Non-string values are treated as missing.
        (42, "Complex tasks", "Complex tasks"),
        ("Everyday help", 4, "Everyday help"),
        # Nothing usable falls back to the stable model_name.
        ("", None, "fake-model-name"),
        (None, None, "fake-model-name"),
        (None, "", "fake-model-name"),
    ],
)
def test_model_label_picks_real_model_name(display_name, description, expected):
    """Both upstream field layouts must yield the human-facing model name."""

    model = _model("fake-model-name", display_name, description)
    assert model_label(model) == expected


def test_model_label_tolerates_missing_attributes():
    """Offline fixtures without description must still use display_name."""

    assert model_label(SimpleNamespace(model_name="m", display_name="Fake model")) == "Fake model"
    assert model_label(SimpleNamespace(model_name="m", description="4 Hydrogen")) == "4 Hydrogen"
    assert model_label(SimpleNamespace(model_name="only-name")) == "only-name"


def test_model_label_strips_surrounding_whitespace():
    assert model_label(_model("m", " Everyday help ", " 4 Hydrogen ")) == "4 Hydrogen"


def test_resolved_model_label_uses_label_or_saved_name():
    """Group menus must show the real name, or the saved name when unresolvable."""

    client = MagicMock()
    client.resolve_model.return_value = HYDROGEN
    assert _resolved_model_label(client, "gemini-4-hydrogen") == "4 Hydrogen"
    client.resolve_model.return_value = None
    assert _resolved_model_label(client, "gemini-gone") == "gemini-gone"
    client.resolve_model.side_effect = ValueError("FAKE_RESOLVE_ERROR")
    assert _resolved_model_label(client, "gemini-gone") == "gemini-gone"


async def test_model_menu_shows_real_names_with_model_name_callbacks(handlers_factory):
    """/model buttons show "4 Hydrogen" while callback_data keeps the stable model_name."""

    client = MagicMock()
    client.list_models.return_value = [HYDROGEN, ARGON]
    handlers, _ = handlers_factory(client)
    update = _update()

    await handlers.model(update, SimpleNamespace())

    keyboard = update.effective_message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert [row[0].text for row in keyboard] == ["4 Hydrogen", "4 Argon"]
    assert [row[0].callback_data for row in keyboard] == [
        f"{MODEL_CALLBACK_PREFIX}gemini-4-hydrogen",
        f"{MODEL_CALLBACK_PREFIX}gemini-argon",
    ]
    assert "gemini-4-hydrogen" in keyboard[0][0].callback_data
    assert all("Everyday help" not in row[0].text for row in keyboard)


async def test_model_tap_and_level_tap_name_the_real_model(handlers_factory, registry):
    """Both /model selection steps must name the model by its real name."""

    client = MagicMock()
    client.list_models.return_value = [HYDROGEN]
    client.resolve_model.return_value = HYDROGEN
    handlers, _ = handlers_factory(client)

    update = _callback_update(f"{MODEL_CALLBACK_PREFIX}gemini-4-hydrogen")
    await handlers.callback(update, SimpleNamespace())
    edit = update.callback_query.edit_message_text.await_args
    assert edit.args[0] == translate("model.choose_thinking", LANGUAGE_ENGLISH, model="4 Hydrogen")
    assert [button.callback_data for button in edit.kwargs["reply_markup"].inline_keyboard[0]] == [
        f"{MODEL_LEVEL_CALLBACK_PREFIX}{level.value}:gemini-4-hydrogen" for level in ThinkingLevel
    ]

    update = _callback_update(f"{MODEL_LEVEL_CALLBACK_PREFIX}high:gemini-4-hydrogen")
    await handlers.callback(update, SimpleNamespace())
    registry.set_model_and_thinking_level.assert_awaited_once_with(
        202, "gemini-4-hydrogen", ThinkingLevel.HIGH
    )
    assert update.callback_query.edit_message_text.await_args.args[0] == translate(
        "model.selected", LANGUAGE_ENGLISH, model="4 Hydrogen",
        level=translate("thinking.level.high", LANGUAGE_ENGLISH),
    )


def _use_new_layout(h):
    h.models[:] = [
        _model(DEFAULT_MODEL, "Everyday help", "4 Hydrogen"),
        _model(NEW_MODEL, "Most complex work", "4 Carbon"),
        _model("FAKE_UNAVAILABLE", "Quick answers", "3.5 Flash-Lite", is_available=False),
    ]


async def test_group_model_menu_shows_real_names(h):
    """/group_model must label the current model and buttons by real name."""

    _use_new_layout(h)
    sent = AsyncMock(wraps=h.bot.send_message)
    object.__setattr__(h.bot, "send_message", sent)

    await h.application.process_update(h.update(1, "/group_model", user_id=1))

    assert h.bot.sent[-1][1] == translate(
        "group_model.current", LANGUAGE_ENGLISH, model="4 Hydrogen", level="Medium",
        source=translate("group_model.source_default", LANGUAGE_ENGLISH),
    )
    keyboard = sent.await_args.kwargs["reply_markup"].inline_keyboard
    assert [row[0].text for row in keyboard] == [
        "4 Hydrogen", "4 Carbon", translate("group_model.reset", LANGUAGE_ENGLISH),
    ]
    assert [row[0].callback_data for row in keyboard] == [
        f"{GROUP_MODEL_CALLBACK_PREFIX}{DEFAULT_MODEL}",
        f"{GROUP_MODEL_CALLBACK_PREFIX}{NEW_MODEL}",
        GROUP_MODEL_RESET_CALLBACK,
    ]


async def test_group_model_steps_and_reset_name_the_real_model(h):
    """Every /group_model confirmation must use the real model name."""

    _use_new_layout(h)

    await tap(h, f"{GROUP_MODEL_CALLBACK_PREFIX}{NEW_MODEL}")
    assert h.bot.edited[-1][1] == translate(
        "group_model.choose_thinking", LANGUAGE_ENGLISH, model="4 Carbon"
    )

    await tap(h, f"{GROUP_MODEL_LEVEL_CALLBACK_PREFIX}low:{NEW_MODEL}")
    assert h.bot.edited[-1][1] == translate(
        "group_model.selected", LANGUAGE_ENGLISH, model="4 Carbon",
        level=translate("thinking.level.low", LANGUAGE_ENGLISH),
    )
    assert h.sessions.group_settings.model_name == NEW_MODEL

    await tap(h, GROUP_MODEL_RESET_CALLBACK)
    assert h.bot.edited[-1][1] == translate(
        "group_model.reset_done", LANGUAGE_ENGLISH, model="4 Hydrogen", level="Medium",
    )
