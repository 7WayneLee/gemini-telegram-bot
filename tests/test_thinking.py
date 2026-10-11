"""Offline transport contract tests using upstream's real request builder."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any

import pytest
from gemini_webapi import AvailableModel
from gemini_webapi import exceptions as gw_exc
from gemini_webapi.constants import Endpoint

from gemini_tg_bot.gemini.thinking import (
    HIGH_WIRE_VALUE,
    LOW_WIRE_VALUE,
    MEDIUM_WIRE_VALUE,
    ThinkingLevel,
    ThinkingLevelGeminiClient,
    rewrite_thinking_request,
    thinking_request,
)


MODEL_HEADER = "x-goog-ext-525001261-jspb"
RANDOM_HEADER = "x-goog-ext-525005358-jspb"
LEVEL_CASES = [
    (ThinkingLevel.LOW, LOW_WIRE_VALUE),
    (ThinkingLevel.MEDIUM, MEDIUM_WIRE_VALUE),
    (ThinkingLevel.HIGH, HIGH_WIRE_VALUE),
]


@dataclass
class RecordedRequest:
    method: str
    url: Any
    headers: dict[str, str]
    data: dict[str, str]


class FakeSession:
    """Record sends and stop generation before any network or response parsing."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.post_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.result = object()

    def stream(self, method: str, url: Any, **kwargs: Any) -> object:
        self.requests.append(
            RecordedRequest(method, url, kwargs["headers"], kwargs["data"])
        )
        if url == Endpoint.GENERATE:
            raise gw_exc.GeminiError("Offline capture complete")
        return self.result

    def post(self, *args: Any, **kwargs: Any) -> object:
        self.post_calls.append((args, kwargs))
        return self.result


@pytest.fixture
def client() -> ThinkingLevelGeminiClient:
    instance = ThinkingLevelGeminiClient(
        secure_1psid="FAKE_1PSID_FOR_TEST",
        secure_1psidts="FAKE_1PSIDTS_FOR_TEST",
    )
    # The running decorator would otherwise call init(), which uses the network.
    instance._running = True
    instance.client = FakeSession()
    return instance


@pytest.fixture
def model() -> AvailableModel:
    return AvailableModel(
        model_id="FAKE_MODEL_ID_FOR_TEST",
        model_name="fake-test-model",
        display_name="Fake test model",
        description="Offline transport fixture",
        capacity=1,
        model_number=7,
    )


def _inner(request: RecordedRequest) -> list[Any]:
    return json.loads(json.loads(request.data["f.req"])[1])


async def _capture(
    client: ThinkingLevelGeminiClient,
    model: AvailableModel | None,
    kwargs: dict[str, bool],
) -> RecordedRequest:
    session = client.client
    count = len(session.requests)
    with pytest.raises(gw_exc.GeminiError, match="Offline capture complete"):
        async for _ in client._generate(
            prompt="FAKE prompt 中文", model=model, **kwargs
        ):
            pytest.fail("The fake transport must stop before yielding a response")
    assert len(session.requests) == count + 1
    request = session.requests[-1]
    assert request.method == "POST"
    assert request.url == Endpoint.GENERATE
    return request


@pytest.mark.parametrize(("level", "wire_value"), LEVEL_CASES)
async def test_selected_model_level(
    client: ThinkingLevelGeminiClient,
    model: AvailableModel,
    level: ThinkingLevel,
    wire_value: int,
) -> None:
    """Selected models need matching effort fields without losing session identity."""

    with thinking_request(level) as kwargs:
        assert kwargs == {"extended_thinking": level is not ThinkingLevel.LOW}
        request = await _capture(client, model, kwargs)

    assert _inner(request)[80] == wire_value
    header = json.loads(request.headers[MODEL_HEADER])
    assert header[-3] == model.model_number
    assert header[-2] == wire_value
    assert header[-1] == client._sessionid


@pytest.mark.parametrize(("level", "wire_value"), LEVEL_CASES)
async def test_default_model_level(
    client: ThinkingLevelGeminiClient,
    level: ThinkingLevel,
    wire_value: int,
) -> None:
    """Account-default selection must support Medium even without a model header."""

    with thinking_request(level) as kwargs:
        request = await _capture(client, None, kwargs)

    assert _inner(request)[80] == wire_value
    assert MODEL_HEADER not in request.headers


