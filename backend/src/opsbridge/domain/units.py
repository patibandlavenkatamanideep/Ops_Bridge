"""Unit-of-measure canonicalization and explicitly configured conversions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


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

    Only direct conversions declared in policy are used. Inverse conversions are
    derived only when the inverse is exact in Decimal (e.g. 1/10), otherwise None.
    """
    if from_unit == to_unit:
        return Decimal(1)
    for conv in conversions:
        if conv.from_unit == from_unit and conv.to_unit == to_unit:
            return conv.factor
    for conv in conversions:
        if conv.from_unit == to_unit and conv.to_unit == from_unit and conv.factor != 0:
            inverse = Decimal(1) / conv.factor
            if inverse * conv.factor == Decimal(1):
                return inverse
    return None
