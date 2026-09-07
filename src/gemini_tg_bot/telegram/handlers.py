"""Telegram command, callback, and text-message handlers."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from gemini_tg_bot.gemini.errors import classify_error
from gemini_tg_bot.gemini.service import GeminiService, ServiceUnavailableError
from gemini_tg_bot.queue import RateLimitExceeded, RequestQueue
from gemini_tg_bot.storage.models import UsageLog, UsageLogDAO

from .auth import AuthMiddleware
from .rendering import render_markdown_chunks

if TYPE_CHECKING:
    from gemini_tg_bot.gemini.sessions import ChatSessionRegistry


LOGGER = logging.getLogger(__name__)

CALLBACK_DATA_LIMIT = 64
MODEL_CALLBACK_PREFIX = "model:"
GEM_CALLBACK_PREFIX = "gem:"

HELP_TEXT = """可用指令：
/new — 開始新的對話
/model — 選擇 Gemini 模型
/gem — 選擇 Gem
/temp — 切換 temporary mode
/status — 查看目前狀態

直接傳送文字即可延續目前對話。"""

MODEL_LIST_UNAVAILABLE = "模型清單暫時無法取得，請稍後再試。"
GEM_LIST_UNAVAILABLE = "Gem 清單暫時無法取得，請稍後再試。"
SERVICE_UNAVAILABLE = "Gemini 服務目前無法接受請求，請稍後再試。"
GENERIC_FAILURE = "處理請求時發生錯誤，請稍後再試。"


class EgressMeter:
    """Count media bytes relayed through the VM during the current month."""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        current = self._now()
        self._month = (current.year, current.month)
        self._bytes = 0

    def record(self, num_bytes: int) -> None:
        """Add a non-negative number of relayed bytes."""

        if isinstance(num_bytes, bool) or not isinstance(num_bytes, int):
            raise TypeError("num_bytes must be an integer")
        if num_bytes < 0:
            raise ValueError("num_bytes must not be negative")
        self._roll_month()
        self._bytes += num_bytes

    @property
    def month_to_date_bytes(self) -> int:
        """Return the current month's in-process egress estimate."""

        self._roll_month()
        return self._bytes

    def _roll_month(self) -> None:
        current = self._now()
        month = (current.year, current.month)
        if month != self._month:
            self._month = month
            self._bytes = 0


