"""One line per identity on a purchase order or invoice.

Two PO lines named "1" made the matcher keep whichever it read last, so the order of the
input chose which price was compared (production review A02). The pack mapping and the
matcher now refuse such records; these indexes keep the database from holding them.
Identity is compared the way the matcher compares it: trimmed and upper-cased.

Existing duplicates are neither deleted nor chosen between. The upgrade lists them and
stops, so a person decides which record is right (correct it at the source and let the next
read supersede it) before running the upgrade again. The scan sets each tenant's context in
turn: row-level security binds the migrator too, and a scan without it would see no rows.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IDENTITY = "upper(btrim(line_number))"
TABLES = (
    ("po_lines", "purchase_order_id", "uq_po_lines_identity"),
    ("invoice_lines", "invoice_id", "uq_invoice_lines_identity"),
)


def _duplicates() -> list[str]:
    bind = op.get_bind()
    found: list[str] = []
    for (tenant_id,) in bind.execute(sa.text("SELECT id FROM tenants ORDER BY id")).all():
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        for table, parent, _name in TABLES:
            rows = bind.execute(
                sa.text(
                    f"SELECT {parent} AS parent, {IDENTITY} AS line, count(*) AS n "  # noqa: S608
                    f"FROM {table} GROUP BY 1, 2 HAVING count(*) > 1 ORDER BY 1, 2"  # fixed names
                )
            ).all()
            found += [
                f"{table}: tenant {tenant_id}, parent {r.parent}, line {r.line!r} x{r.n}"
                for r in rows
            ]
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    return found


def upgrade() -> None:
    found = _duplicates()
    if found:
        raise RuntimeError(
            "0019 stopped: these lines share an identity, so which one is right must be decided "
            "first (nothing was changed):\n" + "\n".join(found[:50])
        )
    for table, parent, name in TABLES:
        op.create_index(name, table, ["tenant_id", parent, sa.text(IDENTITY)], unique=True)


def downgrade() -> None:
    for table, _parent, name in TABLES:
        op.drop_index(name, table_name=table)
