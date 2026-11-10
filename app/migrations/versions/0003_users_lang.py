"""users_lang — interface language per account (ru / en / zh-HK)

One nullable column. NULL means "never chosen": the request then falls back to
the `op_lang` cookie, then `Accept-Language`, then ru — see app/i18n.py.

It is read on the hot path, but not by a query of its own: the column joins the
existing session JOIN users SELECT in app/auth/session.py (_AUTH_SQL), so the
per-request SQL count does not change.

`op.add_column` on SQLite is a plain ALTER TABLE ADD COLUMN — no table rewrite.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # A database built by create_all() from the current models already has it
    # (it is declared on User), and that database is stamped, not upgraded —
    # but the guard costs one PRAGMA and keeps the revision replayable.
    if "lang" not in _columns("users"):
        op.add_column("users", sa.Column("lang", sa.String(8), nullable=True))


def downgrade() -> None:
    if "lang" in _columns("users"):
        op.drop_column("users", "lang")
