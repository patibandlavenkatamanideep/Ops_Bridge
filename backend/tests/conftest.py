"""Shared pytest configuration.

Integration tests use the dedicated `opsbridge_test` database in the local compose
PostgreSQL. They are skipped with an explicit reason when it is unreachable, and
`make test-integration` fails instead of skipping (OPSBRIDGE_REQUIRE_INTEGRATION=1).
"""

from __future__ import annotations

import os
import socket

import pytest

# Never let tests reach paid providers or real customer systems.
os.environ.setdefault("OPSBRIDGE_ENV", "test")
os.environ["OPSBRIDGE_LIVE_MODEL_OPT_IN"] = "false"
os.environ["OPSBRIDGE_GITHUB_LIVE_WRITES_AUTHORIZED"] = "false"
os.environ.pop("ANTHROPIC_API_KEY", None)


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    require = os.environ.get("OPSBRIDGE_REQUIRE_INTEGRATION") == "1"
    db_up = _port_open("127.0.0.1", int(os.environ.get("OPSBRIDGE_DB_PORT", "5433")))
    kc_up = _port_open("127.0.0.1", 8081)
    for item in items:
        if "integration" in item.keywords and not db_up:
            if require:
                raise pytest.UsageError("PostgreSQL on :5433 is required (run `make dev`)")
            item.add_marker(pytest.mark.skip(reason="PostgreSQL :5433 unreachable; run `make dev`"))
        if "keycloak" in item.keywords and not kc_up:
            if require:
                raise pytest.UsageError("Keycloak on :8081 is required (run `make dev`)")
            item.add_marker(pytest.mark.skip(reason="Keycloak :8081 unreachable; run `make dev`"))
        if "live" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="live provider tests never run by default"))
