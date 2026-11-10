"""quest_companion — three quest-profile columns for the "Companion" (ADR-17b)

`quest_profiles.companion_since` / `companion_off` / `autumn_theme` — all three
nullable, no DEFAULT and no foreign keys: on SQLite this is a plain ALTER TABLE
ADD COLUMN, the table is not rebuilt, and a database built by `create_all()` from
the current models is indistinguishable from one that arrived here by migration
(tests/smoke_migration.py compares PRAGMA table_info).

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_TABLE = "quest_profiles"
_COLUMNS = (
    ("companion_since", sa.DateTime()),
    ("companion_off", sa.Boolean()),
    ("autumn_theme", sa.Boolean()),
)


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if _TABLE not in set(sa.inspect(op.get_bind()).get_table_names()):
        return  # a fresh file is built by create_all() and stamped, not upgraded
    have = _columns(_TABLE)
    for name, typ in _COLUMNS:
        if name not in have:
            op.add_column(_TABLE, sa.Column(name, typ, nullable=True))


def downgrade() -> None:
    if _TABLE not in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    have = _columns(_TABLE)
    for name, _typ in reversed(_COLUMNS):
        if name in have:
            op.drop_column(_TABLE, name)
