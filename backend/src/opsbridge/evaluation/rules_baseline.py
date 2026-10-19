"""Rules-only baseline: deterministic matching plus templated next action.

Runs each scenario through the same code path the product uses for source data —
native-format bytes → bounded parser → pack mapping → matcher → router — without a
database, so it is fast and fully offline. The stack-level path (simulator HTTP,
ingestion, PostgreSQL, worker) is exercised by integration tests and `eval` runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from opsbridge.canonical import sha256_bytes
from opsbridge.domain.decision import recommend
from opsbridge.domain.matching import MatchResult, reconcile
from opsbridge.domain.policy import ReconciliationPolicy, policy_effective_on
from opsbridge.domain.records import MatchInput, Supplier
from opsbridge.evidence.parsers import parse
from opsbridge.mapping import DocRef, MappingResult, PackSpec, RecordType, map_document
from opsbridge.packs import PackRegistry, load_policy_file
from opsbridge.simulator.scenarios import Scenario, render

DISCREPANCY_TYPES = ("missing_receipt", "quantity_mismatch", "price_mismatch")


@dataclass
class Verdict:
    case_id: str
    family: str
    split: str
    pack: str
    outcome: str
    types: list[str]
    next_action: str
    expected_outcome: str
    expected_types: list[str]
    expected_next_action: str
    problems: list[str] = field(default_factory=list)
    finding_codes: list[str] = field(default_factory=list)

    @property
    def outcome_correct(self) -> bool:
        return self.outcome == self.expected_outcome

    @property
    def types_correct(self) -> bool:
        return sorted(self.types) == sorted(self.expected_types)

    @property
    def action_correct(self) -> bool:
        return self.next_action == self.expected_next_action

    @property
    def case_correct(self) -> bool:
        return self.outcome_correct and self.types_correct and self.action_correct

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "family": self.family,
            "split": self.split,
            "pack": self.pack,
            "outcome": self.outcome,
            "types": self.types,
            "next_action": self.next_action,
            "expected": {
                "outcome": self.expected_outcome,
                "types": self.expected_types,
                "next_action": self.expected_next_action,
            },
            "correct": {
                "outcome": self.outcome_correct,
                "types": self.types_correct,
                "action": self.action_correct,
                "case": self.case_correct,
            },
            "finding_codes": self.finding_codes,
            "problems": self.problems,
        }


class PolicyBook:
    def __init__(self, packs_dir: Path) -> None:
        self.packs = PackRegistry(packs_dir)
        self.policies: list[ReconciliationPolicy] = [
            load_policy_file(p).policy for p in sorted(packs_dir.glob("*/policies/*.yaml"))
        ]

    def effective(self, policy_id: str, on: date) -> ReconciliationPolicy | None:
        return policy_effective_on(self.policies, policy_id, on)


def _record_type(pack: PackSpec, collection: str) -> RecordType | None:
    for rtype, spec in pack.sources.items():
        if spec.collection == collection:
            return rtype
    return None


def build_input(s: Scenario, pack: PackSpec) -> tuple[MatchInput | None, list[str], MappingResult]:
    combined = MappingResult()
    problems: list[str] = []
    for collection, key, body, ctype, _po in render(s):
        rtype = _record_type(pack, collection)
        if rtype is None:
            continue
        parsed = parse(body, ctype)
        sha = sha256_bytes(body)
        doc = DocRef(
            document_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{collection}/{key}")),
            version_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{collection}/{key}/{sha}")),
            sha256=sha,
        )
        if parsed.status != "parsed":
            problems.append(f"{collection}/{key}: {parsed.status} ({parsed.detail})")
            continue
        m = map_document(pack, rtype, parsed, doc)
        problems += [f"{collection}/{key}: {p}" for p in m.problems]
        combined.purchase_orders += m.purchase_orders
        combined.invoices += m.invoices
        combined.receipts += m.receipts
        combined.allocations += m.allocations
        combined.assertions += m.assertions
    invoices = [i for i in combined.invoices if i.invoice_number == s.invoice.number.upper()]
    if not invoices:
        return None, problems, combined
    inv = invoices[0]
    suppliers = tuple(
        {
            po.supplier_id: Supplier(po.supplier_id, po.supplier_name)
            for po in combined.purchase_orders
        }.values()
    )
    return (
        MatchInput(
            invoice=inv,
            purchase_orders=tuple(combined.purchase_orders),
            receipts=tuple(combined.receipts),
            prior_allocations=tuple(combined.allocations),
            supplier_assertions=tuple(combined.assertions),
            suppliers=suppliers,
        ),
        problems,
        combined,
    )


def evaluate(s: Scenario, book: PolicyBook) -> Verdict:
    pack = book.packs.get(s.pack)
    match_input, problems, _ = build_input(s, pack)

    def verdict(outcome: str, types: list[str], action: str, codes: list[str]) -> Verdict:
        return Verdict(
            s.id,
            s.family,
            s.split,
            s.pack,
            outcome,
            types,
            action,
            s.expected.outcome,
            list(s.expected.types),
            s.expected.next_action,
            problems,
            codes,
        )

    policy = book.effective(pack.policy_id, s.invoice.date)
    if policy is None:
        return verdict("manual_review", [], "escalate:assign_case", ["policy_unavailable"])
    if match_input is None:
        # Mirrors the product: an unreadable invoice is manual review; an absent one needs information.
        if any("invoices/" in p for p in problems):
            return verdict(
                "manual_review",
                [],
                f"escalate:{policy.action_routes['manual_review'].action_type}",
                ["invoice_extraction_needs_review"],
            )
        return verdict(
            "needs_information",
            [],
            f"request_information:{policy.action_routes['missing_receipt'].action_type}",
            ["invoice_record_unavailable"],
        )
    result: MatchResult = reconcile(match_input, policy)
    rec = recommend(result, policy, s.id)
    types: list[str] = sorted(
        {str(f.finding_type) for f in result.findings if f.finding_type in DISCREPANCY_TYPES}
    )
    return verdict(result.outcome, types, rec.label, [f.reason_code for f in result.findings])
