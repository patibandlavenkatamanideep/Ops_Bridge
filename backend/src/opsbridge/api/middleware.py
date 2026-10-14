"""Perimeter middleware: correlation IDs, security headers, size and rate limits.

Application rate limits protect the service from accidental overload and simple
abuse. They are per process and are NOT a DDoS defence; see docs/security-and-threat-model.md
for the reverse-proxy/WAF boundary.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from opsbridge.auth.sessions import COOKIE_NAME, hash_token
from opsbridge.telemetry.context import correlation_id_var

_CID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

API_CSP = "default-src 'none'; frame-ancestors 'none'"
SPA_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'"
)


class PerimeterMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, max_body_bytes: int, rate_limit_per_minute: int) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.max_body_bytes = max_body_bytes
        self.limiter = FixedWindowLimiter(rate_limit_per_minute, 60.0)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-correlation-id", "")
        cid = incoming if _CID_RE.match(incoming) else secrets.token_hex(12)
        request.state.correlation_id = cid
        token = correlation_id_var.set(cid)
        try:
            response = await self._guarded(request, call_next, cid)
        finally:
            correlation_id_var.reset(token)
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
        if request.url.path.startswith(("/api/", "/auth/")):
            cookie = request.cookies.get(COOKIE_NAME)
            client = request.client.host if request.client else "unknown"
            key = f"s:{hash_token(cookie)}" if cookie else f"ip:{client}"
            allowed, retry_after = self.limiter.hit(key)
            if not allowed:
                resp = _error(429, "rate_limited", "Too many requests; slow down.", cid, True)
                resp.headers["Retry-After"] = str(retry_after)
                return resp
        return await call_next(request)


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
    """Thread-safe fixed-window counter keyed by session or client address."""

    def __init__(self, limit: int, window_seconds: float, max_keys: int = 50_000) -> None:
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._counts: dict[str, tuple[int, float]] = {}

    def hit(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            count, start = self._counts.get(key, (0, now))
            if now - start >= self.window:
                count, start = 0, now
            count += 1
            if len(self._counts) >= self.max_keys and key not in self._counts:
                # Bound memory: drop expired windows.
                self._counts = {k: v for k, v in self._counts.items() if now - v[1] < self.window}
            self._counts[key] = (count, start)
            if count > self.limit:
                return False, max(1, int(self.window - (now - start)))
            return True, 0
