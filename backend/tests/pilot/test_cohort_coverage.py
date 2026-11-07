"""The frozen cohort, and every enrolled invoice accounted for (review N05).

Under opsbridge-pilot/1 the measures took their denominators from the cases opened and the
rows written: an enrolled invoice never opened was in none of them, and a missing row left a
member out of accuracy with nothing saying so. Now the cohort is read strictly, a reference
row outside it is refused, every member is in a coverage count, and a member with no reviewer
decision at the cut-off makes the report incomplete instead of shrinking its denominator.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from opsbridge import cli
from opsbridge.pilot.cohort import COLUMNS, CohortError, read_cohort
from opsbridge.pilot.measures import CohortMismatch, report

from .test_measures import CASES, FOLLOW_UP, REFERENCE, at, cohort, facts, ref


def write(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join([",".join(COLUMNS), *lines]) + "\n", encoding="utf-8")
    return path


def test_a_cohort_is_read_with_its_digest(tmp_path: Path) -> None:
    path = write(
        tmp_path / "c.csv", ["opsbridge,,inv-7001 ,sup-1", "manual,M-1,,", "opsbridge,,INV-7002,"]
    )
    c = read_cohort(path)
    assert [(m.line, m.arm, m.case, m.invoice) for m in c.members] == [
        (2, "opsbridge", "", ("INV-7001", "SUP-1")),  # normalized as intake does
        (3, "manual", "M-1", None),
        (4, "opsbridge", "", ("INV-7002", None)),
    ]
    assert c.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("opsbridge,C-1,INV-1,", "case must be empty"),
        ("opsbridge,,,", "invoice is required"),
        ("manual,,,", "case is required"),
        ("manual,M-1,INV-1,", "must be empty for manual"),
        ("both,M-1,,", "is not opsbridge or manual"),
        ("opsbridge,,INV-1", "fields, expected 4"),
    ],
)
def test_a_cohort_row_is_read_strictly(tmp_path: Path, line: str, problem: str) -> None:
    with pytest.raises(CohortError) as exc:
        read_cohort(write(tmp_path / "c.csv", [line]))
    assert any(problem in p for p in exc.value.problems), exc.value.problems


def test_a_member_appears_once_and_the_columns_are_exact(tmp_path: Path) -> None:
    with pytest.raises(CohortError, match="1 problem") as exc:
        read_cohort(write(tmp_path / "c.csv", ["opsbridge,,INV-1,", "opsbridge,,inv-1,"]))
    assert exc.value.problems == ["line 3: repeats line 2"]
    (tmp_path / "bad.csv").write_text("arm,case,invoice\n", encoding="utf-8")
    with pytest.raises(CohortError, match="1 problem"):
        read_cohort(tmp_path / "bad.csv")
    with pytest.raises(CohortError):
        read_cohort(write(tmp_path / "empty.csv", []))


def test_a_reference_row_outside_the_cohort_is_refused() -> None:
    """A row for an invoice that was not enrolled means the sample was not the frozen one."""
    rows = [*REFERENCE, ref("opsbridge", "INV-Z", 11)]
    with pytest.raises(CohortMismatch) as exc:
        report(CASES, rows, cohort("INV-A"), since=at(-24), until=at(1), as_of=at(24))
    assert "reference line 3" in exc.value.problems[0]  # INV-B, the first row not enrolled


def test_a_member_without_a_decision_makes_the_report_incomplete() -> None:
    members = cohort("INV-A", "INV-B", "INV-F")  # INV-F never opened, and never reviewed
    rows = [ref("opsbridge", "INV-A", 2), ref("opsbridge", "INV-B", 3)]
    result = report(
        [facts("CASE-A"), facts("CASE-B")], rows, members, since=at(-24), until=at(1), as_of=at(24)
    )
    assert not result["complete"] and not result["headline"]["complete"]
    assert result["incomplete_because"] == [
        "1 enrolled invoice(s) have no reviewer decision recorded by the cut-off (cohort lines 4)"
    ]
    coverage = result["coverage"]["opsbridge"]
    assert coverage["references"] == {"blind": 2, "missing": 1} and coverage["no_case"] == [4]
    # The headline does not quietly become 2/2: the member is counted as missing.
    assert (result["headline"]["correct"], result["headline"]["n"]) == (2, 2)
    assert result["accuracy"]["opsbridge"]["blind"]["next_action"]["n"] == 2


def test_the_cli_says_incomplete_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """pilot-check-reference reads both files without a database and names stray rows."""
    members = write(tmp_path / "cohort.csv", ["opsbridge,,INV-1,", "manual,M-1,,"])
    reference = tmp_path / "reference.csv"
    from opsbridge.pilot.reference import COLUMNS as REF

    row = {c: "" for c in REF} | {
        "arm": "opsbridge", "invoice": "INV-2", "reviewed_at": at(-1).isoformat(),
        "outcome": FOLLOW_UP.outcome, "types": ";".join(FOLLOW_UP.types),
        "next_action": FOLLOW_UP.next_action,
    }  # fmt: skip
    reference.write_text(",".join(REF) + "\n" + ",".join(row[c] for c in REF) + "\n")
    code = cli.main(
        ["pilot-check-reference", "--reference", str(reference), "--cohort", str(members)]
    )
    assert code == 2 and "lines 2 name no invoice in the cohort" in capsys.readouterr().out
    row["invoice"] = "INV-1"
    reference.write_text(",".join(REF) + "\n" + ",".join(row[c] for c in REF) + "\n")
    assert (
        cli.main(["pilot-check-reference", "--reference", str(reference), "--cohort", str(members)])
        == 0
    )
    assert "2 enrolled, 1 without a row yet" in capsys.readouterr().out
