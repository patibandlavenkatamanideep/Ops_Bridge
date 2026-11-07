"""The four pilot measures (docs/pilot-plan.md, protocol opsbridge-pilot/2), on facts, a cohort
and reference rows written out by hand so every count below can be checked by reading them.

The unit is the enrolled invoice. Each measure keeps its failures in its denominator: an
enrolled invoice never opened or never investigated counts as wrong, a case still open at the
cut-off is reported as open (not dropped from the times), and a reviewer decision recorded
after the case reached OpsBridge is reported apart from the blind ones, since the reviewer may
have seen its output. A row finds its case by the invoice, as intake identified it, because a
blind decision is recorded before the case and its number exist.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from opsbridge.evaluation.verdict import Verdict
from opsbridge.pilot.cohort import Cohort, Member
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
    """CASE-A's invoice is INV-A, from no named supplier, unless changed."""
    base: dict[str, object] = {
        "case_number": case,
        "invoice": (f"INV-{case.removeprefix('CASE-')}", None),
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
        "explanation_origin": "template",
    }
    return CaseFacts(**(base | changes))  # type: ignore[arg-type]


def ref(arm: str, key: str, line: int, **changes: object) -> ReferenceRow:
    """key: the invoice number of an opsbridge row, the pseudonym of a manual one."""
    base: dict[str, object] = {
        "line": line,
        "arm": arm,
        "case": "" if arm == "opsbridge" else key,
        "invoice": (key, None) if arm == "opsbridge" else None,
        "reviewed_at": at(-1),
        "reference": FOLLOW_UP,
        "process": None,
        "started_at": None,
        "decided_at": None,
        "resolved_at": None,
        "excluded": "",
    }
    return ReferenceRow(**(base | changes))  # type: ignore[arg-type]


def cohort(*keys: str) -> Cohort:
    """Members in order, from line 2: INV-* are opsbridge invoices, M-* manual references."""
    return Cohort(
        tuple(
            Member(line, "opsbridge", "", (k, None))
            if k.startswith("INV-")
            else Member(line, "manual", k, None)
            for line, k in enumerate(keys, start=2)
        ),
        "c0ffee" * 10 + "c0ff",
    )


COHORT = cohort("INV-A", "INV-B", "INV-C", "INV-D", "INV-E", "INV-F", "M-1", "M-2", "M-3")
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
        explanation_origin="model",
    ),
    # Evidence incomplete; its proposal was rejected; still open.
    facts(
        "CASE-C",
        evidence_readiness="incomplete",
        chains=[ProposalChain(DRAFT, "rejected", DRAFT, 1)],
    ),
    # Excluded by the reviewer: in no measure at all.
    facts("CASE-D", evidence_readiness="incomplete", resolved_at=at(0.5)),
    # Opened but never investigated: wrong on every axis, not left out.
    facts(
        "CASE-E",
        first_investigated_at=None,
        verdict=None,
        evidence_readiness="not_investigated",
        documents=Counter(),
        explanation_origin=None,
    ),
    # Opened for an invoice that was not enrolled: listed, measured nowhere.
    facts("CASE-X", chains=[ProposalChain(DRAFT, "approved", DRAFT, 1)]),
]  # INV-F was enrolled and never opened.
REFERENCE = [
    ref("opsbridge", "INV-A", 2),
    ref("opsbridge", "INV-B", 3, reviewed_at=at(2), reference=QUERY),
    ref("opsbridge", "INV-C", 4),
    ref("opsbridge", "INV-D", 5, reference=None, reviewed_at=None, excluded="credit note"),
    ref("opsbridge", "INV-E", 6),
    ref("opsbridge", "INV-F", 7),
    ref(
        "manual", "M-1", 8, reference=MATCHED, process=MATCHED,
        started_at=at(-1), decided_at=at(1), resolved_at=at(5),
    ),
    ref("manual", "M-2", 9, reference=QUERY, process=FOLLOW_UP, started_at=at(-1), decided_at=at(2)),
    ref("manual", "M-3", 10, reference=QUERY, started_at=at(-1)),  # process not recorded
]  # fmt: skip


def run(as_of: datetime = at(24)) -> dict[str, Any]:
    return report(CASES, REFERENCE, COHORT, since=at(-24), until=at(1), as_of=as_of)


