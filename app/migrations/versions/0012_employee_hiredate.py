"""employees.batjoindate -> employees.hiredate

The column carried a legacy name from an early HR export format — it was
renamed to a neutral one.

Why this is a migration and not a model edit. On prod the table holds the old name;
renaming the field in `app/models.py` without touching the database means getting
`no such column` on the very first directory read. No data changes
in any row — only the name moves.

The COLUMN HEADER IN THE EXPORT is a separate matter and is not settled here:
`COLUMN_MAP` now expects the neutral `hiredate`, and the HR system's real header
is declared by the site through the `OP_EMPLOYEE_COLUMN_ALIASES` setting
(`batjoindate=hiredate`). Without it the import would not fail but would silently stop
filling the field — exactly the kind of failure nobody notices.

Revision ID: 0012
Revises: 0011
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "employees" not in tables:
        return  # fresh database: create_all() already made it with the new name
    cols = _columns("employees")
    if "hiredate" in cols or "batjoindate" not in cols:
        return  # already renamed, or never had the old name
    # batch_alter_table: SQLite has no ALTER COLUMN RENAME before 3.25 and
    # rebuilds the table anyway; Alembic's batch mode handles both paths.
    with op.batch_alter_table("employees") as batch:
        batch.alter_column("batjoindate", new_column_name="hiredate")


def downgrade() -> None:
    cols = _columns("employees")
    if "batjoindate" in cols or "hiredate" not in cols:
        return
    with op.batch_alter_table("employees") as batch:
        batch.alter_column("hiredate", new_column_name="batjoindate")
