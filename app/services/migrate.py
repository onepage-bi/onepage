"""Schema migrations at boot: Alembic, once, under a file lock.

Before this module the lifespan re-ran a wall of `ALTER TABLE` /
`CREATE INDEX IF NOT EXISTS` on every start of every worker and swallowed the
errors. Now the DDL lives in app/migrations/versions and the database records
what it has.

Three shapes of database arrive here, and `run()` tells them apart:

  * **no tables at all** — a fresh install (or a smoke test's temp file).
    `create_all()` builds the current schema and the database is stamped at
    head: there is nothing for a migration to catch up on.
  * **tables, no `alembic_version`** — production as it stands today, built by
    create_all + the boot-time ALTERs of every release since. `create_all()`
    first (it is the only thing that creates tables a newer build added and it
    is a no-op for the ones already there), then stamp 0001 and upgrade: 0002
    checks the inspector for every column and index, so the ALTERs that
    already ran are skipped, not repeated.
  * **`alembic_version` present** — plain `upgrade head`.

The lock: with `OP_WORKERS > 1` uvicorn forks several processes that all run
the lifespan at once, and two concurrent `CREATE INDEX` on the same SQLite file
is at best a `database is locked` and at worst a half-applied revision. One
worker holds an exclusive `flock` and migrates; the others block, then see the
work already done.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

from sqlalchemy import inspect

from app.config import settings
from app.database import Base, engine
from app import models  # noqa: F401 — importing it is what fills Base.metadata

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

# Revision a pre-Alembic production database is stamped with before the
# catch-up runs. See the module docstring.
LEGACY_STAMP = "0001"

# Its presence means "this file already holds a OnePage schema". `users` has
# existed since the first release and no code path creates it on its own.
ANCHOR_TABLE = "users"


def config():
    """An Alembic Config that needs no alembic.ini (the container has none)."""
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # ConfigParser interpolates '%' — a database path may contain one.
    cfg.set_main_option("sqlalchemy.url", str(engine.url).replace("%", "%%"))
    return cfg


def current_revision() -> str | None:
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _pending(cfg, current: str | None) -> list[str]:
    """Revisions that `upgrade head` would apply, oldest first (for the log)."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(cfg)
    revs = list(script.iterate_revisions("heads", current, select_for_downgrade=False))
    return [r.revision for r in reversed(revs)]


def run() -> None:
    """Bring the database to head. Safe to call from every worker."""
    lock_path = settings.abs_db_path.with_name(settings.abs_db_path.name + ".migrate.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _run_locked()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _run_locked() -> None:
    from alembic import command

    cfg = config()
    tables = set(inspect(engine).get_table_names())
    versioned = "alembic_version" in tables
    populated = ANCHOR_TABLE in tables

    if not versioned:
        # create_all is still the thing that CREATES tables, on both paths:
        # for a fresh file it builds everything, for a legacy one it adds the
        # tables newer releases introduced. It never touches a table that
        # exists, which is exactly why 0002 has to fill in the columns.
        Base.metadata.create_all(bind=engine)
        if populated:
            command.stamp(cfg, LEGACY_STAMP)
            print(f"[alembic] existing database adopted at {LEGACY_STAMP}")
        else:
            command.stamp(cfg, "head")
            print(f"[alembic] new database created at head ({current_revision()})")
            return

    before = current_revision()
    pending = _pending(cfg, before)
    if not pending:
        print(f"[alembic] at head ({before})")
        return

    print(f"[alembic] {before} -> head, applying: {', '.join(pending)}")
    command.upgrade(cfg, "head")
    print(f"[alembic] applied {len(pending)} revision(s), now at {current_revision()}")
