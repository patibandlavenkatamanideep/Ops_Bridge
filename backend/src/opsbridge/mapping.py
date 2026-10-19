"""Organization-pack field mapping: source documents → canonical procurement records.

The engine is generic and data-driven. A pack declares, per record type, how fields are
read (JSON path, CSV column, or a named group of a pack-supplied line pattern over PDF
text) and which allowlisted transform applies. There is no organization-specific code.

Every mapped value carries a SourceRef locator. Values that cannot be read or validated
are reported as problems; they are never defaulted silently (explicit `default` values
in the pack are recorded as such).
"""

from __future__ import annotations

import dataclasses
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from opsbridge.domain.money import DecimalParseError, parse_decimal
from opsbridge.domain.records import (
    GoodsReceipt,
    Invoice,
    InvoiceAllocation,
    InvoiceLine,
    POLine,
    PurchaseOrder,
    ReceiptLine,
    SourceRef,
    SupplierAssertion,
)
from opsbridge.evidence.parsers import ParsedDocument

RecordType = Literal[
    "purchase_order", "invoice", "goods_receipt", "allocation_history", "supplier_message"
]
Transform = Literal["str", "decimal", "date", "upper", "lower", "strip"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldSpec(_Strict):
    path: str | None = None  # JSON dotted path (relative to the record)
    column: str | None = None  # CSV column
    group: str | None = None  # named regex group (pdf_text / text)
    transform: Transform = "str"
    date_format: str | None = None
    map: dict[str, str] | None = None
    default: str | None = None
    optional: bool = False


class LinesSpec(_Strict):
    path: str | None = None  # JSON array path
    pattern: str | None = None  # regex applied to each text line (pdf_text)
    record: dict[str, FieldSpec]


class SourceSpec(_Strict):
    format: Literal["json", "csv", "pdf_text", "text"]
    collection: str | None = None  # simulator collection name (json/csv/text sources)
    root: str | None = (
        None  # JSON path to an array of records (json); None = document is one record
    )
    group_by: str | None = None  # CSV column grouping rows into one record
    header_patterns: dict[str, str] = Field(
        default_factory=dict
    )  # pdf_text: field → regex with (?P<v>)
    record: dict[str, FieldSpec] = Field(default_factory=dict)
    lines: LinesSpec | None = None
    assertion_patterns: list[str] = Field(default_factory=list)  # text: regexes with named groups
    amount_check: bool = False  # pdf_text: verify line amounts and total


class PackSpec(_Strict):
    schema_version: Literal[1] = 1
    pack_id: str
    version: int
    display_name: str
    policy_id: str
    sources: dict[RecordType, SourceSpec]


@dataclass(frozen=True)
class DocRef:
    document_id: str
    version_id: str
    sha256: str
    classification: str = "internal"

    def ref(self, locator: str, field_name: str) -> SourceRef:
        return SourceRef(
            self.document_id, self.version_id, self.sha256, locator, field_name, self.classification
        )


@dataclass
class ExtractedValue:
    record_type: str
    record_key: str
    field_path: str
    value: str | None
    value_type: str
    locator: str
    method: str
    status: str
    note: str | None = None


@dataclass
class MappingResult:
    purchase_orders: list[PurchaseOrder] = field(default_factory=list)
    invoices: list[Invoice] = field(default_factory=list)
    receipts: list[GoodsReceipt] = field(default_factory=list)
    allocations: list[InvoiceAllocation] = field(default_factory=list)
    assertions: list[SupplierAssertion] = field(default_factory=list)
    fields: list[ExtractedValue] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


class _Missing(Exception):
    pass


def _json_get(obj: Any, path: str) -> tuple[Any, str]:
    """Resolve a dotted path; returns (value, RFC 6901 pointer suffix)."""
    pointer = ""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            if not part.isdigit() or int(part) >= len(cur):
                raise _Missing(path)
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            if part not in cur:
                raise _Missing(path)
            cur = cur[part]
        else:
            raise _Missing(path)
        pointer += "/" + part.replace("~", "~0").replace("/", "~1")
    return cur, pointer


def _transform(raw: Any, spec: FieldSpec) -> tuple[Any, str]:
    if spec.map is not None:
        key = str(raw).strip()
        if key not in spec.map:
            raise ValueError(f"value {key!r} is not in the pack's value map")
        return spec.map[key], "enum"
    if spec.transform == "decimal":
        return parse_decimal(raw), "decimal"
    if spec.transform == "date":
        text = str(raw).strip()
        if spec.date_format:
            return datetime.strptime(text, spec.date_format).date(), "date"  # noqa: DTZ007
        return date.fromisoformat(text), "date"
    if raw is None or isinstance(raw, (dict, list)):
        raise ValueError("expected a scalar value")
    text = str(raw).strip()
    if spec.transform == "upper":
        return text.upper(), "string"
    if spec.transform == "lower":
        return text.lower(), "string"
    return text, "string"


def _render(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


class _Reader:
    """Reads one record's fields from a source, collecting values, refs and problems."""

    def __init__(
        self, out: MappingResult, doc: DocRef, record_type: str, method: str, status: str
    ) -> None:
        self.out = out
        self.doc = doc
        self.record_type = record_type
        self.method = method
        self.status = status

    def read(
        self,
        specs: dict[str, FieldSpec],
        getter: Any,
        prefix: str,
        record_key: str,
        refs: dict[str, SourceRef],
    ) -> dict[str, Any] | None:
        values: dict[str, Any] = {}
        ok = True
        for name, spec in specs.items():
            canonical = f"{prefix}.{name}"
            try:
                raw, locator = getter(spec)
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    raise _Missing(name)
                value, vtype = _transform(raw, spec)
            except _Missing:
                if spec.default is not None:
                    value, vtype = _transform(spec.default, spec.model_copy(update={"map": None}))
                    locator = "pack:default"
                elif spec.optional:
                    values[name] = None
                    continue
                else:
                    self.out.problems.append(f"{record_key}: missing {canonical}")
                    ok = False
                    continue
            except (ValueError, DecimalParseError) as exc:
                self.out.problems.append(f"{record_key}: invalid {canonical}: {exc}")
                ok = False
                continue
            values[name] = value
            refs[canonical] = self.doc.ref(locator, canonical)
            self.out.fields.append(
                ExtractedValue(
                    self.record_type,
                    record_key,
                    canonical,
                    _render(value),
                    vtype,
                    locator,
                    "pack_default" if locator == "pack:default" else self.method,
                    self.status,
                )
            )
        return values if ok else None


def map_ocr_candidates(
    pack: PackSpec, record_type: RecordType, parsed: ParsedDocument, doc: DocRef
) -> MappingResult:
    """Field values read from OCR text, for a person to review. They are stored as
    `ocr` / `needs_review` and no record is built from them, so the matching rules never use
    them: an OCR guess must not become an authoritative business fact."""
    if parsed.ocr_engine is None:
        raise ValueError("not OCR text")
    full = map_document(pack, record_type, dataclasses.replace(parsed, status="parsed"), doc)
    note = f"read by OCR ({parsed.ocr_engine}); not used by the matching rules"
    return MappingResult(
        fields=[
            dataclasses.replace(f, method="ocr", status="needs_review", note=note)
            for f in full.fields
        ],
        problems=list(full.problems),
    )


def map_document(
    pack: PackSpec, record_type: RecordType, parsed: ParsedDocument, doc: DocRef
) -> MappingResult:
    out = MappingResult()
    spec = pack.sources.get(record_type)
    if spec is None:
        out.problems.append(f"pack {pack.pack_id} has no mapping for {record_type}")
        return out
    if parsed.status not in ("parsed",):
        out.problems.append(f"document not parsed: {parsed.status} ({parsed.detail})")
        return out
    if spec.format == "json":
        _map_json(spec, record_type, parsed, doc, out)
    elif spec.format == "csv":
        _map_csv(spec, record_type, parsed, doc, out)
    elif spec.format == "pdf_text":
        _map_pdf_text(spec, record_type, parsed, doc, out)
    else:
        _map_text(spec, record_type, parsed, doc, out)
    return out


# --- JSON ------------------------------------------------------------------------------


def _map_json(
    spec: SourceSpec, rtype: RecordType, parsed: ParsedDocument, doc: DocRef, out: MappingResult
) -> None:
    data = parsed.data
    base_pointer = ""
    items: list[tuple[Any, str]]
    if spec.root:
        try:
            arr, base_pointer = _json_get(data, spec.root)
        except _Missing:
            out.problems.append(f"missing record array {spec.root}")
            return
        if not isinstance(arr, list):
            out.problems.append(f"{spec.root} is not an array")
            return
        items = [(rec, f"{base_pointer}/{i}") for i, rec in enumerate(arr)]
    else:
        items = [(data, "")]
    reader = _Reader(out, doc, rtype, "structured", "authoritative")
    prefix = _prefix(rtype)
    for idx, (rec, ptr) in enumerate(items):

        def getter(fs: FieldSpec, rec: Any = rec, ptr: str = ptr) -> tuple[Any, str]:
            if fs.path is None:
                raise _Missing("no path")
            value, sub = _json_get(rec, fs.path)
            return value, f"json:{ptr}{sub}"

        key_hint = f"{rtype}[{idx}]"
        refs: dict[str, SourceRef] = {}
        header = reader.read(spec.record, getter, prefix, key_hint, refs)
        lines: list[tuple[dict[str, Any], dict[str, SourceRef]]] = []
        if spec.lines is not None and spec.lines.path:
            try:
                arr, sub = _json_get(rec, spec.lines.path)
            except _Missing:
                out.problems.append(f"{key_hint}: missing lines array {spec.lines.path}")
                continue
            for li, line_rec in enumerate(arr if isinstance(arr, list) else []):
                lptr = f"{ptr}{sub}/{li}"

                def lgetter(
                    fs: FieldSpec, line_rec: Any = line_rec, lptr: str = lptr
                ) -> tuple[Any, str]:
                    if fs.path is None:
                        raise _Missing("no path")
                    value, lsub = _json_get(line_rec, fs.path)
                    return value, f"json:{lptr}{lsub}"

                lrefs: dict[str, SourceRef] = {}
                lv = reader.read(
                    spec.lines.record, lgetter, _line_prefix(rtype), f"{key_hint}.line[{li}]", lrefs
                )
                if lv is not None:
                    lines.append((lv, lrefs))
        if header is not None:
            _build(rtype, header, refs, lines, out)


# --- CSV -------------------------------------------------------------------------------


def _map_csv(
    spec: SourceSpec, rtype: RecordType, parsed: ParsedDocument, doc: DocRef, out: MappingResult
) -> None:
    rows: list[dict[str, str]] = parsed.data or []
    reader = _Reader(out, doc, rtype, "structured", "authoritative")
    groups: dict[str, list[tuple[int, dict[str, str]]]] = defaultdict(list)
    for i, row in enumerate(rows):
        key = row.get(spec.group_by, "") if spec.group_by else str(i)
        groups[key].append((i + 2, row))  # +2: header is row 1
    for key, members in groups.items():
        first_row_no, first = members[0]

        def getter(
            fs: FieldSpec, row: dict[str, str] = first, rn: int = first_row_no
        ) -> tuple[Any, str]:
            if fs.column is None or fs.column not in row:
                raise _Missing(fs.column or "")
            return row[fs.column], f"csv:row={rn};col={fs.column}"

        refs: dict[str, SourceRef] = {}
        header = reader.read(spec.record, getter, _prefix(rtype), f"{rtype}:{key}", refs)
        lines = []
        if spec.lines is not None:
            for rn, row in members:

                def lgetter(
                    fs: FieldSpec, row: dict[str, str] = row, rn: int = rn
                ) -> tuple[Any, str]:
                    if fs.column is None or fs.column not in row:
                        raise _Missing(fs.column or "")
                    return row[fs.column], f"csv:row={rn};col={fs.column}"

                lrefs: dict[str, SourceRef] = {}
                lv = reader.read(
                    spec.lines.record, lgetter, _line_prefix(rtype), f"{rtype}:{key}:row{rn}", lrefs
                )
                if lv is not None:
                    lines.append((lv, lrefs))
        if header is not None:
            _build(rtype, header, refs, lines, out)


# --- PDF text (pattern extraction) ----------------------------------------------------


def _map_pdf_text(
    spec: SourceSpec, rtype: RecordType, parsed: ParsedDocument, doc: DocRef, out: MappingResult
) -> None:
    found: dict[str, tuple[str, str]] = {}
    for name, pattern in spec.header_patterns.items():
        rx = re.compile(pattern)
        for tl in parsed.lines:
            m = rx.search(tl.text[:500])
            if m:
                found[name] = (m.group("v"), tl.locator)
                break
    # Extraction from a text layer is deterministic but not authoritative until checks pass.
    provisional = len(out.fields)
    reader = _Reader(out, doc, rtype, "parsed", "authoritative")

    def getter(fs: FieldSpec) -> tuple[Any, str]:
        key = fs.group or ""
        if key not in found:
            raise _Missing(key)
        return found[key]

    refs: dict[str, SourceRef] = {}
    header = reader.read(spec.record, getter, _prefix(rtype), rtype, refs)
    lines = []
    amounts: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    if spec.lines is not None and spec.lines.pattern:
        rx = re.compile(spec.lines.pattern)
        for tl in parsed.lines:
            m = rx.match(tl.text[:500])
            if not m:
                continue
            groups = m.groupdict()

            def lgetter(
                fs: FieldSpec, groups: dict[str, str] = groups, loc: str = tl.locator
            ) -> tuple[Any, str]:
                key = fs.group or ""
                if groups.get(key) in (None, ""):
                    raise _Missing(key)
                return groups[key], loc

            lrefs: dict[str, SourceRef] = {}
            lv = reader.read(
                spec.lines.record, lgetter, _line_prefix(rtype), f"{rtype}:{tl.locator}", lrefs
            )
            if lv is not None:
                lines.append((lv, lrefs))
                if "amount" in groups and groups.get("amount"):
                    try:
                        amounts.append(
                            (
                                lv["quantity"],
                                lv["unit_price"],
                                lv.get("discount_amount") or Decimal(0),
                                parse_decimal(groups["amount"]),
                            )
                        )
                    except (DecimalParseError, KeyError):
                        out.problems.append(f"{rtype}: unreadable line amount at {tl.locator}")
    if spec.amount_check:
        issues = []
        for qty, price, disc, amount in amounts:
            if qty * price - disc != amount:
                issues.append(f"line amount {amount} != {qty} x {price} - {disc}")
        total_raw = found.get("total")
        if total_raw is not None:
            try:
                if sum((a[3] for a in amounts), Decimal(0)) != parse_decimal(total_raw[0]):
                    issues.append("line amounts do not sum to the invoice total")
            except DecimalParseError:
                issues.append("invoice total unreadable")
        else:
            issues.append("invoice total not found")
        if not lines:
            issues.append("no invoice lines recognized")
        if issues:
            for fv in out.fields[provisional:]:
                fv.status = "needs_review"
                fv.note = "; ".join(issues)
            out.problems.extend(f"{rtype}: {i}" for i in issues)
            return
    if header is not None:
        _build(rtype, header, refs, lines, out)


# --- Plain text (supplier correspondence assertions) ----------------------------------


def _map_text(
    spec: SourceSpec, rtype: RecordType, parsed: ParsedDocument, doc: DocRef, out: MappingResult
) -> None:
    patterns = [re.compile(p) for p in spec.assertion_patterns]
    for tl in parsed.lines:
        for rx in patterns:
            m = rx.search(tl.text[:500])
            if not m:
                continue
            g = m.groupdict()
            try:
                qty = parse_decimal(g["quantity"])
            except (KeyError, DecimalParseError):
                continue
            po = g.get("po_number")
            if not po:
                out.problems.append(f"{tl.locator}: delivery claim without a PO reference")
                continue
            ref = doc.ref(tl.locator, "supplier_message.delivered_quantity")
            out.assertions.append(
                SupplierAssertion(
                    po_number=po.strip().upper(),
                    po_line_number=g.get("po_line_number"),
                    claim="delivered_quantity",
                    quantity=qty,
                    unit=g.get("unit"),
                    extraction_status="unconfirmed",
                    refs={"supplier_message.claim": ref},
                )
            )
            out.fields.append(
                ExtractedValue(
                    rtype,
                    f"{po}:{tl.locator}",
                    "supplier_message.delivered_quantity",
                    format(qty, "f"),
                    "decimal",
                    tl.locator,
                    "pattern",
                    "needs_review",
                    "pattern match in free text; unconfirmed",
                )
            )
            break


# --- builders -------------------------------------------------------------------------


def _prefix(rtype: str) -> str:
    return {
        "purchase_order": "purchase_order",
        "invoice": "invoice",
        "goods_receipt": "goods_receipt",
        "allocation_history": "allocation",
        "supplier_message": "supplier_message",
    }[rtype]


def _line_prefix(rtype: str) -> str:
    return {
        "purchase_order": "po_line",
        "invoice": "invoice_line",
        "goods_receipt": "receipt_line",
        "allocation_history": "allocation",
        "supplier_message": "supplier_message",
    }[rtype]


def _build(
    rtype: RecordType,
    h: dict[str, Any],
    refs: dict[str, SourceRef],
    lines: list[tuple[dict[str, Any], dict[str, SourceRef]]],
    out: MappingResult,
) -> None:
    if rtype == "purchase_order":
        out.purchase_orders.append(
            PurchaseOrder(
                po_number=h["po_number"],
                supplier_id=h["supplier_id"],
                supplier_name=h["supplier_name"],
                currency=h["currency"],
                status=h.get("status") or "open",
                lines=tuple(
                    POLine(
                        line_number=lv["line_number"],
                        item_code=lv.get("item_code"),
                        description=lv.get("description") or "",
                        quantity=lv["quantity"],
                        unit=lv["unit"],
                        unit_price=lv["unit_price"],
                        discount_amount=lv.get("discount_amount") or Decimal(0),
                        refs=lr,
                    )
                    for lv, lr in lines
                ),
                refs=refs,
            )
        )
    elif rtype == "invoice":
        out.invoices.append(
            Invoice(
                invoice_number=h["invoice_number"],
                supplier_id=h.get("supplier_id"),
                supplier_name=h.get("supplier_name") or "",
                po_number=h.get("po_number"),
                currency=h["currency"],
                invoice_date=h["invoice_date"],
                status=h.get("status") or "open",
                lines=tuple(
                    InvoiceLine(
                        line_number=lv["line_number"],
                        po_line_number=lv.get("po_line_number"),
                        item_code=lv.get("item_code"),
                        description=lv.get("description") or "",
                        quantity=lv["quantity"],
                        unit=lv["unit"],
                        unit_price=lv["unit_price"],
                        discount_amount=lv.get("discount_amount") or Decimal(0),
                        kind=lv.get("kind") or "goods",
                        refs=lr,
                    )
                    for lv, lr in lines
                ),
                refs=refs,
            )
        )
    elif rtype == "goods_receipt":
        out.receipts.append(
            GoodsReceipt(
                receipt_number=h["receipt_number"],
                po_number=h["po_number"],
                received_date=h["received_date"],
                lines=tuple(
                    ReceiptLine(
                        po_line_number=lv["po_line_number"],
                        quantity=lv["quantity"],
                        unit=lv["unit"],
                        kind=lv.get("kind") or "receipt",
                        status=lv.get("status") or "accepted",
                        refs=lr,
                    )
                    for lv, lr in lines
                ),
                refs=refs,
            )
        )
    elif rtype == "allocation_history":
        out.allocations.append(
            InvoiceAllocation(
                invoice_number=h["invoice_number"],
                po_number=h["po_number"],
                po_line_number=h["po_line_number"],
                quantity=h["quantity"],
                status=h.get("status") or "active",
                refs={k: v for k, v in refs.items() if k == "allocation.quantity"},
            )
        )
