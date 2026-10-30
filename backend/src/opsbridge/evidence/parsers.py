"""Bounded, type-checked parsing of untrusted source documents.

Supported: application/json, text/csv, text/plain, application/pdf (text layer).
Everything else is rejected. Declared content types are checked against the bytes.
Parsers never fetch URLs, never execute embedded content, and run under size, page,
row and time limits. A PDF without a text layer is `needs_review`. With OCR enabled and
tesseract installed, its pages are read by OCR, but the document stays `needs_review`:
OCR text is labelled as such and never becomes an authoritative business fact.
"""

from __future__ import annotations

import base64
import csv
import functools
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from opsbridge import safejson

from .normalize import normalize_text

ParseStatus = Literal["parsed", "needs_review", "unsupported", "rejected"]

ALLOWED_TYPES = ("application/json", "text/csv", "text/plain", "application/pdf")
MAX_BYTES = 5 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_CSV_ROWS = 10_000
MAX_CSV_COLS = 100
MAX_PDF_PAGES = 50
PDF_TIMEOUT_SECONDS = 10.0
# PDF subprocesses running at once in this process: each may use up to the worker's memory
# cap (pdf_worker.MEMORY_LIMIT_BYTES) for up to the timeout, so a burst of uploads waits here
# instead of multiplying that.
PDF_CONCURRENCY = 2
_PDF_SLOTS = threading.BoundedSemaphore(PDF_CONCURRENCY)
_ACTIVE_PDF = re.compile(rb"/(JavaScript|JS|Launch|EmbeddedFile|OpenAction|AA|RichMedia|XFA)\b")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class TextLine:
    page: int | None
    line: int
    text: str
    origin: Literal["layer", "ocr"] = "layer"

    @property
    def locator(self) -> str:
        if self.page is None:
            return f"text:line={self.line}"
        return f"{'ocr' if self.origin == 'ocr' else 'pdf'}:page={self.page};line={self.line}"


