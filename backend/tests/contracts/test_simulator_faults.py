"""The simulator's timed faults measure time on the clients' clock.

`timeout_after_commit` commits the task and answers after `slow_response_seconds`, so a
client with a shorter deadline sees a timeout after the commit; `delay` holds a request
before handling it. Both waited on the event loop's timer. uvicorn runs uvloop, whose clock on
macOS counts time the host slept while the clients' `time.monotonic()` does not, so after a
host sleep the "late" reply arrived within the client's deadline and an integration test
expecting reconciliation saw a plain confirmation. They now wait by `time.monotonic()`.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from opsbridge.connectors.tasks import AmbiguousFailure, SimTaskConnector, TaskRequest
from opsbridge.simulator.app import create_simulator

from .jumping_clock import serve_jumping

MASTER = "simulator-fault-test-secret"
ORG = "fault-org"


@pytest.mark.parametrize(
    ("mode", "fault"),
    [("timeout_after_commit", {}), ("delay", {"delay_ms": 3000})],
    ids=["timeout_after_commit", "delay"],
)
def test_a_late_reply_stays_late_when_the_server_clock_jumps(
    tmp_path: Path, mode: str, fault: dict[str, int]
) -> None:
    app = create_simulator(
        tmp_path / "sim.sqlite3", MASTER, fault_controls=True, production=False,
        slow_response_seconds=3,
    )  # fmt: skip
    with serve_jumping(app) as (base, loop):
        httpx.post(
            f"{base}/_control/faults",
            json={"org": ORG, "route": "tasks.create", "mode": mode, "count": 1, **fault},
        ).raise_for_status()
        tracker = SimTaskConnector(base, MASTER, ORG, idempotency_keys=False, lookup="strong")
        loop.jump_after(0.1, 5.0)  # the host sleeps: 5 s pass for the simulator, none here
        with pytest.raises(AmbiguousFailure) as exc:
            tracker.create(TaskRequest("t", "b", None, "opsbridge-op:late"), None, 1.0)
        assert isinstance(exc.value.__cause__, httpx.TimeoutException)
