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
from .media import MediaHandler, MediaUploadError
from .rendering import render_markdown_chunks

if TYPE_CHECKING:
    from gemini_tg_bot.gemini.sessions import ChatSessionRegistry
    from gemini_tg_bot.storage.db import Database


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
ADMIN_ONLY = "此指令僅限管理員使用。"
ADMIN_NOT_CONFIGURED = "管理員功能尚未設定。"
COOKIE_PROMPT = (
    "請在下一則訊息貼上兩行 cookie：第一行 __Secure-1PSID，"
    "第二行 __Secure-1PSIDTS。該訊息收到後會立即刪除。"
)
COOKIE_INPUT_INVALID = "Cookie 格式無效，請重新執行 /setcookie。"


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
        database: Database | None = None,
        media_handler: MediaHandler | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._sessions = sessions
        self._request_queue = request_queue
        self._usage_dao = usage_dao
        self._egress_meter = egress_meter
        self._cookie_path = cookie_path
        self._secure_1psid = secure_1psid
        self._database = database
        self._media = media_handler or MediaHandler(egress_meter=egress_meter)
        self._auth: AuthMiddleware | None = None
        self._awaiting_cookie_users: set[int] = set()
        self._now = now or (lambda: datetime.now(UTC))

    def bind_auth(self, auth: AuthMiddleware) -> None:
        """Bind the application-scoped authorization middleware."""

        self._auth = auth

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

    async def admin_command(
        self,
        update: Update,
        context: CallbackContext,
    ) -> None:
        """Route the compact set of administrator-only commands."""

        message = update.effective_message
        command = _command_name(message.text if message is not None else None)
        handler = {
            "setcookie": self.setcookie,
            "allow": self.allow,
            "deny": self.deny,
            "health": self.health,
        }.get(command)
        if handler is not None:
            await handler(update, context)

    async def setcookie(
        self,
        update: Update,
        context: CallbackContext,
    ) -> None:
        """Enter the administrator credential-replacement interaction."""

        del context
        identity = await self._require_admin(update)
        if identity is None:
            return
        user_id, _, message = identity
        self._awaiting_cookie_users.add(user_id)
        await message.reply_text(COOKIE_PROMPT)

    async def setcookie_value(
        self,
        update: Update,
        context: CallbackContext,
    ) -> None:
        """Delete a credential message, then hot-restart the sole client."""

        del context
        identity = await self._require_admin(update)
        if identity is None:
            return
        user_id, _, message = identity
        if user_id not in self._awaiting_cookie_users:
            return

        try:
            await message.delete()
        except Exception as error:
            self._awaiting_cookie_users.discard(user_id)
            _log_handler_error("credential message deletion", error)
            await message.reply_text(
                "無法刪除含憑證的訊息；未套用 Cookie，請稍後再試。"
            )
            return

        self._awaiting_cookie_users.discard(user_id)
        credentials = _parse_cookie_credentials(message.text)
        if credentials is None:
            await message.reply_text(COOKIE_INPUT_INVALID)
            return

        secure_1psid, secure_1psidts = credentials
        try:
            await self._service.reinit(
                secure_1psid=secure_1psid,
                secure_1psidts=secure_1psidts,
            )
        except Exception as error:
            _log_handler_error("Gemini client hot restart", error)
            await message.reply_text(
                "Cookie 更新失敗，Gemini 服務尚未恢復。"
                "請重新執行 /setcookie。"
            )
            return

        self._secure_1psid = SecretStr(secure_1psid)
        await message.reply_text("Cookie 已更新，Gemini 服務已熱重啟。")

    async def allow(self, update: Update, context: CallbackContext) -> None:
        """Persist an allow decision that takes effect immediately."""

        identity = await self._require_admin(update)
        if identity is None:
            return
        target_user_id = _command_user_id(context)
        message = identity[2]
        if target_user_id is None:
            await message.reply_text("用法：/allow <user_id>")
            return
        assert self._auth is not None
        try:
            await self._auth.allow(target_user_id)
        except Exception as error:
            _log_handler_error("allowlist write", error)
            await message.reply_text("白名單更新失敗，請稍後再試。")
            return
        await message.reply_text(f"已允許使用者 {target_user_id}。")

    async def deny(self, update: Update, context: CallbackContext) -> None:
        """Persist a deny decision that takes effect immediately."""

        identity = await self._require_admin(update)
        if identity is None:
            return
        target_user_id = _command_user_id(context)
        message = identity[2]
        if target_user_id is None:
            await message.reply_text("用法：/deny <user_id>")
            return
        assert self._auth is not None
        try:
            await self._auth.deny(target_user_id)
        except Exception as error:
            _log_handler_error("denylist write", error)
            await message.reply_text("白名單更新失敗，請稍後再試。")
            return
        await message.reply_text(f"已拒絕使用者 {target_user_id}。")

    async def health(self, update: Update, context: CallbackContext) -> None:
        """Report secret-free client, recent-error, and database health."""

        del context
        identity = await self._require_admin(update)
        if identity is None:
            return
        message = identity[2]
        health = self._service.health
        last_error_kind = health.last_error_kind
        last_error_type = health.last_error_type
        if last_error_kind is None and last_error_type is None:
            last_error = "無"
        else:
            kind = getattr(last_error_kind, "value", last_error_kind)
            last_error = f"{kind or 'unknown'} ({last_error_type or 'unknown'})"
        database_state = "healthy" if await self._database_healthy() else "unavailable"
        accepting = "是" if health.accepting_requests else "否"
        await message.reply_text(
            "\n".join(
                (
                    f"Client 狀態：{health.state.value}",
                    f"接受請求：{accepting}",
                    f"最近錯誤：{last_error}",
                    f"DB 狀態：{database_state}",
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

        identity = _message_identity(update)
        if identity is None:
            return
        user_id, chat_id, message = identity
        if user_id in self._awaiting_cookie_users:
            await self.setcookie_value(update, context)
            return
        del context
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
            await self._reply_output(message, output)
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

    async def user_message(
        self,
        update: Update,
        context: CallbackContext,
    ) -> None:
        """Route ordinary text and supported Telegram uploads."""

        message = update.effective_message
        if message is None:
            return
        if getattr(message, "photo", None) or getattr(message, "document", None):
            await self.media_message(update, context)
            return
        await self.text_message(update, context)

    async def media_message(
        self,
        update: Update,
        context: CallbackContext,
    ) -> None:
        """Send one size-checked photo/document through the current session."""

        del context
        identity = _message_identity(update)
        if identity is None:
            return
        user_id, chat_id, message = identity
        started = time.monotonic()
        state = await self._sessions.get_state(chat_id)
        ok = False
        error_kind: str | None = None

        try:
            async with self._media.prepare_upload(message) as upload:
                async with self._request_queue.request(user_id):
                    session = await self._sessions.get_or_create(chat_id)
                    output = await self._service.execute(
                        lambda _client: session.send_message(
                            upload.prompt,
                            files=upload.files,
                            temporary=state.temporary,
                        )
                    )
                self._media.record_upload(upload)
            await self._sessions.persist(chat_id, session)
            await self._reply_output(message, output)
            ok = True
        except MediaUploadError as error:
            error_kind = "media_rejected"
            await message.reply_text(str(error))
        except RateLimitExceeded as error:
            error_kind = "rate_limit"
            await _reply_rate_limited(message, error)
        except ServiceUnavailableError:
            error_kind = "unavailable"
            await message.reply_text(SERVICE_UNAVAILABLE)
        except Exception as error:
            error_kind = classify_error(error).value
            _log_handler_error("media message", error)
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

    async def _reply_output(self, message: Any, output: Any) -> None:
        images = getattr(output, "images", ())
        LOGGER.debug(
            "Gemini response text=%r image_count=%d",
            output.text,
            len(images),
        )
        for index, image in enumerate(images):
            LOGGER.debug(
                "Gemini response image index=%d url=%s title=%r alt=%r",
                index,
                image.url,
                image.title,
                image.alt,
            )

        await _reply_rendered(message, output.text)
        if images:
            await self._media.send_output_images(message, output)

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

    async def _require_admin(
        self,
        update: Update,
    ) -> tuple[int, int, Any] | None:
        identity = _message_identity(update)
        if identity is None:
            return None
        user_id, _, message = identity
        if self._auth is None:
            await message.reply_text(ADMIN_NOT_CONFIGURED)
            return None
        try:
            is_admin = self._auth.is_admin(user_id)
        except ValueError:
            is_admin = False
        if not is_admin:
            await message.reply_text(ADMIN_ONLY)
            return None
        return identity

    async def _database_healthy(self) -> bool:
        if self._database is None:
            return False
        try:
            async with self._database.connection.execute("SELECT 1") as cursor:
                return await cursor.fetchone() is not None
        except Exception as error:
            _log_handler_error("database health check", error)
            return False


def register_handlers(
    application: Application[Any, Any, Any, Any, Any, Any],
    *,
    auth: AuthMiddleware,
    handlers: TelegramHandlers,
) -> None:
    """Register authorization first, then commands, callbacks, and text."""

    handlers.bind_auth(auth)
    application.add_handler(TypeHandler(Update, auth), group=-1)
    application.add_handler(CommandHandler(["start", "help"], handlers.start))
    application.add_handler(CommandHandler("new", handlers.new))
    application.add_handler(CommandHandler("model", handlers.model))
    application.add_handler(CommandHandler("gem", handlers.gem))
    application.add_handler(CommandHandler("temp", handlers.temp))
    application.add_handler(CommandHandler("status", handlers.status))
    application.add_handler(
        CommandHandler(
            ["setcookie", "allow", "deny", "health"],
            handlers.admin_command,
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handlers.callback,
            pattern=rf"^(?:{MODEL_CALLBACK_PREFIX}|{GEM_CALLBACK_PREFIX})",
        )
    )
    user_messages = (
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.ALL
    )
    application.add_handler(MessageHandler(user_messages, handlers.user_message))


def _message_identity(update: Update) -> tuple[int, int, Any] | None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if user is None or chat is None or message is None:
        return None
    return user.id, chat.id, message


def _command_name(text: str | None) -> str | None:
    if not text:
        return None
    token = text.split(maxsplit=1)[0]
    if not token.startswith("/"):
        return None
    return token[1:].split("@", maxsplit=1)[0].lower()


def _command_user_id(context: CallbackContext) -> int | None:
    args = getattr(context, "args", None)
    if not isinstance(args, (list, tuple)) or len(args) != 1:
        return None
    try:
        user_id = int(args[0])
    except (TypeError, ValueError):
        return None
    return user_id if user_id > 0 else None


def _parse_cookie_credentials(text: str | None) -> tuple[str, str] | None:
    if not text:
        return None

    parts = [part.strip() for part in text.replace(";", "\n").splitlines()]
    parts = [part for part in parts if part]
    if len(parts) == 1:
        parts = parts[0].split()

    if len(parts) != 2:
        return None
    if all("=" not in part for part in parts):
        return (parts[0], parts[1]) if all(parts) else None

    values: dict[str, str] = {}
    for part in parts:
        key, separator, value = part.partition("=")
        if not separator or not value or key in values:
            return None
        values[key] = value
    secure_1psid = values.get("__Secure-1PSID")
    secure_1psidts = values.get("__Secure-1PSIDTS")
    if not secure_1psid or not secure_1psidts or len(values) != 2:
        return None
    return secure_1psid, secure_1psidts


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
