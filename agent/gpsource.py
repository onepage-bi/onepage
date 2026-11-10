"""The agent's GreenPlum executor - the "wow" core: frozen SQL -> csv.gz, fast.

For each query in `sources` (frozen in the immutable version, see
genmanifest) the agent runs `COPY (<SELECT>) TO STDOUT (FORMAT csv, HEADER)`
and STREAMS the output into gzip on disk - O(chunk) memory, no DataFrame, no
JSON.parse. N parallel connections (ceiling from the manifest, default 3):
both psycopg on the network read and gzip on compression RELEASE the GIL, so
the parallelism is real. The live rows/sec + MB/s ticker is the demo effect;
the row counter for the ticker is `bytes.count(b"\\n")` (no CSV parsing),
while the TRUE row count used for truncation detection comes from the
server's own COPY status (`COPY N`), not from newline counting.

Security invariants:
  * The password NEVER reaches events, logs or error messages.
  * The session is read-only: `default_transaction_read_only=on`,
    `statement_timeout`, `idle_in_transaction_session_timeout`;
    `application_name` carries the personal login -> the GP audit attributes
    the query to a specific analyst (matters: tax-ID fields).
  * Failure taxonomy (gp_auth/gp_network/gp_timeout/gp_query) - diagnosis
    cards; the server text is passed through only for gp_query/gp_timeout
    (about the SQL itself, not the data) and never for auth/network.

psycopg is imported LAZILY inside `_default_connect` - the module loads and
is tested where psycopg is absent (inject `connect=` into run_sources).
"""
from __future__ import annotations

import concurrent.futures
import gzip
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

GZIP_LEVEL = 4              # level 4: best throughput/ratio for csv (the G1 measurement will tune it)
CONNECT_TIMEOUT_S = 15
TICK_INTERVAL_S = 0.25     # refresh rate of the live rows/sec ticker

_AUTH_SQLSTATES = {"28000", "28P01"}


class GpError(Exception):
    """An executor failure with a taxonomy kind. `message` NEVER carries the password."""
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class RunCanceled(Exception):
    """The run was canceled by the user (DELETE /runs/{id})."""


@dataclass(slots=True)
class QueryResult:
    name: str
    rows: int
    raw_bytes: int
    gz_bytes: int
    truncated: bool
    elapsed_ms: int


@dataclass(slots=True)
class SourcesResult:
    queries: dict[str, QueryResult]
    total_rows: int
    total_gz_bytes: int


# --------------------------------------------------------------------------- #
# Pure helpers (tested without psycopg)                                        #
# --------------------------------------------------------------------------- #
def _read_sql(sql_dir: Path, rel: str) -> str:
    """Read a frozen SQL file from the unpacked bundle without letting the
    path leave the directory (server validation + freezing + this barrier)."""
    root = sql_dir.resolve()
    path = (root / rel).resolve()
    if path != root and root not in path.parents:
        raise GpError("gp_query", f"sql_file outside the bundle: {rel!r}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise GpError("gp_query", f"cannot read sql_file {rel!r}: {e.strerror}")


def _build_copy_sql(inner_sql: str) -> str:
    """Wrap the frozen SELECT in COPY ... TO STDOUT. Strip a trailing `;`,
    otherwise `COPY (SELECT ...;) ...` is a syntax error."""
    s = inner_sql.strip()
    while s.endswith(";"):
        s = s[:-1].rstrip()
    return f"COPY (\n{s}\n) TO STDOUT (FORMAT csv, HEADER)"


def _copy_rowcount(cur, fallback: int) -> int:
    """The true row count from the server's COPY status (`COPY N`), then
    rowcount, and only last the estimate from newline counting."""
    msg = getattr(cur, "statusmessage", None)
    if isinstance(msg, str) and msg.upper().startswith("COPY "):
        parts = msg.split()
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1])
    rc = getattr(cur, "rowcount", None)
    if isinstance(rc, int) and rc >= 0:
        return rc
    return fallback


