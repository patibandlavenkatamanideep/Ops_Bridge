"""Bounded HTTP reads for every outside response OpsBridge parses.

A response is streamed and its decoded bytes counted as they arrive; the read stops and the
connection is closed as soon as the count passes the limit, whatever Content-Length claims
(a declared length past the limit is refused before the body is read). Compressed bodies are
decoded here, never by the client, with each decompression step capped at what the limit
still allows, so a small gzip body cannot expand without bound; encodings that cannot be
decoded this way are refused. A compressed body counts only if its stream reached its own
end: one that stops short (even after a complete-looking document), fails its checksum, or
has anything after its end is refused. A second gzip member is refused too: HTTP servers send
one, and accepting more would let bytes after the checked end reach the parser. A response
with no body bytes at all (204, 304) has no stream to check and is empty.

One monotonic deadline covers the whole exchange (review N07): connecting, TLS, sending, the
headers, every body read and the end of the stream. It was checked only between body chunks,
so the headers were bounded by the per-read timeout alone (sent a byte at a time, they never
ended), a read that blocked waited its full timeout however little of the total was left,
and a reply whose headers or end came late, with no chunk after them, was accepted. Now
`DeadlineBackend`, the network backend under `deadline_transport()`, caps each socket wait
at the time left of the request in progress, and the deadline is checked again after the
headers and at the end. Connectors use that transport unless a test gives its own; it
connects directly, without proxies from the environment. A wait cut short raises the
client's own timeout errors; the response, and with it the connection, is closed.

The errors subclass httpx's own, so a caller's existing handling stays right: a create whose
reply could not be read is still a read failure after the request was sent (ambiguous), and
a read-back or a source read is unavailable or invalid as before.
"""

from __future__ import annotations

import contextvars
import ssl
import time
import typing
import zlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpcore
import httpx

from opsbridge import safejson

# Encodings decoded here (anything else is refused); sent as Accept-Encoding so the server
# does not choose one this module cannot bound.
ACCEPTED_ENCODINGS = "gzip, deflate"
_monotonic = time.monotonic


# The monotonic time by which the request in progress on this thread must be done.
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "opsbridge_http_deadline", default=None
)
_MIN_WAIT = 0.001  # a socket wait of zero would not block at all; this one times out


@contextmanager
def until(when: float) -> Iterator[None]:
    """Socket waits under `DeadlineBackend` end by `when` (time.monotonic)."""
    token = _deadline.set(when)
    try:
        yield
    finally:
        _deadline.reset(token)


def deadline(total_seconds: float) -> typing.ContextManager[None]:
    """Socket waits under `DeadlineBackend` end within `total_seconds` from now."""
    return until(time.monotonic() + total_seconds)


def capped(timeout: float | None) -> float | None:
    """`timeout`, or less: what is left of the deadline in progress, if there is one."""
    until = _deadline.get()
    if until is None:
        return timeout
    left = max(until - time.monotonic(), _MIN_WAIT)
    return left if timeout is None else min(timeout, left)


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(self, inner: httpcore.NetworkStream) -> None:
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
    ) -> httpcore.NetworkStream:
        return _DeadlineStream(self._inner.start_tls(ssl_context, server_hostname, capped(timeout)))

    def get_extra_info(self, info: str) -> typing.Any:
        return self._inner.get_extra_info(info)


class DeadlineBackend(httpcore.NetworkBackend):
    """httpcore's network backend, each wait capped at the time left (`deadline`)."""

    def __init__(self, inner: httpcore.NetworkBackend | None = None) -> None:
        self._inner = inner or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        return _DeadlineStream(
            self._inner.connect_tcp(host, port, capped(timeout), local_address, socket_options)
        )

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.NetworkStream:
        return _DeadlineStream(
            self._inner.connect_unix_socket(path, capped(timeout), socket_options)
        )

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def deadline_transport(**kwargs: Any) -> httpx.HTTPTransport:
    """An httpx transport whose connections wait no longer than the deadline in progress.

    httpx builds its httpcore pool without a way to choose the network backend; the backend
    is set on the pool it built. A version of either that moves it fails here, not silently
    (tests/test_http_transport.py drives it against a scripted server)."""
    transport = httpx.HTTPTransport(**kwargs)
    pool = transport._pool
    if not isinstance(pool, httpcore.ConnectionPool) or not hasattr(pool, "_network_backend"):
        raise RuntimeError("httpx's transport no longer exposes the pool's network backend")
    pool._network_backend = DeadlineBackend(pool._network_backend)
    return transport