class TelegramHandlers:
    """Bind Telegram updates to the queue, service, and session registry."""

    def __init__(
        self,
        *,
        service: GeminiService,
        sessions: ChatSessionRegistry,
        request_queue: RequestQueue,
        usage_dao: UsageLogDAO | None,
        egress_meter: EgressMeter,
        cookie_path: Path,
        secure_1psid: SecretStr,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._sessions = sessions
        self._request_queue = request_queue
        self._usage_dao = usage_dao
        self._egress_meter = egress_meter
        self._cookie_path = cookie_path
        self._secure_1psid = secure_1psid
        self._now = now or (lambda: datetime.now(UTC))

    async def start(self, update: Update, context: CallbackContext) -> None:
        """Explain the bot's user-facing commands."""

        del context
        message = update.effective_message
        if message is not None:
            await message.reply_text(HELP_TEXT)

    async def help(self, update: Update, context: CallbackContext) -> None:
        """Alias for :meth:`start`."""

        await self.start(update, context)

    async def new(self, update: Update, context: CallbackContext) -> None:
        """Reset conversation identifiers while preserving user settings."""

        del context
        identity = _message_identity(update)
        if identity is None:
            return
        _, chat_id, message = identity
        await self._sessions.reset(chat_id)
        await message.reply_text("已開始新的對話。")

    async def model(self, update: Update, context: CallbackContext) -> None:
        """Dynamically list currently available upstream models."""

        del context
        identity = _message_identity(update)
        if identity is None:
            return
        user_id, _, message = identity

        try:
            async with self._request_queue.request(user_id):
                models = await self._service.execute(
                    lambda client: client.list_models()
                )
        except RateLimitExceeded as error:
            await _reply_rate_limited(message, error)
            return
        except ServiceUnavailableError:
            await message.reply_text(SERVICE_UNAVAILABLE)
            return
        except Exception as error:
            _log_handler_error("model list", error)
            await message.reply_text(MODEL_LIST_UNAVAILABLE)
            return

        buttons: list[list[InlineKeyboardButton]] = []
        if models is not None:
            for model in models:
                if not model.is_available:
                    continue
                callback_data = f"{MODEL_CALLBACK_PREFIX}{model.model_name}"
                if not _valid_callback_data(callback_data):
                    LOGGER.warning(
                        "Skipping model whose Telegram callback data exceeds %d bytes",
                        CALLBACK_DATA_LIMIT,
                    )
                    continue
                buttons.append(
                    [
                        InlineKeyboardButton(
                            model.display_name,
                            callback_data=callback_data,
                        )
                    ]
                )

        if not buttons:
            await message.reply_text(MODEL_LIST_UNAVAILABLE)
            return
        await message.reply_text(
            "請選擇模型：",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def gem(self, update: Update, context: CallbackContext) -> None:
        """Dynamically list the account's visible Gems."""

        del context
        identity = _message_identity(update)
        if identity is None:
            return
        user_id, _, message = identity

        try:
            async with self._request_queue.request(user_id):
                gem_jar = await self._service.execute(
                    lambda client: client.fetch_gems(include_hidden=False)
                )
        except RateLimitExceeded as error:
            await _reply_rate_limited(message, error)
            return
        except ServiceUnavailableError:
            await message.reply_text(SERVICE_UNAVAILABLE)
            return
        except Exception as error:
            _log_handler_error("gem list", error)
            await message.reply_text(GEM_LIST_UNAVAILABLE)
            return

        buttons: list[list[InlineKeyboardButton]] = []
        if gem_jar is not None:
            for gem in gem_jar.values():
                callback_data = f"{GEM_CALLBACK_PREFIX}{gem.id}"
                if not _valid_callback_data(callback_data):
                    LOGGER.warning(
                        "Skipping Gem whose Telegram callback data exceeds %d bytes",
                        CALLBACK_DATA_LIMIT,
                    )
                    continue
                buttons.append(
                    [InlineKeyboardButton(gem.name, callback_data=callback_data)]
                )

        if not buttons:
            await message.reply_text(GEM_LIST_UNAVAILABLE)
            return
        await message.reply_text(
            "請選擇 Gem：",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def temp(self, update: Update, context: CallbackContext) -> None:
        """Toggle the persisted temporary-conversation setting."""

        del context
        identity = _message_identity(update)
        if identity is None:
            return
        _, chat_id, message = identity
        state = await self._sessions.get_state(chat_id)
        enabled = not state.temporary
        await self._sessions.set_temporary(chat_id, enabled)
        label = "開啟" if enabled else "關閉"
        await message.reply_text(f"Temporary mode 已{label}。")

    async def status(self, update: Update, context: CallbackContext) -> None:
        """Report session, queue, refresh, usage, and egress state."""

        del context
        identity = _message_identity(update)
        if identity is None:
            return
        _, chat_id, message = identity
        state = await self._sessions.get_state(chat_id)
        health = self._service.health
        refresh_time = self._cookie_last_refresh()
        refresh_label = (
            "尚未刷新"
            if refresh_time is None
            else refresh_time.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        )
        today_usage = await self._today_usage(chat_id)
        reason = (
            ""
            if health.degraded_reason is None
            else f" ({health.degraded_reason.value})"
        )
        temporary = "開啟" if state.temporary else "關閉"
        await message.reply_text(
            "\n".join(
                (
                    f"目前模型：{state.model or '帳號預設'}",
                    f"Session CID：{state.cid or '尚未建立'}",
                    f"Temporary mode：{temporary}",
                    f"服務狀態：{health.state.value}{reason}",
                    f"Cookie 最後刷新時間：{refresh_label}",
                    f"佇列深度：{self._request_queue.queue_depth}",
                    f"今日用量：{today_usage}",
                    "本月累計 egress 估算值："
                    f"{_format_bytes(self._egress_meter.month_to_date_bytes)}",
                )
            )
        )

    async def callback(self, update: Update, context: CallbackContext) -> None:
        """Apply a model or Gem selected by an inline keyboard callback."""

        del context
        query = update.callback_query
        user = update.effective_user
        chat = update.effective_chat
        if query is None or user is None or chat is None:
            return
        await query.answer()
        data = query.data
        if not isinstance(data, str) or not _valid_callback_data(data):
            await query.edit_message_text("無效的選項，請重新執行指令。")
            return

        if data.startswith(MODEL_CALLBACK_PREFIX):
            await self._select_model(
                query=query,
                user_id=user.id,
                chat_id=chat.id,
                model_name=data[len(MODEL_CALLBACK_PREFIX) :],
            )
        elif data.startswith(GEM_CALLBACK_PREFIX):
            await self._select_gem(
                query=query,
                user_id=user.id,
                chat_id=chat.id,
                gem_id=data[len(GEM_CALLBACK_PREFIX) :],
            )

    async def text_message(
        self,
        update: Update,
        context: CallbackContext,
    ) -> None:
        """Send plain text through the current ChatSession and render its reply."""

        del context
        identity = _message_identity(update)
        if identity is None:
            return
        user_id, chat_id, message = identity
        prompt = message.text
        if not prompt:
            return

        started = time.monotonic()
        state = await self._sessions.get_state(chat_id)
        ok = False
        error_kind: str | None = None
        try:
            async with self._request_queue.request(user_id):
                session = await self._sessions.get_or_create(chat_id)
                output = await self._service.execute(
                    lambda _client: session.send_message(
                        prompt,
                        temporary=state.temporary,
                    )
                )
            await self._sessions.persist(chat_id, session)
            await _reply_rendered(message, output.text)
            ok = True
        except RateLimitExceeded as error:
            error_kind = "rate_limit"
            await _reply_rate_limited(message, error)
        except ServiceUnavailableError:
            error_kind = "unavailable"
            await message.reply_text(SERVICE_UNAVAILABLE)
        except Exception as error:
            error_kind = classify_error(error).value
            _log_handler_error("text message", error)
            await message.reply_text(GENERIC_FAILURE)
        finally:
            await self._record_usage(
                user_id=user_id,
                chat_id=chat_id,
                model=state.model,
                ok=ok,
                error_kind=error_kind,
                latency_ms=round((time.monotonic() - started) * 1000),
            )

    async def _select_model(
        self,
        *,
        query: Any,
        user_id: int,
        chat_id: int,
        model_name: str,
    ) -> None:
        if not model_name:
            await query.edit_message_text("無效的模型，請重新執行 /model。")
            return

        def resolve(client: Any) -> Any | None:
            models = client.list_models()
            if models is None or not any(
                model.is_available and model.model_name == model_name
                for model in models
            ):
                return None
            return client.resolve_model(model_name)

        try:
            async with self._request_queue.request(user_id):
                selected = await self._service.execute(resolve)
        except RateLimitExceeded as error:
            await query.edit_message_text(_rate_limit_message(error))
            return
        except ServiceUnavailableError:
            await query.edit_message_text(SERVICE_UNAVAILABLE)
            return
        except Exception as error:
            _log_handler_error("model selection", error)
            await query.edit_message_text(MODEL_LIST_UNAVAILABLE)
            return

        if selected is None:
            await query.edit_message_text("此模型已無法使用，請重新執行 /model。")
            return
        await self._sessions.set_model(chat_id, selected.model_name)
        await query.edit_message_text(f"已選擇模型：{selected.display_name}")

    async def _select_gem(
        self,
        *,
        query: Any,
        user_id: int,
        chat_id: int,
        gem_id: str,
    ) -> None:
        if not gem_id:
            await query.edit_message_text("無效的 Gem，請重新執行 /gem。")
            return

        try:
            async with self._request_queue.request(user_id):
                gem_jar = await self._service.execute(
                    lambda client: client.fetch_gems(include_hidden=False)
                )
        except RateLimitExceeded as error:
            await query.edit_message_text(_rate_limit_message(error))
            return
        except ServiceUnavailableError:
            await query.edit_message_text(SERVICE_UNAVAILABLE)
            return
        except Exception as error:
            _log_handler_error("gem selection", error)
            await query.edit_message_text(GEM_LIST_UNAVAILABLE)
            return

        selected = None if gem_jar is None else gem_jar.get(id=gem_id)
        if selected is None:
            await query.edit_message_text("此 Gem 已無法使用，請重新執行 /gem。")
            return
        await self._sessions.set_gem(chat_id, selected.id)
        await query.edit_message_text(f"已選擇 Gem：{selected.name}")

    def _cookie_last_refresh(self) -> datetime | None:
        cache_file = self._cookie_path / (
            ".cached_cookies_"
            f"{self._secure_1psid.get_secret_value()}.json"
        )
        try:
            modified_at = cache_file.stat().st_mtime
        except OSError:
            return None
        return datetime.fromtimestamp(modified_at, tz=UTC)

    async def _today_usage(self, chat_id: int) -> int:
        if self._usage_dao is None:
            return 0
        try:
            entries = await self._usage_dao.list_for_chat(chat_id)
        except Exception as error:
            _log_handler_error("usage lookup", error)
            return 0

        today = self._now().astimezone(UTC).date()
        return sum(_created_on(entry.created_at, today) for entry in entries)

    async def _record_usage(
        self,
        *,
        user_id: int,
        chat_id: int,
        model: str | None,
        ok: bool,
        error_kind: str | None,
        latency_ms: int,
    ) -> None:
        if self._usage_dao is None:
            return
        try:
            await self._usage_dao.add(
                UsageLog(
                    user_id=user_id,
                    chat_id=chat_id,
                    command="message",
                    model=model,
                    ok=ok,
                    error_kind=error_kind,
                    latency_ms=latency_ms,
                    created_at=self._now().astimezone(UTC).isoformat(),
                )
            )
        except Exception as error:
            _log_handler_error("usage write", error)


def register_handlers(
    application: Application[Any, Any, Any, Any, Any, Any],
    *,
    auth: AuthMiddleware,
    handlers: TelegramHandlers,
) -> None:
    """Register authorization first, then commands, callbacks, and text."""

    application.add_handler(TypeHandler(Update, auth), group=-1)
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help))
    application.add_handler(CommandHandler("new", handlers.new))
    application.add_handler(CommandHandler("model", handlers.model))
    application.add_handler(CommandHandler("gem", handlers.gem))
    application.add_handler(CommandHandler("temp", handlers.temp))
    application.add_handler(CommandHandler("status", handlers.status))
    application.add_handler(
        CallbackQueryHandler(
            handlers.callback,
            pattern=rf"^(?:{MODEL_CALLBACK_PREFIX}|{GEM_CALLBACK_PREFIX})",
        )
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.text_message)
    )


