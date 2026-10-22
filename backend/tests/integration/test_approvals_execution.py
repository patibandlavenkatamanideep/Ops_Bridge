"""Approval binding, durable execution and recovery (Phase 4 exit gate, OB-11, OB-12).

Every test counts remote tasks in the simulator's own store, so "no duplicate" is
checked against the external system, not inferred from OpsBridge's view.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError

from opsbridge.canonical import digest
from opsbridge.connectors.tasks import SimTaskConnector, TaskConnector, TaskRequest
from opsbridge.persistence.db import Database, DbContext, make_engine
from opsbridge.persistence.models import (
    ApprovalDecision,
    Attempt,
    Operation,
    PolicyVersion,
    Proposal,
)
from opsbridge.privacy.redaction import RedactionResult
from opsbridge.services import proposals as proposals_svc
from opsbridge.workflow import execution, jobs
from opsbridge.workflow.execution import LeaseLost, dispatch
from opsbridge.workflow.worker import Worker

from .stack import MASTER, get, open_case
from .test_api_cases import call

pytestmark = pytest.mark.integration
T = "/api/v1/tenants/demo-furniture"
ORG = "demo-furniture"


# --- helpers ------------------------------------------------------------------------------


def prepared(
    st: dict[str, Any], invoice: str, author: str = "furn-investigator"
) -> tuple[str, dict[str, Any]]:
    case_id = open_case(st, author, "demo-furniture", invoice)
    r = call(
        st["client"],
        st["actors"][author],
        "POST",
        f"{T}/cases/{case_id}/investigations",
        json={"model_mode": "rules_only"},
    )
    assert r.status_code in (200, 202), r.text
    drain(st)
    r = call(st["client"], st["actors"][author], "POST", f"{T}/cases/{case_id}/proposals", json={})
    assert r.status_code == 201, r.text
    return case_id, r.json()


def approve(
    st: dict[str, Any], case_id: str, prop: dict[str, Any], who: str = "furn-approver"
) -> httpx.Response:
    return call(
        st["client"],
        st["actors"][who],
        "POST",
        f"{T}/cases/{case_id}/proposals/{prop['id']}/decision",
        json={
            "decision": "approve",
            "version": prop["version"],
            "payload_digest": prop["payload_digest"],
        },
    )


def drain(st: dict[str, Any], worker: Worker | None = None) -> None:
    w = worker or st["worker"]
    while w.run_once():
        pass


def operations(st: dict[str, Any], case_id: str) -> list[dict[str, Any]]:
    r = get(st, "furn-approver", f"{T}/cases/{case_id}/operations")
    assert r.status_code == 200, r.text
    return list(r.json()["items"])


def remote_tasks(st: dict[str, Any], op: dict[str, Any]) -> list[dict[str, Any]]:
    return list(st["store"].find_tasks(ORG, f"opsbridge-op:{op['operation_key']}"))


def fault(st: dict[str, Any], mode: str, count: int = 1, route: str = "tasks.create") -> None:
    httpx.post(
        f"{st['base']}/_control/faults",
        json={"org": ORG, "route": route, "mode": mode, "count": count},
    ).raise_for_status()


def use_connector(st: dict[str, Any], factory: Any, **overrides: Any) -> None:
    st["exec"]["exec"] = replace(
        st["exec"]["exec"], connector_for=lambda tid, name, op: factory(), **overrides
    )


def sim(st: dict[str, Any], **caps: Any) -> SimTaskConnector:
    return SimTaskConnector(st["base"], MASTER, ORG, **caps)


def expire_attempts_and_leases(worker_db: Database) -> None:
    with worker_db.session(DbContext()) as s:
        s.execute(
            text(
                "UPDATE jobs SET lease_expires_at = now() - interval '1 second', run_after = now() "
                "WHERE status IN ('leased', 'queued')"
            )
        )
    with worker_db.session(
        DbContext()
    ) as s:  # attempts are tenant-scoped; age all tenants' via job tenants
        tenants = s.execute(text("SELECT DISTINCT tenant_id FROM jobs")).scalars().all()
    for t in tenants:
        with worker_db.session(DbContext(tenant_id=t)) as s:
            s.execute(
                text(
                    "UPDATE attempts SET started_at = started_at - interval '1 hour' WHERE status = 'started'"
                )
            )


class SimulatedCrash(BaseException):
    """Stands in for a worker process dying (not an ordinary exception the loop handles)."""


class CrashingConnector:
    def __init__(self, inner: TaskConnector, when: str) -> None:
        self.inner, self.when = inner, when
        self.name, self.version, self.capabilities = inner.name, inner.version, inner.capabilities

    def create(self, req: TaskRequest, key: str | None, timeout: float) -> Any:
        if self.when == "before":
            raise SimulatedCrash("worker died before the remote call")
        self.inner.create(req, key, timeout)
        raise SimulatedCrash("worker died after the remote commit, before recording it")

    def get(self, ref: str, timeout: float) -> Any:
        return self.inner.get(ref, timeout)

    def find_by_marker(self, marker: str, timeout: float) -> Any:
        return self.inner.find_by_marker(marker, timeout)


@pytest.fixture(autouse=True)
def _reset(stack):  # type: ignore[no-untyped-def]
    original = stack["exec"]["exec"]
    httpx.delete(f"{stack['base']}/_control/faults")
    yield
    stack["exec"]["exec"] = original
    httpx.delete(f"{stack['base']}/_control/faults")


# --- happy path and outcome separation ------------------------------------------------------


def test_approved_follow_up_is_created_once_confirmed_and_case_stays_open(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    assert prop["status"] == "awaiting_approval" and prop["payload_digest"].startswith("sha256:")
    assert "Does not change the invoice" in prop["effect_summary"]
    r = approve(st, case_id, prop)
    assert r.status_code == 200, r.text
    assert r.json()["operation"]["status"] == "pending_dispatch"
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and op["confirmation"]["method"] == "read_back"
    assert [(a["kind"], a["status"]) for a in op["attempts"]] == [
        ("dispatch", "succeeded"),
        ("readback", "succeeded"),
    ]
    assert len(remote_tasks(st, op)) == 1
    case = get(st, "furn-investigator", f"{T}/cases/{case_id}").json()
    assert case["status"] == "follow_up_pending"  # follow-up done, exception NOT resolved
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{T}/cases/{case_id}/resolve",
        json={},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "resolution_evidence_missing"


NUMERIC_PO = "4500012345"  # a bare 10-digit purchase order number (SAP-style)


def seed_numeric_po_case(st: dict[str, Any]) -> str:
    """INV-9001's facts under invoice INV-9451 and PO 4500012345 (own receipt ids, so the
    module's other INV-9001 records stay untouched); returns the invoice number."""
    from opsbridge.config import REPO_ROOT
    from opsbridge.simulator.scenarios import load_corpus, render

    base = next(
        sc
        for sc in load_corpus(REPO_ROOT / "fixtures" / "scenarios" / "dev.yaml").scenarios
        if sc.invoice.number == "INV-9001"
    )
    numeric = base.model_copy(
        update={
            "po": base.po.model_copy(update={"number": NUMERIC_PO}),  # type: ignore[union-attr]
            "receipts": [
                r.model_copy(update={"id": f"GR-9451{i}"}) for i, r in enumerate(base.receipts)
            ],
            "invoice": base.invoice.model_copy(
                update={"number": "INV-9451", "po_number": NUMERIC_PO}
            ),
        }
    )
    for collection, key, body, ctype, po_index in render(numeric):
        st["store"].put_record(ORG, collection, key, body, ctype, po_index)
    return "INV-9451"


