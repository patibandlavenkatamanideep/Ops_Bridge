"""Responses read by the Anthropic and MCP SDKs are bounded too (production review A04).

Both SDKs read their responses themselves, through their own `httpx2` client: the Anthropic
SDK reads a whole reply into memory before parsing it, and the MCP client reads a JSON reply
with `aread()` (SSE events are capped per event, a stream as a whole is not). A transport
under each client now counts the bytes as they arrive and stops past the limit, holds the
whole response to the request's own timeout as a total deadline (so a drip just inside the
per-read timeout stops), and asks for unencoded replies, refusing a compressed one that the
client above would inflate without a bound. Its errors subclass `httpx2`'s, so each SDK
reports them as it reports other transport failures: the adapter as model unavailable or
timed out (the rules-only path takes over), the MCP client as an outcome it cannot know.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx2
import pytest

from opsbridge.agents.adapters import AnthropicAdapter
from opsbridge.agents.contracts import ModelRequest, ModelTimeout, ModelUnavailable
from opsbridge.connectors import sdk_transport
from opsbridge.mcp_services.client import McpToolClient, McpTransportError

MiB = 1024 * 1024
CHUNK = 64 * 1024
MESSAGE = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "claude-test",
    "content": [{"type": "text", "text": '{"summary": "ok"}'}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 5},
}


class Body:
    """A response body streamed in chunks that records how much of it was read."""

    def __init__(self, data: bytes) -> None:
        self.data, self.sent = data, 0

    def chunks(self) -> Iterator[bytes]:
        for i in range(0, len(self.data), CHUNK):
            piece = self.data[i : i + CHUNK]
            self.sent += len(piece)
            yield piece

    def __iter__(self) -> Iterator[bytes]:
        return self.chunks()


class AsyncBody:
    """The same for an async client. It must not be iterable synchronously at all: a body
    that is would be read as a sync stream, and the async client would fail before reading
    it (a test would then pass without reading anything)."""

    def __init__(self, data: bytes) -> None:
        self.body = Body(data)

    @property
    def sent(self) -> int:
        return self.body.sent

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for piece in self.body.chunks():
            yield piece


def _request() -> ModelRequest:
    return ModelRequest(
        template_id="test",
        template_digest="sha256:test",
        system="s",
        user="u",
        output_schema={"type": "object"},
        max_output_tokens=100,
        timeout_seconds=30.0,
        params={},
    )


def _adapter(handler: Any) -> AnthropicAdapter:
    return AnthropicAdapter("claude-test", "test-key", transport=httpx2.MockTransport(handler))


def test_an_ordinary_model_reply_is_read_and_asked_for_unencoded() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("accept-encoding", ""))
        return httpx2.Response(200, json=MESSAGE)

    reply = _adapter(handler).generate(_request())
    assert reply.text == '{"summary": "ok"}' and seen == ["identity"]


def test_a_model_reply_past_the_limit_is_stopped_before_it_is_consumed() -> None:
    body = Body(b"x" * (2 * sdk_transport.MAX_MODEL_REPLY_BYTES))
    with pytest.raises(ModelUnavailable):
        _adapter(lambda request: httpx2.Response(200, content=body)).generate(_request())
    limit = sdk_transport.MAX_MODEL_REPLY_BYTES
    assert limit < body.sent <= limit + CHUNK  # read up to the limit, and no further


def test_a_compressed_model_reply_is_refused() -> None:
    packed = gzip.compress(json.dumps(MESSAGE).encode())
    with pytest.raises(ModelUnavailable):
        _adapter(
            lambda request: httpx2.Response(
                200, headers={"content-encoding": "gzip"}, content=Body(packed)
            )
        ).generate(_request())


def test_a_model_reply_that_drips_stops_at_the_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]

    def clock() -> float:
        now[0] += 5.0  # each chunk arrives inside any per-read timeout, but they add up
        return now[0]

    monkeypatch.setattr(sdk_transport, "_monotonic", clock)
    body = Body(json.dumps(MESSAGE).encode() + b" " * (20 * CHUNK))
    with pytest.raises(ModelTimeout):
        _adapter(lambda request: httpx2.Response(200, content=body)).generate(_request())
    assert body.sent < len(body.data)


def test_an_mcp_reply_past_the_limit_is_an_unknown_outcome() -> None:
    body = AsyncBody(b"x" * (8 * MiB))
    client = McpToolClient(
        "http://casework/mcp",
        lambda: "service-token",
        timeout_seconds=5.0,
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, headers={"content-type": "application/json"}, content=body
            )
        ),
    )
    with pytest.raises(McpTransportError, match="ResponseTooLarge"):
        client.call("get_follow_up_task", {}, "delegation")
    limit = sdk_transport.MAX_MCP_RESPONSE_BYTES
    assert limit < body.sent <= limit + CHUNK  # read up to the limit, and no further


def test_a_reply_that_is_not_a_message_is_model_unavailable() -> None:
    # Before: the SDK handed the unparseable 200 body back as text and the adapter failed on
    # it with an AttributeError instead of falling back.
    with pytest.raises(ModelUnavailable, match="not a message"):
        _adapter(lambda request: httpx2.Response(200, content=b"<html>proxy page</html>")).generate(
            _request()
        )


def test_a_declared_length_past_the_limit_is_refused_before_reading() -> None:
    body = Body(b"x" * (2 * MiB))
    transport = sdk_transport.BoundedTransport(
        httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, headers={"content-length": str(2 * MiB)}, content=body
            )
        ),
        max_bytes=MiB,
    )
    with httpx2.Client(transport=transport) as client, pytest.raises(httpx2.ReadError):
        client.get("http://model/v1")
    assert body.sent <= CHUNK
