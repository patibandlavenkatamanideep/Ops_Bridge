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


def test_a_document_cannot_hide_or_reorder_its_problem_on_the_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A problem can quote a document's own value. Sent raw, an escape sequence in it could
    erase the problem's line or retitle the window, and a bidirectional override could
    reorder what the operator reads."""
    invoice = copy.deepcopy(INVOICE)
    invoice["invoiceId"] = "INV-7001\x1b[2K\x1b]0;retitled\x07‮"
    records = consistent()
    records[1] = ("invoices", "INV-7001", "PO-2001", body(invoice))
    root = write_export(tmp_path / "export", records)
    assert cli.main(["export-check", "--export", str(root), "--pack", "it_hardware"]) == 1
    printed = capsys.readouterr().out
    assert [c for c in printed if not (c.isprintable() or c == "\n")] == []
    assert "INV-7001\\x1b[2K\\x1b]0;RETITLED\\x07\\u202e" in printed


# --- Which cases the export can support (pilot preflight) ------------------------------


def cases(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["invoice"]: c for c in report["cases"]}


def test_a_consistent_export_supports_its_case_and_says_what_was_loaded(tmp_path: Path) -> None:
    report = check(write_export(tmp_path, consistent()))
    assert report["schema"] == "opsbridge-export-check/2"
    assert report["cases"] == [
        {
            "invoice": "INV-7001",
            "purchase_order": "PO-2001",
            "evaluable": True,
            "blocking": [],
            "notes": [],
        }
    ]
    assert report["loaded"] == {"invoices": 1, "purchase_orders": 1, "receipts": 1}
    assert report["unlinked"] == []


def test_a_case_whose_purchase_order_is_missing_cannot_be_evaluated(tmp_path: Path) -> None:
    case = cases(check(write_export(tmp_path, consistent()[1:2])))["INV-7001"]  # invoice only
    assert case["evaluable"] is False
    assert case["blocking"] == [
        "purchase order PO-2001 is not in this export, so an investigation will find no "
        "purchase order"
    ]
    assert case["notes"] == []  # no receipt note for an order that is not there at all


def test_a_record_the_case_reads_that_cannot_be_read_blocks_the_case(tmp_path: Path) -> None:
    receipt = copy.deepcopy(RECEIPT)
    receipt["items"][0]["qtyReceived"] = ""  # blank: never read as zero received
    records = consistent()
    records[2] = ("receipts", "GRN-6001A", "PO-2001", body(receipt))
    report = check(write_export(tmp_path, records))
    case = cases(report)["INV-7001"]
    assert case["evaluable"] is False
    [blocking] = case["blocking"]
    assert blocking.startswith("receipts GRN-6001A: ") and "receipt_line.quantity" in blocking
    assert report["loaded"] == {"invoices": 1, "purchase_orders": 1}


def test_no_exported_receipt_is_a_note_that_the_case_will_ask_for_it(tmp_path: Path) -> None:
    case = cases(check(write_export(tmp_path, consistent()[:2])))["INV-7001"]
    assert case["evaluable"] is True  # missing evidence is a finding, not a defect
    assert case["notes"] == [
        "no receipt is exported for PO-2001: the investigation will report the receiving "
        "record as missing (needs information), never as zero received"
    ]


def test_a_partial_export_supports_no_case_reliably(tmp_path: Path) -> None:
    case = cases(check(write_export(tmp_path, consistent(), complete=False)))["INV-7001"]
    assert case["evaluable"] is False
    assert case["blocking"] == [
        "the export is partial (complete: false), so a record it lacks cannot be told from one "
        "the source lacks; the investigation will send the case to manual review"
    ]


def test_records_no_case_can_reach_are_listed_as_unlinked(tmp_path: Path) -> None:
    stray = {**RECEIPT, "grnNumber": "GRN-6009A", "poRef": "PO-2009"}
    records = [
        *consistent(),
        ("receipts", "GRN-6009A", "PO-2009", body(stray)),  # its PO is not in the export
        ("receipts", "GRN-6010A", None, body({**RECEIPT, "grnNumber": "GRN-6010A"})),
    ]
    report = check(write_export(tmp_path, records))
    assert report["unlinked"] == [
        {"collection": "receipts", "key": "GRN-6009A",
         "reason": "listed under PO-2009, which is not in this export"},
        {"collection": "receipts", "key": "GRN-6010A",
         "reason": "listed under no purchase order, so no case reads it"},
    ]  # fmt: skip
    assert cases(report)["INV-7001"]["evaluable"] is True


def test_the_command_prints_the_cases_and_this_installation_s_write_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = write_export(tmp_path / "short", consistent()[1:])
    assert cli.main(["export-check", "--export", str(root), "--pack", "it_hardware"]) == 1
    printed = capsys.readouterr().out
    assert "0 of 1 invoice(s) can be evaluated; 1 cannot be evaluated reliably" in printed
    assert "INV-7001 (PO-2001): purchase order PO-2001 is not in this export" in printed
    assert "loaded: invoices 1" in printed
    assert "outbound writes enabled" in printed  # the default; a dry run says disabled


# --- The synthetic rehearsal export (docs/pilot/quickstart.md) -------------------------


def test_the_rehearsal_export_supports_four_cases_and_names_the_fifth(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from opsbridge.pilot.rehearsal import REHEARSAL, write_rehearsal_export
    from opsbridge.simulator.scenarios import load_corpus

    root = write_rehearsal_export(load_corpus(REHEARSAL), tmp_path, datetime.now(UTC))
    assert root == tmp_path / "demo-furniture"
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["complete"] is True and "not a customer export" in manifest["source_system"]
    report = check_export(ExportSource(root), PackRegistry(REPO_ROOT / "packs").get("furniture"))
    by_invoice = {c["invoice"]: c for c in report["cases"]}
    assert sorted(by_invoice) == ["INV-8401", "INV-8402", "INV-8403", "INV-8404", "INV-8405"]
    assert [i for i, c in by_invoice.items() if not c["evaluable"]] == ["INV-8405"]
    assert by_invoice["INV-8405"]["blocking"] == [
        "purchase order PO-4005 is not in this export, so an investigation will find no "
        "purchase order"
    ]
    assert by_invoice["INV-8403"]["notes"][0].startswith("no receipt is exported for PO-4003")
    assert report["loaded"] == {
        "invoices": 5,
        "messages": 1,
        "purchase_orders": 4,
        "receipts": 3,
    }


def test_the_rehearsal_export_never_writes_over_an_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "demo-furniture").mkdir()
    (tmp_path / "demo-furniture" / "manifest.json").write_text("{}")
    assert cli.main(["pilot-rehearsal-export", "--out", str(tmp_path)]) == 2
    assert "Refusing" in capsys.readouterr().out
    assert (tmp_path / "demo-furniture" / "manifest.json").read_text() == "{}"
    assert cli.main(["pilot-rehearsal-export", "--out", str(tmp_path / "fresh")]) == 0
    assert (tmp_path / "fresh" / "demo-furniture" / "manifest.json").is_file()


def test_the_receiving_pack_exports_its_events_and_names_an_unlinked_rejection(
    tmp_path: Path,
) -> None:
    """The receiving pack as an export (docs/receiving-disposition.md): every receiving event
    is read, and a rejection naming a receipt the export lacks is a case note before any case
    is opened: it will wait for that receipt, never be attached to another."""
    from datetime import UTC, datetime

    from opsbridge.pilot.receiving_pack import load_pack, write_receiving_export

    root = write_receiving_export(load_pack(), tmp_path, datetime.now(UTC))
    report = check_export(ExportSource(root), PackRegistry(REPO_ROOT / "packs").get("furniture"))
    assert report["problems"] == []
    assert report["loaded"]["receiving_events"] == 8
    cases = {c["invoice"]: c for c in report["cases"]}
    assert all(c["evaluable"] for c in cases.values())
    assert any(
        "rejection REJ-4610 refers to receipt GR-6610, which is not in this export" in n
        for n in cases["INV-8610"]["notes"]
    )
    assert not [n for k, c in cases.items() if k != "INV-8610" for n in c["notes"]]


def test_the_receiving_pack_never_writes_over_an_export(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from opsbridge.pilot.receiving_pack import load_pack, write_receiving_export
    from opsbridge.pilot.rehearsal import ExportExists

    write_receiving_export(load_pack(), tmp_path, datetime.now(UTC))
    with pytest.raises(ExportExists):
        write_receiving_export(load_pack(), tmp_path, datetime.now(UTC))
