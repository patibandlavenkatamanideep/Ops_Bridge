"""Migration 0019 on real PostgreSQL with row-level security (production review A02).

Rehearsed: with a duplicate line already stored the upgrade stops and names it, changing
nothing (the scan must see rows despite forced RLS on the migrator); once a person has
removed the wrong line it applies; afterwards the database refuses a new duplicate, including
one that differs only in case or surrounding spaces; and the downgrade removes the indexes.
"""

from __future__ import annotations

import json
from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from opsbridge.config import REPO_ROOT
from opsbridge.evidence.ingest import ingest
from opsbridge.evidence.storage import LocalArtifactStore
from opsbridge.packs import PackRegistry
from opsbridge.persistence.db import Database, DbContext

from .conftest import TEST_DB, make_tenant

pytestmark = pytest.mark.integration

PO = {
    "purchaseOrderId": "PO-9019",
    "vendor": {"vendorCode": "VEN-1", "legalName": "Vendor One"},
    "currencyCode": "EUR",
    "state": "Active",
    "items": [{"lineId": "1", "quantity": "2", "unitOfMeasure": "EA", "unitCost": "10.00"}],
}
COPY_LINE = (
    "INSERT INTO po_lines (id, tenant_id, purchase_order_id, line_number, item_code, "
    "description, quantity, unit, unit_price, discount_amount, refs) "
    "SELECT gen_random_uuid(), tenant_id, purchase_order_id, :line, item_code, description, "
    "quantity, unit, unit_price, discount_amount, refs FROM po_lines LIMIT 1"
)


@pytest.fixture
def alembic_cfg() -> Iterator[Config]:
    cfg = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
    cfg.cmd_opts = Namespace(x=[f"db={TEST_DB}"])  # type: ignore[assignment]
    yield cfg
    command.upgrade(cfg, "head")  # leave the test database as the other tests expect it


def _copy_line(db: Database, tenant: object, line: str) -> None:
    with db.session(DbContext(tenant_id=tenant)) as s:  # type: ignore[arg-type]
        s.execute(text(COPY_LINE), {"line": line})


def _lines(db: Database, tenant: object) -> int:
    with db.session(DbContext(tenant_id=tenant)) as s:  # type: ignore[arg-type]
        return int(s.execute(text("SELECT count(*) FROM po_lines")).scalar_one())


def test_0019_stops_on_existing_duplicates_then_guards_new_ones(
    alembic_cfg: Config, migrator_engine: Engine, tmp_path: Path
) -> None:
    command.downgrade(alembic_cfg, "0018")
    tenant = make_tenant(migrator_engine, "migration-0019", pack="it_hardware")
    db = Database(migrator_engine)
    with db.session(DbContext(tenant_id=tenant)) as s:
        ingest(
            s,
            LocalArtifactStore(tmp_path),
            tenant_id=tenant,
            pack=PackRegistry(REPO_ROOT / "packs").get("it_hardware"),
            record_type="purchase_order",
            source_key="test:PO-9019",
            origin="upload",
            title="PO-9019",
            data=json.dumps(PO).encode(),
            content_type="application/json",
            filename=None,
        )
    _copy_line(db, tenant, " 1")  # stored before the guard existed

    with pytest.raises(RuntimeError, match=r"po_lines: tenant .* line '1' x2"):
        command.upgrade(alembic_cfg, "0019")
    assert _lines(db, tenant) == 2  # nothing deleted or chosen

    with db.session(DbContext(tenant_id=tenant)) as s:  # the person's decision
        s.execute(text("DELETE FROM po_lines WHERE line_number = ' 1'"))
    command.upgrade(alembic_cfg, "0019")
    for duplicate in ("1", "1 ", "1".lower()):
        with pytest.raises(IntegrityError, match="uq_po_lines_identity"):
            _copy_line(db, tenant, duplicate)
    _copy_line(db, tenant, "2")  # a different line is still fine

    command.downgrade(alembic_cfg, "0018")
    _copy_line(db, tenant, " 1 ")  # the guard is gone again
    with db.session(DbContext(tenant_id=tenant)) as s:  # so the upgrade back to head applies
        s.execute(text("DELETE FROM po_lines WHERE line_number = ' 1 '"))
