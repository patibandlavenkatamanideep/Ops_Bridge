"""Each application container is given only the secrets its own process uses (production
review A06). Before, one shared environment block gave every container every secret: the
simulator and both MCP services held the migrator's database password, which owns every
table, and the session and client secrets of the API and the worker."""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from opsbridge.config import REPO_ROOT

COMPOSE = REPO_ROOT / "infra" / "compose" / "docker-compose.yml"
SECRET = re.compile(r"PASSWORD|SECRET|PRIVATE_KEY|TOKEN")
# What each process reads (opsbridge/cli.py, api/, workflow/worker.py, mcp_services/factory.py):
# its own database role; the API its login and session secrets; the worker its client secret
# for MCP access tokens; the simulator secret wherever simulator tasks or records are reached.
EXPECTED = {
    "bootstrap": {"OPSBRIDGE_DB_MIGRATOR_PASSWORD"},
    "api": {
        "OPSBRIDGE_DB_APP_PASSWORD",
        "OPSBRIDGE_OIDC_CLIENT_SECRET",
        "OPSBRIDGE_SESSION_SECRET",
        "OPSBRIDGE_SIM_MASTER_SECRET",
    },
    "worker": {
        "OPSBRIDGE_DB_WORKER_PASSWORD",
        "OPSBRIDGE_WORKER_CLIENT_SECRET",
        "OPSBRIDGE_SIM_MASTER_SECRET",
    },
    "records-mcp": {"OPSBRIDGE_DB_MCP_PASSWORD"},
    "casework-mcp": {"OPSBRIDGE_DB_MCP_PASSWORD", "OPSBRIDGE_SIM_MASTER_SECRET"},
    "simulator": {"OPSBRIDGE_SIM_MASTER_SECRET"},
}


def services() -> dict[str, Any]:
    return dict(yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"])


def test_every_application_container_is_listed() -> None:
    app = {name for name, s in services().items() if "app" in s.get("profiles", [])}
    assert app == set(EXPECTED)


@pytest.mark.parametrize("service", sorted(EXPECTED))
def test_a_container_holds_only_the_secrets_its_process_uses(service: str) -> None:
    env = services()[service].get("environment") or {}
    assert {name for name in env if SECRET.search(name)} == EXPECTED[service]