async def test_medium_changes_only_effort_fields(
    client: ThinkingLevelGeminiClient,
    model: AvailableModel,
) -> None:
    """The adapter must preserve the turn counter and all unrelated request fields."""

    with thinking_request(ThinkingLevel.HIGH) as kwargs:
        high = await _capture(client, model, kwargs)
    with thinking_request(ThinkingLevel.MEDIUM) as kwargs:
        medium = await _capture(client, model, kwargs)

    high_inner, medium_inner = _inner(high), _inner(medium)
    assert len(high_inner) == len(medium_inner)
    assert high_inner[80] == HIGH_WIRE_VALUE
    assert medium_inner[80] == MEDIUM_WIRE_VALUE
    for index, (high_value, medium_value) in enumerate(
        zip(high_inner, medium_inner, strict=True)
    ):
        if index not in (59, 80):
            assert medium_value == high_value, f"Unexpected change at index {index}"
    assert medium_inner[17] == high_inner[17] == [[0]]

    high_header = json.loads(high.headers[MODEL_HEADER])
    medium_header = json.loads(medium.headers[MODEL_HEADER])
    assert high_header[-2] == HIGH_WIRE_VALUE
    assert medium_header[-2] == MEDIUM_WIRE_VALUE
    assert len(high_header) == len(medium_header)
    for index, (high_value, medium_value) in enumerate(
        zip(high_header, medium_header, strict=True)
    ):
        if index != len(high_header) - 2:
            assert medium_value == high_value
    assert {
        key: value for key, value in medium.headers.items()
        if key not in (MODEL_HEADER, RANDOM_HEADER)
    } == {
        key: value for key, value in high.headers.items()
        if key not in (MODEL_HEADER, RANDOM_HEADER)
    }
    high_outer = json.loads(high.data["f.req"])
    medium_outer = json.loads(medium.data["f.req"])
    assert medium_outer[:1] + medium_outer[2:] == high_outer[:1] + high_outer[2:]
    assert {key: value for key, value in medium.data.items() if key != "f.req"} == {
        key: value for key, value in high.data.items() if key != "f.req"
    }


def _high_request() -> tuple[dict[str, str], dict[str, str]]:
    inner: list[Any] = [None] * 81
    inner[0] = ["FAKE prompt 中文"]
    inner[17] = [[3]]
    inner[80] = HIGH_WIRE_VALUE
    return (
        {"at": "FAKE_ACCESS_TOKEN_FOR_TEST", "f.req": json.dumps([None, json.dumps(inner)])},
        {MODEL_HEADER: json.dumps([None, 7, HIGH_WIRE_VALUE, "FAKE_SESSION_ID"]),
         "cookie": "FAKE_COOKIE_FOR_TEST"},
    )


@pytest.mark.parametrize("with_header", [True, False])
def test_rewrite_copies_and_preserves_unicode(with_header: bool) -> None:
    """Rewriting must preserve callers' dictionaries and upstream's UTF-8 encoding."""

    data, headers = _high_request()
    if not with_header:
        del headers[MODEL_HEADER]
    original_data, original_headers = data.copy(), headers.copy()

    rewritten_data, rewritten_headers = rewrite_thinking_request(data, headers)

    assert data == original_data
    assert headers == original_headers
    assert rewritten_data is not data
    assert rewritten_headers is not headers
    inner = json.loads(json.loads(rewritten_data["f.req"])[1])
    assert inner[80] == MEDIUM_WIRE_VALUE
    assert inner[17] == [[3]]
    assert "中文" in rewritten_data["f.req"]
    assert json.loads(rewritten_data["f.req"])[1] == json.dumps(
        inner, separators=(",", ":"), ensure_ascii=False
    )
    assert rewritten_data["f.req"] == json.dumps(
        json.loads(rewritten_data["f.req"]), separators=(",", ":"), ensure_ascii=False
    )
    if with_header:
        assert json.loads(rewritten_headers[MODEL_HEADER])[-2] == MEDIUM_WIRE_VALUE
    else:
        assert MODEL_HEADER not in rewritten_headers


