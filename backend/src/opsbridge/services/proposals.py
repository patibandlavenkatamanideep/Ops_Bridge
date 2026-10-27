"""Versioned action proposals, payload-bound approvals, operations and cancellation.

Invariants enforced here and re-checked at dispatch:
- A proposal version is immutable; edits create a new version and supersede the old one.
- An approval binds approver, tenant, proposal version, canonical payload digest, policy
  version, evidence/source manifest digest and an expiry. The approver must present the
  digest they reviewed.
- The payload includes the destination (connector and organization or repository, never
  credentials) and the exact text that will leave OpsBridge. Both are recomputed when
  approving and before every dispatch; any difference makes the proposal stale.
- Separation of duties: the proposal author cannot approve when the policy requires it.
- Approver authority = permission + action-type scope from server-side membership.
- Approval consumption, the logical operation, the outbox event and the dispatch job are
  written in ONE transaction.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from opsbridge.audit import events as audit
from opsbridge.auth.permissions import Principal, permissions_for, require
from opsbridge.canonical import CANONICALIZATION, digest
from opsbridge.domain.policy import ReconciliationPolicy
from opsbridge.evidence.records import case_identifiers, decision_date
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import (
    ApprovalDecision,
    Attempt,
    Case,
    InvestigationVersion,
    Membership,
    Operation,
    OutboxEvent,
    PolicyVersion,
    Proposal,
)
from opsbridge.privacy.redaction import REDACTION_VERSION, redact
from opsbridge.telemetry import metrics
from opsbridge.workflow import jobs

from . import connections
from .cases import _visible_filter, transition
from .freshness import stale_reason
from .investigations import Conflict, NotFound

PROPOSAL_SCHEMA = "opsbridge.proposal/1"
EXTERNAL_ACTIONS = frozenset({"create_follow_up_task"})
LOCAL_DESTINATION = {"connector": "opsbridge_local"}
# Title and body redacted (keeping the case's own identifiers); dispatch appends one line with
# the operation marker. A change here or in redaction makes approved proposals stale.
OUTBOUND_TRANSFORM = f"opsbridge-outbound/1+{REDACTION_VERSION}"


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FollowUpTaskParams(_Params):
    title: str = Field(min_length=3, max_length=140)
    body: str = Field(default="", max_length=2000)
    assignee_group: str = Field(min_length=1, max_length=64)


class SupplierDraftParams(_Params):
    title: str = Field(min_length=3, max_length=140)
    body: str = Field(min_length=1, max_length=4000)


class AssignCaseParams(_Params):
    assignee_group: str = Field(min_length=1, max_length=64)


class ReviewNoteParams(_Params):
    title: str = Field(min_length=3, max_length=140)
    body: str = Field(default="", max_length=2000)


PARAMS: dict[str, type[_Params]] = {
    "create_follow_up_task": FollowUpTaskParams,
    "create_supplier_message_draft": SupplierDraftParams,
    "assign_case": AssignCaseParams,
    "record_review_note": ReviewNoteParams,
}


class Forbidden(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _ctx(p: Principal) -> DbContext:
    return DbContext(tenant_id=p.tenant_id, user_id=p.user_id)


def _now() -> datetime:
    return datetime.now(UTC)


def _case(s: Session, p: Principal, case_id: uuid.UUID, lock: bool = False) -> Case:
    q = select(Case).where(Case.id == case_id, _visible_filter(p))
    case = s.execute(q.with_for_update() if lock else q).scalar_one_or_none()
    if case is None:
        raise NotFound("case")
    return case


def payload_of(pr: Proposal) -> dict[str, Any]:
    """The canonical payload an approval is bound to."""
    return {
        "schema": PROPOSAL_SCHEMA,
        "tenant_id": str(pr.tenant_id),
        "case_id": str(pr.case_id),
        "proposal_key": str(pr.proposal_key),
        "version": pr.version,
        "action_type": pr.action_type,
        "parameters": pr.parameters,
        "policy_version_id": str(pr.policy_version_id),
        "investigation_version_id": str(pr.investigation_version_id),
        "source_manifest_digest": pr.source_manifest_digest,
        "expires_at": pr.expires_at.isoformat(),
        # Absent on proposals made before destinations were bound, so their digests hold;
        # an external one of those can no longer be approved or dispatched.
        **(
            {"destination": pr.destination, "outbound": pr.outbound}
            if pr.destination is not None
            else {}
        ),
    }


def outbound_of(action: str, params: dict[str, Any], keep: list[str]) -> dict[str, Any] | None:
    """The task content that leaves OpsBridge for an external action (None for local ones)."""
    if action not in EXTERNAL_ACTIONS:
        return None
    return {
        "title": redact(params["title"], keep).text,
        "body": redact(params.get("body") or "", keep).text,
        "assignee_group": params.get("assignee_group"),
        "transform": OUTBOUND_TRANSFORM,
    }


def destination_now(s: Session, tenant_id: uuid.UUID, action: str) -> dict[str, str]:
    """Where this action would go under the tenant's task connection today (Conflict when it
    was revoked, names no repository, or the tier no longer enables its connector)."""
    if action not in EXTERNAL_ACTIONS:
        return dict(LOCAL_DESTINATION)
    return dict(connections.usable(s, tenant_id).destination)


def describe(destination: dict[str, str]) -> str:
    where = destination.get("organization") or destination.get("repository")
    return f"{destination['connector']} ({where})" if where else destination["connector"]


def operation_key(pr: Proposal) -> str:
    raw = f"{pr.tenant_id}|{pr.proposal_key}|{pr.version}|{pr.payload_digest}".encode()
    return "opk-" + hashlib.sha256(raw).hexdigest()[:40]


def _policy(s: Session, policy_version_id: uuid.UUID) -> ReconciliationPolicy:
    pv = s.get(PolicyVersion, policy_version_id)
    if pv is None:
        raise Conflict("policy_missing", "The policy version is not available.")
    return ReconciliationPolicy.model_validate(pv.document)


def _effect(
    action: str, params: dict[str, Any], destination: dict[str, str]
) -> tuple[str, list[str]]:
    if action == "create_follow_up_task":
        return (
            f"Create a follow-up task in {describe(destination)} for group "
            f"'{params['assignee_group']}': \"{params['title']}\". Does not change the invoice, "
            "purchase order or payment status.",
            [f"task_system:{destination['connector']}"],
        )
    if action == "create_supplier_message_draft":
        return (
            "Save a supplier message DRAFT in OpsBridge for human sending. Nothing is sent externally.",
            ["opsbridge:supplier_drafts"],
        )
    if action == "assign_case":
        return (
            f"Assign this case to group '{params['assignee_group']}'.",
            ["opsbridge:case_assignment"],
        )
    return ("Record a review note on this case.", ["opsbridge:case_notes"])


@dataclass
class StaleCheck:
    ok: bool
    reasons: list[str]


def evidence_state(s: Session, case: Case) -> tuple[InvestigationVersion | None, uuid.UUID | None]:
    """Latest investigation version and the policy version effective for the invoice now."""
    latest = s.execute(
        select(InvestigationVersion)
        .where(InvestigationVersion.case_id == case.id)
        .order_by(InvestigationVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return None, None
    current_policy = s.get(PolicyVersion, latest.policy_version_id)
    if current_policy is None:
        return latest, None
    on, _ = decision_date(s, case.invoice_number, case.supplier_id)
    effective = s.execute(
        select(PolicyVersion)
        .where(
            PolicyVersion.policy_id == current_policy.policy_id,
            PolicyVersion.effective_from <= on,
        )
        .order_by(PolicyVersion.effective_from.desc(), PolicyVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return latest, (effective.id if effective else None)


def check_current(s: Session, case: Case, pr: Proposal) -> StaleCheck:
    reasons = []
    if pr.destination is None:
        if pr.action_type in EXTERNAL_ACTIONS:
            reasons.append("the proposal was made before destinations were bound to approvals")
    else:
        try:
            if destination_now(s, pr.tenant_id, pr.action_type) != pr.destination:
                reasons.append("the destination changed since the proposal was made")
        except Conflict as exc:
            reasons.append(str(exc))
        keep = case_identifiers(s, case.id)
        if outbound_of(pr.action_type, pr.parameters, keep) != pr.outbound:
            reasons.append("the text that would be sent changed since the proposal was made")
    latest, effective_policy = evidence_state(s, case)
    if latest is None or latest.source_manifest_digest != pr.source_manifest_digest:
        reasons.append("source evidence changed since the proposal was made")
    elif (changed := stale_reason(s, case.id, latest)) is not None:
        reasons.append(changed)
    if (
        latest is not None
        and latest.id != pr.investigation_version_id
        and (latest.source_manifest_digest != pr.source_manifest_digest)
    ):
        reasons.append("a newer investigation version exists")
    if effective_policy != pr.policy_version_id:
        reasons.append("the effective policy version changed")
    if digest(payload_of(pr)) != pr.payload_digest:
        reasons.append("stored payload does not match its digest")
    return StaleCheck(not reasons, reasons)


# --- create / edit ----------------------------------------------------------------------


def create_from_investigation(
    db: Database,
    p: Principal,
    case_id: uuid.UUID,
    overrides: dict[str, Any],
    correlation_id: str | None,
) -> Proposal:
    require(p, "proposal.create")
    with db.session(_ctx(p)) as s:
        case = _case(s, p, case_id, lock=True)
        if case.status not in ("awaiting_review", "needs_information"):
            raise Conflict(
                "invalid_state",
                f"Proposals can be made when a case awaits review (now {case.status}).",
            )
        latest, effective_policy = evidence_state(s, case)
        if latest is None:
            raise Conflict("no_investigation", "Run an investigation first.")
        draft = (latest.recommendation or {}).get("proposal_draft") or {}
        if not draft or not (draft.get("gate") or {}).get("passed"):
            raise Conflict(
                "no_valid_draft",
                "The latest investigation has no draft that passed the policy gate.",
            )
        if effective_policy != latest.policy_version_id:
            raise Conflict("stale_investigation", "The policy changed; re-run the investigation.")
        changed = stale_reason(s, case.id, latest)
        if changed is not None:
            raise Conflict("stale_investigation", changed[0].upper() + changed[1:] + ".")
        action = draft["action_type"]
        params = _params_for(action, {**_draft_params(action, draft), **overrides})
        policy = _policy(s, latest.policy_version_id)
        _check_params_policy(action, params, policy)
        for old in s.execute(
            select(Proposal).where(
                Proposal.case_id == case.id, Proposal.status == "awaiting_approval"
            )
        ).scalars():
            old.status = "superseded"
            old.status_changed_at = _now()
        destination = destination_now(s, p.tenant_id, action)
        effect, affected = _effect(action, params, destination)
        refs = [e for f in latest.findings for e in f.get("evidence", [])][:20]
        pr = Proposal(
            tenant_id=p.tenant_id,
            case_id=case.id,
            proposal_key=uuid.uuid4(),
            version=1,
            action_type=action,
            parameters=params,
            effect_summary=effect,
            affected_resources=[f"case:{case.id}", *affected],
            evidence_refs=refs,
            source_manifest=latest.source_manifest,
            source_manifest_digest=latest.source_manifest_digest,
            investigation_version_id=latest.id,
            policy_version_id=latest.policy_version_id,
            policy_ref=latest.policy_ref,
            destination=destination,
            outbound=outbound_of(action, params, case_identifiers(s, case.id)),
            required_permission=f"proposal.decide:{action}",
            payload_digest="pending",
            canonicalization=CANONICALIZATION,
            author_kind="user",
            author_id=p.user_id,
            run_id=latest.run_id,
            status="awaiting_approval",
            expires_at=_now() + timedelta(minutes=policy.approval.lifetime_minutes),
        )
        pr.payload_digest = digest(payload_of(pr))
        s.add(pr)
        s.flush()
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="proposal.created",
            resource_type="proposal",
            resource_id=str(pr.id),
            correlation_id=correlation_id,
            details={
                "case_id": str(case.id),
                "version": 1,
                "action_type": action,
                "payload_digest": pr.payload_digest,
                "draft_origin": draft.get("origin"),
            },
        )
        return pr


def edit(
    db: Database,
    p: Principal,
    case_id: uuid.UUID,
    proposal_id: uuid.UUID,
    expected_digest: str,
    changes: dict[str, Any],
    correlation_id: str | None,
) -> Proposal:
    """Create version n+1 with edited parameters; version n is superseded (never mutated)."""
    require(p, "proposal.create")
    with db.session(_ctx(p)) as s:
        case = _case(s, p, case_id, lock=True)
        old = s.execute(
            select(Proposal)
            .where(Proposal.id == proposal_id, Proposal.case_id == case.id)
            .with_for_update()
        ).scalar_one_or_none()
        if old is None:
            raise NotFound("proposal")
        if old.payload_digest != expected_digest:
            raise Conflict(
                "stale_version",
                "The proposal changed since you loaded it.",
                {"current_digest": old.payload_digest, "current_version": old.version},
            )
        if old.status not in ("awaiting_approval", "approved"):
            raise Conflict("invalid_state", f"A {old.status} proposal cannot be edited.")
        params = _params_for(old.action_type, {**old.parameters, **changes})
        policy = _policy(s, old.policy_version_id)
        _check_params_policy(old.action_type, params, policy)
        old.status = "superseded"
        old.status_changed_at = _now()
        destination = destination_now(s, p.tenant_id, old.action_type)
        effect, affected = _effect(old.action_type, params, destination)
        new = Proposal(
            tenant_id=old.tenant_id,
            case_id=old.case_id,
            proposal_key=old.proposal_key,
            version=old.version + 1,
            action_type=old.action_type,
            parameters=params,
            effect_summary=effect,
            affected_resources=[f"case:{case.id}", *affected],
            evidence_refs=old.evidence_refs,
            source_manifest=old.source_manifest,
            source_manifest_digest=old.source_manifest_digest,
            investigation_version_id=old.investigation_version_id,
            policy_version_id=old.policy_version_id,
            policy_ref=old.policy_ref,
            destination=destination,
            outbound=outbound_of(old.action_type, params, case_identifiers(s, case.id)),
            required_permission=old.required_permission,
            payload_digest="pending",
            canonicalization=CANONICALIZATION,
            author_kind="user",
            author_id=p.user_id,
            run_id=old.run_id,
            status="awaiting_approval",
            expires_at=_now() + timedelta(minutes=policy.approval.lifetime_minutes),
        )
        new.payload_digest = digest(payload_of(new))
        s.add(new)
        s.flush()
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="proposal.edited",
            resource_type="proposal",
            resource_id=str(new.id),
            correlation_id=correlation_id,
            details={
                "supersedes": str(old.id),
                "version": new.version,
                "payload_digest": new.payload_digest,
            },
        )
        return new


def _draft_params(action: str, draft: dict[str, Any]) -> dict[str, Any]:
    if action == "assign_case":
        return {"assignee_group": draft.get("assignee_group")}
    out = {"title": draft.get("title"), "body": draft.get("body") or ""}
    if action == "create_follow_up_task":
        out["assignee_group"] = draft.get("assignee_group")
    return out


def _params_for(action: str, raw: dict[str, Any]) -> dict[str, Any]:
    model = PARAMS.get(action)
    if model is None:
        raise Conflict("unsupported_action", f"Action {action!r} is not supported.")
    try:
        return model.model_validate(
            {k: v for k, v in raw.items() if k in model.model_fields}
        ).model_dump()
    except ValidationError as exc:
        raise Conflict(
            "invalid_parameters",
            "The action parameters are invalid.",
            {"errors": [e["msg"] for e in exc.errors()]},
        ) from exc


def _check_params_policy(action: str, params: dict[str, Any], policy: ReconciliationPolicy) -> None:
    if action not in policy.allowed_actions:
        raise Conflict("action_not_allowed", f"Policy {policy.ref} does not allow {action}.")
    group = params.get("assignee_group")
    if group is not None and group not in {r.assignee_group for r in policy.action_routes.values()}:
        raise Conflict(
            "invalid_parameters", f"Assignee group {group!r} is not defined by policy {policy.ref}."
        )


# --- decide -----------------------------------------------------------------------------


def decide(
    db: Database,
    p: Principal,
    case_id: uuid.UUID,
    proposal_id: uuid.UUID,
    *,
    decision: Literal["approve", "reject"],
    version: int,
    payload_digest: str,
    reason: str | None,
    correlation_id: str | None,
) -> tuple[Proposal, ApprovalDecision, Operation | None]:
    require(p, "proposal.decide")
    with db.session(_ctx(p)) as s:
        case = _case(s, p, case_id, lock=True)
        pr = s.execute(
            select(Proposal)
            .where(Proposal.id == proposal_id, Proposal.case_id == case.id)
            .with_for_update()
        ).scalar_one_or_none()
        if pr is None:
            raise NotFound("proposal")
        if not p.can_approve(pr.action_type):
            raise Forbidden("scope", f"Your approval scope does not include {pr.action_type}.")
        if pr.status == "awaiting_approval" and pr.expires_at <= _now():
            pr.status = "expired"
            pr.status_changed_at = _now()
            s.flush()
        if pr.status != "awaiting_approval":
            raise Conflict("invalid_state", f"The proposal is {pr.status}.", {"status": pr.status})
        if pr.version != version or pr.payload_digest != payload_digest:
            raise Conflict(
                "stale_version",
                "You reviewed a different version of this proposal.",
                {"current_version": pr.version, "current_digest": pr.payload_digest},
            )
        policy = _policy(s, pr.policy_version_id)
        sod = "not_required"
        if policy.approval.separation_of_duties:
            if pr.author_id == p.user_id:
                raise Forbidden(
                    "separation_of_duties", "You cannot approve a proposal you authored."
                )
            sod = "passed"
        stale = check_current(s, case, pr)
        if decision == "approve" and not stale.ok:
            pr.status = "superseded"
            pr.status_changed_at = _now()
            audit.record(
                s,
                tenant_id=p.tenant_id,
                actor_kind="user",
                actor_id=str(p.user_id),
                action="proposal.stale",
                resource_type="proposal",
                resource_id=str(pr.id),
                correlation_id=correlation_id,
                details={"reasons": stale.reasons},
            )
            s.commit()
            raise Conflict(
                "stale_inputs",
                "Evidence or policy changed; create a new proposal.",
                {"reasons": stale.reasons},
            )
        now = _now()
        ad = ApprovalDecision(
            tenant_id=p.tenant_id,
            proposal_id=pr.id,
            proposal_version=pr.version,
            approver_id=p.user_id,
            decision="approved" if decision == "approve" else "rejected",
            payload_digest=pr.payload_digest,
            policy_version_id=pr.policy_version_id,
            source_manifest_digest=pr.source_manifest_digest,
            scope={"action_types": list(p.approval_scope), "roles": list(p.roles)},
            separation_of_duties=sod,
            reason=reason,
            expires_at=now + timedelta(minutes=policy.approval.lifetime_minutes),
            # Consumed by exactly one logical operation, in this same transaction. Set at insert
            # time so approval rows stay insert-only for the application role.
            consumed_at=now if decision == "approve" else None,
        )
        s.add(ad)
        s.flush()
        # Human wait: submitted for approval (status_changed_at) to this decision.
        waited = max((now - pr.status_changed_at).total_seconds(), 0.0)
        metrics.after_commit(s, lambda: metrics.APPROVAL_WAIT.labels(decision).observe(waited))
        op: Operation | None = None
        if decision == "reject":
            pr.status = "rejected"
            pr.status_changed_at = now
        else:
            pr.status = "approved"
            pr.status_changed_at = now
            op = Operation(
                tenant_id=p.tenant_id,
                case_id=case.id,
                proposal_id=pr.id,
                approval_id=ad.id,
                operation_key=operation_key(pr),
                action_type=pr.action_type,
                # The destination the approver saw (check_current: still what settings give).
                connector=(pr.destination or LOCAL_DESTINATION)["connector"],
                payload=payload_of(pr),
                payload_digest=pr.payload_digest,
                status="pending_dispatch",
                cancellation="none",
                requested_by=pr.author_id,
                approved_by=p.user_id,
                row_version=1,
            )
            s.add(op)
            s.flush()
            job = jobs.enqueue(
                s,
                tenant_id=p.tenant_id,
                kind="dispatch_operation",
                payload={"operation_id": str(op.id)},
                dedupe_key=f"dispatch:{op.id}",
            )
            s.add(
                OutboxEvent(
                    tenant_id=p.tenant_id,
                    event_type="operation.approved",
                    aggregate_type="operation",
                    aggregate_id=op.id,
                    payload={"operation_key": op.operation_key},
                    job_id=job.id,
                )
            )
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action=f"proposal.{ad.decision}",
            resource_type="proposal",
            resource_id=str(pr.id),
            correlation_id=correlation_id,
            details={
                "version": pr.version,
                "payload_digest": pr.payload_digest,
                "approval_id": str(ad.id),
                "operation_id": str(op.id) if op else None,
                "sod": sod,
            },
        )
        s.flush()
        s.refresh(pr)
        return pr, ad, op


def approver_still_authorized(
    s: Session, tenant_id: uuid.UUID, approver_id: uuid.UUID, action_type: str
) -> bool:
    m = s.execute(
        select(Membership).where(
            Membership.tenant_id == tenant_id,  # explicit, not only through row-level security
            Membership.user_id == approver_id,
            Membership.status == "active",
        )
    ).scalar_one_or_none()
    if m is None:
        return False
    return "proposal.decide" in permissions_for(list(m.roles)) and action_type in (
        m.approval_scope or []
    )


# --- listing, cancellation, review --------------------------------------------------------


def list_proposals(
    db: Database, p: Principal, case_id: uuid.UUID
) -> list[tuple[Proposal, str | None]]:
    """Proposals, newest first, each with why this viewer cannot decide it now (None: they
    can). The client shows that reason instead of re-deriving policy."""
    require(p, "proposal.read")
    with db.session(_ctx(p)) as s:
        _case(s, p, case_id)
        rows = list(
            s.execute(
                select(Proposal)
                .where(Proposal.case_id == case_id)
                .order_by(Proposal.generated_at.desc(), Proposal.version.desc())
            ).scalars()
        )
        return [(pr, decision_blocked(s, p, pr)) for pr in rows]


def decision_blocked(s: Session, p: Principal, pr: Proposal) -> str | None:
    """Why `p` cannot approve or reject `pr` now, by the same rules `decide` applies."""
    if pr.status != "awaiting_approval":
        return "not_awaiting_approval"
    if pr.expires_at <= _now():
        return "expired"
    if not p.has("proposal.decide"):
        return "no_permission"
    if not p.can_approve(pr.action_type):
        return "out_of_scope"
    if _policy(s, pr.policy_version_id).approval.separation_of_duties and pr.author_id == p.user_id:
        return "separation_of_duties"
    return None


def list_operations(
    db: Database, p: Principal, case_id: uuid.UUID
) -> list[tuple[Operation, list[Attempt]]]:
    require(p, "operation.read")
    with db.session(_ctx(p)) as s:
        _case(s, p, case_id)
        ops = list(
            s.execute(
                select(Operation)
                .where(Operation.case_id == case_id)
                .order_by(Operation.created_at.desc())
            ).scalars()
        )
        out = []
        for op in ops:
            atts = list(
                s.execute(
                    select(Attempt)
                    .where(Attempt.operation_id == op.id)
                    .order_by(Attempt.started_at, Attempt.retry_index)
                ).scalars()
            )
            out.append((op, atts))
        return out


def request_cancellation(
    db: Database,
    p: Principal,
    case_id: uuid.UUID,
    operation_id: uuid.UUID,
    correlation_id: str | None,
) -> Operation:
    require(p, "operation.cancel")
    with db.session(_ctx(p)) as s:
        _case(s, p, case_id)
        op = s.execute(
            select(Operation)
            .where(Operation.id == operation_id, Operation.case_id == case_id)
            .with_for_update()
        ).scalar_one_or_none()
        if op is None:
            raise NotFound("operation")
        if op.status in ("confirmed", "failed", "cancelled_before_dispatch", "outcome_unknown"):
            raise Conflict(
                "too_late",
                f"The operation is already {op.status}; local cancellation cannot undo "
                "an external action.",
                {"status": op.status},
            )
        op.cancellation = "requested"
        op.row_version += 1
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="operation.cancellation_requested",
            resource_type="operation",
            resource_id=str(op.id),
            correlation_id=correlation_id,
            details={"status_at_request": op.status},
        )
        s.flush()
        s.refresh(op)
        return op


def review_unknown_outcome(
    db: Database,
    p: Principal,
    case_id: uuid.UUID,
    operation_id: uuid.UUID,
    *,
    finding: Literal["exists", "absent"],
    remote_ref: str | None,
    note: str,
    correlation_id: str | None,
) -> Operation:
    """A person checked the external system and records what they found."""
    require(p, "operation.cancel")
    with db.session(_ctx(p)) as s:
        _case(s, p, case_id)
        op = s.execute(
            select(Operation)
            .where(Operation.id == operation_id, Operation.case_id == case_id)
            .with_for_update()
        ).scalar_one_or_none()
        if op is None:
            raise NotFound("operation")
        if op.status != "outcome_unknown":
            raise Conflict("invalid_state", "Only operations with an unknown outcome need review.")
        from opsbridge.workflow.execution import finish_operation

        # The same completion as the executor's (case, proposal, outbox, audit), by this
        # person; the confirmation says it is their finding, not proof from the provider.
        # Counted once already, when it finished as unknown.
        by = ("user", str(p.user_id))
        if finding == "exists":
            if not remote_ref:
                raise Conflict("invalid_parameters", "Provide the remote reference you found.")
            confirmation = {
                "method": "human_review",
                "reviewer": str(p.user_id),
                "note": note[:500],
                "at": _now().isoformat(),
            }
            finish_operation(
                s, op, "confirmed", remote_ref=remote_ref, confirmation=confirmation,
                actor=by, count=False,
            )  # fmt: skip
        else:
            finish_operation(
                s, op, "failed", category="verified_absent_by_human_review", actor=by, count=False
            )
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="operation.reviewed",
            resource_type="operation",
            resource_id=str(op.id),
            correlation_id=correlation_id,
            details={"finding": finding},
        )
        s.flush()
        s.refresh(op)
        return op


__all__ = ["Conflict", "Forbidden", "NotFound", "transition"]
