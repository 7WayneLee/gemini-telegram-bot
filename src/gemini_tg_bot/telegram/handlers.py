"""Telegram command, callback, and text-message handlers."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gemini_webapi.constants import AccountStatus
from pydantic import SecretStr
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
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

from gemini_tg_bot.config import persist_runtime_credentials
from gemini_tg_bot.gemini.errors import AccountStatusError, classify_error
from gemini_tg_bot.gemini.service import (
    DegradedReason,
    GeminiService,
    ServiceUnavailableError,
    account_status_guidance,
)
from gemini_tg_bot.queue import QueueAcquireTimeout, RateLimitExceeded, RequestQueue
from gemini_tg_bot.storage.models import UsageLog, UsageLogDAO

from .auth import AuthMiddleware
from .media import MediaHandler, MediaUploadError, caption_is_eligible
from .rendering import render_markdown_chunks
from .sending import (
    FloodControlExceeded,
    SERVICE_BUSY,
    answer_callback,
    call_telegram,
    delete_message,
    edit_message_text_or_busy,
    send_text_or_busy,
)
from .streaming import stream_response

if TYPE_CHECKING:
    from gemini_tg_bot.gemini.research import ResearchManager
    from gemini_tg_bot.gemini.sessions import ChatSessionRegistry
    from gemini_tg_bot.storage.db import Database


LOGGER = logging.getLogger(__name__)

CALLBACK_DATA_LIMIT = 64
MODEL_CALLBACK_PREFIX = "model:"
GEM_CALLBACK_PREFIX = "gem:"

PUBLIC_BOT_COMMANDS = (
    BotCommand("start", "顯示使用說明"),
    BotCommand("help", "顯示使用說明"),
    BotCommand("new", "開始新的對話"),
    BotCommand("model", "選擇 Gemini 模型"),
    BotCommand("gem", "選擇 Gem"),
    BotCommand("temp", "切換暫時對話模式"),
    BotCommand("img", "生成圖片"),
    BotCommand("research", "提交 Deep Research 任務"),
    BotCommand("research_status", "查看 Deep Research 任務狀態"),
    BotCommand("status", "查看目前狀態"),
)

HELP_TEXT = "\n".join(
    (
        "可用指令：",
        *(f"/{item.command} — {item.description}" for item in PUBLIC_BOT_COMMANDS),
        "",
        "直接傳送文字即可延續目前對話。",
    )
)

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
RESEARCH_USAGE = "用法：/research <topic>"
RESEARCH_UNAVAILABLE = "Deep Research 服務目前無法使用，請稍後再試。"
IMAGE_USAGE = "用法：/img <prompt>"
IMAGE_GENERATION_PREFIX = (
    "Generate an original AI image based on the following request. "
    "Do not search for or return existing web images:"
)
class _StreamingMessageProxy:
    """Capture the placeholder while delegating Telegram reply operations."""

    def __init__(self, message: Any) -> None:
        self._message = message
        self.placeholder: Any | None = None

    def reply_text(self, *args: Any, **kwargs: Any) -> Any:
        operation = self._message.reply_text
        return self._capture_reply(operation, *args, **kwargs)

    async def _capture_reply(
        self,
        operation: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        reply = await operation(*args, **kwargs)
        if self.placeholder is None:
            self.placeholder = reply
        return reply


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
        research: ResearchManager | None = None,
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
        self._research = research
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
            await send_text_or_busy(message, HELP_TEXT)

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
        await send_text_or_busy(message, "已開始新的對話。")

    async def model(self, update: Update, context: CallbackContext) -> None:
        """Dynamically list currently available upstream models."""

        del context
        identity = _message_identity(update)
        if identity is None:
            return
        user_id, _, message = identity

        try:
            self._ensure_service_accepting_requests()
            async with self._request_queue.request(user_id):
                models = await self._service.execute(
                    lambda client: client.list_models()
                )
        except RateLimitExceeded as error:
            await _reply_rate_limited(message, error)
            return
        except QueueAcquireTimeout:
            await send_text_or_busy(message, SERVICE_BUSY)
            return
        except ServiceUnavailableError as error:
            await send_text_or_busy(message, _service_unavailable_message(error))
            return
        except Exception as error:
            _log_handler_error("model list", error)
            await send_text_or_busy(message, MODEL_LIST_UNAVAILABLE)
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
            await send_text_or_busy(message, MODEL_LIST_UNAVAILABLE)
            return
        await send_text_or_busy(
            message,
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
            self._ensure_service_accepting_requests()
            async with self._request_queue.request(user_id):
                gem_jar = await self._service.execute(
                    lambda client: client.fetch_gems(include_hidden=False)
                )
        except RateLimitExceeded as error:
            await _reply_rate_limited(message, error)
            return
        except QueueAcquireTimeout:
            await send_text_or_busy(message, SERVICE_BUSY)
            return
        except ServiceUnavailableError as error:
            await send_text_or_busy(message, _service_unavailable_message(error))
            return
        except Exception as error:
            _log_handler_error("gem list", error)
            await send_text_or_busy(message, GEM_LIST_UNAVAILABLE)
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
            await send_text_or_busy(message, GEM_LIST_UNAVAILABLE)
            return
        await send_text_or_busy(
            message,
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
        await send_text_or_busy(message, f"Temporary mode 已{label}。")

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
        await send_text_or_busy(
            message,
            "\n".join(
                (
                    f"目前模型：{state.model or '帳號預設'}",
                    f"Session CID：{state.cid or '尚未建立'}",
                    f"Temporary mode：{temporary}",
                    f"服務狀態：{health.state.value}{reason}",
                    "Account status："
                    f"{_account_status_label(getattr(health, 'account_status', None))}",
                    f"Cookie 最後刷新時間：{refresh_label}",
                    f"佇列深度：{self._request_queue.queue_depth}",
                    f"今日用量：{today_usage}",
                    "本月累計 egress 估算值："
                    f"{_format_bytes(self._egress_meter.month_to_date_bytes)}",
                )
            )
        )

    async def research(self, update: Update, context: CallbackContext) -> None:
        """Submit Deep Research work and return its task id immediately."""

        identity = _message_identity(update)
        if identity is None:
            return
        _, chat_id, message = identity
        prompt = _command_prompt(context)
        if prompt is None:
            await send_text_or_busy(message, RESEARCH_USAGE)
            return
        if self._research is None:
            await send_text_or_busy(message, RESEARCH_UNAVAILABLE)
            return

        try:
            task_id = await self._research.submit(chat_id, prompt)
        except ValueError:
            await send_text_or_busy(message, RESEARCH_USAGE)
            return
        except Exception as error:
            _log_handler_error("research submission", error)
            await send_text_or_busy(message, RESEARCH_UNAVAILABLE)
            return
        await send_text_or_busy(message, f"Deep Research 任務已提交：{task_id}")

    async def img(self, update: Update, context: CallbackContext) -> None:
        """Generate an image through the existing streaming media path."""

        identity = _message_identity(update)
        if identity is None:
            return
        prompt = _command_prompt(context)
        message = identity[2]
        if prompt is None:
            await send_text_or_busy(message, IMAGE_USAGE)
            return

        await self._stream_prompt(
            identity,
            f"{IMAGE_GENERATION_PREFIX}\n\n{prompt}",
        )

    async def research_status(
        self,
        update: Update,
        context: CallbackContext,
    ) -> None:
        """List persisted Deep Research task states for the current chat."""

        del context
        identity = _message_identity(update)
        if identity is None:
            return
        _, chat_id, message = identity
        if self._research is None:
            await send_text_or_busy(message, RESEARCH_UNAVAILABLE)
            return

        try:
            tasks = await self._research.status(chat_id)
        except Exception as error:
            _log_handler_error("research status", error)
            await send_text_or_busy(message, RESEARCH_UNAVAILABLE)
            return
        if not tasks:
            await send_text_or_busy(message, "目前沒有 Deep Research 任務。")
            return

        lines = ["Deep Research 任務狀態："]
        lines.extend(f"{task.task_id}：{task.status.value}" for task in tasks)
        await send_text_or_busy(message, "\n".join(lines))

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
        await send_text_or_busy(message, COOKIE_PROMPT)

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
            await delete_message(message)
        except Exception as error:
            self._awaiting_cookie_users.discard(user_id)
            _log_handler_error("credential message deletion", error)
            await send_text_or_busy(
                message,
                "無法刪除含憑證的訊息；未套用 Cookie，請稍後再試。"
            )
            return

        self._awaiting_cookie_users.discard(user_id)
        credentials = _parse_cookie_credentials(message.text)
        if credentials is None:
            await send_text_or_busy(message, COOKIE_INPUT_INVALID)
            return

        secure_1psid, secure_1psidts = credentials
        try:
            await self._service.reinit(
                secure_1psid=secure_1psid,
                secure_1psidts=secure_1psidts,
            )
        except AccountStatusError as error:
            _log_handler_error("Gemini client hot restart", error)
            await send_text_or_busy(
                message,
                "Cookie 更新失敗，Gemini 服務尚未恢復。\n"
                f"{account_status_guidance(error.status)}",
            )
            return
        except Exception as error:
            _log_handler_error("Gemini client hot restart", error)
            await send_text_or_busy(
                message,
                "Cookie 更新失敗，Gemini 服務尚未恢復。"
                "請重新執行 /setcookie。"
            )
            return

        account_status = getattr(
            self._service.health,
            "account_status",
            None,
        )
        if account_status is not AccountStatus.AVAILABLE:
            if isinstance(account_status, AccountStatus):
                guidance = account_status_guidance(account_status)
            else:
                guidance = "Gemini 帳號狀態無法確認，請稍後重試。"
            await send_text_or_busy(
                message,
                f"Cookie 更新失敗，Gemini 服務尚未恢復。\n{guidance}",
            )
            return

        self._secure_1psid = SecretStr(secure_1psid)
        try:
            persist_runtime_credentials(
                self._cookie_path,
                secure_1psid,
                secure_1psidts,
            )
        except Exception as error:
            _log_handler_error("runtime credential persistence", error)
            await send_text_or_busy(
                message,
                "Cookie 已套用且 Gemini 服務已恢復，"
                "但無法保存供重啟使用；請重新執行 /setcookie。",
            )
            return
        await send_text_or_busy(
            message,
            "Cookie 已更新，Gemini 服務已熱重啟。",
        )

    async def allow(self, update: Update, context: CallbackContext) -> None:
        """Persist an allow decision that takes effect immediately."""

        identity = await self._require_admin(update)
        if identity is None:
            return
        target_user_id = _command_user_id(context)
        message = identity[2]
        if target_user_id is None:
            await send_text_or_busy(message, "用法：/allow <user_id>")
            return
        assert self._auth is not None
        try:
            await self._auth.allow(target_user_id)
        except Exception as error:
            _log_handler_error("allowlist write", error)
            await send_text_or_busy(
                message,
                "白名單更新失敗，請稍後再試。",
            )
            return
        await send_text_or_busy(message, f"已允許使用者 {target_user_id}。")

    async def deny(self, update: Update, context: CallbackContext) -> None:
        """Persist a deny decision that takes effect immediately."""

        identity = await self._require_admin(update)
        if identity is None:
            return
        target_user_id = _command_user_id(context)
        message = identity[2]
        if target_user_id is None:
            await send_text_or_busy(message, "用法：/deny <user_id>")
            return
        assert self._auth is not None
        try:
            await self._auth.deny(target_user_id)
        except Exception as error:
            _log_handler_error("denylist write", error)
            await send_text_or_busy(
                message,
                "白名單更新失敗，請稍後再試。",
            )
            return
        await send_text_or_busy(message, f"已拒絕使用者 {target_user_id}。")

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
        await send_text_or_busy(
            message,
            "\n".join(
                (
                    f"Client 狀態：{health.state.value}",
                    "Account status："
                    f"{_account_status_label(getattr(health, 'account_status', None))}",
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
        try:
            await answer_callback(query)
        except FloodControlExceeded:
            await edit_message_text_or_busy(query, SERVICE_BUSY)
            return
        data = query.data
        if not isinstance(data, str) or not _valid_callback_data(data):
            await edit_message_text_or_busy(
                query,
                "無效的選項，請重新執行指令。",
            )
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
        """Stream plain text through the current ChatSession."""

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

        await self._stream_prompt(identity, prompt)

    async def _stream_prompt(
        self,
        identity: tuple[int, int, Any],
        prompt: str,
    ) -> None:
        """Stream one prompt and deliver its images through ``MediaHandler``."""

        user_id, chat_id, message = identity
        started = time.monotonic()
        state = await self._sessions.get_state(chat_id)
        ok = False
        error_kind: str | None = None
        stream_message = _StreamingMessageProxy(message)
        try:
            self._ensure_service_accepting_requests()
            async with self._request_queue.request(user_id) as permit:
                session = await self._sessions.get_or_create(chat_id)
                streamed = await self._service.execute(
                    lambda client: stream_response(
                        stream_message,
                        client,
                        prompt,
                        chat=session,
                        temporary=state.temporary,
                        flood_wait=permit.wait_for_flood_control,
                    )
                )
            await self._sessions.persist(chat_id, session)
            await self._reply_streamed_output_images(
                message,
                stream_message.placeholder,
                streamed.text,
                streamed.output,
            )
            ok = True
        except RateLimitExceeded as error:
            error_kind = "rate_limit"
            await _reply_rate_limited(message, error)
        except FloodControlExceeded as error:
            error_kind = "flood_control"
            _log_handler_error("Telegram flood control", error)
            await send_text_or_busy(message, SERVICE_BUSY)
        except QueueAcquireTimeout as error:
            error_kind = "queue_timeout"
            _log_handler_error("request queue acquisition", error)
            await send_text_or_busy(message, SERVICE_BUSY)
        except ServiceUnavailableError as error:
            error_kind = "unavailable"
            await send_text_or_busy(message, _service_unavailable_message(error))
        except Exception as error:
            error_kind = classify_error(error).value
            _log_handler_error("text message", error)
            await send_text_or_busy(message, GENERIC_FAILURE)
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
            self._ensure_service_accepting_requests()
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
            await send_text_or_busy(message, str(error))
        except RateLimitExceeded as error:
            error_kind = "rate_limit"
            await _reply_rate_limited(message, error)
        except FloodControlExceeded as error:
            error_kind = "flood_control"
            _log_handler_error("Telegram flood control", error)
            await send_text_or_busy(message, SERVICE_BUSY)
        except QueueAcquireTimeout as error:
            error_kind = "queue_timeout"
            _log_handler_error("request queue acquisition", error)
            await send_text_or_busy(message, SERVICE_BUSY)
        except ServiceUnavailableError as error:
            error_kind = "unavailable"
            await send_text_or_busy(message, _service_unavailable_message(error))
        except Exception as error:
            error_kind = classify_error(error).value
            _log_handler_error("media message", error)
            await send_text_or_busy(message, GENERIC_FAILURE)
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
        rendered_chunks = render_markdown_chunks(output.text)
        images = getattr(output, "images", ())
        caption = _caption_from_chunks(rendered_chunks) if images else None
        if images and (caption is not None or not rendered_chunks):
            await self._reply_output_images(message, output, caption=caption)
            return
        await _reply_rendered_chunks(message, rendered_chunks)
        await self._reply_output_images(message, output)

    async def _reply_streamed_output_images(
        self,
        message: Any,
        placeholder: Any | None,
        markdown: str,
        output: Any | None,
    ) -> None:
        images = getattr(output, "images", ()) if output is not None else ()
        rendered_chunks = render_markdown_chunks(markdown)
        caption = _caption_from_chunks(rendered_chunks) if images else None
        if images and not rendered_chunks:
            await _delete_placeholder(placeholder)
            await self._reply_output_images(message, output, caption=None)
            return
        await self._reply_output_images(message, output, caption=caption)
        if images and (caption is not None or not rendered_chunks):
            await _delete_placeholder(placeholder)

    async def _reply_output_images(
        self,
        message: Any,
        output: Any | None,
        *,
        caption: str | None = None,
    ) -> None:
        if output is None:
            LOGGER.debug("Gemini response text='' image_count=0")
            return

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

        if images:
            await self._media.send_output_images(message, output, caption=caption)

    async def _select_model(
        self,
        *,
        query: Any,
        user_id: int,
        chat_id: int,
        model_name: str,
    ) -> None:
        if not model_name:
            await edit_message_text_or_busy(
                query,
                "無效的模型，請重新執行 /model。",
            )
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
            self._ensure_service_accepting_requests()
            async with self._request_queue.request(user_id):
                selected = await self._service.execute(resolve)
        except RateLimitExceeded as error:
            await edit_message_text_or_busy(query, _rate_limit_message(error))
            return
        except QueueAcquireTimeout:
            await edit_message_text_or_busy(query, SERVICE_BUSY)
            return
        except ServiceUnavailableError as error:
            await edit_message_text_or_busy(
                query,
                _service_unavailable_message(error),
            )
            return
        except Exception as error:
            _log_handler_error("model selection", error)
            await edit_message_text_or_busy(query, MODEL_LIST_UNAVAILABLE)
            return

        if selected is None:
            await edit_message_text_or_busy(
                query,
                "此模型已無法使用，請重新執行 /model。",
            )
            return
        await self._sessions.set_model(chat_id, selected.model_name)
        await edit_message_text_or_busy(
            query,
            f"已選擇模型：{selected.display_name}",
        )

    async def _select_gem(
        self,
        *,
        query: Any,
        user_id: int,
        chat_id: int,
        gem_id: str,
    ) -> None:
        if not gem_id:
            await edit_message_text_or_busy(
                query,
                "無效的 Gem，請重新執行 /gem。",
            )
            return

        try:
            self._ensure_service_accepting_requests()
            async with self._request_queue.request(user_id):
                gem_jar = await self._service.execute(
                    lambda client: client.fetch_gems(include_hidden=False)
                )
        except RateLimitExceeded as error:
            await edit_message_text_or_busy(query, _rate_limit_message(error))
            return
        except QueueAcquireTimeout:
            await edit_message_text_or_busy(query, SERVICE_BUSY)
            return
        except ServiceUnavailableError as error:
            await edit_message_text_or_busy(
                query,
                _service_unavailable_message(error),
            )
            return
        except Exception as error:
            _log_handler_error("gem selection", error)
            await edit_message_text_or_busy(query, GEM_LIST_UNAVAILABLE)
            return

        selected = None if gem_jar is None else gem_jar.get(id=gem_id)
        if selected is None:
            await edit_message_text_or_busy(
                query,
                "此 Gem 已無法使用，請重新執行 /gem。",
            )
            return
        await self._sessions.set_gem(chat_id, selected.id)
        await edit_message_text_or_busy(query, f"已選擇 Gem：{selected.name}")

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

    def _ensure_service_accepting_requests(self) -> None:
        health = self._service.health
        if getattr(health, "accepting_requests", True) is False:
            raise ServiceUnavailableError(
                health.state,
                getattr(health, "degraded_reason", None),
            )

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
            await send_text_or_busy(message, ADMIN_NOT_CONFIGURED)
            return None
        try:
            is_admin = self._auth.is_admin(user_id)
        except ValueError:
            is_admin = False
        if not is_admin:
            await send_text_or_busy(message, ADMIN_ONLY)
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
    application.add_handler(CommandHandler("img", handlers.img))
    application.add_handler(CommandHandler("research", handlers.research))
    application.add_handler(
        CommandHandler("research_status", handlers.research_status)
    )
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


async def register_command_menu(
    application: Application[Any, Any, Any, Any, Any, Any],
) -> None:
    """Publish user commands without making menu availability startup-critical."""

    try:
        await call_telegram(application.bot.set_my_commands, PUBLIC_BOT_COMMANDS)
    except Exception as error:
        LOGGER.warning(
            "Unable to register Telegram command menu (%s)",
            type(error).__name__,
        )


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


def _command_prompt(context: CallbackContext) -> str | None:
    args = getattr(context, "args", None)
    if not isinstance(args, (list, tuple)) or not all(
        isinstance(arg, str) for arg in args
    ):
        return None
    prompt = " ".join(args).strip()
    return prompt or None


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
    await _reply_rendered_chunks(message, render_markdown_chunks(markdown))


async def _reply_rendered_chunks(message: Any, chunks: list[str]) -> None:
    if not chunks:
        await send_text_or_busy(message, "Gemini 未回傳文字。")
        return
    for chunk in chunks:
        await send_text_or_busy(message, chunk, parse_mode=ParseMode.HTML)


def _caption_from_chunks(chunks: list[str]) -> str | None:
    if len(chunks) != 1:
        return None
    caption = chunks[0]
    return caption if caption_is_eligible(caption) else None


async def _delete_placeholder(placeholder: Any | None) -> None:
    if placeholder is None:
        return
    try:
        await delete_message(placeholder)
    except Exception as error:
        _log_handler_error("stream placeholder deletion", error)


def _account_status_label(status: Any) -> str:
    if isinstance(status, AccountStatus):
        return f"{status.name} — {status.description}"
    return "尚未完成初始化"


def _service_unavailable_message(error: ServiceUnavailableError) -> str:
    if error.degraded_reason is DegradedReason.AUTH:
        return "Gemini 服務目前處於認證失效狀態，暫時無法接受請求。"
    if error.degraded_reason is DegradedReason.BLOCKED:
        return "Gemini 服務目前處於暫時受限狀態，將在冷卻後自動重試。"
    return SERVICE_UNAVAILABLE


def _rate_limit_message(error: RateLimitExceeded) -> str:
    seconds = max(1, round(error.retry_after))
    return f"請求過於頻繁，請約 {seconds} 秒後再試。"


async def _reply_rate_limited(message: Any, error: RateLimitExceeded) -> None:
    await send_text_or_busy(message, _rate_limit_message(error))


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
