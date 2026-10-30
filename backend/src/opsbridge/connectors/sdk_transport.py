"""Bounded reads for the responses the Anthropic and MCP SDKs read themselves.

Both SDKs read through their own `httpx2` client: the Anthropic SDK reads a whole reply into
memory before parsing it, and the MCP client reads a JSON reply with `aread()` (its SSE
parser caps each event, not a stream). `http_transport` cannot reach those reads, so a
transport under each client bounds every response as it arrives:
- its bytes may not pass `max_bytes`, and a declared length past it is refused before
  reading;
- the whole response must arrive within the request's own read timeout, which the client
  otherwise applies to each read, so a body trickling in just inside it stops there;
- replies are asked for unencoded, and a compressed one is refused, since the client above
  would inflate it without a bound.

The errors subclass `httpx2`'s, so each SDK reports them as it reports any other transport
failure: the Anthropic SDK as a connection error or timeout, the MCP client as a failed
call whose outcome is unknown.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator

import httpx2

# A model reply is a few kilobytes of JSON; an MCP tool result is smaller still.
MAX_MODEL_REPLY_BYTES = 8 * 1024 * 1024
MAX_MCP_RESPONSE_BYTES = 1024 * 1024
# For a request that carries no read timeout of its own.
DEFAULT_TOTAL_SECONDS = 60.0
_monotonic = time.monotonic


class ResponseTooLarge(httpx2.ReadError):
    """The response passed its limit; the rest was not read."""


class ReadDeadlineExceeded(httpx2.ReadTimeout):
    """The whole response took longer than the request's timeout."""


class UnsupportedEncoding(httpx2.DecodingError):
    """The response is compressed; it was asked for unencoded."""


class _Budget:
    def __init__(self, request: httpx2.Request, max_bytes: int, until: float) -> None:
        self.request, self.max_bytes, self.until, self.seen = request, max_bytes, until, 0

    def take(self, chunk: bytes) -> bytes:
        if _monotonic() > self.until:
            raise ReadDeadlineExceeded("response not complete in time", request=self.request)
        self.seen += len(chunk)
        if self.seen > self.max_bytes:
            raise ResponseTooLarge(
                f"response passed the {self.max_bytes}-byte limit; the rest was not read",
                request=self.request,
            )
        return chunk


class _Stream(httpx2.SyncByteStream):
    def __init__(self, inner: httpx2.SyncByteStream, budget: _Budget) -> None:
        self._inner, self._budget = inner, budget

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._inner:
            yield self._budget.take(chunk)

    def close(self) -> None:
        self._inner.close()


class _AsyncStream(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream, budget: _Budget) -> None:
        self._inner, self._budget = inner, budget

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            yield self._budget.take(chunk)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _prepare(request: httpx2.Request) -> float:
    """Asks for an unencoded reply; returns the response's total deadline."""
    request.headers["Accept-Encoding"] = "identity"
    read = (request.extensions.get("timeout") or {}).get("read")
    return _monotonic() + (float(read) if read else DEFAULT_TOTAL_SECONDS)


def _refusal(
    response: httpx2.Response, request: httpx2.Request, max_bytes: int
) -> Exception | None:
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        return UnsupportedEncoding(
            f"content encoding {encoding!r} was not asked for", request=request
        )
    declared = response.headers.get("content-length", "").strip()
    if declared.isdigit() and int(declared) > max_bytes:
        return ResponseTooLarge(
            f"response declares {declared} bytes, over the {max_bytes}-byte limit",
            request=request,
        )
    return None


class BoundedTransport(httpx2.BaseTransport):
    def __init__(self, inner: httpx2.BaseTransport, *, max_bytes: int) -> None:
        self._inner, self._max_bytes = inner, max_bytes

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        until = _prepare(request)
        response = self._inner.handle_request(request)
        if (refused := _refusal(response, request, self._max_bytes)) is not None:
            response.close()
            raise refused
        assert isinstance(response.stream, httpx2.SyncByteStream)
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_Stream(response.stream, _Budget(request, self._max_bytes, until)),
            extensions=response.extensions,
        )

    def close(self) -> None:
        self._inner.close()


class AsyncBoundedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport, *, max_bytes: int) -> None:
        self._inner, self._max_bytes = inner, max_bytes

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        until = _prepare(request)
        response = await self._inner.handle_async_request(request)
        if (refused := _refusal(response, request, self._max_bytes)) is not None:
            await response.aclose()
            raise refused
        assert isinstance(response.stream, httpx2.AsyncByteStream)
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_AsyncStream(response.stream, _Budget(request, self._max_bytes, until)),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()
