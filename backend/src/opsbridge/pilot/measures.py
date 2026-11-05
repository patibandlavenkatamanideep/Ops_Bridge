"""The four pilot measures (protocol opsbridge-pilot/2, defined in docs/pilot-plan.md before
any pilot data exists).

The unit is the enrolled invoice: a member of the cohort, frozen before any case was opened
(pilot/cohort.py). Every member is accounted for at the cut-off, in its arm: excluded by the
reviewer, never opened as a case in the window, opened but not investigated, or investigated
(OpsBridge); outside the window, not started, or started (manual). An excluded member is in no
measure. Failures stay in their denominators: a member never opened or never investigated
counts as wrong, and a case not decided or resolved by the cut-off is reported as open, never
dropped from the times.

A reviewer decision is blind only when it was recorded before the case reached OpsBridge;
later ones are reported apart, since the reviewer may have seen its output. A decision recorded
after the cut-off, like a missing one, did not exist at the cut-off: the member has no
reference, and the report says it is incomplete rather than leave the member out of a
denominator (review N05). Operational coverage is reported apart from accuracy, and accuracy
both over every enrolled member and over the ones investigated, which is a selected subset and
is labelled so.

A reference row finds its member by the invoice, as intake identified it (number and
supplier), because a blind decision is recorded before the case and its number exist. The
report names cases by number and rows by line, never by invoice.

Opsbridge-pilot/2 replaces /1 before any pilot data existed (docs/pilot-plan.md): /1 took its
denominators from the cases opened and the rows written, so an enrolled invoice never opened,
or a decision never recorded, was in none of them.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Any

from evalseal import wilson_ci

from opsbridge.evaluation.verdict import Verdict
from opsbridge.pilot.cohort import Cohort, Member, key_of
from opsbridge.pilot.reference import ReferenceRow

PROTOCOL = "opsbridge-pilot/2"
REPLACES = "opsbridge-pilot/1"
AXES = ("next_action", "outcome", "types", "case")


@dataclass(frozen=True)
class ProposalChain:
    """One proposal and its edited versions, as they stood at the cut-off."""

    drafted: dict[str, Any]  # the parameters the investigation's draft gave
    status: str  # of the last version then: approved | rejected | expired | superseded | ...
    final: dict[str, Any]  # the parameters of the last version
    versions: int


@dataclass(frozen=True)
class CaseFacts:
    """What OpsBridge had recorded about one pilot case by the cut-off."""

    case_number: str
    invoice: tuple[str, str | None] | None  # (number, supplier) as intake identified it
    created_at: datetime
    first_investigated_at: datetime | None
    verdict: Verdict | None  # of the first investigation: what the investigator saw first
    evidence_readiness: str  # of the first investigation, or not_investigated
    documents: Counter[str]  # parse status of each document version read
    fields: Counter[str]  # status of each extracted field
    field_corrections: int
    chains: list[ProposalChain]
    first_approved_at: datetime | None
    resolved_at: datetime | None
    explanation_origin: str | None = field(default=None)  # of the first investigation


class CohortMismatch(ValueError):
    """Reference rows that name no enrolled invoice: the sample was not the one frozen."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{len(problems)} reference row(s) outside the cohort")
        self.problems = problems


def check_window(since: datetime, until: datetime, as_of: datetime) -> None:
    for name, t in (("since", since), ("until", until), ("as_of", as_of)):
        if t.tzinfo is None or t.utcoffset() is None:
            raise ValueError(f"{name} has no time zone")
    if not since < until <= as_of:
        raise ValueError("the window must satisfy since < until <= as_of")


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


@dataclass(frozen=True)
class _Enrolled:
    member: Member
    row: ReferenceRow | None
    reference: str  # excluded | missing | after_cut_off | recorded (manual) | blind | not_blind
    case: CaseFacts | None = None  # opsbridge

    @property
    def verdict(self) -> Verdict | None:
        return self.case.verdict if self.case else None


def _reference_state(row: ReferenceRow | None, as_of: datetime) -> str:
    if row is None:
        return "missing"
    if row.excluded:
        return "excluded"
    if row.reviewed_at is None or row.reviewed_at > as_of:
        return "after_cut_off"
    return "recorded"


