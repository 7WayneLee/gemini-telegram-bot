"""Tests for the dependency-free bilingual message catalog."""

from __future__ import annotations

import pytest

from gemini_tg_bot.i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGE_CHINESE,
    LANGUAGE_ENGLISH,
    MESSAGES,
    resolve_language,
    translate,
)


def test_every_message_has_both_supported_languages() -> None:
    """Catalog growth must never ship a key that breaks one supported UI."""

    expected = {LANGUAGE_ENGLISH, LANGUAGE_CHINESE}
    for key, translations in MESSAGES.items():
        assert set(translations) == expected, key


def test_unknown_message_id_raises_key_error() -> None:
    """A programming typo must fail during testing instead of leaking as UI."""

    with pytest.raises(KeyError, match="missing.message"):
        translate("missing.message", LANGUAGE_ENGLISH)


def test_missing_requested_translation_falls_back_to_english(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial future translation must still leave the product usable."""

    monkeypatch.setitem(
        MESSAGES,
        "temporary.partial",
        {LANGUAGE_ENGLISH: "Fallback"},
    )

    assert translate("temporary.partial", "fr") == "Fallback"


def test_named_parameters_are_formatted() -> None:
    """Parameterized messages must replace the f-strings used by handlers."""

    assert (
        translate(
            "lang.selected",
            LANGUAGE_ENGLISH,
            language="正體中文",
        )
        == "Interface language set to 正體中文."
    )


def test_resolve_language_honors_stored_then_telegram_then_default() -> None:
    """An explicit chat choice must survive Telegram profile changes."""

    assert resolve_language(LANGUAGE_ENGLISH, "zh-tw") == LANGUAGE_ENGLISH
    assert resolve_language(None, "zh-tw") == LANGUAGE_CHINESE
    assert resolve_language(None, None) == DEFAULT_LANGUAGE


@pytest.mark.parametrize(
    "telegram_code",
    ["zh", "zh-hant", "zh-tw", "zh-hans", "zh-cn"],
)
def test_all_chinese_telegram_variants_use_traditional_chinese(
    telegram_code: str,
) -> None:
    """Chinese users should receive the sole Chinese catalog, not English."""

    assert resolve_language(None, telegram_code) == LANGUAGE_CHINESE


def test_unknown_telegram_language_uses_english() -> None:
    """Unsupported Telegram locales need the documented primary language."""

    assert resolve_language(None, "ja") == LANGUAGE_ENGLISH
