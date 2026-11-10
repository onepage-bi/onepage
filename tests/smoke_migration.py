"""Smoke test for the startup migration discipline (Alembic, app/migrations).

Boot no longer re-runs a wall of ALTER TABLE: app/services/migrate.py decides
what the file in front of it is and lets Alembic do the rest. The four shapes
that has to survive, all exercised here:

  (a) a database written by the OLD code — pre-Lineage-v2 column lists, no
      `alembic_version` — must be adopted at 0001 and upgraded to head, and
      come out with every column, index and table the current models declare;
  (b) a clean file must be built by create_all and stamped at head, with no
      revision actually applied and no data migration run;
  (c) a second boot must change NOTHING: same rows, byte-identical
      `sqlite_master`, and a log that says "at head" instead of "applying";
  (d) `alembic downgrade 0001` must really undo every revision above the
      baseline (the columns they add are
      gone) and `alembic upgrade head` must put it back;
  (e) the schema the REVISIONS build on their own (`downgrade base` + `upgrade
      head`, and an empty file upgraded from nothing) must be the schema
      `create_all()` builds — compared in full: `PRAGMA table_info` (name,
      type, NOT NULL, DEFAULT, PK), `PRAGMA foreign_key_list` and the index
      definitions of every table. Prod has both kinds of database (a fresh
      install is stamped, an old one is migrated) and they must be the same
      product.

The data migrations that ride along with the schema — `exclude_rules` moving
onto (generator, source) pairs, the observation backfill — are still asserted
here: they run in the same lifespan and the old-schema fixture is the only
place they can be seen.

Each boot runs in a SUBPROCESS (`--boot <db>`), and so does each Alembic
command (`--alembic <db> <cmd> <rev>`): the SQLAlchemy engine binds to
OP_DB_PATH at import, so one process can only ever address one database.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# Child mode: boot the app against a given DB and report the migration output
# ---------------------------------------------------------------------------

def boot(db_path):
    tmp = tempfile.mkdtemp(prefix="op-mig-boot-")
    os.environ.update({
        "OP_DB_PATH": db_path,
        "OP_UPLOADS_DIR": f"{tmp}/uploads",
        "OP_PERF_DIR": f"{tmp}/perflog",
        "OP_DEV_BYPASS_AUTH": "true",
        "OP_INITIAL_ADMIN": "admin1",
        "OP_CACHE_WARM": "0",
    })
    sys.path.insert(0, os.getcwd())
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)
        assert r.status_code == 302, f"dev-login: {r.status_code}"
        r = c.get("/health")
        assert r.status_code == 200, f"health: {r.status_code}"
    print("BOOT-OK")


def alembic_cmd(db_path, *argv):
    """Child mode: run an Alembic command against `db_path` and print the head."""
    tmp = tempfile.mkdtemp(prefix="op-mig-alembic-")
    os.environ.update({"OP_DB_PATH": db_path, "OP_UPLOADS_DIR": f"{tmp}/uploads"})
    sys.path.insert(0, os.getcwd())
    from alembic import command
    from app.services import migrate

    cfg = migrate.config()
    getattr(command, argv[0])(cfg, argv[1])
    print(f"REVISION {migrate.current_revision()}")


# ---------------------------------------------------------------------------
# Old-schema fixture: pre-v2 column lists, copied from app/models.py
# ---------------------------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY, person_id VARCHAR(128) NOT NULL UNIQUE,
    display_name VARCHAR(256), role VARCHAR(32) NOT NULL,
    is_active BOOLEAN, created_at DATETIME, last_login DATETIME);

CREATE TABLE generators (
    id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL UNIQUE,
    is_archived BOOLEAN, git_repo VARCHAR(512), git_branch VARCHAR(128),
    git_token VARCHAR(512), git_last_synced_sha VARCHAR(64),
    git_last_synced_at DATETIME, git_last_sync_error TEXT,
    created_by INTEGER, created_at DATETIME);

CREATE TABLE generator_versions (
    id INTEGER PRIMARY KEY, generator_id INTEGER NOT NULL,
    version_number INTEGER NOT NULL, source_type VARCHAR(16) NOT NULL,
    bundle_path TEXT NOT NULL, manifest_json TEXT NOT NULL,
    git_commit_sha VARCHAR(64), is_current BOOLEAN,
    checksum_sha256 VARCHAR(64) NOT NULL, created_by INTEGER,
    created_at DATETIME, UNIQUE (generator_id, version_number));

CREATE TABLE dashboards (
    id VARCHAR(36) PRIMARY KEY, slug VARCHAR(256) UNIQUE,
    title VARCHAR(512) NOT NULL, description TEXT, dash_type VARCHAR(32),
    owner_id INTEGER, is_published BOOLEAN, is_public BOOLEAN,
    current_version INTEGER, slot_count INTEGER, folder_id INTEGER,
    generator_id INTEGER, render_size INTEGER,
    created_at DATETIME, updated_at DATETIME);

CREATE TABLE generation_runs (
    id INTEGER PRIMARY KEY, dashboard_id VARCHAR(36) NOT NULL,
    generator_version_id INTEGER NOT NULL, engine VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL, started_by INTEGER, started_at DATETIME,
    heartbeat_at DATETIME, finished_at DATETIME, input_manifest TEXT,
    error_message TEXT, error_detail TEXT, output_version_number INTEGER,
    duration_ms INTEGER, bytes_published INTEGER);

CREATE TABLE data_sources (
    id INTEGER PRIMARY KEY, kind VARCHAR(16) NOT NULL,
    key_name VARCHAR(255) NOT NULL, declared_path VARCHAR(1024),
    label VARCHAR(255), notes TEXT, exclude_rules TEXT,
    updated_by INTEGER, updated_at DATETIME, created_at DATETIME,
    CONSTRAINT uq_data_sources_kind_key UNIQUE (kind, key_name));
"""