def report(
    cases: list[CaseFacts],
    reference: list[ReferenceRow],
    cohort: Cohort,
    *,
    since: datetime,
    until: datetime,
    as_of: datetime,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The measures over the cohort at the cut-off, from OpsBridge's facts (read as they stood
    then, pilot/collect.py) and the reviewer's rows."""
    check_window(since, until, as_of)
    members = {m.key: m for m in cohort.members}
    rows: dict[tuple[str, ...], ReferenceRow] = {}
    strays = []
    for r in reference:
        k = key_of(r.arm, r.case, r.invoice)
        if k in members:
            rows[k] = r
        else:
            strays.append(f"reference line {r.line}: names no invoice in the cohort")
    if strays:
        raise CohortMismatch(strays)
    by_invoice = {c.invoice: c for c in cases if c.invoice is not None}
    enrolled_invoices = {m.invoice for m in cohort.members if m.arm == "opsbridge"}

    ops: list[_Enrolled] = []
    manual: list[_Enrolled] = []
    for m in cohort.members:
        row = rows.get(m.key)
        state = _reference_state(row, as_of)
        if m.arm == "opsbridge":
            case = by_invoice.get(m.invoice) if m.invoice is not None else None
            if state == "recorded":
                assert row is not None and row.reviewed_at is not None
                state = (
                    "blind" if case is None or row.reviewed_at < case.created_at else "not_blind"
                )
            ops.append(_Enrolled(m, row, state, case))
        else:
            manual.append(_Enrolled(m, row, state))

    # --- OpsBridge arm -------------------------------------------------------------------
    measured = [e for e in ops if e.reference != "excluded"]
    with_case = [e for e in measured if e.case is not None]
    scope = [e.case for e in with_case if e.case is not None]
    investigated = [e for e in with_case if e.verdict is not None]
    blind = [e for e in measured if e.reference == "blind"]
    not_blind = [e for e in measured if e.reference == "not_blind"]

    def name(e: _Enrolled) -> dict[str, Any]:
        return {"case": e.case.case_number if e.case else None, "cohort_line": e.member.line}

    disagreements = [
        {
            **name(e),
            "blind": e.reference == "blind",
            "expected": e.row.reference.as_dict(),
            "got": e.verdict.as_dict() if e.verdict else None,
        }
        for e in blind + not_blind
        if e.row is not None
        and e.row.reference is not None
        and (e.verdict is None or not e.verdict.correct(e.row.reference)["case"])
    ]

    def pairs(group: list[_Enrolled]) -> list[tuple[Verdict | None, Verdict]]:
        return [(e.verdict, e.row.reference) for e in group if e.row and e.row.reference]

    # --- manual arm ----------------------------------------------------------------------
    manual_measured = [e for e in manual if e.reference != "excluded"]

    def manual_progress(e: _Enrolled) -> str:
        started = e.row.started_at if e.row else None
        if started is None or started > as_of:
            return "not_started"
        return "started" if since <= started < until else "outside_window"

    started = [e for e in manual_measured if manual_progress(e) == "started"]
    process: list[tuple[Verdict, Verdict]] = []
    for e in started:
        kept = e.row
        if (
            e.reference == "recorded"
            and kept is not None
            and kept.reference is not None
            and kept.process is not None
            and kept.decided_at is not None
            and kept.decided_at <= as_of
        ):
            process.append((kept.process, kept.reference))
    referenced_manual = [e for e in started if e.reference == "recorded"]

    # --- completeness ----------------------------------------------------------------------
    unreferenced = [
        e for e in measured + manual_measured if e.reference in ("missing", "after_cut_off")
    ]
    incomplete = (
        [
            f"{len(unreferenced)} enrolled invoice(s) have no reviewer decision recorded by the "
            f"cut-off (cohort lines {', '.join(str(e.member.line) for e in unreferenced)})"
        ]
        if unreferenced
        else []
    )

    chains = [ch for c in scope for ch in c.chains]
    approved = [ch for ch in chains if ch.status == "approved"]
    changed = [ch for ch in approved if ch.final != ch.drafted]
    rejected = sum(ch.status == "rejected" for ch in chains)
    changed_fields = Counter(
        k for ch in changed for k in sorted(set(ch.final) | set(ch.drafted))
        if ch.final.get(k) != ch.drafted.get(k)
    )  # fmt: skip
    readiness = Counter(e.case.evidence_readiness if e.case else "no_case" for e in measured)
    enrolled_blind = _accuracy(pairs(blind))

    def count(group: list[_Enrolled], attribute: str) -> dict[str, int]:
        return dict(Counter(getattr(e, attribute) for e in group))

    return {
        "protocol": PROTOCOL,
        "replaces": REPLACES,
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "as_of": as_of.isoformat(),
        },
        "cohort": {"sha256": cohort.sha256, "members": len(cohort.members)},
        "provenance": provenance or {},
        "complete": not incomplete,
        "incomplete_because": incomplete,
        "headline": {
            "measure": (
                "blind next-action agreement over every enrolled OpsBridge invoice with a blind "
                "decision; one never opened or never investigated counts as wrong"
            ),
            **enrolled_blind["next_action"],
            "complete": not incomplete,
        },
        "coverage": {
            "opsbridge": {
                "enrolled": len(ops),
                "excluded": [
                    {**name(e), "reason": e.row.excluded}
                    for e in ops
                    if e.reference == "excluded" and e.row is not None
                ],
                "no_case": [e.member.line for e in measured if e.case is None],
                "not_investigated": [
                    e.case.case_number for e in with_case if e.case and e.verdict is None
                ],
                "investigated": len(investigated),
                "references": count(measured, "reference"),
                "decided_by_cut_off": sum(_decided(c) is not None for c in scope),
                "resolved_by_cut_off": sum(c.resolved_at is not None for c in scope),
                "explanations": dict(
                    Counter(c.explanation_origin for c in scope if c.explanation_origin)
                ),
                "cases_outside_cohort": sorted(
                    c.case_number for c in cases if c.invoice not in enrolled_invoices
                ),
            },
            "manual": {
                "enrolled": len(manual),
                "excluded": [
                    {"cohort_line": e.member.line, "reason": e.row.excluded}
                    for e in manual
                    if e.reference == "excluded" and e.row is not None
                ],
                "progress": dict(Counter(manual_progress(e) for e in manual_measured)),
                "references": count(manual_measured, "reference"),
            },
        },
        "extraction": {
            "evidence_readiness": dict(readiness),
            "complete": _rate(readiness["complete"], len(measured)),
            "documents": dict(sum((c.documents for c in scope), Counter())),
            "fields": dict(sum((c.fields for c in scope), Counter())),
            "field_corrections": sum(c.field_corrections for c in scope),
            "cases_with_corrections": sum(c.field_corrections > 0 for c in scope),
        },
        "accuracy": {
            "opsbridge": {
                "blind": enrolled_blind,
                "blind_investigated_only": {
                    "note": "a selected subset: enrolled invoices investigated by the cut-off",
                    **_accuracy(pairs([e for e in blind if e.verdict is not None])),
                },
                "not_blind": _accuracy(pairs(not_blind)),
                "disagreements": disagreements,
            },
            "manual": {
                **_accuracy(list(process)),
                "without_process_decision": len(referenced_manual) - len(process),
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
                "not_opened": len(measured) - len(with_case),
                "to_decision": _durations([(c.created_at, _decided(c)) for c in scope], as_of),
                "to_resolution": _durations([(c.created_at, c.resolved_at) for c in scope], as_of),
            },
            "manual": {
                "not_started": len(manual_measured) - len(started),
                "to_decision": _durations(
                    [
                        (e.row.started_at, e.row.decided_at)
                        for e in started
                        if e.row and e.row.started_at
                    ],
                    as_of,
                ),
                "to_resolution": _durations(
                    [
                        (e.row.started_at, e.row.resolved_at)
                        for e in started
                        if e.row and e.row.started_at
                    ],
                    as_of,
                ),
            },
        },
    }
