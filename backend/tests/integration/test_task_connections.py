"""Tenant task connections: versioned destinations and credentials (review Package 11).

Before: the GitHub repository and token were deployment-wide settings and the connector was a
tenant setting, so every GitHub tenant wrote to one repository; dispatch built its connector
from the configuration at the moment it ran, so recovering a write that had already landed
looked wherever the configuration pointed by then (a probe left a task that existed in the
approved repository as `outcome_unknown` for review once the repository setting changed);
and there was no way to stop a tenant's external writes or to see who changed where they go.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select

from opsbridge.config import ConnectionCredential
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import AuditEvent
from opsbridge.services import connections
from opsbridge.services.investigations import Conflict
from opsbridge.workflow.worker import build_execution_deps

from ..contracts.fake_github import FakeGitHub
from .servers import serve
from .stack import get
from .test_api_cases import call
from .test_approvals_execution import (
    IT,
    SANDBOX_REPO,
    CrashingConnector,
    SimulatedCrash,
    _approve_it,
    _it_proposal,
    _tenant_of,
    approve,
    drain,
    expire_attempts_and_leases,
    it_admin,
    prepared,
)

pytestmark = pytest.mark.integration
T = "/api/v1/tenants/demo-furniture"
ELSEWHERE = "opsbridge-sandbox/elsewhere"
# The deployment registers the IT tenant's credential for the repositories it may serve.
REGISTERED = {
    "it-github": ConnectionCredential(
        tenant="demo-it", connector="github_issues", repositories=[SANDBOX_REPO, ELSEWHERE]
    )
}


@pytest.fixture
def github(stack, worker_db: Database, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """The fake GitHub behind the real dispatch wiring; the IT tenant may use two repositories,
    and its first GitHub connection's credential comes from the secret store."""
    fake = FakeGitHub(repo=SANDBOX_REPO, token="token-from-the-store")
    monkeypatch.setenv(connections.SECRET_PREFIX + "IT_GITHUB", "token-from-the-store")
    original = stack["exec"]["exec"]
    with serve(fake.app(), health_path="/docs") as base:
        settings = stack["settings"].model_copy(
            update={
                "github_api_url": base,
                "github_repository_allowlist": [SANDBOX_REPO, ELSEWHERE],
                "github_live_writes_authorized": True,
                "github_token": SecretStr("deployment-default-token"),
                "connection_credentials": REGISTERED,
            }
        )
        stack["exec"]["exec"] = replace(
            build_execution_deps(settings, worker_db), timeout_seconds=1.0
        )
        try:
            yield {"fake": fake, "settings": settings, "st": stack}
        finally:
            stack["exec"]["exec"] = original


def connect_it(
    g: dict[str, Any], app_db: Database, migrator_engine: Engine, repo: str, ref: str | None
) -> dict[str, Any]:
    return connections.change(
        app_db, it_admin(g["st"], migrator_engine), g["settings"], connector="github_issues",
        repository=repo, credential_ref=ref, reason=f"to {repo}",
    )  # fmt: skip


def crash_after_the_create(g: dict[str, Any]) -> None:
    """The next dispatch creates the issue, then the worker dies before recording it."""
    deps = g["st"]["exec"]["exec"]
    real, calls = deps.connector_for, []

    def connector_for(tid: Any, name: str, op: dict[str, Any]) -> Any:
        calls.append(1)
        conn = real(tid, name, op)
        return CrashingConnector(conn, "after") if len(calls) == 1 else conn

    g["st"]["exec"]["exec"] = replace(deps, connector_for=connector_for)


def it_operation(st: dict[str, Any], case_id: str) -> dict[str, Any]:
    [op] = get(st, "it-approver", f"{IT}/cases/{case_id}/operations").json()["items"]
    return dict(op)


