"""Real-data smoke: index a REAL folder through the browser engine's own code
(static/py/_explore.py under CPython), bind a generator's semantics to it and
run every KPI — the check that must pass before anything ships.

    OP_REAL_DIR=/path/to/SL python tools/real_smoke.py --semantics /path/slreport-bundle.zip --generator slreport \
        [--glob 'SL_*.xlsx'] [--sheet Заказы] [--table sl_zakazy]

Prints per-file stage timings (parse / coerce / register / view), the union's
column types, the semantic layer status (bound/unbound tables, KPI readiness),
one row per KPI (rows returned, error) and a few intents through intent_sql.
Exit 1 on any engine error. The folder is never modified; files are copied
to a temp dir because index_file unlinks its input.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "static", "py"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.environ.get("OP_REAL_DIR"))
    ap.add_argument("--glob", default="*.xlsx")
    ap.add_argument("--sheet", default=None, help="sheet name (default: first)")
    ap.add_argument("--table", default="real_table")
    ap.add_argument("--semantics", default=None, help="bundle zip or semantics.json")
    ap.add_argument("--generator", default="gen")
    ap.add_argument("--questions", default=None, help="intent JSON list to run through intent_sql")
    ap.add_argument("--max-kpi-ms", type=int, default=0, help="fail (exit 1) if any KPI's ms exceeds this (0 = report only)")
    ap.add_argument("--files-scale", type=int, default=1, help="project each KPI's ms linearly to N x the sampled file count (report only)")
    args = ap.parse_args()
    if not args.dir or not os.path.isdir(args.dir):
        print("no real folder (OP_REAL_DIR / --dir) — skipping")
        return 0

    import _explore
    from app.services import semantics as S
    files = sorted(p for p in glob.glob(os.path.join(args.dir, args.glob)) if not os.path.basename(p).startswith("~$"))
    if not files:
        print("no files match", args.glob)
        return 1
    tmp = tempfile.mkdtemp(prefix="op-real-")
    _explore.CACHE_DIR = os.path.join(tmp, "cache")
    catalog = {"tables": [{"table_id": "t_real", "name": args.table, "display_name": args.table,
                           "columns": [], "partitions": []}]}
    sem = None
    if args.semantics:
        if args.semantics.endswith(".zip"):
            from app.services import genstorage
            raw = genstorage.read_semantics_from_zip(open(args.semantics, "rb").read())
        else:
            raw = open(args.semantics, "rb").read()
        sem = S.parse_bytes(raw, args.generator)

    # Index everything first (bindings need the union's column names).
    _explore.set_catalog(catalog)
    totals = {}
    errors = 0
    for p in files:
        name = os.path.basename(p)
        dst = os.path.join(tmp, name)
        shutil.copy(p, dst)
        st = os.stat(p)
        key = _explore.cache_key(name, st.st_size, int(st.st_mtime * 1000), args.sheet or "", 0)
        t0 = time.time()
        try:
            out = _explore.index_file(dst, args.sheet or "", 0, args.table, name.rsplit(".", 1)[0], name, key)
        except Exception as e:
            errors += 1
            print(f"ERROR {name}: {e}")
            continue
        ms = out.get("ms") or {}
        for k, v in ms.items():
            totals[k] = totals.get(k, 0) + v
        print(f"{name:22s} rows={out['rows']:7d} cols={len(out['columns']):3d} "
              f"parse={ms.get('parse', 0):5d} coerce={ms.get('coerce', 0):4d} register={ms.get('register', 0):4d} "
              f"view={ms.get('view', 0):3d} cache={ms.get('cache_write', 0):4d} ms  total={int((time.time() - t0) * 1000)} ms")
    print("stage totals ms:", totals)

    # Column types after the union (the thing 1C exports break).
    con = _explore._conn()
    types = con.execute(f"DESCRIBE main.{_explore._qi(args.table)}").fetchall()
    mixed = [(c, t) for c, t in ((r[0], str(r[1])) for r in types)]
    print(f"union columns: {len(mixed)}; VARCHAR={sum(1 for _, t in mixed if t == 'VARCHAR')} "
          f"DOUBLE={sum(1 for _, t in mixed if t == 'DOUBLE')} TIMESTAMP={sum(1 for _, t in mixed if 'TIMESTAMP' in t)} "
          f"other={sum(1 for _, t in mixed if t not in ('VARCHAR', 'DOUBLE') and 'TIMESTAMP' not in t)}")

    # Re-index from cache: the second open.
    t0 = time.time()
    for p in files:
        name = os.path.basename(p)
        st = os.stat(p)
        key = _explore.cache_key(name, st.st_size, int(st.st_mtime * 1000), args.sheet or "", 0)
        _explore.index_cached(key, args.table, name.rsplit(".", 1)[0], name, args.sheet or "")
    print(f"re-open from Parquet cache: {int((time.time() - t0) * 1000)} ms for {len(files)} files")

    if sem is None:
        return 1 if errors else 0

    # Bind semantics to the indexed table and materialise the views.
    cols = [{"name": r[0]} for r in types]
    catalog["tables"][0]["columns"] = cols
    catalog["tables"][0]["partitions"] = [{"file": os.path.basename(p), "sheet": args.sheet or "", "key": os.path.basename(p).rsplit(".", 1)[0]} for p in files]
    bindings = S.bind(sem, catalog["tables"])
    _explore.set_catalog({**catalog, "semantics": [{"generator": args.generator, "generator_id": 1, "semantics": sem, "bindings": bindings}]})
    t0 = time.time()
    st = _explore.semantics_status()
    g = st["generators"][0]
    print(f"semantics: tables={g['tables']} views={g['views']} errors={g['errors']} ({int((time.time() - t0) * 1000)} ms)")
    kpi_errors = 0
    kpi_ms = {}
    for k in g["kpis"]:
        if not k["ready"]:
            print(f"  KPI {k['key']:28s} NOT READY")
            kpi_errors += 1
            continue
        out = _explore.intent_sql({"kpi": k["key"], "generator_id": 1, "dims": k["dims"][:2], "limit": 5})
        if out.get("error"):
            print(f"  KPI {k['key']:28s} intent error: {out['error']}")
            kpi_errors += 1
            continue
        t0 = time.time()
        res = _explore.run_sql(out["sql"], limit=5)
        ms = int((time.time() - t0) * 1000)
        if res.get("error"):
            print(f"  KPI {k['key']:28s} SQL ERROR ({ms} ms): {res['error'][:160]}")
            kpi_errors += 1
        else:
            kpi_ms[k["key"]] = ms
            scale_note = f"  ≈ {ms * args.files_scale:6d} ms on {args.files_scale}x files (linear projection)" if args.files_scale != 1 else ""
            print(f"  KPI {k['key']:28s} ok {ms:5d} ms rows={len(res['rows'])} total={res.get('total')} sample={res['rows'][:1]}{scale_note}")

    if args.max_kpi_ms:
        offenders = [(key, ms) for key, ms in kpi_ms.items() if ms > args.max_kpi_ms]
        if offenders:
            print("KPI over budget: " + ", ".join(f"{key} ({ms} ms)" for key, ms in offenders))
            kpi_errors += 1

    if args.questions:
        for q in json.load(open(args.questions, encoding="utf-8")):
            intent = dict(q.get("intent") or {}, generator_id=1)
            out = _explore.intent_sql(intent)
            t_q = time.perf_counter()
            res = _explore.run_sql(out["sql"]) if out.get("sql") else {"error": out.get("error")}
            q_ms = int((time.perf_counter() - t_q) * 1000)
            print(f"  Q {q.get('question', '')[:50]:50s} {q_ms:6d} ms -> {'ERROR ' + str(res.get('error'))[:80] if res.get('error') else str(res['rows'][:2])}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if (errors or kpi_errors) else 0


if __name__ == "__main__":
    sys.exit(main())
