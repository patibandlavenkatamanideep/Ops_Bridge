"""The pilot's enrolment list, frozen before any outcome exists (protocol opsbridge-pilot/2).

Protocol step 1 fixes the sample before any case is opened: the blocked invoices of an agreed
period, up to an agreed number, each assigned to an arm. Under opsbridge-pilot/1 nothing held
that sample: the measures took their denominators from the cases opened and the reference
rows written, so an enrolled invoice never opened, or a row never written, left no trace in
them (review N05). The cohort file is that sample, written once, in the order the invoices
were blocked:

    arm,case,invoice,supplier

with the same rules as the reference file: an `opsbridge` row names its invoice as intake
will (number and supplier id, `case` empty); a `manual` row names its pseudonymous reference
(`invoice` and `supplier` empty). Its SHA-256 is recorded when it is frozen and appears in
every report, so a report shows which sample it measured. The file is read strictly.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

from opsbridge.domain import identifiers

COLUMNS = ("arm", "case", "invoice", "supplier")
ARMS = ("opsbridge", "manual")

Key = tuple[str, ...]


def key_of(arm: str, case: str, invoice: tuple[str, str | None] | None) -> Key:
    """How a cohort member and its reference row are matched."""
    if arm == "opsbridge" and invoice is not None:
        return (arm, invoice[0], invoice[1] or "")
    return (arm, case)


class CohortError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{len(problems)} problem(s) in the cohort file")
        self.problems = problems


@dataclass(frozen=True)
class Member:
    line: int
    arm: str
    case: str  # manual: the pseudonymous reference; "" for opsbridge
    invoice: tuple[str, str | None] | None  # opsbridge: (number, supplier), normalized as intake

    @property
    def key(self) -> Key:
        return key_of(self.arm, self.case, self.invoice)


@dataclass(frozen=True)
class Cohort:
    members: tuple[Member, ...]
    sha256: str  # of the file's bytes, as frozen


def read_cohort(path: Path) -> Cohort:
    """Every member, or CohortError listing every problem in the file."""
    data = path.read_bytes()
    problems: list[str] = []
    members: list[Member] = []
    seen: dict[Key, int] = {}
    reader = csv.reader(data.decode("utf-8-sig").splitlines())
    header = tuple(next(reader, ()))
    if header != COLUMNS:
        raise CohortError([f"line 1: columns must be {','.join(COLUMNS)}"])
    for fields in reader:
        line = reader.line_num
        if not any(f.strip() for f in fields):
            continue
        if len(fields) != len(COLUMNS):
            problems.append(f"line {line}: {len(fields)} fields, expected {len(COLUMNS)}")
            continue
        arm, case, given, supplier = (f.strip() for f in fields)
        number = identifiers.normalize(given)
        invoice = (number, identifiers.normalize(supplier)) if number else None
        found: list[str] = []
        if arm == "opsbridge":
            if case:
                found.append("case must be empty for opsbridge: the row names the invoice")
            if invoice is None:
                found.append("invoice is required for opsbridge")
        elif arm == "manual":
            if not case:
                found.append("case is required for manual")
            if given or supplier:
                found.append("invoice and supplier must be empty for manual: it is pseudonymous")
            invoice = None
        else:
            found.append(f"arm {arm!r} is not opsbridge or manual")
        member = Member(line, arm, case if arm == "manual" else "", invoice)
        if not found and member.key in seen:
            found.append(f"repeats line {seen[member.key]}")
        seen.setdefault(member.key, line)
        problems.extend(f"line {line}: {p}" for p in found)
        members.append(member)
    if not members and not problems:
        problems.append("the cohort has no members")
    if problems:
        raise CohortError(problems)
    return Cohort(tuple(members), hashlib.sha256(data).hexdigest())