def test_a_bare_numeric_po_number_stays_readable_in_the_created_task(stack) -> None:  # type: ignore[no-untyped-def]
    """Redaction before the external write keeps the case's own identifiers: a 10-digit PO
    number looks like a phone number but must reach the follow-up task's title."""
    st = stack
    case_id, prop = prepared(st, seed_numeric_po_case(st))
    assert NUMERIC_PO in prop["parameters"]["title"]
    assert approve(st, case_id, prop).status_code == 200
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed"
    [task] = remote_tasks(st, op)
    assert NUMERIC_PO in task["title"] and "[REDACTED" not in task["title"]


# --- approval authority ---------------------------------------------------------------------


def test_self_approval_is_rejected_by_separation_of_duties(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9003", author="multi-member")  # investigator + approver roles
    r = approve(st, case_id, prop, who="multi-member")
    assert r.status_code == 403 and r.json()["error"]["code"] == "separation_of_duties"
    assert approve(st, case_id, prop).status_code == 200


def test_approver_scope_and_role_are_enforced(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9005")
    r = approve(st, case_id, prop, who="furn-approver2")  # scope: record_review_note only
    assert r.status_code == 403 and r.json()["error"]["code"] == "scope"
    r = approve(st, case_id, prop, who="furn-investigator")  # no proposal.decide permission
    assert r.status_code == 403
    r = approve(st, case_id, prop, who="furn-admin")  # admin role does not imply approval authority
    assert r.status_code == 403


def test_approval_must_present_the_reviewed_version_and_digest(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9012")
    bad = {**prop, "payload_digest": "sha256:" + "f" * 64}
    r = approve(st, case_id, bad)
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_version"
    # Editing creates version 2; approving what you saw as version 1 fails.
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "PUT",
        f"{T}/cases/{case_id}/proposals/{prop['id']}",
        json={
            "expected_payload_digest": prop["payload_digest"],
            "title": "Provide receiving record for PO-1012",
        },
    )
    assert r.status_code == 201 and r.json()["version"] == 2
    v2 = r.json()
    assert approve(st, case_id, prop).status_code == 409
    assert approve(st, case_id, v2).status_code == 200


# --- stale inputs between approval and dispatch ----------------------------------------------


def test_payload_edited_after_approval_cannot_be_dispatched(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9014")
    assert approve(st, case_id, prop).status_code == 200
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "PUT",
        f"{T}/cases/{case_id}/proposals/{prop['id']}",
        json={"expected_payload_digest": prop["payload_digest"], "title": "Changed after approval"},
    )
    assert r.status_code == 201
    drain(st)
    [op] = operations(st, case_id)
    assert (
        op["status"] == "cancelled_before_dispatch"
        and op["failure_category"] == "precondition_failed"
    )
    assert op["attempts"] == [] and remote_tasks(st, op) == []


def test_source_change_before_dispatch_rejects_the_approval(stack, worker_db: Database) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9016")
    assert approve(st, case_id, prop).status_code == 200
    # New evidence arrives (a receipt correction) and a fresh investigation is recorded.
    docs = get(st, "furn-investigator", f"{T}/cases/{case_id}/documents").json()["items"]
    gr = next(d for d in docs if d["kind"] == "goods_receipt")
    detail = get(
        st,
        "furn-investigator",
        f"{T}/cases/{case_id}/documents/versions/{gr['versions'][0]['version_id']}",
    ).json()
    fld = next(f for f in detail["fields"] if f["field_path"] == "receipt_line.quantity")
    assert (
        call(
            st["client"],
            st["actors"]["furn-investigator"],
            "POST",
            f"{T}/cases/{case_id}/fields/{fld['field_id']}/corrections",
            json={"new_value": "20", "reason": "Second delivery note found"},
        ).status_code
        == 201
    )
    # Dispatch is still queued; process only the re-investigation first.
    with worker_db.session(DbContext()) as s:
        s.execute(
            text(
                "UPDATE jobs SET run_after = now() + interval '1 hour' WHERE kind = 'dispatch_operation' "
                "AND status = 'queued'"
            )
        )
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{T}/cases/{case_id}/investigations",
        json={"model_mode": "rules_only"},
    )
    assert r.status_code in (200, 202), r.text
    drain(st)
    with worker_db.session(DbContext()) as s:
        s.execute(
            text(
                "UPDATE jobs SET run_after = now() WHERE kind = 'dispatch_operation' AND status = 'queued'"
            )
        )
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "cancelled_before_dispatch"
    assert remote_tasks(st, op) == []


def test_revoked_approver_or_expired_approval_cannot_dispatch(
    stack, migrator_engine: Engine, worker_db: Database
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_a, prop_a = prepared(st, "INV-9001")
    case_b, prop_b = prepared(st, "INV-9003")
    assert approve(st, case_a, prop_a).status_code == 200
    assert approve(st, case_b, prop_b, who="multi-member").status_code == 200
    # Approver A loses approval authority before dispatch.
    with migrator_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE memberships SET approval_scope = '{record_review_note}' FROM users "
                "WHERE memberships.user_id = users.id AND users.username = 'furn-approver'"
            )
        )
    # Approval B expires before dispatch.
    tid = _tenant(worker_db)
    with worker_db.session(DbContext(tenant_id=tid)) as s:
        s.execute(
            text(
                "UPDATE approval_decisions SET expires_at = now() - interval '1 second' "
                "WHERE proposal_id = :p"
            ),
            {"p": prop_b["id"]},
        )
    try:
        drain(st)
    finally:
        with migrator_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE memberships SET approval_scope = '{create_follow_up_task,assign_case,"
                    "create_supplier_message_draft,record_review_note}' FROM users WHERE "
                    "memberships.user_id = users.id AND users.username = 'furn-approver'"
                )
            )
    [op_a], [op_b] = operations(st, case_a), operations(st, case_b)
    assert op_a["status"] == op_b["status"] == "cancelled_before_dispatch"
    assert remote_tasks(st, op_a) == [] and remote_tasks(st, op_b) == []
    with worker_db.session(DbContext(tenant_id=tid)) as s:
        details = (
            s.execute(
                text(
                    "SELECT details->>'detail' FROM audit_events WHERE action = "
                    "'operation.cancelled_before_dispatch' ORDER BY seq DESC LIMIT 2"
                )
            )
            .scalars()
            .all()
        )
    assert any("approver no longer holds" in d for d in details)
    assert any("approval expired" in d for d in details)


