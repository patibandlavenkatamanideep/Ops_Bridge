"""The four pilot measures (protocol opsbridge-pilot/1, defined in docs/pilot-plan.md before
any pilot data exists).

The unit is the case: one blocked invoice. A case the reviewer excluded is in no measure; a
case in scope without a reference row is in every measure except accuracy. Failures stay in
their denominators: a referenced case OpsBridge never investigated counts as wrong, and a
case not decided or resolved by the cut-off is reported as open, never dropped from the
times. A reviewer decision is blind only when it was recorded before the case reached
OpsBridge; later ones are reported apart, since the reviewer may have seen its output.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any

from evalseal import wilson_ci

from opsbridge.evaluation.verdict import Verdict
from opsbridge.pilot.reference import ReferenceRow

PROTOCOL = "opsbridge-pilot/1"
AXES = ("next_action", "outcome", "types", "case")


@dataclass(frozen=True)
class ProposalChain:
    """One proposal and its edited versions."""

    drafted: dict[str, Any]  # the parameters the investigation's draft gave
    status: str  # of the last version: approved | rejected | expired | superseded | ...
    final: dict[str, Any]  # the parameters of the last version
    versions: int


@dataclass(frozen=True)
class CaseFacts:
    """What OpsBridge recorded about one pilot case."""

    case_number: str
    created_at: datetime
    first_investigated_at: datetime | None
    verdict: Verdict | None  # of the first investigation: what the investigator saw first
    evidence_readiness: str  # of the first investigation, or the case's when there is none
    documents: Counter[str]  # parse status of each document version read
    fields: Counter[str]  # status of each extracted field
    field_corrections: int
    chains: list[ProposalChain]
    first_approved_at: datetime | None
    resolved_at: datetime | None


def _rate(k: int, n: int) -> dict[str, Any]:
    return {
        "correct": k,
        "n": n,
        "rate": round(k / n, 4) if n else None,
        "wilson95": [round(x, 4) for x in wilson_ci(k, n)] if n else None,
    }


def _durations(spans: list[tuple[datetime, datetime | None]], as_of: datetime) -> dict[str, Any]:
    """Hours from start to end for the spans that ended by `as_of`; the rest are open."""
    hours = sorted(
        (end - start).total_seconds() / 3600 for start, end in spans if end and end <= as_of
    )
    return {
        "n": len(spans),
        "reached": len(hours),
        "open": len(spans) - len(hours),
        "median": round(median(hours), 2) if hours else None,
        "p90": round(hours[math.ceil(0.9 * len(hours)) - 1], 2) if hours else None,
    }


def _accuracy(pairs: list[tuple[Verdict | None, Verdict]]) -> dict[str, Any]:
    scored = [
        got.correct(expected) if got else dict.fromkeys(AXES, False) for got, expected in pairs
    ]
    return {axis: _rate(sum(s[axis] for s in scored), len(scored)) for axis in AXES}


def _decided(c: CaseFacts) -> datetime | None:
    return min((t for t in (c.first_approved_at, c.resolved_at) if t is not None), default=None)


def report(
    cases: list[CaseFacts],
    reference: list[ReferenceRow],
    *,
    since: datetime,
    until: datetime,
    as_of: datetime,
) -> dict[str, Any]:
    """The measures over the cases OpsBridge recorded in the window and the reviewer's rows."""
    by_case = {c.case_number: c for c in cases}
    ops_rows = {r.case: r for r in reference if r.arm == "opsbridge"}
    excluded = [r for r in reference if r.excluded]
    left_out = {r.case for r in excluded if r.arm == "opsbridge"}
    scope = sorted((c for c in cases if c.case_number not in left_out), key=lambda c: c.case_number)
    manual = [r for r in reference if r.arm == "manual" and not r.excluded]

    # (case, the reviewer's verdict, whether it was recorded before the case reached OpsBridge)
    referenced = [
        (c, row.reference, row.reviewed_at is not None and row.reviewed_at < c.created_at)
        for c in scope
        if (row := ops_rows.get(c.case_number)) is not None and row.reference is not None
    ]
    disagreements = [
        {
            "case": c.case_number,
            "blind": blind,
            "expected": expected.as_dict(),
            "got": c.verdict.as_dict() if c.verdict else None,
        }
        for c, expected, blind in referenced
        if c.verdict is None or not c.verdict.correct(expected)["case"]
    ]
    with_process = [
        (r.process, r.reference)
        for r in manual
        if r.process is not None and r.reference is not None
    ]

    chains = [ch for c in scope for ch in c.chains]
    approved = [ch for ch in chains if ch.status == "approved"]
    changed = [ch for ch in approved if ch.final != ch.drafted]
    rejected = sum(ch.status == "rejected" for ch in chains)
    changed_fields = Counter(
        k for ch in changed for k in sorted(set(ch.final) | set(ch.drafted))
        if ch.final.get(k) != ch.drafted.get(k)
    )  # fmt: skip

    readiness = Counter(c.evidence_readiness for c in scope)
    return {
        "protocol": PROTOCOL,
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "as_of": as_of.isoformat(),
        },
        "scope": {
            "opsbridge_cases": len(scope),
            "manual_cases": len(manual),
            "excluded": [{"arm": r.arm, "case": r.case, "reason": r.excluded} for r in excluded],
            "unreferenced": [c.case_number for c in scope if c.case_number not in ops_rows],
            "unknown_references": [
                {"line": r.line, "case": r.case}
                for r in ops_rows.values()
                if r.case not in by_case and not r.excluded
            ],
        },
        "extraction": {
            "evidence_readiness": dict(readiness),
            "complete": _rate(readiness["complete"], len(scope)),
            "documents": dict(sum((c.documents for c in scope), Counter())),
            "fields": dict(sum((c.fields for c in scope), Counter())),
            "field_corrections": sum(c.field_corrections for c in scope),
            "cases_with_corrections": sum(c.field_corrections > 0 for c in scope),
        },
        "accuracy": {
            "opsbridge": {
                "blind": _accuracy([(c.verdict, e) for c, e, blind in referenced if blind]),
                "not_blind": _accuracy([(c.verdict, e) for c, e, blind in referenced if not blind]),
                "disagreements": disagreements,
            },
            "manual": {
                **_accuracy(list(with_process)),
                "without_process_decision": len(manual) - len(with_process),
            },
        },
        "approval": {
            "chains": len(chains),
            "approved_as_drafted": len(approved) - len(changed),
            "approved_changed": len(changed),
            "rejected": rejected,
            "undecided": len(chains) - len(approved) - rejected,
            "changed_fields": dict(changed_fields),
            "corrected": _rate(len(changed) + rejected, len(approved) + rejected),
        },
        "time_hours": {
            "opsbridge": {
                "to_decision": _durations([(c.created_at, _decided(c)) for c in scope], as_of),
                "to_resolution": _durations([(c.created_at, c.resolved_at) for c in scope], as_of),
            },
            "manual": {
                "to_decision": _durations(
                    [(r.started_at, r.decided_at) for r in manual if r.started_at], as_of
                ),
                "to_resolution": _durations(
                    [(r.started_at, r.resolved_at) for r in manual if r.started_at], as_of
                ),
            },
        },
    }
