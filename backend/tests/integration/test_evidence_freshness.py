"""Decisions are bound to the evidence they were computed from (review finding F04).

A correction, an upload, or a new version of a source document ingested for another case
changes what matching would read. Until the investigation is re-run, its result must not be
proposed, approved, dispatched or used to resolve the case.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select, text

from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import Case, ExtractedField, FieldCorrection

from .stack import get, investigate, open_case
from .test_api_cases import call
from .test_approvals_execution import (
    _publish_policy_v3,
    _tenant_of,
    approve,
    drain,
    operations,
    prepared,
    remote_tasks,
)
from .test_hardening import upload

pytestmark = pytest.mark.integration
ORG = "demo-furniture"
T = f"/api/v1/tenants/{ORG}"


def receipt_quantity(st: dict[str, Any], case_id: str) -> dict[str, Any]:
    docs = get(st, "furn-investigator", f"{T}/cases/{case_id}/documents").json()["items"]
    gr = next(d for d in docs if d["kind"] == "goods_receipt")
    url = f"{T}/cases/{case_id}/documents/versions/{gr['versions'][-1]['version_id']}"
    detail = get(st, "furn-investigator", url).json()
    return next(f for f in detail["fields"] if f["field_path"] == "receipt_line.quantity")


def correct(st: dict[str, Any], case_id: str, value: str | None = None) -> None:
    fld = receipt_quantity(st, case_id)
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{T}/cases/{case_id}/fields/{fld['field_id']}/corrections",
        json={"new_value": value or fld["current_value"], "reason": "Warehouse recount"},
    )
    assert r.status_code == 201, r.text


def reasons(r: httpx.Response) -> str:
    return " ".join(r.json()["error"]["details"]["reasons"])


def change_po_through_another_case(st: dict[str, Any], po_number: str) -> None:
    """A different case's investigation ingests a new version of the same purchase order."""
    store = st["store"]
    found = store.get_record(ORG, "purchase_orders", po_number)
    assert found is not None
    body, ctype, _ = found
    po = json.loads(body)
    po["lines"][0]["description"] += " (revised)"
    store.put_record(ORG, "purchase_orders", po_number, json.dumps(po).encode(), ctype, po_number)
    try:
        r = call(
            st["client"],
            st["actors"]["furn-investigator"],
            "POST",
            f"{T}/cases",
            json={"title": f"Order {po_number} question", "po_number": po_number},
        )
        assert r.status_code == 201, r.text
        investigate(st, "furn-investigator", ORG, r.json()["id"])
    finally:
        store.put_record(ORG, "purchase_orders", po_number, body, ctype, po_number)


def test_a_correction_after_approval_stops_the_dispatch_without_a_rerun(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    assert approve(st, case_id, prop).status_code == 200
    correct(st, case_id)  # nobody re-runs the investigation
    drain(st)
    [op] = operations(st, case_id)
    assert (op["status"], op["failure_category"]) == (
        "cancelled_before_dispatch",
        "precondition_failed",
    )
    assert remote_tasks(st, op) == [] and op["attempts"] == []


@pytest.mark.parametrize("change", ["correction", "upload", "shared_source"])
def test_changed_evidence_blocks_approval_and_new_proposals_until_rerun(
    stack, change: str
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    if change == "correction":
        correct(st, case_id)
    elif change == "upload":
        note = b"Hello,\nWe delivered all 20 chairs for PO-1001 on Monday.\nRegards"
        actor = st["actors"]["furn-investigator"]
        r = upload(st["client"], actor, case_id, "supplier_message", "note.txt", note, "text/plain")
        assert r.status_code == 201, r.text
    else:
        change_po_through_another_case(st, "PO-1001")
    r = approve(st, case_id, prop)
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_inputs", r.text
    assert "evidence changed since the investigation" in reasons(r)
    again = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{T}/cases/{case_id}/proposals",
        json={},
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "stale_investigation"
    investigate(st, "furn-investigator", ORG, case_id)  # a re-run makes the result current
    fresh = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{T}/cases/{case_id}/proposals",
        json={},
    )
    assert fresh.status_code == 201, fresh.text


@pytest.mark.parametrize("change", ["correction", "policy"])
def test_resolution_needs_an_investigation_of_the_current_evidence_and_policy(
    stack, worker_db: Database, migrator_engine: Engine, change: str
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, "furn-investigator", ORG, "INV-9002")  # matches policy
    investigate(st, "furn-investigator", ORG, case_id)
    if change == "correction":
        correct(st, case_id)  # same value, but nothing has re-checked it
    else:
        _publish_policy_v3(worker_db, _tenant_of(migrator_engine, ORG))

    def resolve() -> httpx.Response:
        return call(
            st["client"],
            st["actors"]["furn-investigator"],
            "POST",
            f"{T}/cases/{case_id}/resolve",
            json={"note": "Matches after review"},
        )

    r = resolve()
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_investigation", r.text
    investigate(st, "furn-investigator", ORG, case_id)
    assert resolve().status_code == 200


def test_a_correction_committed_while_dispatch_waits_for_the_case_is_seen(
    stack, app_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    """Dispatch re-checks evidence under the case lock that corrections take: whichever
    commits first decides, and a correction that wins is never dispatched past."""
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    assert approve(st, case_id, prop).status_code == 200
    field_id = uuid.UUID(receipt_quantity(st, case_id)["field_id"])
    tid = _tenant_of(migrator_engine, ORG)
    worker = threading.Thread(target=lambda: drain(st))
    with app_db.session(DbContext(tenant_id=tid)) as s:
        s.execute(select(Case).where(Case.id == uuid.UUID(case_id)).with_for_update())
        fld = s.get(ExtractedField, field_id)
        assert fld is not None
        s.add(
            FieldCorrection(
                tenant_id=tid,
                field_id=field_id,
                previous_value=fld.value_text,
                new_value=fld.value_text,
                reason="Warehouse recount",
                author_id=st["actors"]["furn-investigator"].user_id,
            )
        )
        s.flush()
        worker.start()
        time.sleep(1.0)  # the worker reaches the case lock and waits
        assert worker.is_alive()
    worker.join(timeout=30)
    [op] = operations(st, case_id)
    assert op["status"] == "cancelled_before_dispatch" and remote_tasks(st, op) == []
    with app_db.session(DbContext(tenant_id=tid)) as s:
        assert s.execute(text("SELECT count(*) FROM field_corrections")).scalar_one() == 1
