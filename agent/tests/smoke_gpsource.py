"""Smoke for the GP executor (gpsource) + credential stores (creds), no psycopg/Postgres.

The fake connection mirrors the psycopg3 COPY API (conn.cursor() -> cur.copy(stmt) ->
iterable chunks; cur.statusmessage; conn.cancel()/close()), so all the
streaming/gzip/parallelism/ticker/truncation/taxonomy/cancel code runs for
real. The real psycopg adapter (_default_connect) is thin and is checked on
a machine (E2E, M2).

Run:  python agent/tests/smoke_gpsource.py
"""
from __future__ import annotations

import gzip
import io
import os
import sys
import threading
import time
import zipfile  # noqa: F401  (kept parallel to other smokes; harmless)
from pathlib import Path

sys.path.insert(0, os.getcwd())
os.environ["OP_AGENT_DEV_CRED_STORE"] = "1"  # enable the non-Windows dev credential store

from agent import creds, gpsource  # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


# --------------------------------------------------------------------------- #
# Fake psycopg3-like connection                                                #
# --------------------------------------------------------------------------- #
class FakeCopy:
    def __init__(self, cur, chunks, rows, barrier):
        self._cur, self._chunks, self._rows, self._barrier = cur, chunks, rows, barrier

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._cur.statusmessage = f"COPY {self._rows}"
        return False

    def __iter__(self):
        if self._barrier is not None:
            self._barrier.wait()          # proof: all workers are here at the same time
        for ch in self._chunks:
            yield ch


class FakeCursor:
    def __init__(self, plan):
        self._plan = plan
        self.statusmessage = None
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def copy(self, stmt):
        chunks, rows, exc, barrier = self._plan(stmt)
        if exc is not None:
            raise exc
        return FakeCopy(self, chunks, rows, barrier)


class FakeConn:
    def __init__(self, plan):
        self._plan = plan
        self.closed = False
        self.cancel_calls = 0

    def cursor(self):
        return FakeCursor(self._plan)

    def cancel(self):
        self.cancel_calls += 1

    def close(self):
        self.closed = True


class PgError(Exception):
    """Fake psycopg exception: carries sqlstate + diag.message_primary."""
    def __init__(self, sqlstate, message_primary=""):
        super().__init__(message_primary or sqlstate)
        self.sqlstate = sqlstate

        class _Diag:
            pass
        self.diag = _Diag()
        self.diag.sqlstate = sqlstate
        self.diag.message_primary = message_primary


def make_connect(plan, track=None):
    """Connection factory for injection into run_sources. `track` is a list that
    collects the created FakeConn objects (to check close/cancel)."""
    def connect(topology, password, app_name, connect_timeout_s, statement_timeout_ms):
        # The password reaches here but leaks NOWHERE - checked separately.
        c = FakeConn(plan)
        c._app_name = app_name
        c._password_seen = password
        if track is not None:
            track.append(c)
        return c
    return connect


def write_sql(sql_dir: Path, rel: str, marker: str, body: str = "SELECT 1") -> None:
    p = sql_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{body} -- MARK:{marker}\n;\n", encoding="utf-8")  # trailing ; on purpose


def collector():
    evs: list[dict] = []
    lock = threading.Lock()

    def emit(ev):
        with lock:
            evs.append(dict(ev))
    return evs, emit


TMP = Path(os.environ.get("TMPDIR", "/tmp")) / f"op-gp-smoke-{os.getpid()}"