def _gzip_stream(chunks: Iterable, out_path: Path, cancel: Callable[[], bool],
                 tick: Callable[[int, int], None],
                 gzip_level: int = GZIP_LEVEL,
                 tick_interval_s: float = TICK_INTERVAL_S) -> tuple[int, int, int]:
    """Drain COPY chunks into a gzip file, O(chunk) memory. Returns
    (raw_bytes, lines, gz_bytes). Exactly ONE memoryview->bytes copy per
    chunk - it feeds both gzip and the line count (bytes.count is a fast
    memchr)."""
    raw = 0
    lines = 0
    last = time.monotonic()
    with open(out_path, "wb") as f:
        gz = gzip.GzipFile(fileobj=f, mode="wb", compresslevel=gzip_level, mtime=0)
        try:
            for chunk in chunks:
                if cancel():
                    raise RunCanceled()
                b = chunk if type(chunk) is bytes else bytes(chunk)
                gz.write(b)
                raw += len(b)
                lines += b.count(b"\n")
                now = time.monotonic()
                if now - last >= tick_interval_s:
                    tick(raw, lines)
                    last = now
        finally:
            gz.close()
    return raw, lines, out_path.stat().st_size


_PW_RE = re.compile(r"(password\s*=\s*)\S+", re.I)


def _safe_msg(exc: Exception, limit: int = 300) -> str:
    """Exception text for the diagnosis card, with the password scrubbed.

    libpq messages about a failed connect do not contain the password (they
    have host, port and reason), but conninfo could in theory end up in the
    text - so `password=...` is masked just in case. WITHOUT this text the
    diagnosis is unreadable: the first live run returned "connection failure
    (OperationalError)" and said nothing else.
    """
    txt = _PW_RE.sub(r"\1***", str(exc) or "")
    return " ".join(txt.split())[:limit]


def _classify(exc: Exception) -> tuple[str, str]:
    """psycopg exception -> (kind, message).

    The exception text is ALWAYS passed through (sanitised): without it
    failures are indistinguishable. These messages contain no password - it
    is part of neither the query nor libpq diagnostics; `_safe_msg` is an
    extra safety net.
    """
    sqlstate = getattr(exc, "sqlstate", None)
    diag = getattr(exc, "diag", None)
    if sqlstate is None and diag is not None:
        sqlstate = getattr(diag, "sqlstate", None)
    detail = _safe_msg(exc)

    if sqlstate in _AUTH_SQLSTATES:
        return "gp_auth", f"GreenPlum rejected the login or password: {detail}"
    if sqlstate == "57014":
        return "gp_timeout", f"query exceeded statement_timeout: {detail}"
    if isinstance(sqlstate, str) and sqlstate.startswith("08"):
        return "gp_network", f"cannot connect to GreenPlum: {detail}"
    if sqlstate:
        m = getattr(diag, "message_primary", None) if diag is not None else None
        return "gp_query", "query error: " + (str(m)[:200] if m else detail)
    # no sqlstate - a failure at connect/socket level (DNS, refusal, timeout, TLS, pool)
    return "gp_network", f"connection failure ({type(exc).__name__}): {detail}"


# --------------------------------------------------------------------------- #
# Real connection (the only psycopg-specific code)                             #
# --------------------------------------------------------------------------- #
def _default_connect(topology: dict, password: str, app_name: str,
                     connect_timeout_s: int, statement_timeout_ms: int):
    """Connection to GreenPlum. IMPORTANT: session guards are NOT set here.

    The prod entry point is often not GP itself but a pool (pgbouncer/odyssey,
    typical port 6432). Through a pool the `options` startup parameter may be
    rejected, and a session-level `SET` in transaction mode lands on another
    server connection - so read-only/timeout would silently not apply to our
    COPY. Hence the guards are set TRANSACTIONALLY, next to the COPY
    (`_apply_guards`): that is correct both directly against GP and through
    any pool. Hence autocommit=False - the first SET opens the transaction
    and it covers the COPY.
    """
    import psycopg  # lazy import: the module loads without the driver

    kwargs = dict(
        host=topology.get("host"),
        port=topology.get("port"),
        dbname=topology.get("dbname"),
        user=topology.get("user"),
        password=password,
        autocommit=False,                # transaction BEGIN...COPY... (rollback on close)
        connect_timeout=connect_timeout_s,
    )
    # Optional libpq parameters from agent.ini. `gssencmode=disable` is often
    # required when the entry point is a pool: libpq 12+ tries GSSAPI
    # encryption by default, and the pool drops the connection without
    # replying with an error. `op_agent.py test-conn` helps find a working
    # combination.
    for key in ("sslmode", "gssencmode"):
        value = topology.get(key)
        if value:
            kwargs[key] = value
    # `application_name = off` in agent.ini pins the label-less mode: then we
    # do not pay an extra TCP handshake on EVERY connection (with N parallel -
    # N extra). Set according to the `test-conn` result.
    if str(topology.get("application_name", "")).strip().lower() in ("off", "no", "false", "0"):
        return psycopg.connect(**kwargs)
    try:
        # application_name - so the run is identifiable in the GP audit.
        return psycopg.connect(application_name=app_name, **kwargs)
    except psycopg.OperationalError:
        # Pools are strict about startup parameters. Odyssey (measured on prod
        # on 22.07) does NOT reply with an error to an unknown parameter but
        # silently drops the socket - so checking the exception text is
        # useless, we always retry. Attribution is not lost: identity comes
        # from the login itself (current_user), application_name is only a
        # label on top of it.
        return psycopg.connect(**kwargs)