def test_recovery_after_the_connection_moved_looks_where_the_write_went(
    github, app_db: Database, worker_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    g, st, fake = github, github["st"], github["fake"]
    connect_it(g, app_db, migrator_engine, SANDBOX_REPO, "it-github")
    case_id, prop = _it_proposal(st)
    assert prop["destination"] == {"connector": "github_issues", "repository": SANDBOX_REPO}
    assert _approve_it(st, case_id, prop).status_code == 200
    crash_after_the_create(g)
    with pytest.raises(SimulatedCrash):
        st["worker"].run_once()
    assert len(fake.issues) == 1

    connect_it(g, app_db, migrator_engine, ELSEWHERE, None)  # the connection moves on
    expire_attempts_and_leases(worker_db)
    drain(st)
    op = it_operation(st, case_id)
    assert (op["status"], op["confirmation"]["method"]) == ("confirmed", "reconciled_by_marker")
    assert len(fake.issues) == 1  # found where it went, with that version's credential
    assert {r["headers"]["authorization"] for r in fake.requests} == {"Bearer token-from-the-store"}


def test_a_revoked_credential_is_not_used_even_to_recover_a_write(
    github, app_db: Database, worker_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    g, st, fake = github, github["st"], github["fake"]
    connect_it(g, app_db, migrator_engine, SANDBOX_REPO, "it-github")
    case_id, prop = _it_proposal(st)
    assert _approve_it(st, case_id, prop).status_code == 200
    crash_after_the_create(g)
    with pytest.raises(SimulatedCrash):
        st["worker"].run_once()
    connections.revoke(app_db, it_admin(st, migrator_engine), g["settings"], reason="token leaked")
    requests_before = len(fake.requests)
    expire_attempts_and_leases(worker_db)
    drain(st)
    op = it_operation(st, case_id)
    assert (op["status"], op["failure_category"], op["review_required"]) == (
        "outcome_unknown",
        "connector_unavailable",
        True,
    )
    assert len(fake.requests) == requests_before and len(fake.issues) == 1


def test_a_revoked_connection_sends_nothing_until_a_new_one_is_made(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    adm = st["actors"]["furn-admin"]
    case_id, prop = prepared(st, "INV-9001")
    assert approve(st, case_id, prop).status_code == 200
    r = call(st["client"], adm, "POST", f"{T}/task-connection/revoke", json={"reason": "audit"})
    assert r.status_code == 200 and r.json()["status"] == "revoked", r.text
    drain(st)
    [op] = get(st, "furn-approver", f"{T}/cases/{case_id}/operations").json()["items"]
    assert (op["status"], op["failure_category"]) == (
        "cancelled_before_dispatch",
        "precondition_failed",
    )
    assert op["attempts"] == []
    who = st["actors"]["furn-investigator"]
    blocked = call(st["client"], who, "POST", f"{T}/cases/{case_id}/proposals", json={})
    assert blocked.status_code == 409, blocked.text
    health = get(st, "furn-admin", f"{T}/connectors/health").json()
    assert health["connector"] == "none" and health["connection"] is None

    sim = {"connector": "sim_tasks", "reason": "audit finished"}
    r = call(st["client"], adm, "POST", f"{T}/task-connection", json=sim)
    assert r.status_code == 201 and r.json()["version"] == 2, r.text
    again = call(st["client"], who, "POST", f"{T}/cases/{case_id}/proposals", json={})
    assert again.status_code == 201, again.text


def test_connection_changes_are_validated_versioned_and_audited(
    stack, app_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    adm = st["actors"]["furn-admin"]
    view = get(st, "furn-admin", f"{T}/task-connection").json()
    assert view["active"]["version"] == 1 and view["active"]["destination"] == {
        "connector": "sim_tasks",
        "organization": "demo-furniture",
    }
    sim = {"connector": "sim_tasks", "reason": "again"}
    r = call(st["client"], adm, "POST", f"{T}/task-connection", json=sim)
    assert r.status_code == 409 and r.json()["error"]["code"] == "task_connection_unchanged"
    who = st["actors"]["furn-investigator"]
    assert call(st["client"], who, "POST", f"{T}/task-connection", json=sim).status_code == 403
    assert get(st, "furn-auditor", f"{T}/task-connection").status_code == 200

    admin, settings = (
        it_admin(st, migrator_engine),
        st["settings"].model_copy(
            update={
                "github_repository_allowlist": [SANDBOX_REPO],
                "connection_credentials": REGISTERED,
            }
        ),
    )
    for repository, ref, code in (
        ("someone/else", None, "repository_not_allowed"),
        ("not a repository", None, "invalid_task_connection"),
        (SANDBOX_REPO, "Has Spaces", "invalid_task_connection"),
    ):
        with pytest.raises(Conflict) as exc:
            connections.change(
                app_db, admin, settings, connector="github_issues", repository=repository,
                credential_ref=ref, reason="try",
            )  # fmt: skip
        assert exc.value.code == code, repository
    made = connections.change(
        app_db, admin, settings, connector="github_issues", repository=SANDBOX_REPO.upper(),
        credential_ref="it-github", reason="tasks go to GitHub",
    )  # fmt: skip
    assert made["version"] == 2 and made["destination"]["repository"] == SANDBOX_REPO
    assert made["credential_source"] == "secret store reference 'it-github'"
    assert made["credential_configured"] is False  # the secret store has no such secret here
    tid = _tenant_of(migrator_engine, "demo-it")
    with app_db.session(DbContext(tenant_id=tid)) as s:
        rows = s.execute(
            select(AuditEvent.action, AuditEvent.details).where(
                AuditEvent.action == "task_connection.changed"
            )
        ).all()
    assert [(a, d["version"], d["previous_version"]) for a, d in rows] == [
        ("task_connection.changed", 2, 1)
    ]
    first = {"connector": "sim_tasks", "organization": "demo-it"}
    with app_db.session(DbContext(tenant_id=tid)) as s:
        before = connections.for_destination(s, tid, first)
        assert before is not None
        assert (before.status, before.end_reason) == ("superseded", "tasks go to GitHub")