def _message_identity(update: Update) -> tuple[int, int, Any] | None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if user is None or chat is None or message is None:
        return None
    return user.id, chat.id, message


def _valid_callback_data(value: str) -> bool:
    return bool(value) and len(value.encode("utf-8")) <= CALLBACK_DATA_LIMIT


async def _reply_rendered(message: Any, markdown: str) -> None:
    chunks = render_markdown_chunks(markdown)
    if not chunks:
        await message.reply_text("Gemini 未回傳文字。")
        return
    for chunk in chunks:
        await message.reply_text(chunk, parse_mode=ParseMode.HTML)


def _rate_limit_message(error: RateLimitExceeded) -> str:
    seconds = max(1, round(error.retry_after))
    return f"請求過於頻繁，請約 {seconds} 秒後再試。"


async def _reply_rate_limited(message: Any, error: RateLimitExceeded) -> None:
    await message.reply_text(_rate_limit_message(error))


def _created_on(value: str, expected: date) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).date() == expected


def _format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024**2:
        return f"{num_bytes / 1024:.1f} KiB"
    if num_bytes < 1024**3:
        return f"{num_bytes / 1024**2:.1f} MiB"
    return f"{num_bytes / 1024**3:.2f} GiB"


def _log_handler_error(operation: str, error: BaseException) -> None:
    # Do not log exception text or traceback: upstream errors can contain a
    # request path, and the cookie cache path embeds credential material.
    LOGGER.error(
        "Telegram handler operation failed operation=%s error_type=%s",
        operation,
        type(error).__name__,
    )


__all__ = [
    "EgressMeter",
    "TelegramHandlers",
    "register_handlers",
]
