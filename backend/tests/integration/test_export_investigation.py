"""An investigation reads an organization's export instead of the live source (production
review A08, first step without access to a customer system).

The export is hand-written here in the IT pack's format (not rendered by the simulator):
PO-2001 for 10 laptops, INV-7001 billing 10, a receipt of 8. The same rules find the same
shortfall. The export's age is the data's age: an investigation records the export's
`extracted_at` as its read time, so an export older than the age limit cannot be acted on,
however recently it was read.
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
