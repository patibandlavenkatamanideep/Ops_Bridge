"""The four pilot measures (docs/pilot-plan.md, protocol opsbridge-pilot/1), on facts and
reference rows written out by hand so every count below can be checked by reading them.

Each measure keeps its failures in its denominator: a referenced case OpsBridge never
investigated counts as wrong, a case still open at the cut-off is reported as open (not
dropped from the times), and a reviewer decision recorded after the case reached OpsBridge is
reported apart from the blind ones, since the reviewer may have seen its output.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from opsbridge.evaluation.verdict import Verdict
from opsbridge.pilot.measures import CaseFacts, ProposalChain, report
from opsbridge.pilot.reference import ReferenceRow

T0 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
FOLLOW_UP = Verdict(
    "discrepancies_found", ("quantity_mismatch",), "propose_action:create_follow_up_task"
)
QUERY = Verdict(
    "discrepancies_found", ("price_mismatch",), "propose_action:create_supplier_message_draft"
)
MATCHED = Verdict("matched", (), "no_discrepancy:record_review_note")
DRAFT = {
    "title": "Receipt short by 2",
    "body": "Please post the receipt.",
    "assignee_group": "receiving",
}


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def facts(case: str, **changes: object) -> CaseFacts:
    base: dict[str, object] = {
        "case_number": case,
        "created_at": T0,
        "first_investigated_at": at(0.1),
        "verdict": FOLLOW_UP,
        "evidence_readiness": "complete",
        "documents": Counter({"parsed": 1}),
        "fields": Counter(),
        "field_corrections": 0,
        "chains": [],
        "first_approved_at": None,
        "resolved_at": None,
    }
    return CaseFacts(**(base | changes))  # type: ignore[arg-type]


def ref(arm: str, case: str, line: int, **changes: object) -> ReferenceRow:
    base: dict[str, object] = {
        "line": line,
        "arm": arm,
        "case": case,
        "reviewed_at": at(-1),
        "reference": FOLLOW_UP,
        "process": None,
        "started_at": None,
        "decided_at": None,
        "resolved_at": None,
        "excluded": "",
    }
    return ReferenceRow(**(base | changes))  # type: ignore[arg-type]


CASES = [
    # Reviewed before intake (blind); OpsBridge agrees; follow-up approved as drafted.
    facts(
        "CASE-A",
        chains=[ProposalChain(DRAFT, "approved", DRAFT, 1)],
        first_approved_at=at(1),
    ),
    # Reviewed after intake (not blind); OpsBridge proposed a follow-up, the reviewer a
    # supplier query; the approver changed the body; resolved later.
    facts(
        "CASE-B",
        evidence_readiness="needs_review",
        documents=Counter({"parsed": 1, "needs_review": 1}),
        fields=Counter({"authoritative": 3, "needs_review": 1, "corrected": 1}),
        field_corrections=1,
        chains=[ProposalChain(DRAFT, "approved", DRAFT | {"body": "Ask the supplier."}, 2)],
        first_approved_at=at(4),
        resolved_at=at(6),
    ),
    # No reference row; evidence incomplete; its proposal was rejected; still open.
    facts(
        "CASE-C",
        evidence_readiness="incomplete",
        chains=[ProposalChain(DRAFT, "rejected", DRAFT, 1)],
    ),
    # Excluded by the reviewer: in no measure at all.
    facts("CASE-D", evidence_readiness="incomplete", resolved_at=at(0.5)),
    # Referenced but never investigated: wrong on every axis, not left out.
    facts(
        "CASE-E",
        first_investigated_at=None,
        verdict=None,
        evidence_readiness="not_collected",
        documents=Counter(),
    ),
]
REFERENCE = [
    ref("opsbridge", "CASE-A", 2),
    ref("opsbridge", "CASE-B", 3, reviewed_at=at(2), reference=QUERY),
    ref("opsbridge", "CASE-D", 4, reference=None, reviewed_at=None, excluded="credit note"),
    ref("opsbridge", "CASE-E", 5),
    ref("opsbridge", "CASE-9999", 6),
    ref(
        "manual", "M-1", 7, reference=MATCHED, process=MATCHED,
        started_at=at(-1), decided_at=at(1), resolved_at=at(5),
    ),
    ref("manual", "M-2", 8, reference=QUERY, process=FOLLOW_UP, started_at=at(-1)),
    ref("manual", "M-3", 9, reference=QUERY, started_at=at(-1)),  # process not recorded
]  # fmt: skip


def run(as_of: datetime = at(24)) -> dict[str, Any]:
    return report(CASES, REFERENCE, since=at(-24), until=at(1), as_of=as_of)


def test_scope_names_what_was_left_out_and_why() -> None:
    scope = run()["scope"]
    assert scope["opsbridge_cases"] == 4 and scope["manual_cases"] == 3
    assert scope["excluded"] == [{"arm": "opsbridge", "case": "CASE-D", "reason": "credit note"}]
    assert scope["unreferenced"] == ["CASE-C"]  # measured, but not for accuracy
    assert scope["unknown_references"] == [{"line": 6, "case": "CASE-9999"}]


def test_extraction_completeness_counts_every_case_and_field() -> None:
    extraction = run()["extraction"]
    assert extraction["evidence_readiness"] == {
        "complete": 1, "needs_review": 1, "incomplete": 1, "not_collected": 1,
    }  # fmt: skip
    assert extraction["complete"]["correct"] == 1 and extraction["complete"]["n"] == 4
    assert extraction["documents"] == {"parsed": 3, "needs_review": 1}  # CASE-D's not counted
    assert extraction["fields"] == {"authoritative": 3, "needs_review": 1, "corrected": 1}
    assert extraction["field_corrections"] == 1 and extraction["cases_with_corrections"] == 1


def test_accuracy_separates_blind_reviews_and_keeps_failures() -> None:
    ops = run()["accuracy"]["opsbridge"]
    assert ops["blind"]["next_action"] | {"wilson95": None} == {
        "correct": 1, "n": 2, "rate": 0.5, "wilson95": None,
    }  # fmt: skip  # CASE-A right, CASE-E never investigated
    assert ops["blind"]["case"]["correct"] == 1
    assert ops["not_blind"]["next_action"]["correct"] == 0
    assert ops["not_blind"]["next_action"]["n"] == 1
    assert [(d["case"], d["blind"], d["got"]) for d in ops["disagreements"]] == [
        ("CASE-B", False, FOLLOW_UP.as_dict()),
        ("CASE-E", True, None),
    ]
    manual = run()["accuracy"]["manual"]
    assert manual["next_action"]["correct"] == 1 and manual["next_action"]["n"] == 2
    assert manual["without_process_decision"] == 1


def test_approval_corrections_compare_what_was_approved_with_the_draft() -> None:
    # CASE-C has no reference row, but it is in scope: only accuracy needs a reference.
    approval = run()["approval"]
    counts = ("chains", "approved_as_drafted", "approved_changed", "rejected", "undecided")
    assert {k: approval[k] for k in counts} == {
        "chains": 3, "approved_as_drafted": 1, "approved_changed": 1, "rejected": 1,
        "undecided": 0,
    }  # fmt: skip
    assert approval["changed_fields"] == {"body": 1}
    assert approval["corrected"]["correct"] == 2 and approval["corrected"]["n"] == 3


def test_times_report_open_cases_and_respect_the_cut_off() -> None:
    times = run()["time_hours"]
    assert times["opsbridge"]["to_decision"] == {
        "n": 4, "reached": 2, "open": 2, "median": 2.5, "p90": 4.0,
    }  # fmt: skip
    assert times["opsbridge"]["to_resolution"] == {
        "n": 4, "reached": 1, "open": 3, "median": 6.0, "p90": 6.0,
    }  # fmt: skip
    assert times["manual"]["to_decision"] == {
        "n": 3, "reached": 1, "open": 2, "median": 2.0, "p90": 2.0,
    }  # fmt: skip
    early = report(CASES, REFERENCE, since=at(-24), until=at(1), as_of=at(3))["time_hours"]
    assert early["opsbridge"]["to_decision"]["reached"] == 1  # CASE-B's approval came later
    assert early["opsbridge"]["to_resolution"]["reached"] == 0
