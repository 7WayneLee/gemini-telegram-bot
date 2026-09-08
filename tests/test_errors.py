"""Tests for Gemini error classification."""

import asyncio
from collections.abc import Iterator

import pytest
from curl_cffi.curl import CurlError
from curl_cffi.requests import exceptions as cc_exc
from gemini_webapi import exceptions as gw_exc
from gemini_webapi.constants import AccountStatus

from gemini_tg_bot.gemini.errors import (
    AccountStatusError,
    ErrorKind,
    classify_error,
)


def _exception_instance(exception_type: type[BaseException]) -> BaseException:
    """Create an instance without relying on an upstream constructor signature."""

    class SyntheticError(exception_type):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            Exception.__init__(self, "synthetic test error")

    return SyntheticError()


@pytest.fixture(params=[gw_exc.AuthError])
def auth_error(request: pytest.FixtureRequest) -> Iterator[BaseException]:
    yield _exception_instance(request.param)


@pytest.fixture(
    params=[gw_exc.UsageLimitExceededError, gw_exc.TemporarilyBlockedError]
)
def rate_limit_error(request: pytest.FixtureRequest) -> Iterator[BaseException]:
    yield _exception_instance(request.param)


@pytest.fixture(
    params=[
        gw_exc.TimeoutError,
        cc_exc.Timeout,
        cc_exc.ConnectionError,
        cc_exc.ChunkedEncodingError,
        cc_exc.IncompleteRead,
        cc_exc.ProxyError,
        CurlError,
        asyncio.TimeoutError,
        OSError,
    ]
)
def transient_error(request: pytest.FixtureRequest) -> Iterator[BaseException]:
    yield _exception_instance(request.param)


@pytest.fixture(
    params=[
        gw_exc.ModelInvalidError,
        gw_exc.ImageGenerationError,
        gw_exc.APIError,
        gw_exc.GeminiError,
        cc_exc.InvalidURL,
        cc_exc.MissingSchema,
        cc_exc.InvalidSchema,
        cc_exc.URLRequired,
        cc_exc.InvalidHeader,
        cc_exc.ImpersonateError,
        ValueError,
    ]
)
def fatal_error(request: pytest.FixtureRequest) -> Iterator[BaseException]:
    yield _exception_instance(request.param)


def test_classifies_auth_errors(auth_error: BaseException) -> None:
    assert classify_error(auth_error) is ErrorKind.AUTH


@pytest.mark.parametrize(
    "status",
    [
        AccountStatus.UNAUTHENTICATED,
        AccountStatus.LOCATION_REJECTED,
        AccountStatus.ACCOUNT_REJECTED,
        AccountStatus.ACCOUNT_UNTRUSTED,
        AccountStatus.TOS_PENDING,
        AccountStatus.TOS_OUT_OF_DATE,
        AccountStatus.ACCOUNT_REJECTED_BY_GUARDIAN,
        AccountStatus.GUARDIAN_APPROVAL_REQUIRED,
    ],
)
def test_classifies_non_available_account_statuses_as_auth(
    status: AccountStatus,
) -> None:
    assert classify_error(AccountStatusError(status)) is ErrorKind.AUTH


def test_classifies_temporarily_unavailable_account_status_as_rate_limit() -> None:
    error = AccountStatusError(AccountStatus.ACCESS_TEMPORARILY_UNAVAILABLE)

    assert classify_error(error) is ErrorKind.RATE_LIMIT


def test_classifies_rate_limit_errors(rate_limit_error: BaseException) -> None:
    assert classify_error(rate_limit_error) is ErrorKind.RATE_LIMIT


def test_classifies_transient_errors(transient_error: BaseException) -> None:
    assert classify_error(transient_error) is ErrorKind.TRANSIENT


def test_classifies_fatal_errors(fatal_error: BaseException) -> None:
    assert classify_error(fatal_error) is ErrorKind.FATAL


def test_fatal_transport_error_takes_precedence_over_curl_fallback() -> None:
    error = _exception_instance(cc_exc.InvalidURL)

    assert isinstance(error, CurlError)
    assert classify_error(error) is ErrorKind.FATAL
