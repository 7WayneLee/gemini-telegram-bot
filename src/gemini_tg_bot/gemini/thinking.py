"""Request-scoped thinking levels for the pinned upstream transport."""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from gemini_webapi import GeminiClient
from gemini_webapi.constants import Endpoint


class ThinkingLevel(StrEnum):
    """Effort levels offered by Gemini Web."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


DEFAULT_THINKING_LEVEL = ThinkingLevel.MEDIUM


# Official-client capture on 2026-10-11; see docs/upstream-api-contract.md
# for the pinned upstream contract. Index 17 is a turn counter, not effort.
LOW_WIRE_VALUE = 1
MEDIUM_WIRE_VALUE = 4
HIGH_WIRE_VALUE = 2

_MODEL_HEADER = "x-goog-ext-525001261-jspb"
_thinking_wire_value: ContextVar[int | None] = ContextVar(
    "_thinking_wire_value", default=None
)
LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class VideoRequestState:
    """Track transport adaptation without retaining any request content."""

    applied: bool = False
    failed: bool = False


_video_request: ContextVar[VideoRequestState | None] = ContextVar(
    "_video_request", default=None
)


@contextmanager
def video_request() -> Iterator[VideoRequestState]:
    """Scope the official video request and suppress Medium adaptation."""

    state = VideoRequestState()
    video_token = _video_request.set(state)
    thinking_token = _thinking_wire_value.set(None)
    try:
        yield state
    finally:
        _thinking_wire_value.reset(thinking_token)
        _video_request.reset(video_token)


def rewrite_video_request(data: Any, state: VideoRequestState) -> Any:
    """Reproduce contract §五之二 atomically, preserving unknown shapes."""

    try:
        if not isinstance(data, dict):
            raise ValueError
        outer = json.loads(data["f.req"])
        if (
            not isinstance(outer, list)
            or len(outer) != 2
            or not isinstance(outer[1], str)
        ):
            raise ValueError
        inner = json.loads(outer[1])
        if not isinstance(inner, list) or len(inner) != 81:
            raise ValueError
        content = inner[0]
        if (
            not isinstance(content, list)
            or len(content) != 7
            or content[1:] != [0, None, None, None, None, 0]
            or inner[80] != LOW_WIRE_VALUE
            or inner[30] != [4]
            or any(inner[index] is not None for index in (3, 4, 49, 55, 67))
            or inner[68] != 1
        ):
            raise ValueError
        content.extend([
            None, None,
            [None, None, None, None, None, None, [[None, None, None, 1]]],
        ])
        inner.extend([None] * (99 - len(inner)))
        inner[3] = "!" + secrets.token_urlsafe(2600)
        inner[4] = uuid4().hex
        for index, value in {
            30: [4, 16, 19],
            49: 11,
            55: [[16]],
            67: 0,
            68: 2,
            91: 1,
            96: 0,
            98: 1,
        }.items():
            inner[index] = value
        outer[1] = json.dumps(inner, separators=(",", ":"), ensure_ascii=False)
        rewritten = {
            **data,
            "f.req": json.dumps(outer, separators=(",", ":"), ensure_ascii=False),
        }
        state.applied = True
        return rewritten
    except Exception:
        if not state.failed:
            LOGGER.warning(
                "Video request could not be applied; request was left unchanged"
            )
        state.failed = True
        return data


@contextmanager
def thinking_request(level: ThinkingLevel) -> Iterator[dict[str, bool]]:
    """Supply upstream kwargs and scope the Medium transport override."""

    token = (
        _thinking_wire_value.set(MEDIUM_WIRE_VALUE)
        if level is ThinkingLevel.MEDIUM
        else None
    )
    try:
        yield {"extended_thinking": level is not ThinkingLevel.LOW}
    finally:
        if token is not None:
            _thinking_wire_value.reset(token)


def rewrite_thinking_request(
    data: Any,
    headers: Any,
    wire_value: int = MEDIUM_WIRE_VALUE,
) -> tuple[Any, Any]:
    """Copy and rewrite both effort fields, or preserve the entire request."""

    try:
        if not isinstance(data, dict):
            raise ValueError("Unexpected request data shape")
        outer = json.loads(data["f.req"])
        if (
            not isinstance(outer, list)
            or len(outer) < 2
            or not isinstance(outer[1], str)
        ):
            raise ValueError("Unexpected outer request shape")
        inner = json.loads(outer[1])
        if (
            not isinstance(inner, list)
            or len(inner) <= 80
            or inner[80] != HIGH_WIRE_VALUE
        ):
            raise ValueError("Unexpected thinking field")

        if headers is not None and not isinstance(headers, dict):
            raise ValueError("Unexpected headers shape")
        model_header = None
        if headers is not None and _MODEL_HEADER in headers:
            model_header = json.loads(headers[_MODEL_HEADER])
            if (
                not isinstance(model_header, list)
                or len(model_header) < 2
                or model_header[-2] != HIGH_WIRE_VALUE
            ):
                raise ValueError("Unexpected model header shape")

        inner[80] = wire_value
        outer[1] = json.dumps(inner, separators=(",", ":"), ensure_ascii=False)
        new_data = data.copy()
        new_data["f.req"] = json.dumps(
            outer, separators=(",", ":"), ensure_ascii=False
        )
        new_headers = headers.copy() if headers is not None else None
        if model_header is not None:
            model_header[-2] = wire_value
            new_headers[_MODEL_HEADER] = json.dumps(
                model_header, separators=(",", ":"), ensure_ascii=False
            )
        return new_data, new_headers
    except Exception:
        # Unknown upstream shapes must never break a send or expose its contents.
        LOGGER.warning(
            "Medium thinking level could not be applied; request was sent as High"
        )
        return data, headers


class _ThinkingSessionProxy:
    """Delegate the session, intercepting only generation streams."""

    __slots__ = ("_session", "_wire_value", "_video_state")

    def __init__(
        self,
        session: Any,
        wire_value: int | None,
        video_state: VideoRequestState | None = None,
    ) -> None:
        self._session = session
        self._wire_value = wire_value
        self._video_state = video_state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        url = args[1] if len(args) > 1 else kwargs.get("url")
        if url == Endpoint.GENERATE:
            if self._video_state is not None:
                data = rewrite_video_request(kwargs.get("data"), self._video_state)
                headers = kwargs.get("headers")
            else:
                data, headers = rewrite_thinking_request(
                    kwargs.get("data"), kwargs.get("headers"), self._wire_value
                )
            if data is not kwargs.get("data"):
                kwargs = {**kwargs, "data": data, "headers": headers}
        return self._session.stream(*args, **kwargs)


class ThinkingLevelGeminiClient(GeminiClient):
    """Adapt request-scoped Medium effort or video generation."""

    __slots__ = ()

    @property
    def _live_client(self) -> Any:
        session = super()._live_client
        wire_value = _thinking_wire_value.get()
        video_state = _video_request.get()
        if wire_value is None and video_state is None:
            return session
        return _ThinkingSessionProxy(session, wire_value, video_state)
