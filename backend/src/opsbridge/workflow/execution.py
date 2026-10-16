"""Dispatch of approved operations with an attempt ledger and reconciliation.

Algorithm for one `dispatch_operation` job (delivery is at-least-once):

1. Terminal operations are left alone (duplicate delivery is a no-op).
2. A pending operation with cancellation requested → cancelled_before_dispatch.
3. Pending → re-validate: proposal still approved and current, approval unexpired,
   approver still authorized, evidence and policy unchanged. Failure → cancelled.
4. Inspect prior attempts. An attempt still `started` within the lease window is
   in flight elsewhere → back off. An abandoned `started` attempt or an
   `ambiguous_failure` → RECONCILE before anything else.
5. Reconciliation by connector capability:
   - strong lookup: exactly one marker match → confirmed; none → safe to create;
     more than one → outcome_unknown (duplicates exist; human review);
   - idempotency keys (no lookup): re-sending with the same key is safe;
   - otherwise (or lookup failure / eventual consistency with no match):
     outcome_unknown + review_required. Never a blind re-create.
6. Commit a `started` attempt BEFORE the remote call; call outside any DB transaction;
   record the outcome; read back the created task before marking confirmed.
Every state change re-checks the job's fencing token so a stale worker cannot overwrite
the current lease holder's results.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from opsbridge.audit import events as audit
from opsbridge.canonical import canonical_json, digest
from opsbridge.connectors.tasks import (
    AmbiguousFailure,
    DefiniteFailure,
    LookupUnavailable,
    RemoteTask,
    TaskConnector,
    TaskRequest,
)
from opsbridge.evidence.storage import ArtifactStore
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import (
    ApprovalDecision,
    Artifact,
    Attempt,
    Case,
    ConversationMessage,
    Operation,
    OutboxEvent,
    Proposal,
)
from opsbridge.privacy.redaction import redact
from opsbridge.services.cases import transition
from opsbridge.telemetry import metrics, tracing

from . import jobs
from .investigation import StepFailed

EXECUTOR = "service:opsbridge-worker"
TERMINAL = frozenset({"confirmed", "failed", "outcome_unknown", "cancelled_before_dispatch"})


class LeaseLost(Exception):
    """This worker no longer holds the job; it must stop without writing results."""


@dataclass
class ExecutionDeps:
    db: Database
    # (tenant_id, connector name, operation context) → connector bound to that operation
    connector_for: Callable[[uuid.UUID, str, dict[str, Any]], TaskConnector]
    timeout_seconds: float
    lease_seconds: int
    # Where attempt requests/responses are kept (content-addressed, tenant-scoped); without
    # one only digests are recorded.
    store: ArtifactStore | None = None


def _ctx(tenant_id: uuid.UUID) -> DbContext:
    return DbContext(tenant_id=tenant_id)


def _now() -> datetime:
    return datetime.now(UTC)


def marker_for(op: Operation) -> str:
    return f"opsbridge-op:{op.operation_key}"


def _guard(s: Session, job: jobs.ClaimedJob) -> None:
    if not jobs.holds_lease(s, job):
        raise LeaseLost(f"job {job.id} lease lost (token {job.fencing_token})")


def dispatch(deps: ExecutionDeps, job: jobs.ClaimedJob) -> None:
    op_id = uuid.UUID(job.payload["operation_id"])
    tid = job.tenant_id
    with deps.db.session(_ctx(tid)) as s:
        _guard(s, job)
        op = s.execute(
            select(Operation).where(Operation.id == op_id).with_for_update()
        ).scalar_one()
        if op.status in TERMINAL:
            return
        if op.status == "pending_dispatch":
            if op.cancellation == "requested":
                _finish(
                    s,
                    op,
                    "cancelled_before_dispatch",
                    category="cancelled_by_user",
                    cancellation="confirmed",
                )
                return
            problems = _revalidate(s, op)
            if problems:
                _finish(
                    s,
                    op,
                    "cancelled_before_dispatch",
                    category="precondition_failed",
                    detail="; ".join(problems),
                )
                return
        attempts = list(
            s.execute(
                select(Attempt).where(Attempt.operation_id == op.id).order_by(Attempt.started_at)
            ).scalars()
        )
        in_flight = [
            a
            for a in attempts
            if a.status == "started"
            and a.started_at > _now() - timedelta(seconds=deps.lease_seconds)
            and a.fencing_token != job.fencing_token
        ]
        if in_flight:
            raise StepFailed("another attempt is in flight; backing off", retryable=True)
        needs_reconcile = any(a.status in ("started", "ambiguous_failure") for a in attempts)
        succeeded = [
            a for a in attempts if a.status == "succeeded" and a.kind == "dispatch" and a.remote_ref
        ]
        op.status = "executing"
        op.executed_by = EXECUTOR
        op.row_version += 1
        snapshot = _snapshot(op, attempts)
    if snapshot["action_type"] != "create_follow_up_task":
        _execute_local(deps, job, snapshot)
        return
    connector = deps.connector_for(tid, snapshot["connector"], snapshot)
    if succeeded:  # created earlier; only read-back was missing
        _read_back(
            deps, job, snapshot, connector, succeeded[-1].remote_ref or "", parent=succeeded[-1].id
        )
        return
    if needs_reconcile:
        outcome = _reconcile(deps, job, snapshot, connector)
        if outcome in ("confirmed", "unknown"):
            return
    _create(deps, job, snapshot, connector)


def _snapshot(op: Operation, attempts: list[Attempt]) -> dict[str, Any]:
    return {
        "operation_id": op.id,
        "tenant_id": op.tenant_id,
        "case_id": op.case_id,
        "operation_key": op.operation_key,
        "action_type": op.action_type,
        "connector": op.connector,
        "payload": op.payload,
        "marker": marker_for(op),
        "retry_index": len(attempts),
        "last_attempt_id": attempts[-1].id if attempts else None,
        "approved_by": op.approved_by,
    }


def _revalidate(s: Session, op: Operation) -> list[str]:
    from opsbridge.services.proposals import approver_still_authorized, check_current, payload_of

    problems = []
    pr = s.get(Proposal, op.proposal_id)
    ad = s.get(ApprovalDecision, op.approval_id)
    case = s.get(Case, op.case_id)
    if pr is None or ad is None or case is None:
        return ["proposal, approval or case missing"]
    if pr.status != "approved":
        problems.append(f"proposal is {pr.status}")
    if (
        ad.decision != "approved"
        or ad.payload_digest != pr.payload_digest
        or op.payload_digest != pr.payload_digest
    ):
        problems.append("approval is not bound to this payload")
    if digest(payload_of(pr)) != op.payload_digest:
        problems.append("operation payload does not match the approved digest")
    if ad.expires_at <= _now():
        problems.append("approval expired before dispatch")
    if not approver_still_authorized(s, ad.approver_id, op.action_type):
        problems.append("approver no longer holds approval authority for this action")
    stale = check_current(s, case, pr)
    problems += stale.reasons
    return problems


_AUDIT_OUTCOME = {
    "confirmed": "ok",
    "outcome_unknown": "unknown",
    "cancelled_before_dispatch": "cancelled",
}


def _finish(
    s: Session,
    op: Operation,
    status: str,
    *,
    category: str | None = None,
    detail: str | None = None,
    remote_ref: str | None = None,
    confirmation: dict[str, Any] | None = None,
    cancellation: str | None = None,
    review: bool = False,
) -> None:
    if status in TERMINAL:
        elapsed = (_now() - op.created_at).total_seconds()
        connector = op.connector

        def _record() -> None:
            metrics.OPERATIONS_FINISHED.labels(status, connector).inc()
            metrics.EXECUTION_DURATION.labels(status).observe(max(elapsed, 0.0))

        metrics.after_commit(s, _record)
        tracing.set_ids(operation_id=op.id, remote_ref=remote_ref or op.remote_ref)
        trace.get_current_span().set_attribute("opsbridge.operation.status", status)
    op.status = status
    op.failure_category = category if status != "confirmed" else None
    op.remote_ref = remote_ref or op.remote_ref
    op.confirmation = confirmation or op.confirmation
    op.review_required = review
    if cancellation:
        op.cancellation = cancellation
    elif op.cancellation == "requested" and status == "confirmed":
        op.failure_category = "cancellation_too_late"
    op.row_version += 1
    case = s.execute(select(Case).where(Case.id == op.case_id).with_for_update()).scalar_one()
    if (
        status == "confirmed"
        and op.action_type != "record_review_note"
        and case.status == "awaiting_review"
    ):
        transition(case, "follow_up_pending")  # follow-up done; the business exception stays open
    if status == "cancelled_before_dispatch" and category == "precondition_failed":
        pr = s.get(Proposal, op.proposal_id)
        if pr is not None and pr.status == "approved":
            pr.status = "superseded"
            pr.status_changed_at = _now()
    if status == "confirmed":
        s.execute(
            text(
                "UPDATE outbox_events SET delivered_at = now() WHERE aggregate_id = :id "
                "AND delivered_at IS NULL"
            ),
            {"id": op.id},
        )
    audit.record(
        s,
        tenant_id=op.tenant_id,
        actor_kind="service",
        actor_id=EXECUTOR,
        on_behalf_of=op.approved_by,
        action=f"operation.{status}",
        resource_type="operation",
        resource_id=str(op.id),
        outcome=_AUDIT_OUTCOME.get(status, status),
        details={"category": category, "detail": (detail or "")[:500], "remote_ref": remote_ref},
    )


def _keep(
    deps: ExecutionDeps, s: Session, tid: uuid.UUID, kind: str, payload: Any
) -> uuid.UUID | None:
    """Store a canonical-JSON copy of an attempt's request or response; its digest equals the
    digest recorded on the attempt. Retention (`tool_artifacts_days`) applies to it."""
    if deps.store is None:
        return None
    data = canonical_json(payload).encode("utf-8")
    key, sha = deps.store.put(tid, data)
    art = Artifact(
        tenant_id=tid,
        kind=kind,
        sha256=sha,
        size_bytes=len(data),
        content_type="application/json",
        storage_key=key,
        classification="internal",
    )
    s.add(art)
    s.flush()
    return art.id


def _new_attempt(
    deps: ExecutionDeps,
    job: jobs.ClaimedJob,
    snap: dict[str, Any],
    kind: str,
    tool: str,
    args: dict[str, Any],
    connector_version: str,
) -> uuid.UUID:
    with deps.db.session(_ctx(snap["tenant_id"])) as s:
        _guard(s, job)
        retry_index = s.execute(
            select(Attempt).where(Attempt.operation_id == snap["operation_id"])
        ).all()
        parent = s.execute(
            select(Attempt.id)
            .where(Attempt.operation_id == snap["operation_id"])
            .order_by(Attempt.started_at.desc())
            .limit(1)
        ).scalar()
        a = Attempt(
            tenant_id=snap["tenant_id"],
            case_id=snap["case_id"],
            run_id=None,
            step_id="dispatch_operation",
            operation_id=snap["operation_id"],
            parent_attempt_id=parent,
            retry_index=len(retry_index),
            kind=kind,
            tool_name=tool,
            connector_version=connector_version,
            operation_key=snap["operation_key"],
            status="started",
            input_digest=digest(args),
            input_artifact_id=_keep(deps, s, snap["tenant_id"], "tool_request", args),
            response_mode="live",
            worker_id=job.lease_owner,
            fencing_token=job.fencing_token,
        )
        s.add(a)
        s.flush()
        attempt_id = a.id
    # Child of the job span; ends in _close_attempt with the outcome.
    sp = tracing.tracer.start_span(f"connector {tool}")
    tracing.set_ids(
        sp,
        tenant_id=snap["tenant_id"],
        case_id=snap["case_id"],
        operation_id=snap["operation_id"],
        attempt_id=attempt_id,
    )
    sp.set_attribute("opsbridge.connector.phase", kind)
    sp.set_attribute("opsbridge.connector.version", connector_version)
    _OPEN[attempt_id] = (sp, time.monotonic(), kind)
    return attempt_id


# attempt id -> (span, start, phase) for attempts opened by this worker process
_OPEN: dict[uuid.UUID, tuple[Span, float, str]] = {}


def _close_attempt(
    deps: ExecutionDeps,
    tid: uuid.UUID,
    attempt_id: uuid.UUID,
    status: str,
    *,
    output: Any = None,
    category: str | None = None,
    detail: str | None = None,
    remote_ref: str | None = None,
) -> None:
    # Attempt outcomes are facts and are always recorded, even by a worker whose lease expired.
    with deps.db.session(_ctx(tid)) as s:
        a = s.get(Attempt, attempt_id)
        assert a is not None
        a.status = status
        a.output_digest = digest(output) if output is not None else None
        if output is not None:
            a.output_artifact_id = _keep(deps, s, tid, "tool_response", output)
        a.error_category = category
        a.error_detail = (detail or "")[:1000] or None
        a.remote_ref = remote_ref
        a.finished_at = _now()
    opened = _OPEN.pop(attempt_id, None)
    if opened is not None:
        sp, started, phase = opened
        sp.set_attribute("opsbridge.attempt.status", status)
        if category:
            sp.set_attribute("opsbridge.attempt.category", category)
        tracing.set_ids(sp, remote_ref=remote_ref)
        if status != "succeeded":
            sp.set_status(Status(StatusCode.ERROR, category or status))
        sp.end()
        metrics.CONNECTOR_ATTEMPTS.labels(phase, status).inc()
        metrics.CONNECTOR_DURATION.labels(phase).observe(time.monotonic() - started)


def _complete(
    deps: ExecutionDeps, job: jobs.ClaimedJob, snap: dict[str, Any], status: str, **kw: Any
) -> None:
    with deps.db.session(_ctx(snap["tenant_id"])) as s:
        _guard(s, job)
        op = s.execute(
            select(Operation).where(Operation.id == snap["operation_id"]).with_for_update()
        ).scalar_one()
        _finish(s, op, status, **kw)


def _task_request(snap: dict[str, Any]) -> TaskRequest:
    params = snap["payload"]["parameters"]
    # Only synthetic/redacted content leaves OpsBridge; the marker enables reconciliation.
    body = redact(params.get("body") or "").text
    body = f"{body}\n\nOpsBridge operation marker: {snap['marker']}"
    return TaskRequest(
        title=redact(params["title"]).text,
        body=body,
        assignee_group=params.get("assignee_group"),
        marker=snap["marker"],
    )


def _remote_dict(t: RemoteTask) -> dict[str, Any]:
    return {"remote_ref": t.remote_ref, "url": t.url, "marker": t.marker, "title": t.title}


def _create(
    deps: ExecutionDeps, job: jobs.ClaimedJob, snap: dict[str, Any], connector: TaskConnector
) -> None:
    req = _task_request(snap)
    key = snap["operation_key"] if connector.capabilities.idempotency_keys else None
    args = {
        "title": req.title,
        "assignee_group": req.assignee_group,
        "marker": req.marker,
        "idempotency_key": key,
    }
    attempt_id = _new_attempt(
        deps, job, snap, "dispatch", "create_follow_up_task", args, connector.version
    )
    try:
        task = connector.create(req, key, deps.timeout_seconds)
    except DefiniteFailure as exc:
        _close_attempt(
            deps,
            snap["tenant_id"],
            attempt_id,
            "definite_failure",
            category="definite",
            detail=str(exc),
        )
        if exc.retryable:
            raise StepFailed(f"definite retryable failure: {exc}", retryable=True) from exc
        _complete(deps, job, snap, "failed", category="rejected_by_provider", detail=str(exc))
        return
    except AmbiguousFailure as exc:
        _close_attempt(
            deps,
            snap["tenant_id"],
            attempt_id,
            "ambiguous_failure",
            category="ambiguous",
            detail=str(exc),
        )
        outcome = _reconcile(deps, job, snap, connector)
        if outcome == "absent":
            # Strong lookup proved the earlier write did not happen; retry later via backoff.
            raise StepFailed(
                "ambiguous failure reconciled as absent; will retry", retryable=True
            ) from exc
        return
    _close_attempt(
        deps,
        snap["tenant_id"],
        attempt_id,
        "succeeded",
        output=_remote_dict(task),
        remote_ref=task.remote_ref,
    )
    _read_back(deps, job, snap, connector, task.remote_ref, parent=attempt_id)


def _read_back(
    deps: ExecutionDeps,
    job: jobs.ClaimedJob,
    snap: dict[str, Any],
    connector: TaskConnector,
    remote_ref: str,
    parent: uuid.UUID,
) -> None:
    attempt_id = _new_attempt(
        deps,
        job,
        snap,
        "readback",
        "get_follow_up_task",
        {"remote_ref": remote_ref},
        connector.version,
    )
    try:
        task = connector.get(remote_ref, deps.timeout_seconds)
    except LookupUnavailable as exc:
        _close_attempt(
            deps,
            snap["tenant_id"],
            attempt_id,
            "ambiguous_failure",
            category="readback_unavailable",
            detail=str(exc),
        )
        raise StepFailed(
            "created but read-back unavailable; will retry read-back", retryable=True
        ) from exc
    if task is None or task.marker != snap["marker"]:
        _close_attempt(
            deps,
            snap["tenant_id"],
            attempt_id,
            "definite_failure",
            category="readback_mismatch",
            detail="task missing or marker mismatch",
        )
        _complete(
            deps,
            job,
            snap,
            "outcome_unknown",
            category="readback_mismatch",
            remote_ref=remote_ref,
            review=True,
        )
        return
    _close_attempt(
        deps,
        snap["tenant_id"],
        attempt_id,
        "succeeded",
        output=_remote_dict(task),
        remote_ref=remote_ref,
    )
    _complete(
        deps,
        job,
        snap,
        "confirmed",
        remote_ref=remote_ref,
        confirmation={
            "method": "read_back",
            "marker": snap["marker"],
            "task": _remote_dict(task),
            "at": _now().isoformat(),
        },
    )


def _reconcile(
    deps: ExecutionDeps, job: jobs.ClaimedJob, snap: dict[str, Any], connector: TaskConnector
) -> str:
    """Returns 'confirmed', 'absent' (safe to create) or 'unknown' (review required)."""
    caps = connector.capabilities
    if caps.lookup != "none":
        attempt_id = _new_attempt(
            deps,
            job,
            snap,
            "reconcile",
            "reconcile_operation",
            {"marker": snap["marker"]},
            connector.version,
        )
        try:
            found = connector.find_by_marker(snap["marker"], deps.timeout_seconds)
        except LookupUnavailable as exc:
            _close_attempt(
                deps,
                snap["tenant_id"],
                attempt_id,
                "ambiguous_failure",
                category="lookup_unavailable",
                detail=str(exc),
            )
            found = None
        if found is not None:
            _close_attempt(
                deps,
                snap["tenant_id"],
                attempt_id,
                "succeeded",
                output=[_remote_dict(t) for t in found],
            )
            if len(found) == 1:
                _complete(
                    deps,
                    job,
                    snap,
                    "confirmed",
                    remote_ref=found[0].remote_ref,
                    confirmation={
                        "method": "reconciled_by_marker",
                        "marker": snap["marker"],
                        "task": _remote_dict(found[0]),
                        "at": _now().isoformat(),
                    },
                )
                return "confirmed"
            if len(found) > 1:
                _complete(
                    deps,
                    job,
                    snap,
                    "outcome_unknown",
                    category="duplicates_found",
                    detail=f"{len(found)} tasks carry this marker",
                    review=True,
                )
                return "unknown"
            if caps.lookup == "strong":
                return "absent"
    if caps.idempotency_keys:
        return "absent"  # re-sending with the same idempotency key cannot create a duplicate
    _complete(
        deps,
        job,
        snap,
        "outcome_unknown",
        category="unreconcilable",
        detail="no idempotency key support and no conclusive lookup; human review required",
        review=True,
    )
    return "unknown"


def _execute_local(deps: ExecutionDeps, job: jobs.ClaimedJob, snap: dict[str, Any]) -> None:
    """OpsBridge-internal actions: same ledger, no external side effect."""
    params = snap["payload"]["parameters"]
    attempt_id = _new_attempt(
        deps, job, snap, "dispatch", snap["action_type"], params, "opsbridge-local/1"
    )
    with deps.db.session(_ctx(snap["tenant_id"])) as s:
        _guard(s, job)
        case = s.execute(
            select(Case).where(Case.id == snap["case_id"]).with_for_update()
        ).scalar_one()
        ref = None
        if snap["action_type"] == "assign_case":
            case.assignee_group = params["assignee_group"]
            ref = f"case:{case.id}:assignee_group"
        else:
            seq = (
                s.execute(
                    text(
                        "SELECT coalesce(max(seq), 0) FROM conversation_messages WHERE case_id = :c"
                    ),
                    {"c": case.id},
                ).scalar_one()
            ) + 1
            kind = (
                "Supplier message DRAFT (not sent)"
                if snap["action_type"] == "create_supplier_message_draft"
                else "Review note"
            )
            body = f"{kind}: {params['title']}\n\n{params.get('body', '')}"
            msg = ConversationMessage(
                tenant_id=snap["tenant_id"],
                case_id=case.id,
                seq=seq,
                author_kind="system",
                author_id=None,
                body=body,
                body_sha256=digest({"body": body}),
            )
            s.add(msg)
            s.flush()
            ref = f"message:{msg.id}"
        s.add(
            OutboxEvent(
                tenant_id=snap["tenant_id"],
                event_type="operation.local_applied",
                aggregate_type="operation",
                aggregate_id=snap["operation_id"],
                payload={"ref": ref},
            )
        )
    _close_attempt(
        deps, snap["tenant_id"], attempt_id, "succeeded", output={"ref": ref}, remote_ref=ref
    )
    _complete(
        deps,
        job,
        snap,
        "confirmed",
        remote_ref=ref,
        confirmation={"method": "local_transaction", "ref": ref, "at": _now().isoformat()},
    )


def mark_unknown_after_exhaustion(db: Database, job: jobs.ClaimedJob, error: str) -> None:
    """Called when a dispatch job is dead: never leave an operation silently 'executing'."""
    op_id = uuid.UUID(job.payload["operation_id"])
    with db.session(_ctx(job.tenant_id)) as s:
        op = s.execute(
            select(Operation).where(Operation.id == op_id).with_for_update()
        ).scalar_one()
        if op.status in TERMINAL:
            return
        attempted = (
            s.execute(select(Attempt.id).where(Attempt.operation_id == op.id).limit(1)).first()
            is not None
        )
        if op.status == "pending_dispatch" and not attempted:
            _finish(s, op, "failed", category="dispatch_exhausted", detail=error)
        else:
            _finish(
                s, op, "outcome_unknown", category="dispatch_exhausted", detail=error, review=True
            )
