from __future__ import annotations

from time import perf_counter

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _get_engine():
    db_path = settings.abs_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )


engine = _get_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# ---------------------------------------------------------------------------
# SQLite tuning, applied to every new connection.
# WAL lets readers proceed during writes (no more reader-blocks-writer stalls);
# synchronous=NORMAL is the recommended pairing with WAL (fsync on checkpoint,
# not on every commit). busy_timeout retries instead of instantly raising
# "database is locked".
#
# foreign_keys (2026-09-06): SQLite's default is OFF and this app ran that way
# for its whole life, so the pragma is NOT set here unconditionally — it is
# armed by enable_foreign_keys() from the lifespan, AFTER migrations. Two
# reasons for the delay: Alembic's SQLite batch ALTER rebuilds a table
# (create-copy-drop-rename) and enforcement during the drop would refuse the
# migration, and the data migrations that follow it read legacy rows that were
# written with no enforcement at all. From that point on every new connection
# gets `PRAGMA foreign_keys=ON` — the pragma is per-connection, never stored
# in the file. Rows already in the file are NOT re-checked by SQLite: run
# tools/fk_orphans.py on a copy before deploying (docs/DEPLOY.md §2d).
# ---------------------------------------------------------------------------

_fk_enforced = False


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA cache_size=-64000")      # 64 MB page cache
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.execute("PRAGMA mmap_size=268435456")    # 256 MB
    if _fk_enforced:
        cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def enable_foreign_keys() -> None:
    """Enforce foreign keys on every connection from here on.

    `dispose()` and not a loop over the pool: connections checked out during
    the migration were opened with enforcement off and there is no way to
    reach them through the pool's public API. Dropping the pool is free at
    boot (nothing is in flight) and guarantees that no connection with the old
    pragma survives. Idempotent."""
    global _fk_enforced
    if _fk_enforced:
        return
    _fk_enforced = True
    engine.dispose()


# ---------------------------------------------------------------------------
# Per-request SQL timing → perflog (mutates the request's perf record in place;
# no-op when there is no active request record or perf is disabled)
# ---------------------------------------------------------------------------

if settings.perf_enabled:
    from app.services import perflog

    @event.listens_for(engine, "before_cursor_execute")
    def _sql_timer_start(conn, cursor, statement, parameters, context, executemany):
        context._op_t0 = perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _sql_timer_stop(conn, cursor, statement, parameters, context, executemany):
        rec = perflog.request_record.get()
        if rec is None:
            return
        dur_ms = (perf_counter() - getattr(context, "_op_t0", perf_counter())) * 1000
        rec["db_n"] = rec.get("db_n", 0) + 1
        rec["db_ms"] = rec.get("db_ms", 0.0) + dur_ms
        if settings.perf_sql:
            rec.setdefault("sql", []).append([statement[:200], round(dur_ms, 3)])


# Async generator on purpose: FastAPI runs sync generator deps through the
# threadpool (an extra hop per request); creating/closing a Session is sub-ms
# and safe in the event loop (queries themselves are fast SQLite reads —
# heavy work in this app always runs via run_in_threadpool).
async def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables() -> None:
    Base.metadata.create_all(bind=engine)
