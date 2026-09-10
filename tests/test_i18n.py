"""Tests for the dependency-free bilingual message catalog."""

from __future__ import annotations

import ast
from pathlib import Path
import re

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


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (LANGUAGE_ENGLISH, "This command is only available in private chats."),
        (LANGUAGE_CHINESE, "此指令僅限私訊使用。"),
    ],
)
def test_private_command_boundary_is_clear_in_each_language(
    language: str,
    expected: str,
) -> None:
    """A rejected command must explain the safe private-chat path to every user."""

    assert translate("command.private_only", language) == expected


def test_user_facing_source_strings_are_catalog_backed() -> None:
    """UI text must not bypass i18n as the source tree evolves.

    Comments and docstrings are deliberately excluded because they document
    implementation details for maintainers and are never shown to Telegram
    users.  The catalog module itself is the sole allowed home for localized
    Chinese string literals.
    """

    source_root = Path(__file__).parents[1] / "src" / "gemini_tg_bot"
    chinese = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
    violations: list[str] = []
    for path in source_root.rglob("*.py"):
        if path.name == "i18n.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = {
            id(owner.body[0].value)
            for owner in ast.walk(tree)
            if isinstance(
                owner,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            )
            and owner.body
            and isinstance(owner.body[0], ast.Expr)
            and isinstance(owner.body[0].value, ast.Constant)
            and isinstance(owner.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and chinese.search(node.value)
            ):
                relative = path.relative_to(source_root)
                violations.append(f"{relative}:{node.lineno}: {node.value!r}")

    assert violations == []


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


@pytest.mark.parametrize(
    "telegram_code",
    [
        "zh-TW",
        "zh-tw",
        "zh-Hant",
        "zh-hant",
        "zh-Hant-TW",
        "zh-hant-tw",
    ],
)
def test_explicit_traditional_chinese_tags_use_chinese(
    telegram_code: str,
) -> None:
    """Recognized Traditional tags should localize the first interaction."""

    assert resolve_language(None, telegram_code) == LANGUAGE_CHINESE


@pytest.mark.parametrize(
    "telegram_code",
    ["zh-Hans", "zh-CN", "zh-SG", "zh-HK", "zh-MO", "zh"],
)
def test_other_chinese_tags_use_english(telegram_code: str) -> None:
    """Ambiguous or Simplified tags must not opt users into Traditional UI."""

    assert resolve_language(None, telegram_code) == LANGUAGE_ENGLISH


@pytest.mark.parametrize("telegram_code", ["ja", "fr", None])
def test_non_chinese_or_missing_telegram_language_uses_english(
    telegram_code: str | None,
) -> None:
    """Every unrecognized locale needs a predictable, usable fallback."""

    assert resolve_language(None, telegram_code) == DEFAULT_LANGUAGE


@pytest.mark.parametrize("telegram_code", ["en", "zh-Hans"])
def test_stored_chinese_preference_overrides_telegram_language(
    telegram_code: str,
) -> None:
    """A deliberate /language choice must survive app-language changes."""

    assert resolve_language(LANGUAGE_CHINESE, telegram_code) == LANGUAGE_CHINESE


def test_stored_english_preference_overrides_traditional_telegram_tag() -> None:
    """A deliberate English choice must not be undone by profile metadata."""

    assert resolve_language(LANGUAGE_ENGLISH, "zh-TW") == LANGUAGE_ENGLISH
