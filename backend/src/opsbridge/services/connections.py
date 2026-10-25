"""Tenant task connections: where follow-up tasks go, and with which credential (0017).

A connection version is immutable except for ending it. Its `destination` (connector plus the
simulator organization or the GitHub repository) is what an approval binds, so pointing the
connection elsewhere makes pending approvals stale instead of redirecting them, and dispatch
recovers an earlier write at the approved destination. `credential_ref` names a secret in the
deployment's secret store, read here from `OPSBRIDGE_CONNECTION_SECRET_<REF>` (upper case,
`-` as `_`); the secret is never stored or shown. A GitHub connection without a reference uses
the deployment's `OPSBRIDGE_GITHUB_TOKEN`. Repositories must be on the deployment's allowlist,
and live GitHub writes stay off unless the deployment authorizes them.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from opsbridge.audit import events as audit
from opsbridge.auth.permissions import TIERS, Principal, require
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import TaskConnection, Tenant, TenantSettings, User

from .investigations import Conflict

REF_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
SECRET_PREFIX = "OPSBRIDGE_CONNECTION_SECRET_"


def _ctx(p: Principal) -> DbContext:
    return DbContext(tenant_id=p.tenant_id, user_id=p.user_id)


def active(s: Session, tenant_id: uuid.UUID) -> TaskConnection | None:
    return s.execute(
        select(TaskConnection).where(
            TaskConnection.tenant_id == tenant_id, TaskConnection.status == "active"
        )
    ).scalar_one_or_none()


def usable(s: Session, tenant_id: uuid.UUID) -> TaskConnection:
    """The active connection, if follow-up tasks can go to it now (Conflict otherwise)."""
    conn = active(s, tenant_id)
    if conn is None:
        raise Conflict(
            "no_task_connection", "The task connection was revoked; follow-up tasks are not sent."
        )
    tenant = s.get(Tenant, tenant_id)
    if tenant is None or conn.connector not in TIERS[tenant.tier].external_connectors:
        raise Conflict(
            "connector_not_entitled",
            f"Task connector {conn.connector!r} is not enabled for this tenant's tier.",
        )
    if conn.connector == "github_issues" and not conn.destination.get("repository"):
        raise Conflict(
            "task_connection_incomplete",
            "The GitHub task connection names no repository; set it in tenant settings.",
        )
    return conn


def for_destination(
    s: Session, tenant_id: uuid.UUID, destination: dict[str, Any]
) -> TaskConnection | None:
    """The newest version that wrote to `destination`: its credential recovers an approved
    write there, even after the connection moved elsewhere (unless that version was revoked)."""
    rows = s.execute(
        select(TaskConnection)
        .where(TaskConnection.tenant_id == tenant_id)
        .order_by(TaskConnection.version.desc())
    ).scalars()
    return next((c for c in rows if c.destination == destination), None)


def credential(settings: Any, conn: TaskConnection | None, connector: str) -> str | None:
    """The secret a connection version uses, or None when it is not configured or the version
    was revoked (a revoked credential is not used again, not even to recover a write). The
    simulator's credential is derived per organization; GitHub without a reference (or with
    no connection, for operations approved before 0017) uses the deployment token."""
    if connector == "sim_tasks":
        return None
    if conn is not None and conn.status == "revoked":
        return None
    if conn is not None and conn.credential_ref:
        return os.environ.get(SECRET_PREFIX + conn.credential_ref.upper().replace("-", "_"))
    token = getattr(settings, "github_token", None)
    return token.get_secret_value() if token else None


def allowed_repositories(settings: Any) -> frozenset[str]:
    names = list(getattr(settings, "github_repository_allowlist", []) or [])
    if getattr(settings, "github_sandbox_repo", None):
        names.append(settings.github_sandbox_repo)
    return frozenset(n.lower() for n in names)


def view(s: Session, conn: TaskConnection, settings: Any) -> dict[str, Any]:
    def who(uid: uuid.UUID | None) -> str | None:
        user = s.get(User, uid) if uid else None
        return user.username if user else None

    if conn.connector == "sim_tasks":
        source = "derived from the simulator key for this organization"
    elif conn.credential_ref:
        source = f"secret store reference {conn.credential_ref!r}"
    else:
        source = "the deployment's default GitHub token"
    return {
        "id": str(conn.id),
        "version": conn.version,
        "connector": conn.connector,
        "destination": dict(conn.destination),
        "credential_ref": conn.credential_ref,
        "credential_source": source,
        "credential_configured": conn.connector == "sim_tasks"
        or credential(settings, conn, conn.connector) is not None,
        "status": conn.status,
        "reason": conn.reason,
        "created_at": conn.created_at.isoformat(),
        "created_by": who(conn.created_by),
        "ended_at": conn.ended_at.isoformat() if conn.ended_at else None,
        "ended_by": who(conn.ended_by),
        "end_reason": conn.end_reason,
    }


def history(db: Database, p: Principal, settings: Any) -> dict[str, Any]:
    require(p, "tenant.read_settings")
    with db.session(_ctx(p)) as s:
        rows = s.execute(
            select(TaskConnection)
            .where(TaskConnection.tenant_id == p.tenant_id)
            .order_by(TaskConnection.version.desc())
        ).scalars()
        versions = [view(s, c, settings) for c in rows]
    return {
        "active": next((v for v in versions if v["status"] == "active"), None),
        "versions": versions,
        "allowed_repositories": sorted(allowed_repositories(settings)),
    }


def _reason(reason: str) -> str:
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise Conflict("reason_required", "Give a reason (up to 500 characters).")
    return reason


def change(
    db: Database,
    p: Principal,
    settings: Any,
    *,
    connector: str,
    repository: str | None,
    credential_ref: str | None,
    reason: str,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Make a new active version; the previous one is superseded (it stays the record of
    where earlier approvals went)."""
    require(p, "tenant.manage_connectors")
    reason = _reason(reason)
    with db.session(_ctx(p)) as s:
        tenant = s.get(Tenant, p.tenant_id)
        assert tenant is not None
        if connector not in TIERS[tenant.tier].external_connectors:
            raise Conflict("not_entitled", f"Connector {connector!r} is not enabled for this tier.")
        if connector == "sim_tasks":
            if repository or credential_ref:
                raise Conflict(
                    "invalid_task_connection",
                    "The simulator connection takes no repository or credential reference.",
                )
            destination: dict[str, Any] = {"connector": connector, "organization": tenant.slug}
        elif connector == "github_issues":
            repo = (repository or "").strip().lower()
            if not REPO_RE.match(repo):
                raise Conflict("invalid_task_connection", "Name the repository as owner/name.")
            if repo not in allowed_repositories(settings):
                raise Conflict(
                    "repository_not_allowed",
                    f"Repository {repo!r} is not on this deployment's allowlist.",
                    {"allowed_repositories": sorted(allowed_repositories(settings))},
                )
            if credential_ref is not None and not REF_RE.match(credential_ref):
                raise Conflict(
                    "invalid_task_connection",
                    "A credential reference is lower-case letters, digits, '-' and '_'.",
                )
            destination = {"connector": connector, "repository": repo}
        else:
            raise Conflict("invalid_task_connection", f"Unknown task connector {connector!r}.")
        # One change at a time per tenant: the version number and the active row move together.
        s.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"task-connection:{p.tenant_id}"},
        )
        previous = active(s, p.tenant_id)
        if (
            previous is not None
            and previous.destination == destination
            and previous.credential_ref == credential_ref
        ):
            raise Conflict("task_connection_unchanged", "That is the current task connection.")
        now = datetime.now(UTC)
        if previous is not None:
            previous.status, previous.ended_at, previous.ended_by = "superseded", now, p.user_id
            previous.end_reason = reason
            s.flush()  # one active row per tenant (partial unique index)
        version = (
            s.execute(
                select(func.coalesce(func.max(TaskConnection.version), 0)).where(
                    TaskConnection.tenant_id == p.tenant_id
                )
            ).scalar_one()
            + 1
        )
        conn = TaskConnection(
            tenant_id=p.tenant_id,
            version=version,
            connector=connector,
            destination=destination,
            credential_ref=credential_ref,
            status="active",
            reason=reason,
            created_by=p.user_id,
        )
        s.add(conn)
        ts = s.get(TenantSettings, p.tenant_id)
        if ts is not None:
            ts.task_connector = connector  # kept in step so a downgrade below 0017 still works
        s.flush()
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="task_connection.changed",
            resource_type="task_connection",
            resource_id=str(conn.id),
            correlation_id=correlation_id,
            details={
                "version": version,
                "previous_version": previous.version if previous else None,
                "destination": destination,
                "credential_ref": credential_ref,
                "reason": reason,
            },
        )
        s.refresh(conn)
        return view(s, conn, settings)


def revoke(
    db: Database, p: Principal, settings: Any, *, reason: str, correlation_id: str | None = None
) -> dict[str, Any]:
    """Stop sending follow-up tasks: no new write goes anywhere until a new version is made.
    Writes that may already have landed are still recovered with the approved version."""
    require(p, "tenant.manage_connectors")
    reason = _reason(reason)
    with db.session(_ctx(p)) as s:
        s.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"task-connection:{p.tenant_id}"},
        )
        conn = active(s, p.tenant_id)
        if conn is None:
            raise Conflict("no_task_connection", "There is no active task connection to revoke.")
        conn.status, conn.ended_at, conn.ended_by = "revoked", datetime.now(UTC), p.user_id
        conn.end_reason = reason
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="task_connection.revoked",
            resource_type="task_connection",
            resource_id=str(conn.id),
            correlation_id=correlation_id,
            details={"version": conn.version, "reason": reason},
        )
        s.flush()
        return view(s, conn, settings)
