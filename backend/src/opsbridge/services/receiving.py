"""The case's receiving view (docs/receiving-disposition.md): what was delivered, accepted,
rejected and remedied, the timeline of receiving evidence, the open question with its owner
and basis, and what changed since each approval.

Positions are derived from the current records with the same function the rules use
(domain/receiving.py), so the view and the findings cannot disagree about a total; the
matched and disputed split is the one the latest investigation concluded. Times are kept
apart: when an event occurred, as the source states it, and when OpsBridge recorded it. Reading
it changes nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsbridge.auth.permissions import Principal, require
from opsbridge.domain.money import decimal_str
from opsbridge.domain.policy import ReconciliationPolicy
from opsbridge.domain.receiving import (
    LinePosition,
    positions,
    precision,
    resolve_po,
    unknown_line_events,
)
from opsbridge.domain.records import SourceRef
from opsbridge.evidence.records import load_match_input
from opsbridge.persistence.db import Database
from opsbridge.persistence.models import (
    ApprovalDecision,
    DocumentVersion,
    GoodsReceiptRecord,
    InvestigationVersion,
    Operation,
    PolicyVersion,
    Proposal,
    ReceiptLineRecord,
    ReceivingEventRecord,
    SourceDocument,
)

from .investigations import NotFound, _case, _ctx

_ACTED = ("confirmed", "outcome_unknown")
_PENDING = ("pending_dispatch", "executing")


def _d(x: Decimal | None) -> str | None:
    return None if x is None else decimal_str(x)


def _ref(r: SourceRef) -> dict[str, str]:
    return {k: v for k, v in r.to_dict().items() if k != "classification"}


def _when(t: datetime | None) -> str | None:
    return t.isoformat() if t is not None else None


def _line(p: LinePosition, concluded: dict[str, dict[str, str]]) -> dict[str, Any]:
    split = concluded.get(p.po_line_number, {})
    return {
        "po_line_number": p.po_line_number,
        "unit": p.unit,
        "ordered": _d(p.ordered),
        "delivered": _d(p.delivered),
        "accepted": _d(p.accepted),
        "rejected": _d(p.rejected),
        "undelivered": _d(p.undelivered),
        "returned_after_acceptance": _d(p.returned_after_acceptance),
        "returned": _d(p.returned),
        "replacement_promised": _d(p.replacement_promised),
        "replacement_delivered": _d(p.replacement_delivered),
        "replacement_accepted": _d(p.replacement_accepted),
        "credit_promised": _d(p.credit_promised),
        "credit_posted": _d(p.credit_posted),
        "credits_elsewhere": list(p.credits_elsewhere),
        "covered": _d(p.covered),
        "open_rejected": _d(p.open_rejected),
        "disposition": p.disposition,
        "invoiced": split.get("invoiced"),
        "matched": split.get("matched"),
        "disputed": split.get("disputed"),
        "rejections": [
            {
                "source": r.source,
                "receipt_number": r.receipt_number,
                "quantity": _d(r.quantity),
                "reason": r.reason,
                "occurred": r.occurred,
                "occurred_precision": precision(r.occurred),
                "late": r.late,
                "evidence": [_ref(e) for e in r.evidence],
            }
            for r in p.rejections
        ],
        "problems": [
            {"status": pr.status, "code": pr.code, "detail": pr.detail} for pr in p.problems
        ],
        "evidence": [_ref(e) for e in (*p.accepted_evidence, *p.remedy_evidence)],
    }


def _concluded(iv: InvestigationVersion | None) -> dict[str, dict[str, str]]:
    """Per PO line: what the investigation billed, matched and disputed (quantities only)."""
    out: dict[str, dict[str, str]] = {}
    if iv is None:
        return out
    disputed: dict[str, Decimal] = {}
    for f in iv.findings:
        line, values = f.get("po_line_number"), f.get("values") or {}
        if line is None or "invoiced" not in values:
            continue
        out.setdefault(line, {})["invoiced"] = values["invoiced"]
        if f["reason_code"] in ("invoiced_exceeds_received", "receipt_quantity_already_allocated"):
            disputed[line] = disputed.get(line, Decimal(0)) + Decimal(values["excess"])
        elif f["finding_type"] == "disposition" and "disputed" in values:
            disputed[line] = disputed.get(line, Decimal(0)) + Decimal(values["disputed"])
    for line, entry in out.items():
        d = disputed.get(line, Decimal(0))
        entry["disputed"] = decimal_str(d)
        entry["matched"] = decimal_str(Decimal(entry["invoiced"]) - d)
    return out


def _versions(
    s: Session, ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[DocumentVersion, SourceDocument]]:
    if not ids:
        return {}
    rows = s.execute(
        select(DocumentVersion, SourceDocument)
        .join(SourceDocument, SourceDocument.id == DocumentVersion.document_id)
        .where(DocumentVersion.id.in_(ids))
    ).all()
    return {v.id: (v, d) for v, d in rows}


def _timeline(
    s: Session,
    po_number: str,
    approvals: list[tuple[ApprovalDecision, Proposal]],
    ops: list[Operation],
) -> list[dict[str, Any]]:
    first_approval = min((ad.decided_at for ad, _ in approvals), default=None)
    entries: list[dict[str, Any]] = []
    receipts = list(
        s.execute(
            select(GoodsReceiptRecord).where(GoodsReceiptRecord.po_number == po_number)
        ).scalars()
    )
    events = list(
        s.execute(
            select(ReceivingEventRecord).where(ReceivingEventRecord.po_number == po_number)
        ).scalars()
    )
    docs = _versions(
        s, {r.source_version_id for r in receipts} | {e.source_version_id for e in events}
    )

    def source(version_id: uuid.UUID) -> dict[str, Any]:
        v, d = docs[version_id]
        return {
            "document_id": str(d.id),
            "document_title": d.title,
            "version_id": str(v.id),
            "version_no": v.version_no,
            "withdrawn": d.withdrawn_at is not None,
        }

    for gr in receipts:
        lines = s.execute(
            select(ReceiptLineRecord).where(ReceiptLineRecord.goods_receipt_id == gr.id)
        ).scalars()
        parts = [
            f"line {ln.po_line_number}: {decimal_str(ln.quantity)} {ln.unit} "
            f"{ln.kind} {ln.status}" + (f" ({ln.reason})" if ln.reason else "")
            for ln in lines
        ]
        entries.append(
            {
                "kind": "receipt",
                "id": gr.receipt_number,
                "summary": "; ".join(parts),
                "occurred": gr.received_date.isoformat(),
                "occurred_precision": "date",
                "recorded_at": gr.created_at.isoformat(),
                "current": gr.superseded_at is None,
                "superseded_at": _when(gr.superseded_at),
                "after_approval": first_approval is not None and gr.created_at > first_approval,
                **source(gr.source_version_id),
            }
        )
    for ev in events:
        what = ev.event_type.replace("_", " ")
        qty = (
            f" of {decimal_str(ev.quantity)} {ev.unit or ''}".rstrip()
            if ev.quantity is not None
            else ""
        )
        refs = ", ".join(
            x
            for x in (
                f"receipt {ev.receipt_number}" if ev.receipt_number else "",
                f"follows {ev.follows}" if ev.follows else "",
                f"invoice {ev.invoice_number}" if ev.invoice_number else "",
                f"document {ev.document_number}" if ev.document_number else "",
            )
            if x
        )
        entries.append(
            {
                "kind": "event",
                "id": ev.event_id,
                "event_type": ev.event_type,
                "summary": f"{what}{qty} on PO line {ev.po_line_number}"
                + (f" ({ev.reason})" if ev.reason else "")
                + (f"; {refs}" if refs else ""),
                "occurred": ev.occurred,
                "occurred_precision": precision(ev.occurred),
                "recorded_at": ev.created_at.isoformat(),
                "current": ev.superseded_at is None,
                "superseded_at": _when(ev.superseded_at),
                "after_approval": first_approval is not None and ev.created_at > first_approval,
                **source(ev.source_version_id),
            }
        )
    for ad, pr in approvals:
        entries.append(
            {
                "kind": "approval",
                "id": str(pr.id),
                "summary": f"{ad.decision} {pr.action_type.replace('_', ' ')} (proposal "
                f"version {ad.proposal_version})",
                "occurred": None,
                "occurred_precision": "unknown",
                "recorded_at": ad.decided_at.isoformat(),
                "current": True,
                "after_approval": False,
            }
        )
    for op in ops:
        entries.append(
            {
                "kind": "execution",
                "id": str(op.id),
                "summary": f"{op.action_type.replace('_', ' ')}: {op.status.replace('_', ' ')}"
                + (f" ({op.failure_category})" if op.failure_category else ""),
                "occurred": None,
                "occurred_precision": "unknown",
                "recorded_at": op.updated_at.isoformat(),
                "current": True,
                "after_approval": False,
            }
        )
    return sorted(entries, key=lambda e: (e["recorded_at"], e["kind"], e["id"]))


def _since_approval(
    s: Session,
    approvals: list[tuple[ApprovalDecision, Proposal]],
    ops: list[Operation],
    current: set[uuid.UUID],
) -> list[dict[str, Any]]:
    by_approval = {op.approval_id: op for op in ops}
    out = []
    for ad, pr in approvals:
        if ad.decision != "approved":
            continue
        approved = {
            uuid.UUID(m["version_id"])
            for m in pr.source_manifest
            if "version_id" in m and "correction_id" not in m
        }
        added, dropped = current - approved, approved - current
        docs = _versions(s, added | dropped)
        changes = [
            {
                "change": "added" if vid in added else "no longer current",
                "document_title": docs[vid][1].title,
                "kind": docs[vid][1].kind,
                "version_no": docs[vid][0].version_no,
                "recorded_at": docs[vid][0].created_at.isoformat(),
            }
            for vid in sorted(added | dropped, key=lambda v: docs[v][0].created_at)
            if vid in docs
        ]
        op = by_approval.get(ad.id)
        if not changes:
            requires = "nothing: the evidence is what was approved"
        elif op is not None and op.status in _ACTED:
            requires = (
                "reconciliation: the action was "
                + ("carried out" if op.status == "confirmed" else "possibly carried out")
                + " on evidence that has since changed; nothing was repeated or undone"
            )
        elif op is not None and op.status in _PENDING:
            requires = "review: the pending action will not be sent on the changed evidence"
        else:
            requires = "review: the approval does not carry over to the changed evidence"
        out.append(
            {
                "proposal_id": str(pr.id),
                "action_type": pr.action_type,
                "proposal_status": pr.status,
                "approved_at": ad.decided_at.isoformat(),
                "operation_status": op.status if op is not None else None,
                "changes": changes,
                "requires": requires,
            }
        )
    return out


def receiving_view(db: Database, p: Principal, case_id: uuid.UUID) -> dict[str, Any]:
    require(p, "evidence.read")
    with db.session(_ctx(p)) as s:
        case = _case(s, p, case_id)
        iv = s.execute(
            select(InvestigationVersion)
            .where(InvestigationVersion.case_id == case_id)
            .order_by(InvestigationVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if iv is None or not case.invoice_number:
            raise NotFound("investigation")
        doc = s.get(PolicyVersion, iv.policy_version_id)
        if doc is None:
            raise NotFound("policy")
        policy = ReconciliationPolicy.model_validate(doc.document)
        state = iv.evidence_state or {}
        loaded = load_match_input(
            s, case.invoice_number, state.get("po_number") or case.po_number, case.supplier_id
        )
        data = loaded.match_input
        po = resolve_po(data) if data is not None else None
        approvals = list(
            s.execute(
                select(ApprovalDecision, Proposal)
                .join(Proposal, Proposal.id == ApprovalDecision.proposal_id)
                .where(Proposal.case_id == case_id)
                .order_by(ApprovalDecision.decided_at)
            ).tuples()
        )
        ops = list(
            s.execute(
                select(Operation).where(Operation.case_id == case_id).order_by(Operation.created_at)
            ).scalars()
        )
        rec = iv.recommendation or {}
        basis_ids = set(rec.get("finding_ids") or [])
        hold = next(
            (f for f in iv.findings if str(f.get("reason_code", "")).startswith("hold_")), None
        )
        return {
            "invoice_number": case.invoice_number,
            "po_number": po.po_number if po is not None else state.get("po_number"),
            "investigation_version": iv.version,
            "rules_version": iv.rules_version,
            "policy_ref": iv.policy_ref,
            "lines": [_line(x, _concluded(iv)) for x in positions(data, policy)]
            if data is not None
            else [],
            "unattached_events": [
                {"event_id": ev.event_id, "po_line_number": ev.po_line_number}
                for ev in (unknown_line_events(data, po) if data is not None and po else [])
            ],
            "split": (
                {
                    "hold": hold["values"].get("hold"),
                    "matched_quantity": hold["values"].get("matched_quantity"),
                    "disputed_quantity": hold["values"].get("disputed_quantity"),
                    "unit": hold["values"].get("unit"),
                    "summary": hold["summary"],
                }
                if hold
                else None
            ),
            "question": {
                "decision": rec.get("decision"),
                "action_type": rec.get("action_type"),
                "owner": rec.get("assignee_group"),
                "owner_configured": not any(
                    f.get("reason_code") == "disposition_owner_unconfigured" for f in iv.findings
                ),
                "title": rec.get("title"),
                # The route's findings first, then every other open question: when the case
                # goes to manual review because no owner is set, the disposition it is about
                # stays in view.
                "basis": [
                    {
                        "finding_id": f["finding_id"],
                        "reason_code": f["reason_code"],
                        "summary": f["summary"],
                    }
                    for f in sorted(
                        (f for f in iv.findings if f.get("status") != "informational"),
                        key=lambda f: f.get("finding_id") not in basis_ids,
                    )
                ],
            },
            "timeline": _timeline(s, po.po_number, approvals, ops) if po is not None else [],
            "since_approval": _since_approval(s, approvals, ops, loaded.version_ids),
        }
