"""Rules-only next-action selection from deterministic findings.

This is the "deterministic matching plus templated guidance" baseline. The
agent-assisted path may phrase explanations and proposals differently, but its
proposals are validated against the same findings and allowed-action policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .matching import Finding, MatchResult
from .policy import ActionType, ReconciliationPolicy

Decision = Literal["propose_action", "request_information", "escalate", "no_discrepancy"]

# Route keys in priority order: the first route whose condition holds is used.
ROUTE_PRIORITY: tuple[str, ...] = (
    "manual_review",
    "missing_receipt",
    "quantity_mismatch",
    "price_mismatch",
    "matched",
)


@dataclass(frozen=True)
class Recommendation:
    decision: Decision
    route: str
    action_type: ActionType
    assignee_group: str | None
    title: str
    finding_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "route": self.route,
            "action_type": self.action_type,
            "assignee_group": self.assignee_group,
            "title": self.title,
            "finding_ids": list(self.finding_ids),
        }

    @property
    def label(self) -> str:
        """Compact label used for case-level scoring: decision + action type."""
        return f"{self.decision}:{self.action_type}"


def _route_findings(result: MatchResult, route: str) -> list[Finding]:
    if route == "manual_review":
        return [f for f in result.findings if f.status == "needs_review"]
    if route == "missing_receipt":
        return [f for f in result.findings if f.status == "needs_information"]
    if route in ("quantity_mismatch", "price_mismatch"):
        return [f for f in result.findings if f.finding_type == route and f.status == "discrepancy"]
    return []


_DECISION_FOR_ROUTE: dict[str, Decision] = {
    "manual_review": "escalate",
    "missing_receipt": "request_information",
    "quantity_mismatch": "propose_action",
    "price_mismatch": "propose_action",
    "matched": "no_discrepancy",
}


def recommend(result: MatchResult, policy: ReconciliationPolicy, case_ref: str) -> Recommendation:
    for route in ROUTE_PRIORITY:
        findings = _route_findings(result, route)
        if route != "matched" and not findings:
            continue
        spec = policy.action_routes.get(route)
        if spec is None:
            raise KeyError(f"policy {policy.ref} has no action route for {route}")
        title = spec.title_template.format(
            case=case_ref,
            po=result.resolved_po_number or "unknown PO",
            count=len(findings),
        )
        return Recommendation(
            decision=_DECISION_FOR_ROUTE[route],
            route=route,
            action_type=spec.action_type,
            assignee_group=spec.assignee_group,
            title=title,
            finding_ids=tuple(f.finding_id for f in findings),
        )
    raise AssertionError("unreachable: 'matched' route always applies")  # pragma: no cover
