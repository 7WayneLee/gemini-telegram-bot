"""Mock-only tests for GeminiService lifecycle and health transitions."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from gemini_webapi import exceptions as gw_exc
from gemini_webapi.constants import AccountStatus

from gemini_tg_bot.config import Settings
from gemini_tg_bot.gemini import service as service_module
from gemini_tg_bot.gemini.errors import AccountStatusError, ErrorKind
from gemini_tg_bot.gemini.service import (
    AUTH_DEGRADED_NOTIFICATION,
    BLOCKED_ESCALATION_THRESHOLD,
    DegradedReason,
    GeminiService,
    ServiceState,
    ServiceUnavailableError,
    SingletonViolationError,
    account_status_guidance,
)
from gemini_tg_bot.i18n import (
    LANGUAGE_CHINESE,
    LANGUAGE_ENGLISH,
    translate,
)


def _exception_instance(exception_type: type[BaseException]) -> BaseException:
    """Build a test exception without assuming an upstream constructor."""

    class SyntheticError(exception_type):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            Exception.__init__(self, "synthetic test error")

    return SyntheticError()


def _mock_client(
    account_status: AccountStatus = AccountStatus.AVAILABLE,
) -> MagicMock:
    client = MagicMock(name="GeminiClientMock")
    client.init = AsyncMock(name="init")
    client.close = AsyncMock(name="close")
    client.account_status = account_status
    return client


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="FAKE_TELEGRAM_BOT_TOKEN_FOR_TEST",
        ADMIN_USER_ID=123456789,
        GEMINI_SECURE_1PSID="FAKE_1PSID_FOR_TEST",
        GEMINI_SECURE_1PSIDTS="FAKE_1PSIDTS_FOR_TEST",
        GEMINI_COOKIE_PATH=tmp_path / "cookies",
        GEMINI_PROXY="http://fake-proxy.invalid",
    )


@pytest.fixture(autouse=True)
async def singleton_isolation() -> AsyncIterator[None]:
    assert GeminiService._instance is None
    yield
    instance = GeminiService._instance
    if instance is not None:
        await instance.close()
    assert GeminiService._instance is None


def _patch_client_factory(
    monkeypatch: pytest.MonkeyPatch,
    *clients: MagicMock,
) -> MagicMock:
    factory = MagicMock(name="GeminiClientFactory", side_effect=clients)
    monkeypatch.setattr(service_module, "GeminiClient", factory)
    return factory


@pytest.mark.parametrize("language", [LANGUAGE_ENGLISH, LANGUAGE_CHINESE])
async def test_auth_notification_uses_configured_default_language(
    settings: Settings,
    language: str,
) -> None:
    """Administrator pages lack chat state and need an explicit locale source."""

    localized_settings = settings.model_copy(
        update={"default_language": language}
    )
    notifier = AsyncMock()
    service = GeminiService(localized_settings, notifier)

    await service.handle_error(_exception_instance(gw_exc.AuthError))

    notifier.assert_awaited_once_with(translate("auth.degraded", language))


async def test_init_configures_cookie_path_and_auto_refresh(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _mock_client()
    factory = _patch_client_factory(monkeypatch, client)
    service = GeminiService(settings)

    await service.init()

    assert os.environ["GEMINI_COOKIE_PATH"] == os.fspath(
        settings.gemini_cookie_path
    )
    factory.assert_called_once_with(
        secure_1psid="FAKE_1PSID_FOR_TEST",
        secure_1psidts="FAKE_1PSIDTS_FOR_TEST",
        proxy="http://fake-proxy.invalid",
    )
    client.init.assert_awaited_once_with(auto_refresh=True)
    assert service.state is ServiceState.HEALTHY
    assert service.accepting_requests is True
    assert service.client is client
    assert service.health.account_status is AccountStatus.AVAILABLE


async def test_new_service_does_not_trust_client_default_available_status(
    settings: Settings,
) -> None:
    service = GeminiService(settings)

    assert service.state is ServiceState.NEW
    assert service.accepting_requests is False
    assert service.health.account_status is None


async def test_post_init_unauthenticated_status_enters_auth_degraded(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _mock_client()

    async def initialize(**kwargs: object) -> None:
        assert kwargs == {"auto_refresh": True}
        client.account_status = AccountStatus.UNAUTHENTICATED

    client.init.side_effect = initialize
    _patch_client_factory(monkeypatch, client)
    notifier = AsyncMock()
    service = GeminiService(settings, notifier)

    await service.init()

    assert service.state is ServiceState.DEGRADED
    assert service.health.degraded_reason is DegradedReason.AUTH
    assert service.health.account_status is AccountStatus.UNAUTHENTICATED
    assert service.accepting_requests is False
    notification = notifier.await_args.args[0]
    assert AUTH_DEGRADED_NOTIFICATION in notification
    assert AccountStatus.UNAUTHENTICATED.description in notification
    assert "reason=auth" in caplog.text
    assert "account_status=UNAUTHENTICATED" in caplog.text
    assert "/setcookie" in caplog.text
    assert "FAKE_1PSID_FOR_TEST" not in caplog.text
    assert "FAKE_1PSIDTS_FOR_TEST" not in caplog.text


async def test_post_init_location_rejected_has_distinct_remediation(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _mock_client(AccountStatus.LOCATION_REJECTED)
    _patch_client_factory(monkeypatch, client)
    notifier = AsyncMock()
    service = GeminiService(settings, notifier)

    await service.init()

    assert service.state is ServiceState.DEGRADED
    assert service.health.degraded_reason is DegradedReason.AUTH
    notification = notifier.await_args.args[0]
    assert AccountStatus.LOCATION_REJECTED.description in notification
    assert "SSH SOCKS" in notification
    assert notification != AUTH_DEGRADED_NOTIFICATION


async def test_temporarily_unavailable_status_uses_blocked_half_open_recovery(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    client = _mock_client(AccountStatus.ACCESS_TEMPORARILY_UNAVAILABLE)
    replacement = _mock_client()
    _patch_client_factory(monkeypatch, client, replacement)
    notifier = AsyncMock()
    service = GeminiService(
        settings,
        notifier,
        time_source=lambda: now[0],
        blocked_cooldown_sec=60.0,
    )

    await service.init()

    assert service.state is ServiceState.DEGRADED
    assert service.health.degraded_reason is DegradedReason.BLOCKED
    assert service.health.blocked_until == 60.0
    assert (
        AccountStatus.ACCESS_TEMPORARILY_UNAVAILABLE.description
        in notifier.await_args.args[0]
    )
    with pytest.raises(ServiceUnavailableError):
        await service.execute(AsyncMock(return_value="too early"))

    now[0] = 60.0
    operation = AsyncMock(return_value="recovered")
    assert await service.execute(operation) == "recovered"
    operation.assert_awaited_once_with(replacement)
    client.close.assert_awaited_once_with()
    replacement.init.assert_awaited_once_with(auto_refresh=True)
    assert service.state is ServiceState.HEALTHY
    assert service.health.account_status is AccountStatus.AVAILABLE


@pytest.mark.parametrize(
    ("status", "expected_guidance"),
    [
        (AccountStatus.ACCOUNT_REJECTED, "帳號層級限制"),
        (AccountStatus.ACCOUNT_UNTRUSTED, "帳號層級限制"),
        (AccountStatus.ACCOUNT_REJECTED_BY_GUARDIAN, "帳號層級限制"),
        (AccountStatus.GUARDIAN_APPROVAL_REQUIRED, "帳號層級限制"),
        (AccountStatus.TOS_PENDING, "網頁版接受最新服務條款"),
        (AccountStatus.TOS_OUT_OF_DATE, "網頁版接受最新服務條款"),
    ],
)
def test_account_status_guidance_preserves_description_and_remediation(
    status: AccountStatus,
    expected_guidance: str,
) -> None:
    guidance = account_status_guidance(status, LANGUAGE_CHINESE)

    assert status.description in guidance
    assert expected_guidance in guidance


async def test_service_rejects_a_second_instance(settings: Settings) -> None:
    service = GeminiService(settings)

    with pytest.raises(SingletonViolationError):
        GeminiService(settings)

    assert service.state is ServiceState.NEW


async def test_reinit_closes_old_client_before_constructing_replacement(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _mock_client()
    second = _mock_client()

    calls = 0

    def client_factory(**kwargs: object) -> MagicMock:
        nonlocal calls
        calls += 1
        if calls == 1:
            return first
        assert first.close.await_count == 1
        assert kwargs["secure_1psid"] == "FAKE_REPLACEMENT_1PSID_FOR_TEST"
        assert kwargs["secure_1psidts"] == "FAKE_REPLACEMENT_1PSIDTS_FOR_TEST"
        return second

    factory = MagicMock(side_effect=client_factory)
    monkeypatch.setattr(service_module, "GeminiClient", factory)
    service = GeminiService(settings)
    await service.init()

    await service.reinit(
        secure_1psid="FAKE_REPLACEMENT_1PSID_FOR_TEST",
        secure_1psidts="FAKE_REPLACEMENT_1PSIDTS_FOR_TEST",
    )

    first.close.assert_awaited_once_with()
    second.init.assert_awaited_once_with(auto_refresh=True)
    assert factory.call_count == 2
    assert service.client is second
    assert service.state is ServiceState.HEALTHY


async def test_reinit_rejects_unauthenticated_replacement_and_notifies(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _mock_client()
    replacement = _mock_client(AccountStatus.UNAUTHENTICATED)
    _patch_client_factory(monkeypatch, first, replacement)
    notifier = AsyncMock()
    service = GeminiService(settings, notifier)
    await service.init()

    with pytest.raises(AccountStatusError) as exc_info:
        await service.reinit(
            secure_1psid="FAKE_REPLACEMENT_1PSID_FOR_TEST",
            secure_1psidts="FAKE_REPLACEMENT_1PSIDTS_FOR_TEST",
        )

    assert exc_info.value.status is AccountStatus.UNAUTHENTICATED
    first.close.assert_awaited_once_with()
    replacement.close.assert_not_awaited()
    assert service.state is ServiceState.DEGRADED
    assert service.health.degraded_reason is DegradedReason.AUTH
    assert service.health.account_status is AccountStatus.UNAUTHENTICATED
    assert AUTH_DEGRADED_NOTIFICATION in notifier.await_args.args[0]


async def test_reinit_waits_for_active_work_and_rejects_new_work(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _mock_client()
    second = _mock_client()
    _patch_client_factory(monkeypatch, first, second)
    service = GeminiService(settings)
    await service.init()

    started = asyncio.Event()
    release = asyncio.Event()

    async def long_operation(client: object) -> str:
        assert client is first
        started.set()
        await release.wait()
        return "done"

    active_request = asyncio.create_task(service.execute(long_operation))
    await started.wait()
    reinitializing = asyncio.create_task(service.reinit())
    await asyncio.sleep(0)

    assert service.state is ServiceState.REINITIALIZING
    first.close.assert_not_awaited()
    with pytest.raises(ServiceUnavailableError):
        await service.execute(AsyncMock())

    release.set()
    assert await active_request == "done"
    await reinitializing
    first.close.assert_awaited_once_with()
    assert service.client is second


async def test_auth_error_enters_degraded_notifies_and_rejects_requests(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _mock_client()
    _patch_client_factory(monkeypatch, client)
    notifier = AsyncMock()
    service = GeminiService(settings, notifier)
    await service.init()
    auth_error = _exception_instance(gw_exc.AuthError)

    with pytest.raises(gw_exc.AuthError):
        await service.execute(AsyncMock(side_effect=auth_error))

    assert service.state is ServiceState.DEGRADED
    assert service.health.degraded_reason is DegradedReason.AUTH
    assert service.health.last_error_kind is ErrorKind.AUTH
    assert service.accepting_requests is False
    notifier.assert_awaited_once_with(AUTH_DEGRADED_NOTIFICATION)
    with pytest.raises(ServiceUnavailableError) as rejected:
        await service.execute(AsyncMock())
    assert rejected.value.degraded_reason is DegradedReason.AUTH


async def test_auth_degradation_never_recovers_from_elapsed_time(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    client = _mock_client()
    replacement = _mock_client()
    _patch_client_factory(monkeypatch, client, replacement)
    service = GeminiService(settings, time_source=lambda: now[0])
    await service.init()
    await service.handle_error(_exception_instance(gw_exc.AuthError))

    now[0] = 100_000.0
    with pytest.raises(ServiceUnavailableError):
        await service.execute(AsyncMock(return_value="not admitted"))

    await service.reinit()
    assert service.state is ServiceState.HEALTHY
    assert service.client is replacement


async def test_three_consecutive_temporary_blocks_enter_degraded(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _mock_client()
    _patch_client_factory(monkeypatch, client)
    notifier = AsyncMock()
    service = GeminiService(
        settings,
        notifier,
        time_source=lambda: 10.0,
        blocked_cooldown_sec=120.0,
    )
    await service.init()

    for failure_number in range(1, BLOCKED_ESCALATION_THRESHOLD + 1):
        error = _exception_instance(gw_exc.TemporarilyBlockedError)
        with pytest.raises(gw_exc.TemporarilyBlockedError):
            await service.execute(AsyncMock(side_effect=error))
        if failure_number < BLOCKED_ESCALATION_THRESHOLD:
            assert service.state is ServiceState.HEALTHY

    assert service.state is ServiceState.DEGRADED
    assert service.health.degraded_reason is DegradedReason.BLOCKED
    assert service.health.blocked_until == 130.0
    assert service.accepting_requests is False
    notifier.assert_awaited_once()
    assert "2 minutes" in notifier.await_args.args[0]


async def test_success_resets_consecutive_temporary_block_failures(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _mock_client()
    _patch_client_factory(monkeypatch, client)
    service = GeminiService(settings)
    await service.init()

    for _ in range(BLOCKED_ESCALATION_THRESHOLD - 1):
        with pytest.raises(gw_exc.TemporarilyBlockedError):
            await service.execute(
                AsyncMock(
                    side_effect=_exception_instance(
                        gw_exc.TemporarilyBlockedError
                    )
                )
            )
    assert service.health.consecutive_blocked_errors == 2

    assert await service.execute(AsyncMock(return_value="ok")) == "ok"
    assert service.health.consecutive_blocked_errors == 0

    for _ in range(BLOCKED_ESCALATION_THRESHOLD - 1):
        with pytest.raises(gw_exc.TemporarilyBlockedError):
            await service.execute(
                AsyncMock(
                    side_effect=_exception_instance(
                        gw_exc.TemporarilyBlockedError
                    )
                )
            )
    assert service.state is ServiceState.HEALTHY
    assert service.health.consecutive_blocked_errors == 2


async def test_expired_block_allows_one_half_open_probe_and_success_recovers(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    client = _mock_client()
    _patch_client_factory(monkeypatch, client)
    service = GeminiService(
        settings,
        time_source=lambda: now[0],
        blocked_cooldown_sec=60.0,
    )
    await service.init()
    for _ in range(BLOCKED_ESCALATION_THRESHOLD):
        with pytest.raises(gw_exc.TemporarilyBlockedError):
            await service.execute(
                AsyncMock(
                    side_effect=_exception_instance(
                        gw_exc.TemporarilyBlockedError
                    )
                )
            )

    with pytest.raises(ServiceUnavailableError):
        await service.execute(AsyncMock(return_value="too early"))

    now[0] = 60.0
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def probe(client_arg: object) -> str:
        assert client_arg is client
        probe_started.set()
        await release_probe.wait()
        return "recovered"

    probing = asyncio.create_task(service.execute(probe))
    await probe_started.wait()
    assert service.state is ServiceState.HALF_OPEN
    with pytest.raises(ServiceUnavailableError):
        await service.execute(AsyncMock(return_value="second probe"))

    release_probe.set()
    assert await probing == "recovered"
    assert service.state is ServiceState.HEALTHY
    assert service.health.degraded_reason is None
    assert service.health.consecutive_blocked_errors == 0


async def test_failed_half_open_probe_restarts_blocked_cooldown(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [5.0]
    client = _mock_client()
    _patch_client_factory(monkeypatch, client)
    service = GeminiService(
        settings,
        time_source=lambda: now[0],
        blocked_cooldown_sec=30.0,
    )
    await service.init()
    for _ in range(BLOCKED_ESCALATION_THRESHOLD):
        with pytest.raises(gw_exc.TemporarilyBlockedError):
            await service.execute(
                AsyncMock(
                    side_effect=_exception_instance(
                        gw_exc.TemporarilyBlockedError
                    )
                )
            )

    now[0] = 35.0
    with pytest.raises(TimeoutError):
        await service.execute(AsyncMock(side_effect=TimeoutError("probe failed")))

    assert service.state is ServiceState.DEGRADED
    assert service.health.degraded_reason is DegradedReason.BLOCKED
    assert service.health.blocked_until == 65.0


async def test_auth_failure_during_init_is_degraded_and_mock_client_is_closed(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _mock_client()
    client.init.side_effect = _exception_instance(gw_exc.AuthError)
    _patch_client_factory(monkeypatch, client)
    notifier = AsyncMock()
    service = GeminiService(settings, notifier)

    await service.init()

    client.close.assert_awaited_once_with()
    assert service.state is ServiceState.DEGRADED
    assert service.health.degraded_reason is DegradedReason.AUTH
    notifier.assert_awaited_once_with(AUTH_DEGRADED_NOTIFICATION)


async def test_non_auth_failure_during_init_remains_fatal(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _mock_client()
    client.init.side_effect = RuntimeError("synthetic startup failure")
    _patch_client_factory(monkeypatch, client)
    service = GeminiService(settings)

    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        await service.init()

    client.close.assert_awaited_once_with()
    assert service.state is ServiceState.FAILED


async def test_health_and_errors_never_render_credentials(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _mock_client()
    _patch_client_factory(monkeypatch, client)
    service = GeminiService(settings)
    await service.init()
    await service.handle_error(_exception_instance(gw_exc.AuthError))

    rendered = f"{service.health!r}"
    assert "FAKE_1PSID_FOR_TEST" not in rendered
    assert "FAKE_1PSIDTS_FOR_TEST" not in rendered


async def test_repeated_unauthenticated_reinit_notifies_once(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the transition into AUTH degradation pages the administrator.

    A restart loop or a retrying caller must not turn one auth failure into a
    stream of identical pushes: Telegram rate-limits a chat, and the flood
    would bury the ``/setcookie`` prompt that is the only way to recover.
    """

    clients = [_mock_client(AccountStatus.UNAUTHENTICATED) for _ in range(5)]
    _patch_client_factory(monkeypatch, *clients)
    notifier = AsyncMock()
    service = GeminiService(settings, notifier)

    await service.init()
    assert service.health.degraded_reason is DegradedReason.AUTH
    assert notifier.await_count == 1

    for _ in range(4):
        with pytest.raises(AccountStatusError):
            await service.reinit(
                secure_1psid="FAKE_REPLACEMENT_1PSID_FOR_TEST",
                secure_1psidts="FAKE_REPLACEMENT_1PSIDTS_FOR_TEST",
            )

    assert notifier.await_count == 1
    assert service.health.degraded_reason is DegradedReason.AUTH


