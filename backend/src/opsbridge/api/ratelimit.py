"""API rate limit shared by every API instance, counted in PostgreSQL.

A fixed window aligned by the database clock (so instances agree), one upsert per request on
`rate_limit_windows`. If the database cannot be used, each process falls back to its own
in-memory window so the limit still applies (per instance) rather than disappearing. Like the
in-process limiter this protects against overload and simple abuse; it is not a DDoS defence
(docs/security-and-threat-model.md).
"""

from __future__ import annotations

import logging
import random
import time

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from .middleware import FixedWindowLimiter

log = logging.getLogger("opsbridge.api.ratelimit")

_HIT = text(
    "INSERT INTO rate_limit_windows (key, window_start, count) "
    "VALUES (:key, date_bin(make_interval(secs => :w), now(), TIMESTAMPTZ '2000-01-01 00:00:00+00'), 1) "
    "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limit_windows.count + 1 "
    "RETURNING count, "
    "ceil(extract(epoch FROM rate_limit_windows.window_start + make_interval(secs => :w) - now()))"
)
_PRUNE = text(
    "DELETE FROM rate_limit_windows WHERE window_start < now() - make_interval(secs => :w * 2)"
)


class SharedWindowLimiter:
    def __init__(
        self, engine: Engine, limit: int, window_seconds: int, prune_probability: float = 0.002
    ) -> None:
        self.engine = engine
        self.limit = limit
        self.window = window_seconds
        self.prune_probability = prune_probability
        self.fallback = FixedWindowLimiter(limit, float(window_seconds))
        self._warned_at = 0.0

    def hit(self, key: str) -> tuple[bool, int]:
        try:
            with self.engine.begin() as conn:
                count, seconds_left = conn.execute(_HIT, {"key": key, "w": self.window}).one()
                if random.random() < self.prune_probability:  # noqa: S311 - not security
                    conn.execute(_PRUNE, {"w": self.window})
        except SQLAlchemyError:
            now = time.monotonic()
            if now - self._warned_at > 60:
                self._warned_at = now
                log.warning("shared rate limit unavailable; counting in this process for now")
            return self.fallback.hit(key)
        if int(count) > self.limit:
            return False, max(1, int(seconds_left or 1))
        return True, 0

    def prune(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(_PRUNE, {"w": self.window})
