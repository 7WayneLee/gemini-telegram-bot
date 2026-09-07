"""Application-scoped lifecycle manager for the Gemini client."""

from __future__ import annotations

import asyncio
import inspect
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, TypeVar, cast

from gemini_webapi import GeminiClient
from gemini_webapi import exceptions as gw_exc
from pydantic import SecretStr

from gemini_tg_bot.config import Settings

from .errors import ErrorKind, classify_error


BLOCKED_ESCALATION_THRESHOLD = 3
BLOCKED_COOLDOWN_SEC = 900.0
AUTH_DEGRADED_NOTIFICATION = "認證失效，請 /setcookie"


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
        self._state = ServiceState.NEW
        self._degraded_reason: DegradedReason | None = None
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

        return ServiceHealth(
            state=self._state,
            accepting_requests=self.accepting_requests,
            degraded_reason=self._degraded_reason,
            blocked_until=self._blocked_until,
            consecutive_blocked_errors=self._consecutive_blocked_errors,
            active_requests=self._active_requests,
            last_error_kind=self._last_error_kind,
            last_error_type=self._last_error_type,
        )

    async def init(self) -> None:
        """Construct and initialize the sole Gemini client."""

        failure: BaseException | None = None
        should_notify = False
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
                should_notify = self._record_lifecycle_failure_locked(error)
            else:
                self._set_healthy_locked()
            self._lifecycle.notify_all()

        if failure is not None:
            if should_notify:
                await self._notify_admin(AUTH_DEGRADED_NOTIFICATION)
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
        should_notify = False
        async with self._lifecycle:
            if self._state in {ServiceState.CLOSED, ServiceState.CLOSING}:
                raise RuntimeError("a closed GeminiService cannot be reinitialized")

            self._state = ServiceState.REINITIALIZING
            while self._active_requests:
                await self._lifecycle.wait()

            try:
                if self._client is not None:
                    await self._client.close()
                    self._client = None

                if new_credentials is not None:
                    self._secure_1psid, self._secure_1psidts = new_credentials

                await self._start_new_client_locked()
            except BaseException as error:
                failure = error
                should_notify = self._record_lifecycle_failure_locked(error)
            else:
                self._set_healthy_locked()
            self._lifecycle.notify_all()

        if failure is not None:
            if should_notify:
                await self._notify_admin(AUTH_DEGRADED_NOTIFICATION)
            raise failure.with_traceback(failure.__traceback__)

    async def close(self) -> None:
        """Stop accepting work and close the owned client."""

        should_notify = False
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

                self._state = ServiceState.CLOSED
                self._degraded_reason = None
                self._blocked_until = None
                self._lifecycle.notify_all()
                if type(self)._instance is self:
                    type(self)._instance = None
        except BaseException as error:
            should_notify = await self._record_lifecycle_failure(error)
            if should_notify:
                await self._notify_admin(AUTH_DEGRADED_NOTIFICATION)
            raise

    async def execute(self, operation: ClientOperation[ResultT]) -> ResultT:
        """Run one client operation and update health from its outcome."""

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

    async def _record_lifecycle_failure(self, error: BaseException) -> bool:
        async with self._lifecycle:
            should_notify = self._record_lifecycle_failure_locked(error)
            self._lifecycle.notify_all()
            return should_notify

    def _record_lifecycle_failure_locked(self, error: BaseException) -> bool:
        kind = classify_error(error)
        self._last_error_kind = kind
        self._last_error_type = type(error).__name__
        self._consecutive_blocked_errors = 0
        self._blocked_until = None
        if kind is ErrorKind.AUTH:
            return self._set_auth_degraded_locked(error)
        self._state = ServiceState.FAILED
        self._degraded_reason = None
        return False

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

    async def _finish_successful_request(self, is_probe: bool) -> None:
        async with self._lifecycle:
            self._active_requests -= 1
            self._consecutive_blocked_errors = 0
            self._last_error_kind = None
            self._last_error_type = None
            if is_probe and self._state is ServiceState.HALF_OPEN:
                self._set_healthy_locked()
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
        self._state = ServiceState.HEALTHY
        self._degraded_reason = None
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
