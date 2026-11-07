"""Independent labels, agreement and adjudication for evaluation v2 (A10), before any label.

The kappas are checked against values worked out by hand from the textbook formula
(observed agreement corrected by the agreement the two labellers' own frequencies predict),
not against this module's own output. Labels and adjudications are written out here; no real
labeller has labelled anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opsbridge import cli
from opsbridge.evaluation.labeling import (
    ADJUDICATION_COLUMNS,
    LABEL_COLUMNS,
    LabelError,
    agreement,
    kappa,
    read_adjudications,
    read_labels,
    reference,
)

FOLLOW_UP = "propose_action:create_follow_up_task"
QUERY = "propose_action:create_supplier_message_draft"


def write(path: Path, columns: tuple[str, ...], rows: list[str]) -> Path:
    path.write_text("\n".join([",".join(columns), *rows]) + "\n", encoding="utf-8")
    return path


def test_kappa_matches_the_textbook_value() -> None:
    # 2x2: both yes 20, yes/no 5, no/yes 10, both no 15. Observed 35/50 = 0.7; expected
    # (25*30 + 25*20) / 2500 = 0.5; kappa = (0.7 - 0.5) / (1 - 0.5) = 0.4.
    pairs = [(True, True)] * 20 + [(True, False)] * 5 + [(False, True)] * 10 + [(False, False)] * 15
    assert kappa(pairs) == {"n": 50, "agreement": 0.7, "kappa": 0.4}
    # One labeller always "yes", the other too: no chance correction is possible.
    assert kappa([(True, True)] * 4) == {"n": 4, "agreement": 1.0, "kappa": None}
    assert kappa([]) == {"n": 0, "agreement": None, "kappa": None}


LABELS = [
    # case, labeller, outcome, types, next_action, required facts, forbidden facts
    "C1,ana,matched,,no_discrepancy:record_review_note,,F9",
    "C1,ben,matched,,no_discrepancy:record_review_note,,F9",
    f"C2,ana,discrepancies_found,quantity_mismatch,{FOLLOW_UP},F1;F2,",
    f"C2,ben,discrepancies_found,quantity_mismatch,{FOLLOW_UP},F1,",
    f"C3,ana,discrepancies_found,price_mismatch,{QUERY},F3,",
    "C3,ben,needs_information,,request_information:create_follow_up_task,F3,",
    "C4,ana,manual_review,,escalate:assign_case,,",
    "C4,ben,manual_review,,escalate:assign_case,,",
]


def test_agreement_is_reported_per_field_and_chance_corrected(tmp_path: Path) -> None:
    labels = read_labels(write(tmp_path / "labels.csv", LABEL_COLUMNS, LABELS))
    result = agreement(labels)
    # outcome: ana m,d,d,r ; ben m,d,n,r -> 3/4 agree. Frequencies: ana m1 d2 r1, ben m1 d1 n1
    # r1 -> expected (1*1 + 2*1 + 1*1)/16 = 0.25; kappa = (0.75 - 0.25)/0.75 = 0.6667.
    [outcome] = result["outcome"]
    assert outcome == {
        "labellers": ["ana", "ben"],
        "cases": 4,
        "n": 4,
        "agreement": 0.75,
        "kappa": 0.6667,
    }
    # required facts: one decision per case and fact either named: C2 F1 (yes, yes), C2 F2
    # (yes, no), C3 F3 (yes, yes) -> 2/3; all of ana's are yes, so expected = ben's yes share
    # 2/3 -> kappa 0.
    [facts] = result["required_facts"]
    assert (facts["n"], facts["agreement"], facts["kappa"]) == (3, 0.6667, 0.0)


@pytest.mark.parametrize(
    ("rows", "problem"),
    [
        ([LABELS[0]], "has 1 labeller(s); at least two"),
        ([LABELS[0], LABELS[0]], "labels case 'C1' again"),
        ([LABELS[0].replace("matched", "fine"), LABELS[1]], "outcome 'fine' is not one of"),
        (
            [LABELS[2].replace("quantity_mismatch", "fraud"), LABELS[3]],
            "types: 'fraud' is not one of",
        ),
        (
            [",ana,matched,,no_discrepancy:record_review_note,,", LABELS[1]],
            "case and labeller are required",
        ),
    ],
)
def test_labels_are_read_strictly(tmp_path: Path, rows: list[str], problem: str) -> None:
    with pytest.raises(LabelError) as exc:
        read_labels(write(tmp_path / "labels.csv", LABEL_COLUMNS, rows))
    assert any(problem in p for p in exc.value.problems), exc.value.problems


def test_a_disagreement_is_kept_and_settled_only_by_an_independent_adjudication(
    tmp_path: Path,
) -> None:
    labels = read_labels(write(tmp_path / "labels.csv", LABEL_COLUMNS, LABELS))
    unsettled = reference(labels, [])
    assert not unsettled["complete"]
    assert unsettled["unresolved"] == [
        {"case": "C2", "field": "required_facts"},
        {"case": "C3", "field": "outcome"},
        {"case": "C3", "field": "next_action"},
        {"case": "C3", "field": "types"},
    ]
    assert sorted(unsettled["cases"]) == ["C1", "C4"]  # a case in dispute is not scored
    path = write(
        tmp_path / "adjudications.csv",
        ADJUDICATION_COLUMNS,
        [
            "C2,required_facts,cy,F1;F2,the receipt date is needed to act",
            "C3,outcome,cy,needs_information,the price list is missing",
            "C3,types,cy,,no discrepancy can be shown without the price list",
            "C3,next_action,cy,request_information:create_follow_up_task,as the outcome",
        ],
    )
    settled = reference(labels, read_adjudications(path, labels))
    assert settled["complete"] and sorted(settled["cases"]) == ["C1", "C2", "C3", "C4"]
    assert settled["cases"]["C2"]["required_facts"] == ["F1", "F2"]
    assert settled["cases"]["C3"]["outcome"] == "needs_information"
    c2 = next(d for d in settled["disagreements"] if d["case"] == "C2")
    assert c2["labels"] == {"ana": ["F1", "F2"], "ben": ["F1"]}  # never overwritten
    assert c2["adjudication"] == {
        "by": "cy",
        "decision": ["F1", "F2"],
        "reason": "the receipt date is needed to act",
    }


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        ("C2,required_facts,ana,F1,she was there", "adjudicator 'ana' labelled case 'C2'"),
        ("C1,outcome,cy,matched,why not", "the labellers agreed on outcome"),
        ("C2,required_facts,cy,F1,", "adjudicator and reason are required"),
        ("C9,outcome,cy,matched,unknown case", "case 'C9' was not labelled"),
        ("C3,outcome,cy,fine,bad value", "outcome 'fine' is not one of"),
    ],
)
def test_an_adjudication_is_read_strictly(tmp_path: Path, row: str, problem: str) -> None:
    labels = read_labels(write(tmp_path / "labels.csv", LABEL_COLUMNS, LABELS))
    with pytest.raises(LabelError) as exc:
        read_adjudications(write(tmp_path / "a.csv", ADJUDICATION_COLUMNS, [row]), labels)
    assert any(problem in p for p in exc.value.problems), exc.value.problems


def test_the_command_reports_agreement_and_exits_1_while_disputes_remain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = write(tmp_path / "labels.csv", LABEL_COLUMNS, LABELS)
    out = tmp_path / "report.json"
    assert cli.main(["eval-labels", "--labels", str(labels), "--out", str(out)]) == 1
    assert "4 field(s) still in dispute" in capsys.readouterr().out
    assert out.is_file()
    bad = write(tmp_path / "bad.csv", LABEL_COLUMNS, [LABELS[0]])
    assert cli.main(["eval-labels", "--labels", str(bad), "--out", str(out)]) == 2
