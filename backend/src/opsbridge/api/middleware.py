"""Perimeter middleware: correlation IDs, security headers, size and rate limits.

Application rate limits protect the service from accidental overload and simple
abuse. Every API request counts against its client address here, before anything else, so
a made-up cookie cannot pick a fresh budget; a verified user's own limit is applied after
the session is checked (api/deps.py). Both are shared by all API instances through
PostgreSQL (api/ratelimit.py, with a per-process fallback) and are NOT a DDoS defence; see
docs/security-and-threat-model.md for the reverse-proxy/WAF boundary.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from ipaddress import IPv4Network, IPv6Network, ip_address
from typing import Any

from opentelemetry.trace import SpanKind
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from opsbridge.telemetry import metrics, tracing
from opsbridge.telemetry.context import correlation_id_var

_CID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

API_CSP = "default-src 'none'; frame-ancestors 'none'"
SPA_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'"
)


Network = IPv4Network | IPv6Network


def client_address(peer: str, forwarded_for: str | None, trusted: Sequence[Network]) -> str:
    """The address a request came from. X-Forwarded-For is believed only when the direct peer
    is a configured proxy, and then only the nearest hop that is not itself a proxy: entries
    further left were supplied by the client and cannot choose its bucket."""

    def is_proxy(addr: str) -> bool:
        try:
            return any(ip_address(addr) in net for net in trusted)
        except ValueError:
            return False

    if not forwarded_for or not is_proxy(peer):
        return peer
    for hop in reversed([h.strip() for h in forwarded_for.split(",")]):
        try:
            ip_address(hop)
        except ValueError:
            return peer  # malformed chain: fall back to the proxy itself
        if not is_proxy(hop):
            return hop
    return peer


class PerimeterMiddleware(BaseHTTPMiddleware):
    def __init__(  # type: ignore[no-untyped-def]
        self,
        app,
        *,
        max_body_bytes: int,
        address_limit_per_minute: int,
        limiter: Any = None,
        trusted_proxies: Sequence[Network] = (),
    ) -> None:
        super().__init__(app)
        self.max_body_bytes = max_body_bytes
        self.limiter = limiter or FixedWindowLimiter(address_limit_per_minute, 60.0)
        self.trusted_proxies = list(trusted_proxies)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-correlation-id", "")
        cid = incoming if _CID_RE.match(incoming) else secrets.token_hex(12)
        request.state.correlation_id = cid
        token = correlation_id_var.set(cid)
        started = time.monotonic()
        # A new trace per request: trace context from the internet is not trusted.
        with tracing.span(
            f"{request.method} request", kind=SpanKind.SERVER, correlation_id=cid
        ) as sp:
            try:
                response = await self._guarded(request, call_next, cid)
            finally:
                correlation_id_var.reset(token)
            route = _route_template(request)
            sp.update_name(f"{request.method} {route}")
            sp.set_attribute("http.request.method", request.method)
            sp.set_attribute("http.route", route)
            sp.set_attribute("http.response.status_code", response.status_code)
        metrics.HTTP_REQUESTS.labels(
            request.method, route, metrics.status_class(response.status_code)
        ).inc()
        metrics.HTTP_DURATION.labels(route).observe(time.monotonic() - started)
        response.headers["X-Correlation-Id"] = cid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        path = request.url.path
        is_api = path.startswith(("/api/", "/auth/", "/healthz", "/readyz", "/metrics"))
        response.headers.setdefault("Content-Security-Policy", API_CSP if is_api else SPA_CSP)
        if path.startswith(("/api/", "/auth/")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    async def _guarded(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]], cid: str
    ) -> Response:
        length = request.headers.get("content-length")
        if length is not None:
            try:
                too_big = int(length) > self.max_body_bytes
            except ValueError:
                too_big = True
            if too_big:
                return _error(
                    413, "payload_too_large", "The request body is too large.", cid, False
                )
        elif "chunked" in request.headers.get("transfer-encoding", "").lower():
            # Without a declared length the limit could only be enforced after buffering.
            return _error(
                411, "length_required", "Request bodies must declare Content-Length.", cid, False
            )
        if request.url.path.startswith(("/api/", "/auth/")):
            peer = request.client.host if request.client else "unknown"
            addr = client_address(
                peer, request.headers.get("x-forwarded-for"), self.trusted_proxies
            )
            key = f"ip:{addr}"
            # The shared limiter waits on the database: keep it off the event loop.
            allowed, retry_after = await run_in_threadpool(self.limiter.hit, key)
            if not allowed:
                resp = _error(429, "rate_limited", "Too many requests; slow down.", cid, True)
                resp.headers["Retry-After"] = str(retry_after)
                return resp
        return await call_next(request)


def _route_template(request: Request) -> str:
    """Bounded label: the matched route template, never the raw path."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return str(path)
    if request.url.path.startswith(("/api/", "/auth/")):
        return "unmatched"
    return "static"


def _error(status: int, code: str, message: str, cid: str, retryable: bool) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "correlation_id": cid,
                "retryable": retryable,
                "details": {},
            }
        },
    )


class FixedWindowLimiter:
    """Thread-safe fixed-window counter keyed by user or client address. It holds at most
    `max_keys` keys plus one overflow window: once full (after dropping expired windows),
    new keys share the overflow window, so memory stays bounded and they are limited
    together rather than not at all."""

    OVERFLOW = "*overflow*"

    def __init__(self, limit: int, window_seconds: float, max_keys: int = 50_000) -> None:
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._counts: dict[str, tuple[int, float]] = {}

    def hit(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            if key not in self._counts and len(self._counts) >= self.max_keys:
                self._counts = {k: v for k, v in self._counts.items() if now - v[1] < self.window}
                if len(self._counts) >= self.max_keys:
                    key = self.OVERFLOW
            count, start = self._counts.get(key, (0, now))
            if now - start >= self.window:
                count, start = 0, now
            count += 1
            self._counts[key] = (count, start)
            if count > self.limit:
                return False, max(1, int(self.window - (now - start)))
            return True, 0
