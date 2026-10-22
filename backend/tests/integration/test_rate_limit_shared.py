"""One API rate limit across API instances (counted in PostgreSQL), with an in-process
fallback when the database is unavailable. Before, each process counted for itself, so two
instances allowed twice the configured rate."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from opsbridge.api.app import create_app
from opsbridge.api.ratelimit import SharedWindowLimiter
from opsbridge.persistence.db import Database

pytestmark = pytest.mark.integration
HOUR = 3600  # long windows keep a test from straddling a window boundary


def _codes(settings: Any, db: Database, requests: int) -> list[int]:
    a, b = (
        TestClient(create_app(settings=settings, db=db), base_url="http://localhost:5173")
        for _ in range(2)
    )
    with a, b:  # two API instances, requests alternating between them
        return [(a if i % 2 == 0 else b).get("/api/v1/me").status_code for i in range(requests)]


def test_two_api_instances_share_one_limit(app_db: Database, settings: Any) -> None:
    limited = {"rate_limit_per_minute": 3, "rate_limit_window_seconds": HOUR}
    shared = _codes(settings.model_copy(update=limited), app_db, 6)
    assert [c == 429 for c in shared] == [False, False, False, True, True, True]
    per_process = _codes(
        settings.model_copy(update={**limited, "rate_limit_shared": False}), app_db, 6
    )
    assert 429 not in per_process  # the old behaviour: 3 per instance, 6 in total


def test_the_window_rolls_over_and_retry_after_is_truthful(app_db: Database, settings: Any) -> None:
    s = settings.model_copy(update={"rate_limit_per_minute": 2, "rate_limit_window_seconds": 2})
    with TestClient(create_app(settings=s, db=app_db), base_url="http://localhost:5173") as c:
        codes = [c.get("/api/v1/me") for _ in range(3)]
        if codes[2].status_code != 429:  # the first requests straddled a 2 s boundary
            codes = [c.get("/api/v1/me") for _ in range(3)]
        limited = codes[2]
        assert limited.status_code == 429 and limited.json()["error"]["retryable"] is True
        wait = int(limited.headers["Retry-After"])
        assert 1 <= wait <= 2
        time.sleep(wait + 0.2)
        assert c.get("/api/v1/me").status_code != 429


def test_without_the_database_each_process_still_limits() -> None:
    down = create_engine(
        "postgresql+psycopg://nobody:nothing@127.0.0.1:9/none", connect_args={"connect_timeout": 2}
    )
    limiter = SharedWindowLimiter(down, limit=2, window_seconds=HOUR)
    assert [limiter.hit("ip:192.0.2.1")[0] for _ in range(3)] == [True, True, False]
    down.dispose()


def test_expired_windows_are_pruned(app_db: Database) -> None:
    limiter = SharedWindowLimiter(app_db.engine, limit=10, window_seconds=60)
    assert limiter.hit("ip:192.0.2.2") == (True, 0)
    with app_db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rate_limit_windows (key, window_start, count) "
                "VALUES ('ip:192.0.2.3', now() - interval '1 hour', 99)"
            )
        )
    limiter.prune()
    with app_db.engine.begin() as conn:
        keys = conn.execute(text("SELECT key FROM rate_limit_windows ORDER BY key")).scalars().all()
    assert keys == ["ip:192.0.2.2"]
