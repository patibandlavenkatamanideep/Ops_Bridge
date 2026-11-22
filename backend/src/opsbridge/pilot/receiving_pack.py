"""Synthetic receiving and disposition scenarios (fixtures/receiving/disposition.yaml).

Each case is a purchasing scenario in the frozen scenario format (simulator/scenarios.py),
which renders the order, invoice, allocations and messages, plus what that format cannot hold:
receiving records whose rejected lines give a reason, and receiving events (late rejections,
returns, replacements, credits). The scenario format stays untouched because the frozen
benchmarks bind its renderer by digest.

Records render in the furniture organization's native formats (packs/furniture/pack.yaml):
one CSV per receiving record, one CSV per batch of events, each filed under its PO. The same
records seed the simulator or are written as an `opsbridge-export/1` export. Labels are
written by hand from policy furniture-ap@v2, the demo tenant's policy, which names no owner
for rejected goods. This is regression evidence for these rules on synthetic data, not a
benchmark and not a measure of accuracy on anyone's records.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from opsbridge.config import REPO_ROOT
from opsbridge.simulator.scenarios import Record, Scenario, render
from opsbridge.simulator.store import SimStore

PACK = REPO_ROOT / "fixtures" / "receiving" / "disposition.yaml"
ORG = "demo-furniture"
SOURCE_SYSTEM = "OpsBridge synthetic receiving and disposition pack (not a customer export)"
RECEIPT_COLUMNS = [
    "receipt_id",
    "po_ref",
    "po_line",
    "received_on",
    "qty",
    "uom",
    "movement",
    "status",
    "reason",
]
EVENT_COLUMNS = [
    "event_id",
    "event",
    "po_ref",
    "po_line",
    "qty",
    "uom",
    "occurred_on",
    "receipt_id",
    "follows",
    "invoice",
    "document",
    "reason",
]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReceivingLine(_M):
    line: int
    qty: Decimal
    unit: str = "EA"
    movement: Literal["RECEIPT", "RETURN"] = "RECEIPT"
    status: Literal["ACCEPTED", "REJECTED", "REVERSED"] = "ACCEPTED"
    reason: str | None = None


class Receiving(_M):
    id: str
    date: date
    lines: list[ReceivingLine]


class Event(_M):
    id: str
    event: Literal[
        "REJECTED",
        "RETURNED",
        "REPLACEMENT_PROMISED",
        "REPLACEMENT_DELIVERED",
        "REPLACEMENT_ACCEPTED",
        "CREDIT_PROMISED",
        "CREDIT_POSTED",
    ]
    line: int = 1
    qty: Decimal | None = None
    unit: str | None = "EA"
    occurred: str | None = None
    receipt: str | None = None
    follows: str | None = None
    invoice: str | None = None
    document: str | None = None
    reason: str | None = None
    file: str | None = None  # the record key the event is filed in; default <PO>-EVT
    # Recorded after the case was approved: the demo writes it only when asked
    # (`receiving-pack --seed-simulator --late-events`); exports and tests always hold it.
    after_approval: bool = False


class Expected(_M):
    outcome: Literal["matched", "discrepancies_found", "needs_information", "manual_review"]
    next_action: str
    owner: str | None
    codes: list[str]  # every finding's reason code, notes included, sorted
    note: str = ""


class Case(_M):
    id: str
    description: str
    scenario: Scenario
    receipts: list[Receiving] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    expected: Expected

    @model_validator(mode="after")
    def _receipts_here(self) -> Case:
        if self.scenario.receipts:
            raise ValueError(f"{self.id}: give receipts under the case, with their reasons")
        return self

    @property
    def po_number(self) -> str:
        po = self.scenario.po.number if self.scenario.po else self.scenario.invoice.po_number
        assert po is not None
        return po


class Pack(_M):
    schema_version: Literal[1] = 1
    corpus_id: str
    cases: list[Case]


def load_pack(path: Path = PACK) -> Pack:
    pack = Pack.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    ids = [c.id for c in pack.cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    return pack


def _csv(header: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode()


def _s(value: object) -> str:
    if value is None:
        return ""
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def receipt_record(po: str, r: Receiving) -> Record:
    rows = [
        [r.id, po, str(ln.line), r.date.isoformat(), _s(ln.qty), ln.unit, ln.movement, ln.status,
         _s(ln.reason)]
        for ln in r.lines
    ]  # fmt: skip
    return ("receipts", r.id, _csv(RECEIPT_COLUMNS, rows), "text/csv", po)


def event_records(po: str, events: list[Event]) -> list[Record]:
    """One CSV per file key, its events in the order given."""
    files: dict[str, list[list[str]]] = {}
    for ev in events:
        files.setdefault(ev.file or f"{po}-EVT", []).append(
            [ev.id, ev.event, po, str(ev.line), _s(ev.qty), _s(ev.unit), _s(ev.occurred),
             _s(ev.receipt), _s(ev.follows), _s(ev.invoice), _s(ev.document), _s(ev.reason)]
        )  # fmt: skip
    return [
        ("receiving_events", key, _csv(EVENT_COLUMNS, rows), "text/csv", po)
        for key, rows in files.items()
    ]


def render_case(c: Case) -> list[Record]:
    out = [r for r in render(c.scenario) if r[0] != "receipts"]
    out += [receipt_record(c.po_number, r) for r in c.receipts]
    out += event_records(c.po_number, c.events)
    return out


def write_receiving_export(pack: Pack, out: Path, extracted_at: datetime) -> Path:
    """Write the pack as an `opsbridge-export/1` export under `out`/demo-furniture."""
    from .rehearsal import write_export

    records = [r for c in pack.cases for r in render_case(c)]
    return write_export(records, out / ORG, SOURCE_SYSTEM, extracted_at)


def seed_simulator(store: SimStore, pack: Pack, late: bool = False) -> int:
    """Put the pack in the simulator without the events recorded after approval, or (`late`)
    only those events, as the purchasing system would hold them later."""
    count = 0
    for c in pack.cases:
        if late:
            records = event_records(c.po_number, [ev for ev in c.events if ev.after_approval])
        else:
            early = c.model_copy(
                update={"events": [ev for ev in c.events if not ev.after_approval]}
            )
            records = render_case(early)
        for collection, key, body, ctype, po in records:
            store.put_record(ORG, collection, key, body, ctype, po)
            count += 1
    return count
