"""The worker process survives a database outage instead of exiting (found when the dev
database container was recreated and the worker crashed, taking the dev stack down)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from opsbridge.config import get_settings
from opsbridge.workflow import jobs
from opsbridge.workflow.worker import Worker


def test_worker_backs_off_on_database_errors_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    worker = Worker(db=None, settings=get_settings())  # type: ignore[arg-type]
    waits: list[float] = []

    def claim(*_a: Any, **_k: Any) -> None:
        calls["n"] += 1
        if calls["n"] <= 3:
            raise OperationalError(
                "SELECT 1", {}, Exception("the database system is shutting down")
            )
        worker.stop_event.set()  # database is back; nothing to do; stop the loop
        return None

    def wait(timeout: float | None = None) -> bool:
        waits.append(float(timeout or 0))
        return worker.stop_event.is_set()

    monkeypatch.setattr(jobs, "claim", claim)
    monkeypatch.setattr(worker.stop_event, "wait", wait)
    worker.run_forever()
    assert calls["n"] == 4
    assert waits[:3] == [0.5, 1.0, 2.0]  # exponential backoff while the database is down
