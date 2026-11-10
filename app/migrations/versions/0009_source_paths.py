"""source_paths — file and root paths sent by the desktop application

Three nullable columns without DEFAULT and without foreign keys: `source_files.path`,
`data_sources.root_path`, `data_sources.root_id`. On SQLite this is a plain
ALTER TABLE ADD COLUMN, the tables are not rebuilt, and a database built by
`create_all()` from the current models is indistinguishable from one that arrived here by migration
(tests/smoke_migration.py compares PRAGMA table_info).

No indexes on purpose: nobody searches or joins on these columns, they are
only shown next to a row that has already been found by another key.
An index on a String(512) filled in only for some rows would charge every
SourceFile insert for a query that does not exist.

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("source_files", "path", 512),
    ("data_sources", "root_path", 512),
    ("data_sources", "root_id", 32),
)


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table, name, length in _COLUMNS:
        # A fresh file is built by create_all() and stamped at head — but the check
        # costs one PRAGMA and makes the revision repeatable.
        if table in tables and name not in _columns(table):
            op.add_column(table, sa.Column(name, sa.String(length), nullable=True))


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table, name, _length in reversed(_COLUMNS):
        if table in tables and name in _columns(table):
            op.drop_column(table, name)
