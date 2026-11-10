#!/usr/bin/env python3
"""Baseline for tools/bench_engine2.mjs: the SHIPPED /explore engine.

    .venv/bin/python tools/bench_engine1.py --dir ~/data/SL \
        --explore /path/to/_explore.py [--json out.json]

Same four files, same three queries, same Parquet round-trip, so the two
outputs line up column for column.

READ THIS BEFORE QUOTING ANY RATIO
----------------------------------
This runs `_explore.py` under **CPython on the host**, and CPython is *not*
what users run. /explore runs it inside Pyodide, in a wasm CPython, in a Web
Worker. Published Pyodide benchmarks put pure-Python work at roughly 3–20×
slower than native CPython depending on the workload, and the pandas/DuckDB
parts are wasm-compiled too. So:

  * the CPython column below is a LOWER BOUND on the current engine's cost —
    the real browser is slower, by a factor this script cannot measure;
  * the engine-v2 column is measured in Node, i.e. the same V8 + the same wasm
    binaries a Chromium tab runs, so it is close to the browser number;
  * therefore any v2/v1 ratio computed from these two files UNDERSTATES the
    win. `tools/bench_engine1_pyodide.mjs` closes that gap; run it when you
    need a defensible number.

`_explore.py` is not vendored in this repo yet (it lives in the /explore
workstream). Point `--explore` at it; the script imports the file directly and
touches nothing else in it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


def load_explore(path: Path):
    spec = importlib.util.spec_from_file_location("_explore_bench", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.expanduser("~/data/SL"))
    ap.add_argument("--sheet", default="Заказы")
    ap.add_argument("--table", default="zakazy")
    ap.add_argument("--header", type=int, default=0)
    ap.add_argument("--explore", default=None,
                    help="path to _explore.py (default: search the usual places)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    candidates = [Path(args.explore)] if args.explore else [
        Path(__file__).resolve().parent.parent / "static" / "py" / "_explore.py",
        Path.home() / "Downloads" / "Onepage" / "_explore.py",
    ]
    src = next((p for p in candidates if p.exists()), None)
    if src is None:
        print("!! _explore.py not found — pass --explore", file=sys.stderr)
        return 2

    ex = load_explore(src)
    import duckdb
    import pandas
    print(f"engine v1 (SHIPPED)  {src}")
    print(f"python {sys.version.split()[0]}  pandas {pandas.__version__}  "
          f"duckdb {duckdb.__version__}   ** CPython, not Pyodide — see header **")
    print(f"dir={args.dir}  sheet={args.sheet}  table={args.table}\n")

    files = sorted(f for f in os.listdir(args.dir)
                   if f.lower().endswith((".xlsx", ".xls")) and not f.startswith("~$"))
    out = {"engine": "v1", "files": [], "queries": [], "parquet": {}}

    print("file                 bytes    rows   cols  index_ms   MB/s")
    print("-----------------------------------------------------------")
    inbox = Path(tempfile.mkdtemp(prefix="op-bench1-"))
    try:
        for f in files:
            # index_file() unlinks its input, which is the /inbox discipline —
            # so it gets a copy, exactly like gen-worker.js hands it one.
            tmp = inbox / f
            shutil.copyfile(Path(args.dir) / f, tmp)
            size = tmp.stat().st_size
            t0 = time.perf_counter()
            r = ex.index_file(str(tmp), sheet=args.sheet, header_row=args.header,
                              table=args.table, partition=f.rsplit(".", 1)[0], file=f)
            dt = (time.perf_counter() - t0) * 1000
            print(f"{f:<20} {size:>8} {r['rows']:>6} {len(r['columns']):>6} "
                  f"{dt:>9.1f} {size / 1048576 / (dt / 1000):>6.1f}")
            out["files"].append({"file": f, "bytes": size, "rows": r["rows"],
                                 "cols": len(r["columns"]), "index_ms": dt})
    finally:
        shutil.rmtree(inbox, ignore_errors=True)

    tot_ms = sum(f["index_ms"] for f in out["files"])
    tot_rows = sum(f["rows"] for f in out["files"])
    tot_bytes = sum(f["bytes"] for f in out["files"])
    out["totals"] = {"index_ms": tot_ms, "rows": tot_rows, "bytes": tot_bytes}
    print("-----------------------------------------------------------")
    print(f"TOTAL                {tot_bytes:>8} {tot_rows:>6}        {tot_ms:>9.1f}")
    print(f"index wall-clock: {tot_ms:.1f} ms for {tot_rows} rows / "
          f"{tot_bytes / 1048576:.1f} MB")
    print(f"frames resident:  {ex.stats()['bytes'] / 1048576:.1f} MB "
          f"(what /explore shows against the 2.75 GB tab ceiling)\n")

    con = ex._conn()
    schema = [(r[0], str(r[1]).upper())
              for r in con.execute(f'DESCRIBE SELECT * FROM "{args.table}"').fetchall()]
    numeric = next((c for c, t in schema
                    if any(t.startswith(p) for p in ("DOUBLE", "BIGINT", "INTEGER", "DECIMAL"))
                    and not c.startswith("__")), None)
    text = next((c for c, t in schema if t == "VARCHAR" and not c.startswith("__")), None)
    date = next((c for c, t in schema if t.startswith(("TIMESTAMP", "DATE"))), None)

    queries = [("count per __partition",
                f'SELECT __partition, count(*) AS n FROM "{args.table}" GROUP BY 1 ORDER BY 1')]
    if numeric and text:
        queries.append((f"SUM({numeric}) by {text}",
                        f'SELECT "{text}" AS k, sum("{numeric}") AS s FROM "{args.table}" '
                        f"GROUP BY 1 ORDER BY s DESC NULLS LAST LIMIT 20"))
    if date:
        queries.append((f"filter on {date}",
                        f'SELECT count(*) AS n FROM "{args.table}" '
                        f"WHERE \"{date}\" >= TIMESTAMP '2000-01-01'"))
    else:
        anydate = next((c for c, _ in schema if "дата" in c or "date" in c.lower()), None)
        if anydate:
            queries.append((f"filter on {anydate} (VARCHAR — see note)",
                            f'SELECT count(*) AS n FROM "{args.table}" '
                            f"WHERE \"{anydate}\" LIKE '2026%'"))

    print("query                                              rows      ms   (best of 3)")
    print("-----------------------------------------------------------")
    for label, sql in queries:
        best, rows = float("inf"), 0
        for _ in range(3):
            t0 = time.perf_counter()
            res = ex.run_sql(sql, limit=1000)
            best = min(best, (time.perf_counter() - t0) * 1000)
            rows = res.get("total") or len(res.get("rows") or [])
        print(f"{label:<50} {rows:>5} {best:>7.1f}")
        out["queries"].append({"label": label, "sql": sql, "rows": rows, "ms": best})
    print()

    # Parquet, through DuckDB, so the comparison is COPY vs COPY.
    #
    # NOT through `con`: the /explore query connection is opened with
    # `enable_external_access=false`, so it cannot write a file at all
    # (measured — it raises PermissionException). That is exactly why the
    # shipped engine grew a second, IO-only connection for its OPFS cache. We
    # reproduce that here: a fresh connection with the same frames registered
    # and the engine's own `_rebuild_view` run over it.
    io = duckdb.connect()
    for rel, frame in ex._FRAMES.items():
        io.register(rel, frame)
    ex._rebuild_view(io, args.table)
    tmpdir = Path(tempfile.mkdtemp(prefix="op-bench1-pq-"))
    try:
        pq = tmpdir / "t.parquet"
        t0 = time.perf_counter()
        io.execute(f"COPY \"{args.table}\" TO '{pq}' (FORMAT PARQUET)")
        w = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        n = io.execute(f"SELECT count(*) FROM read_parquet('{pq}')").fetchone()[0]
        rd = (time.perf_counter() - t0) * 1000
        size = pq.stat().st_size
        out["parquet"] = {"write_ms": w, "read_ms": rd, "bytes": size, "rows": int(n)}
        print(f"parquet write {w:.1f} ms -> {size / 1048576:.1f} MB "
              f"({tot_bytes / size:.1f}x smaller than the xlsx)")
        print(f"parquet read  {rd:.1f} ms -> {n} rows back")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss / (1048576 if sys.platform == "darwin" else 1024)
        out["rss_mb"] = rss_mb
        print(f"\npython peak rss: {rss_mb:.0f} MB")
    except Exception:
        pass

    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2, ensure_ascii=False))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
