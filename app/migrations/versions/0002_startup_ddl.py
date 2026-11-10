"""startup_ddl — the ALTER TABLE / CREATE INDEX block that used to live in lifespan

Every statement app/main.py ran on every boot, in the same order, moved here
once. The point of the move is that it now runs ONCE and is recorded, instead
of being re-attempted (and re-swallowed) on every start of every worker.

Idempotent on purpose, and not by catching exceptions: each step asks the
inspector whether the table / column / index is already there. That is what
makes the migration safe to apply to the three shapes of database we actually
have — one that predates all of it, one where the old boot-time ALTERs already
ran, and a fresh one built by create_all() where 0001 already declares
everything (there the whole revision is a no-op).

`op.add_column` on SQLite is a plain ALTER TABLE ADD COLUMN — cheap, no table
rewrite — so this revision does not use batch mode.

Downgrade DROPs those columns and their data. See docs/DEPLOY.md, "Migrations":
take the backup first.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


# (table, column, type, extra kwargs) — the ALTER TABLE list from lifespan,
# in its original order. The comments are the ones that stood next to it.
COLUMNS: list[tuple[str, str, sa.types.TypeEngine, dict]] = [
    ("dashboards", "is_public", sa.Boolean(), {"nullable": False, "server_default": "0"}),
    # 2026-08-28: users.upn (app/services/identity.py)
    ("users", "upn", sa.String(255), {}),
    # folders feature
    ("dashboards", "folder_id", sa.Integer(), {}),
    # render_size: NULLs are backfilled from disk by the app after the upgrade
    # (app/main.py lifespan), not here — it needs the uploads directory.
    ("dashboards", "render_size", sa.Integer(), {}),
    # report generators
    ("dashboards", "generator_id", sa.Integer(), {}),
    # ADR-18 row-level security
    ("dashboards", "rls_json", sa.Text(), {}),
    # Data Lineage v2
    ("generation_runs", "outputs_json", sa.Text(), {}),
    ("data_sources", "version", sa.Integer(), {"nullable": False, "server_default": "1"}),
    ("data_sources", "first_seen_at", sa.DateTime(), {}),
    ("data_sources", "last_seen_at", sa.DateTime(), {}),
    ("data_sources", "fingerprint", sa.String(64), {}),
    ("data_sources", "last_file_count", sa.Integer(), {}),
    # Data Quality: per-source thresholds and per-(generator, source) aliases
    ("data_sources", "stale_after_days", sa.Integer(), {}),
    ("data_sources", "min_files", sa.Integer(), {}),
    ("data_sources", "thresholds_json", sa.Text(), {}),
    ("input_overrides", "column_aliases", sa.Text(), {}),
    # Impact Analysis addendum: source ownership
    ("data_sources", "owner_id", sa.Integer(), {}),
    # ADR-13: semantics.json frozen per generator version
    ("generator_versions", "semantics_json", sa.Text(), {}),
    ("employees", "search_blob", sa.Text(), {}),
    # ADR-17 pending surprises
    ("quest_openings", "revealed_at", sa.DateTime(), {}),
]

# Columns 0001 declares with a REFERENCES clause. They are ADDED without one
# (Alembic emits a foreign key as a separate ADD CONSTRAINT, which SQLite does
# not have; the old boot-time ALTERs could inline it, but the connection runs
# with foreign_keys OFF by design — see app/database.py — so the clause was
# documentation, not enforcement). On the way DOWN the clause is there on any
# database create_all built, and SQLite re-parses the schema after DROP COLUMN
# and rejects the now-dangling reference, so these three need batch mode.
REFERENCING: frozenset[tuple[str, str]] = frozenset({
    ("dashboards", "folder_id"),
    ("dashboards", "generator_id"),
    ("data_sources", "owner_id"),
})

# (name, table, columns, unique) — the CREATE INDEX IF NOT EXISTS list.
# Names match SQLAlchemy's index=True convention (ix_<table>_<column>).
INDEXES: list[tuple[str, str, list[str], bool]] = [
    ("ix_dashboards_owner_id", "dashboards", ["owner_id"], False),
    ("ix_dashboards_is_public", "dashboards", ["is_public"], False),
    ("ix_dashboards_updated_at", "dashboards", ["updated_at"], False),
    ("ix_dashboards_folder_id", "dashboards", ["folder_id"], False),
    ("ix_dashboards_generator_id", "dashboards", ["generator_id"], False),
    ("ix_dashboard_permissions_user_id", "dashboard_permissions", ["user_id"], False),
    ("ix_audit_log_created_at", "audit_log", ["created_at"], False),
    ("ix_users_upn", "users", ["upn"], False),
    # Data Quality
    ("ix_quality_results_source_file_id", "quality_results", ["source_file_id"], False),
    ("ix_quality_results_observation_id", "quality_results", ["observation_id"], False),
    ("ix_value_baselines_data_source_id", "value_baselines", ["data_source_id"], False),
    ("ix_value_baselines_generator_id", "value_baselines", ["generator_id"], False),
    ("ix_quality_acks_finding_id", "quality_acks", ["finding_id"], False),
    # Annotations (P3.6)
    ("ix_annotations_data_source_id", "annotations", ["data_source_id"], False),
    # Stages (P4.1)
    ("ix_run_stages_run_id", "run_stages", ["run_id"], False),
    # Catalogue (Leg 1)
    ("ix_catalog_tables_table_id", "catalog_tables", ["table_id"], True),
    ("ix_catalog_tables_data_source_id", "catalog_tables", ["data_source_id"], False),
    ("ix_catalog_partitions_table_id", "catalog_partitions", ["table_id"], False),
    ("ix_catalog_consumers_table_id", "catalog_consumers", ["table_id"], False),
    ("ix_catalog_consumers_generator_id", "catalog_consumers", ["generator_id"], False),
    ("ix_catalog_nontables_data_source_id", "catalog_nontables", ["data_source_id"], False),
    # "My reports" favourites
    ("ix_dashboard_favorites_user_id", "dashboard_favorites", ["user_id"], False),
    ("ix_presentations_owner_id", "presentations", ["owner_id"], False),
    ("ix_presentation_items_presentation_id", "presentation_items", ["presentation_id"], False),
    ("ix_employees_oneviewid", "employees", ["oneviewid"], False),
    ("ix_llm_access_user_id", "llm_access", ["user_id"], True),
    # Reference tables (ADR-16)
    ("ix_reference_tables_responsible_user_id", "reference_tables", ["responsible_user_id"], False),
    ("ix_reference_table_versions_table_id", "reference_table_versions", ["table_id"], False),
    ("ix_run_reference_uses_run_id", "run_reference_uses", ["run_id"], False),
    ("ix_run_reference_uses_table_id", "run_reference_uses", ["table_id"], False),
    # Quests (ADR-17). audit_log's (actor_id, created_at) index is for the
    # quest-completion evidence scan (QuestProfile.verified_at).
    ("ix_audit_log_actor_created", "audit_log", ["actor_id", "created_at"], False),
    ("ix_quest_openings_user_id", "quest_openings", ["user_id"], False),
    ("ix_quest_openings_user_created", "quest_openings", ["user_id", "created_at"], False),
    ("ix_quest_cards_user_id", "quest_cards", ["user_id"], False),
    ("ix_quest_completions_user_id", "quest_completions", ["user_id"], False),
    ("ix_quiz_questions_day", "quiz_questions", ["day"], False),
    ("ix_quiz_attempts_user_id", "quiz_attempts", ["user_id"], False),
    ("ix_quiz_attempts_token", "quiz_attempts", ["token"], False),
    ("ix_quest_client_events_user_id", "quest_client_events", ["user_id"], False),
    # Row-level security (ADR-18)
    ("ix_sales_scopes_oneviewid", "sales_scopes", ["oneviewid"], False),
    ("ix_sales_scopes_node_norm", "sales_scopes", ["node_norm"], False),
    # Server-side record of an AI answer (no data)
    ("ix_explore_answers_user_created", "explore_answers", ["user_id", "created_at"], False),
    ("ix_explore_answers_generator_created", "explore_answers", ["generator_id", "created_at"], False),
]


def _inspector():
    return sa.inspect(op.get_bind())


def _columns(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def _index_names(insp, table: str) -> set[str]:
    return {i["name"] for i in insp.get_indexes(table)}


def upgrade() -> None:
    insp = _inspector()
    tables = set(insp.get_table_names())

    for table, column, type_, kwargs in COLUMNS:
        # A table can be absent on a database that predates the feature and
        # was never touched by create_all() — nothing to add a column to.
        if table not in tables or column in _columns(insp, table):
            continue
        op.add_column(table, sa.Column(column, type_, **kwargs))

    insp = _inspector()  # the ALTERs above changed what the inspector sees
    for name, table, columns, unique in INDEXES:
        if table not in tables or name in _index_names(insp, table):
            continue
        # The column may be missing not because of an "old database" but because
        # it was renamed LATER (0013: oneviewid -> person_id). This revision is
        # history: it keeps calling the column what it was called on
        # its day, and it must not be rewritten — it runs on databases
        # that have not reached the rename yet. But it must not fail on a database
        # where the rename has already happened either: an index on a vanished
        # name is pointless, and the correct one is created by the revision that renamed it.
        if any(c not in _columns(insp, table) for c in columns):
            continue
        op.create_index(name, table, columns, unique=unique)


def downgrade() -> None:
    insp = _inspector()
    tables = set(insp.get_table_names())

    # Indexes first: SQLite refuses to DROP a column an index still mentions.
    for name, table, _columns_, _unique in INDEXES:
        if table in tables and name in _index_names(insp, table):
            op.drop_index(name, table_name=table)

    insp = _inspector()
    for table, column, _type, _kwargs in reversed(COLUMNS):
        if table not in tables or column not in _columns(insp, table):
            continue
        if (table, column) in REFERENCING:
            # "unknown column ... in foreign key definition" — see REFERENCING.
            with op.batch_alter_table(table) as batch:
                batch.drop_column(column)
        else:
            op.drop_column(table, column)
