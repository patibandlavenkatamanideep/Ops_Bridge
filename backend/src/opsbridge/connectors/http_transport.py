"""Bounded HTTP reads for every outside response OpsBridge parses.

A response is streamed and its decoded bytes counted as they arrive; the read stops and the
connection is closed as soon as the count passes the limit, whatever Content-Length claims
(a declared length past the limit is refused before the body is read). Compressed bodies are
decoded here, never by the client, with each decompression step capped at what the limit
still allows, so a small gzip body cannot expand without bound; encodings that cannot be
decoded this way are refused. A total deadline covers the whole read: a body trickling in
just inside the per-read timeout stops there.

The errors subclass httpx's own, so a caller's existing handling stays right: a create whose
reply could not be read is still a read failure after the request was sent (ambiguous), and
a read-back or a source read is unavailable or invalid as before.
"""

from __future__ import annotations

import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from opsbridge import safejson

# Encodings decoded here (anything else is refused); sent as Accept-Encoding so the server
# does not choose one this module cannot bound.
ACCEPTED_ENCODINGS = "gzip, deflate"
_monotonic = time.monotonic


class ResponseTooLarge(httpx.ReadError):
    """The decoded body passed its limit; the rest was not read."""


class ReadDeadlineExceeded(httpx.ReadTimeout):
    """The whole read took longer than its total deadline."""


class UnsupportedEncoding(httpx.DecodingError):
    """The body uses a content encoding this module cannot decode within a bound."""


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
        out = bytearray()
        data = raw
        while data and len(out) < room:
            out += self._z.decompress(data, room - len(out))
            data = self._z.unconsumed_tail
        return bytes(out)


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
    `total_seconds`. Raises ResponseTooLarge, ReadDeadlineExceeded, UnsupportedEncoding or
    the client's own httpx errors."""
    now = clock or _monotonic
    started = now()
    headers = {"Accept-Encoding": ACCEPTED_ENCODINGS, **(kwargs.pop("headers", None) or {})}
    with client.stream(method, url, headers=headers, **kwargs) as r:
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
            if now() - started > total_seconds:
                raise ReadDeadlineExceeded(
                    f"response not complete within {total_seconds:g} s", request=r.request
                )
            body += decoder.feed(raw, max_bytes + 1 - len(body))
            if len(body) > max_bytes:
                raise ResponseTooLarge(
                    f"response passed the {max_bytes}-byte limit; the rest was not read",
                    request=r.request,
                )
        return BoundedResponse(r.status_code, r.headers, bytes(body))
