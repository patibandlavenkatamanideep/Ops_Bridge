"""Bounded parsing of untrusted documents (OB-04, adversarial inputs)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

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