DAY_MS = 86400_000
NOW_MS = int(time.time() * 1000)
MB = 1048576

# (name, size, mtime_ms, dir/aux) per run, exactly as gen-engine.js writes it:
# `dir` omitted on the main folder, `aux: true` on the analyst's own upload.
RUN_A1 = {
    "dirs": ["Stock_RDC", "Reasons_Catalog"],
    "files": [
        {"name": "w31.xlsx", "size": 10 * MB, "mtime_ms": NOW_MS - DAY_MS},
        {"name": "w32.xlsx", "size": 30 * MB, "mtime_ms": NOW_MS},
        {"name": "SL_NWD.xls", "size": 2 * MB, "mtime_ms": NOW_MS - 40 * DAY_MS, "dir": 1},
        {"name": "own.xlsx", "size": 99 * MB, "mtime_ms": NOW_MS, "aux": True},
    ],
    "skipped": [{"name": "~$w31.xlsx", "reason": "ignored"}],
}
RUN_A2 = {
    "dirs": ["Stock_RDC"],
    "files": [{"name": "b31.xlsx", "size": 4 * MB, "mtime_ms": NOW_MS}],
    "skipped": [],
}
RUN_B1 = {
    "dirs": ["Stock_RDC"],
    "files": [{"name": "ops.xlsx", "size": 5 * MB, "mtime_ms": NOW_MS},
              {"name": "ops2.xlsx", "size": 1 * MB, "mtime_ms": NOW_MS}],
    "skipped": [],
}


def build_old_db(path):
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    now = "2026-08-01 10:00:00"
    con.execute("INSERT INTO users VALUES (1,'admin1','Admin','administrator',1,?,NULL)", (now,))
    for gid, name in ((1, "gen-a"), (2, "gen-b")):
        con.execute("INSERT INTO generators (id,name,is_archived,created_by,created_at)"
                    " VALUES (?,?,0,1,?)", (gid, name, now))
        con.execute("INSERT INTO generator_versions (id,generator_id,version_number,"
                    "source_type,bundle_path,manifest_json,is_current,checksum_sha256,"
                    "created_by,created_at) VALUES (?,?,1,'bundle',?,?,1,?,1,?)",
                    (gid, gid, f"_generators/{gid}/v1", json.dumps({"name": name}),
                     "0" * 64, now))
    for did, title, gen in (("dash-1", "A one", 1), ("dash-2", "A two", 1),
                            ("dash-3", "B one", 2)):
        con.execute("INSERT INTO dashboards (id,title,dash_type,owner_id,is_published,"
                    "is_public,current_version,slot_count,generator_id,created_at,updated_at)"
                    " VALUES (?,?,'static',1,1,0,1,0,?,?,?)", (did, title, gen, now, now))
    runs = (("dash-1", RUN_A1, "2026-08-02 09:00:00"),
            ("dash-2", RUN_A2, "2026-08-03 09:00:00"),
            ("dash-3", RUN_B1, "2026-08-04 09:00:00"))
    for i, (did, im, started) in enumerate(runs, start=1):
        con.execute("INSERT INTO generation_runs (id,dashboard_id,generator_version_id,"
                    "engine,status,started_by,started_at,finished_at,input_manifest,"
                    "output_version_number) VALUES (?,?,?, 'browser','success',1,?,?,?,1)",
                    (i, did, 1 if did != "dash-3" else 2, started, started,
                     json.dumps(im, ensure_ascii=False)))
    con.execute("INSERT INTO data_sources (id,kind,key_name,exclude_rules,created_at)"
                " VALUES (1,'folder','Stock_RDC',?,?)",
                (json.dumps(["~$*", "archive/**"]), now))
    con.execute("INSERT INTO data_sources (id,kind,key_name,exclude_rules,created_at)"
                " VALUES (2,'folder','Orphan_Folder',?,?)",
                (json.dumps(["*.tmp"]), now))
    con.commit()
    con.close()


