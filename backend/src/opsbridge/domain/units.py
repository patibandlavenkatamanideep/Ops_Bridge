"""Unit-of-measure canonicalization and explicitly configured conversions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction


@dataclass(frozen=True)
class UnitConversion:
    from_unit: str
    to_unit: str
    factor: Decimal  # quantity_in_to_unit = quantity_in_from_unit * factor


def canonical_unit(raw: str | None, aliases: dict[str, list[str]]) -> str | None:
    """Map a source unit label to a canonical unit, or None when unknown.

    Matching is case-insensitive and whitespace-trimmed; there is no fuzzy matching.
    """
    if raw is None:
        return None
    key = raw.strip().lower()
    if not key:
        return None
    for canonical, labels in aliases.items():
        if key == canonical.lower() or key in (label.lower() for label in labels):
            return canonical
    return None


def conversion_factor(
    from_unit: str, to_unit: str, conversions: list[UnitConversion]
) -> Decimal | None:
    """Return the configured factor, or None when no conversion is defined.

    Only direct conversions declared in policy are used. An inverse is derived only when it
    has a finite decimal expansion (1/8 = 0.125); otherwise (1/6) there is no exact factor and
    None is returned, so the condition is escalated rather than rounded.
    """
    if from_unit == to_unit:
        return Decimal(1)
    for conv in conversions:
        if conv.from_unit == from_unit and conv.to_unit == to_unit:
            return conv.factor
    for conv in conversions:
        if conv.from_unit == to_unit and conv.to_unit == from_unit:
            return exact_inverse(conv.factor)
    return None


def exact_inverse(factor: Decimal) -> Decimal | None:
    """1/factor as an exact Decimal, or None when it does not terminate (or factor <= 0).

    Computed with rational arithmetic: Decimal division rounds, and 6 x 0.1666...667 rounds
    back to 1, which once let a rounded reciprocal pass as exact.
    """
    if not factor.is_finite() or factor <= 0:
        return None
    inverse = 1 / Fraction(factor)
    rest, places = inverse.denominator, 0
    for prime in (2, 5):
        while rest % prime == 0:
            rest //= prime
    if rest != 1:
        return None  # a prime other than 2 or 5 divides the denominator: no finite decimal
    while 10**places % inverse.denominator:
        places += 1
    return Decimal(inverse.numerator * (10**places // inverse.denominator)).scaleb(-places)
