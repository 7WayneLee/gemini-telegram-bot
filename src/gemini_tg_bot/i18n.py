"""Dependency-free message catalog and language resolution."""

from __future__ import annotations


LANGUAGE_ENGLISH = "en"
LANGUAGE_CHINESE = "zh-hant"
DEFAULT_LANGUAGE = LANGUAGE_ENGLISH


MESSAGES: dict[str, dict[str, str]] = {
    "think.enabled": {
        LANGUAGE_ENGLISH: (
            "Extended thinking is on. Replies include an expandable thought "
            "process.\n"
            "Note: this draws on the Gemini Advanced quota, faster than "
            "normal mode."
        ),
        LANGUAGE_CHINESE: (
            "Extended Thinking 已開啟。回覆會附上可展開的思考過程。\n"
            "注意：這會消耗 Gemini Advanced 額度，用得比一般模式快。"
        ),
    },
    "think.disabled": {
        LANGUAGE_ENGLISH: "Extended thinking is off.",
        LANGUAGE_CHINESE: "Extended Thinking 已關閉。",
    },
    "generic.failure": {
        LANGUAGE_ENGLISH: (
            "An error occurred while processing your request. "
            "Please try again later."
        ),
        LANGUAGE_CHINESE: "處理請求時發生錯誤，請稍後再試。",
    },
    "new.started": {
        LANGUAGE_ENGLISH: "Started a new conversation.",
        LANGUAGE_CHINESE: "已開始新的對話。",
    },
    "auth.degraded": {
        LANGUAGE_ENGLISH: "Authentication expired. Please use /setcookie.",
        LANGUAGE_CHINESE: "認證失效，請 /setcookie",
    },
    "lang.choose": {
        LANGUAGE_ENGLISH: "Choose an interface language:",
        LANGUAGE_CHINESE: "請選擇介面語言：",
    },
    "lang.selected": {
        LANGUAGE_ENGLISH: "Interface language set to {language}.",
        LANGUAGE_CHINESE: "介面語言已設為{language}。",
    },
    "lang.invalid": {
        LANGUAGE_ENGLISH: "Invalid language. Please run /lang again.",
        LANGUAGE_CHINESE: "無效的語言，請重新執行 /lang。",
    },
    "help.heading": {
        LANGUAGE_ENGLISH: "Available commands:",
        LANGUAGE_CHINESE: "可用指令：",
    },
    "help.footer": {
        LANGUAGE_ENGLISH: (
            "Send text directly to continue the current conversation."
        ),
        LANGUAGE_CHINESE: "直接傳送文字即可延續目前對話。",
    },
    "command.start.description": {
        LANGUAGE_ENGLISH: "Show usage instructions",
        LANGUAGE_CHINESE: "顯示使用說明",
    },
    "command.help.description": {
        LANGUAGE_ENGLISH: "Show usage instructions",
        LANGUAGE_CHINESE: "顯示使用說明",
    },
    "command.new.description": {
        LANGUAGE_ENGLISH: "Start a new conversation",
        LANGUAGE_CHINESE: "開始新的對話",
    },
    "command.model.description": {
        LANGUAGE_ENGLISH: "Choose a Gemini model",
        LANGUAGE_CHINESE: "選擇 Gemini 模型",
    },
    "command.gem.description": {
        LANGUAGE_ENGLISH: "Choose a Gem",
        LANGUAGE_CHINESE: "選擇 Gem",
    },
    "command.temp.description": {
        LANGUAGE_ENGLISH: "Toggle temporary chat mode",
        LANGUAGE_CHINESE: "切換暫時對話模式",
    },
    "command.think.description": {
        LANGUAGE_ENGLISH: "Toggle Extended Thinking",
        LANGUAGE_CHINESE: "切換 Extended Thinking",
    },
    "command.lang.description": {
        LANGUAGE_ENGLISH: "Choose interface language",
        LANGUAGE_CHINESE: "選擇介面語言",
    },
    "command.img.description": {
        LANGUAGE_ENGLISH: "Generate an image",
        LANGUAGE_CHINESE: "生成圖片",
    },
    "command.research.description": {
        LANGUAGE_ENGLISH: "Submit a Deep Research task",
        LANGUAGE_CHINESE: "提交 Deep Research 任務",
    },
    "command.research_status.description": {
        LANGUAGE_ENGLISH: "View Deep Research task status",
        LANGUAGE_CHINESE: "查看 Deep Research 任務狀態",
    },
    "command.status.description": {
        LANGUAGE_ENGLISH: "View current status",
        LANGUAGE_CHINESE: "查看目前狀態",
    },
}


def translate(key: str, language: str, /, **params: object) -> str:
    """Return one localized message, formatting named parameters.

    A missing translation falls back to English.  An unknown message ID is a
    programming error and deliberately raises :class:`KeyError`.
    """

    translations = MESSAGES[key]
    message = translations.get(language, translations[LANGUAGE_ENGLISH])
    return message.format(**params)


def resolve_language(stored: str | None, telegram_code: str | None) -> str:
    """Resolve a stored preference, Telegram hint, or the English default.

    Every Telegram code beginning with ``zh`` maps to Traditional Chinese,
    including Simplified variants such as ``zh-hans`` and ``zh-cn``.  The
    project has one Chinese catalog, and Traditional Chinese is considered a
    better fallback for those users than English.
    """

    candidate = stored if stored is not None else telegram_code
    if candidate is None:
        return DEFAULT_LANGUAGE
    if candidate.casefold().replace("_", "-").startswith("zh"):
        return LANGUAGE_CHINESE
    return LANGUAGE_ENGLISH
