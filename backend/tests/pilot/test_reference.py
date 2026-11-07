"""The reviewer's reference file for a pilot (docs/pilot-plan.md, protocol opsbridge-pilot/1).

The reviewer records an independent decision for each sampled blocked invoice, and the
existing process's decisions and timings for the manual arm. A blind decision is recorded
before intake, when the case and its number don't exist yet, so an OpsBridge row names its
invoice as intake will identify it; a manual row is pseudonymous. The file is read strictly: a
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
    "invoice": "inv-7001",
    "supplier": "ven-lumen",
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
    # Normalized as intake normalizes it, so the row finds the case intake creates.
    assert (ops.arm, ops.invoice, ops.case, ops.line) == (
        "opsbridge",
        ("INV-7001", "VEN-LUMEN"),
        "",
        2,
    )
    assert (manual.case, manual.invoice) == ("M-7", None)
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
    excluded = {"arm": "opsbridge", "invoice": "INV-7002", "excluded": "credit note, out of scope"}
    [row] = read_reference(write(tmp_path, excluded))
    assert row.excluded == "credit note, out of scope" and row.reference is None


@pytest.mark.parametrize(
    ("bad", "problem"),
    [
        ({"arm": "both"}, "arm 'both' is not opsbridge or manual"),
        ({"invoice": ""}, "invoice is required for opsbridge"),
        # Assigned at intake, after a blind decision: a number typed in later could be wrong.
        ({"case": "CASE-00001"}, "case must be empty for opsbridge"),
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


@pytest.mark.parametrize(
    ("bad", "problem"),
    [
        ({"case": ""}, "case is required for manual"),
        ({"invoice": "INV-7"}, "invoice must be empty for manual"),
        ({"supplier": "VEN-LUMEN"}, "supplier must be empty for manual"),
    ],
)
def test_a_manual_row_is_pseudonymous(tmp_path: Path, bad: dict[str, str], problem: str) -> None:
    with pytest.raises(ReferenceError) as caught:
        read_reference(write(tmp_path, OPSBRIDGE, MANUAL | bad))
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


def test_a_blank_time_is_a_point_not_reached_so_impossible_orders_are_refused(
    tmp_path: Path,
) -> None:
    """A process decision without the time it was made, or a resolution before the decision,
    cannot be placed against a cut-off (review N05)."""
    undated = MANUAL | {"decided_at": "", "resolved_at": ""}
    reversed_ = MANUAL | {
        "case": "M-9", "decided_at": "2026-11-03T12:00:00+00:00",
        "resolved_at": "2026-11-03T11:00:00+00:00",
    }  # fmt: skip
    with pytest.raises(ReferenceError) as caught:
        read_reference(write(tmp_path, undated, reversed_))
    assert caught.value.problems == [
        "line 2: a process decision needs the decided_at it was made at",
        "line 3: resolved_at is before decided_at",
    ]


def test_an_invoice_or_case_appears_once_and_the_columns_are_exact(tmp_path: Path) -> None:
    same = OPSBRIDGE | {"invoice": "INV-7001 ", "supplier": "VEN-LUMEN"}  # once normalized
    other_supplier = OPSBRIDGE | {"supplier": ""}  # intake keeps these apart, so may the file
    with pytest.raises(ReferenceError) as caught:
        read_reference(write(tmp_path, OPSBRIDGE, same, other_supplier, MANUAL, MANUAL))
    assert caught.value.problems == [
        "line 3: opsbridge invoice 'INV-7001' from 'VEN-LUMEN' repeats line 2",
        "line 6: manual case 'M-7' repeats line 5",
    ]
    renamed = tuple("result" if c == "outcome" else c for c in COLUMNS)
    with pytest.raises(ReferenceError) as caught:
        read_reference(write(tmp_path, {"arm": "opsbridge"}, columns=renamed))
    assert caught.value.problems[0].startswith("line 1: columns must be")


def test_the_file_can_be_checked_while_the_pilot_runs_without_the_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """pilot-report reads the file only at the cut-off, weeks after a row was written; the
    operator can check it as it grows, the same strict way, with nothing else needed."""
    from opsbridge import cli

    good = write(tmp_path, OPSBRIDGE, MANUAL)
    assert cli.main(["pilot-check-reference", "--reference", str(good)]) == 0
    assert "2 rows (1 opsbridge, 1 manual)" in capsys.readouterr().out

    late = {**OPSBRIDGE, "reviewed_at": "2026-11-02T09:00:00"}  # no time zone
    bad = write(tmp_path, late, {**MANUAL, "outcome": "fine"})
    assert cli.main(["pilot-check-reference", "--reference", str(bad)]) == 2
    printed = capsys.readouterr().out
    assert "2 problem(s)" in printed
    assert "line 2: reviewed_at" in printed and "line 3: outcome 'fine'" in printed
