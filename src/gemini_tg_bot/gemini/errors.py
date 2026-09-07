"""Classify errors raised while communicating with Gemini."""

import asyncio
from enum import StrEnum

from curl_cffi.curl import CurlError
from curl_cffi.requests import exceptions as cc_exc
from gemini_webapi import exceptions as gw_exc


class ErrorKind(StrEnum):
    """Error categories consumed by the Gemini service state machine."""

    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    FATAL = "fatal"


_AUTH_ERRORS = (gw_exc.AuthError,)

_RATE_LIMIT_ERRORS = (
    gw_exc.UsageLimitExceededError,
    gw_exc.TemporarilyBlockedError,
)

_UPSTREAM_TRANSIENT_ERRORS = (gw_exc.TimeoutError,)

_UPSTREAM_FATAL_ERRORS = (
    gw_exc.ModelInvalidError,
    gw_exc.ImageGenerationError,
    gw_exc.APIError,
    gw_exc.GeminiError,
)

_FATAL_TRANSPORT_ERRORS = (
    cc_exc.InvalidURL,
    cc_exc.MissingSchema,
    cc_exc.InvalidSchema,
    cc_exc.URLRequired,
    cc_exc.InvalidHeader,
    cc_exc.ImpersonateError,
)

_TRANSIENT_ERRORS = (
    cc_exc.Timeout,
    cc_exc.ConnectionError,
    cc_exc.ChunkedEncodingError,
    cc_exc.IncompleteRead,
    cc_exc.ProxyError,
    CurlError,
    asyncio.TimeoutError,
    OSError,
)


def classify_error(error: BaseException) -> ErrorKind:
    """Return the handling category for an exception.

    Unknown exceptions fail closed as fatal so callers never retry an
    unclassified programming or configuration error.
    """

    if isinstance(error, _AUTH_ERRORS):
        return ErrorKind.AUTH
    if isinstance(error, _RATE_LIMIT_ERRORS):
        return ErrorKind.RATE_LIMIT
    if isinstance(error, _UPSTREAM_TRANSIENT_ERRORS):
        return ErrorKind.TRANSIENT
    if isinstance(error, _UPSTREAM_FATAL_ERRORS):
        return ErrorKind.FATAL

    # Transport configuration errors also inherit CurlError/OSError, so they
    # must be classified before the retryable transport fallbacks.
    if isinstance(error, _FATAL_TRANSPORT_ERRORS):
        return ErrorKind.FATAL
    if isinstance(error, _TRANSIENT_ERRORS):
        return ErrorKind.TRANSIENT

    return ErrorKind.FATAL
