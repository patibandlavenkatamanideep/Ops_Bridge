"""Spend settings must be amounts that can be compared (review finding F06)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from opsbridge.config import Settings


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("live_spend_ceiling_usd", "-1"),
        ("live_spend_ceiling_usd", "NaN"),
        ("live_spend_ceiling_usd", "ten dollars"),
        ("anthropic_price_input_per_mtok_usd", "-3"),
        ("anthropic_price_output_per_mtok_usd", "Infinity"),
    ],
)
def test_ceilings_and_prices_must_be_finite_and_non_negative(name: str, value: str) -> None:
    with pytest.raises(ValidationError, match=name):
        Settings(**{name: value})


def test_ordinary_amounts_are_accepted() -> None:
    s = Settings(
        live_spend_ceiling_usd="5.00",
        anthropic_price_input_per_mtok_usd="3",
        anthropic_price_output_per_mtok_usd="15",
    )
    assert s.live_spend_ceiling_usd == "5.00"
