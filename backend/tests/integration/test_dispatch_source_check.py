"""Dispatch asks the purchasing system before a new external write (workflow/source_check.py).

Local freshness cannot see a change that the purchasing system holds but OpsBridge has not
read. Here the change is made upstream only, in the local simulator, and the case is NOT
re-run: before the follow-up task would be created, the executor asks the source. Changed or
unverifiable means the outbound connector is never called. Synthetic records; rules only.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select

from opsbridge.connectors.exports import ExportSource
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import AuditEvent, ReceivingEventRecord
from opsbridge.pilot.receiving_pack import render_case
from opsbridge.pilot.rehearsal import write_export

from .stack import get
from .test_approvals_execution import ORG, T, approve, drain, operations, remote_tasks
from .test_receiving_disposition import (
    hold_dispatch,
    investigate,
    late,
    opened,
    posted_for_twenty,
    propose,
    put,
    tenant_id,
    variant,
    view,
)

pytestmark = pytest.mark.integration


class Spy:
    """Stands in for the outbound connector factory and the purchasing source: records each
    call. The connector side must never be reached when the source check says no."""

    def __init__(self, real: Any = None) -> None:
        self.calls: list[Any] = []
        self.real = real

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        if self.real is None:
            raise AssertionError(f"called with {args!r}")
        return self.real(*args)


def spy_on_connector(st: dict[str, Any]) -> Spy:
    spy = Spy(st["exec"]["exec"].connector_for)
    st["exec"]["exec"] = replace(st["exec"]["exec"], connector_for=spy)
    return spy


def approved_and_held(
    st: dict[str, Any], worker_db: Database, n: str
) -> tuple[Any, str, dict[str, Any], dict[str, Any]]:
    """A receipt for 20 accepted, the follow-up approved, its dispatch held back."""
    case = posted_for_twenty(n)
    case_id, first = opened(st, case)
    assert first["recommendation"]["route"] == "quantity_mismatch"
    prop = propose(st, case_id)
    assert prop["action_type"] == "create_follow_up_task"
    assert approve(st, case_id, prop).status_code == 200
    hold_dispatch(worker_db)
    return case, case_id, first, prop


def dispatch_now(st: dict[str, Any], worker_db: Database) -> None:
    hold_dispatch(worker_db, hold=False)
    drain(st)


def audit(engine: Engine, op_id: str, action: str) -> list[dict[str, Any]]:
    with Database(engine).session(DbContext(tenant_id=tenant_id(engine))) as s:
        return [
            dict(d)
            for d in s.execute(
                select(AuditEvent.details)
                .where(AuditEvent.resource_id == op_id, AuditEvent.action == action)
                .order_by(AuditEvent.seq)
            ).scalars()
        ]


def proposal_status(st: dict[str, Any], case_id: str, prop_id: str) -> str:
    items = get(st, "furn-investigator", f"{T}/cases/{case_id}/proposals").json()["items"]
    [status] = [p["status"] for p in items if p["id"] == prop_id]
    return str(status)


def test_a_rejection_recorded_upstream_only_never_reaches_the_outbound_connector(
    stack: dict[str, Any], worker_db: Database, migrator_engine: Engine
) -> None:
    st = stack
    case, case_id, first, prop = approved_and_held(st, worker_db, "4801")

    # The purchasing system records a rejection of 2. OpsBridge has not read it, and the case
    # is not re-run: locally the evidence is exactly what was approved.
    put(st, late(case))
    connector = spy_on_connector(st)
    dispatch_now(st, worker_db)

    [op] = operations(st, case_id)
    assert (op["status"], op["failure_category"]) == ("cancelled_before_dispatch", "source_changed")
    assert connector.calls == []  # the outbound connector was never even built
    assert op["attempts"] == [] and remote_tasks(st, op) == []
    assert proposal_status(st, case_id, prop["id"]) == "superseded"
    [refused] = audit(migrator_engine, op["id"], "operation.cancelled_before_dispatch")
    assert refused["category"] == "source_changed"
    assert refused["detail"] == (
        "nothing was sent: the purchasing system changed after the investigation (receiving "
        "event PO-4801-EVT-LATE is new); re-run the investigation to read it"
    )

    # The record is named as the source files it (the events file holding REJ-4801), as its
    # document is titled once read. Nothing was read into OpsBridge by the check: the
    # rejection is still only upstream.
    with Database(migrator_engine).session(DbContext(tenant_id=tenant_id(migrator_engine))) as s:
        assert (
            s.execute(
                select(ReceivingEventRecord.id).where(ReceivingEventRecord.event_id == "REJ-4801")
            ).all()
            == []
        )
    current = get(st, "furn-investigator", f"{T}/cases/{case_id}/investigation").json()
    assert current["version"] == first["version"]

    # The Receiving tab says why nothing was sent.
    [since] = view(st, case_id)["since_approval"]
    assert since["changes"] == []
    assert since["requires"] == "review: " + refused["detail"]

    # Re-running reads the rejection; the case is back for review on 18 accepted, 2 rejected.
    latest = investigate(st, case_id)
    assert "disposition_unrecorded" in [f["reason_code"] for f in latest["findings"]]


def test_with_the_source_unchanged_the_same_follow_up_is_sent_once(
    stack: dict[str, Any], worker_db: Database, migrator_engine: Engine
) -> None:
    """The control: the check passes, the connector is called, the task is created."""
    st = stack
    _case, case_id, _first, _prop = approved_and_held(st, worker_db, "4802")
    connector = spy_on_connector(st)
    dispatch_now(st, worker_db)

    [op] = operations(st, case_id)
    assert op["status"] == "confirmed"
    assert len(connector.calls) == 1 and len(remote_tasks(st, op)) == 1
    [verified] = audit(migrator_engine, op["id"], "operation.source_verified")
    # Invoice, order and receipt fetched; four record types listed under the order.
    assert (verified["records"], verified["listings"]) == (3, 4)
    assert verified["connector"] == "purchasing-sim/2"


def test_a_receipt_edited_upstream_is_caught_before_dispatch(
    stack: dict[str, Any], worker_db: Database
) -> None:
    st = stack
    case, case_id, _first, _prop = approved_and_held(st, worker_db, "4803")
    edited = variant(
        "rcv-f-late-rejection",
        "4803",
        **{
            "events": [],
            "scenario.invoice.lines.0.qty": "22",
            "receipts.0.lines": [
                {"line": 1, "qty": "18"},
                {"line": 1, "qty": "2", "status": "REJECTED", "reason": "damaged in transit"},
            ],
        },
    )
    assert edited.receipts[0].id == case.receipts[0].id == "GR-4803"
    put(st, render_case(edited))
    connector = spy_on_connector(st)
    dispatch_now(st, worker_db)

    [op] = operations(st, case_id)
    assert (op["status"], op["failure_category"]) == ("cancelled_before_dispatch", "source_changed")
    assert connector.calls == [] and remote_tasks(st, op) == []


def test_an_unreachable_source_keeps_the_action_in_review(
    stack: dict[str, Any], worker_db: Database, migrator_engine: Engine
) -> None:
    st = stack
    _case, case_id, _first, prop = approved_and_held(st, worker_db, "4804")
    httpx.post(
        f"{st['base']}/_control/faults",
        json={"org": ORG, "route": "records.list", "mode": "fail_500", "count": 5},
    ).raise_for_status()
    connector = spy_on_connector(st)
    try:
        dispatch_now(st, worker_db)
    finally:
        httpx.delete(f"{st['base']}/_control/faults").raise_for_status()

    [op] = operations(st, case_id)
    assert (op["status"], op["failure_category"]) == (
        "cancelled_before_dispatch",
        "source_unverified",
    )
    assert connector.calls == [] and remote_tasks(st, op) == []
    assert proposal_status(st, case_id, prop["id"]) == "superseded"
    [refused] = audit(migrator_engine, op["id"], "operation.cancelled_before_dispatch")
    assert "could not be reached (purchasing system returned 500)" in refused["detail"]
    assert refused["detail"].endswith("the action is back in review")


def test_an_export_cannot_establish_freshness_so_the_action_stays_in_review(
    stack: dict[str, Any], worker_db: Database, migrator_engine: Engine, tmp_path: Path
) -> None:
    st = stack
    case, case_id, _first, _prop = approved_and_held(st, worker_db, "4805")
    root = tmp_path / ORG
    write_export(render_case(case), root, "synthetic", datetime(2026, 11, 18, 9, 0, tzinfo=UTC))
    st["exec"]["exec"] = replace(st["exec"]["exec"], source_for=lambda org: ExportSource(root))
    connector = spy_on_connector(st)
    dispatch_now(st, worker_db)

    [op] = operations(st, case_id)
    assert (op["status"], op["failure_category"]) == (
        "cancelled_before_dispatch",
        "source_unverified",
    )
    assert connector.calls == [] and remote_tasks(st, op) == []
    [refused] = audit(migrator_engine, op["id"], "operation.cancelled_before_dispatch")
    assert "comes from an export, taken 2026-11-18 09:00 UTC" in refused["detail"]


def test_with_outbound_writes_off_or_a_local_action_the_source_is_not_asked(
    stack: dict[str, Any], worker_db: Database
) -> None:
    st = stack
    source = Spy(st["exec"]["exec"].source_for)
    st["exec"]["exec"] = replace(st["exec"]["exec"], source_for=source)

    # A dry run: the write is refused for that reason before anything is asked.
    _case, case_id, _first, _prop = approved_and_held(st, worker_db, "4806")
    st["exec"]["exec"] = replace(st["exec"]["exec"], outbound_writes_enabled=False)
    dispatch_now(st, worker_db)
    [op] = operations(st, case_id)
    assert op["failure_category"] == "outbound_writes_disabled"
    st["exec"]["exec"] = replace(st["exec"]["exec"], outbound_writes_enabled=True)

    # A review note stays inside OpsBridge.
    matched = variant("rcv-f-late-rejection", "4807", events=[])
    note_case, iv = opened(st, matched)
    assert iv["outcome"] == "matched"
    note = propose(st, note_case)
    assert note["action_type"] == "record_review_note"
    assert approve(st, note_case, note).status_code == 200
    drain(st)
    [done] = operations(st, note_case)
    assert done["status"] == "confirmed"

    assert source.calls == []
