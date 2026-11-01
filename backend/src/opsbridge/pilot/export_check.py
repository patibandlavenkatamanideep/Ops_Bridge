"""Checks an organization's export against its pack before a pilot reads it (protocol
`opsbridge-pilot/1`, docs/pilot-plan.md).

An investigation reads an export one record at a time, so a defect in it shows up only in the
case that needs that record, and in a pilot that case counts against OpsBridge. This reads every
listed record the way an investigation does (`ExportSource.fetch`, `parse`, `map_document`),
stores nothing, and lists each problem by collection and key:
- the file is there, within the size limit, and matches its digest;
- its collection is one the pack reads;
- it parses and maps through the pack without problems;
- an invoice's or purchase order's number is its key, since a case fetches it by that number;
- a record read by purchase order is listed under the purchase order it is for;
- the purchase order each invoice names is in the export.

A problem is something to review before the first case, not always a defect: an invoice whose
purchase order the source really lacks is a fact. The report holds keys, counts and problems,
not document content, but a mapping problem can quote the value that failed, so the report
stays where the export is kept.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from opsbridge.connectors.exports import ExportSource
from opsbridge.connectors.source_contract import SourceRecordInvalid
from opsbridge.evidence.parsers import OcrSettings, parse
from opsbridge.mapping import DocRef, MappingResult, PackSpec, RecordType, map_document

SCHEMA = "opsbridge-export-check/1"


def _by_po(record_type: RecordType, mapped: MappingResult) -> list[Any]:
    """The records a case reads by listing a purchase order's."""
    by_type: dict[str, list[Any]] = {
        "goods_receipt": mapped.receipts,
        "allocation_history": mapped.allocations,
        "supplier_message": mapped.assertions,
    }
    return by_type.get(record_type, [])


def _disagreements(
    record_type: RecordType, key: str, listed_po: str | None, mapped: MappingResult
) -> list[str]:
    """Where the manifest and the document disagree about what a case would look up."""
    if record_type == "invoice":
        numbers = [i.invoice_number for i in mapped.invoices]
        what = "invoice"
    elif record_type == "purchase_order":
        numbers = [p.po_number for p in mapped.purchase_orders]
        what = "purchase order"
    else:
        records = _by_po(record_type, mapped)
        under = listed_po or "no purchase order"
        return [
            f"listed under {under}, but the document is for {po}, so a case for {po} would "
            "not read it"
            for po in sorted({r.po_number for r in records} - {listed_po})
        ]
    return [
        f"the document is {what} {n}, so a case for {key} would not find it"
        for n in numbers
        if n != key
    ]


def check_export(
    source: ExportSource, pack: PackSpec, ocr: OcrSettings | None = None
) -> dict[str, Any]:
    read_as = {s.collection: rt for rt, s in pack.sources.items() if s.collection is not None}
    po_spec = pack.sources.get("purchase_order")
    status: Counter[str] = Counter()
    problems: list[dict[str, str]] = []
    named: list[tuple[str, str, str]] = []  # invoice collection, key, the PO it names

    def problem(collection: str, key: str, text: str) -> None:
        problems.append({"collection": collection, "key": key, "problem": text})

    listed = source.listed()
    for collection, key, listed_po in listed:
        record_type = read_as.get(collection)
        if record_type is None:
            status["not_read"] += 1
            problem(
                collection,
                key,
                f"the {pack.pack_id} pack reads no {collection}, so no case uses it",
            )
            continue
        try:
            fetched = source.fetch(collection, key)
        except SourceRecordInvalid as exc:
            status["unreadable"] += 1
            problem(collection, key, str(exc))
            continue
        if fetched is None:  # a listed record is held; fetch answers None only otherwise
            continue
        parsed = parse(fetched.body, fetched.content_type, ocr)
        if parsed.status != "parsed":
            status[parsed.status] += 1
            problem(collection, key, f"{parsed.status}: {parsed.detail}")
            continue
        mapped = map_document(
            pack,
            record_type,
            parsed,
            DocRef(f"{collection}/{key}", "export-check", fetched.etag or ""),
        )
        if mapped.problems:
            status["needs_review"] += 1
            problem(collection, key, "; ".join(mapped.problems))
        else:
            status["parsed"] += 1
        for text in _disagreements(record_type, key, listed_po, mapped):
            problem(collection, key, text)
        named += [(collection, key, i.po_number) for i in mapped.invoices if i.po_number]

    if po_spec is not None and po_spec.collection is not None:
        for collection, key, po in named:
            held = source.holds(po_spec.collection, po)
            if held is False:
                problem(
                    collection,
                    key,
                    f"purchase order {po} is not in this export, so an investigation will "
                    "find no purchase order",
                )
            elif held is None:
                problem(
                    collection,
                    key,
                    f"purchase order {po} is not in this partial export, so an investigation "
                    "cannot tell if it exists",
                )
    return {
        "schema": SCHEMA,
        "pack": f"{pack.pack_id}@v{pack.version}",
        "extracted_at": source.as_of.isoformat(),
        "complete": source.complete,
        "records": len(listed),
        "status": dict(status),
        "problems": problems,
    }
