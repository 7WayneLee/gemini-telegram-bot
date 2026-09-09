"""Long-polling entry point and deterministic offline integration check."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import signal
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import SecretStr
from telegram import Update
from telegram.ext import Application

from gemini_tg_bot.config import RUNTIME_CREDENTIALS_FILENAME, Settings
from gemini_tg_bot.gemini.research import ResearchManager
from gemini_tg_bot.gemini.service import GeminiService
from gemini_tg_bot.i18n import DEFAULT_LANGUAGE, translate
from gemini_tg_bot.queue import RequestQueue
from gemini_tg_bot.storage.db import Database
from gemini_tg_bot.storage.models import AdminNotificationDAO, UsageLogDAO
from gemini_tg_bot.telegram.auth import AuthMiddleware, SQLiteAccessOverrides
from gemini_tg_bot.telegram.handlers import (
    EgressMeter,
    TelegramHandlers,
    register_command_menu,
    register_handlers,
)
from gemini_tg_bot.telegram.streaming import PLACEHOLDER_TEXT


LOGGER = logging.getLogger(__name__)
DATABASE_PATH = Path("data/db/bot.sqlite3")

# How long an identical administrator notification stays suppressed.  The
# service already refuses to page twice for one degraded reason, but a
# supervisor restarting a process that keeps failing the same way defeats that,
# and the resulting flood costs more than the missed repeat: Telegram
# rate-limits a chat, so it buries the /setcookie prompt that is the only way
# back.  Nothing is lost by waiting -- AUTH degradation never heals on its own,
# and a failed /setcookie answers in the chat rather than through this path.
ADMIN_NOTIFICATION_COOLDOWN_SEC = 900.0


def _log_cookie_location(settings: Settings) -> None:
    """Record where the session actually lives, resolved to an absolute path.

    ``GEMINI_COOKIE_PATH`` is resolved against the working directory, so a
    relative value lands somewhere other than the absolute path a service unit
    names, and the two then disagree about where the session is kept.  Nothing
    else reports which one won, which makes an empty directory look like a lost
    session.  The directory is safe to log; the cache *file* names inside it are
    not, because upstream keys them on the cookie value.
    """

    cookie_path = settings.gemini_cookie_path.resolve()
    LOGGER.info(
        "Cookie state directory: %s (exists=%s, /setcookie override=%s)",
        cookie_path,
        cookie_path.is_dir(),
        (cookie_path / RUNTIME_CREDENTIALS_FILENAME).is_file(),
    )


async def _run_polling(settings: Settings) -> None:
    """Run Telegram long polling and Gemini on one asyncio event loop."""

    _log_cookie_location(settings)
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    database = Database(DATABASE_PATH)
    await database.connect()

    application: Application[Any, Any, Any, Any, Any, Any]

    admin_notifications = AdminNotificationDAO(database.connection)

    async def notify_admin(text: str) -> None:
        if not await admin_notifications.claim(
            text,
            now=time.time(),
            cooldown_sec=ADMIN_NOTIFICATION_COOLDOWN_SEC,
        ):
            LOGGER.info(
                "Suppressed an administrator notification repeated within %.0fs",
                ADMIN_NOTIFICATION_COOLDOWN_SEC,
            )
            return
        await application.bot.send_message(
            chat_id=settings.admin_user_id,
            text=text,
        )

    async def notify_research(chat_id: int, text: str) -> None:
        await application.bot.send_message(chat_id=chat_id, text=text)

    service = GeminiService(settings, admin_notifier=notify_admin)
    research: ResearchManager | None = None
    try:
        # Deliberately delayed: T2.3 owns this implementation and may land in
        # the shared worktree after this entry point is imported by unit tests.
        from gemini_tg_bot.gemini.sessions import ChatSessionRegistry

        sessions = ChatSessionRegistry(service, database)
        auth = AuthMiddleware(
            admin_user_id=settings.admin_user_id,
            allowed_user_ids=settings.allowed_user_ids,
            access_overrides=SQLiteAccessOverrides(database.connection),
        )
        application = (
            Application.builder()
            .token(settings.telegram_bot_token.get_secret_value())
            .build()
        )
        research = ResearchManager(
            service,
            database,
            timeout_sec=settings.research_timeout_sec,
            notify=notify_research,
            default_language=settings.default_language,
        )
        handlers = TelegramHandlers(
            service=service,
            sessions=sessions,
            request_queue=RequestQueue(
                max_concurrency=settings.max_concurrency,
                user_rate_limit_per_min=settings.user_rate_limit_per_min,
            ),
            usage_dao=UsageLogDAO(database.connection),
            egress_meter=EgressMeter(),
            cookie_path=settings.gemini_cookie_path,
            secure_1psid=settings.gemini_secure_1psid,
            database=database,
            research=research,
        )
        register_handlers(application, auth=auth, handlers=handlers)

        updater = application.updater
        if updater is None:
            raise RuntimeError("Telegram long-polling updater is unavailable")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed_signals: list[signal.Signals] = []
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stop_event.set)
            except NotImplementedError:
                continue
            installed_signals.append(signum)

        try:
            async with application:
                await _initialize_and_start_polling(
                    service,
                    sessions,
                    research,
                    updater,
                )
                try:
                    await _start_application(application)
                    await stop_event.wait()
                finally:
                    if updater.running:
                        await updater.stop()
                    if application.running:
                        await application.stop()
                    await research.close()
                    await service.close()
        finally:
            for signum in installed_signals:
                loop.remove_signal_handler(signum)
    finally:
        try:
            if research is not None:
                await research.close()
        finally:
            try:
                await service.close()
            finally:
                await database.close()


async def _initialize_and_start_polling(
    service: GeminiService,
    sessions: Any,
    research: ResearchManager,
    updater: Any,
) -> None:
    """Restore runtime state and start polling even if Gemini is degraded."""

    await service.init()
    await sessions.restore_all()
    await research.restore_running()
    await updater.start_polling(allowed_updates=Update.ALL_TYPES)


async def _start_application(
    application: Application[Any, Any, Any, Any, Any, Any],
) -> None:
    """Register the optional command menu, then start update processing."""

    await register_command_menu(application)
    await application.start()


class _DryRunMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[tuple[str, dict[str, Any]]] = []
        self.placeholder = _DryRunPlaceholder()

    async def reply_text(self, text: str, **kwargs: Any) -> _DryRunPlaceholder:
        self.replies.append((text, kwargs))
        return self.placeholder


class _DryRunPlaceholder:
    def __init__(self) -> None:
        self.edits: list[tuple[str, dict[str, Any]]] = []

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append((text, kwargs))


class _DryRunSession:
    def __init__(self) -> None:
        self.cid = ""
        self.metadata = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _generate(self) -> Any:
        yield SimpleNamespace(
            text="**dry-run ok**",
            text_delta="**dry-run ok**",
            images=(),
        )

    def send_message_stream(self, prompt: str, **kwargs: Any) -> Any:
        self.calls.append((prompt, kwargs))
        return self._generate()


class _DryRunRegistry:
    def __init__(self) -> None:
        self.session = _DryRunSession()
        self.persisted = False
        self.state = SimpleNamespace(
            chat_id=7002,
            cid=None,
            model=None,
            gem_id=None,
            temporary=False,
            extended_thinking=False,
            updated_at=None,
        )

    async def get_state(self, chat_id: int) -> Any:
        assert chat_id == self.state.chat_id
        return self.state

    async def get_or_create(self, chat_id: int) -> _DryRunSession:
        assert chat_id == self.state.chat_id
        return self.session

    async def persist(self, chat_id: int, session: _DryRunSession) -> None:
        assert chat_id == self.state.chat_id
        assert session is self.session
        self.persisted = True


class _DryRunService:
    def __init__(self) -> None:
        self.client = SimpleNamespace()
        self.execute_count = 0
        self.health = SimpleNamespace(
            state=SimpleNamespace(value="healthy"),
            degraded_reason=None,
        )

    async def execute(self, operation: Any) -> Any:
        self.execute_count += 1
        result = operation(self.client)
        return await result if inspect.isawaitable(result) else result

class _DryRunResearch:
    def __init__(self) -> None:
        self.task = SimpleNamespace(
            task_id="dry-run-research-task",
            status=SimpleNamespace(value="pending"),
        )
        self.submissions: list[tuple[int, str]] = []

    async def submit(self, chat_id: int, prompt: str) -> str:
        self.submissions.append((chat_id, prompt))
        return self.task.task_id

    async def status(self, chat_id: int) -> list[Any]:
        assert chat_id == 7002
        return [self.task]


class _DryRunApplication:
    def __init__(self) -> None:
        self.handlers: list[tuple[int, Any]] = []

    def add_handler(self, handler: Any, group: int = 0) -> None:
        self.handlers.append((group, handler))


async def _run_dry_run() -> None:
    """Exercise the offline receive-to-send path without external I/O."""

    database = Database(":memory:")
    await database.connect()
    try:
        user_id = 7001
        chat_id = 7002
        auth = AuthMiddleware(
            admin_user_id=7999,
            allowed_user_ids={user_id},
            access_overrides=SQLiteAccessOverrides(database.connection),
        )
        service = _DryRunService()
        sessions = _DryRunRegistry()
        research = _DryRunResearch()
        handlers = TelegramHandlers(
            service=service,  # type: ignore[arg-type]
            sessions=sessions,  # type: ignore[arg-type]
            request_queue=RequestQueue(
                max_concurrency=1,
                user_rate_limit_per_min=1,
            ),
            usage_dao=UsageLogDAO(database.connection),
            egress_meter=EgressMeter(),
            cookie_path=Path("unused-dry-run-cookie-cache"),
            secure_1psid=SecretStr("FAKE_1PSID_FOR_TEST"),
            database=database,
            research=research,  # type: ignore[arg-type]
        )
        fake_application = _DryRunApplication()
        register_handlers(
            fake_application,  # type: ignore[arg-type]
            auth=auth,
            handlers=handlers,
        )

        message = _DryRunMessage("dry-run request")
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=chat_id),
            effective_message=message,
        )
        context = SimpleNamespace()

        await auth(update, context)  # type: ignore[arg-type]
        await handlers.text_message(update, context)  # type: ignore[arg-type]

        research_message = _DryRunMessage("/research dry-run topic")
        research_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=chat_id),
            effective_message=research_message,
        )
        research_context = SimpleNamespace(args=["dry-run", "topic"])
        await auth(research_update, research_context)  # type: ignore[arg-type]
        await handlers.research(
            research_update,  # type: ignore[arg-type]
            research_context,  # type: ignore[arg-type]
        )

        status_message = _DryRunMessage("/research_status")
        status_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=chat_id),
            effective_message=status_message,
        )
        await auth(status_update, context)  # type: ignore[arg-type]
        await handlers.research_status(
            status_update,  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )

        usage = await UsageLogDAO(database.connection).list_for_chat(chat_id)
        assert fake_application.handlers[0][0] == -1
        assert service.execute_count == 1
        assert sessions.session.calls == [
            (
                "dry-run request",
                {
                    "temporary": False,
                    "extended_thinking": False,
                },
            )
        ]
        assert sessions.persisted is True
        assert message.replies == [(PLACEHOLDER_TEXT, {})]
        assert message.placeholder.edits == [
            ("<b>dry-run ok</b>", {"parse_mode": "HTML"})
        ]
        assert await handlers._database_healthy() is True
        assert len(usage) == 1 and usage[0].ok is True
        assert research.submissions == [(chat_id, "dry-run topic")]
        assert research_message.replies == [
            (
                translate(
                    "research.submitted",
                    DEFAULT_LANGUAGE,
                    task_id="dry-run-research-task",
                ),
                {},
            )
        ]
        assert status_message.replies == [
            (
                "\n".join(
                    (
                        translate("research.status_heading", DEFAULT_LANGUAGE),
                        translate(
                            "research.status_line",
                            DEFAULT_LANGUAGE,
                            task_id="dry-run-research-task",
                            status="pending",
                        ),
                    )
                ),
                {},
            )
        ]
    finally:
        await database.close()

    print(
        "dry-run: receive -> allowlist -> queue -> service -> stream -> "
        "render -> send -> research -> database: ok"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gemini Telegram bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the offline integration path once and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.dry_run:
        asyncio.run(_run_dry_run())
        return 0

    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run_polling(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
