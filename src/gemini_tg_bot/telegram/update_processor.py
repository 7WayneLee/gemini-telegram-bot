"""Concurrent Telegram updates with ordered state changes within each chat."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from telegram.ext import BaseUpdateProcessor


LOCK_FREE_COMMANDS = frozenset(
    {
        "start", "help", "status", "research_status", "setcookie", "allow",
        "deny", "allow_chat", "deny_chat", "access", "health",
    }
)


@dataclass
class _ChatLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class PerChatUpdateProcessor(BaseUpdateProcessor):
    """Keep one chat's mutations in arrival order while other chats proceed."""

    def __init__(self) -> None:
        super().__init__(max_concurrent_updates=64)
        self._chat_locks: dict[int, _ChatLock] = {}

    async def initialize(self) -> None:
        """No resources are needed beyond the current event loop."""

    async def shutdown(self) -> None:
        """Application shutdown already waits for outstanding updates."""

    async def do_process_update(
        self, update: object, coroutine: Awaitable[Any]
    ) -> None:
        chat = getattr(update, "effective_chat", None)
        message = getattr(update, "effective_message", None)
        text = getattr(message, "text", None)
        command = (
            text.split(maxsplit=1)[0][1:].split("@", 1)[0].lower()
            if isinstance(text, str) and text.startswith("/")
            else None
        )
        if chat is None or (
            getattr(update, "callback_query", None) is None
            and command in LOCK_FREE_COMMANDS
        ):
            await coroutine
            return

        entry = self._chat_locks.setdefault(chat.id, _ChatLock())
        # Count holders and waiters before yielding, including cancelled waiters.
        # asyncio.Lock is fair, so waiting updates run in arrival order.
        entry.users += 1
        started = False
        try:
            async with entry.lock:
                started = True
                await coroutine
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self._chat_locks[chat.id]
            if not started and inspect.iscoroutine(coroutine):
                coroutine.close()
