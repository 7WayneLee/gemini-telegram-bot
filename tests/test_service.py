"""Mock-only tests for GeminiService lifecycle and health transitions."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from gemini_webapi import exceptions as gw_exc

from gemini_tg_bot.config import Settings
from gemini_tg_bot.gemini import service as service_module
from gemini_tg_bot.gemini.errors import ErrorKind
from gemini_tg_bot.gemini.service import (
    AUTH_DEGRADED_NOTIFICATION,
    BLOCKED_ESCALATION_THRESHOLD,
    DegradedReason,
    GeminiService,
    ServiceState,
    ServiceUnavailableError,
    SingletonViolationError,
)


def _exception_instance(exception_type: type[BaseException]) -> BaseException:
    """Build a test exception without assuming an upstream constructor."""

    class SyntheticError(exception_type):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            Exception.__init__(self, "synthetic test error")

    return SyntheticError()


def _mock_client() -> MagicMock:
    client = MagicMock(name="GeminiClientMock")
    client.init = AsyncMock(name="init")
    client.close = AsyncMock(name="close")
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
    assert "2 分鐘" in notifier.await_args.args[0]


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

    with pytest.raises(gw_exc.AuthError):
        await service.init()

    client.close.assert_awaited_once_with()
    assert service.state is ServiceState.DEGRADED
    assert service.health.degraded_reason is DegradedReason.AUTH
    notifier.assert_awaited_once_with(AUTH_DEGRADED_NOTIFICATION)


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
