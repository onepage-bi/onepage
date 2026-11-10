"""quiz_i18n — translations of a bank question, next to the Russian source

One nullable TEXT column on `quiz_questions`. It holds
`{"en": {"t","q","o","e"}, "zh-HK": {…}}` — the block the bank file carries per
question — or NULL for a question nobody has translated. The Russian columns
(`topic`, `question`, `options_json`, `explanation`) remain the source and the
fallback, and `answer_idx` is deliberately NOT duplicated: a translation
replaces the four option strings in place and never reorders them, so the index
of the right answer is the same in every language.

A JSON blob rather than a `quiz_question_translations` table: the whole thing is
read only as part of the question it belongs to, never queried across rows, and
it lives and dies with a bank import that rewrites the question anyway. A join
would buy nothing and cost a query on the quiz's hot path.

The boot-time DDL block moved into 0002 and is not repeated here; `op.add_column`
on SQLite is a plain ALTER TABLE ADD COLUMN, no table rewrite.

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _columns() -> set[str] | None:
    """The columns of `quiz_questions`, or None if the table is not there.

    Same shape of guard 0002 uses: a database that predates the quests feature
    and was never rebuilt by create_all() has no such table, and there is
    nothing to add a column to.
    """
    insp = sa.inspect(op.get_bind())
    if "quiz_questions" not in set(insp.get_table_names()):
        return None
    return {c["name"] for c in insp.get_columns("quiz_questions")}


def upgrade() -> None:
    # Same guard as 0003 on top of that: a database create_all() built from the
    # current models already has the column and is stamped rather than
    # upgraded, but the check costs one PRAGMA and keeps the revision
    # replayable.
    cols = _columns()
    if cols is not None and "i18n_json" not in cols:
        op.add_column("quiz_questions", sa.Column("i18n_json", sa.Text(), nullable=True))


def downgrade() -> None:
    # Dropping it loses the translations; the next bank import restores them
    # from app/services/data/quiz_bank.json.
    cols = _columns()
    if cols is not None and "i18n_json" in cols:
        op.drop_column("quiz_questions", "i18n_json")