def probe(topology: dict, password: str, app_name: str = "op-agent/probe",
          connect_timeout_s: int = CONNECT_TIMEOUT_S, connect=None) -> dict:
    """Check the connection: {user, db, version}. Exceptions propagate.

    It lives here, not in the UI, so that "check connection" from the tray
    menu and the console `test-conn` take ONE path: a divergence between them
    would mean the button checks something other than what the run later does.
    """
    conn = (connect or _default_connect)(topology, password, app_name,
                                         connect_timeout_s, 30_000)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, current_database(), version()")
            row = cur.fetchone() or ("", "", "")
        try:
            conn.rollback()      # autocommit=False: do not leave a transaction open
        except Exception:        # noqa: BLE001
            pass
        return {"user": row[0], "db": row[1], "version": row[2]}
    finally:
        try:
            conn.close()
        except Exception:        # noqa: BLE001
            pass


def _apply_guards(cur, statement_timeout_ms: int) -> None:
    """Transactional guards before COPY: read-only + statement_timeout.

    `SET TRANSACTION READ ONLY` must be the first statement of the
    transaction, so it is called before any query. `SET LOCAL` lives until the
    end of the transaction - i.e. exactly for our COPY, regardless of the pool
    mode. We deliberately do NOT touch `idle_in_transaction_session_timeout`:
    GreenPlum 6 (PG 9.4 core) has no such parameter, a failing SET would abort
    the transaction, and we have no "idle in transaction" anyway - between
    BEGIN and COMMIT there is only the COPY.

    Fake cursors in tests have no execute - the guards are then skipped (a
    real psycopg cursor always has it).
    """
    execute = getattr(cur, "execute", None)
    if execute is None:
        return
    execute("SET TRANSACTION READ ONLY")
    execute(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}")


# --------------------------------------------------------------------------- #
# A single query                                                                #
# --------------------------------------------------------------------------- #
def _run_one(query: dict, conn, cancel: Callable[[], bool],
             emit: Callable[[dict], None], out_dir: Path, sql_dir: Path,
             gzip_level: int, tick_interval_s: float,
             statement_timeout_ms: int) -> QueryResult:
    name = query["output_name"]
    limit = query.get("limit_declared")
    stmt = _build_copy_sql(_read_sql(sql_dir, query["sql_file"]))
    out_path = out_dir / name

    emit({"t": "progress", "stage": "copy", "output": name, "phase": "start"})
    t0 = time.monotonic()
    sample = {"t": t0, "raw": 0, "lines": 0}

    def tick(raw: int, lines: int) -> None:
        now = time.monotonic()
        dt = now - sample["t"] or 1e-6
        rps = (lines - sample["lines"]) / dt
        mbps = (raw - sample["raw"]) / dt / 1e6
        sample.update(t=now, raw=raw, lines=lines)
        emit({"t": "progress", "stage": "copy", "output": name,
              "rows": max(0, lines - 1), "rows_per_s": round(rps),
              "bytes": raw, "mb_per_s": round(mbps, 1)})

    with conn.cursor() as cur:
        _apply_guards(cur, statement_timeout_ms)
        with cur.copy(stmt) as copy:
            raw, lines, gz = _gzip_stream(copy, out_path, cancel, tick,
                                          gzip_level, tick_interval_s)
        rows = _copy_rowcount(cur, fallback=max(0, lines - 1))

    elapsed_ms = round((time.monotonic() - t0) * 1000)
    truncated = limit is not None and rows == limit
    emit({"t": "progress", "stage": "copy", "output": name, "phase": "done",
          "rows": rows, "bytes": raw, "gz_bytes": gz, "elapsed_ms": elapsed_ms})
    if truncated:
        emit({"t": "warning", "kind": "truncated", "output": name,
              "rows": rows, "limit": limit,
              "msg": (f"{name}: got exactly {rows} rows = LIMIT {limit}; "
                      f"the data may be truncated")})
    return QueryResult(name, rows, raw, gz, truncated, elapsed_ms)


