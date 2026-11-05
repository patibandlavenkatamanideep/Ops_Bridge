"""Migration 0023 on real PostgreSQL: the status a field had before each correction (review N05).

Rehearsed on a database holding a correction: the downgrade drops only the column and keeps
the correction; the upgrade adds it back empty, as on an install upgraded with corrections
already made; and a pilot report of a cut-off before such a correction counts the field as
of unrecorded status instead of guessing what it was.
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from opsbridge.config import REPO_ROOT
from opsbridge.persistence.db import Database
from opsbridge.pilot.collect import collect

from .conftest import TEST_DB
from .stack import investigate, open_case
from .test_api_cases import call
from .test_approvals_execution import ORG, T
from .test_pilot_report import _invoice_quantity, case_number, db_now, tenant_id

pytestmark = pytest.mark.integration


@pytest.fixture
def alembic_cfg() -> Iterator[Config]:
    cfg = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
    cfg.cmd_opts = Namespace(x=[f"db={TEST_DB}"])  # type: ignore[assignment]
    yield cfg
    command.upgrade(cfg, "head")  # leave the test database as the other tests expect it


def _column(engine: Engine) -> bool:
    with engine.begin() as conn:
        return bool(
            conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns WHERE table_name = "
                    "'field_corrections' AND column_name = 'previous_status'"
                )
            ).first()
        )


def _corrections(engine: Engine) -> list[Any]:
    with engine.begin() as conn:
        return list(conn.execute(text("SELECT id, new_value FROM field_corrections ORDER BY id")))


def test_0023_keeps_every_correction_and_an_unrecorded_status_is_not_guessed(
    stack: dict[str, Any], alembic_cfg: Config, migrator_engine: Engine, worker_db: Database
) -> None:
    st = stack
    since = db_now(migrator_engine)
    case_id = open_case(st, "furn-investigator", ORG, "INV-9001")
    investigate(st, "furn-investigator", ORG, case_id)
    cut = db_now(migrator_engine)
    qty = _invoice_quantity(st, case_id)
    r = call(
        st["client"], st["actors"]["furn-investigator"], "POST",
        f"{T}/cases/{case_id}/fields/{qty['field_id']}/corrections",
        json={"new_value": "18", "reason": "Receiving counted 18"},
    )  # fmt: skip
    assert r.status_code in (200, 201), r.text
    held = _corrections(migrator_engine)

    command.downgrade(alembic_cfg, "0022")
    assert not _column(migrator_engine) and _corrections(migrator_engine) == held
    command.upgrade(alembic_cfg, "0023")
    assert _column(migrator_engine) and _corrections(migrator_engine) == held

    tid = tenant_id(migrator_engine, "demo-furniture")
    [facts] = [
        f for f in collect(worker_db, tid, since=since, until=cut, as_of=cut).cases
        if f.case_number == case_number(st, case_id)
    ]  # fmt: skip
    assert facts.fields["status_not_recorded"] == 1 and facts.field_corrections == 0
