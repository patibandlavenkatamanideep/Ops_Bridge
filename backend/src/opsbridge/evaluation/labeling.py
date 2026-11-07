"""Independent labels, their agreement and their adjudication (evaluation plan v2, A10).

Prepared before any labeller exists, so how agreement is measured and how a disagreement is
settled are fixed before a label is seen. Nothing here has been run on real labels.

Labels (CSV, one row per case and labeller):

    case,labeller,outcome,types,next_action,required_facts,forbidden_facts

`outcome`, `types` (separated by `;`) and `next_action` use OpsBridge's vocabulary.
`required_facts` and `forbidden_facts` are the ids, separated by `;`, of the facts an
explanation must contain and must not state. The labelling instructions define those ids,
so here they are opaque. Every case needs at least two labellers. The file is read strictly,
like the pilot's reference file: every problem is reported with its line.

Agreement is reported per field, as Cohen's kappa for each pair of labellers over the cases
both labelled. A categorical field (`outcome`, `next_action`) is one decision per case. A
set field (`types`, the facts) is one yes-or-no decision per case and member, over the
members either labeller named. Kappa is undefined when both labellers used a single value
throughout (expected agreement 1); it is reported as None, with the raw agreement.

Adjudications (CSV): `case,field,adjudicator,decision,reason`. A decision is a value of the
field, sets separated by `;`. A disagreement is never overwritten: it stays listed with every
label, and the reference takes the adjudicated value. An adjudicator who labelled the case,
a decision for a field that agreed, or a missing reason is refused. A field still in
disagreement leaves its case out of the reference, and the reference is incomplete.
"""

from __future__ import annotations

import csv
import itertools
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opsbridge.evaluation.verdict import DISCREPANCY_TYPES, NEXT_ACTIONS, OUTCOMES

LABEL_COLUMNS = (
    "case",
    "labeller",
    "outcome",
    "types",
    "next_action",
    "required_facts",
    "forbidden_facts",
)
ADJUDICATION_COLUMNS = ("case", "field", "adjudicator", "decision", "reason")
CATEGORICAL = ("outcome", "next_action")
SETS = ("types", "required_facts", "forbidden_facts")
FIELDS = CATEGORICAL + SETS
_VOCABULARY: dict[str, tuple[str, ...]] = {
    "outcome": OUTCOMES,
    "next_action": NEXT_ACTIONS,
    "types": DISCREPANCY_TYPES,
}

Value = str | frozenset[str]


class LabelError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{len(problems)} problem(s) in the file")
        self.problems = problems


@dataclass(frozen=True)
class Label:
    line: int
    case: str
    labeller: str
    values: dict[str, Value]


@dataclass(frozen=True)
class Adjudication:
    line: int
    case: str
    field: str
    adjudicator: str
    decision: Value
    reason: str


def _members(text: str) -> frozenset[str]:
    return frozenset(m.strip() for m in text.split(";") if m.strip())


def _value(field: str, text: str, problems: list[str]) -> Value:
    allowed = _VOCABULARY.get(field)
    if field in CATEGORICAL:
        if allowed is not None and text not in allowed:
            problems.append(f"{field} {text!r} is not one of the labels allowed")
        return text
    members = _members(text)
    if allowed is not None:
        problems.extend(
            f"{field}: {m!r} is not one of {', '.join(allowed)}"
            for m in sorted(members - set(allowed))
        )
    return members