def test_every_enrolled_invoice_is_accounted_for() -> None:
    result = run()
    ops = result["coverage"]["opsbridge"]
    assert ops["enrolled"] == 6
    assert ops["excluded"] == [{"case": "CASE-D", "cohort_line": 5, "reason": "credit note"}]
    assert ops["no_case"] == [7]  # INV-F, by its cohort line
    assert ops["not_investigated"] == ["CASE-E"] and ops["investigated"] == 3
    assert ops["references"] == {"blind": 4, "not_blind": 1}
    assert ops["cases_outside_cohort"] == ["CASE-X"]
    assert ops["explanations"] == {"template": 2, "model": 1}
    assert result["coverage"]["manual"]["progress"] == {"started": 3}
    assert result["complete"] and result["incomplete_because"] == []
    assert result["cohort"]["members"] == 9
    # Cases are named by number, rows by line; the report names no invoice.
    assert "INV-" not in json.dumps(result)


def test_a_row_finds_its_case_by_the_invoice_as_intake_identified_it() -> None:
    # Intake keeps an invoice from a named supplier apart from the same number without one,
    # so a member matches a case only when both agree.
    cases = [facts("CASE-F", invoice=("INV-F", "SUP-1")), facts("CASE-G", verdict=QUERY)]
    rows = [ref("opsbridge", "INV-F", 2), ref("opsbridge", "INV-G", 3, reference=QUERY)]
    result = report(cases, rows, cohort("INV-F", "INV-G"), since=at(-24), until=at(1), as_of=at(24))
    assert result["coverage"]["opsbridge"]["no_case"] == [2]
    assert result["coverage"]["opsbridge"]["cases_outside_cohort"] == ["CASE-F"]
    blind = result["accuracy"]["opsbridge"]["blind"]["case"]
    assert (blind["correct"], blind["n"]) == (1, 2)  # INV-F never reached its case: wrong


def test_extraction_completeness_counts_every_enrolled_invoice() -> None:
    extraction = run()["extraction"]
    assert extraction["evidence_readiness"] == {
        "complete": 1, "needs_review": 1, "incomplete": 1, "not_investigated": 1, "no_case": 1,
    }  # fmt: skip
    assert extraction["complete"]["correct"] == 1 and extraction["complete"]["n"] == 5
    assert extraction["documents"] == {"parsed": 3, "needs_review": 1}  # CASE-D's not counted
    assert extraction["fields"] == {"authoritative": 3, "needs_review": 1, "corrected": 1}
    assert extraction["field_corrections"] == 1 and extraction["cases_with_corrections"] == 1


def test_accuracy_separates_blind_reviews_and_keeps_failures() -> None:
    result = run()
    ops = result["accuracy"]["opsbridge"]
    # A and C right; E never investigated and F never opened, both wrong.
    assert ops["blind"]["next_action"] | {"wilson95": None} == {
        "correct": 2, "n": 4, "rate": 0.5, "wilson95": None,
    }  # fmt: skip
    assert result["headline"]["correct"] == 2 and result["headline"]["n"] == 4
    only = ops["blind_investigated_only"]
    assert "selected subset" in only["note"] and only["next_action"]["n"] == 2
    assert ops["not_blind"]["next_action"]["correct"] == 0
    assert ops["not_blind"]["next_action"]["n"] == 1
    assert [(d["case"], d["cohort_line"], d["blind"], d["got"]) for d in ops["disagreements"]] == [
        ("CASE-E", 6, True, None),
        (None, 7, True, None),
        ("CASE-B", 3, False, FOLLOW_UP.as_dict()),
    ]
    manual = result["accuracy"]["manual"]
    assert manual["next_action"]["correct"] == 1 and manual["next_action"]["n"] == 2
    assert manual["without_process_decision"] == 1


def test_approval_corrections_compare_what_was_approved_with_the_draft() -> None:
    # CASE-X's approval is not counted: its invoice was not enrolled.
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
    assert times["opsbridge"]["not_opened"] == 1
    assert times["opsbridge"]["to_decision"] == {
        "n": 4, "reached": 2, "open": 2, "median": 2.5, "p90": 4.0,
    }  # fmt: skip
    assert times["opsbridge"]["to_resolution"] == {
        "n": 4, "reached": 1, "open": 3, "median": 6.0, "p90": 6.0,
    }  # fmt: skip
    assert times["manual"]["to_decision"] == {
        "n": 3, "reached": 2, "open": 1, "median": 2.5, "p90": 3.0,
    }  # fmt: skip
    early = run(as_of=at(3))["time_hours"]
    assert early["opsbridge"]["to_decision"]["reached"] == 1  # CASE-B's approval came later
    assert early["opsbridge"]["to_resolution"]["reached"] == 0
