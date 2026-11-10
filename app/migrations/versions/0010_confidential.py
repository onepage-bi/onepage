"""confidential — a second lock on a report: a password on top of the access list

Two things. `dashboards.conf_hash` — nullable String(256) without DEFAULT (on
SQLite a plain ALTER TABLE ADD COLUMN, the table is not rebuilt), and a new
table `dashboard_unlocks` with locks opened in specific sessions.

Why opened locks are a table and not an in-memory dict: with `OP_WORKERS > 1`
a person would get "open" and "enter the password" on alternate requests. Rows live
for minutes and are cleaned up by expiry on read — the platform has no background sweeper
by construction (the same discipline as sessions).

Revision ID: 0010
Revises: 0009
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "dashboards" in tables and "conf_hash" not in _columns("dashboards"):
        op.add_column("dashboards", sa.Column("conf_hash", sa.String(256), nullable=True))

    if "dashboard_unlocks" not in tables:
        op.create_table(
            "dashboard_unlocks",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.String(64), nullable=False),
            sa.Column("dashboard_id", sa.String(36), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("conf_fp", sa.String(16), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["dashboard_id"], ["dashboards.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("session_id", "dashboard_id", name="uq_unlock_session_dash"),
        )
        op.create_index("ix_dashboard_unlocks_session_id", "dashboard_unlocks", ["session_id"])
        op.create_index("ix_dashboard_unlocks_dashboard_id", "dashboard_unlocks", ["dashboard_id"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    if "dashboard_unlocks" in tables:
        op.drop_index("ix_dashboard_unlocks_dashboard_id", table_name="dashboard_unlocks")
        op.drop_index("ix_dashboard_unlocks_session_id", table_name="dashboard_unlocks")
        op.drop_table("dashboard_unlocks")
    if "dashboards" in tables and "conf_hash" in _columns("dashboards"):
        op.drop_column("dashboards", "conf_hash")
