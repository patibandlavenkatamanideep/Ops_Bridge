"""Reconciliation policy versions.

Policies are data, loaded from validated files in an organization pack. Changing a
tolerance or precedence rule means publishing a new policy version; findings retain
the version they were evaluated under.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .money import CURRENCY_MINOR_UNITS, ROUNDING_MODES
from .units import UnitConversion

SourceKind = Literal["goods_receipt", "supplier_message", "invoice", "purchase_order"]
ActionType = Literal[
    "create_follow_up_task",
    "assign_case",
    "create_supplier_message_draft",
    "record_review_note",
]
DataRouting = Literal["hosted_permitted", "local_required", "human_only"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PriceTolerance(_Strict):
    """Tolerance for net unit price differences.

    `absolute` is in currency units per unit of measure; `percent` is a percentage of
    the PO net unit price. `mode` decides how configured rules combine:
    - "either": the difference passes if any configured rule passes;
    - "both": the difference passes only if every configured rule passes.
    A difference exactly equal to a limit passes (inclusive boundary).
    `direction` "over_only" ignores invoices priced below the PO; "both" checks
    either direction.
    """

    absolute: Decimal | None = None
    percent: Decimal | None = None
    mode: Literal["either", "both"] = "either"
    direction: Literal["over_only", "both"] = "both"
    basis: Literal["net_unit_price"] = "net_unit_price"

    @model_validator(mode="after")
    def _at_least_one(self) -> PriceTolerance:
        if self.absolute is None and self.percent is None:
            raise ValueError("price tolerance needs an absolute or percent rule")
        for v in (self.absolute, self.percent):
            if v is not None and v < 0:
                raise ValueError("tolerances cannot be negative")
        return self


class QuantityTolerance(_Strict):
    """Allowed over-invoicing relative to allocatable received quantity (inclusive)."""

    absolute: Decimal = Decimal(0)
    percent: Decimal = Decimal(0)

    @model_validator(mode="after")
    def _non_negative(self) -> QuantityTolerance:
        if self.absolute < 0 or self.percent < 0:
            raise ValueError("tolerances cannot be negative")
        return self


class PriceRounding(_Strict):
    places: int = Field(default=4, ge=0, le=8)
    mode: str = "ROUND_HALF_EVEN"

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        if v not in ROUNDING_MODES:
            raise ValueError(f"rounding mode must be one of {sorted(ROUNDING_MODES)}")
        return v


class ConversionSpec(_Strict):
    from_unit: str
    to_unit: str
    factor: Decimal

    @field_validator("factor")
    @classmethod
    def _positive_finite(cls, v: Decimal) -> Decimal:
        if not v.is_finite() or v <= 0:
            raise ValueError(f"conversion factor must be finite and positive, got {v}")
        return v

    def to_domain(self) -> UnitConversion:
        return UnitConversion(self.from_unit, self.to_unit, self.factor)


class ActionRoute(_Strict):
    """Templated next action for a finding type (rules-only baseline and validation)."""

    action_type: ActionType
    assignee_group: str | None = None
    title_template: str


class ApprovalPolicy(_Strict):
    lifetime_minutes: int = Field(default=30, ge=1, le=24 * 60)
    separation_of_duties: bool = True


class ReconciliationPolicy(_Strict):
    schema_version: Literal[1] = 1
    policy_id: str
    version: int = Field(ge=1)
    effective_from: date
    description: str = ""
    supported_currencies: list[str]
    price_tolerance: PriceTolerance
    quantity_tolerance: QuantityTolerance = QuantityTolerance()
    price_rounding: PriceRounding = PriceRounding()
    unit_aliases: dict[str, list[str]]
    unit_conversions: list[ConversionSpec] = []
    # Ordered from most to least authoritative. Empty means precedence is undefined and
    # conflicts between sources must be escalated.
    source_precedence: list[SourceKind] = []
    action_routes: dict[str, ActionRoute]
    allowed_actions: list[ActionType]
    approval: ApprovalPolicy = ApprovalPolicy()
    data_routing: DataRouting = "hosted_permitted"

    @field_validator("supported_currencies")
    @classmethod
    def _known_currencies(cls, v: list[str]) -> list[str]:
        unknown = [c for c in v if c not in CURRENCY_MINOR_UNITS]
        if unknown:
            raise ValueError(f"currencies without minor-unit definitions: {unknown}")
        return v

    @model_validator(mode="after")
    def _units_consistent(self) -> ReconciliationPolicy:
        owner: dict[str, str] = {}
        for canonical, labels in self.unit_aliases.items():
            for label in {canonical.lower(), *(x.strip().lower() for x in labels)}:
                if owner.setdefault(label, canonical) != canonical:
                    raise ValueError(
                        f"unit alias {label!r} names both {owner[label]} and {canonical}"
                    )
        factors: dict[tuple[str, str], Decimal] = {}
        for c in self.unit_conversions:
            pair = (c.from_unit, c.to_unit)
            if pair in factors:
                raise ValueError(f"conversion {pair[0]}->{pair[1]} is declared more than once")
            factors[pair] = c.factor
            back = factors.get((c.to_unit, c.from_unit))
            if back is not None and Fraction(back) * Fraction(c.factor) != 1:
                raise ValueError(
                    f"conversions {c.from_unit}->{c.to_unit} ({c.factor}) and back ({back}) "
                    "contradict each other"
                )
        return self

    @model_validator(mode="after")
    def _routes_allowed(self) -> ReconciliationPolicy:
        for name, route in self.action_routes.items():
            if route.action_type not in self.allowed_actions:
                raise ValueError(f"route {name} uses action {route.action_type} not allowed")
        return self

    @property
    def ref(self) -> str:
        return f"{self.policy_id}@v{self.version}"

    def conversions(self) -> list[UnitConversion]:
        return [c.to_domain() for c in self.unit_conversions]


def policy_effective_on(
    policies: list[ReconciliationPolicy], policy_id: str, on: date
) -> ReconciliationPolicy | None:
    """Pick the latest version of `policy_id` whose effective date is on or before `on`."""
    candidates = [p for p in policies if p.policy_id == policy_id and p.effective_from <= on]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.effective_from, p.version))
