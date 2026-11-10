"""device_tokens — read keys for the desktop program

A new table; touches nothing existing. Needed because a session lives
eight hours, while background mirror sync must work at night, when the
machine has no person at it: a program cannot pass SSO, and prod has no password sign-in.

The key is strictly read-only — `app/auth/deps.py` enforces that by refusing it on
any method except GET and HEAD. Only the sha256 is stored; revocation is `revoked_at`,
not deleting the row, so that a revocation remains a visible fact.

Revision ID: 0008
Revises: 0007
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_TABLE = "device_tokens"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        # No server_default and the same nullability as in the models:
        # smoke_migration compares PRAGMA table_info byte for byte, and a database
        # built by create_all() must be indistinguishable from one that arrived
        # by migration. Defaults are set by Python.
        sa.Column("label", sa.String(64), nullable=False),
        # NOT NULL, as in the model: the annotation there is `datetime`, not `datetime | None`,
        # so SQLAlchemy builds the column as required. The value is set by Python.
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_device_tokens_user_id", _TABLE, ["user_id"])
    op.create_index("ix_device_tokens_token_hash", _TABLE, ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_table(_TABLE)
