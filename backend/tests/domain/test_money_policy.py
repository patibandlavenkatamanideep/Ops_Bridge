from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from opsbridge.domain.money import DecimalParseError, parse_decimal, quantize
from opsbridge.domain.policy import ReconciliationPolicy, policy_effective_on

from .builders import load_policy


@pytest.mark.parametrize(
    "bad", [0.1, float("nan"), "NaN", "Infinity", "1e5", "1.2.3", "", "12,34", True, None]
)
def test_parse_decimal_rejects_inexact_or_malformed(bad: object) -> None:
    with pytest.raises(DecimalParseError):
        parse_decimal(bad)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("185.00", "185.00"), ("1,234.50", "1234.50"), (" 7 ", "7"), (3, "3"), ("-0.01", "-0.01")],
)
def test_parse_decimal_accepts_plain_decimals(raw: object, expected: str) -> None:
    assert parse_decimal(raw) == Decimal(expected)


def test_quantize_half_even_vs_half_up() -> None:
    assert quantize(Decimal("2.345"), 2, "ROUND_HALF_EVEN") == Decimal("2.34")
    assert quantize(Decimal("2.345"), 2, "ROUND_HALF_UP") == Decimal("2.35")


def test_policy_effective_version_is_selected_by_decision_date() -> None:
    v1 = load_policy("furniture/policies/furniture-ap-v1.yaml")
    v2 = load_policy("furniture/policies/furniture-ap-v2.yaml")
    assert policy_effective_on([v1, v2], "furniture-ap", date(2025, 6, 1)) is v1
    assert policy_effective_on([v1, v2], "furniture-ap", date(2026, 1, 1)) is v2
    assert policy_effective_on([v1, v2], "furniture-ap", date(2024, 12, 31)) is None


def test_policy_rejects_route_to_disallowed_action() -> None:
    raw = load_policy("furniture/policies/furniture-ap-v2.yaml").model_dump(mode="json")
    raw["allowed_actions"] = ["record_review_note"]
    with pytest.raises(ValidationError, match="not allowed"):
        ReconciliationPolicy.model_validate(raw)


def test_policy_rejects_unknown_currency_and_empty_tolerance() -> None:
    raw = load_policy("furniture/policies/furniture-ap-v2.yaml").model_dump(mode="json")
    with pytest.raises(ValidationError):
        ReconciliationPolicy.model_validate({**raw, "supported_currencies": ["XXX"]})
    with pytest.raises(ValidationError):
        ReconciliationPolicy.model_validate({**raw, "price_tolerance": {"mode": "either"}})
    with pytest.raises(ValidationError):
        ReconciliationPolicy.model_validate({**raw, "unexpected_field": 1})
