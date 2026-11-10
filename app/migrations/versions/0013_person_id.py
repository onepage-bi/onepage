"""oneviewid -> person_id in every table

`oneviewid` is a legacy name from an early directory format, not a concept of
this platform: to a reader it says nothing. The cross-cutting person
identifier was renamed to a neutral one.

There are five tables, and the fifth is a trap. `users`, `employees`, `sales_scopes` and
`user_scopes` are declared in the models, while `sessions` is created by RAW SQL at startup
(`app/auth/session.py`, `CREATE TABLE IF NOT EXISTS`) and Alembic does not know about it
at all. On an empty database the new code creates it with the new name, but on a running one
it would keep the old one — and the very first `SELECT s.person_id` would fail on the sign-in
of any user. Hence the migration goes by FACT: it takes the table list from
the database and renames the column wherever it exists.

Index names are renamed separately. `batch_alter_table` recreates
indexes under their FORMER names, so after the column rename we would be left
with `ix_employees_oneviewid` pointing at `person_id`: it works, but it lies — and
the next migration looking an index up by the model name would not find it.

No data changes in any row: only names move.

Revision ID: 0013
Revises: 0012
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# `sessions` is in this list on purpose: see the docstring.
TABLES = ("users", "employees", "sales_scopes", "user_scopes", "sessions")
INDEX_RENAMES = {
    "ix_employees_oneviewid": ("ix_employees_person_id", "employees"),
    "ix_sales_scopes_oneviewid": ("ix_sales_scopes_person_id", "sales_scopes"),
}


def _tables() -> set[str]:
    # The schema is asked of SQLite directly, not through `sa.inspect`. The inspector
    # is cached PER CONNECTION, while `batch_alter_table` rebuilds the table
    # whole — after the very first rename the inspector returned the old
    # picture, and the index was created on a column that in its view
    # did not exist yet (or any more): "no such column: person_id" out of nowhere.
    rows = op.get_bind().exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _columns(table: str) -> set[str]:
    rows = op.get_bind().exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()
    return {r[1] for r in rows}


def _indexes(table: str) -> set[str]:
    rows = op.get_bind().exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:t",
    ).fetchall() if False else op.get_bind().exec_driver_sql(
        f"SELECT name FROM sqlite_master WHERE type='index' "
        f"AND tbl_name='{table}'").fetchall()
    return {r[0] for r in rows}


def _rename_column(table: str, old: str, new: str) -> None:
    if table not in _tables():
        return
    cols = _columns(table)
    if new in cols or old not in cols:
        return  # already renamed, or this column was never here
    with op.batch_alter_table(table) as batch:
        batch.alter_column(old, new_column_name=new)


def _rename_indexes(mapping: dict[str, tuple[str, str]], column: str) -> None:
    tables = _tables()
    for old_name, (new_name, table) in mapping.items():
        if table not in tables:
            continue
        if column not in _columns(table):
            continue  # column not renamed yet — nothing to rename
        names = _indexes(table)
        if old_name not in names or new_name in names:
            continue
        op.drop_index(old_name, table_name=table)
        op.create_index(new_name, table, [column], unique=False)


def upgrade() -> None:
    for table in TABLES:
        _rename_column(table, "oneviewid", "person_id")
    _rename_indexes(INDEX_RENAMES, "person_id")


def downgrade() -> None:
    # The order is the reverse of upgrade and must be exactly this: an index is created ON
    # a column, so the column name is restored first, and only then the index
    # name. The other way round gives "no such column: oneviewid" on the very first downgrade.
    for table in TABLES:
        _rename_column(table, "person_id", "oneviewid")
    back = {new: (old, tbl) for old, (new, tbl) in INDEX_RENAMES.items()}
    _rename_indexes(back, "oneviewid")
