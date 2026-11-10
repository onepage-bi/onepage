"""learn_progress — completed lessons of "Working on the platform"

A new table; touches nothing existing: one row per (person,
lesson), appearing at the moment of completion. UNIQUE(user_id, lesson_id) —
idempotency under a race between tabs; the user_id index — the course state
is read in exactly one query per page open.

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_TABLE = "learn_progress"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("lesson_id", sa.String(32), nullable=False),
        # No server_default and the same nullability as in the models:
        # smoke_migration compares PRAGMA table_info byte for byte, and a database
        # built by create_all() must be indistinguishable from one that arrived here
        # by migration. Defaults are set by Python (models.py).
        sa.Column("xp", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "lesson_id", name="uq_learn_progress_user_lesson"),
    )
    op.create_index("ix_learn_progress_user_id", _TABLE, ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_learn_progress_user_id", table_name=_TABLE)
    op.drop_table(_TABLE)
