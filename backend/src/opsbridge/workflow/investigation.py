"""Bounded investigation workflow with durable, resumable steps.

Steps (names are part of the trace contract):
  collect_context    fetch authorized source records and ingest them as document versions
  retrieve_policy    select the policy version effective for the invoice date
  reconcile_records  deterministic matching (no model) + rules-only next action
  explain_findings   explanation text (template, or a validated model draft in Phase 3)

Each completed step is persisted with an input digest, output and timestamps; a restarted
worker skips completed steps. An unreachable source raises `SourceUnavailable`, which is
retried — it is never treated as "the record does not exist".
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select, text

from opsbridge.audit import events as audit
from opsbridge.canonical import digest
from opsbridge.connectors.purchasing import PurchasingClient
from opsbridge.domain.decision import recommend
from opsbridge.domain.matching import Finding, MatchResult, reconcile
from opsbridge.domain.policy import ReconciliationPolicy
from opsbridge.evidence.citations import resolve_all
from opsbridge.evidence.ingest import IngestRejected, ingest
from opsbridge.evidence.records import load_match_input, source_manifest
from opsbridge.evidence.storage import ArtifactStore
from opsbridge.mapping import PackSpec, RecordType
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import (
    Case,
    InvestigationVersion,
    InvoiceRecord,
    PolicyVersion,
    Tenant,
    WorkflowRun,
    WorkflowStep,
)
from opsbridge.services.cases import transition

WORKFLOW_VERSION = "investigation/1.0.0"
SERVICE_IDENTITY = "service:opsbridge-worker"


@dataclass
class InvestigationDeps:
    db: Database
    store: ArtifactStore
    pack_for: Callable[[str], PackSpec]
    purchasing_for: Callable[[str], PurchasingClient]
    explain: Callable[..., dict[str, Any]] | None = None  # Phase 3 hook; default template


class StepFailed(Exception):
    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _ctx(tenant_id: uuid.UUID) -> DbContext:
    return DbContext(tenant_id=tenant_id)


def _run_step(
    deps: InvestigationDeps,
    run: WorkflowRun,
    seq: int,
    name: str,
    inputs: Any,
    fn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    ctx = _ctx(run.tenant_id)
    with deps.db.session(ctx) as s:
        step = s.execute(
            select(WorkflowStep).where(
                WorkflowStep.run_id == run.id, WorkflowStep.step_name == name
            )
        ).scalar_one_or_none()
        if step is not None and step.status == "completed":
            return dict(step.output or {})
        if step is None:
            step = WorkflowStep(
                tenant_id=run.tenant_id, run_id=run.id, seq=seq, step_name=name, status="running"
            )
            s.add(step)
        step.status = "running"
        step.input_digest = digest(inputs)
        step.started_at = datetime.now(UTC)
        step.error = None
    try:
        output = fn()
    except Exception as exc:
        with deps.db.session(ctx) as s:
            step = s.execute(
                select(WorkflowStep).where(
                    WorkflowStep.run_id == run.id, WorkflowStep.step_name == name
                )
            ).scalar_one()
            step.status = "failed"
            step.error = f"{type(exc).__name__}: {exc}"[:2000]
            step.finished_at = datetime.now(UTC)
        raise
    with deps.db.session(ctx) as s:
        step = s.execute(
            select(WorkflowStep).where(
                WorkflowStep.run_id == run.id, WorkflowStep.step_name == name
            )
        ).scalar_one()
        step.status = "completed"
        step.output = output
        step.output_digest = digest(output)
        step.finished_at = datetime.now(UTC)
    return output


def run_investigation(
    deps: InvestigationDeps, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> uuid.UUID:
    """Execute (or resume) an investigation run. Returns the investigation version id."""
    from opsbridge.connectors.purchasing import SourceRecordInvalid, SourceUnavailable

    ctx = _ctx(tenant_id)
    with deps.db.session(ctx) as s:
        run = s.get(WorkflowRun, run_id)
        if run is None:
            raise StepFailed("run not found in tenant", retryable=False)
        if run.status == "completed":
            existing = s.execute(
                select(InvestigationVersion.id).where(InvestigationVersion.run_id == run_id)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        case = s.get(Case, run.case_id)
        tenant = s.get(Tenant, tenant_id)
        assert case is not None and tenant is not None
        if case.status in ("resolved", "closed_without_resolution"):
            raise StepFailed("case is closed", retryable=False)
        run.status = "running"
        run.started_at = run.started_at or datetime.now(UTC)
        run.executed_by = SERVICE_IDENTITY
        if case.status in ("new", "needs_information", "awaiting_review", "follow_up_pending"):
            transition(case, "collecting_evidence")
        case_snapshot = {
            "case_id": str(case.id),
            "invoice_number": case.invoice_number,
            "po_number": case.po_number,
            "case_number": case.case_number,
        }
        tenant_slug, pack_id = tenant.slug, tenant.pack_id
    pack = deps.pack_for(pack_id)

    def collect() -> dict[str, Any]:
        try:
            return _collect(deps, tenant_id, tenant_slug, pack, case_snapshot)
        except SourceUnavailable as exc:
            raise StepFailed(f"purchasing system unavailable: {exc}", retryable=True) from exc
        except SourceRecordInvalid as exc:
            raise StepFailed(
                f"purchasing system returned an unusable record: {exc}", retryable=False
            ) from exc

    collected = _run_step(deps, run, 1, "collect_context", case_snapshot, collect)
    policy_out = _run_step(
        deps,
        run,
        2,
        "retrieve_policy",
        {"pack": pack.policy_id, **collected},
        lambda: _retrieve_policy(deps, tenant_id, pack.policy_id, case_snapshot),
    )
    recon = _run_step(
        deps,
        run,
        3,
        "reconcile_records",
        {"policy": policy_out, "collected": collected},
        lambda: _reconcile(deps, tenant_id, case_snapshot, policy_out, collected),
    )
    explained = _run_step(
        deps,
        run,
        4,
        "explain_findings",
        recon,
        lambda: (deps.explain or template_explanation)(
            recon=recon, policy=policy_out, case=case_snapshot
        ),
    )
    return _finalize(
        deps, tenant_id, run_id, case_snapshot, collected, policy_out, recon, explained
    )


# --- collect_context ------------------------------------------------------------------


def _ingest_record(
    deps: InvestigationDeps,
    tenant_id: uuid.UUID,
    pack: PackSpec,
    client: PurchasingClient,
    record_type: RecordType,
    key: str,
    case_id: str,
    linked: list[dict[str, Any]],
    problems: list[str],
) -> bool:
    spec = pack.sources.get(record_type)
    if spec is None or spec.collection is None:
        return False
    fetched = client.fetch(spec.collection, key)
    if fetched is None:
        return False
    with deps.db.session(_ctx(tenant_id)) as s:
        try:
            res = ingest(
                s,
                deps.store,
                tenant_id=tenant_id,
                pack=pack,
                record_type=record_type,
                source_key=f"purchasing:{spec.collection}:{key}",
                origin="source_api",
                title=f"{record_type.replace('_', ' ')} {key}",
                data=fetched.body,
                content_type=fetched.content_type,
                filename=None,
            )
        except IngestRejected as exc:
            problems.append(f"{record_type} {key}: rejected ({exc.reason})")
            return False
        s.execute(
            text(
                "INSERT INTO case_documents (tenant_id, case_id, document_id, role) "
                "VALUES (:t, :c, :d, :r) ON CONFLICT DO NOTHING"
            ),
            {"t": tenant_id, "c": case_id, "d": res.document_id, "r": record_type},
        )
    linked.append(
        {
            "record_type": record_type,
            "key": key,
            "document_id": str(res.document_id),
            "version_id": str(res.version_id),
            "version_no": res.version_no,
            "raw_sha256": res.raw_sha256,
            "parse_status": res.parse_status,
            "new_version": res.new_version,
        }
    )
    problems.extend(f"{record_type} {key}: {p}" for p in res.problems if not p.startswith("flag:"))
    return True


def _collect(
    deps: InvestigationDeps, tenant_id: uuid.UUID, org: str, pack: PackSpec, case: dict[str, Any]
) -> dict[str, Any]:
    client = deps.purchasing_for(org)
    linked: list[dict[str, Any]] = []
    problems: list[str] = []
    unavailable: list[str] = []
    try:
        invoice_no = case["invoice_number"]
        po_no = case["po_number"]
        if invoice_no and not _ingest_record(
            deps, tenant_id, pack, client, "invoice", invoice_no, case["case_id"], linked, problems
        ):
            unavailable.append(f"invoice {invoice_no}")
        if invoice_no:
            with deps.db.session(_ctx(tenant_id)) as s:
                found = s.execute(
                    select(InvoiceRecord.po_number).where(
                        InvoiceRecord.invoice_number == invoice_no,
                        InvoiceRecord.superseded_at.is_(None),
                    )
                ).scalar()
            po_no = found or po_no
        if po_no:
            if not _ingest_record(
                deps,
                tenant_id,
                pack,
                client,
                "purchase_order",
                po_no,
                case["case_id"],
                linked,
                problems,
            ):
                unavailable.append(f"purchase order {po_no}")
            follow_ups: tuple[RecordType, ...] = (
                "goods_receipt",
                "allocation_history",
                "supplier_message",
            )
            for record_type in follow_ups:
                spec = pack.sources.get(record_type)
                if spec is None or spec.collection is None:
                    continue
                for key in client.list_keys(spec.collection, po_no):
                    _ingest_record(
                        deps,
                        tenant_id,
                        pack,
                        client,
                        record_type,
                        key,
                        case["case_id"],
                        linked,
                        problems,
                    )
    finally:
        client.close()
    return {
        "documents": linked,
        "unavailable": unavailable,
        "problems": problems,
        "po_number": po_no,
    }


# --- retrieve_policy ------------------------------------------------------------------


def _retrieve_policy(
    deps: InvestigationDeps, tenant_id: uuid.UUID, policy_id: str, case: dict[str, Any]
) -> dict[str, Any]:
    with deps.db.session(_ctx(tenant_id)) as s:
        inv_date = None
        if case["invoice_number"]:
            inv_date = s.execute(
                select(InvoiceRecord.invoice_date).where(
                    InvoiceRecord.invoice_number == case["invoice_number"],
                    InvoiceRecord.superseded_at.is_(None),
                )
            ).scalar()
        decision_date: date = inv_date or datetime.now(UTC).date()
        versions = list(
            s.execute(
                select(PolicyVersion)
                .where(PolicyVersion.policy_id == policy_id)
                .order_by(PolicyVersion.effective_from, PolicyVersion.version)
            ).scalars()
        )
        effective = [v for v in versions if v.effective_from <= decision_date]
        if not effective:
            return {
                "policy_version_id": None,
                "policy_ref": None,
                "decision_date": decision_date.isoformat(),
                "superseded": [],
                "problem": f"no {policy_id} policy effective on {decision_date}",
            }
        chosen = effective[-1]
        return {
            "policy_version_id": str(chosen.id),
            "policy_ref": f"{chosen.policy_id}@v{chosen.version}",
            "policy_sha256": chosen.sha256,
            "decision_date": decision_date.isoformat(),
            "decision_date_source": "invoice_date" if inv_date else "today",
            "superseded": [
                f"{v.policy_id}@v{v.version} (effective {v.effective_from})"
                for v in versions
                if v.id != chosen.id and v.effective_from <= decision_date
            ],
            "later_versions": [
                f"{v.policy_id}@v{v.version} (effective {v.effective_from})"
                for v in versions
                if v.effective_from > decision_date
            ],
        }


# --- reconcile_records ----------------------------------------------------------------


def _manual_finding(reason: str, summary: str, status: str = "needs_review") -> dict[str, Any]:
    return Finding("manual_review", status, reason, summary).to_dict()  # type: ignore[arg-type]


def _reconcile(
    deps: InvestigationDeps,
    tenant_id: uuid.UUID,
    case: dict[str, Any],
    policy_out: dict[str, Any],
    collected: dict[str, Any],
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    uncertainty: list[str] = list(collected.get("problems", []))
    with deps.db.session(_ctx(tenant_id)) as s:
        if policy_out.get("policy_version_id") is None:
            findings.append(
                _manual_finding(
                    "policy_unavailable", policy_out.get("problem", "No effective policy.")
                )
            )
            return {
                "outcome": "manual_review",
                "findings": findings,
                "recommendation": None,
                "uncertainty": uncertainty,
                "version_ids": [],
                "corrections": [],
            }
        doc = s.get(PolicyVersion, uuid.UUID(policy_out["policy_version_id"]))
        assert doc is not None
        policy = ReconciliationPolicy.model_validate(doc.document)
        if not case["invoice_number"]:
            findings.append(
                _manual_finding(
                    "invoice_reference_missing",
                    "The case does not identify an invoice; add the invoice number.",
                    "needs_information",
                )
            )
            return {
                "outcome": "needs_information",
                "findings": findings,
                "recommendation": None,
                "uncertainty": uncertainty,
                "version_ids": [],
                "corrections": [],
                "policy_ref": policy.ref,
            }
        loaded = load_match_input(s, case["invoice_number"], collected.get("po_number"))
        version_ids = sorted(str(v) for v in loaded.version_ids)
        if loaded.match_input is None:
            reasons = collected.get("unavailable") or loaded.missing
            needs_review_docs = [
                d
                for d in collected.get("documents", [])
                if d["record_type"] == "invoice" and d["parse_status"] != "parsed"
            ]
            if needs_review_docs:
                findings.append(
                    _manual_finding(
                        "invoice_extraction_needs_review",
                        "The invoice document could not be read reliably ("
                        + "; ".join(
                            p for p in collected.get("problems", []) if p.startswith("invoice")
                        )[:400]
                        + "). Its values were not used; a person must review or supply a structured copy.",
                    )
                )
                outcome = "manual_review"
            else:
                findings.append(
                    _manual_finding(
                        "invoice_record_unavailable",
                        f"The invoice record is not available from the authorized sources ({', '.join(reasons)}).",
                        "needs_information",
                    )
                )
                outcome = "needs_information"
            return {
                "outcome": outcome,
                "findings": findings,
                "recommendation": _rec_for(outcome, policy, case),
                "uncertainty": uncertainty,
                "version_ids": version_ids,
                "corrections": [],
                "policy_ref": policy.ref,
            }
        result: MatchResult = reconcile(loaded.match_input, policy)
        rec = recommend(result, policy, case["case_number"])
        uncertainty += [
            f"supplier claim unconfirmed: {f.summary}"
            for f in result.findings
            if f.reason_code == "supplier_claim_unverified"
        ]
        uncertainty += [
            f"corrected value used for {c['field']} ({c['original']} → {c['corrected']})"
            for c in loaded.corrections_applied
        ]
        return {
            "outcome": result.outcome,
            "findings": [f.to_dict() for f in result.findings],
            "recommendation": rec.to_dict(),
            "uncertainty": uncertainty,
            "version_ids": version_ids,
            "corrections": loaded.corrections_applied,
            "policy_ref": result.policy_ref,
            "rules_version": result.rules_version,
        }


def _rec_for(outcome: str, policy: ReconciliationPolicy, case: dict[str, Any]) -> dict[str, Any]:
    route = "manual_review" if outcome == "manual_review" else "missing_receipt"
    spec = policy.action_routes[route]
    return {
        "decision": "escalate" if route == "manual_review" else "request_information",
        "route": route,
        "action_type": spec.action_type,
        "assignee_group": spec.assignee_group,
        "title": spec.title_template.format(case=case["case_number"], po="unknown PO", count=1),
        "finding_ids": [],
    }


# --- explain_findings (template baseline) ---------------------------------------------

_OUTCOME_TEXT = {
    "matched": "No supported discrepancy was found",
    "discrepancies_found": "Discrepancies were found",
    "needs_information": "The investigation is waiting for information",
    "manual_review": "The case needs manual review",
}


def template_explanation(
    *, recon: dict[str, Any], policy: dict[str, Any], case: dict[str, Any]
) -> dict[str, Any]:
    lines = [
        f"{_OUTCOME_TEXT.get(recon['outcome'], recon['outcome'])} under policy "
        f"{policy.get('policy_ref') or 'unavailable'} (decision date {policy.get('decision_date')})."
    ]
    for f in recon["findings"]:
        if f["status"] == "informational" and f["finding_type"] == "note":
            continue
        lines.append(f"- {f['summary']}")
    rec = recon.get("recommendation")
    if rec:
        lines.append(
            f"Recommended next step: {rec['action_type'].replace('_', ' ')}"
            + (f" for {rec['assignee_group']}" if rec.get("assignee_group") else "")
            + f' — "{rec["title"]}". This is follow-up work; it does not resolve the discrepancy.'
        )
    if policy.get("superseded"):
        lines.append(
            "Superseded policy versions not applied: " + ", ".join(policy["superseded"]) + "."
        )
    return {"text": "\n".join(lines), "origin": "template", "model_call_id": None}


# --- finalize -------------------------------------------------------------------------


def _readiness(outcome: str, collected: dict[str, Any]) -> str:
    if collected.get("unavailable") or outcome == "needs_information":
        return "incomplete"
    if outcome == "manual_review" or any(
        d["parse_status"] != "parsed" for d in collected.get("documents", [])
    ):
        return "needs_review"
    return "complete"


def _finalize(
    deps: InvestigationDeps,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    case_snapshot: dict[str, Any],
    collected: dict[str, Any],
    policy_out: dict[str, Any],
    recon: dict[str, Any],
    explained: dict[str, Any],
) -> uuid.UUID:
    from opsbridge.domain.records import SourceRef

    with deps.db.session(_ctx(tenant_id)) as s:
        existing = s.execute(
            select(InvestigationVersion.id).where(InvestigationVersion.run_id == run_id)
        ).scalar_one_or_none()
        if (
            existing is not None
        ):  # idempotent finalize after a crash between commit and job completion
            return existing
        run = s.get(WorkflowRun, run_id)
        case = s.execute(
            select(Case).where(Case.id == uuid.UUID(case_snapshot["case_id"])).with_for_update()
        ).scalar_one()
        assert run is not None
        refs = [SourceRef(**e) for f in recon["findings"] for e in f.get("evidence", [])]
        citations = [c.to_dict(include_excerpt=False) for c in resolve_all(s, refs)]
        version_ids = {uuid.UUID(v) for v in recon.get("version_ids", [])}
        version_ids |= {uuid.UUID(d["version_id"]) for d in collected.get("documents", [])}
        manifest = source_manifest(s, version_ids) + [
            {
                "correction_id": c["correction_id"],
                "field": c["field"],
                "version_id": c["version_id"],
                "locator": c["locator"],
                "corrected": c["corrected"],
            }
            for c in recon.get("corrections", [])
        ]
        next_no = (
            s.execute(
                select(func.max(InvestigationVersion.version)).where(
                    InvestigationVersion.case_id == case.id
                )
            ).scalar()
            or 0
        ) + 1
        policy_vid = policy_out.get("policy_version_id")
        if policy_vid is None:
            raise StepFailed(
                "no effective policy; cannot record an investigation version", retryable=False
            )
        iv = InvestigationVersion(
            tenant_id=tenant_id,
            case_id=case.id,
            version=next_no,
            run_id=run_id,
            outcome=recon["outcome"],
            findings=recon["findings"],
            explanation=explained["text"],
            explanation_origin=explained["origin"],
            recommendation=recon.get("recommendation") or {},
            evidence_readiness=_readiness(recon["outcome"], collected),
            citations=citations,
            uncertainty=recon.get("uncertainty", []),
            policy_version_id=uuid.UUID(policy_vid),
            policy_ref=policy_out["policy_ref"],
            rules_version=recon.get("rules_version", "n/a"),
            source_manifest=manifest,
            source_manifest_digest=digest(manifest),
        )
        s.add(iv)
        s.flush()
        if case.status == "collecting_evidence":
            transition(case, "investigating")
        target = (
            "needs_information" if recon["outcome"] == "needs_information" else "awaiting_review"
        )
        transition(case, target)
        case.discrepancy_types = sorted(
            {
                f["finding_type"]
                for f in recon["findings"]
                if f["finding_type"] in ("missing_receipt", "quantity_mismatch", "price_mismatch")
            }
        )
        case.evidence_readiness = iv.evidence_readiness
        if not case.po_number and collected.get("po_number"):
            case.po_number = collected["po_number"]  # learned from the invoice record
        if recon.get("recommendation", {}) and recon["recommendation"].get("assignee_group"):
            case.assignee_group = recon["recommendation"]["assignee_group"]
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        audit.record(
            s,
            tenant_id=tenant_id,
            actor_kind="service",
            actor_id=SERVICE_IDENTITY,
            on_behalf_of=run.requested_by,
            action="investigation.completed",
            resource_type="case",
            resource_id=str(case.id),
            details={"run_id": str(run_id), "version": next_no, "outcome": recon["outcome"]},
        )
        return iv.id
