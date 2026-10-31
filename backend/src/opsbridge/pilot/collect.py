"""The facts the pilot report needs, read from OpsBridge's records (protocol opsbridge-pilot/1).

Everything is read in one read-only transaction under the pilot tenant's row-level security,
so the report can neither write nor see another tenant's cases. The cases are those created
in [since, until). For each, the first investigation is the one compared with the reviewer:
it is what the investigator saw first, from the same records the reviewer decided on.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
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
from opsbridge.pilot.measures import CaseFacts, ProposalChain
from opsbridge.services.proposals import drafted_parameters


@contextmanager
def read_only_session(db: Database, tenant_id: uuid.UUID) -> Iterator[Session]:
    with db.session(DbContext(tenant_id=tenant_id)) as s:
        s.execute(text("SET TRANSACTION READ ONLY"))
        yield s


def _chains(s: Session, case_id: uuid.UUID, drafts: dict[uuid.UUID, Any]) -> list[ProposalChain]:
    proposals = s.execute(
        select(Proposal)
        .where(Proposal.case_id == case_id)
        .order_by(Proposal.proposal_key, Proposal.version)
    ).scalars()
    chains = []
    for _, group in groupby(proposals, key=lambda p: p.proposal_key):
        versions = list(group)
        first, last = versions[0], versions[-1]
        draft = drafts.get(first.investigation_version_id) or {}
        chains.append(
            ProposalChain(
                drafted_parameters(first.action_type, draft),
                last.status,
                dict(last.parameters),
                len(versions),
            )
        )
    return chains


def _facts(s: Session, case: Case) -> CaseFacts:
    investigations = (
        s.execute(
            select(InvestigationVersion)
            .where(InvestigationVersion.case_id == case.id)
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
        select(func.count()).where(FieldCorrection.field_id.in_([f.id for f in fields]))
    ).scalar_one()
    drafts = {iv.id: (iv.recommendation or {}).get("proposal_draft") for iv in investigations}
    approved_at = s.execute(
        select(func.min(ApprovalDecision.decided_at))
        .join(Proposal, Proposal.id == ApprovalDecision.proposal_id)
        .where(Proposal.case_id == case.id, ApprovalDecision.decision == "approved")
    ).scalar_one()
    resolved_at = s.execute(
        select(func.min(AuditEvent.occurred_at)).where(
            AuditEvent.action == "case.resolved",
            AuditEvent.resource_type == "case",
            AuditEvent.resource_id == str(case.id),
        )
    ).scalar_one()
    return CaseFacts(
        case_number=case.case_number,
        created_at=case.created_at,
        first_investigated_at=first.created_at if first else None,
        verdict=verdict_of(first.outcome, first.findings, first.recommendation) if first else None,
        evidence_readiness=first.evidence_readiness if first else case.evidence_readiness,
        documents=Counter(e["parse_status"] for e in read),
        fields=Counter(f.status for f in fields),
        field_corrections=int(corrections),
        chains=_chains(s, case.id, drafts),
        first_approved_at=approved_at,
        resolved_at=resolved_at,
    )


def collect(
    db: Database, tenant_id: uuid.UUID, *, since: datetime, until: datetime
) -> list[CaseFacts]:
    with read_only_session(db, tenant_id) as s:
        cases = s.execute(
            select(Case)
            .where(Case.created_at >= since, Case.created_at < until)
            .order_by(Case.case_number)
        ).scalars()
        return [_facts(s, case) for case in cases.all()]
