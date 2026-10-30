"""Admission of a NEW external write: one contract for every write boundary.

The executor admits a write before it sends one; the Casework MCP tool, which performs the
write when execution goes through MCP, admits it again when the request reaches it, because
a request can arrive long after the executor's check (a delayed, retried or replayed call).
Both use `refusal`, so neither boundary is weaker than the other.

Admission point: each check reads committed state at the moment it runs. A change committed
before it is seen and refuses the write; one committed after it is too late for this write,
and cancelling cannot retract a request the provider already accepted. Recovering a write
that may have landed (read-back, reconciliation) is not a new write and is not refused here.

The approved request is built in one place (`approved_request`): exactly the outbound text
and destination sealed in the approved payload, plus the operation marker and transport
metadata (deadline, attempt). Nothing is re-rendered from the raw parameters after approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsbridge.canonical import digest
from opsbridge.connectors.tasks import TaskRequest
from opsbridge.persistence.models import ApprovalDecision, Case, Operation, Proposal


@dataclass(frozen=True)
class Refusal:
    """Why a new write may not be admitted now."""

    category: str  # cancelled_by_user | precondition_failed
    problems: tuple[str, ...]

    @property
    def detail(self) -> str:
        return "; ".join(self.problems)


def marker(operation_key: str) -> str:
    """The marker embedded in every remote task, by which reconciliation finds it."""
    return f"opsbridge-op:{operation_key}"


def refusal(s: Session, op: Operation, *, lock_case: bool = True) -> Refusal | None:
    """None when a new write for `op` may be sent now; otherwise why not.

    `lock_case` takes the case row lock that corrections and uploads also take, so one
    committed first is seen and one waiting lands after the decision. A boundary without
    UPDATE rights (the casework service) reads committed state instead: same ordering, no
    lock.
    """
    if op.cancellation == "requested":
        return Refusal("cancelled_by_user", ("cancellation was requested",))
    problems = _eligibility(s, op, lock_case=lock_case)
    return Refusal("precondition_failed", tuple(problems)) if problems else None


def _eligibility(s: Session, op: Operation, *, lock_case: bool) -> list[str]:
    from opsbridge.services.proposals import approver_still_authorized, check_current, payload_of

    problems = []
    pr = s.get(Proposal, op.proposal_id)
    ad = s.get(ApprovalDecision, op.approval_id)
    query = select(Case).where(Case.id == op.case_id)
    case = s.execute(query.with_for_update() if lock_case else query).scalar_one_or_none()
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
    if ad.expires_at <= datetime.now(UTC):
        problems.append("approval expired before dispatch")
    if not approver_still_authorized(s, op.tenant_id, ad.approver_id, op.action_type):
        problems.append("approver no longer holds approval authority for this action")
    stale = check_current(s, case, pr)
    problems += stale.reasons
    return problems


def approved_request(
    payload: dict[str, Any],
    operation_key: str,
    *,
    deadline: float | None = None,
    attempt_id: str | None = None,
) -> TaskRequest:
    """Exactly the approved outbound text; the marker line lets reconciliation find it."""
    out = payload["outbound"]
    mark = marker(operation_key)
    return TaskRequest(
        title=out["title"],
        body=f"{out['body']}\n\nOpsBridge operation marker: {mark}",
        assignee_group=out["assignee_group"],
        marker=mark,
        deadline=deadline,
        attempt_id=attempt_id,
    )
