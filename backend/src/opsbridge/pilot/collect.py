"""The facts the pilot report needs, as they stood at its cut-off (protocol opsbridge-pilot/2).

Everything is read in one read-only, repeatable-read transaction under the pilot tenant's
row-level security (`Database.snapshot`): every query sees the database as of the first, so
a change committed while the report runs is in none of them, and the report can neither
write nor see another tenant's cases. The cases are those created in [since, until). For
each, the first investigation is the one compared with the reviewer: it is what the
investigator saw first, from the same records the reviewer decided on.

Review N05. `--as-of` was called the cut-off, but the present was read: the first
investigation could be one made after it, a proposal counted with the status it has now, and
corrections whenever made, so anything done after the cut-off could improve the report. Now
only what existed at the cut-off is read:
- the first investigation made by then (none: the case is `not_investigated`);
- each proposal's versions made by then, and the last one's status then: its present status
  if it changed by then, otherwise an approval or rejection decided by then, otherwise
  awaiting approval;
- field corrections made by then. A field corrected since is counted with the status the
  first such correction found (`previous_status`, recorded from migration 0023); one
  corrected before that upgrade is `status_not_recorded`, not guessed;
- the first approval and the resolution made by then.
The times compared are the database's own (it records every one), so the cut-off is a time
on the database's clock, and one later than the snapshot itself is refused: facts after it
could still change. This is a consistent snapshot read with recorded event times, not a
reconstruction of anything those times do not record.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from opsbridge.evaluation.verdict import verdict_of
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import (
    ApprovalDecision,
    AuditEvent,
    Case,
    ExtractedField,
    FieldCorrection,
    InvestigationVersion,
    Proposal,
)
from opsbridge.pilot.measures import CaseFacts, ProposalChain, check_window
from opsbridge.services.proposals import drafted_parameters


@dataclass(frozen=True)
class Snapshot:
    """The transaction the facts were read in, as the database reports it."""

    isolation: str
    read_only: bool
    taken_at: datetime  # the database's clock when the transaction began
    xid_snapshot: str  # pg_current_snapshot(): which transactions it can see

    def as_dict(self) -> dict[str, Any]:
        return {
            "isolation": self.isolation,
            "read_only": self.read_only,
            "taken_at": self.taken_at.isoformat(),
            "xid_snapshot": self.xid_snapshot,
        }


@dataclass(frozen=True)
class Collected:
    cases: list[CaseFacts]
    snapshot: Snapshot


def _status_then(p: Proposal, decision: ApprovalDecision | None, as_of: datetime) -> str:
    if p.status_changed_at <= as_of:
        return p.status
    if decision is not None:  # decided by then; changed again since (approved, then refused)
        return decision.decision
    return "awaiting_approval"


def _chains(
    s: Session, case_id: uuid.UUID, drafts: dict[uuid.UUID, Any], as_of: datetime
) -> list[ProposalChain]:
    proposals = (
        s.execute(
            select(Proposal)
            .where(Proposal.case_id == case_id, Proposal.generated_at <= as_of)
            .order_by(Proposal.proposal_key, Proposal.version)
        )
        .scalars()
        .all()
    )
    decisions = {
        d.proposal_id: d
        for d in s.execute(
            select(ApprovalDecision).where(
                ApprovalDecision.proposal_id.in_([p.id for p in proposals]),
                ApprovalDecision.decided_at <= as_of,
            )
        ).scalars()
    }
    chains = []
    for _, group in groupby(proposals, key=lambda p: p.proposal_key):
        versions = list(group)
        first, last = versions[0], versions[-1]
        draft = drafts.get(first.investigation_version_id) or {}
        chains.append(
            ProposalChain(
                drafted_parameters(first.action_type, draft),
                _status_then(last, decisions.get(last.id), as_of),
                dict(last.parameters),
                len(versions),
            )
        )
    return chains


def _facts(s: Session, case: Case, as_of: datetime) -> CaseFacts:
    investigations = (
        s.execute(
            select(InvestigationVersion)
            .where(
                InvestigationVersion.case_id == case.id, InvestigationVersion.created_at <= as_of
            )
            .order_by(InvestigationVersion.version)
        )
        .scalars()
        .all()
    )
    first = investigations[0] if investigations else None
    read = [e for e in (first.source_manifest if first else []) if "parse_status" in e]
    version_ids = [uuid.UUID(e["version_id"]) for e in read]
    fields = s.execute(
        select(ExtractedField.id, ExtractedField.status).where(
            ExtractedField.version_id.in_(version_ids)
        )
    ).all()
    corrections = s.execute(
        select(
            FieldCorrection.field_id, FieldCorrection.created_at, FieldCorrection.previous_status
        )
        .where(FieldCorrection.field_id.in_([f.id for f in fields]))
        .order_by(FieldCorrection.created_at)
    ).all()
    made = [c for c in corrections if c.created_at <= as_of]
    corrected = {c.field_id for c in made}
    since_cut_off: dict[uuid.UUID, str | None] = {}
    for c in corrections:
        if c.created_at > as_of:
            since_cut_off.setdefault(c.field_id, c.previous_status)  # the first one after it

    def status_then(field_id: uuid.UUID, status: str) -> str:
        if field_id in corrected:
            return "corrected"
        if field_id in since_cut_off:  # corrected since: as the first such correction found it
            return since_cut_off[field_id] or "status_not_recorded"
        return status

    drafts = {iv.id: (iv.recommendation or {}).get("proposal_draft") for iv in investigations}
    approved_at = s.execute(
        select(func.min(ApprovalDecision.decided_at))
        .join(Proposal, Proposal.id == ApprovalDecision.proposal_id)
        .where(
            Proposal.case_id == case.id,
            ApprovalDecision.decision == "approved",
            ApprovalDecision.decided_at <= as_of,
        )
    ).scalar_one()
    resolved_at = s.execute(
        select(func.min(AuditEvent.occurred_at)).where(
            AuditEvent.action == "case.resolved",
            AuditEvent.resource_type == "case",
            AuditEvent.resource_id == str(case.id),
            AuditEvent.occurred_at <= as_of,
        )
    ).scalar_one()
    return CaseFacts(
        case_number=case.case_number,
        invoice=(case.invoice_number, case.supplier_id) if case.invoice_number else None,
        created_at=case.created_at,
        first_investigated_at=first.created_at if first else None,
        verdict=verdict_of(first.outcome, first.findings, first.recommendation) if first else None,
        evidence_readiness=first.evidence_readiness if first else "not_investigated",
        documents=Counter(e["parse_status"] for e in read),
        fields=Counter(status_then(f.id, f.status) for f in fields),
        field_corrections=len(made),
        chains=_chains(s, case.id, drafts, as_of),
        first_approved_at=approved_at,
        resolved_at=resolved_at,
        explanation_origin=first.explanation_origin if first else None,
    )


def _snapshot(s: Session) -> Snapshot:
    isolation, read_only, taken_at, xids = s.execute(
        text(
            "SELECT current_setting('transaction_isolation'), "
            "current_setting('transaction_read_only'), now(), pg_current_snapshot()::text"
        )
    ).one()
    return Snapshot(str(isolation), read_only == "on", taken_at, str(xids))


def collect(
    db: Database, tenant_id: uuid.UUID, *, since: datetime, until: datetime, as_of: datetime
) -> Collected:
    check_window(since, until, as_of)
    with db.snapshot(DbContext(tenant_id=tenant_id)) as s:
        snapshot = _snapshot(s)
        if as_of > snapshot.taken_at:
            raise ValueError(
                f"the cut-off {as_of.isoformat()} is later than the database's clock "
                f"({snapshot.taken_at.isoformat()}): what happens before it can still change"
            )
        cases = s.execute(
            select(Case)
            .where(Case.created_at >= since, Case.created_at < until)
            .order_by(Case.case_number)
        ).scalars()
        return Collected([_facts(s, case, as_of) for case in cases.all()], snapshot)
