"""End-to-end smoke of the M2 spine: run_generator (agent/runs.py) as a whole.

A real generator subprocess (the reference `passthrough`) + a fake GP connect:
bundle -> COPY->csv.gz in inbox/ -> `python -m passthrough` -> slots+HTML in out/ ->
registering outputs -> the done event. Plus the failure taxonomy of the agent spine
(cred_missing / gp_config / gen_crash / gen_no_result).

Run:  python agent/tests/smoke_runflow.py
"""
from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
os.environ["OP_AGENT_DEV_CRED_STORE"] = "1"

from agent import creds, runs  # noqa: E402
from agent.config import Config  # noqa: E402

FAILURES: list[str] = []
REF_PASSTHROUGH = Path("docs/reference_generators/passthrough/passthrough.py").resolve()


def check(label: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


# ── fake psycopg3-like connection (as in smoke_gpsource) ─────────────────────
class _Copy:
    def __init__(self, cur, chunks, rows):
        self.cur, self.chunks, self.rows = cur, chunks, rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.cur.statusmessage = f"COPY {self.rows}"
        return False

    def __iter__(self):
        yield from self.chunks


class _Cursor:
    def __init__(self, plan):
        self.plan = plan
        self.statusmessage = None
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def copy(self, stmt):
        chunks, rows = self.plan(stmt)
        return _Copy(self, chunks, rows)


class _Conn:
    def __init__(self, plan):
        self.plan = plan
        self.closed = False

    def cursor(self):
        return _Cursor(self.plan)

    def cancel(self):
        pass

    def close(self):
        self.closed = True


CSV0 = b"a,b\n1,2\n3,4\n"                                   # 2 rows
CSV2 = b"x\n" + b"".join(b"%d\n" % i for i in range(5))    # 5 rows


def gp_plan(stmt):
    if "MARK:q0" in stmt:
        return [CSV0], 2
    if "MARK:q2" in stmt:
        return [CSV2], 5
    return [b""], 0


def fake_connect(topology, password, app_name, connect_timeout_s, statement_timeout_ms):
    assert password == "SECRET-PW"          # the password reaches the executor
    return _Conn(gp_plan)


def make_bundle(*, entry_module="passthrough", with_template=True,
                extra_files=None, manifest_override=None) -> tuple[bytes, str]:
    import hashlib
    manifest = manifest_override or {
        "name": "gp-delta", "version": "1.0",
        "entrypoint": {"module": entry_module}, "engine": "agent",
        "outputs": {"html": True, "slots": [
            {"index": 0, "name": "slot_0.csv.gz"},
            {"index": 2, "name": "slot_2.csv.gz"}]},
        "sources": {"type": "greenplum", "connection": "analytics_dw", "parallelism": 2,
                    "queries": [
                        {"sql_file": "sql/q0.sql", "output_name": "slot_0.csv.gz", "format": "csv_gz"},
                        {"sql_file": "sql/q2.sql", "output_name": "slot_2.csv.gz",
                         "format": "csv_gz", "limit_declared": 200000}]},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        z.writestr("sql/q0.sql", "SELECT 0 -- MARK:q0\n;\n")
        z.writestr("sql/q2.sql", "SELECT 2 -- MARK:q2\n;\n")
        if with_template:
            z.writestr("template.html", "<html><body>DELTA report</body></html>")
        if entry_module == "passthrough":
            z.writestr("passthrough.py", REF_PASSTHROUGH.read_text(encoding="utf-8"))
        for name, content in (extra_files or {}).items():
            z.writestr(name, content)
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def run_once(tmp: Path, bundle: bytes, checksum: str, *, with_password=True,
             with_topology=True, connect=fake_connect):
    conns = {}
    if with_topology:
        conns = {"analytics_dw": {"host": "gp.local", "port": "5432",
                               "dbname": "dwh", "user": "a.ivanova"}}
    config = Config(allowed_origins=("https://onepage.example.com",), connections=conns)
    store = creds.CredStore(root=tmp / "creds")
    if with_password:
        store.set_password("analytics_dw", "SECRET-PW")

    run = runs.Run("r-" + os.urandom(4).hex(), tmp / "work")
    meta = {"checksum_sha256": checksum, "run_id": "rr",
            "generator": "gp-delta", "version": "1"}
    runs.run_generator(run, bundle, meta, config=config, cred_store=store,
                       python_exe=sys.executable, connect=connect)
    return run, list(run.stream())


def main() -> None:
    check("reference passthrough.py present", REF_PASSTHROUGH.is_file(), str(REF_PASSTHROUGH))
    base = Path(tempfile.mkdtemp(prefix="op-runflow-"))

    # ── happy path ───────────────────────────────────────────────────────
    bundle, checksum = make_bundle()
    run, evs = run_once(base / "ok", bundle, checksum)
    kinds = [e.get("t") for e in evs]
    done = [e for e in evs if e.get("t") == "done"]
    errs = [e for e in evs if e.get("t") == "error"]
    check("happy: no error events", not errs, str(errs))
    check("happy: exactly one done", len(done) == 1, str(kinds))
    if done:
        out = done[0]["outputs"]
        check("happy: done html=dashboard.html", out.get("html") == "dashboard.html", str(out))
        check("happy: done slots {0,2}", set(out.get("slots", {})) == {"0", "2"}, str(out.get("slots")))
    # outputs are registered + physically on disk + correct content
    check("happy: 3 outputs registered (2 slots + html)", len(run.output_paths) == 3,
          str(list(run.output_paths)))
    if "slot_0.csv.gz" in run.output_paths:
        got0 = gzip.decompress(run.output_paths["slot_0.csv.gz"].read_bytes())
        check("happy: slot_0 gunzips to GP csv", got0 == CSV0, repr(got0))
    if "slot_2.csv.gz" in run.output_paths:
        got2 = gzip.decompress(run.output_paths["slot_2.csv.gz"].read_bytes())
        check("happy: slot_2 gunzips to GP csv (5 rows)", got2 == CSV2, repr(got2))
    if "dashboard.html" in run.output_paths:
        html = run.output_paths["dashboard.html"].read_text(encoding="utf-8")
        check("happy: html is the bundle template", "DELTA report" in html, html[:60])
    # progress came both from GP (copy) and from the generator ([copy]/[html])
    gp_copy = [e for e in evs if e.get("stage") == "copy" and e.get("phase") == "done"]
    gen_stage = [e for e in evs if e.get("t") == "stage" and e.get("stage") == "generate"]
    gen_prog = [e for e in evs if e.get("t") == "progress" and e.get("stage") in ("copy", "html")
                and "done" in e and "phase" not in e]
    check("happy: GP emitted per-query copy done (x2)", len(gp_copy) == 2, str(len(gp_copy)))
    check("happy: generate stage emitted", len(gen_stage) == 1, str(len(gen_stage)))
    check("happy: generator progress forwarded", len(gen_prog) >= 1, str(len(gen_prog)))

    # ── cred_missing: no password ─────────────────────────────────────────
    _, evs = run_once(base / "nocred", bundle, checksum, with_password=False)
    e = [x for x in evs if x.get("t") == "error"]
    check("cred_missing -> error kind=cred_missing",
          len(e) == 1 and e[0]["kind"] == "cred_missing", str(e))
    check("cred_missing: no done", not any(x.get("t") == "done" for x in evs))

    # ── gp_config: no connection topology ─────────────────────────────────
    _, evs = run_once(base / "nocfg", bundle, checksum, with_topology=False)
    e = [x for x in evs if x.get("t") == "error"]
    check("gp_config -> error kind=gp_config", len(e) == 1 and e[0]["kind"] == "gp_config", str(e))

    # ── checksum mismatch ─────────────────────────────────────────────────
    _, evs = run_once(base / "badsum", bundle, "0" * 64)
    e = [x for x in evs if x.get("t") == "error"]
    check("checksum mismatch -> error", len(e) == 1 and e[0]["kind"] == "checksum_mismatch", str(e))

    # ── gen_crash: the generator dies with a non-zero code ─────────────────
    crash_manifest = {
        "name": "gp-crash", "version": "1.0", "entrypoint": {"module": "crasher"},
        "engine": "agent",
        "outputs": {"html": True, "slots": [{"index": 0, "name": "slot_0.csv.gz"}]},
        "sources": {"type": "greenplum", "connection": "analytics_dw", "parallelism": 1,
                    "queries": [{"sql_file": "sql/q0.sql", "output_name": "slot_0.csv.gz",
                                 "format": "csv_gz"}]},
    }
    crasher = ("import sys\n"
               "print('[warm] 0/1 starting', flush=True)\n"
               "sys.stderr.write('boom in pandas\\n')\n"
               "sys.exit(3)\n")
    bundle_c, checksum_c = make_bundle(entry_module="crasher",
                                       manifest_override=crash_manifest,
                                       extra_files={"crasher.py": crasher})
    _, evs = run_once(base / "crash", bundle_c, checksum_c)
    e = [x for x in evs if x.get("t") == "error"]
    check("gen_crash -> error kind=gen_crash", len(e) == 1 and e[0]["kind"] == "gen_crash", str(e))
    check("gen_crash: GP ran before crash (copy done seen)",
          any(x.get("phase") == "done" for x in evs))

    # ── gen_no_result: the generator does not print JSON as the last line ───
    noresult_manifest = {**crash_manifest, "name": "gp-nores",
                         "entrypoint": {"module": "silent"}}
    silent = "print('[stage] 1/1 done', flush=True)\n"   # there is progress, no JSON
    bundle_n, checksum_n = make_bundle(entry_module="silent",
                                       manifest_override=noresult_manifest,
                                       extra_files={"silent.py": silent})
    _, evs = run_once(base / "nores", bundle_n, checksum_n)
    e = [x for x in evs if x.get("t") == "error"]
    check("gen_no_result -> error kind=gen_no_result",
          len(e) == 1 and e[0]["kind"] == "gen_no_result", str(e))

    shutil.rmtree(base, ignore_errors=True)
    print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURES: {FAILURES}"))
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
