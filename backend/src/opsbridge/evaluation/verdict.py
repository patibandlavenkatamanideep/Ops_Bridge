"""An investigation read as a verdict: its outcome, discrepancy types and next action.

The benchmark scores its trials with it and the pilot report compares investigations with a
reviewer's decisions, so both read an investigation in exactly the same way. The next action
is `decision:action_type`, the label a case is scored on (domain/decision.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any, get_args

from opsbridge.domain.decision import Decision
from opsbridge.domain.policy import ActionType

OUTCOMES = ("matched", "discrepancies_found", "needs_information", "manual_review")
DISCREPANCY_TYPES = ("missing_receipt", "quantity_mismatch", "price_mismatch")
NEXT_ACTIONS = tuple(f"{d}:{a}" for d, a in product(get_args(Decision), get_args(ActionType)))
NO_ACTION = "none"


@dataclass(frozen=True)
class Verdict:
    outcome: str
    types: tuple[str, ...]  # sorted, without repeats
    next_action: str

    def correct(self, expected: Verdict) -> dict[str, bool]:
        action = self.next_action == expected.next_action
        outcome = self.outcome == expected.outcome
        types = self.types == expected.types
        return {
            "next_action": action,
            "outcome": outcome,
            "types": types,
            "case": action and outcome and types,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "types": list(self.types), "next_action": self.next_action}


def verdict_of(
    outcome: str, findings: list[dict[str, Any]], recommendation: dict[str, Any] | None
) -> Verdict:
    """The verdict of an investigation version, from its stored outcome, findings and
    recommendation."""
    rec = recommendation or {}
    return Verdict(
        outcome=outcome,
        types=tuple(
            sorted({f["finding_type"] for f in findings if f["finding_type"] in DISCREPANCY_TYPES})
        ),
        next_action=f"{rec['decision']}:{rec['action_type']}"
        if rec.get("action_type")
        else NO_ACTION,
    )
