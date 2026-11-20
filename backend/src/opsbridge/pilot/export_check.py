"""Checks an organization's export against its pack before a pilot reads it (protocol
`opsbridge-pilot/2`, docs/pilot-plan.md, docs/pilot/export-contract.md).

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

It also says, per invoice, whether a case for it can be evaluated reliably from this export,
and if not, why: its own document, its purchase order, or a record listed under that order
has a problem, or the export is partial. A blank or unreadable value is a problem here, never
a zero, and an order with no exported receipt is noted: its case will ask for the receiving
record. Records no case can reach (listed under no order, or under one the export lacks) are
listed as unlinked.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from opsbridge.connectors.exports import ExportSource
from opsbridge.connectors.source_contract import SourceRecordInvalid
from opsbridge.domain.records import ReceivingEvent
from opsbridge.evidence.parsers import OcrSettings, parse
from opsbridge.mapping import DocRef, MappingResult, PackSpec, RecordType, map_document

SCHEMA = "opsbridge-export-check/2"
_BY_PO: tuple[RecordType, ...] = (
    "goods_receipt",
    "allocation_history",
    "supplier_message",
    "receiving_event",
)


def _by_po(record_type: RecordType, mapped: MappingResult) -> list[Any]:
    """The records a case reads by listing a purchase order's."""
    by_type: dict[str, list[Any]] = {
        "goods_receipt": mapped.receipts,
        "allocation_history": mapped.allocations,
        "supplier_message": mapped.assertions,
        "receiving_event": mapped.events,
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
    loaded: Counter[str] = Counter()  # records read and mapped without a problem
    problems: list[dict[str, str]] = []
    named: list[tuple[str, str, str]] = []  # invoice collection, key, the PO it names
    invoices: dict[tuple[str, str], str | None] = {}  # invoice -> the PO its case reads
    own: dict[tuple[str, str], list[str]] = defaultdict(list)  # a record's own problems
    receipt_numbers: dict[str, set[str]] = defaultdict(set)  # PO -> receipts mapped for it
    events: dict[str, list[ReceivingEvent]] = defaultdict(list)  # PO -> events mapped for it

    def problem(collection: str, key: str, text: str) -> None:
        problems.append({"collection": collection, "key": key, "problem": text})
        own[(collection, key)].append(text)

    listed = source.listed()
    for collection, key, listed_po in listed:
        record_type = read_as.get(collection)
        if record_type == "invoice":
            invoices[(collection, key)] = listed_po
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
            loaded[collection] += 1
        if record_type == "invoice":  # the order the document names, as a case reads it
            named_po = next((i.po_number for i in mapped.invoices if i.po_number), None)
            invoices[(collection, key)] = named_po or listed_po
        for text in _disagreements(record_type, key, listed_po, mapped):
            problem(collection, key, text)
        named += [(collection, key, i.po_number) for i in mapped.invoices if i.po_number]
        for gr in mapped.receipts:
            receipt_numbers[gr.po_number].add(gr.receipt_number)
        for ev in mapped.events:
            events[ev.po_number].append(ev)

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
        "loaded": dict(sorted(loaded.items())),
        "problems": problems,
        "cases": _cases(source, pack, listed, invoices, own, _unresolved(receipt_numbers, events)),
        "unlinked": _unlinked(source, pack, listed),
    }


def _cases(
    source: ExportSource,
    pack: PackSpec,
    listed: list[tuple[str, str, str | None]],
    invoices: dict[tuple[str, str], str | None],
    own: dict[tuple[str, str], list[str]],
    unresolved: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Whether a case for each invoice can be evaluated reliably from this export."""
    po_collection = _collection(pack, "purchase_order")
    receipts = _collection(pack, "goods_receipt")
    by_po = _by_po_collections(pack)
    out = []
    for (collection, key), po in invoices.items():
        blocking = list(own.get((collection, key), []))
        notes: list[str] = []
        if not source.complete:
            blocking.append(
                "the export is partial (complete: false), so a record it lacks cannot be told "
                "from one the source lacks; the investigation will send the case to manual review"
            )
        if po is None:
            blocking.append("the invoice names no purchase order, so a case reads none")
        else:
            # Problems of the order itself and of every record a case lists under it.
            blocking += [
                f"{c} {k}: {text}"
                for c, k, listed_po in listed
                if (c == po_collection and k == po) or (c in by_po and listed_po == po)
                for text in own.get((c, k), [])
            ]
            held = po_collection is not None and source.holds(po_collection, po) is True
            if held and not any(c == receipts and listed_po == po for c, _k, listed_po in listed):
                notes.append(
                    f"no receipt is exported for {po}: the investigation will report the "
                    "receiving record as missing (needs information), never as zero received"
                )
            notes += unresolved.get(po, [])
        out.append(
            {
                "invoice": key,
                "purchase_order": po,
                "evaluable": not blocking,
                "blocking": blocking,
                "notes": notes,
            }
        )
    return out


def _unresolved(
    receipt_numbers: dict[str, set[str]], events: dict[str, list[ReceivingEvent]]
) -> dict[str, list[str]]:
    """Per PO, receiving events whose receipt or preceding event this export does not hold:
    an investigation leaves them unapplied and asks for the record (domain/receiving.py)."""
    out: dict[str, list[str]] = defaultdict(list)
    for po, evs in events.items():
        ids = {ev.event_id for ev in evs}
        for ev in evs:
            what = ev.event_type.replace("_", " ")
            if ev.follows is not None and ev.follows not in ids:
                missing = f"event {ev.follows}"
            elif (
                ev.follows is None
                and ev.receipt_number is not None
                and (ev.receipt_number not in receipt_numbers.get(po, set()))
            ):
                missing = f"receipt {ev.receipt_number}"
            else:
                continue
            out[po].append(
                f"{what} {ev.event_id} refers to {missing}, which is not in this export: it will "
                "not be applied, and the investigation will ask for it (needs information)"
            )
    return out


def _unlinked(
    source: ExportSource, pack: PackSpec, listed: list[tuple[str, str, str | None]]
) -> list[dict[str, str]]:
    """Records read by purchase order that no case can reach."""
    po_collection = _collection(pack, "purchase_order")
    by_po = _by_po_collections(pack)
    out = []
    for collection, key, po in listed:
        if collection not in by_po:
            continue
        if po is None:
            reason = "listed under no purchase order, so no case reads it"
        elif po_collection is not None and source.holds(po_collection, po) is False:
            reason = f"listed under {po}, which is not in this export"
        else:
            continue
        out.append({"collection": collection, "key": key, "reason": reason})
    return out


def _collection(pack: PackSpec, record_type: RecordType) -> str | None:
    spec = pack.sources.get(record_type)
    return spec.collection if spec is not None else None


def _by_po_collections(pack: PackSpec) -> set[str]:
    return {c for rt in _BY_PO if (c := _collection(pack, rt)) is not None}
