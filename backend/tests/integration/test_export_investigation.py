"""An investigation reads an organization's export instead of the live source (production
review A08, first step without access to a customer system).

The export is hand-written here in the IT pack's format (not rendered by the simulator):
PO-2001 for 10 laptops, INV-7001 billing 10, a receipt of 8. The same rules find the same
shortfall. The export's age is the data's age: an investigation records the export's
`extracted_at` as its read time, so an export older than the age limit cannot be acted on,
however recently it was read. An export dated ahead of this host's clock by more than the
allowance is not read at all, and one a little ahead is taken as read now (review N03): it
used to give a result that could be acted on until its claimed time plus the age limit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsbridge.connectors.exports import EXPORT_SCHEMA, ExportSource

from .stack import get, investigate, open_case
from .test_api_cases import call

pytestmark = pytest.mark.integration
ORG = "demo-it"
T = f"/api/v1/tenants/{ORG}"
WHO = "it-investigator"

RECORDS: dict[str, tuple[str, dict[str, Any]]] = {
    "purchase_orders/PO-2001.json": (
        "purchase_orders",
        {
            "purchaseOrderId": "PO-2001",
            "vendor": {"vendorCode": "VEN-LUMEN", "legalName": "Lumen Compute GmbH"},
            "currencyCode": "EUR",
            "state": "Active",
            "items": [
                {"lineId": "1", "partNumber": "LT-14", "desc": "Laptop 14in", "quantity": "10",
                 "unitOfMeasure": "EA", "unitCost": "1450.00"}
            ],
        },
    ),
    "invoices/INV-7001.json": (
        "invoices",
        {
            "invoiceId": "INV-7001",
            "issuedOn": "2026-09-25",
            "vendorCode": "VEN-LUMEN",
            "vendorName": "Lumen Compute GmbH",
            "purchaseOrderRef": "PO-2001",
            "currencyCode": "EUR",
            "state": "Open",
            "items": [
                {"lineId": "1", "poLineRef": "1", "partNumber": "LT-14", "desc": "Laptop 14in",
                 "quantity": "10", "unitOfMeasure": "EA", "unitCost": "1450.00",
                 "lineType": "Hardware"}
            ],
        },
    ),
    "receipts/GRN-6001A.json": (
        "receipts",
        {
            "grnNumber": "GRN-6001A",
            "poRef": "PO-2001",
            "receivedDate": "2026-09-10",
            "items": [
                {"poLineId": "1", "qtyReceived": "8", "uom": "EA", "movementType": "Inbound",
                 "condition": "Accepted"}
            ],
        },
    ),
}  # fmt: skip


def write_export(root: Path, extracted_at: datetime) -> Path:
    export = root / ORG
    entries = []
    for name, (collection, doc) in RECORDS.items():
        body = json.dumps(doc, indent=2).encode()
        (export / name).parent.mkdir(parents=True, exist_ok=True)
        (export / name).write_bytes(body)
        entries.append(
            {
                "collection": collection,
                "key": Path(name).stem,
                "po_number": "PO-2001",
                "file": name,
                "content_type": "application/json",
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    manifest = {
        "schema": EXPORT_SCHEMA,
        "source_system": "hand-written test export",
        "extracted_at": extracted_at.isoformat(),
        "complete": True,
        "records": entries,
        "withdrawn": [],
    }
    (export / "manifest.json").write_text(json.dumps(manifest))
    return root


def _investigate_from(st: dict[str, Any], root: Path) -> tuple[str, dict[str, Any]]:
    deps = st["deps"]
    original = deps.purchasing_for
    deps.purchasing_for = lambda org: ExportSource(root / org)
    try:
        case_id = open_case(st, WHO, ORG, "INV-7001")
        investigate(st, WHO, ORG, case_id)
    finally:
        deps.purchasing_for = original
    r = get(st, WHO, f"{T}/cases/{case_id}/investigation")
    assert r.status_code == 200, r.text
    return case_id, dict(r.json())


def test_an_investigation_reads_the_export_and_finds_the_shortfall(stack, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    extracted = datetime.now(UTC) - timedelta(hours=1)
    _, result = _investigate_from(stack, write_export(tmp_path, extracted))
    assert result["outcome"] == "discrepancies_found", result["findings"]
    assert "quantity_mismatch" in {f["finding_type"] for f in result["findings"]}
    assert datetime.fromisoformat(result["source_read_at"]) == extracted  # the export's age


def test_an_export_older_than_the_age_limit_cannot_be_acted_on(stack, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, result = _investigate_from(
        st, write_export(tmp_path, datetime.now(UTC) - timedelta(days=2))
    )
    assert result["outcome"] == "discrepancies_found"
    assert "purchasing data was read" in (result["stale_reason"] or "")
    r = call(st["client"], st["actors"][WHO], "POST", f"{T}/cases/{case_id}/proposals", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_investigation", r.text


def test_an_export_dated_ahead_of_the_clock_is_not_read(stack, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    st = stack
    write_export(tmp_path, datetime.now(UTC) + timedelta(days=365))
    deps = st["deps"]
    original = deps.purchasing_for
    deps.purchasing_for = lambda org: ExportSource(tmp_path / org)
    try:
        case_id = open_case(st, WHO, ORG, "INV-7001")
        run = investigate(st, WHO, ORG, case_id)
    finally:
        deps.purchasing_for = original
    status = get(st, WHO, f"{T}/cases/{case_id}/runs/{run['run_id']}").json()
    assert status["status"] == "failed", status
    assert "ahead of this host's clock" in status["steps"][0]["error"]
    assert get(st, WHO, f"{T}/cases/{case_id}/investigation").status_code == 404


def test_an_export_a_little_ahead_is_taken_as_read_now(stack, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    before = datetime.now(UTC)
    _, result = _investigate_from(stack, write_export(tmp_path, before + timedelta(minutes=2)))
    read = datetime.fromisoformat(result["source_read_at"])
    assert before <= read <= datetime.now(UTC)  # not the export's claim, two minutes ahead
    assert result["stale_reason"] is None


def test_the_pilot_dry_run_reads_the_rehearsal_export_and_sends_nothing(
    stack, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """docs/pilot/quickstart.md, end to end: the synthetic rehearsal export, rules only,
    outbound writes disabled. One case is investigated with its evidence, one stops for the
    receipt it lacks, and an approved follow-up reaches no task system."""
    from dataclasses import replace

    from opsbridge.pilot.rehearsal import REHEARSAL, write_rehearsal_export
    from opsbridge.simulator.scenarios import load_corpus

    from .test_approvals_execution import approve, drain, operations, remote_tasks

    st = stack
    extracted = datetime.now(UTC) - timedelta(minutes=5)
    write_rehearsal_export(load_corpus(REHEARSAL), tmp_path, extracted)
    t = "/api/v1/tenants/demo-furniture"
    who = st["actors"]["furn-investigator"]
    deps, executor = st["deps"], st["exec"]["exec"]
    original = deps.purchasing_for
    deps.purchasing_for = lambda org: ExportSource(tmp_path / org)
    st["exec"]["exec"] = replace(executor, outbound_writes_enabled=False)
    try:
        cases = {}
        for invoice in ("INV-8401", "INV-8403"):
            case_id = open_case(st, "furn-investigator", "demo-furniture", invoice)
            r = call(
                st["client"],
                who,
                "POST",
                f"{t}/cases/{case_id}/investigations",
                json={"model_mode": "rules_only"},
            )
            assert r.status_code in (200, 202), r.text
            drain(st)
            cases[invoice] = case_id
        found = get(st, "furn-investigator", f"{t}/cases/{cases['INV-8401']}/investigation").json()
        stops = get(st, "furn-investigator", f"{t}/cases/{cases['INV-8403']}/investigation").json()
        assert datetime.fromisoformat(found["source_read_at"]) == extracted
        # Rules 1.4.0: 2 of the 20 were rejected with no reason recorded, so receiving is asked
        # for the inspection record (docs/receiving-disposition.md).
        assert found["outcome"] == "needs_information"
        assert {f["reason_code"] for f in found["findings"]} == {
            "rejection_reason_missing",
            "hold_unconfigured",
            "supplier_claim_includes_rejected",
        }
        # Missing evidence stops the case and asks for it; it is never read as zero received.
        assert stops["outcome"] == "needs_information"
        [missing] = stops["findings"]
        assert missing["reason_code"] == "receiving_record_unavailable"
        assert "does not establish whether the goods were delivered" in missing["summary"]
        assert (stops["recommendation"]["decision"], stops["recommendation"]["assignee_group"]) == (
            "request_information",
            "receiving",
        )

        # Approving the follow-up records the decision and sends nothing.
        r = call(st["client"], who, "POST", f"{t}/cases/{cases['INV-8401']}/proposals", json={})
        assert r.status_code == 201, r.text
        assert approve(st, cases["INV-8401"], r.json()).status_code == 200
        drain(st)
        [op] = operations(st, cases["INV-8401"])
        assert (op["status"], op["failure_category"]) == (
            "cancelled_before_dispatch",
            "outbound_writes_disabled",
        )
        assert remote_tasks(st, op) == []
    finally:
        deps.purchasing_for = original
        st["exec"]["exec"] = executor
