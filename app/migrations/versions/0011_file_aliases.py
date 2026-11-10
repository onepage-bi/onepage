"""file_aliases — "this very file is that declared input"

One nullable Text column on `input_overrides`. On SQLite this is a plain
ALTER TABLE ADD COLUMN, the table is not rebuilt.

Why `column_aliases` was not reused: file names could have been put there
under a reserved sheet name (`schemadiff` would not notice such an entry),
but a column about columns stuffed with file names is a trap
for whoever comes here six months later.

Revision ID: 0011
Revises: 0010
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "input_overrides" in tables and "file_aliases" not in _columns("input_overrides"):
        op.add_column("input_overrides", sa.Column("file_aliases", sa.Text(), nullable=True))


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "input_overrides" in tables and "file_aliases" in _columns("input_overrides"):
        op.drop_column("input_overrides", "file_aliases")
