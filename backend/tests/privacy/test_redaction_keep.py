"""Outbound redaction keeps the case's own business identifiers (exact values only)."""

from __future__ import annotations

from opsbridge.privacy.redaction import redact

PO = "4500012345"  # a bare 10-digit purchase order number, as SAP-style systems issue them


def test_a_case_identifier_that_looks_like_a_phone_number_is_kept() -> None:
    title = f"Confirm delivered quantity for {PO} (CASE-000123)"
    assert redact(title).text == "Confirm delivered quantity for [REDACTED:phone] (CASE-000123)"
    kept = redact(title, keep=[PO, "CASE-000123"])
    assert kept.text == title and kept.counts == {}


def test_keeping_ids_does_not_keep_personal_data_next_to_them() -> None:
    r = redact(f"PO {PO}: call 4155550134 or ap@acme.example", keep=[PO])
    assert PO in r.text and "4155550134" not in r.text and "ap@acme.example" not in r.text
    assert r.counts == {"phone": 1, "email": 1}


def test_only_whole_exact_values_are_kept() -> None:
    longer = f"{PO}6789"  # contains the PO number but is a different, longer number
    assert longer not in redact(f"ref {longer}", keep=[PO]).text
    assert redact("call 4155550134", keep=["41"]).counts == {"phone": 1}  # too short to keep
    assert redact("id a+b.c 4155550134", keep=["a+b.c"]).text == "id a+b.c [REDACTED:phone]"
