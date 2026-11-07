"""Bounded reads for the responses the Anthropic and MCP SDKs read themselves.

Both SDKs read through their own `httpx2` client: the Anthropic SDK reads a whole reply into
memory before parsing it, and the MCP client reads a JSON reply with `aread()` (its SSE
parser caps each event, not a stream). `http_transport` cannot reach those reads, so a
transport under each client bounds every response as it arrives:
- its bytes may not pass `max_bytes`, and a declared length past it is refused before
  reading;
- the whole response must arrive within the request's own read timeout, which the client
  otherwise applies to each read (review N07): every socket wait of the synchronous client
  (connecting, TLS, sending, each read of the headers and the body) is capped at the time
  left, under `deadline_transport()`, and the deadline is checked when the headers have
  arrived, as each chunk arrives and at the end, so a reply trickling in, or late with no
  chunk after it, stops there. Looking the host up and trying each of its addresses count
  against the same deadline, as in `http_transport`. The asynchronous MCP client is bounded
  by a cancel scope around each call instead (mcp_services/client.py);
- replies are asked for unencoded, and a compressed one is refused, since the client above
  would inflate it without a bound.

The errors subclass `httpx2`'s, so each SDK reports them as it reports any other transport
failure: the Anthropic SDK as a connection error or timeout, the MCP client as a failed
call whose outcome is unknown.
"""

from __future__ import annotations

import ssl
import time
import typing
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any

import httpcore2
import httpx2

from .http_transport import addresses, capped, out_of_time, until

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


class _DeadlineStream(httpcore2.NetworkStream):
    def __init__(self, inner: httpcore2.NetworkStream) -> None:
        self._inner = inner

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._inner.read(max_bytes, capped(timeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._inner.write(buffer, capped(timeout))

    def close(self) -> None:
        self._inner.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore2.NetworkStream:
        return _DeadlineStream(self._inner.start_tls(ssl_context, server_hostname, capped(timeout)))

    def get_extra_info(self, info: str) -> typing.Any:
        return self._inner.get_extra_info(info)


class DeadlineBackend(httpcore2.NetworkBackend):
    """httpcore2's network backend, each wait capped at the time left of the request."""

    def __init__(self, inner: httpcore2.NetworkBackend | None = None) -> None:
        self._inner = inner or httpcore2.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore2.NetworkStream:
        options = None if socket_options is None else list(socket_options)
        try:
            found = addresses(host, port)
        except TimeoutError as exc:
            raise httpcore2.ConnectTimeout(str(exc)) from exc
        except OSError as exc:
            raise httpcore2.ConnectError(str(exc)) from exc
        for i, address in enumerate(found):  # in the resolver's order, each with the time left
            try:
                stream = self._inner.connect_tcp(
                    address, port, capped(timeout), local_address, options
                )
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout):
                if i == len(found) - 1 or out_of_time():
                    raise
                continue
            return _DeadlineStream(stream)
        raise httpcore2.ConnectError(f"{host!r} has no address")

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore2.NetworkStream:
        return _DeadlineStream(
            self._inner.connect_unix_socket(path, capped(timeout), socket_options)
        )

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def deadline_transport(**kwargs: Any) -> httpx2.HTTPTransport:
    """An httpx2 transport whose connections wait no longer than the request in progress
    allows (the pool's backend is set after httpx2 builds it; a version that moves it fails
    here)."""
    transport = httpx2.HTTPTransport(**kwargs)
    pool = transport._pool
    if not isinstance(pool, httpcore2.ConnectionPool) or not hasattr(pool, "_network_backend"):
        raise RuntimeError("httpx2's transport no longer exposes the pool's network backend")
    pool._network_backend = DeadlineBackend(pool._network_backend)
    return transport


class _Budget:
    def __init__(self, request: httpx2.Request, max_bytes: int, seconds: float) -> None:
        self.request, self.max_bytes, self.seen = request, max_bytes, 0
        self.until = _monotonic() + seconds  # the checks (a test may move this clock)
        self.socket_until = time.monotonic() + seconds  # the socket waits

    def check(self) -> None:
        if _monotonic() > self.until:
            raise ReadDeadlineExceeded("response not complete in time", request=self.request)

    def take(self, chunk: bytes) -> bytes:
        self.check()
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
        chunks = iter(self._inner)
        while True:
            with until(self._budget.socket_until):  # the reads the SDK makes, later
                chunk = next(chunks, None)
            if chunk is None:
                break
            yield self._budget.take(chunk)
        self._budget.check()  # the end came in time

    def close(self) -> None:
        self._inner.close()


class _AsyncStream(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream, budget: _Budget) -> None:
        self._inner, self._budget = inner, budget

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            yield self._budget.take(chunk)
        self._budget.check()  # the end came in time

    async def aclose(self) -> None:
        await self._inner.aclose()


def _prepare(request: httpx2.Request) -> float:
    """Asks for an unencoded reply; returns the seconds the whole response may take."""
    request.headers["Accept-Encoding"] = "identity"
    read = (request.extensions.get("timeout") or {}).get("read")
    return float(read) if read else DEFAULT_TOTAL_SECONDS


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
        budget = _Budget(request, self._max_bytes, _prepare(request))
        with until(budget.socket_until):
            response = self._inner.handle_request(request)
        try:
            budget.check()  # the headers came in time
            if (refused := _refusal(response, request, self._max_bytes)) is not None:
                raise refused
        except Exception:
            response.close()
            raise
        assert isinstance(response.stream, httpx2.SyncByteStream)
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_Stream(response.stream, budget),
            extensions=response.extensions,
        )

    def close(self) -> None:
        self._inner.close()


class AsyncBoundedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport, *, max_bytes: int) -> None:
        self._inner, self._max_bytes = inner, max_bytes

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        budget = _Budget(request, self._max_bytes, _prepare(request))
        response = await self._inner.handle_async_request(request)
        try:
            budget.check()  # the headers came in time
            if (refused := _refusal(response, request, self._max_bytes)) is not None:
                raise refused
        except Exception:
            await response.aclose()
            raise
        assert isinstance(response.stream, httpx2.AsyncByteStream)
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_AsyncStream(response.stream, budget),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()
