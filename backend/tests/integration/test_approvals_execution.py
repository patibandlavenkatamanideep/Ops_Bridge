"""Approval binding, durable execution and recovery (Phase 4 exit gate, OB-11, OB-12).

Every test counts remote tasks in the simulator's own store, so "no duplicate" is
checked against the external system, not inferred from OpsBridge's view.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select, text

from opsbridge.connectors.tasks import SimTaskConnector, TaskConnector, TaskRequest
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import ApprovalDecision, Attempt, Operation
from opsbridge.workflow import jobs
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
