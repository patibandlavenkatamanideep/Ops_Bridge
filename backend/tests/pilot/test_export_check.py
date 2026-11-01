"""Checking an organization's export against its pack before a pilot reads it
(docs/pilot-plan.md, protocol opsbridge-pilot/1).

An investigation reads an export one record at a time, so a defect in it shows up only in the
case that needs that record, and in a pilot that case counts against OpsBridge. The check reads
every listed record the way an investigation would, stores nothing, and lists each problem by
collection and key. The exports are hand-written here in the IT pack's format; no customer
export has been read.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from opsbridge import cli
from opsbridge.config import REPO_ROOT
from opsbridge.connectors.exports import EXPORT_SCHEMA, ExportSource
from opsbridge.packs import PackRegistry
from opsbridge.pilot.export_check import check_export

PACK = PackRegistry(REPO_ROOT / "packs").get("it_hardware")

PO: dict[str, Any] = {
    "purchaseOrderId": "PO-2001",
    "vendor": {"vendorCode": "VEN-LUMEN", "legalName": "Lumen Compute GmbH"},
    "currencyCode": "EUR",
    "state": "Active",
    "items": [
        {"lineId": "1", "partNumber": "LT-14", "desc": "Laptop 14in", "quantity": "10",
         "unitOfMeasure": "EA", "unitCost": "1450.00"}
    ],
}  # fmt: skip
INVOICE: dict[str, Any] = {
    "invoiceId": "INV-7001",
    "issuedOn": "2026-09-25",
    "vendorCode": "VEN-LUMEN",
    "vendorName": "Lumen Compute GmbH",
    "purchaseOrderRef": "PO-2001",
    "currencyCode": "EUR",
    "state": "Open",
    "items": [
        {"lineId": "1", "poLineRef": "1", "partNumber": "LT-14", "desc": "Laptop 14in",
         "quantity": "10", "unitOfMeasure": "EA", "unitCost": "1450.00", "lineType": "Hardware"}
    ],
}  # fmt: skip
RECEIPT: dict[str, Any] = {
    "grnNumber": "GRN-6001A",
    "poRef": "PO-2001",
    "receivedDate": "2026-09-10",
    "items": [
        {"poLineId": "1", "qtyReceived": "8", "uom": "EA", "movementType": "Inbound",
         "condition": "Accepted"}
    ],
}  # fmt: skip

Record = tuple[str, str, str | None, bytes]  # collection, key, the PO it is listed under, body


def body(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, indent=2).encode()


def consistent() -> list[Record]:
    return [
        ("purchase_orders", "PO-2001", "PO-2001", body(PO)),
        ("invoices", "INV-7001", "PO-2001", body(INVOICE)),
        ("receipts", "GRN-6001A", "PO-2001", body(RECEIPT)),
    ]


def write_export(root: Path, records: list[Record], *, complete: bool = True) -> Path:
    entries = []
    for collection, key, po, data in records:
        name = f"{collection}/{key}.json"
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
        entries.append(
            {
                "collection": collection,
                "key": key,
                "po_number": po,
                "file": name,
                "content_type": "application/json",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {
        "schema": EXPORT_SCHEMA,
        "source_system": "hand-written test export",
        "extracted_at": "2026-10-27T09:00:00+00:00",
        "complete": complete,
        "records": entries,
        "withdrawn": [],
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def check(root: Path) -> dict[str, Any]:
    return check_export(ExportSource(root), PACK)


def problems(report: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(p["collection"], p["key"], p["problem"]) for p in report["problems"]]


def test_a_consistent_export_has_nothing_to_review(tmp_path: Path) -> None:
    report = check(write_export(tmp_path, consistent()))
    assert report["problems"] == []
    assert (report["pack"], report["records"], report["status"]) == (
        "it_hardware@v1",
        3,
        {"parsed": 3},
    )
    assert (report["extracted_at"], report["complete"]) == ("2026-10-27T09:00:00+00:00", True)
    assert "Lumen" not in json.dumps(report)  # keys and problems, no document content


def test_every_unreadable_record_is_reported_not_only_the_first(tmp_path: Path) -> None:
    root = write_export(tmp_path, consistent())
    (root / "invoices/INV-7001.json").write_bytes(b"{}")  # changed after the manifest
    (root / "receipts/GRN-6001A.json").unlink()
    report = check(root)
    found = problems(report)
    assert [(c, k) for c, k, _ in found] == [("invoices", "INV-7001"), ("receipts", "GRN-6001A")]
    assert "does not match its digest" in found[0][2] and "missing" in found[1][2]
    assert report["status"] == {"parsed": 1, "unreadable": 2}


def test_a_collection_the_pack_does_not_read_is_reported(tmp_path: Path) -> None:
    records = [*consistent(), ("credit_notes", "CN-1", "PO-2001", body({"creditNoteId": "CN-1"}))]
    report = check(write_export(tmp_path, records))
    assert problems(report) == [
        ("credit_notes", "CN-1", "the it_hardware pack reads no credit_notes, so no case uses it")
    ]
    assert report["status"] == {"parsed": 3, "not_read": 1}


def test_a_document_the_pack_cannot_read_or_map_is_reported(tmp_path: Path) -> None:
    invoice = copy.deepcopy(INVOICE)
    del invoice["currencyCode"]
    receipt = copy.deepcopy(RECEIPT)
    receipt["items"][0]["qtyReceived"] = "eight"
    records = [
        ("purchase_orders", "PO-2001", "PO-2001", b"PO-2001, 10 laptops"),  # not JSON
        ("invoices", "INV-7001", "PO-2001", body(invoice)),
        ("receipts", "GRN-6001A", "PO-2001", body(receipt)),
    ]
    report = check(write_export(tmp_path, records))
    assert report["status"] == {"rejected": 1, "needs_review": 2}
    found = problems(report)
    assert [(c, k) for c, k, _ in found] == [
        ("purchase_orders", "PO-2001"),
        ("invoices", "INV-7001"),
        ("receipts", "GRN-6001A"),
    ]
    assert "declared application/json" in found[0][2]
    assert "invoice.currency" in found[1][2]
    assert "receipt_line.quantity" in found[2][2]


def test_numbers_and_listings_must_agree_with_the_documents(tmp_path: Path) -> None:
    records = [
        ("purchase_orders", "PO-2001", "PO-2001", body({**PO, "purchaseOrderId": "PO-2009"})),
        ("invoices", "INV-7001", "PO-2001", body({**INVOICE, "invoiceId": "INV-7002"})),
        ("receipts", "GRN-6001A", "PO-2002", body(RECEIPT)),
    ]
    assert problems(check(write_export(tmp_path, records))) == [
        (
            "purchase_orders",
            "PO-2001",
            "the document is purchase order PO-2009, so a case for PO-2001 would not find it",
        ),
        (
            "invoices",
            "INV-7001",
            "the document is invoice INV-7002, so a case for INV-7001 would not find it",
        ),
        (
            "receipts",
            "GRN-6001A",
            "listed under PO-2002, but the document is for PO-2001, "
            "so a case for PO-2001 would not read it",
        ),
    ]


@pytest.mark.parametrize(
    ("complete", "consequence"),
    [
        (True, "is not in this export, so an investigation will find no purchase order"),
        (False, "is not in this partial export, so an investigation cannot tell if it exists"),
    ],
)
def test_an_invoices_purchase_order_must_be_in_the_export(
    tmp_path: Path, complete: bool, consequence: str
) -> None:
    report = check(write_export(tmp_path, consistent()[1:], complete=complete))
    assert problems(report) == [("invoices", "INV-7001", f"purchase order PO-2001 {consequence}")]


def test_the_command_exits_by_whether_anything_needs_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def run(root: Path) -> int:
        out = tmp_path / f"{root.name}.json"
        return cli.main(["export-check", "--export", str(root), "--pack", "it_hardware",
                         "--out", str(out)])  # fmt: skip

    assert run(write_export(tmp_path / "good", consistent())) == 0
    assert json.loads((tmp_path / "good.json").read_text())["problems"] == []
    assert run(write_export(tmp_path / "short", consistent()[1:])) == 1
    printed = capsys.readouterr().out
    assert "1 problem" in printed and "invoices INV-7001: purchase order PO-2001" in printed
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "manifest.json").write_text("{}")
    assert run(tmp_path / "broken") == 2
    assert not (tmp_path / "broken.json").exists()
    assert "Refusing" in capsys.readouterr().out