def main() -> None:
    TMP.mkdir(parents=True, exist_ok=True)

    # ── creds: dev round-trip + no plaintext on disk ─────────────────────────
    cred_root = TMP / "creds_root"
    store = creds.CredStore(root=cred_root)
    secret = "P@ss-\u03c0\u03b1\u03c3\u03c3-\U0001f512-\u201cquotes\u201d"
    check("creds: get before set -> None", store.get_password("analytics_dw") is None)
    store.set_password("analytics_dw", secret)
    check("creds: round-trip get == set", store.get_password("analytics_dw") == secret,
          repr(store.get_password("analytics_dw")))
    check("creds: has_password True", store.has_password("analytics_dw") is True)
    blob = (cred_root / "creds" / "analytics_dw.bin").read_bytes()
    check("creds: plaintext NOT on disk", secret.encode("utf-8") not in blob)
    check("creds: entropy binds connection (other name -> None)",
          store.get_password("other_conn") is None)
    store.delete_password("analytics_dw")
    check("creds: delete -> get None", store.get_password("analytics_dw") is None)
    for bad in ("../evil", "a/b", "a\\b"):
        try:
            store.get_password(bad)
            check(f"creds: reject bad name {bad!r}", False, "no error raised")
        except creds.CredError:
            check(f"creds: reject bad name {bad!r}", True)

    # ── gpsource happy path: 2 queries, streaming into gzip, server rowcount ─
    sql_dir = TMP / "bundle"
    out_dir = TMP / "out"
    write_sql(sql_dir, "sql/01_head.sql", "q1")
    write_sql(sql_dir, "sql/04_rej.sql", "q2")

    csv1 = b"a,b\n1,2\n3,4\n5,6\n"          # 3 data rows
    csv2 = b"x\n" + b"".join(b"%d\n" % i for i in range(10))  # 10 rows

    def plan_ok(stmt):
        if "MARK:q1" in stmt:
            # memoryview chunks - exercises the memoryview->bytes path
            chunks = [memoryview(csv1)[i:i + 5] for i in range(0, len(csv1), 5)]
            return chunks, 3, None, None
        if "MARK:q2" in stmt:
            chunks = [csv2[i:i + 7] for i in range(0, len(csv2), 7)]
            return chunks, 10, None, None
        return [b""], 0, None, None

    spec = {
        "type": "greenplum", "connection": "analytics_dw", "parallelism": 2,
        "statement_timeout_ms": 300000,
        "queries": [
            {"sql_file": "sql/01_head.sql", "output_name": "slot_0.csv.gz", "format": "csv_gz"},
            {"sql_file": "sql/04_rej.sql", "output_name": "slot_2.csv.gz", "format": "csv_gz"},
        ],
    }
    evs, emit = collector()
    track: list = []
    res = gpsource.run_sources(
        spec, {"host": "h", "port": "5432", "dbname": "d", "user": "a.ivanova"},
        "SECRET-PW", "op-agent/0.2 gp-delta@1", sql_dir, out_dir, emit,
        cancel=lambda: False, connect=make_connect(plan_ok, track), tick_interval_s=0.0)

    check("gp: 2 query results", len(res.queries) == 2, str(res.queries.keys()))
    check("gp: rows from server COPY status (q1=3)", res.queries["slot_0.csv.gz"].rows == 3,
          str(res.queries["slot_0.csv.gz"]))
    check("gp: rows from server COPY status (q2=10)", res.queries["slot_2.csv.gz"].rows == 10)
    check("gp: total_rows aggregate", res.total_rows == 13, str(res.total_rows))
    # gzip integrity: decompress and compare with the original bytes
    got1 = gzip.decompress((out_dir / "slot_0.csv.gz").read_bytes())
    got2 = gzip.decompress((out_dir / "slot_2.csv.gz").read_bytes())
    check("gp: gzip q1 decompresses to exact bytes", got1 == csv1, repr(got1))
    check("gp: gzip q2 decompresses to exact bytes", got2 == csv2, repr(got2))
    check("gp: gz_bytes recorded > 0", res.queries["slot_0.csv.gz"].gz_bytes > 0)
    check("gp: connections closed", all(c.closed for c in track) and len(track) == 2,
          f"{[c.closed for c in track]}")
    check("gp: application_name carried personal login",
          all("gp-delta@1" in c._app_name for c in track))
    # ticker: rows/sec events appeared (tick_interval=0 -> on every chunk after the first)
    ticks = [e for e in evs if e.get("stage") == "copy" and "rows_per_s" in e]
    check("gp: rows/sec ticker emitted", len(ticks) >= 1, f"{len(ticks)} ticks")
    dones = [e for e in evs if e.get("phase") == "done"]
    check("gp: done events per query", len(dones) == 2, str(len(dones)))

    # ── server rowcount WINS over newline counting (embedded \n) ───────────
    sql_dir2 = TMP / "bundle2"
    write_sql(sql_dir2, "sql/nl.sql", "qnl")
    csv_nl = b'a\n"line1\nline2"\n'          # 1 data row, but 3 newlines
    spec_nl = {"type": "greenplum", "connection": "c", "parallelism": 1,
               "queries": [{"sql_file": "sql/nl.sql", "output_name": "o.csv.gz", "format": "csv_gz"}]}
    evs2, emit2 = collector()
    res_nl = gpsource.run_sources(
        spec_nl, {}, "pw", "app", sql_dir2, TMP / "out2", emit2, lambda: False,
        connect=make_connect(lambda s: ([csv_nl], 1, None, None)), tick_interval_s=0.0)
    check("gp: server rowcount beats newline count (rows=1, not 2)",
          res_nl.queries["o.csv.gz"].rows == 1, str(res_nl.queries["o.csv.gz"].rows))

    # ── truncation detection: rows == limit_declared -> warning ─────────────
    sql_dir3 = TMP / "bundle3"
    write_sql(sql_dir3, "sql/lim.sql", "qlim")
    csv_lim = b"h\n" + b"".join(b"%d\n" % i for i in range(200))
    spec_lim = {"type": "greenplum", "connection": "c", "parallelism": 1,
                "queries": [{"sql_file": "sql/lim.sql", "output_name": "slot_2.csv.gz",
                             "format": "csv_gz", "limit_declared": 200}]}
    evs3, emit3 = collector()
    gpsource.run_sources(spec_lim, {}, "pw", "app", sql_dir3, TMP / "out3", emit3,
                         lambda: False, connect=make_connect(lambda s: ([csv_lim], 200, None, None)),
                         tick_interval_s=0.0)
    warns = [e for e in evs3 if e.get("t") == "warning" and e.get("kind") == "truncated"]
    check("gp: truncation warning when rows==limit", len(warns) == 1 and warns[0]["rows"] == 200,
          str(warns))

    # no truncation: rows < limit -> no warning
    spec_ok = {**spec_lim, "queries": [{**spec_lim["queries"][0], "limit_declared": 5000}]}
    evs3b, emit3b = collector()
    gpsource.run_sources(spec_ok, {}, "pw", "app", sql_dir3, TMP / "out3b", emit3b,
                         lambda: False, connect=make_connect(lambda s: ([csv_lim], 200, None, None)),
                         tick_interval_s=0.0)
    check("gp: no truncation warning when rows<limit",
          not any(e.get("kind") == "truncated" for e in evs3b))

    # ── error taxonomy: sqlstate -> kind, the whole run fails ───────────────
    sql_dir4 = TMP / "bundle4"
    write_sql(sql_dir4, "sql/a.sql", "qa")
    write_sql(sql_dir4, "sql/b.sql", "qb")
    cases = [
        ("28P01", "gp_auth", "password authentication failed for user u10000009"),
        ("57014", "gp_timeout", ""),
        ("08006", "gp_network", ""),
        ("42601", "gp_query", "syntax error near FROM"),
    ]
    for sqlstate, want_kind, srvtext in cases:
        spec4 = {"type": "greenplum", "connection": "c", "parallelism": 2,
                 "queries": [
                     {"sql_file": "sql/a.sql", "output_name": "a.csv.gz", "format": "csv_gz"},
                     {"sql_file": "sql/b.sql", "output_name": "b.csv.gz", "format": "csv_gz"},
                 ]}
        evs4, emit4 = collector()

        def plan_err(stmt, _ss=sqlstate, _txt=srvtext):
            if "MARK:qb" in stmt:
                return None, 0, PgError(_ss, _txt), None
            return [b"h\n1\n"], 1, None, None

        raised = None
        try:
            gpsource.run_sources(spec4, {}, "pw", "app", sql_dir4, TMP / f"out4-{sqlstate}",
                                 emit4, lambda: False, connect=make_connect(plan_err),
                                 tick_interval_s=0.0)
        except gpsource.GpError as e:
            raised = e
        errev = [e for e in evs4 if e.get("t") == "error"]
        check(f"gp: {sqlstate} -> raises GpError kind={want_kind}",
              raised is not None and raised.kind == want_kind,
              f"raised={raised and raised.kind}")
        check(f"gp: {sqlstate} -> error event kind={want_kind}",
              len(errev) == 1 and errev[0]["kind"] == want_kind, str(errev))
        if want_kind == "gp_auth":
            # Policy changed after the first live run: the server text is
            # PASSED THROUGH - without it a "connection failure" cannot be diagnosed.
            check("gp: auth error carries diagnosable server text",
                  raised is not None and "authentication failed" in raised.message,
                  raised and raised.message)
        if want_kind == "gp_query":
            check("gp: query error carries sanitized server hint",
                  raised is not None and "syntax error" in raised.message, raised and raised.message)

    # the password is scrubbed from ANY exception text, even if conninfo leaked into it
    kind_l, msg_l = gpsource._classify(
        Exception("connection failed: host=warehouse.example.com port=6432 "
                  "password=hunter2 sslmode=prefer"))
    check("gp: password scrubbed from diagnostics",
          "hunter2" not in msg_l and "***" in msg_l, msg_l)
    check("gp: connect failure keeps its real text (diagnosable)",
          "warehouse.example.com" in msg_l and kind_l == "gp_network", f"{kind_l}: {msg_l}")

    # ── parallelism: 3 queries, Barrier(3) - if serial, the barrier times out ──
    sql_dir5 = TMP / "bundle5"
    for n in range(3):
        write_sql(sql_dir5, f"sql/p{n}.sql", f"qp{n}")
    barrier = threading.Barrier(3, timeout=5)
    spec5 = {"type": "greenplum", "connection": "c", "parallelism": 3,
             "queries": [{"sql_file": f"sql/p{n}.sql", "output_name": f"p{n}.csv.gz",
                          "format": "csv_gz"} for n in range(3)]}

    def plan_par(stmt):
        return [b"h\n1\n2\n"], 2, None, barrier

    evs5, emit5 = collector()
    t0 = time.monotonic()
    try:
        res5 = gpsource.run_sources(spec5, {}, "pw", "app", sql_dir5, TMP / "out5", emit5,
                                    lambda: False, connect=make_connect(plan_par),
                                    tick_interval_s=0.0)
        check("gp: parallelism=3 runs concurrently (barrier cleared)", len(res5.queries) == 3,
              str(res5.queries.keys()))
    except threading.BrokenBarrierError:
        check("gp: parallelism=3 runs concurrently (barrier cleared)", False,
              "barrier timed out -> queries ran serially")
    check("gp: parallel run was fast (<5s)", time.monotonic() - t0 < 5)

    # ── cancel: cancel() True -> RunCanceled + PQcancel to live connections ──
    sql_dir6 = TMP / "bundle6"
    write_sql(sql_dir6, "sql/c.sql", "qc")
    spec6 = {"type": "greenplum", "connection": "c", "parallelism": 1,
             "queries": [{"sql_file": "sql/c.sql", "output_name": "c.csv.gz", "format": "csv_gz"}]}

    def plan_slow(stmt):
        # many small chunks - the _gzip_stream loop will get to notice cancel()
        return [b"row\n"] * 100000, 100000, None, None

    evs6, emit6 = collector()

    # The cancel arrives on the fifth POLL, not "after 20 ms" by a timer. A
    # timer made the test a race against the hardware: on macos-14 (Apple
    # Silicon) a hundred thousand tiny chunks compressed faster than the
    # thread could set the flag, and the stream finished before the cancel -
    # which is how the public repository's CI failed. A poll counter does not
    # depend on speed: however much a chunk costs, the fifth cancel() call
    # always happens in the middle of the stream.
    polls = {"n": 0}

    def cancel_on_fifth_poll():
        polls["n"] += 1
        return polls["n"] >= 5

    canceled = False
    try:
        gpsource.run_sources(spec6, {}, "pw", "app", sql_dir6, TMP / "out6", emit6,
                             cancel_on_fifth_poll, connect=make_connect(plan_slow),
                             tick_interval_s=0.0)
    except gpsource.RunCanceled:
        canceled = True
    check("gp: cancel -> RunCanceled", canceled)

    # ── guards are transactional (safe through a pgbouncer pool on 6432) ────
    executed: list[str] = []

    class GuardCursor(FakeCursor):
        def execute(self, sql, *a):
            executed.append(sql)

    class GuardConn(FakeConn):
        def cursor(self):
            return GuardCursor(self._plan)

    def guard_connect(topology, password, app_name, ct, st):
        return GuardConn(lambda stmt: ([b"h\n1\n"], 1, None, None))

    sql_dir7 = TMP / "bundle7"
    write_sql(sql_dir7, "sql/g.sql", "qg")
    spec7 = {"type": "greenplum", "connection": "c", "parallelism": 1,
             "statement_timeout_ms": 45000,
             "queries": [{"sql_file": "sql/g.sql", "output_name": "g.csv.gz", "format": "csv_gz"}]}
    evs7, emit7 = collector()
    gpsource.run_sources(spec7, {}, "pw", "app", sql_dir7, TMP / "out7", emit7,
                         lambda: False, connect=guard_connect, tick_interval_s=0.0)
    check("gp: READ ONLY is the FIRST statement of the transaction",
          bool(executed) and executed[0] == "SET TRANSACTION READ ONLY", str(executed))
    check("gp: statement_timeout applied via SET LOCAL (transaction-scoped)",
          "SET LOCAL statement_timeout = 45000" in executed, str(executed))
    check("gp: no idle_in_transaction guard (absent in GreenPlum 6)",
          not any("idle_in_transaction" in s for s in executed), str(executed))

    # ── _build_copy_sql strips the trailing ; and wraps in COPY ──────────────
    built = gpsource._build_copy_sql("SELECT 1 FROM t ;  \n ; ")
    check("gp: build_copy strips trailing ';'", ";" not in built.split("TO STDOUT")[0].rstrip()[-3:]
          and built.startswith("COPY (") and "TO STDOUT (FORMAT csv, HEADER)" in built, built)

    # ── path traversal in sql_file is blocked ────────────────────────────────
    try:
        gpsource._read_sql(sql_dir6, "../secret.sql")
        check("gp: _read_sql blocks traversal", False, "no error")
    except gpsource.GpError:
        check("gp: _read_sql blocks traversal", True)

    print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURES: {FAILURES}"))
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