def _rows(path: Path, columns: tuple[str, ...]) -> list[tuple[int, dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        if tuple(next(reader, ())) != columns:
            raise LabelError([f"line 1: columns must be {','.join(columns)}"])
        rows, problems = [], []
        for fields in reader:
            if not any(x.strip() for x in fields):
                continue
            if len(fields) != len(columns):
                problems.append(
                    f"line {reader.line_num}: {len(fields)} fields, expected {len(columns)}"
                )
                continue
            rows.append(
                (reader.line_num, dict(zip(columns, (x.strip() for x in fields), strict=True)))
            )
    if problems:
        raise LabelError(problems)
    return rows


def read_labels(path: Path) -> list[Label]:
    problems: list[str] = []
    labels: list[Label] = []
    seen: dict[tuple[str, str], int] = {}
    for line, row in _rows(path, LABEL_COLUMNS):
        found: list[str] = []
        if not row["case"] or not row["labeller"]:
            found.append("case and labeller are required")
        values = {f: _value(f, row[f], found) for f in FIELDS}
        key = (row["case"], row["labeller"])
        if key in seen:
            found.append(
                f"labeller {row['labeller']!r} labels case {row['case']!r} again (line {seen[key]})"
            )
        seen.setdefault(key, line)
        problems.extend(f"line {line}: {p}" for p in found)
        labels.append(Label(line, row["case"], row["labeller"], values))
    per_case = Counter(label.case for label in labels)
    problems.extend(
        f"case {case!r} has {n} labeller(s); at least two are needed"
        for case, n in sorted(per_case.items())
        if n < 2
    )
    if problems:
        raise LabelError(problems)
    return labels


def read_adjudications(path: Path, labels: list[Label]) -> list[Adjudication]:
    by_case: dict[str, list[Label]] = defaultdict(list)
    for label in labels:
        by_case[label.case].append(label)
    problems: list[str] = []
    out: list[Adjudication] = []
    seen: dict[tuple[str, str], int] = {}
    for line, row in _rows(path, ADJUDICATION_COLUMNS):
        found: list[str] = []
        case, field = row["case"], row["field"]
        if case not in by_case:
            found.append(f"case {case!r} was not labelled")
        if field not in FIELDS:
            found.append(f"field {field!r} is not one of {', '.join(FIELDS)}")
        elif case in by_case and len({label.values[field] for label in by_case[case]}) == 1:
            found.append(
                f"the labellers agreed on {field} for case {case!r}: nothing to adjudicate"
            )
        if row["adjudicator"] in {label.labeller for label in by_case.get(case, [])}:
            found.append(f"adjudicator {row['adjudicator']!r} labelled case {case!r}")
        if not row["adjudicator"] or not row["reason"]:
            found.append("adjudicator and reason are required")
        decision = _value(field, row["decision"], found) if field in FIELDS else row["decision"]
        if (case, field) in seen:
            found.append(f"case {case!r} {field} is adjudicated again (line {seen[(case, field)]})")
        seen.setdefault((case, field), line)
        problems.extend(f"line {line}: {p}" for p in found)
        out.append(Adjudication(line, case, field, row["adjudicator"], decision, row["reason"]))
    if problems:
        raise LabelError(problems)
    return out


def kappa(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    """Cohen's kappa over paired decisions, with the raw agreement it corrects."""
    n = len(pairs)
    if not n:
        return {"n": 0, "agreement": None, "kappa": None}
    observed = sum(a == b for a, b in pairs) / n
    first, second = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(first[v] * second[v] for v in first.keys() | second.keys()) / (n * n)
    k = None if expected == 1 else (observed - expected) / (1 - expected)
    return {
        "n": n,
        "agreement": round(observed, 4),
        "kappa": None if k is None else round(k, 4),
    }


def _decisions(a: Label, b: Label, field: str) -> list[tuple[Any, Any]]:
    x, y = a.values[field], b.values[field]
    if field in CATEGORICAL:
        return [(x, y)]
    assert isinstance(x, frozenset) and isinstance(y, frozenset)
    return [(m in x, m in y) for m in sorted(x | y)]


def agreement(labels: list[Label]) -> dict[str, list[dict[str, Any]]]:
    """Per field, Cohen's kappa for each pair of labellers over the cases both labelled."""
    by_labeller: dict[str, dict[str, Label]] = defaultdict(dict)
    for label in labels:
        by_labeller[label.labeller][label.case] = label
    out: dict[str, list[dict[str, Any]]] = {}
    for field in FIELDS:
        rows = []
        for p, q in itertools.combinations(sorted(by_labeller), 2):
            shared = sorted(by_labeller[p].keys() & by_labeller[q].keys())
            pairs = [
                d for c in shared for d in _decisions(by_labeller[p][c], by_labeller[q][c], field)
            ]
            rows.append({"labellers": [p, q], "cases": len(shared), **kappa(pairs)})
        out[field] = rows
    return out


def _shown(value: Value) -> str | list[str]:
    return sorted(value) if isinstance(value, frozenset) else value


def reference(labels: list[Label], adjudications: list[Adjudication]) -> dict[str, Any]:
    """The reference each case is scored against: agreed fields as labelled, disagreements as
    adjudicated. Every disagreement stays listed with every label."""
    by_case: dict[str, list[Label]] = defaultdict(list)
    for label in labels:
        by_case[label.case].append(label)
    decided = {(a.case, a.field): a for a in adjudications}
    cases: dict[str, dict[str, Any]] = {}
    disagreements, unresolved = [], []
    for case, case_labels in sorted(by_case.items()):
        values: dict[str, Any] = {}
        for field in FIELDS:
            seen = {label.values[field] for label in case_labels}
            if len(seen) == 1:
                values[field] = _shown(next(iter(seen)))
                continue
            a = decided.get((case, field))
            disagreements.append(
                {
                    "case": case,
                    "field": field,
                    "labels": {
                        label.labeller: _shown(label.values[field]) for label in case_labels
                    },
                    "adjudication": None
                    if a is None
                    else {"by": a.adjudicator, "decision": _shown(a.decision), "reason": a.reason},
                }
            )
            if a is None:
                unresolved.append({"case": case, "field": field})
            else:
                values[field] = _shown(a.decision)
        if len(values) == len(FIELDS):
            cases[case] = values
    return {
        "complete": not unresolved,
        "cases": cases,
        "disagreements": disagreements,
        "unresolved": unresolved,
    }
