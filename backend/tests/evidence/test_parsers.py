"""Bounded parsing of untrusted documents (OB-04, adversarial inputs)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest

from opsbridge.evidence import parsers
from opsbridge.evidence.normalize import NORMALIZATION_VERSION, normalize_text, normalized_digest
from opsbridge.evidence.parsers import MAX_BYTES, parse, safe_filename
from opsbridge.simulator.minipdf import image_pdf, text_pdf

DOCS = Path(__file__).resolve().parents[3] / "fixtures" / "documents"


def test_independently_generated_pdf_parses_with_line_locators() -> None:
    doc = parse((DOCS / "cupsfilter_text.pdf").read_bytes(), "application/pdf")
    assert doc.status == "parsed" and doc.page_count == 1
    assert doc.parser.startswith("pypdf/")
    by_text = {line.text: line.locator for line in doc.lines}
    assert by_text["Invoice No: INV-CUPS-1"] == "pdf:page=1;line=2"


def test_hand_authored_malformed_pdf_is_rejected() -> None:
    doc = parse((DOCS / "malformed_truncated.pdf").read_bytes(), "application/pdf")
    assert doc.status == "rejected"
    assert "malformed" in (doc.detail or "")


def test_image_only_pdf_needs_review_and_never_invents_text() -> None:
    from PIL import Image

    img = Image.new("L", (400, 200), 255)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    doc = parse(image_pdf(buf.getvalue(), 400, 200), "application/pdf")
    assert doc.status == "needs_review"
    assert "no extractable text layer" in (doc.detail or "")
    assert doc.text.strip() == ""


def test_active_pdf_content_is_flagged_not_executed() -> None:
    pdf = text_pdf([["Invoice No: INV-1"]]).replace(
        b"/Type /Catalog", b"/Type /Catalog /OpenAction << /S /JavaScript /JS (app.alert(1)) >>"
    )
    doc = parse(pdf, "application/pdf")
    assert "active_content_present_not_executed" in doc.flags


@pytest.mark.parametrize(
    ("data", "declared", "reason"),
    [
        (b'{"a": 1}', "application/pdf", "looks like"),
        (b"<html><script>alert(1)</script></html>", "text/plain", "looks like text/html"),
        (b"\x89PNG\r\n\x1a\n....", "image/png", "not supported"),
        (b"%PDF-1.4 fake", "text/csv", "looks like"),
        (b"x" * (MAX_BYTES + 1), "text/plain", "exceeds"),
    ],
)
def test_type_and_size_checks_reject_predictably(data: bytes, declared: str, reason: str) -> None:
    doc = parse(data, declared)
    assert doc.status == "rejected"
    assert reason in (doc.detail or "")


def test_json_limits_and_exact_numbers() -> None:
    deep = b"[" * 40 + b"]" * 40
    assert parse(deep, "application/json").status == "rejected"
    # Nested far beyond the interpreter's recursion limit (the decoder used to raise
    # RecursionError out of parse(): an upload answered 500, a source record failed the step).
    abyss = parse(b"[" * 100_000 + b"]" * 100_000, "application/json")
    assert (abyss.status, abyss.detail) == ("rejected", "JSON nesting too deep")
    brackets_in_text = b'{"note": "' + b"[" * 100_000 + b'"}'  # text, not nesting
    assert parse(brackets_in_text, "application/json").status == "parsed"
    assert parse(b'{"x": NaN}', "application/json").status == "rejected"
    ok = parse(b'{"price": 185.10, "qty": 20}', "application/json")
    assert ok.data == {"price": "185.10", "qty": 20}  # fractional numbers keep their exact text


def test_csv_shape_is_validated() -> None:
    assert parse(b"a,b\n1,2,3\n", "text/csv").status == "rejected"
    assert parse(b"a,a\n1,2\n", "text/csv").status == "rejected"
    ok = parse("﻿po,qty\nPO-1,5\n".encode(), "text/csv")
    assert ok.status == "parsed" and ok.data == [{"po": "PO-1", "qty": "5"}]


def test_raw_and_normalized_digests_are_separate() -> None:
    crlf, lf = "Line one  \r\nLine two\r\n", "Line one\nLine two\n"
    assert normalize_text(crlf) == normalize_text(lf)
    assert normalized_digest(crlf) == normalized_digest(lf)
    assert crlf.encode() != lf.encode()  # raw bytes (and raw digests) differ
    assert NORMALIZATION_VERSION == "opsbridge-norm/1"


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\boot.ini", "boot.ini"),
        ("invoice (1).pdf", "invoice_1_.pdf"),
        (".hidden", "hidden"),
        ("", None),
        ("///", None),
    ],
)
def test_safe_filename(raw: str, safe: str | None) -> None:
    assert safe_filename(raw) == safe


def _inflating_pdf(pages: int, inflated_bytes: int) -> bytes:
    """A small PDF whose content streams each inflate to `inflated_bytes` of text."""
    line = b"BT /F1 12 Tf 72 720 Td (" + b"A" * 200 + b") Tj ET\n"
    stream = zlib.compress(line * (inflated_bytes // len(line)), 9)
    kids = b" ".join(b"%d 0 R" % (4 + 2 * i) for i in range(pages))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, pages),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i in range(pages):
        objs.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (5 + 2 * i)
        )
        objs.append(
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream)
            + stream
            + b"\nendstream"
        )
    out, offsets = bytearray(b"%PDF-1.7\n"), []
    for n, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="address-space limits are enforced only on Linux (the container and CI)",
)
def test_a_pdf_inflating_past_the_memory_cap_is_rejected_before_the_timeout() -> None:
    """Under 1 MB on disk, about 850 MB in the parser before the 10 s timeout stopped it."""
    bomb = _inflating_pdf(pages=3, inflated_bytes=70_000_000)
    assert len(bomb) < 1_000_000
    started = time.monotonic()
    doc = parse(bomb, "application/pdf")
    assert (doc.status, doc.detail) == ("rejected", "PDF needs more memory than the parser may use")
    assert time.monotonic() - started < parsers.PDF_TIMEOUT_SECONDS


def test_no_more_than_two_pdfs_are_parsed_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    running, peak, lock = 0, 0, threading.Lock()

    def slow_worker(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.2)
        with lock:
            running -= 1
        out = {
            "status": "ok",
            "detail": None,
            "page_count": 1,
            "pages": [[1, "Invoice"]],
            "images": 0,
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(out).encode(), b"")

    monkeypatch.setattr(parsers.subprocess, "run", slow_worker)
    pdf = b"%PDF-1.7\n%%EOF\n"
    threads = [threading.Thread(target=parse, args=(pdf, "application/pdf")) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak == parsers.PDF_CONCURRENCY == 2