def _tenant(worker_db: Database) -> uuid.UUID:
    with worker_db.session(DbContext()) as s:
        return s.execute(
            text("SELECT tenant_id FROM jobs WHERE kind = 'dispatch_operation' LIMIT 1")
        ).scalar_one()


# --- delivery semantics and concurrency -------------------------------------------------------


def test_duplicate_delivery_after_completion_is_a_no_op(stack, worker_db: Database) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    op_id = approve(st, case_id, prop).json()["operation"]["id"]
    drain(st)
    tid = _tenant(worker_db)
    with worker_db.session(
        DbContext(tenant_id=tid)
    ) as s:  # the outbox publishes the same event again
        jobs.enqueue(s, tenant_id=tid, kind="dispatch_operation", payload={"operation_id": op_id})
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and len(remote_tasks(st, op)) == 1
    assert len([a for a in op["attempts"] if a["kind"] == "dispatch"]) == 1


def test_two_concurrent_workers_produce_one_operation_and_one_task(
    stack, worker_db: Database
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    op_id = approve(st, case_id, prop).json()["operation"]["id"]
    tid = _tenant(worker_db)
    with worker_db.session(DbContext(tenant_id=tid)) as s:
        jobs.enqueue(s, tenant_id=tid, kind="dispatch_operation", payload={"operation_id": op_id})
    workers = [Worker(db=worker_db, settings=st["settings"]) for _ in range(2)]
    for w in workers:
        w.register("dispatch_operation", lambda job: dispatch(st["exec"]["exec"], job))
    barrier = threading.Barrier(2)

    def run(w: Worker) -> None:
        barrier.wait()
        for _ in range(5):
            w.run_once()

    threads = [threading.Thread(target=run, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    with worker_db.session(DbContext()) as s:
        s.execute(text("UPDATE jobs SET run_after = now() WHERE status = 'queued'"))
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and len(remote_tasks(st, op)) == 1
    with worker_db.session(DbContext(tenant_id=tid)) as s:
        approvals = s.execute(
            select(func.count())
            .select_from(ApprovalDecision)
            .where(ApprovalDecision.proposal_id == uuid.UUID(prop["id"]))
        ).scalar_one()
        ops = s.execute(
            select(func.count())
            .select_from(Operation)
            .where(Operation.proposal_id == uuid.UUID(prop["id"]))
        ).scalar_one()
    assert (approvals, ops) == (1, 1)


def test_stale_worker_cannot_write_after_losing_its_lease(stack, worker_db: Database) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    approve(st, case_id, prop)
    stale = jobs.claim(worker_db, "stale-worker", 30, ["dispatch_operation"])
    assert stale is not None
    with worker_db.session(DbContext()) as s:
        s.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": stale.id},
        )
    drain(st)  # the new lease holder completes the operation
    with pytest.raises(LeaseLost):
        dispatch(st["exec"]["exec"], stale)
    assert jobs.complete(worker_db, stale) is False
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and len(remote_tasks(st, op)) == 1


# --- ambiguous outcomes and crash recovery -----------------------------------------------------


def test_timeout_after_commit_reconciles_instead_of_recreating(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    use_connector(st, lambda: sim(st), timeout_seconds=1.0)
    case_id, prop = prepared(st, "INV-9001")
    approve(st, case_id, prop)
    fault(
        st, "timeout_after_commit"
    )  # the simulator commits, then answers after 3 s; client gives up at 1 s
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and op["confirmation"]["method"] == "reconciled_by_marker"
    assert [(a["kind"], a["status"]) for a in op["attempts"]] == [
        ("dispatch", "ambiguous_failure"),
        ("reconcile", "succeeded"),
    ]
    assert op["attempts"][1]["parent_attempt_id"] == op["attempts"][0]["id"]
    assert len(remote_tasks(st, op)) == 1


def test_no_idempotency_and_no_lookup_yields_outcome_unknown_and_review(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    use_connector(st, lambda: sim(st, idempotency_keys=False, lookup="none"), timeout_seconds=1.0)
    case_id, prop = prepared(st, "INV-9001")
    approve(st, case_id, prop)
    fault(st, "timeout_after_commit")
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "outcome_unknown" and op["review_required"] is True
    assert op["failure_category"] == "unreconcilable"
    assert len(remote_tasks(st, op)) == 1  # it did happen, and was NOT blindly re-created
    # A person checks the task system and records what they found.
    ref = remote_tasks(st, op)[0]["id"]
    r = call(
        st["client"],
        st["actors"]["furn-approver"],
        "POST",
        f"{T}/cases/{case_id}/operations/{op['id']}/review",
        json={"finding": "exists", "remote_ref": ref, "note": "Found the task in the tracker"},
    )
    assert r.status_code == 200 and r.json()["status"] == "confirmed"


def test_ambiguous_500_before_commit_is_reconciled_as_absent_then_retried(
    stack, worker_db: Database
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    approve(st, case_id, prop)
    fault(st, "fail_500")  # nothing committed, but the client cannot know that
    drain(st)
    expire_attempts_and_leases(worker_db)
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and len(remote_tasks(st, op)) == 1
    kinds = [(a["kind"], a["status"]) for a in op["attempts"]]
    assert kinds[:2] == [("dispatch", "ambiguous_failure"), ("reconcile", "succeeded")]


def test_definite_failures_retry_or_fail_without_duplicates(stack, worker_db: Database) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_a, prop_a = prepared(st, "INV-9001")
    approve(st, case_a, prop_a)
    fault(st, "fail_503_not_committed")
    drain(st)
    expire_attempts_and_leases(worker_db)
    drain(st)
    [op_a] = operations(st, case_a)
    assert op_a["status"] == "confirmed" and len(remote_tasks(st, op_a)) == 1
    assert op_a["attempts"][0]["status"] == "definite_failure"

    case_b, prop_b = prepared(st, "INV-9003")
    approve(st, case_b, prop_b)
    fault(st, "fail_400")
    drain(st)
    [op_b] = operations(st, case_b)
    assert op_b["status"] == "failed" and op_b["failure_category"] == "rejected_by_provider"
    assert remote_tasks(st, op_b) == []


@pytest.mark.parametrize("when", ["before", "after"])
def test_worker_crash_before_or_after_remote_commit_recovers_without_duplicate(
    stack,
    worker_db: Database,
    when: str,  # type: ignore[no-untyped-def]
) -> None:
    st = stack
    invoice = "INV-9001" if when == "before" else "INV-9003"
    case_id, prop = prepared(st, invoice)
    approve(st, case_id, prop)
    use_connector(st, lambda: CrashingConnector(sim(st), when))
    with pytest.raises(SimulatedCrash):
        st["worker"].run_once()  # the process "dies" mid-dispatch
    with worker_db.session(DbContext(tenant_id=_tenant(worker_db))) as s:
        started = s.execute(select(Attempt).where(Attempt.status == "started")).scalars().all()
        assert len(started) == 1  # the attempt was committed before the remote call
    use_connector(st, lambda: sim(st))
    expire_attempts_and_leases(worker_db)
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and len(remote_tasks(st, op)) == 1
    assert ("reconcile", "succeeded") in [(a["kind"], a["status"]) for a in op["attempts"]]
    assert op["confirmation"]["method"] == (
        "reconciled_by_marker" if when == "after" else "read_back"
    )


# --- cancellation and local actions -------------------------------------------------------------


def test_cancellation_before_dispatch_and_too_late(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9015")
    op = approve(st, case_id, prop).json()["operation"]
    r = call(
        st["client"],
        st["actors"]["furn-approver"],
        "POST",
        f"{T}/cases/{case_id}/operations/{op['id']}/cancel",
    )
    assert r.status_code == 200 and r.json()["cancellation"] == "requested"
    drain(st)
    [done] = operations(st, case_id)
    assert (done["status"], done["cancellation"]) == ("cancelled_before_dispatch", "confirmed")
    assert remote_tasks(st, done) == []
    r = call(
        st["client"],
        st["actors"]["furn-approver"],
        "POST",
        f"{T}/cases/{case_id}/operations/{op['id']}/cancel",
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "too_late"


# --- a retry needs current eligibility for a new write; recovery of a landed one does not ----


def _expire_approval(worker_db: Database, prop: dict[str, Any]) -> None:
    with worker_db.session(DbContext(tenant_id=_tenant(worker_db))) as s:
        s.execute(
            text(
                "UPDATE approval_decisions SET expires_at = now() - interval '1 second' "
                "WHERE proposal_id = :p"
            ),
            {"p": prop["id"]},
        )


def _publish_policy_v3(worker_db: Database) -> None:
    mdb = Database(make_engine("migrator", database="opsbridge_test"))
    try:
        with mdb.session(DbContext(tenant_id=_tenant(worker_db))) as s:
            v2 = s.execute(select(PolicyVersion).where(PolicyVersion.version == 2)).scalar_one()
            s.add(
                PolicyVersion(
                    tenant_id=v2.tenant_id,
                    policy_id=v2.policy_id,
                    version=3,
                    effective_from=date(2026, 6, 1),
                    document={**v2.document, "version": 3, "effective_from": "2026-06-01"},
                    sha256="sha256:" + "3" * 64,
                )
            )
    finally:
        mdb.engine.dispose()


SCOPE = (
    "UPDATE memberships SET approval_scope = :scope FROM users "
    "WHERE memberships.user_id = users.id AND users.username = 'furn-approver'"
)
FULL_SCOPE = "{create_follow_up_task,assign_case,create_supplier_message_draft,record_review_note}"


@pytest.mark.parametrize("change", ["expired", "revoked", "cancelled", "edited", "policy"])
@pytest.mark.parametrize("first", ["fail_503_not_committed", "fail_500"])
def test_a_retry_rechecks_eligibility_before_any_new_create(
    stack, migrator_engine: Engine, worker_db: Database, first: str, change: str
) -> None:  # type: ignore[no-untyped-def]
    """The first create is rejected, or fails ambiguously and is proved absent, so the
    operation stays `executing` with no effect. A retry would be a new write: it must pass
    the same checks as the first dispatch."""
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    op_id = approve(st, case_id, prop).json()["operation"]["id"]
    fault(st, first)
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "executing" and remote_tasks(st, op) == []
    try:
        if change == "expired":
            _expire_approval(worker_db, prop)
        elif change == "revoked":
            with migrator_engine.begin() as conn:
                conn.execute(text(SCOPE), {"scope": "{record_review_note}"})
        elif change == "cancelled":
            r = call(
                st["client"],
                st["actors"]["furn-approver"],
                "POST",
                f"{T}/cases/{case_id}/operations/{op_id}/cancel",
            )
            assert r.status_code == 200, r.text
        elif change == "edited":
            r = call(
                st["client"],
                st["actors"]["furn-investigator"],
                "PUT",
                f"{T}/cases/{case_id}/proposals/{prop['id']}",
                json={"expected_payload_digest": prop["payload_digest"], "title": "Edited later"},
            )
            assert r.status_code == 201, r.text
        else:
            _publish_policy_v3(worker_db)
        expire_attempts_and_leases(worker_db)
        drain(st)
    finally:
        with migrator_engine.begin() as conn:
            conn.execute(text(SCOPE), {"scope": FULL_SCOPE})
    [op] = operations(st, case_id)
    assert remote_tasks(st, op) == []
    assert [a["kind"] for a in op["attempts"]].count("dispatch") == 1  # no second create
    assert op["status"] == "cancelled_before_dispatch"
    if change == "cancelled":
        assert op["cancellation"] == "confirmed"
    else:
        assert op["failure_category"] == "precondition_failed"


def test_a_create_that_landed_is_confirmed_even_after_the_approval_expires(
    stack, worker_db: Database
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9003")
    approve(st, case_id, prop)
    use_connector(st, lambda: CrashingConnector(sim(st), "after"))
    with pytest.raises(SimulatedCrash):
        st["worker"].run_once()  # the task exists remotely; OpsBridge never heard back
    _expire_approval(worker_db, prop)
    use_connector(st, lambda: sim(st))
    expire_attempts_and_leases(worker_db)
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and op["confirmation"]["method"] == "reconciled_by_marker"
    assert len(remote_tasks(st, op)) == 1


# --- the approved destination and outgoing text --------------------------------------------

IT = "/api/v1/tenants/demo-it"


def _it_proposal(st: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """An IT-tenant follow-up proposal; that tier may use the simulator or GitHub."""
    who = st["actors"]["it-investigator"]
    case_id = open_case(st, "it-investigator", "demo-it", "INV-7002")
    run = f"{IT}/cases/{case_id}/investigations"
    assert call(st["client"], who, "POST", run, json={"model_mode": "rules_only"}).status_code in (
        200,
        202,
    )
    drain(st)
    r = call(st["client"], who, "POST", f"{IT}/cases/{case_id}/proposals", json={})
    assert r.status_code == 201, r.text
    return case_id, r.json()


def _approve_it(st: dict[str, Any], case_id: str, prop: dict[str, Any]) -> httpx.Response:
    return call(
        st["client"],
        st["actors"]["it-approver"],
        "POST",
        f"{IT}/cases/{case_id}/proposals/{prop['id']}/decision",
        json={
            "decision": "approve",
            "version": prop["version"],
            "payload_digest": prop["payload_digest"],
        },
    )


def _set_it_connector(app_db: Database, migrator_engine: Engine, name: str) -> None:
    """What a tenant administrator's settings change does."""
    with app_db.session(DbContext(tenant_id=_tenant_of(migrator_engine, "demo-it"))) as s:
        assert (
            s.execute(text("UPDATE tenant_settings SET task_connector = :n"), {"n": name}).rowcount
            == 1
        )  # type: ignore[attr-defined]


def test_a_settings_change_after_drafting_cannot_redirect_the_approval(
    stack, app_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = _it_proposal(st)
    _set_it_connector(app_db, migrator_engine, "github_issues")
    r = _approve_it(st, case_id, prop)
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_inputs", r.text
    assert "the destination changed" in " ".join(r.json()["error"]["details"]["reasons"])
    assert prop["destination"] == {"connector": "sim_tasks", "organization": "demo-it"}
    assert prop["outbound"]["title"] and "sim_tasks (demo-it)" in prop["effect_summary"]


@pytest.mark.parametrize("change", ["connector", "redaction"])
def test_dispatch_refuses_when_destination_or_outgoing_text_changed_after_approval(
    stack, app_db: Database, migrator_engine: Engine, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = _it_proposal(st)
    assert _approve_it(st, case_id, prop).status_code == 200
    if change == "connector":
        _set_it_connector(app_db, migrator_engine, "github_issues")
    else:  # redaction rules changed: the approved text is no longer what would be sent
        monkeypatch.setattr(
            proposals_svc, "redact", lambda t, keep=(): RedactionResult(t + " [rule 3]", {})
        )
    drain(st)
    r = get(st, "it-approver", f"{IT}/cases/{case_id}/operations")
    [op] = r.json()["items"]
    assert (op["status"], op["failure_category"]) == (
        "cancelled_before_dispatch",
        "precondition_failed",
    )
    assert op["attempts"] == []
    assert list(st["store"].find_tasks("demo-it", f"opsbridge-op:{op['operation_key']}")) == []


def test_a_proposal_from_before_destination_binding_must_be_proposed_again(
    stack, app_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    with app_db.session(DbContext(tenant_id=_tenant_of(migrator_engine, ORG))) as s:
        pr = s.get(Proposal, uuid.UUID(prop["id"]))
        assert pr is not None
        pr.destination = pr.outbound = None  # as stored before migration 0011
        pr.payload_digest = old_digest = digest(proposals_svc.payload_of(pr))
    r = approve(st, case_id, {**prop, "payload_digest": old_digest})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_inputs", r.text
    assert "before destinations were bound" in " ".join(r.json()["error"]["details"]["reasons"])


def _tenant_of(migrator_engine: Engine, slug: str) -> uuid.UUID:
    with migrator_engine.begin() as conn:
        tid: uuid.UUID = conn.execute(
            text("SELECT id FROM tenants WHERE slug = :s"), {"s": slug}
        ).scalar_one()
    return tid


def test_local_review_note_runs_through_the_same_ledger(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(
        st, "INV-9024"
    )  # amount-mismatch invoice → manual review → assign_case
    assert prop["action_type"] == "assign_case"
    r = approve(st, case_id, prop, who="furn-approver2")  # scope: record_review_note only
    assert r.status_code == 403
    assert approve(st, case_id, prop).status_code == 200
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and op["connector"] == "opsbridge_local"
    assert op["confirmation"]["method"] == "local_transaction"
    case = get(st, "furn-investigator", f"{T}/cases/{case_id}").json()
    assert case["assignee_group"] == "ap-review"


def test_a_local_action_takes_effect_once_even_if_the_worker_dies_while_completing(
    stack, worker_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """The draft, its attempt and the operation's confirmation commit together: a worker that
    dies before confirming leaves nothing behind, and the retry writes exactly one draft."""
    st = stack
    case_id, prop = prepared(st, "INV-9009")
    assert prop["action_type"] == "create_supplier_message_draft"
    assert approve(st, case_id, prop).status_code == 200
    real = execution._finish

    def dies_confirming(s: Any, op: Any, status: str, **kw: Any) -> None:
        if status == "confirmed":
            raise SimulatedCrash("worker died while confirming the operation")
        real(s, op, status, **kw)

    def drafts() -> list[dict[str, Any]]:
        items = get(st, "furn-investigator", f"{T}/cases/{case_id}/messages").json()["items"]
        return [m for m in items if (m["body"] or "").startswith("Supplier message DRAFT")]

    monkeypatch.setattr(execution, "_finish", dies_confirming)
    with pytest.raises(SimulatedCrash):
        st["worker"].run_once()
    monkeypatch.setattr(execution, "_finish", real)
    assert drafts() == []  # nothing is applied without the operation's confirmation
    expire_attempts_and_leases(worker_db)
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and len(drafts()) == 1
    assert [(a["kind"], a["status"]) for a in op["attempts"]] == [("dispatch", "succeeded")]
    # The database also refuses a second applied local action for the operation.
    with pytest.raises(IntegrityError, match="uq_attempts_one_local_effect"):
        with worker_db.session(DbContext(tenant_id=_tenant(worker_db))) as s:
            done = s.execute(select(Attempt).where(Attempt.operation_id == op["id"])).scalar_one()
            cols = [c.key for c in Attempt.__mapper__.column_attrs if c.key != "id"]
            s.add(Attempt(**{c: getattr(done, c) for c in cols}))


def test_remote_that_stops_honouring_idempotency_goes_to_review_not_a_duplicate(stack) -> None:  # type: ignore[no-untyped-def]
    """Found by the demo test (Phase 9): the connector hard-coded idempotency support, so when
    the tracker stopped honouring keys and lookup was unavailable, a retry after a timeout
    created a second task. Capabilities are now discovered from the tracker."""
    st = stack
    st["store"].set_capabilities(ORG, False, False)
    try:
        case_id, prop = prepared(st, "INV-9001")
        assert approve(st, case_id, prop).status_code == 200
        fault(st, "timeout_after_commit")
        drain(st)
    finally:
        st["store"].set_capabilities(ORG, True, True)
        httpx.delete(f"{st['base']}/_control/faults")
    [op] = operations(st, case_id)
    assert (op["status"], op["failure_category"]) == ("outcome_unknown", "unreconcilable")
    assert op["review_required"] is True
    assert len(remote_tasks(st, op)) == 1  # committed once; never re-sent


def test_capabilities_are_discovered_and_unknown_means_weakest(stack) -> None:  # type: ignore[no-untyped-def]
    from opsbridge.connectors.tasks import SimTaskConnector

    from .stack import MASTER

    st = stack
    caps = SimTaskConnector(st["base"], MASTER, ORG).capabilities
    assert (caps.idempotency_keys, caps.lookup) == (True, "strong")
    st["store"].set_capabilities(ORG, False, False)
    try:
        caps = SimTaskConnector(st["base"], MASTER, ORG).capabilities
        assert (caps.idempotency_keys, caps.lookup) == (False, "none")
    finally:
        st["store"].set_capabilities(ORG, True, True)
    unknown = SimTaskConnector(st["base"], "wrong-secret", ORG).capabilities  # discovery refused
    assert (unknown.idempotency_keys, unknown.lookup) == (False, "none")
    explicit = SimTaskConnector(
        st["base"], "wrong-secret", ORG, idempotency_keys=True, lookup="eventual"
    )
    assert (explicit.capabilities.idempotency_keys, explicit.capabilities.lookup) == (
        True,
        "eventual",
    )


def test_attempt_requests_and_responses_are_kept_and_match_their_digests(
    stack,
    worker_db: Database,  # type: ignore[no-untyped-def]
    migrator_engine: Engine,
) -> None:
    import json

    from opsbridge.evidence.storage import LocalArtifactStore
    from opsbridge.persistence.models import Artifact

    from .test_hardening import tenant_id

    st = stack
    case_id, prop = prepared(st, "INV-9001")
    assert approve(st, case_id, prop).status_code == 200
    drain(st)
    [op] = operations(st, case_id)
    tid = tenant_id(migrator_engine, ORG)
    store = LocalArtifactStore(st["settings"].artifact_dir)
    with worker_db.session(DbContext(tenant_id=tid)) as s:
        attempts = list(
            s.execute(select(Attempt).where(Attempt.operation_id == uuid.UUID(op["id"]))).scalars()
        )
        assert {a.kind for a in attempts} == {"dispatch", "readback"}
        for a in attempts:
            req = s.get(Artifact, a.input_artifact_id)
            assert req is not None and req.kind == "tool_request" and req.sha256 == a.input_digest
            assert json.loads(store.get(tid, req.storage_key))  # the exact request is retrievable
            resp = s.get(Artifact, a.output_artifact_id)
            assert (
                resp is not None and resp.kind == "tool_response" and resp.sha256 == a.output_digest
            )
            assert json.loads(store.get(tid, resp.storage_key))["remote_ref"] == op["remote_ref"]
        # The dispatch request is the complete request as sent, body and destination included.
        [create] = [a for a in attempts if a.kind == "dispatch"]
        req = s.get(Artifact, create.input_artifact_id)
        assert req is not None
        sent = json.loads(store.get(tid, req.storage_key))
    [task] = remote_tasks(st, op)
    assert sent["body"] == task["body"] and sent["title"] == task["title"]
    assert sent["destination"] == {"connector": "sim_tasks", "organization": ORG}
