"""Migration 0025 on real PostgreSQL: receiving events and rejection reasons.

Rehearsed on a database holding a receipt with a rejection reason and a late rejection. The
upgrade adds the column and the table and infers nothing: existing receipt lines keep their
status, gain no reason, and no event is created. The downgrade drops the events and reasons
and withdraws the documents they came from, so nothing is concluded from a record that lost
part of what it said; once upgraded again, the next read stores them anew from the source.
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
from opsbridge.persistence.db import Database, DbContext

from .conftest import TEST_DB
from .test_receiving_disposition import PACK, codes, opened, tenant_id
from .test_receiving_disposition import investigate as rerun

pytestmark = pytest.mark.integration


@pytest.fixture
def alembic_cfg() -> Iterator[Config]:
    cfg = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
    cfg.cmd_opts = Namespace(x=[f"db={TEST_DB}"])  # type: ignore[assignment]
    yield cfg
    command.upgrade(cfg, "head")  # leave the test database as the other tests expect it


def _q(engine: Engine, sql: str, tid: Any = None) -> list[Any]:
    with Database(engine).session(DbContext(tenant_id=tid)) as s:
        return list(s.execute(text(sql)).all())


def _has(engine: Engine, table: str, column: str | None = None) -> bool:
    where = f"table_name = '{table}'" + (f" AND column_name = '{column}'" if column else "")
    return bool(_q(engine, f"SELECT 1 FROM information_schema.columns WHERE {where}"))


def test_0025_infers_nothing_on_the_way_up_and_withdraws_on_the_way_down(
    stack: dict[str, Any], alembic_cfg: Config, migrator_engine: Engine
) -> None:
    st = stack
    b_id, b = opened(st, PACK["rcv-b-rejected-damaged"])
    f_id, f = opened(st, PACK["rcv-f-late-rejection"])
    assert "disposition_unrecorded" in codes(b) and "disposition_unrecorded" in codes(f)
    tid = tenant_id(migrator_engine)
    statuses = "SELECT id, status, quantity FROM receipt_lines ORDER BY id"
    before = _q(migrator_engine, statuses, tid)

    command.downgrade(alembic_cfg, "0024")
    assert not _has(migrator_engine, "receipt_lines", "reason")
    assert not _has(migrator_engine, "receiving_events")
    assert _q(migrator_engine, statuses, tid) == before  # no status changed
    withdrawn = _q(
        migrator_engine,
        "SELECT source_key FROM source_documents WHERE withdrawn_at IS NOT NULL ORDER BY 1",
        tid,
    )
    assert [k for (k,) in withdrawn] == [
        "purchasing:receipts:GR-6602",
        "purchasing:receiving_events:PO-4609-EVT-1",
    ]

    command.upgrade(alembic_cfg, "0025")
    assert _has(migrator_engine, "receipt_lines", "reason")
    assert _q(migrator_engine, "SELECT count(*) FROM receiving_events", tid) == [(0,)]
    assert _q(
        migrator_engine, "SELECT count(*) FROM receipt_lines WHERE reason IS NOT NULL", tid
    ) == [(0,)]
    assert _q(migrator_engine, statuses, tid) == before

    # Read again from the source, the records are whole again: nothing was guessed meanwhile.
    b_again, f_again = rerun(st, b_id), rerun(st, f_id)
    for iv in (b_again, f_again):
        [rejected] = [x for x in iv["findings"] if x["reason_code"] == "disposition_unrecorded"]
        assert rejected["values"]["rejection_reasons"] == "damaged in transit"