async def test_degraded_reason_change_still_notifies(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deduplication keys on the reason, so BLOCKED -> AUTH still pages."""

    blocked = _mock_client(AccountStatus.ACCESS_TEMPORARILY_UNAVAILABLE)
    unauthenticated = _mock_client(AccountStatus.UNAUTHENTICATED)
    _patch_client_factory(monkeypatch, blocked, unauthenticated)
    notifier = AsyncMock()
    service = GeminiService(settings, notifier)

    await service.init()
    assert service.health.degraded_reason is DegradedReason.BLOCKED
    assert notifier.await_count == 1

    with pytest.raises(AccountStatusError):
        await service.reinit(
            secure_1psid="FAKE_REPLACEMENT_1PSID_FOR_TEST",
            secure_1psidts="FAKE_REPLACEMENT_1PSIDTS_FOR_TEST",
        )

    assert service.health.degraded_reason is DegradedReason.AUTH
    assert notifier.await_count == 2


async def test_unauthenticated_startup_starts_polling_and_pages_once(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real service that cannot authenticate must not stop the process.

    ``/setcookie`` is the only recovery path, and it needs a live process to
    reach.  The companion test in ``test_handlers`` drives this with a mocked
    service, so it cannot catch a regression inside the service itself.
    """

    from gemini_tg_bot.__main__ import _initialize_and_start_polling

    _patch_client_factory(
        monkeypatch,
        _mock_client(AccountStatus.UNAUTHENTICATED),
    )
    notifier = AsyncMock()
    service = GeminiService(settings, notifier)
    sessions = SimpleNamespace(restore_all=AsyncMock())
    research = SimpleNamespace(restore_running=AsyncMock())
    updater = SimpleNamespace(start_polling=AsyncMock())

    await _initialize_and_start_polling(service, sessions, research, updater)

    assert service.state is ServiceState.DEGRADED
    assert service.health.degraded_reason is DegradedReason.AUTH
    updater.start_polling.assert_awaited_once()
    assert notifier.await_count == 1