def fingerprint(names):
    return hashlib.sha256("\n".join(sorted(n.casefold() for n in names))
                          .encode("utf-8")).hexdigest()


def run_child(*argv):
    """Run this file in a child process; returns (rc, combined output)."""
    p = subprocess.run([sys.executable, os.path.abspath(__file__), *argv],
                       cwd=os.getcwd(), capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def run_boot(db_path):
    """Boot the app in a child process; returns (rc, combined output)."""
    return run_child("--boot", db_path)


def run_alembic(db_path, cmd, rev):
    """`alembic <cmd> <rev>` in a child process; returns (rc, combined output)."""
    return run_child("--alembic", db_path, cmd, rev)


def stamped(con):
    """The revision recorded in the database, or None."""
    rows = con.execute("SELECT name FROM sqlite_master WHERE name='alembic_version'").fetchall()
    if not rows:
        return None
    got = con.execute("SELECT version_num FROM alembic_version").fetchall()
    return got[0][0] if got else None


def table_columns(con, table):
    return {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}


def index_names(con):
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")}


# Tables that are NOT in the models and must not be in the revisions: the runtime
# creates them itself with `CREATE TABLE IF NOT EXISTS` (`app/auth/session.py`,
# `app/auth/samlreplay.py`). They are not the application schema but the
# authentication store shared by several uvicorn workers.
RUNTIME_TABLES = {"alembic_version", "sessions", "saml_assertions"}


def app_tables(con):
    return sorted(r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        if r[0] not in RUNTIME_TABLES)


def full_schema(con):
    """Every table as SQLite itself sees it: `PRAGMA table_info` in full
    (name/type/NOT NULL/DEFAULT/PK) plus `PRAGMA foreign_key_list`, plus the
    index definitions.

    Comparing only the COLUMN LIST is not enough: a column declared nullable
    in a revision but NOT NULL in the model (or one that lost its foreign key)
    passed such a test silently, and the drift between a "migrated" database and
    a "create_all()-built" one would first be noticed in prod. Foreign keys are
    sorted by (from, table, to): SQLite numbers them in declaration order, which
    differs between the model's `CREATE TABLE` and the revision's, and need
    not match.
    """
    out = {}
    for t in app_tables(con):
        # Columns are compared BY NAME, not by position: `ALTER TABLE ADD
        # COLUMN` always appends the column, so a migrated database physically
        # cannot reproduce the models' `CREATE TABLE` order without rebuilding
        # the table. The application never reads column order (SQLAlchemy
        # goes by name), and rebuilding a live table for cosmetics is risk
        # with no benefit.
        cols = sorted((r[1], (r[2] or "").upper(), r[3], r[4], r[5])
                      for r in con.execute(f'PRAGMA table_info("{t}")'))
        fks = sorted((r[2], r[3], r[4], (r[5] or "NO ACTION"), (r[6] or "NO ACTION"))
                     for r in con.execute(f'PRAGMA foreign_key_list("{t}")'))
        idx = sorted(r[0] for r in con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL", (t,)))
        out[t] = {"columns": cols, "foreign_keys": fks, "indexes": idx}
    return out


def schema_diff(a, b):
    """The differences between two `full_schema()` results, one line per mismatch,
    so a failed check names the table and column right away."""
    diff = []
    for t in sorted(set(a) | set(b)):
        if t not in a or t not in b:
            diff.append(f"{t}: only in {'create_all' if t in b else 'migrations'}")
            continue
        for part in ("columns", "foreign_keys", "indexes"):
            if a[t][part] != b[t][part]:
                only_a = [x for x in a[t][part] if x not in b[t][part]]
                only_b = [x for x in b[t][part] if x not in a[t][part]]
                diff.append(f"{t}.{part}: migrations={only_a} create_all={only_b}")
    return diff


# Every column the revisions add, as (table, column) — 0002 is the list
# app/main.py used to ALTER in on every boot, 0003 adds users.lang and 0004
# adds quiz_questions.i18n_json. Kept here on
# purpose: this test is the place that notices if a column quietly leaves the
# migration.
# The newest revision. Bump it (and MIGRATED_COLUMNS) when a revision is added
# — this test is deliberately explicit about what "head" means today.
HEAD = "0013"

MIGRATED_COLUMNS = [
    ("quest_profiles", "companion_since"), ("quest_profiles", "companion_off"),
    ("quest_profiles", "autumn_theme"),
    ("dashboards", "is_public"), ("dashboards", "folder_id"),
    ("dashboards", "render_size"), ("dashboards", "generator_id"),
    ("dashboards", "rls_json"), ("users", "upn"),
    ("generation_runs", "outputs_json"), ("data_sources", "version"),
    ("data_sources", "first_seen_at"), ("data_sources", "last_seen_at"),
    ("data_sources", "fingerprint"), ("data_sources", "last_file_count"),
    ("data_sources", "stale_after_days"), ("data_sources", "min_files"),
    ("data_sources", "thresholds_json"), ("data_sources", "owner_id"),
    ("input_overrides", "column_aliases"),
    ("generator_versions", "semantics_json"), ("employees", "search_blob"),
    ("quest_openings", "revealed_at"),
    # 0003 — interface language, read on the hot path through the session JOIN
    ("users", "lang"),
    # 0004 — quiz question translations, next to the Russian source
    ("quiz_questions", "i18n_json"),
    # 0005 — 👍/👎 on a recorded AI answer (P4)
    ("explore_answers", "vote"), ("explore_answers", "voted_at"),
    # 0009 — paths sent by the desktop application
    ("source_files", "path"), ("data_sources", "root_path"), ("data_sources", "root_id"),
    # 0010 — "highly confidential": password hash of the second lock
    ("dashboards", "conf_hash"),
    # 0011 — "this file IS such-and-such declared input"
    ("input_overrides", "file_aliases"),
]

# Tables a revision above the baseline CREATES — so `downgrade 0001` is allowed
# to take them away, and `upgrade head` has to put them back byte-identically.
MIGRATED_TABLES = ["explore_form_suggestions", "learn_progress", "device_tokens",
                   "dashboard_unlocks"]


def q(con, sql, args=()):
    return con.execute(sql, args).fetchall()


def main():
    tmp = tempfile.mkdtemp(prefix="op-mig-smoke-")
    db_path = f"{tmp}/onepage.db"
    build_old_db(db_path)

    rc, out = run_boot(db_path)
    check("first boot succeeds", rc == 0 and "BOOT-OK" in out, out[-1200:])
    check("migration logs one summary line",
          out.count("[lineage-migrate] overrides created") == 1, out[-600:])
    check("orphan source warned about", "Orphan_Folder" in out, out[-600:])
    check("path-separator rule reported", "archive/**" in out, out[-600:])

    # ── (a) the old-code database is ADOPTED, not rebuilt ───────────────
    check("existing database adopted at 0001",
          "[alembic] existing database adopted at 0001" in out, out[-800:])
    check("the catch-up revision is applied", "applying: 0002" in out, out[-800:])

    con = sqlite3.connect(db_path)
    check("database stamped at head after adoption", stamped(con) == HEAD, str(stamped(con)))
    missing = [f"{t}.{c}" for t, c in MIGRATED_COLUMNS if c not in table_columns(con, t)]
    check("every column 0002 owns is on the old database", missing == [], str(missing))
    idx = index_names(con)
    check("indexes 0002 owns are present",
          {"ix_users_upn", "ix_audit_log_actor_created", "ix_llm_access_user_id",
           "ix_dashboards_generator_id", "ix_sales_scopes_person_id"} <= idx,
          str(sorted(idx)))

    # ── ALTERs ──────────────────────────────────────────────────────────
    ds_cols = {r[1] for r in q(con, "PRAGMA table_info(data_sources)")}
    check("data_sources gained the v2 columns",
          {"version", "first_seen_at", "last_seen_at", "fingerprint",
           "last_file_count"} <= ds_cols, str(sorted(ds_cols)))
    run_cols = {r[1] for r in q(con, "PRAGMA table_info(generation_runs)")}
    check("generation_runs gained outputs_json", "outputs_json" in run_cols, str(sorted(run_cols)))

    # ── Data Quality: threshold/alias columns + the three new tables ─────
    check("data_sources gained the quality threshold columns",
          {"stale_after_days", "min_files", "thresholds_json"} <= ds_cols, str(sorted(ds_cols)))
    ov_cols = {r[1] for r in q(con, "PRAGMA table_info(input_overrides)")}
    check("input_overrides gained column_aliases", "column_aliases" in ov_cols, str(sorted(ov_cols)))
    tables = {r[0] for r in q(con, "SELECT name FROM sqlite_master WHERE type='table'")}
    check("quality tables created",
          {"quality_results", "value_baselines", "quality_acks"} <= tables, str(sorted(tables)))
    idx = {r[0] for r in q(con, "SELECT name FROM sqlite_master WHERE type='index'")}
    check("quality indexes present",
          {"ix_quality_results_source_file_id", "ix_quality_results_observation_id",
           "ix_value_baselines_data_source_id", "ix_value_baselines_generator_id",
           "ix_quality_acks_finding_id"} <= idx, str(sorted(i for i in idx if "quality" in i or "baseline" in i)))
    uq = q(con, "SELECT sql FROM sqlite_master WHERE name='value_baselines'")[0][0]
    check("value_baselines carries uq_value_baseline", "uq_value_baseline" in uq, uq)

    # ── Stages (P4.1): a new table + its index on a pre-v2 database ──────
    check("run_stages created", "run_stages" in tables, str(sorted(tables)))
    check("run_stages index present", "ix_run_stages_run_id" in idx,
          str(sorted(i for i in idx if "run_stage" in i)))
    stage_sql = q(con, "SELECT sql FROM sqlite_master WHERE name='run_stages'")[0][0]
    check("run_stages carries uq_run_stage_order", "uq_run_stage_order" in stage_sql, stage_sql)
    check("run_stages columns match the contract",
          {r[1] for r in q(con, "PRAGMA table_info(run_stages)")} ==
          {"id", "run_id", "order", "name", "rows", "columns_json", "profiles_json",
           "inputs_json", "read_files_json", "ms", "created_at"},
          str(sorted(r[1] for r in q(con, "PRAGMA table_info(run_stages)"))))

    # ── Catalogue (Leg 1): four new tables + their indexes on a pre-v2 DB ─
    check("catalog tables created",
          {"catalog_tables", "catalog_partitions", "catalog_consumers",
           "catalog_nontables"} <= tables, str(sorted(tables)))
    check("catalog indexes present",
          {"ix_catalog_tables_table_id", "ix_catalog_tables_data_source_id",
           "ix_catalog_partitions_table_id", "ix_catalog_consumers_table_id",
           "ix_catalog_consumers_generator_id",
           "ix_catalog_nontables_data_source_id"} <= idx,
          str(sorted(i for i in idx if "catalog" in i)))
    check("catalog_tables columns match the contract",
          {r[1] for r in q(con, "PRAGMA table_info(catalog_tables)")} ==
          {"id", "table_id", "data_source_id", "name", "display_name", "name_stem",
           "sheets_json", "core_cols_json", "columns_json", "header_row",
           "confidence", "evidence_json", "n_files", "first_seen_at",
           "last_seen_at", "status", "confirmed_by", "confirmed_at",
           "superseded_by", "owner_id", "version", "updated_at", "created_at"},
          str(sorted(r[1] for r in q(con, "PRAGMA table_info(catalog_tables)"))))
    # unique=True + index=True on the column: SQLAlchemy expresses that as a
    # UNIQUE INDEX, so that is where the constraint is visible.
    tid_idx = q(con, "SELECT sql FROM sqlite_master WHERE name='ix_catalog_tables_table_id'")
    check("catalog_tables.table_id is unique",
          bool(tid_idx) and "UNIQUE" in (tid_idx[0][0] or "").upper(), str(tid_idx))
    for name, uq in (("catalog_partitions", "uq_catalog_partition"),
                     ("catalog_consumers", "uq_catalog_consumer"),
                     ("catalog_nontables", "uq_catalog_nontable")):
        sql = q(con, f"SELECT sql FROM sqlite_master WHERE name='{name}'")[0][0]
        check(f"{name} carries {uq}", uq in sql, sql)

    # ── Explore (Leg 3): one new table + its index on a pre-v2 DB ────────
    check("explore_questions created", "explore_questions" in tables, str(sorted(tables)))
    check("explore_questions index present", "ix_explore_questions_user_id" in idx,
          str(sorted(i for i in idx if "explore" in i)))
    check("explore_questions columns match the contract",
          {r[1] for r in q(con, "PRAGMA table_info(explore_questions)")} ==
          {"id", "user_id", "title", "question", "sql", "table_ids_json",
           "shared", "created_at", "updated_at"},
          str(sorted(r[1] for r in q(con, "PRAGMA table_info(explore_questions)"))))

    # ── Explore feedback (P4, 0005): the vote columns + the suggestion box ─
    check("explore_answers gained the vote columns",
          {"vote", "voted_at"} <= table_columns(con, "explore_answers"),
          str(sorted(table_columns(con, "explore_answers"))))
    check("explore_form_suggestions created", "explore_form_suggestions" in tables,
          str(sorted(tables)))
    check("explore_form_suggestions indexes present",
          {"ix_explore_form_suggestions_generator_status",
           "ix_explore_form_suggestions_user_created"} <= idx,
          str(sorted(i for i in idx if "form_suggestions" in i)))
    check("explore_form_suggestions columns match the contract",
          {r[1] for r in q(con, "PRAGMA table_info(explore_form_suggestions)")} ==
          {"id", "generator_id", "user_id", "answer_id", "question", "question_hash",
           "kpi_key", "intent_json", "sql_hash", "kind", "status", "created_at",
           "decided_at", "decided_by"},
          str(sorted(r[1] for r in q(con, "PRAGMA table_info(explore_form_suggestions)"))))
    sug_sql = q(con, "SELECT sql FROM sqlite_master WHERE name='explore_form_suggestions'")[0][0]
    check("explore_form_suggestions stores a SQL HASH, never SQL text",
          "sql_hash" in sug_sql and '"sql"' not in sug_sql, sug_sql)
    check("explore_form_suggestions declares its four foreign keys",
          sug_sql.upper().count("FOREIGN KEY") == 4, sug_sql)

    # ── exclude_rules -> input_overrides ────────────────────────────────
    ovs = q(con, "SELECT generator_id, data_source_id, exclude_rules FROM input_overrides"
                 " ORDER BY generator_id")
    check("one override per reading generator", len(ovs) == 2, str(ovs))
    check("both generators of the shared folder got one",
          [(o[0], o[1]) for o in ovs] == [(1, 1), (2, 1)], str(ovs))
    check("rules moved verbatim",
          all(json.loads(o[2]) == ["~$*", "archive/**"] for o in ovs), str(ovs))

    rules = dict(q(con, "SELECT key_name, exclude_rules FROM data_sources"))
    check("migrated source's rules cleared", rules.get("Stock_RDC") is None, str(rules))
    check("unreadable source keeps its rules",
          json.loads(rules.get("Orphan_Folder") or "null") == ["*.tmp"], str(rules))

    # ── Observation backfill ────────────────────────────────────────────
    obs = q(con, "SELECT o.run_id, d.key_name, o.dir_index, o.file_count, o.total_bytes,"
                 " o.fingerprint, o.truncated, o.skipped_json FROM source_observations o"
                 " JOIN data_sources d ON d.id = o.data_source_id"
                 " ORDER BY o.run_id, o.dir_index")
    check("one observation per (run, folder)", len(obs) == 4, str(obs))
    by = {(r[0], r[1]): r for r in obs}
    o1 = by.get((1, "Stock_RDC"))
    check("main-folder files attributed to dir 0",
          o1 is not None and o1[2] == 0 and o1[3] == 2 and o1[4] == 40 * MB, str(o1))
    check("aux file belongs to no folder",
          all(r[4] < 99 * MB for r in obs), str([r[4] for r in obs]))
    o2 = by.get((1, "Reasons_Catalog"))
    check("dir=1 file attributed to the second folder",
          o2 is not None and o2[2] == 1 and o2[3] == 1 and o2[4] == 2 * MB, str(o2))
    check("fingerprint over the case-folded name set",
          o1 and o1[5] == fingerprint(["w31.xlsx", "w32.xlsx"]), str(o1))
    check("nothing truncated at this size", all(not r[6] for r in obs), str(obs))
    check("skipped list attributed to the single-folder run", o1[7] is None
          and json.loads(by[(3, "Stock_RDC")][7] or "null") is None, str(o1[7]))

    files = q(con, "SELECT f.name, f.size, f.mtime_ms, f.kind, f.schema_json"
                   " FROM source_files f ORDER BY f.name")
    check("every manifest file became a legacy row",
          [f[0] for f in files] == ["SL_NWD.xls", "b31.xlsx", "ops.xlsx",
                                    "ops2.xlsx", "w31.xlsx", "w32.xlsx"], str(files))
    check("backfilled files are kind=legacy with no schema",
          all(f[3] == "legacy" and f[4] is None for f in files), str(files))
    check("sizes and mtimes carried over",
          all(isinstance(f[1], int) and isinstance(f[2], int) for f in files), str(files))

    # ── data_sources summary ────────────────────────────────────────────
    summ = {r[0]: r for r in q(con, "SELECT key_name, first_seen_at, last_seen_at,"
                                    " fingerprint, last_file_count, version FROM data_sources")}
    s = summ["Stock_RDC"]
    check("summary comes from the newest observation",
          s[4] == 2 and s[3] == fingerprint(["ops.xlsx", "ops2.xlsx"]), str(s))
    check("first_seen_at from the oldest run that saw it",
          str(s[1]).startswith("2026-08-02") and str(s[2]).startswith("2026-08-04"), str(s))
    check("Reasons_Catalog created by the backfill",
          "Reasons_Catalog" in summ and summ["Reasons_Catalog"][4] == 1, str(summ))
    check("version defaults to 1", all(r[5] == 1 for r in summ.values()), str(summ))
    check("orphan source got no observation",
          summ["Orphan_Folder"][4] is None, str(summ["Orphan_Folder"]))

    before = {t: q(con, f"SELECT COUNT(*) FROM {t}")[0][0]
              for t in ("input_overrides", "source_observations", "source_files",
                        "data_sources")}
    schema_before = q(con, "SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2")
    con.close()

    # ── Second boot: the migration must find nothing to do ──────────────
    rc, out = run_boot(db_path)
    check("second boot succeeds", rc == 0 and "BOOT-OK" in out, out[-1200:])
    check("second boot migrates nothing",
          "overrides created" not in out, out[-600:])
    check("second boot finds the schema at head",
          f"[alembic] at head ({HEAD})" in out, out[-800:])
    check("second boot applies no revision and re-adopts nothing",
          "applying:" not in out and "adopted at" not in out, out[-800:])
    # The orphan's rules are still on the row, so the standing warning repeats.
    check("orphan warning still stands", "Orphan_Folder" in out, out[-600:])
    con = sqlite3.connect(db_path)
    after = {t: q(con, f"SELECT COUNT(*) FROM {t}")[0][0] for t in before}
    check("row counts unchanged after re-boot", after == before, f"{before} -> {after}")
    dupes = q(con, "SELECT generator_id, data_source_id, COUNT(*) c FROM input_overrides"
                   " GROUP BY 1,2 HAVING c > 1")
    check("no duplicate overrides", dupes == [], str(dupes))
    schema_after = q(con, "SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2")
    check("second boot leaves the schema byte-identical (ALTERs/indexes are no-ops)",
          schema_after == schema_before,
          str([a for a in schema_after if a not in schema_before])[:600])
    con.close()

    # ── (b) A database that never had any of this boots clean ───────────
    fresh = f"{tmp}/fresh.db"
    rc, out = run_boot(fresh)
    check("fresh empty database boots", rc == 0 and "BOOT-OK" in out, out[-1200:])
    check("fresh database migrates nothing", "[lineage-migrate]" not in out, out[-600:])
    check("fresh database is created and stamped, not upgraded",
          f"[alembic] new database created at head ({HEAD})" in out
          and "applying:" not in out, out[-800:])

    con = sqlite3.connect(fresh)
    check("fresh database stamped at head", stamped(con) == HEAD, str(stamped(con)))
    missing = [f"{t}.{c}" for t, c in MIGRATED_COLUMNS if c not in table_columns(con, t)]
    check("create_all alone already carries every migrated column", missing == [], str(missing))
    fresh_tables = {r[0] for r in q(con, "SELECT name FROM sqlite_master WHERE type='table'"
                                        " AND name NOT LIKE 'sqlite_%'")}
    fresh_idx = index_names(con)
    fresh_cols = {t: table_columns(con, t) for t in fresh_tables}
    # The full shape of the database create_all() builds — every column with
    # its type/NOT NULL/DEFAULT/PK, every foreign key, every index.
    create_all_schema = full_schema(con)
    con.close()

    # ── (d) downgrading to the baseline really undoes every revision above
    #        it, and upgrade puts it all back ────────────────────────────
    rc, out = run_alembic(fresh, "downgrade", "0001")
    check("downgrade to 0001 succeeds", rc == 0 and "REVISION 0001" in out, out[-1200:])
    con = sqlite3.connect(fresh)
    check("downgrade left the database at 0001", stamped(con) == "0001", str(stamped(con)))
    still = [f"{t}.{c}" for t, c in MIGRATED_COLUMNS if c in table_columns(con, t)]
    check("downgrade dropped every migrated column", still == [], str(still))
    check("downgrade dropped the indexes 0002 owns",
          not ({"ix_users_upn", "ix_llm_access_user_id",
                "ix_audit_log_actor_created"} & index_names(con)),
          str(sorted(index_names(con))))
    check("downgrade dropped exactly the tables a revision creates",
          fresh_tables - set(MIGRATED_TABLES) == {
              r[0] for r in q(con, "SELECT name FROM sqlite_master WHERE type='table'"
                                   " AND name NOT LIKE 'sqlite_%'")},
          str(sorted(MIGRATED_TABLES)))
    con.close()

    rc, out = run_alembic(fresh, "upgrade", "head")
    check("upgrade head succeeds", rc == 0 and f"REVISION {HEAD}" in out, out[-1200:])
    con = sqlite3.connect(fresh)
    check("upgrade restored the columns",
          {t: table_columns(con, t) for t in fresh_tables} == fresh_cols,
          str([t for t in fresh_tables if table_columns(con, t) != fresh_cols[t]]))
    check("upgrade restored the indexes", index_names(con) == fresh_idx,
          str(fresh_idx ^ index_names(con)))
    con.close()

    # And the app still boots on it — the round trip left nothing broken.
    rc, out = run_boot(fresh)
    check("app boots after downgrade+upgrade", rc == 0 and "BOOT-OK" in out, out[-1200:])

    # ── (e) the WHOLE schema built by the revisions alone must be the same
    #        database create_all() builds ────────────────────────────────
    # `downgrade base` takes the baseline itself away, so `upgrade head`
    # rebuilds every table from the revision files — the schema a database
    # that was NEVER created by create_all() ends up with. It has to match
    # the models column for column (type, NOT NULL, DEFAULT, PK), foreign key
    # for foreign key, index for index: those two databases are the same
    # product in prod, and a difference here is a bug that only shows up on
    # whichever half of the fleet was built the other way.
    scratch = f"{tmp}/from-revisions.db"
    open(scratch, "wb").close()
    rc, out = run_alembic(scratch, "upgrade", "head")
    check("an empty file upgrades from nothing to head",
          rc == 0 and f"REVISION {HEAD}" in out, out[-1200:])
    con = sqlite3.connect(scratch)
    diff = schema_diff(full_schema(con), create_all_schema)
    check("revisions alone build exactly the create_all schema (PRAGMA table_info"
          " + foreign_key_list + indexes)", diff == [], " | ".join(diff)[:1500])
    con.close()

    rc, out = run_alembic(fresh, "downgrade", "base")
    check("downgrade to base succeeds", rc == 0, out[-1200:])
    con = sqlite3.connect(fresh)
    check("downgrade base leaves no application table", app_tables(con) == [],
          str(app_tables(con)))
    con.close()
    rc, out = run_alembic(fresh, "upgrade", "head")
    check("upgrade head after downgrade base succeeds",
          rc == 0 and f"REVISION {HEAD}" in out, out[-1200:])
    con = sqlite3.connect(fresh)
    diff = schema_diff(full_schema(con), create_all_schema)
    check("downgrade base + upgrade head reproduces the create_all schema exactly",
          diff == [], " | ".join(diff)[:1500])
    con.close()
    rc, out = run_boot(fresh)
    check("app boots after downgrade base + upgrade head",
          rc == 0 and "BOOT-OK" in out, out[-1200:])

    # ── OP_WORKERS > 1: several processes race the same empty file ──────
    # The flock in migrate.run() has to make exactly one of them create the
    # schema; the others must find it finished, not half-built.
    race_db = f"{tmp}/race.db"
    procs = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "--boot", race_db],
                              cwd=os.getcwd(), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True) for _ in range(4)]
    outs = [(p.wait(), p.stdout.read()) for p in procs]
    check("every racing worker boots", all(rc == 0 and "BOOT-OK" in o for rc, o in outs),
          str([o[-400:] for rc, o in outs if rc != 0]))
    creators = sum("new database created at head" in o for _rc, o in outs)
    check("exactly one worker creates the schema", creators == 1,
          str([o for _rc, o in outs if "[alembic]" in o]))
    con = sqlite3.connect(race_db)
    check("the raced database is at head and complete",
          stamped(con) == HEAD
          and [f"{t}.{c}" for t, c in MIGRATED_COLUMNS if c not in table_columns(con, t)] == [],
          str(stamped(con)))
    con.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--boot":
        boot(sys.argv[2])
    elif len(sys.argv) > 3 and sys.argv[1] == "--alembic":
        alembic_cmd(sys.argv[2], *sys.argv[3:])
    else:
        main()
