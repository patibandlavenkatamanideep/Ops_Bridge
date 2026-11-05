"""Which secret a task connection may use, and what a tenant is shown (review N06).

Before: in every environment a GitHub connection without a credential reference used the
deployment's own token, so any tenant administrator could send with it to any allowlisted
repository; two references differing only in `-` and `_` read the same secret; and every
tenant was shown the deployment's whole allowlist as if it could send there. Now the
deployment token serves only development, staging and production refuse two references for
one secret, and a tenant is shown the repositories it can send to.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from opsbridge.config import ConnectionCredential, Settings
from opsbridge.persistence.models import TaskConnection
from opsbridge.services import connections

IT_REPO, FURN_REPO, SHARED = "acme/it-tasks", "acme/furniture-tasks", "acme/shared"
REGISTRY = {
    "it-github": ConnectionCredential(
        tenant="demo-it", connector="github_issues", repositories=[IT_REPO, SHARED]
    ),
    "furniture-github": ConnectionCredential(
        tenant="demo-furniture", connector="github_issues", repositories=[FURN_REPO, SHARED]
    ),
}


def settings(env: str = "development", **changes: Any) -> Settings:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(
        update={
            "env": env,
            "github_token": SecretStr("deployment-token"),
            "github_repository_allowlist": [IT_REPO, FURN_REPO, SHARED, "acme/unregistered"],
            "connection_credentials": REGISTRY,
            **changes,
        }
    )


def row(ref: str | None, repo: str = IT_REPO, status: str = "active") -> TaskConnection:
    return TaskConnection(
        tenant_id=uuid.uuid4(),
        version=1,
        connector="github_issues",
        destination={"connector": "github_issues", "repository": repo},
        credential_ref=ref,
        status=status,
        reason="test",
    )


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(connections.SECRET_PREFIX + "IT_GITHUB", "it-secret")
    monkeypatch.setenv(connections.SECRET_PREFIX + "FURNITURE_GITHUB", "furniture-secret")


@pytest.mark.parametrize("env", ["staging", "production"])
@pytest.mark.parametrize("ref", [None, ""], ids=["no reference", "a blank reference"])
def test_a_deployed_connection_without_a_registered_credential_gets_no_token(
    env: str, ref: str | None
) -> None:
    deployed = settings(env)
    assert connections.credential(deployed, row(ref), "github_issues", "demo-it") is None
    assert connections.credential(deployed, None, "github_issues", "demo-it") is None
    view_source = connections.credential_source(deployed, row(ref))
    assert "registered" in view_source and "deployment" not in view_source
    # Development keeps the single-token fallback, and says so.
    dev = settings()
    assert connections.credential(dev, row(ref), "github_issues", "demo-it") == "deployment-token"
    assert "development" in connections.credential_source(dev, row(ref))


def test_each_tenant_reads_only_its_own_registered_secret() -> None:
    for env in ("development", "staging"):
        s = settings(env)
        assert (
            connections.credential(s, row("it-github"), "github_issues", "demo-it") == "it-secret"
        )
        assert connections.credential(s, row("it-github", SHARED), "github_issues", "demo-it") == (
            "it-secret"
        )
        # Another tenant's reference, or its own for a repository it was not registered for.
        assert (
            connections.credential(s, row("furniture-github"), "github_issues", "demo-it") is None
        )
        assert (
            connections.credential(s, row("it-github", FURN_REPO), "github_issues", "demo-it")
            is None
        )


def test_a_superseded_version_recovers_its_write_and_a_revoked_one_does_not() -> None:
    s = settings("staging")
    superseded = row("it-github", status="superseded")
    assert connections.credential(s, superseded, "github_issues", "demo-it") == "it-secret"
    revoked = row("it-github", status="revoked")
    assert connections.credential(s, revoked, "github_issues", "demo-it") is None


def test_two_references_for_one_secret_are_refused() -> None:
    grant = {"tenant": "demo-it", "connector": "github_issues", "repositories": [IT_REPO]}
    other = grant | {"tenant": "demo-furniture"}
    with pytest.raises(ValidationError, match="both read OPSBRIDGE_CONNECTION_SECRET_SHARED_TOKEN"):
        Settings(
            _env_file=None, connection_credentials={"shared-token": grant, "shared_token": other}
        )  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="lower-case letters"):
        Settings(_env_file=None, connection_credentials={"Shared-Token": grant})  # type: ignore[call-arg]
    assert connections.secret_name("it-github") == "OPSBRIDGE_CONNECTION_SECRET_IT_GITHUB"


def test_a_tenant_is_shown_the_repositories_it_can_send_to() -> None:
    staging = settings("staging")
    assert connections.tenant_repositories(staging, "demo-it") == [IT_REPO, SHARED]
    assert connections.tenant_repositories(staging, "demo-furniture") == [FURN_REPO, SHARED]
    assert connections.tenant_repositories(staging, "demo-other") == []
    # A registered repository the deployment no longer allows is not offered.
    narrowed = settings("staging", github_repository_allowlist=[SHARED])
    assert connections.tenant_repositories(narrowed, "demo-it") == [SHARED]
    # In development the deployment token serves the whole allowlist.
    assert connections.tenant_repositories(settings(), "demo-other") == sorted(
        [IT_REPO, FURN_REPO, SHARED, "acme/unregistered"]
    )
