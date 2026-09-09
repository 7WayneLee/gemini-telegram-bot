"""Application-scoped lifecycle manager for the Gemini client."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, TypeVar, cast

from gemini_webapi import GeminiClient
from gemini_webapi import exceptions as gw_exc
from gemini_webapi.constants import AccountStatus
from gemini_webapi.utils import clear_cookies_cache
from pydantic import SecretStr

from gemini_tg_bot.config import Settings

from .errors import AccountStatusError, ErrorKind, classify_error


BLOCKED_ESCALATION_THRESHOLD = 3
BLOCKED_COOLDOWN_SEC = 900.0
AUTH_DEGRADED_NOTIFICATION = "認證失效，請 /setcookie"

LOGGER = logging.getLogger(__name__)

_ACCOUNT_RESTRICTION_STATUSES = frozenset(
    {
        AccountStatus.ACCOUNT_REJECTED,
        AccountStatus.ACCOUNT_REJECTED_BY_GUARDIAN,
        AccountStatus.ACCOUNT_UNTRUSTED,
        AccountStatus.GUARDIAN_APPROVAL_REQUIRED,
    }
)
_TOS_STATUSES = frozenset(
    {
        AccountStatus.TOS_PENDING,
        AccountStatus.TOS_OUT_OF_DATE,
    }
)


def account_status_guidance(status: AccountStatus) -> str:
    """Return secret-free remediation guidance for an unavailable account."""

    details = f"{status.name}: {status.description}"
    if status is AccountStatus.UNAUTHENTICATED:
        return f"{AUTH_DEGRADED_NOTIFICATION}\n{details}"
    if status is AccountStatus.LOCATION_REJECTED:
        return (
            f"{details}\n可能是 Cookie 取得 IP 與服務使用 IP 不符；"
            "請依 README 的 SSH SOCKS 流程重新取得 Cookie。"
        )
    if status in _ACCOUNT_RESTRICTION_STATUSES:
        return (
            f"{details}\n這可能是帳號層級限制，換 Cookie 無法解除此限制；"
            "請檢查 Google 帳號狀態。"
        )
    if status in _TOS_STATUSES:
        return f"{details}\n請至 Gemini 網頁版接受最新服務條款後再試。"
    if status is AccountStatus.ACCESS_TEMPORARILY_UNAVAILABLE:
        return f"{details}\nGemini 暫時受限，服務會在冷卻後自動重試。"
    return details


class ServiceState(StrEnum):
    """Lifecycle and circuit-breaker states for :class:`GeminiService`."""

    NEW = "new"
    INITIALIZING = "initializing"
    HEALTHY = "healthy"
    REINITIALIZING = "reinitializing"
    DEGRADED = "degraded"
    HALF_OPEN = "half_open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class DegradedReason(StrEnum):
    """Reasons that require different recovery behavior while degraded."""

    AUTH = "auth"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ServiceHealth:
    """A secret-free snapshot suitable for the administrative health command."""

    state: ServiceState
    accepting_requests: bool
    degraded_reason: DegradedReason | None
    blocked_until: float | None
    consecutive_blocked_errors: int
    active_requests: int
    last_error_kind: ErrorKind | None
    last_error_type: str | None
    account_status: AccountStatus | None


class ServiceUnavailableError(RuntimeError):
    """Raised before an operation when the service cannot accept requests."""

    def __init__(
        self,
        state: ServiceState,
        degraded_reason: DegradedReason | None,
    ) -> None:
        self.state = state
        self.degraded_reason = degraded_reason
        super().__init__(f"Gemini service is not accepting requests ({state.value})")


class SingletonViolationError(RuntimeError):
    """Raised when code attempts to create a second service instance."""


ResultT = TypeVar("ResultT")
AdminNotifier = Callable[[str], Awaitable[None]]
TimeSource = Callable[[], float]
ClientOperation = Callable[[GeminiClient], ResultT | Awaitable[ResultT]]


class GeminiService:
    """Own the process's sole live ``GeminiClient`` and its health state.

    ``execute`` is the request boundary: it rejects work while unavailable,
    observes upstream failures, and drives the circuit breaker.  ``reinit``
    closes the old client before using the single construction point to make
    its replacement, so two live clients never overlap.
    """

    _instance: ClassVar[GeminiService | None] = None

    def __new__(cls, *args: Any, **kwargs: Any) -> GeminiService:
        del args, kwargs
        if cls._instance is not None:
            raise SingletonViolationError("GeminiService is already configured")
        instance = super().__new__(cls)
        cls._instance = instance
        return instance

    def __init__(
        self,
        settings: Settings,
        admin_notifier: AdminNotifier | None = None,
        *,
        time_source: TimeSource = time.monotonic,
        blocked_cooldown_sec: float = BLOCKED_COOLDOWN_SEC,
    ) -> None:
        if blocked_cooldown_sec <= 0:
            type(self)._instance = None
            raise ValueError("blocked_cooldown_sec must be positive")

        self._settings = settings
        self._admin_notifier = admin_notifier
        self._time_source = time_source
        self._blocked_cooldown_sec = blocked_cooldown_sec
        self._secure_1psid = settings.gemini_secure_1psid
        self._secure_1psidts = settings.gemini_secure_1psidts

        self._client: GeminiClient | None = None
        self._client_initialized = False
        self._account_status: AccountStatus | None = None
        self._state = ServiceState.NEW
        self._degraded_reason: DegradedReason | None = None
        # The degraded reason the administrator has already been paged about.
        # Cleared on recovery so the next distinct failure pages again.
        self._notified_degraded_reason: DegradedReason | None = None
        self._blocked_until: float | None = None
        self._consecutive_blocked_errors = 0
        self._active_requests = 0
        self._last_error_kind: ErrorKind | None = None
        self._last_error_type: str | None = None
        self._lifecycle = asyncio.Condition()

        # gemini_webapi reads this lazily when persisting refreshed cookies.
        os.environ["GEMINI_COOKIE_PATH"] = os.fspath(settings.gemini_cookie_path)

    @property
    def state(self) -> ServiceState:
        """Return the current state without exposing client credentials."""

        return self._state

    @property
    def accepting_requests(self) -> bool:
        """Whether a new request would be admitted at this instant."""

        if self._state is ServiceState.HEALTHY:
            return True
        return (
            self._state is ServiceState.DEGRADED
            and self._degraded_reason is DegradedReason.BLOCKED
            and self._blocked_until is not None
            and self._client is not None
            and self._time_source() >= self._blocked_until
        )

    @property
    def client(self) -> GeminiClient:
        """Return the ready client for synchronous upstream operations.

        Async upstream work should use :meth:`execute` so failures and active
        operations remain visible to the lifecycle manager.
        """

        if self._state is not ServiceState.HEALTHY or self._client is None:
            raise ServiceUnavailableError(self._state, self._degraded_reason)
        return self._client

    @property
    def health(self) -> ServiceHealth:
        """Return a secret-free point-in-time health snapshot."""

        account_status = self._account_status
        if self._client_initialized and self._client is not None:
            account_status = self._client_account_status(self._client)
        return ServiceHealth(
            state=self._state,
            accepting_requests=self.accepting_requests,
            degraded_reason=self._degraded_reason,
            blocked_until=self._blocked_until,
            consecutive_blocked_errors=self._consecutive_blocked_errors,
            active_requests=self._active_requests,
            last_error_kind=self._last_error_kind,
            last_error_type=self._last_error_type,
            account_status=account_status,
        )

    async def init(self) -> None:
        """Construct and initialize the sole Gemini client."""

        failure: BaseException | None = None
        notification: str | None = None
        async with self._lifecycle:
            if self._state is ServiceState.HEALTHY:
                return
            if self._state is not ServiceState.NEW:
                raise RuntimeError(
                    f"init is not allowed while service is {self._state.value}; "
                    "use reinit instead"
                )
            self._state = ServiceState.INITIALIZING
            try:
                await self._start_new_client_locked()
            except BaseException as error:
                failure = error
                notification = self._record_lifecycle_failure_locked(error)
            else:
                self._set_healthy_locked()
            self._lifecycle.notify_all()

        if failure is not None:
            if notification is not None:
                await self._notify_admin(notification)
            if self._state is ServiceState.DEGRADED:
                degraded_reason = (
                    self._degraded_reason.value
                    if self._degraded_reason is not None
                    else "unknown"
                )
                account_status = (
                    self._account_status.name
                    if self._account_status is not None
                    else "unknown"
                )
                LOGGER.warning(
                    "Gemini startup is DEGRADED reason=%s account_status=%s; "
                    "Telegram polling will continue; use /setcookie to "
                    "restore service",
                    degraded_reason,
                    account_status,
                )
                return
            raise failure.with_traceback(failure.__traceback__)

    async def reinit(
        self,
        *,
        secure_1psid: str | SecretStr | None = None,
        secure_1psidts: str | SecretStr | None = None,
    ) -> None:
        """Hot-restart the client, optionally replacing both credentials.

        The existing client's session and refresh task are closed before its
        replacement is constructed.  Supplying only one credential is rejected
        so a hot update cannot accidentally combine two credential versions.
        """

        new_credentials = self._validated_credentials(
            secure_1psid,
            secure_1psidts,
        )

        failure: BaseException | None = None
        notification: str | None = None
        async with self._lifecycle:
            if self._state in {ServiceState.CLOSED, ServiceState.CLOSING}:
                raise RuntimeError("a closed GeminiService cannot be reinitialized")

            self._state = ServiceState.REINITIALIZING
            while self._active_requests:
                await self._lifecycle.wait()

            try:
                if self._client is not None:
                    if new_credentials is not None:
                        try:
                            clear_cookies_cache(self._client.cookies)
                        except Exception:
                            # Cache removal is best effort.  Never include the
                            # cache path or exception text because both may
                            # contain credential material.
                            LOGGER.warning(
                                "Failed to clear cached cookies during reinit"
                            )
                    await self._client.close()
                    self._client = None
                    self._client_initialized = False
                    self._account_status = None

                if new_credentials is not None:
                    self._secure_1psid, self._secure_1psidts = new_credentials

                await self._start_new_client_locked()
            except BaseException as error:
                failure = error
                notification = self._record_lifecycle_failure_locked(error)
            else:
                self._set_healthy_locked()
            self._lifecycle.notify_all()

        if failure is not None:
            if notification is not None:
                await self._notify_admin(notification)
            raise failure.with_traceback(failure.__traceback__)

    async def close(self) -> None:
        """Stop accepting work and close the owned client."""

        notification: str | None = None
        try:
            async with self._lifecycle:
                if self._state is ServiceState.CLOSED:
                    return

                self._state = ServiceState.CLOSING
                while self._active_requests:
                    await self._lifecycle.wait()

                if self._client is not None:
                    await self._client.close()
                    self._client = None
                    self._client_initialized = False
                    self._account_status = None

                self._state = ServiceState.CLOSED
                self._degraded_reason = None
                self._blocked_until = None
                self._lifecycle.notify_all()
                if type(self)._instance is self:
                    type(self)._instance = None
        except BaseException as error:
            notification = await self._record_lifecycle_failure(error)
            if notification is not None:
                await self._notify_admin(notification)
            raise

    async def execute(self, operation: ClientOperation[ResultT]) -> ResultT:
        """Run one client operation and update health from its outcome."""

        await self._refresh_temporarily_unavailable_client()
        client, is_probe = await self._acquire_request()
        try:
            pending_result = operation(client)
            if inspect.isawaitable(pending_result):
                result = await cast(Awaitable[ResultT], pending_result)
            else:
                result = cast(ResultT, pending_result)
        except asyncio.CancelledError:
            await self._release_cancelled_request(is_probe)
            raise
        except Exception as error:
            notification = await self._finish_failed_request(error, is_probe)
            if notification is not None:
                await self._notify_admin(notification)
            raise
        else:
            await self._finish_successful_request(is_probe)
            return result

    async def handle_error(self, error: BaseException) -> ErrorKind:
        """Record an error from a guarded synchronous client operation.

        Prefer :meth:`execute` for async operations.  This method exists for
        synchronous upstream methods accessed through :attr:`client`.
        """

        async with self._lifecycle:
            kind = classify_error(error)
            self._last_error_kind = kind
            self._last_error_type = type(error).__name__
            notification: str | None = None
            if kind is ErrorKind.AUTH:
                if self._set_auth_degraded_locked(error):
                    notification = AUTH_DEGRADED_NOTIFICATION
            elif (
                isinstance(error, gw_exc.TemporarilyBlockedError)
                and self._state is ServiceState.HEALTHY
            ):
                self._consecutive_blocked_errors += 1
                if (
                    self._consecutive_blocked_errors
                    >= BLOCKED_ESCALATION_THRESHOLD
                ):
                    self._set_blocked_degraded_locked()
                    notification = self._blocked_notification()
            self._lifecycle.notify_all()
        if notification is not None:
            await self._notify_admin(notification)
        return kind

    def _create_client(self) -> GeminiClient:
        """The repository's only GeminiClient construction point."""

        return GeminiClient(
            secure_1psid=self._secure_1psid.get_secret_value(),
            secure_1psidts=self._secure_1psidts.get_secret_value(),
            proxy=self._settings.gemini_proxy,
        )

    async def _start_new_client_locked(self) -> None:
        client = self._create_client()
        self._client = client
        self._client_initialized = False
        self._account_status = None
        try:
            await client.init(auto_refresh=True)
        except BaseException:
            try:
                await client.close()
            except BaseException:
                # Retain ownership when close fails; reinit must close this
                # client successfully before constructing another one.
                pass
            else:
                if self._client is client:
                    self._client = None
            raise

        self._client_initialized = True
        self._account_status = self._client_account_status(client)
        if self._account_status is not AccountStatus.AVAILABLE:
            raise AccountStatusError(self._account_status)

    async def _record_lifecycle_failure(
        self,
        error: BaseException,
    ) -> str | None:
        async with self._lifecycle:
            notification = self._record_lifecycle_failure_locked(error)
            self._lifecycle.notify_all()
            return notification

    def _record_lifecycle_failure_locked(
        self,
        error: BaseException,
    ) -> str | None:
        kind = classify_error(error)
        self._last_error_kind = kind
        self._last_error_type = type(error).__name__
        self._consecutive_blocked_errors = 0
        self._blocked_until = None
        if isinstance(error, AccountStatusError):
            # Only the transition into a degraded reason is worth paging for.
            # Repeated lifecycle failures with the same cause must stay silent,
            # or a restart loop turns one problem into an admin-chat flood that
            # trips Telegram's per-chat rate limit and buries /setcookie.
            if error.status is AccountStatus.ACCESS_TEMPORARILY_UNAVAILABLE:
                self._set_blocked_degraded_locked()
            else:
                self._set_auth_degraded_locked(error)
            if not self._claim_degraded_page_locked():
                return None
            return account_status_guidance(error.status)
        if kind is ErrorKind.AUTH:
            self._set_auth_degraded_locked(error)
            if not self._claim_degraded_page_locked():
                return None
            return AUTH_DEGRADED_NOTIFICATION
        self._state = ServiceState.FAILED
        self._degraded_reason = None
        return None

    async def _acquire_request(self) -> tuple[GeminiClient, bool]:
        async with self._lifecycle:
            is_probe = False
            if (
                self._state is ServiceState.DEGRADED
                and self._degraded_reason is DegradedReason.BLOCKED
                and self._blocked_until is not None
                and self._time_source() >= self._blocked_until
            ):
                self._state = ServiceState.HALF_OPEN
                is_probe = True
            elif self._state is not ServiceState.HEALTHY:
                raise ServiceUnavailableError(self._state, self._degraded_reason)

            if self._client is None:
                raise ServiceUnavailableError(self._state, self._degraded_reason)
            self._active_requests += 1
            return self._client, is_probe

    async def _refresh_temporarily_unavailable_client(self) -> None:
        should_reinit = False
        async with self._lifecycle:
            if not (
                self._state is ServiceState.DEGRADED
                and self._degraded_reason is DegradedReason.BLOCKED
                and self._blocked_until is not None
                and self._time_source() >= self._blocked_until
                and self._account_status
                is AccountStatus.ACCESS_TEMPORARILY_UNAVAILABLE
            ):
                return

            if (
                self._client_initialized
                and self._client is not None
                and self._client_account_status(self._client)
                is AccountStatus.AVAILABLE
            ):
                self._account_status = AccountStatus.AVAILABLE
                return

            self._state = ServiceState.HALF_OPEN
            self._lifecycle.notify_all()
            should_reinit = True

        if should_reinit:
            try:
                # Re-check the temporary account status with the same
                # credentials.  Credential-less reinit intentionally retains
                # D6 cache behavior and does not clear the cookies cache.
                await self.reinit()
            except AccountStatusError as error:
                raise ServiceUnavailableError(
                    self._state,
                    self._degraded_reason,
                ) from error

    async def _finish_successful_request(self, is_probe: bool) -> None:
        async with self._lifecycle:
            self._active_requests -= 1
            self._consecutive_blocked_errors = 0
            self._last_error_kind = None
            self._last_error_type = None
            if is_probe and self._state is ServiceState.HALF_OPEN:
                if self._client_initialized and self._client is not None:
                    self._account_status = self._client_account_status(
                        self._client
                    )
                if self._account_status is AccountStatus.AVAILABLE:
                    self._set_healthy_locked()
                else:
                    self._set_blocked_degraded_locked()
            self._lifecycle.notify_all()

    async def _finish_failed_request(
        self,
        error: Exception,
        is_probe: bool,
    ) -> str | None:
        async with self._lifecycle:
            self._active_requests -= 1
            kind = classify_error(error)
            self._last_error_kind = kind
            self._last_error_type = type(error).__name__
            notification: str | None = None

            if kind is ErrorKind.AUTH:
                if self._set_auth_degraded_locked(error):
                    notification = AUTH_DEGRADED_NOTIFICATION
            elif is_probe:
                self._set_blocked_degraded_locked()
                notification = self._blocked_notification()
            elif (
                isinstance(error, gw_exc.TemporarilyBlockedError)
                and self._state is ServiceState.HEALTHY
            ):
                self._consecutive_blocked_errors += 1
                if (
                    self._consecutive_blocked_errors
                    >= BLOCKED_ESCALATION_THRESHOLD
                ):
                    self._set_blocked_degraded_locked()
                    notification = self._blocked_notification()

            self._lifecycle.notify_all()
            return notification

    async def _release_cancelled_request(self, is_probe: bool) -> None:
        async with self._lifecycle:
            self._active_requests -= 1
            if is_probe and self._state is ServiceState.HALF_OPEN:
                self._set_blocked_degraded_locked()
            self._lifecycle.notify_all()

    def _set_healthy_locked(self) -> None:
        if (
            not self._client_initialized
            or self._account_status is not AccountStatus.AVAILABLE
        ):
            raise RuntimeError(
                "Gemini client must be initialized and available before becoming healthy"
            )
        self._state = ServiceState.HEALTHY
        self._degraded_reason = None
        self._notified_degraded_reason = None
        self._blocked_until = None
        self._consecutive_blocked_errors = 0
        self._last_error_kind = None
        self._last_error_type = None
        self._lifecycle.notify_all()

    def _set_auth_degraded_locked(self, error: BaseException) -> bool:
        was_auth_degraded = (
            self._state is ServiceState.DEGRADED
            and self._degraded_reason is DegradedReason.AUTH
        )
        self._state = ServiceState.DEGRADED
        self._degraded_reason = DegradedReason.AUTH
        self._blocked_until = None
        self._consecutive_blocked_errors = 0
        self._last_error_kind = ErrorKind.AUTH
        self._last_error_type = type(error).__name__
        return not was_auth_degraded

    def _claim_degraded_page_locked(self) -> bool:
        """Return whether the current degraded reason still needs paging.

        ``reinit`` clears ``_state`` before it retries, so a state transition
        is not a reliable signal: a caller that retries -- or a supervisor that
        restarts the process -- would page the administrator on every attempt.
        Tracking the reason already paged for keeps one cause to one message
        until the service recovers, which matters because Telegram rate-limits
        a chat and a flood buries the ``/setcookie`` prompt needed to recover.
        """

        if self._degraded_reason is None:
            return False
        if self._notified_degraded_reason is self._degraded_reason:
            return False
        self._notified_degraded_reason = self._degraded_reason
        return True

    def _set_blocked_degraded_locked(self) -> None:
        self._state = ServiceState.DEGRADED
        self._degraded_reason = DegradedReason.BLOCKED
        self._blocked_until = self._time_source() + self._blocked_cooldown_sec
        self._consecutive_blocked_errors = BLOCKED_ESCALATION_THRESHOLD

    def _blocked_notification(self) -> str:
        minutes = max(1, math.ceil(self._blocked_cooldown_sec / 60))
        return (
            f"Gemini 暫時封鎖，將於約 {minutes} 分鐘後自動重試，"
            "無需人工介入"
        )

    @staticmethod
    def _client_account_status(client: GeminiClient) -> AccountStatus:
        status = client.account_status
        if isinstance(status, AccountStatus):
            return status
        # The pinned upstream contract always exposes AccountStatus.  This
        # fallback keeps older lightweight lifecycle test doubles compatible.
        return AccountStatus.AVAILABLE

    async def _notify_admin(self, message: str) -> None:
        if self._admin_notifier is None:
            return
        try:
            await self._admin_notifier(message)
        except Exception:
            # Notification is best effort.  Never log callback exceptions here:
            # their text is outside this layer's control and could contain a
            # credential supplied by an administrator.
            return

    @staticmethod
    def _validated_credentials(
        secure_1psid: str | SecretStr | None,
        secure_1psidts: str | SecretStr | None,
    ) -> tuple[SecretStr, SecretStr] | None:
        if secure_1psid is None and secure_1psidts is None:
            return None
        if secure_1psid is None or secure_1psidts is None:
            raise ValueError("both Gemini credentials must be supplied together")

        first = (
            secure_1psid
            if isinstance(secure_1psid, SecretStr)
            else SecretStr(secure_1psid)
        )
        second = (
            secure_1psidts
            if isinstance(secure_1psidts, SecretStr)
            else SecretStr(secure_1psidts)
        )
        if not first.get_secret_value() or not second.get_secret_value():
            raise ValueError("Gemini credentials must not be empty")
        return first, second
