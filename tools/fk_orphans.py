#!/usr/bin/env python3
"""Foreign-key orphans: a report and, on request, a cleanup.

Until 2026-09-06 SQLite ran in this application with `foreign_keys=OFF` (the
default), so the file may have accumulated rows whose parent is already gone:
permissions on a deleted dashboard, `generation_runs` of a report that no longer
exists, `audit_log.actor_id` of a deleted account. Turning the check on does
NOT retroactively verify what is already in the file — SQLite only looks at new
writes. So the state has to be inspected separately, before deploying.

The list of foreign keys is built from `Base.metadata` (app/services/integrity.py);
nothing is listed here by hand: a key added to the models tomorrow lands in the
report the same day.

The report contains NO data: only the table name, column name, parent and row
count. So it can be taken from a copy of the production database and shown to
anyone.

`--fix` repairs with exactly the same policy that `integrity.detach()` applies to
live deletions, and it is NOT "delete everything found":
  * NOT NULL — the row exists only together with its parent: delete it and its
    own children (a deleted dashboard's run, its observations and files);
  * nullable — this is a reference, not ownership: NULL the column, the row
    stays. A presentation slide and a recorded AI answer outlive the dashboard
    they referred to; a dashboard with a dangling reference to a folder or
    generator moves to the root rather than being deleted.

References to `users` are NEVER deleted automatically (`--fix` skips them):
such a row stands for someone's work — a granted permission, an audit entry,
an acknowledged Data Quality finding — and a person, not a script, decides what
to do with it. If such orphans exist, the exit code is 2. The protection is on
the REFERENCE, not the row: a favourite orphaned by both its dashboard and its
account goes by the first one — the dashboard is gone anyway.

Run (on a COPY of the database, see docs/DEPLOY.md §2d):
    python tools/fk_orphans.py --db /path/to/copy.db
    python tools/fk_orphans.py --db /path/to/copy.db --fix
    python tools/fk_orphans.py --db /path/to/copy.db --json

Exit codes:
    0 — no orphans (or `--fix` cleaned everything it is allowed to);
    1 — orphans exist (report) or remain after `--fix`;
    2 — orphans referencing `users` exist — a person handles them;
    3 — database not found / cannot be opened.
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import integrity  # noqa: E402

# Parents whose orphans the script never touches: a row referencing a deleted
# account is someone's history, not garbage. They are always reported.
PROTECTED_PARENTS = {"users"}

# How many passes to repeat: deleting an orphan in `generation_runs` orphans its
# observations, and those orphan their files. The chain depth in the schema is 4.
MAX_PASSES = 8


def _open(path: str, *, read_only: bool) -> sqlite3.Connection:
    if not os.path.exists(path):
        print(f"ERROR: database not found: {path}", file=sys.stderr)
        raise SystemExit(3)
    uri = f"file:{path}?mode=ro" if read_only else f"file:{path}"
    conn = sqlite3.connect(uri, uri=True)
    # Autocommit: `fix()` issues BEGIN/COMMIT itself, and the driver's implicit
    # transaction would get in the way of both it and the PRAGMA below.
    conn.isolation_level = None
    # The cleanup runs over a file known to be inconsistent: with the check on,
    # deleting an orphaned parent would trip over its own children.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def scan(conn: sqlite3.Connection) -> list[dict]:
    """[{table, column, parent, protected, rows}] — non-empty only, descending."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    found = []
    for edge in integrity.fk_edges():
        # A table from the models that this file does not have yet (the
        # database predates the release): not an error, nothing to check.
        if edge.child_table not in tables or edge.parent_table not in tables:
            continue
        n = conn.execute(integrity.orphan_count_sql(edge)).fetchone()[0]
        if n:
            found.append({
                "table": edge.child_table,
                "column": edge.child_col,
                "parent": f"{edge.parent_table}.{edge.parent_col}",
                "protected": edge.parent_table in PROTECTED_PARENTS,
                "rows": int(n),
            })
    found.sort(key=lambda r: (-r["rows"], r["table"], r["column"]))
    return found


