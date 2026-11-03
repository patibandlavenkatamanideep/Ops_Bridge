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


# What each container mounts. Before, every one but the simulator mounted the volume that holds
# the delegation signing key, so either MCP service could sign the delegations it is meant
# only to verify. Now only bootstrap (which creates the key) and the worker (the one signer)
# mount it; the MCP services mount its public key read-only and no artifacts.
EXPECTED_VOLUMES = {
    "bootstrap": {
        "opsbridge_delegation_signing:/app/var/keys",
        "opsbridge_delegation_public:/app/var/delegation-public",
    },
    "api": {"opsbridge_appdata:/app/var"},
    "worker": {"opsbridge_appdata:/app/var", "opsbridge_delegation_signing:/app/var/keys:ro"},
    "records-mcp": {"opsbridge_delegation_public:/app/var/delegation-public:ro"},
    "casework-mcp": {"opsbridge_delegation_public:/app/var/delegation-public:ro"},
    "simulator": {"opsbridge_simdata:/app/var"},
}
DOCKERFILE = REPO_ROOT / "infra" / "containers" / "opsbridge.Dockerfile"


def services() -> dict[str, Any]:
    return dict(yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"])


def test_every_application_container_is_listed() -> None:
    app = {name for name, s in services().items() if "app" in s.get("profiles", [])}
    assert app == set(EXPECTED)


@pytest.mark.parametrize("service", sorted(EXPECTED))
def test_a_container_holds_only_the_secrets_its_process_uses(service: str) -> None:
    env = services()[service].get("environment") or {}
    assert {name for name in env if SECRET.search(name)} == EXPECTED[service]


@pytest.mark.parametrize("service", sorted(EXPECTED_VOLUMES))
def test_a_container_mounts_only_the_volumes_its_process_uses(service: str) -> None:
    assert set(services()[service].get("volumes") or []) == EXPECTED_VOLUMES[service]


@pytest.mark.parametrize("service", ["records-mcp", "casework-mcp"])
def test_the_mcp_services_verify_with_the_public_key_they_mount(service: str) -> None:
    path = services()[service]["environment"]["OPSBRIDGE_DELEGATION_PUBLIC_KEY_PATH"]
    assert path == "/app/var/delegation-public/delegation_ed25519.pub.pem"


def test_every_mount_point_under_var_is_made_in_the_image() -> None:
    """Docker gives a new volume the owner of the image's directory it is mounted on; where the
    image has none, the volume is root's and the service user cannot write to it. (Podman
    gives it the container's user instead, so a Podman run does not show the fault; CI's
    image job checks each mount point under Docker.)"""
    made = re.search(r"RUN mkdir -p ([^&]+)&&", DOCKERFILE.read_text(encoding="utf-8"))
    assert made, "the image makes its /app/var directories in one mkdir"
    targets = {
        volume.split(":")[1]
        for name in EXPECTED_VOLUMES
        for volume in services()[name].get("volumes") or []
    }
    assert targets - {"/app/var"} <= set(made.group(1).split())