@pytest.mark.parametrize(
    "mismatch",
    ["missing_data", "missing_key", "outer_json", "outer_shape", "outer_short",
     "inner_json", "inner_shape", "inner_short", "inner_value", "headers_shape",
     "header_json", "header_shape", "header_short", "header_value"],
)
def test_rewrite_mismatch_preserves_request_and_warns_once(
    mismatch: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Upstream drift must fall back atomically without failing or logging secrets."""

    data, headers = _high_request()
    if mismatch == "missing_data":
        data = None
    elif mismatch == "missing_key":
        del data["f.req"]
    elif mismatch.startswith("outer_"):
        data["f.req"] = {"outer_json": "[", "outer_shape": "{}", "outer_short": "[]"}[mismatch]
    elif mismatch.startswith("inner_"):
        inner = json.loads(json.loads(data["f.req"])[1])
        inner[80] = LOW_WIRE_VALUE
        inner_json = {
            "inner_json": "[", "inner_shape": "{}", "inner_short": "[]",
            "inner_value": json.dumps(inner),
        }[mismatch]
        data["f.req"] = json.dumps([None, inner_json])
    elif mismatch == "headers_shape":
        headers = []
    else:
        headers[MODEL_HEADER] = {
            "header_json": "[", "header_shape": "{}", "header_short": "[]",
            "header_value": json.dumps([7, LOW_WIRE_VALUE, "FAKE_SESSION_ID"]),
        }[mismatch]
    original_data = data.copy() if data is not None else None
    original_headers = headers.copy()

    with caplog.at_level(logging.WARNING, logger="gemini_tg_bot.gemini.thinking"):
        result_data, result_headers = rewrite_thinking_request(data, headers)

    assert result_data is data
    assert result_headers is headers
    assert data == original_data
    assert headers == original_headers
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert caplog.records[0].getMessage() == (
        "Medium thinking level could not be applied; request was sent as High"
    )


@pytest.mark.parametrize("keyword_url", [True, False])
def test_proxy_delegates_other_streams_and_attributes(
    client: ThinkingLevelGeminiClient,
    keyword_url: bool,
) -> None:
    """Uploads, cookie operations and batchexecute must retain their session behavior."""

    session = client.client
    data, headers = _high_request()
    with thinking_request(ThinkingLevel.MEDIUM):
        proxy = client._live_client
        assert proxy.result is session.result
        assert proxy.post(Endpoint.GENERATE, data=data, headers=headers) is session.result
        if keyword_url:
            result = proxy.stream(method="POST", url="https://fake.invalid/upload", data=data, headers=headers)
        else:
            result = proxy.stream("POST", "https://fake.invalid/upload", data=data, headers=headers)

    assert result is session.result
    assert session.post_calls == [((Endpoint.GENERATE,), {"data": data, "headers": headers})]
    assert session.post_calls[0][1]["data"] is data
    assert session.post_calls[0][1]["headers"] is headers
    assert session.requests[0].data is data
    assert session.requests[0].headers is headers


def test_live_session_identity_outside_medium(client: ThinkingLevelGeminiClient) -> None:
    """Existing callers and background tasks must receive the original session."""

    assert client._live_client is client.client
    for level in (ThinkingLevel.LOW, ThinkingLevel.HIGH):
        with thinking_request(level):
            assert client._live_client is client.client


@pytest.mark.parametrize("raises", [False, True])
def test_medium_context_resets(client: ThinkingLevelGeminiClient, raises: bool) -> None:
    """A completed or failed Medium send must never affect subsequent requests."""

    def scoped_request() -> None:
        with thinking_request(ThinkingLevel.MEDIUM):
            assert client._live_client is not client.client
            if raises:
                raise RuntimeError("Synthetic request failure")

    if raises:
        with pytest.raises(RuntimeError, match="Synthetic request failure"):
            scoped_request()
    else:
        scoped_request()
    assert client._live_client is client.client


async def test_concurrent_contexts_are_isolated(
    client: ThinkingLevelGeminiClient,
    model: AvailableModel,
) -> None:
    """Concurrent High and Medium sends must not share the transport override."""

    medium_ready = asyncio.Event()
    high_ready = asyncio.Event()

    async def send(level: ThinkingLevel) -> RecordedRequest:
        with thinking_request(level) as kwargs:
            if level is ThinkingLevel.MEDIUM:
                medium_ready.set()
                await high_ready.wait()
            else:
                await medium_ready.wait()
                high_ready.set()
            return await _capture(client, model, kwargs)

    medium, high = await asyncio.gather(
        send(ThinkingLevel.MEDIUM), send(ThinkingLevel.HIGH)
    )

    assert _inner(medium)[80] == MEDIUM_WIRE_VALUE
    assert json.loads(medium.headers[MODEL_HEADER])[-2] == MEDIUM_WIRE_VALUE
    assert _inner(high)[80] == HIGH_WIRE_VALUE
    assert json.loads(high.headers[MODEL_HEADER])[-2] == HIGH_WIRE_VALUE
    assert client._live_client is client.client


def test_upstream_version_pin() -> None:
    """Internal transport assumptions require explicit verification before upgrades."""

    assert version("gemini-webapi") == "2.1.1", (
        "The adapter in thinking.py has to be re-verified before upgrading gemini-webapi"
    )