def fix(conn: sqlite3.Connection) -> dict[str, int]:
    """Repair orphans, except protected ones. One transaction, passes top-down
    along dependencies (parents before children), until a pass changes nothing:
    removing a row can orphan its own children.

    Exactly the same policy as `integrity.detach()` on live deletions:
    NOT NULL — the row exists only together with its parent, delete it;
    nullable — a reference, not ownership, so NULL it and the row lives on.
    The difference is not cosmetic: `presentation_items.dashboard_id` and
    `explore_answers.dashboard_id` were legitimately orphaned on prod (the
    dashboard was deleted before this branch), and the slide and the recorded
    AI answer are needed without it; while `dashboards.folder_id` /
    `dashboards.generator_id` are dangling references to a folder or generator,
    not a reason to lose a whole dashboard.

    Returns `{"<table>.<column>": n}` for deletions and
    `{"<table>.<column>=NULL": n}` for nullifications — like `detach()`."""
    order = {name: i for i, name in enumerate(integrity.table_order())}
    edges = [e for e in integrity.fk_edges() if e.parent_table not in PROTECTED_PARENTS]
    edges.sort(key=lambda e: order.get(e.child_table, 10**6))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    changed: dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for pass_no in range(1, MAX_PASSES + 1):
            pass_deleted = pass_nulled = 0
            for edge in edges:
                if edge.child_table not in tables or edge.parent_table not in tables:
                    continue
                if edge.nullable:
                    n = conn.execute(integrity.orphan_nullify_sql(edge)).rowcount
                    key = f"{edge.child_table}.{edge.child_col}=NULL"
                    pass_nulled += max(n, 0)
                else:
                    n = conn.execute(integrity.orphan_delete_sql(edge)).rowcount
                    key = f"{edge.child_table}.{edge.child_col}"
                    pass_deleted += max(n, 0)
                if n > 0:
                    changed[key] = changed.get(key, 0) + n
            print(f"  pass {pass_no}: deleted {pass_deleted}, nulled {pass_nulled}")
            if pass_deleted == 0 and pass_nulled == 0:
                break
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return changed


def _print_report(rows: list[dict]) -> None:
    if not rows:
        print("No orphans.")
        return
    width = max(len(f'{r["table"]}.{r["column"]}') for r in rows)
    print(f'{"table.column".ljust(width)}  {"parent".ljust(28)}  rows')
    print(f'{"-" * width}  {"-" * 28}  -----')
    for r in rows:
        mark = "  [users — report only]" if r["protected"] else ""
        print(f'{(r["table"] + "." + r["column"]).ljust(width)}  '
              f'{r["parent"].ljust(28)}  {r["rows"]:>5}{mark}')
    print(f'\nTotal orphan rows: {sum(r["rows"] for r in rows)} '
          f'(references: {len(rows)}).')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SQLite foreign-key orphans")
    ap.add_argument("--db", help="path to the database file (default: OP_DB_PATH from settings)")
    ap.add_argument("--fix", action="store_true",
                    help="repair orphans (except references to users) in one transaction: "
                         "delete the row for a NOT NULL reference, NULL a nullable one")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    path = args.db
    if not path:
        from app.config import settings
        path = str(settings.abs_db_path)

    conn = _open(path, read_only=not args.fix)
    try:
        before = scan(conn)
        changed: dict[str, int] = {}
        if args.fix:
            if not args.json:
                print(f"Cleaning: {path}")
            changed = fix(conn)
            after = scan(conn)
        else:
            after = before

        protected = [r for r in after if r["protected"]]
        if args.json:
            print(json.dumps({
                "db": path, "fixed": args.fix,
                "before": before, "changed": changed, "remaining": after,
            }, ensure_ascii=False, indent=2))
        else:
            if args.fix:
                nulled = {k: v for k, v in changed.items() if k.endswith("=NULL")}
                deleted = {k: v for k, v in changed.items() if not k.endswith("=NULL")}
                print(f"\nRows deleted: {sum(deleted.values())}"
                      + (f"  ({', '.join(f'{k}={v}' for k, v in sorted(deleted.items()))})"
                         if deleted else ""))
                print(f"References nulled: {sum(nulled.values())}"
                      + (f"  ({', '.join(f'{k}={v}' for k, v in sorted(nulled.items()))})"
                         if nulled else "")
                      + "  — rows kept")
                print("\nRemaining:")
            _print_report(after)
            if protected:
                print("\nReferences to users are not deleted automatically. The "
                      "administrator decides: move them to the canonical account "
                      "(app/services/identity.py) or delete them by hand.")

        if protected:
            return 2
        return 1 if after else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