# --------------------------------------------------------------------------- #
# N-way parallel orchestration                                                  #
# --------------------------------------------------------------------------- #
def run_sources(spec: dict, topology: dict, password: str, app_name: str,
                sql_dir: Path, out_dir: Path,
                emit: Callable[[dict], None], cancel: Callable[[], bool],
                *, connect: Callable | None = None,
                tick_interval_s: float = TICK_INTERVAL_S) -> SourcesResult:
    """Execute the whole `sources` block. Any query failure fails the whole
    run (the report needs all slots) - GpError with a taxonomy kind.
    Cancel -> RunCanceled."""
    connect = connect or _default_connect
    queries = list(spec["queries"])
    parallelism = max(1, min(int(spec.get("parallelism", 3)), len(queries)))
    stmt_timeout = int(spec.get("statement_timeout_ms", 300000))
    out_dir.mkdir(parents=True, exist_ok=True)

    emit({"t": "stage", "stage": "sql", "queries": len(queries),
          "parallelism": parallelism})

    results: dict[str, QueryResult] = {}
    errors: list[tuple[str, str, str]] = []      # (kind, output, message)
    live: dict[int, object] = {}
    live_lock = threading.Lock()

    def worker(query: dict) -> None:
        name = query["output_name"]
        conn = None
        try:
            if cancel():
                raise RunCanceled()
            conn = connect(topology, password, app_name,
                           CONNECT_TIMEOUT_S, stmt_timeout)
            with live_lock:
                live[id(conn)] = conn
            results[name] = _run_one(query, conn, cancel, emit,
                                     out_dir, sql_dir, GZIP_LEVEL,
                                     tick_interval_s, stmt_timeout)
        except RunCanceled:
            errors.append(("canceled", name, "Canceled by user"))
        except GpError as e:
            errors.append((e.kind, name, e.message))
        except Exception as e:                    # noqa: BLE001 - into the taxonomy, not a crash
            kind, msg = _classify(e)
            errors.append((kind, name, msg))
        finally:
            if conn is not None:
                with live_lock:
                    live.pop(id(conn), None)
                try:
                    conn.close()
                except Exception:
                    pass

    # Cancel monitor: actively sends PQcancel to live connections (a query
    # hanging on the server with no chunks will never check cancel() itself).
    stop = threading.Event()

    def monitor() -> None:
        while not stop.wait(0.1):
            if cancel():
                with live_lock:
                    conns = list(live.values())
                for c in conns:
                    try:
                        c.cancel()
                    except Exception:
                        pass
                return

    mon = threading.Thread(target=monitor, name="gp-cancel", daemon=True)
    mon.start()
    try:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=parallelism, thread_name_prefix="gp") as ex:
            list(ex.map(worker, queries))       # worker swallows everything -> map will not raise
    finally:
        stop.set()

    if errors:
        # canceled takes priority - if it was canceled, that is the cause.
        errors.sort(key=lambda e: 0 if e[0] == "canceled" else 1)
        kind, name, msg = errors[0]
        emit({"t": "error", "kind": kind, "message": msg, "output": name})
        if kind == "canceled":
            raise RunCanceled()
        raise GpError(kind, msg)

    total_rows = sum(r.rows for r in results.values())
    total_gz = sum(r.gz_bytes for r in results.values())
    return SourcesResult(results, total_rows, total_gz)