class ResponseTooLarge(httpx.ReadError):
    """The decoded body passed its limit; the rest was not read."""


class ReadDeadlineExceeded(httpx.ReadTimeout):
    """The whole read took longer than its total deadline."""


class UnsupportedEncoding(httpx.DecodingError):
    """The body uses a content encoding this module cannot decode within a bound."""


class CorruptEncoding(httpx.DecodingError):
    """The compressed body is damaged, ends before its stream does, or continues after it."""


@dataclass(frozen=True)
class BoundedResponse:
    status_code: int
    headers: httpx.Headers
    content: bytes

    def json(self) -> Any:
        return safejson.loads(self.content)


class _Decoder:
    """Decodes one content encoding, never producing more than it is asked for."""

    def __init__(self, encoding: str | None, request: httpx.Request) -> None:
        self._request = request
        self._fed = False
        name = (encoding or "identity").strip().lower()
        if name in ("identity", ""):
            self._z = None
        elif name in ("gzip", "x-gzip"):
            self._z = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif name == "deflate":
            self._z = zlib.decompressobj(zlib.MAX_WBITS)
        else:
            raise UnsupportedEncoding(f"content encoding {name!r} is not accepted", request=request)

    def feed(self, raw: bytes, room: int) -> bytes:
        """Decoded bytes of `raw`, at most `room` of them (room = limit + 1 left)."""
        if self._z is None:
            return raw
        if not raw:
            return b""
        self._fed = True
        if self._z.eof:
            raise CorruptEncoding(
                "data after the end of the compressed body", request=self._request
            )
        out = bytearray()
        data = raw
        try:
            while data and len(out) < room:
                out += self._z.decompress(data, room - len(out))
                data = self._z.unconsumed_tail
        except zlib.error as exc:
            raise CorruptEncoding(
                f"compressed body is damaged: {exc}", request=self._request
            ) from exc
        if self._z.unused_data:
            raise CorruptEncoding(
                "data after the end of the compressed body", request=self._request
            )
        return bytes(out)

    def finish(self) -> None:
        """Raises unless a compressed body that started also reached its end."""
        if self._z is not None and self._fed and not self._z.eof:
            raise CorruptEncoding(
                "compressed body ended before its stream did", request=self._request
            )


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_bytes: int,
    total_seconds: float,
    clock: Callable[[], float] | None = None,
    **kwargs: Any,
) -> BoundedResponse:
    """`client.request(...)`, reading at most `max_bytes` decoded bytes within
    `total_seconds`. Raises ResponseTooLarge, ReadDeadlineExceeded, UnsupportedEncoding,
    CorruptEncoding or the client's own httpx errors."""
    now = clock or _monotonic
    started = now()
    headers = {"Accept-Encoding": ACCEPTED_ENCODINGS, **(kwargs.pop("headers", None) or {})}

    def late(r: httpx.Response) -> ReadDeadlineExceeded | None:
        if now() - started <= total_seconds:
            return None
        return ReadDeadlineExceeded(
            f"response not complete within {total_seconds:g} s", request=r.request
        )

    with deadline(total_seconds), client.stream(method, url, headers=headers, **kwargs) as r:
        if (too_late := late(r)) is not None:  # the headers came after it
            raise too_late
        declared = r.headers.get("content-length", "").strip()
        if declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLarge(
                f"response declares {declared} bytes, over the {max_bytes}-byte limit",
                request=r.request,
            )
        if r.is_stream_consumed:
            # A body httpx already holds in memory (in-process test transports build their
            # responses from bytes): it can only be checked after the fact. Network
            # responses always stream.
            if len(r.content) > max_bytes:
                raise ResponseTooLarge(
                    f"response passed the {max_bytes}-byte limit", request=r.request
                )
            return BoundedResponse(r.status_code, r.headers, r.content)
        decoder = _Decoder(r.headers.get("content-encoding"), r.request)
        body = bytearray()
        for raw in r.iter_raw():
            if (too_late := late(r)) is not None:
                raise too_late
            body += decoder.feed(raw, max_bytes + 1 - len(body))
            if len(body) > max_bytes:
                raise ResponseTooLarge(
                    f"response passed the {max_bytes}-byte limit; the rest was not read",
                    request=r.request,
                )
        if (too_late := late(r)) is not None:  # the end came after it, with no chunk since
            raise too_late
        decoder.finish()
        return BoundedResponse(r.status_code, r.headers, bytes(body))
