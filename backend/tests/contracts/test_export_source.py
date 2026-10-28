"""The export source against the `purchasing-source/1` contract (production review A08, first
step without access to a customer system).

Exports are written by hand here, not produced by the simulator's renderer, and no customer
export has been read: this shows the adapter keeps the contract's rules (absence only when
stated, completeness only when declared, withdrawals listed) and refuses an export it cannot
trust (digest mismatch, a file outside the export, duplicate or conflicting identities, a mixed
snapshot, an unknown schema).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsbridge.connectors.exports import EXPORT_SCHEMA, ExportSource
from opsbridge.connectors.source_contract import SourceRecordInvalid, SourceUnavailable

EXTRACTED = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


def write_export(
    root: Path,
    files: dict[str, tuple[str, str | None, bytes]],
    *,
    complete: bool = True,
    withdrawn: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> Path:
    """files: name -> (collection, po_number, body); keys are the file stems."""
    records = []
    for name, (collection, po, body) in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(body)
        records.append(
            {
                "collection": collection,
                "key": Path(name).stem,
                "po_number": po,
                "file": name,
                "content_type": "application/json",
                "sha256": hashlib.sha256(body).hexdigest(),
                "modified_at": (EXTRACTED - timedelta(hours=1)).isoformat(),
            }
        )
    manifest = {
        "schema": EXPORT_SCHEMA,
        "source_system": "hand-written test export",
        "extracted_at": EXTRACTED.isoformat(),
        "complete": complete,
        "records": records,
        "withdrawn": withdrawn or [],
        **overrides,
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


FILES = {
    "invoices/INV-1.json": ("invoices", "PO-1", b'{"invoiceId": "INV-1"}'),
    "receipts/GR-1.json": ("receipts", "PO-1", b'{"grnNumber": "GR-1"}'),
    "receipts/GR-2.json": ("receipts", "PO-1", b'{"grnNumber": "GR-2"}'),
    "receipts/GR-9.json": ("receipts", "PO-9", b'{"grnNumber": "GR-9"}'),
}


def test_records_are_served_with_their_digest_and_listed_by_po(tmp_path: Path) -> None:
    source = ExportSource(write_export(tmp_path, FILES))
    got = source.fetch("invoices", "INV-1")
    assert got is not None and got.body == b'{"invoiceId": "INV-1"}'
    assert got.etag == hashlib.sha256(got.body).hexdigest()
    listing = source.list_for_po("receipts", "PO-1")
    assert (listing.keys, listing.withdrawn, listing.complete) == (("GR-1", "GR-2"), (), True)
    assert source.as_of == EXTRACTED  # the data is as old as the export


def test_absence_is_a_fact_only_in_a_complete_export(tmp_path: Path) -> None:
    complete = ExportSource(write_export(tmp_path / "a", FILES))
    assert complete.fetch("invoices", "INV-404") is None
    partial = ExportSource(write_export(tmp_path / "b", FILES, complete=False))
    with pytest.raises(SourceRecordInvalid, match="unknown"):
        partial.fetch("invoices", "INV-404")
    assert partial.list_for_po("receipts", "PO-1").complete is False


def test_withdrawn_records_are_listed_and_not_served(tmp_path: Path) -> None:
    gone = [{"collection": "receipts", "key": "GR-3", "po_number": "PO-1"}]
    source = ExportSource(write_export(tmp_path, FILES, withdrawn=gone))
    assert source.list_for_po("receipts", "PO-1").withdrawn == ("GR-3",)
    assert source.fetch("receipts", "GR-3") is None


def test_a_file_that_does_not_match_its_digest_is_refused(tmp_path: Path) -> None:
    source = ExportSource(write_export(tmp_path, FILES))
    (tmp_path / "receipts/GR-1.json").write_bytes(b'{"grnNumber": "GR-1", "qty": 999}')
    with pytest.raises(SourceRecordInvalid, match="digest"):
        source.fetch("receipts", "GR-1")
    (tmp_path / "receipts/GR-2.json").unlink()
    with pytest.raises(SourceRecordInvalid, match="missing"):
        source.fetch("receipts", "GR-2")


def _manifest(root: Path) -> dict[str, Any]:
    return dict(json.loads((root / "manifest.json").read_text()))


@pytest.mark.parametrize(
    "spoil",
    [
        "outside",
        "absolute",
        "duplicate",
        "current_and_withdrawn",
        "mixed_snapshot",
        "schema",
        "no_completeness",
        "naive_time",
        "bad_digest_field",
    ],
)
def test_an_export_it_cannot_trust_is_refused(tmp_path: Path, spoil: str) -> None:
    root = write_export(tmp_path / "export", FILES)
    m = _manifest(root)
    first = m["records"][0]
    if spoil == "outside":
        (tmp_path / "secret.json").write_bytes(b"{}")
        first["file"] = "../secret.json"
    elif spoil == "absolute":
        first["file"] = str(tmp_path / "secret.json")
    elif spoil == "duplicate":
        m["records"].append(dict(first))
    elif spoil == "current_and_withdrawn":
        m["withdrawn"] = [{"collection": "invoices", "key": "INV-1", "po_number": "PO-1"}]
    elif spoil == "mixed_snapshot":
        first["modified_at"] = (EXTRACTED + timedelta(minutes=5)).isoformat()
    elif spoil == "schema":
        m["schema"] = "someone-else/9"
    elif spoil == "no_completeness":
        del m["complete"]
    elif spoil == "naive_time":
        m["extracted_at"] = "2026-10-27T09:00:00"
    else:
        first["sha256"] = "not-a-digest"
    (root / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(SourceRecordInvalid):
        ExportSource(root)


def test_an_export_that_is_not_there_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(SourceUnavailable):
        ExportSource(tmp_path / "never-delivered")


def test_an_unreadable_manifest_is_refused(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{not json")
    with pytest.raises(SourceRecordInvalid):
        ExportSource(tmp_path)
