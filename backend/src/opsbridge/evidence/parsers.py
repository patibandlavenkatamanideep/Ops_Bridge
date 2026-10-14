"""Bounded, type-checked parsing of untrusted source documents.

Supported: application/json, text/csv, text/plain, application/pdf (text layer).
Everything else is rejected. Declared content types are checked against the bytes.
Parsers never fetch URLs, never execute embedded content, and run under size, page,
row and time limits. A PDF without a text layer is `needs_review` (OCR is optional
and never authoritative).
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .normalize import normalize_text

ParseStatus = Literal["parsed", "needs_review", "unsupported", "rejected"]

ALLOWED_TYPES = ("application/json", "text/csv", "text/plain", "application/pdf")
MAX_BYTES = 5 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_CSV_ROWS = 10_000
MAX_CSV_COLS = 100
MAX_PDF_PAGES = 50
PDF_TIMEOUT_SECONDS = 10.0
_ACTIVE_PDF = re.compile(rb"/(JavaScript|JS|Launch|EmbeddedFile|OpenAction|AA|RichMedia|XFA)\b")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class TextLine:
    page: int | None
    line: int
    text: str

    @property
    def locator(self) -> str:
        return (
            f"pdf:page={self.page};line={self.line}"
            if self.page is not None
            else f"text:line={self.line}"
        )


@dataclass
class ParsedDocument:
    content_type: str
    parser: str
    status: ParseStatus
    detail: str | None = None
    text: str = ""
    lines: list[TextLine] = field(default_factory=list)
    data: Any = None  # JSON value or list of CSV row dicts
    csv_header: list[str] | None = None
    page_count: int | None = None
    flags: list[str] = field(default_factory=list)


def safe_filename(name: str | None) -> str | None:
    if not name:
        return None
    base = Path(name.replace("\\", "/")).name
    cleaned = _SAFE_NAME.sub("_", base).lstrip("._")[:100]
    return cleaned or None


def sniff(data: bytes) -> str | None:
    head = data[:1024].lstrip()
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if head[:1] in (b"{", b"["):
        return "application/json"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if b"\x00" in data:
        return None
    lowered = head[:200].lower()
    if lowered.startswith((b"<!doctype", b"<html", b"<script", b"<svg", b"<?xml")):
        return "text/html"
    return "text/plain"


def parse(data: bytes, declared_type: str) -> ParsedDocument:
    declared = declared_type.split(";")[0].strip().lower()
    if len(data) > MAX_BYTES:
        return ParsedDocument(declared, "none", "rejected", f"file exceeds {MAX_BYTES} bytes")
    if declared not in ALLOWED_TYPES:
        return ParsedDocument(
            declared, "none", "rejected", f"content type {declared!r} is not supported"
        )
    sniffed = sniff(data)
    compatible = {
        "application/pdf": {"application/pdf"},
        "application/json": {"application/json"},
        "text/csv": {"text/plain"},
        "text/plain": {"text/plain", "application/json"},
    }[declared]
    if sniffed not in compatible:
        return ParsedDocument(
            declared,
            "none",
            "rejected",
            f"declared {declared} but content looks like {sniffed or 'binary'}",
        )
    if declared == "application/json":
        return _parse_json(data)
    if declared == "text/csv":
        return _parse_csv(data)
    if declared == "text/plain":
        return _parse_text(data)
    return _parse_pdf(data)


def _depth(value: Any, limit: int, level: int = 0) -> bool:
    if level > limit:
        return False
    if isinstance(value, dict):
        return all(_depth(v, limit, level + 1) for v in value.values())
    if isinstance(value, list):
        return all(_depth(v, limit, level + 1) for v in value)
    return True


def _reject_constants(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def _parse_json(data: bytes) -> ParsedDocument:
    try:
        # Numbers are parsed as Decimal-compatible strings by later mapping; reject NaN/Infinity.
        value = json.loads(
            data.decode("utf-8"), parse_constant=_reject_constants, parse_float=lambda s: _Num(s)
        )
    except (ValueError, UnicodeDecodeError) as exc:
        return ParsedDocument("application/json", "json/1", "rejected", f"invalid JSON: {exc}")
    if not _depth(value, MAX_JSON_DEPTH):
        return ParsedDocument("application/json", "json/1", "rejected", "JSON nesting too deep")
    value = _unwrap(value)
    pretty = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    text = normalize_text(pretty)
    lines = [TextLine(None, i + 1, t) for i, t in enumerate(text.splitlines())]
    return ParsedDocument(
        "application/json", "json/1", "parsed", text=text, lines=lines, data=value
    )


class _Num(str):
    """Marks a JSON number with a fractional part; kept as its exact source text."""


def _unwrap(value: Any) -> Any:
    if isinstance(value, _Num):
        return str(value)
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


def _parse_csv(data: bytes) -> ParsedDocument:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ParsedDocument("text/csv", "csv/1", "rejected", "CSV is not UTF-8")
    reader = csv.reader(io.StringIO(text))
    try:
        rows = []
        for i, row in enumerate(reader):
            if i > MAX_CSV_ROWS:
                return ParsedDocument(
                    "text/csv", "csv/1", "rejected", f"more than {MAX_CSV_ROWS} rows"
                )
            if len(row) > MAX_CSV_COLS:
                return ParsedDocument(
                    "text/csv", "csv/1", "rejected", f"more than {MAX_CSV_COLS} columns"
                )
            rows.append(row)
    except csv.Error as exc:
        return ParsedDocument("text/csv", "csv/1", "rejected", f"malformed CSV: {exc}")
    if not rows:
        return ParsedDocument("text/csv", "csv/1", "rejected", "empty CSV")
    header = [h.strip() for h in rows[0]]
    if len(set(header)) != len(header) or any(not h for h in header):
        return ParsedDocument(
            "text/csv", "csv/1", "rejected", "CSV header has blank or duplicate columns"
        )
    records: list[dict[str, str]] = []
    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(header):
            return ParsedDocument(
                "text/csv",
                "csv/1",
                "rejected",
                f"row {len(records) + 2} has {len(row)} cells, expected {len(header)}",
            )
        records.append(dict(zip(header, (c.strip() for c in row), strict=True)))
    norm = normalize_text(text)
    lines = [TextLine(None, i + 1, t) for i, t in enumerate(norm.splitlines())]
    return ParsedDocument(
        "text/csv", "csv/1", "parsed", text=norm, lines=lines, data=records, csv_header=header
    )


def _parse_text(data: bytes) -> ParsedDocument:
    norm = normalize_text(data.decode("utf-8"))
    lines = [TextLine(None, i + 1, t) for i, t in enumerate(norm.splitlines())]
    return ParsedDocument("text/plain", "text/1", "parsed", text=norm, lines=lines)


def _parse_pdf(data: bytes) -> ParsedDocument:
    import pypdf

    parser = f"pypdf/{pypdf.__version__}"
    flags = ["active_content_present_not_executed"] if _ACTIVE_PDF.search(data) else []
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv: this interpreter + our worker module
            [sys.executable, "-m", "opsbridge.evidence.pdf_worker", str(MAX_PDF_PAGES)],
            input=data,
            capture_output=True,
            timeout=PDF_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ParsedDocument(
            "application/pdf", parser, "rejected", "PDF parsing timed out", flags=flags
        )
    try:
        result = json.loads(proc.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ParsedDocument(
            "application/pdf", parser, "rejected", "PDF parser crashed", flags=flags
        )
    status, detail, page_count, pages = (
        result["status"],
        result["detail"],
        result["page_count"],
        result["pages"],
    )
    if status != "ok":
        return ParsedDocument(
            "application/pdf", parser, "rejected", detail, page_count=page_count, flags=flags
        )
    images = int(result.get("images", 0))
    if images:
        flags.append(f"embedded_images={images}")
    lines: list[TextLine] = []
    chunks: list[str] = []
    for page_no, raw in pages:
        page_text = normalize_text(raw)
        chunks.append(page_text)
        for i, t in enumerate(page_text.splitlines()):
            lines.append(TextLine(page_no, i + 1, t))
    text = "".join(chunks)
    if len(text.strip()) < 20:
        reason = "no extractable text layer (likely a scanned image)"
        ocr = ocr_unavailable_reason()
        if ocr:
            reason += f"; {ocr}"
        return ParsedDocument(
            "application/pdf",
            parser,
            "needs_review",
            reason,
            text=text,
            lines=lines,
            page_count=page_count,
            flags=flags,
        )
    return ParsedDocument(
        "application/pdf",
        parser,
        "parsed",
        text=text,
        lines=lines,
        page_count=page_count,
        flags=flags,
    )


def ocr_unavailable_reason() -> str | None:
    """Return why OCR cannot run here, or None when the optional OCR path is available."""
    if shutil.which("tesseract") is None:
        return "OCR unavailable: tesseract is not installed"
    return None


def ocr_image(image_bytes: bytes, timeout: float = 20.0) -> str | None:
    """Optional OCR of one image via the tesseract CLI. Output is never authoritative."""
    binary = shutil.which("tesseract")
    if binary is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "page.png"
        src.write_bytes(image_bytes)
        try:
            # Fixed argument vector: the resolved tesseract binary and our own temp file.
            out = subprocess.run(  # noqa: S603
                [binary, str(src), "stdout"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None
        return out.stdout if out.returncode == 0 else None
