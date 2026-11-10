"""Reproducible hot-path baseline for before/after comparisons.

Boots the app in-process on a throwaway DB seeded with the Chrome harness
fixture (one generator-backed, published dashboard), then hits the routes
whose cost must not move — `/` (workspace home), `/d/{id}` (viewer),
`/render/{id}/` (the fast lane) — and prints per-route p50/p95 of `hdr_ms`,
the median `db_n` (SQL statements per request) and, for the fast lane,
`serve_us` p50. Absolute numbers are in-process TestClient numbers (no
socket, no uvicorn): compare runs of THIS script with each other, never
with `/admin/perf` on a real host.

    python tools/perf_baseline.py            # prints a table
    python tools/perf_baseline.py --json     # machine-readable

Phase-0 baseline (2026-08-22, merged HEAD) is recorded in
docs/PERF_DECISIONS.md ("Baseline measurements 2026-08-22").
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="op-perf-base-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_PERF_SAMPLE": "1.0",
    "OP_PERF_ADAPTIVE": "false",
    "OP_ACCESS_LOG": "false",
})
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "chrome"))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
import seed_explore  # noqa: E402

ROUTES = [("/", 60), ("/d/{id}", 60), ("/render/{id}/", 300)]


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((len(xs) - 1) * p))))
    return xs[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    with TestClient(app) as c:
        c.post("/dev-login", data={"person_id": "admin1"})
        info = seed_explore.seed(c, confirm=False)
        dash = info["dashboard"]
        # warm: first render builds the cell, the rest must be fast-lane hits
        c.get(f"/render/{dash}/")
        t0 = time.time()
        for path, n in ROUTES:
            url = path.replace("{id}", dash)
            for _ in range(n):
                r = c.get(url)
                assert r.status_code == 200, (url, r.status_code)
        wall = time.time() - t0
        # let the perflog writer flush (2 s cadence)
        time.sleep(2.5)
    recs = []
    for f in glob.glob(f"{TMP}/perflog/perf-*.jsonl"):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    pass
    out = {}
    for path, _n in ROUTES:
        rows = [r for r in recs if r.get("route") == path or r.get("path") == path.replace("{id}", dash)]
        hdr = [r.get("hdr_ms") for r in rows if isinstance(r.get("hdr_ms"), (int, float))]
        dbn = [r.get("db_n") for r in rows if isinstance(r.get("db_n"), (int, float))]
        serve = [r["stages"]["serve_us"] for r in rows
                 if isinstance(r.get("stages"), dict) and isinstance(r["stages"].get("serve_us"), (int, float))]
        out[path] = {
            "n": len(rows),
            "hdr_ms_p50": pct(hdr, 0.5), "hdr_ms_p95": pct(hdr, 0.95),
            "db_n_median": statistics.median(dbn) if dbn else None,
            "serve_us_p50": pct(serve, 0.5),
            "fast_lane_hits": len(serve),
        }
    out["_wall_s"] = round(wall, 2)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    print(f"{'route':<16}{'n':>5}{'hdr p50':>10}{'hdr p95':>10}{'db_n':>7}{'serve_us p50':>14}{'lane hits':>11}")
    for path, _n in ROUTES:
        o = out[path]
        print(f"{path:<16}{o['n']:>5}{str(o['hdr_ms_p50']):>10}{str(o['hdr_ms_p95']):>10}"
              f"{str(o['db_n_median']):>7}{str(o['serve_us_p50']):>14}{o['fast_lane_hits']:>11}")
    print(f"wall {out['_wall_s']} s")


if __name__ == "__main__":
    main()
