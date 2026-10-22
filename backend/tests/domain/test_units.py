"""Unit conversions are exact or refused (review finding F10).

An inverse conversion used to be accepted when `inverse * factor == 1` under Decimal
rounding, so the inverse of 6 (0.1666…667) passed as exact. Policies also accepted zero,
negative and non-finite factors, contradictory pairs and aliases naming two units.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from opsbridge.domain.policy import ReconciliationPolicy
from opsbridge.domain.units import UnitConversion, conversion_factor, exact_inverse

from .builders import PACKS


@pytest.mark.parametrize(
    ("factor", "inverse"),
    [("2", "0.5"), ("4", "0.25"), ("5", "0.2"), ("8", "0.125"), ("10", "0.1"), ("2.5", "0.4")],
)
def test_a_terminating_inverse_is_derived_exactly(factor: str, inverse: str) -> None:
    conv = [UnitConversion("BOX", "EA", Decimal(factor))]
    got = conversion_factor("EA", "BOX", conv)
    assert got == Decimal(inverse) and got * Decimal(factor) == 1
    assert conversion_factor("BOX", "EA", conv) == Decimal(factor)


@pytest.mark.parametrize("factor", ["3", "6", "7", "12", "0.3"])
def test_a_nonterminating_inverse_is_refused(factor: str) -> None:
    assert conversion_factor("EA", "BOX", [UnitConversion("BOX", "EA", Decimal(factor))]) is None


def _policy(**changes: Any) -> ReconciliationPolicy:
    base = yaml.safe_load((PACKS / "furniture/policies/furniture-ap-v2.yaml").read_text())
    return ReconciliationPolicy.model_validate({**base, **changes})


@pytest.mark.parametrize("factor", ["0", "-10", "NaN", "Infinity", "-Infinity"])
def test_factors_must_be_finite_and_positive(factor: str) -> None:
    with pytest.raises(ValidationError, match="factor"):
        _policy(unit_conversions=[{"from_unit": "BX10", "to_unit": "EA", "factor": factor}])


def test_both_directions_must_agree_and_pairs_are_declared_once() -> None:
    both = [
        {"from_unit": "BX10", "to_unit": "EA", "factor": "10"},
        {"from_unit": "EA", "to_unit": "BX10", "factor": "0.1"},
    ]
    assert len(_policy(unit_conversions=both).conversions()) == 2
    with pytest.raises(ValidationError, match="contradict"):
        _policy(unit_conversions=[both[0], {**both[1], "factor": "0.2"}])
    with pytest.raises(ValidationError, match="more than once"):
        _policy(unit_conversions=[both[0], {**both[0], "factor": "12"}])


def test_an_alias_names_one_unit() -> None:
    with pytest.raises(ValidationError, match="'box'"):
        _policy(unit_aliases={"EA": ["each", "box"], "BX10": ["Box", "bx10"]})
    with pytest.raises(ValidationError, match="'bx10'"):
        _policy(unit_aliases={"EA": ["each", "BX10"], "BX10": ["box of 10"]})


@given(
    twos=st.integers(min_value=0, max_value=12),
    fives=st.integers(min_value=0, max_value=12),
    other=st.sampled_from([1, 3, 7, 11]),
    shift=st.integers(min_value=-4, max_value=4),
)
def test_an_inverse_exists_exactly_when_it_terminates_and_is_exact(
    twos: int, fives: int, other: int, shift: int
) -> None:
    factor = Decimal(2**twos * 5**fives * other).scaleb(shift)
    inverse = exact_inverse(factor)
    if other == 1:
        assert inverse is not None and inverse * factor == 1  # exact, no rounding involved
        assert Fraction(inverse) == 1 / Fraction(factor)
    else:
        assert inverse is None
