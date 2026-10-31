"""The reviewer's reference file for a pilot (docs/pilot-plan.md, protocol opsbridge-pilot/1).

The reviewer records an independent decision for each sampled blocked invoice, and the
existing process's decisions and timings for the manual arm. The file is read strictly: a
pilot measured against a misspelled outcome, a timestamp without a time zone, or hand-entered
OpsBridge timings would be measured against something other than its definition, so every
problem is reported with its line and nothing is measured.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opsbridge.evaluation.verdict import Verdict
from opsbridge.pilot.reference import COLUMNS, ReferenceError, read_reference


def write(tmp_path: Path, *rows: dict[str, str], columns: tuple[str, ...] = COLUMNS) -> Path:
    path = tmp_path / "reference.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        out = csv.DictWriter(f, fieldnames=columns, restval="")
        out.writeheader()
        out.writerows(rows)
    return path


OPSBRIDGE = {
    "arm": "opsbridge",
    "case": "CASE-0001",
    "reviewed_at": "2026-11-02T09:00:00+00:00",
    "outcome": "discrepancies_found",
    "types": "quantity_mismatch;price_mismatch",
    "next_action": "propose_action:create_follow_up_task",
}
MANUAL = {
    "arm": "manual",
    "case": "M-7",
    "reviewed_at": "2026-11-02T09:30:00+00:00",
    "outcome": "matched",
    "next_action": "no_discrepancy:record_review_note",
    "process_outcome": "discrepancies_found",
    "process_types": "price_mismatch",
    "process_next_action": "propose_action:create_supplier_message_draft",
    "started_at": "2026-11-02T10:00:00+01:00",
    "decided_at": "2026-11-02T12:00:00+01:00",
}


def test_a_valid_file_is_read_with_its_decisions_and_timings(tmp_path: Path) -> None:
    ops, manual = read_reference(write(tmp_path, OPSBRIDGE, MANUAL))
    assert (ops.arm, ops.case, ops.line) == ("opsbridge", "CASE-0001", 2)
    assert ops.reference == Verdict(
        "discrepancies_found",
        ("price_mismatch", "quantity_mismatch"),
        "propose_action:create_follow_up_task",
    )
    assert ops.process is None and ops.started_at is None and not ops.excluded
    assert manual.reference == Verdict("matched", (), "no_discrepancy:record_review_note")
    assert manual.process == Verdict(
        "discrepancies_found", ("price_mismatch",), "propose_action:create_supplier_message_draft"
    )
    assert manual.started_at == datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert manual.resolved_at is None  # still open: censored, not dropped


def test_an_excluded_case_needs_only_its_reason(tmp_path: Path) -> None:
    excluded = {"arm": "opsbridge", "case": "CASE-0002", "excluded": "credit note, out of scope"}
    [row] = read_reference(write(tmp_path, excluded))
    assert row.excluded == "credit note, out of scope" and row.reference is None


@pytest.mark.parametrize(
    ("bad", "problem"),
    [
        ({"arm": "both"}, "arm 'both' is not opsbridge or manual"),
        ({"case": ""}, "case is empty"),
        ({"reviewed_at": ""}, "reviewed_at is required"),
        ({"reviewed_at": "2026-11-02T09:00:00"}, "reviewed_at has no time zone"),
        ({"reviewed_at": "yesterday"}, "reviewed_at 'yesterday' is not an ISO 8601 time"),
        ({"outcome": "mismatch"}, "outcome 'mismatch' is not one of"),
        ({"types": "quantity_mismatch;tax_mismatch"}, "types: 'tax_mismatch' is not one of"),
        ({"next_action": "follow up"}, "next_action 'follow up' is not one of"),
        # OpsBridge's timings and decisions come from its own records, never from the file.
        ({"started_at": "2026-11-02T09:00:00+00:00"}, "started_at must be empty for opsbridge"),
        ({"process_outcome": "matched"}, "process_outcome must be empty for opsbridge"),
    ],
)
def test_each_problem_is_reported_with_its_line(
    tmp_path: Path, bad: dict[str, str], problem: str
) -> None:
    with pytest.raises(ReferenceError) as caught:
        read_reference(write(tmp_path, MANUAL, OPSBRIDGE | bad))
    assert any(p.startswith("line 3: ") and problem in p for p in caught.value.problems), (
        caught.value.problems
    )


def test_manual_timings_must_be_in_order_and_start_is_required(tmp_path: Path) -> None:
    backwards = MANUAL | {"decided_at": "2026-11-02T08:00:00+01:00"}
    unstarted = MANUAL | {"case": "M-8", "started_at": ""}
    with pytest.raises(ReferenceError) as caught:
        read_reference(write(tmp_path, backwards, unstarted))
    assert caught.value.problems == [
        "line 2: decided_at is before started_at",
        "line 3: started_at is required for manual",
    ]


def test_a_case_appears_once_per_arm_and_the_columns_are_exact(tmp_path: Path) -> None:
    with pytest.raises(ReferenceError) as caught:
        read_reference(write(tmp_path, OPSBRIDGE, OPSBRIDGE))
    assert caught.value.problems == ["line 3: opsbridge case 'CASE-0001' repeats line 2"]
    renamed = tuple("result" if c == "outcome" else c for c in COLUMNS)
    with pytest.raises(ReferenceError) as caught:
        read_reference(write(tmp_path, {"arm": "opsbridge"}, columns=renamed))
    assert caught.value.problems[0].startswith("line 1: columns must be")
