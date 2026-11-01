"""The reviewer's reference file (protocol opsbridge-pilot/1, docs/pilot-plan.md).

One CSV row per sampled blocked invoice:
- `arm`: `opsbridge` when the case was worked in OpsBridge, or `manual` when it was worked with
  the existing process;
- `case`: for `manual`, a pseudonymous reference (never an invoice number or a supplier's
  name); empty for `opsbridge`, whose case number is assigned at intake, after a blind
  decision was recorded, so a number typed in later could name the wrong case;
- `invoice`, `supplier` (`opsbridge` only): the sampled invoice as intake identifies it, its
  number and the supplier id entered with it (blank when none is). Intake allows one case per
  invoice and supplier in a tenant, so they name exactly one case once it is opened;
- `reviewed_at`, `outcome`, `types`, `next_action`: the reviewer's independent decision, in
  OpsBridge's vocabulary (types separated by `;`), and when it was recorded;
- `process_outcome`, `process_types`, `process_next_action` (manual arm, optional): what the
  existing process decided;
- `started_at`, `decided_at`, `resolved_at` (manual arm): when work on the invoice began,
  when its follow-up was decided and when it was resolved; blank while not reached;
- `excluded`: a reason to leave the case out of every measure; nothing else is then needed.

Times are ISO 8601 with a time zone. OpsBridge's own decisions and times come from its
records, so those columns must be blank on its arm. The file is read strictly: every problem
is reported with its line, and nothing is measured from a file that has one.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from opsbridge.domain import identifiers
from opsbridge.evaluation.verdict import DISCREPANCY_TYPES, NEXT_ACTIONS, OUTCOMES, Verdict

COLUMNS = (
    "arm",
    "case",
    "invoice",
    "supplier",
    "reviewed_at",
    "outcome",
    "types",
    "next_action",
    "process_outcome",
    "process_types",
    "process_next_action",
    "started_at",
    "decided_at",
    "resolved_at",
    "excluded",
)
ARMS = ("opsbridge", "manual")
MANUAL_ONLY = (
    "process_outcome",
    "process_types",
    "process_next_action",
    "started_at",
    "decided_at",
    "resolved_at",
)


class ReferenceError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{len(problems)} problem(s) in the reference file")
        self.problems = problems


@dataclass(frozen=True)
class ReferenceRow:
    line: int
    arm: str
    case: str  # manual: the pseudonymous reference; "" for opsbridge
    invoice: tuple[str, str | None] | None  # opsbridge: (number, supplier), normalized as intake
    reviewed_at: datetime | None
    reference: Verdict | None  # the reviewer's decision
    process: Verdict | None  # manual arm: the existing process's decision, when recorded
    started_at: datetime | None
    decided_at: datetime | None
    resolved_at: datetime | None
    excluded: str  # the reason, or "" when the case is measured


def _time(values: dict[str, str], name: str, problems: list[str]) -> datetime | None:
    value = values[name]
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        problems.append(f"{name} {value!r} is not an ISO 8601 time")
        return None
    if t.tzinfo is None:
        problems.append(f"{name} has no time zone")
        return None
    return t.astimezone(UTC)


def _verdict(values: dict[str, str], prefix: str, problems: list[str]) -> Verdict:
    outcome, action = values[f"{prefix}outcome"], values[f"{prefix}next_action"]
    types = [t.strip() for t in values[f"{prefix}types"].split(";") if t.strip()]
    if outcome not in OUTCOMES:
        problems.append(f"{prefix}outcome {outcome!r} is not one of {', '.join(OUTCOMES)}")
    for t in types:
        if t not in DISCREPANCY_TYPES:
            problems.append(f"{prefix}types: {t!r} is not one of {', '.join(DISCREPANCY_TYPES)}")
    if action not in NEXT_ACTIONS:
        problems.append(
            f"{prefix}next_action {action!r} is not one of the decision:action_type labels"
        )
    return Verdict(outcome, tuple(sorted(set(types))), action)


def _row(line: int, values: dict[str, str], problems: list[str]) -> ReferenceRow:
    arm, case, excluded = values["arm"], values["case"], values["excluded"]
    number = identifiers.normalize(values["invoice"])
    invoice = (number, identifiers.normalize(values["supplier"])) if number else None
    if arm not in ARMS:
        problems.append(f"arm {arm!r} is not opsbridge or manual")
    if arm == "opsbridge":
        if case:
            problems.append(
                "case must be empty for opsbridge: its number is assigned at intake, after the "
                "decision, so the row names the invoice"
            )
        if invoice is None:
            problems.append("invoice is required for opsbridge")
        problems.extend(
            f"{c} must be empty for opsbridge: its decisions and times come from its records"
            for c in MANUAL_ONLY
            if values[c]
        )
    if arm == "manual":
        if not case:
            problems.append("case is required for manual")
        problems.extend(
            f"{c} must be empty for manual: its case is a pseudonymous reference"
            for c in ("invoice", "supplier")
            if values[c]
        )
    reviewed_at = reference = process = started = decided = resolved = None
    if not excluded:
        if not values["reviewed_at"]:
            problems.append("reviewed_at is required")
        reviewed_at = _time(values, "reviewed_at", problems)
        reference = _verdict(values, "", problems)
    if arm == "manual" and not excluded:
        if values["process_outcome"] or values["process_next_action"]:
            process = _verdict(values, "process_", problems)
        if not values["started_at"]:
            problems.append("started_at is required for manual")
        started = _time(values, "started_at", problems)
        decided = _time(values, "decided_at", problems)
        resolved = _time(values, "resolved_at", problems)
        for name, t in (("decided_at", decided), ("resolved_at", resolved)):
            if started is not None and t is not None and t < started:
                problems.append(f"{name} is before started_at")
    return ReferenceRow(
        line,
        arm,
        case,
        invoice if arm == "opsbridge" else None,
        reviewed_at,
        reference,
        process,
        started,
        decided,
        resolved,
        excluded,
    )


def _identity(row: ReferenceRow) -> tuple[tuple[str, ...], str]:
    """What a row may not share with another, and how a repeat is named."""
    if row.arm == "opsbridge" and row.invoice is not None:
        number, supplier = row.invoice
        named = f"opsbridge invoice {number!r}" + (f" from {supplier!r}" if supplier else "")
        return (row.arm, number, supplier or ""), named
    return (row.arm, row.case), f"{row.arm} case {row.case!r}"


def read_reference(path: Path) -> list[ReferenceRow]:
    """Every row of the file, or ReferenceError listing every problem in it."""
    problems: list[str] = []
    rows: list[ReferenceRow] = []
    seen: dict[tuple[str, ...], int] = {}
    # utf-8-sig: a spreadsheet may save the file with a byte-order mark.
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = tuple(next(reader, ()))
        if header != COLUMNS:
            raise ReferenceError([f"line 1: columns must be {','.join(COLUMNS)}"])
        for fields in reader:
            line = reader.line_num
            if not any(field.strip() for field in fields):
                continue
            if len(fields) != len(COLUMNS):
                problems.append(f"line {line}: {len(fields)} fields, expected {len(COLUMNS)}")
                continue
            found: list[str] = []
            row = _row(line, dict(zip(COLUMNS, (v.strip() for v in fields), strict=True)), found)
            key, named = _identity(row)
            if key in seen:
                found.append(f"{named} repeats line {seen[key]}")
            seen.setdefault(key, line)
            problems.extend(f"line {line}: {p}" for p in found)
            rows.append(row)
    if problems:
        raise ReferenceError(problems)
    return rows