@dataclass(frozen=True)
class OcrSettings:
    """The optional local OCR path for PDFs without a text layer (off unless enabled)."""

    enabled: bool = False
    max_pages: int = 3
    page_timeout_seconds: float = 15.0
    language: str = "eng"

    @classmethod
    def from_settings(cls, settings: Any) -> OcrSettings:
        if settings is None:
            return cls()
        return cls(
            enabled=bool(settings.ocr_enabled),
            max_pages=int(settings.ocr_max_pages),
            page_timeout_seconds=float(settings.ocr_page_timeout_seconds),
            language=str(settings.ocr_language),
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
    ocr_engine: str | None = None  # set when `text` came from OCR (never authoritative)


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


def parse(data: bytes, declared_type: str, ocr: OcrSettings | None = None) -> ParsedDocument:
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
    return _parse_pdf(data, ocr or OcrSettings())


def _reject_constants(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def _parse_json(data: bytes) -> ParsedDocument:
    try:
        # Numbers are parsed as Decimal-compatible strings by later mapping; reject NaN/Infinity.
        value = safejson.loads(
            data.decode("utf-8"),
            max_depth=MAX_JSON_DEPTH,
            parse_constant=_reject_constants,
            parse_float=lambda s: _Num(s),
        )
    except safejson.TooDeep:  # checked before decoding: deep input never reaches the decoder
        return ParsedDocument("application/json", "json/1", "rejected", "JSON nesting too deep")
    except (ValueError, UnicodeDecodeError) as exc:
        return ParsedDocument("application/json", "json/1", "rejected", f"invalid JSON: {exc}")
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


def _parse_pdf(data: bytes, ocr: OcrSettings) -> ParsedDocument:
    import pypdf

    parser = f"pypdf/{pypdf.__version__}"
    flags = ["active_content_present_not_executed"] if _ACTIVE_PDF.search(data) else []
    try:
        with _PDF_SLOTS:
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
        unavailable = ocr_unavailable_reason(ocr)
        if unavailable is None:
            read = _ocr_pages(data, ocr, page_count or 0, parser, flags)
            if read is not None:
                return read
            unavailable = "OCR produced no text"
        reason += f"; {unavailable}"
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


_LANGUAGE = re.compile(r"^[a-z]{3}(\+[a-z]{3})*$")


def ocr_unavailable_reason(ocr: OcrSettings | None = None) -> str | None:
    """Return why OCR cannot run here, or None when the optional OCR path is available."""
    if ocr is None or not ocr.enabled:
        return "OCR is not enabled"
    if not _LANGUAGE.match(ocr.language):
        return f"OCR language {ocr.language!r} is not valid"
    if shutil.which("tesseract") is None:
        return "OCR unavailable: tesseract is not installed"
    return None


@functools.lru_cache(maxsize=1)
def tesseract_version() -> str:
    """'tesseract 5.3.0' (first line of `tesseract --version`), or 'tesseract' if unknown."""
    binary = shutil.which("tesseract")
    if binary is None:
        return "tesseract"
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv: the resolved binary and a flag
            [binary, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except subprocess.TimeoutExpired:
        return "tesseract"
    first = (out.stdout or out.stderr).strip().splitlines()[:1]
    return first[0].strip() if first and first[0].startswith("tesseract") else "tesseract"


def _ocr_pages(
    data: bytes, ocr: OcrSettings, page_count: int, parser: str, flags: list[str]
) -> ParsedDocument | None:
    """OCR the embedded images of the first pages; None if nothing could be read."""
    pages = min(page_count, ocr.max_pages)
    try:
        with _PDF_SLOTS:
            proc = subprocess.run(  # noqa: S603 - fixed argv: this interpreter + our worker module
                [
                    sys.executable,
                    "-m",
                    "opsbridge.evidence.pdf_worker",
                    str(MAX_PDF_PAGES),
                    "images",
                    str(pages),
                ],
                input=data,
                capture_output=True,
                timeout=PDF_TIMEOUT_SECONDS,
                check=False,
            )
        result = json.loads(proc.stdout.decode("utf-8"))
    except (subprocess.TimeoutExpired, ValueError, UnicodeDecodeError):
        return None
    if result.get("status") != "ok":
        return None
    engine = tesseract_version()
    by_page: dict[int, list[str]] = {}
    for page_no, b64 in result["images"]:
        text = ocr_image(base64.b64decode(b64), ocr.page_timeout_seconds, ocr.language)
        if text and text.strip():
            by_page.setdefault(int(page_no), []).append(text)
    if not by_page:
        return None
    lines: list[TextLine] = []
    chunks: list[str] = []
    for page_no in sorted(by_page):
        page_text = normalize_text("\n".join(by_page[page_no]))
        chunks.append(page_text)
        for i, t in enumerate(page_text.splitlines()):
            lines.append(TextLine(page_no, i + 1, t, origin="ocr"))
    problems = [str(p) for p in result.get("problems", [])][:5]
    skipped = page_count - pages
    detail = (
        f"no extractable text layer; text read by OCR ({engine}) from "
        f"{len(by_page)} page(s) is not authoritative: check it against the page image"
    )
    if skipped > 0:
        detail += f"; {skipped} later page(s) not read (OCR page limit {ocr.max_pages})"
    if problems:
        detail += "; " + "; ".join(problems)
    return ParsedDocument(
        "application/pdf",
        f"{parser}+{engine.replace(' ', '/')}"[:64],
        "needs_review",
        detail,
        text="".join(chunks),
        lines=lines,
        page_count=page_count,
        flags=[*flags, "ocr_text_not_authoritative"],
        ocr_engine=engine,
    )


def ocr_image(image_bytes: bytes, timeout: float = 15.0, language: str = "eng") -> str | None:
    """OCR of one PNG image via the tesseract CLI. Output is never authoritative."""
    binary = shutil.which("tesseract")
    if binary is None or not _LANGUAGE.match(language):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "page.png"
        src.write_bytes(image_bytes)
        try:
            # Fixed argument vector: the resolved tesseract binary, our own temp file and a
            # validated language code; one thread so a page cannot take every core.
            out = subprocess.run(  # noqa: S603
                [binary, str(src), "stdout", "-l", language],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
        except subprocess.TimeoutExpired:
            return None
        return out.stdout if out.returncode == 0 else None
