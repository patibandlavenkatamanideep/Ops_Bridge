"""Tenants choose only credentials registered to them (production review A06).

Before, a tenant administrator could name any syntactically valid credential reference, and
dispatch read whatever secret `OPSBRIDGE_CONNECTION_SECRET_<REF>` held; an allowlisted
repository said nothing about who owns a credential. Now the deployment registers each
reference with the tenant, connector and repositories it may serve; a reference that is not
registered to the tenant and destination is refused when the connection is set and is never
read at dispatch.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

from opsbridge.persistence.db import Database
from opsbridge.persistence.models import TaskConnection
from opsbridge.services import connections
from opsbridge.services.investigations import Conflict

from .test_approvals_execution import SANDBOX_REPO, it_admin

pytestmark = pytest.mark.integration
OTHER_REPO = "opsbridge-sandbox/elsewhere"
REGISTRY = {
    "it-github": {
        "tenant": "demo-it",
        "connector": "github_issues",
        "repositories": [SANDBOX_REPO],
    },
    "furniture-github": {
        "tenant": "demo-furniture",
        "connector": "github_issues",
        "repositories": [SANDBOX_REPO],
    },
}


def _settings(st: dict[str, Any]) -> Any:
    return st["settings"].model_copy(
        update={
            "github_repository_allowlist": [SANDBOX_REPO, OTHER_REPO],
            "connection_credentials": {
                ref: connections.ConnectionCredential(**grant) for ref, grant in REGISTRY.items()
            },
        }
    )


@pytest.mark.parametrize(
    ("ref", "repo", "why"),
    [
        ("furniture-github", SANDBOX_REPO, "another tenant's credential"),
        ("anything-at-all", SANDBOX_REPO, "a reference nobody registered"),
        ("it-github", OTHER_REPO, "registered for another repository"),
    ],
)
def test_a_tenant_cannot_choose_a_credential_not_registered_to_it(
    stack, app_db: Database, migrator_engine: Engine, ref: str, repo: str, why: str
) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(Conflict) as exc:
        connections.change(
            app_db, it_admin(stack, migrator_engine), _settings(stack), connector="github_issues",
            repository=repo, credential_ref=ref, reason=why,
        )  # fmt: skip
    assert exc.value.code == "credential_not_registered", why


def test_its_own_registered_credential_is_accepted(
    stack, app_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    made = connections.change(
        app_db, it_admin(stack, migrator_engine), _settings(stack), connector="github_issues",
        repository=SANDBOX_REPO, credential_ref="it-github", reason="tasks go to GitHub",
    )  # fmt: skip
    assert made["credential_ref"] == "it-github"


def test_dispatch_never_reads_a_secret_the_tenant_does_not_own(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    # A connection row naming another tenant's reference (stored before the registry, or
    # written around the service): the secret exists, and must still not be read.
    monkeypatch.setenv(connections.SECRET_PREFIX + "FURNITURE_GITHUB", "furniture-secret")
    monkeypatch.setenv(connections.SECRET_PREFIX + "IT_GITHUB", "it-secret")
    settings = _settings(stack).model_copy(update={"github_token": SecretStr("deployment")})

    def row(ref: str, repo: str = SANDBOX_REPO) -> TaskConnection:
        return TaskConnection(
            tenant_id=uuid.uuid4(),
            version=1,
            connector="github_issues",
            destination={"connector": "github_issues", "repository": repo},
            credential_ref=ref,
            status="active",
            reason="test",
        )

    assert (
        connections.credential(settings, row("furniture-github"), "github_issues", "demo-it")
        is None
    )
    assert (
        connections.credential(settings, row("it-github", OTHER_REPO), "github_issues", "demo-it")
        is None
    )
    assert (
        connections.credential(settings, row("it-github"), "github_issues", "demo-it")
        == "it-secret"
    )
