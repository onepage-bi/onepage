"""explore_feedback — 👍/👎 on a recorded answer and the form-suggestion box (P4)

Two things, both for one task, "feedback on an AI answer":

  * `explore_answers.vote` / `voted_at` — two columns on an existing table.
    On SQLite this is a plain ALTER TABLE ADD COLUMN, without rebuilding the table
    (so no batch mode is needed here: the columns are nullable and without
    DEFAULT, and we touch no foreign key);
  * `explore_form_suggestions` — a new table. Its foreign keys are declared
    WITHOUT `ON DELETE`, like the rest of the graph (see app/services/integrity.py):
    the delete policy is stated once in Python and follows from the column's
    nullability rather than being stored in the `CREATE TABLE` text.

There are no query results and no SQL text in the new table by construction: what goes in is
the question, the metric key, the intent and the sha256 of the SQL — the same discipline as
`explore_answers` (a literal in a filter can quote a real value).

The indexes are named exactly as the model declares them — a database built by
`create_all()` and a database that arrived here by migration must be indistinguishable
(tests/smoke_migration.py checks this).

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_TABLE = "explore_form_suggestions"
# (name, columns, kwargs) — exactly what the model declares. The third index
# is partial and unique: deduplication of UNDECIDED suggestions lives in the database,
# not only in a check before insert (see the model for why `WHERE
# status='new'` and not a full UNIQUE).
_INDEXES = (
    ("ix_explore_form_suggestions_generator_status", ["generator_id", "status"], {}),
    ("ix_explore_form_suggestions_user_created", ["user_id", "created_at"], {}),
    ("uq_explore_form_suggestions_open", ["generator_id", "question_hash", "kind"],
     {"unique": True, "sqlite_where": sa.text("status='new'")}),
)


def _insp():
    return sa.inspect(op.get_bind())


def _tables() -> set[str]:
    return set(_insp().get_table_names())


def _columns(table: str) -> set[str]:
    return {c["name"] for c in _insp().get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in _insp().get_indexes(table)}


def upgrade() -> None:
    # The same guard as in 0003/0004: a database built by create_all() from the current
    # models already has all of this and is stamped, not upgraded — but the check
    # costs one PRAGMA and keeps the revision reproducible.
    tables = _tables()
    if "explore_answers" in tables:
        cols = _columns("explore_answers")
        if "vote" not in cols:
            op.add_column("explore_answers", sa.Column("vote", sa.String(8), nullable=True))
        if "voted_at" not in cols:
            op.add_column("explore_answers", sa.Column("voted_at", sa.DateTime(), nullable=True))

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("generator_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("answer_id", sa.Integer(), nullable=True),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column("question_hash", sa.String(64), nullable=False),
            sa.Column("kpi_key", sa.String(64), nullable=True),
            sa.Column("intent_json", sa.Text(), nullable=True),
            sa.Column("sql_hash", sa.String(64), nullable=True),
            sa.Column("kind", sa.String(1), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            # NOT NULL — as in the model (`Mapped[datetime]` without Optional):
            # create_all() and this revision must produce the same table,
            # tests/smoke_migration.py checks this via PRAGMA table_info.
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("decided_at", sa.DateTime(), nullable=True),
            sa.Column("decided_by", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["generator_id"], ["generators.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["answer_id"], ["explore_answers.id"]),
            sa.ForeignKeyConstraint(["decided_by"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    have = _indexes(_TABLE)
    for name, cols, kw in _INDEXES:
        if name not in have:
            op.create_index(name, _TABLE, cols, **kw)


def downgrade() -> None:
    tables = _tables()
    if _TABLE in tables:
        have = _indexes(_TABLE)
        for name, _cols, _kw in _INDEXES:
            if name in have:
                op.drop_index(name, table_name=_TABLE)
        op.drop_table(_TABLE)
    if "explore_answers" in tables:
        cols = _columns("explore_answers")
        for name in ("voted_at", "vote"):
            if name in cols:
                op.drop_column("explore_answers", name)
