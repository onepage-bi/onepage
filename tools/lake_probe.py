#!/usr/bin/env python3
r"""Leg 0 — the validation gate for the "table inference" hypothesis.

READ-ONLY probe over a COPY of `data/onepage.db`. It answered one question
before `app/services/catalog.py` existed: do the files a folder actually
contains fall into canonical tables by their observed headers, or is that
wishful thinking? (Answer, `docs/PERF_DECISIONS.md` ADR-12: partly — G1 and G4
failed, so the inference ships as a SUGGESTER.)

**The algorithm is not here.** It lives in `app/services/catalog.py` and this
tool calls `catalog.infer()`. That is deliberate: a gate measuring a private
copy of the clusterer would stop measuring the product the first time one of
the two was touched. What remains here is what a gate is made of — the
registered thresholds, the five gates, the report, and the CLI.

The gates, over `catalog.infer()`'s result:

  * G1 separability — share of files whose intra-cluster containment clears
    0.85 while their best inter-cluster containment stays under 0.6;
  * G2 sheet-name split — some sheet name spanning >= 3 files must break into
    >= 3 differently-stemmed tables (the `TDSheet` case);
  * G3 partitions — every table gets a key for >= 80 % of its files;
  * G4 reuse — some table read by >= 2 generators;
  * G5 stability — the older half of the OBSERVING runs mints the same ids.

Usage:
    python tools/lake_probe.py /path/to/onepage.db [--json out.json] [--half]

The database is opened with `mode=ro`: the probe cannot write to it even if a
bug tried to. It never starts the app (no lifespan, no migrations).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# ───────────────────────────── registered thresholds ─────────────────────────
# The clustering thresholds (0.85 join, 0.92 merge, the sim weights) are the
# service's — `catalog.JOIN_MIN` and friends — because they are what production
# runs. Only the GATE thresholds live here.
G1_FILE_SHARE = 0.90   # share of files that must satisfy both bounds
G1_INTRA_MIN = 0.85
G1_INTER_MAX = 0.60
G2_MIN_CLUSTERS = 3
G2_MIN_FILES = 3       # the sheet-name group must span >= 3 FILES to count
G3_PART_SHARE = 0.80
G4_MIN_CONSUMERS = 2
G5_OVERLAP_MIN = 0.95

# Sheet-name stems G2 is a statement about (kept for the report; the gate
# itself is generic — see `gate_g2`).
G2_EXPECTED_STEMS = ("datasalesorder", "datarealisation", "peremes")


# ───────────────────────────── app imports (read-only) ───────────────────────

def load_app(db_path: Path):
    """Point the app at this file and import the pieces we reuse.

    OP_DB_PATH is set BEFORE the import because `app.database` builds its
    engine at import time (same discipline as tests/smoke_quality.py). We do
    not use that engine — every query below runs on our own `mode=ro` one —
    but a stray import must not end up pointed at the developer's real
    `data/onepage.db`.
    """
    repo = Path(__file__).resolve().parent.parent
    os.environ["OP_DB_PATH"] = str(db_path)
    os.environ.setdefault("OP_PERF_ENABLED", "0")
    sys.path.insert(0, str(repo))
    from app import models                                   # noqa: E402
    from app.services import catalog, lineage                 # noqa: E402
    return models, lineage, catalog


def ro_session(db_path: Path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    uri = "sqlite:///file:" + quote(str(db_path)) + "?mode=ro&uri=true"
    engine = create_engine(uri, connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# ───────────────────────────── gates ─────────────────────────────────────────

def gate_g1(res: dict) -> dict:
    files = [f for t in res["tables"] for f in t["files"]]

    def _ok(f):
        return (f["intra_containment"] >= G1_INTRA_MIN
                and f["inter_containment_max"] < G1_INTER_MAX)

    ok = [f for f in files if _ok(f)]
    share = len(ok) / len(files) if files else 0.0
    worst_gap = min((t["gap"] for t in res["tables"]), default=0.0)
    # Informational companion (NOT the gate): the same rule scored with the
    # clusterer's own similarity instead of raw containment. Where the two
    # disagree the offender is a small table nested inside a wide one, not two
    # tables the inference could actually confuse.
    by_sim = [f for f in files
              if f["intra_containment"] >= G1_INTRA_MIN
              and f.get("inter_sim_max", 0.0) < G1_INTER_MAX]
    offenders = sorted(
        ({"file": f["file"], "sheet": f["sheet"],
          "intra": f["intra_containment"], "inter": f["inter_containment_max"],
          "inter_with": f.get("inter_containment_with"),
          "inter_sim": f.get("inter_sim_max")}
         for f in files if not _ok(f)),
        key=lambda x: (x["intra"], -x["inter"]))
    pairs = Counter((o["sheet"], o["inter_with"]) for o in offenders)
    return {"gate": "G1", "name": "separability", "pass": bool(files) and share >= G1_FILE_SHARE,
            "share": round(share, 4), "threshold": G1_FILE_SHARE,
            "files": len(files), "files_ok": len(ok),
            "min_gap_over_clusters": round(worst_gap, 4),
            "share_by_sim_informational": round(len(by_sim) / len(files), 4) if files else 0.0,
            "offender_pairs": [{"sheet": s, "collides_with": w, "files": n}
                               for (s, w), n in pairs.most_common(15)],
            "offenders": offenders[:20]}


def gate_g2(res: dict) -> dict:
    """A sheet NAME must not be a table — SOMEWHERE in the corpus.

    The hypothesis this gate tests is "one sheet name hides several tables",
    and the honest test of an existential claim is existential. The first
    version asked the LARGEST sheet-name group to split, which is a different
    and false claim: production's biggest group is `STOCK` — 129 daily
    snapshots of one genuine table that would be WRONG to split, so the gate
    failed while the corpus contained a textbook example of exactly what it
    was looking for (`TDSheet` -> 4 tables from different file families).

    So: PASS if any sheet-name group spanning >= G2_MIN_FILES files splits
    into >= G2_MIN_CLUSTERS clusters whose names AND whose file-name stems
    differ. The stem test is what keeps the PASS honest — three clusters that
    all came from `svt_*` files are one family the clusterer happened to cut
    up, not three tables sharing a sheet name. `largest_sheet_group` stays in
    the report, informationally, so a reader can see the STOCK case too.
    """
    per_sheet: dict[str, dict] = {}
    for t in res["tables"]:
        for f in t["files"]:
            e = per_sheet.setdefault(f["sheet"], {
                "names": set(), "ids": set(), "stems": set(), "files": set()})
            e["names"].add(t["name"])
            e["ids"].add(t["table_id"])
            e["stems"].add(t.get("name_stem") or t["name"])
            e["files"].add((f["file"], t["data_source_id"]))
    if not per_sheet:
        return {"gate": "G2", "name": "sheet-name split", "pass": False,
                "detail": "no table sheets at all", "per_sheet": []}
    rows = sorted(({"sheet": s, "files": len(e["files"]), "clusters": len(e["ids"]),
                    "names": sorted(e["names"]), "stems": sorted(e["stems"])}
                   for s, e in per_sheet.items()),
                  key=lambda r: (-r["clusters"], -r["files"], r["sheet"]))

    def _splits(r):
        return (r["files"] >= G2_MIN_FILES
                and r["clusters"] >= G2_MIN_CLUSTERS
                and len(r["names"]) >= G2_MIN_CLUSTERS
                and len(r["stems"]) >= G2_MIN_CLUSTERS)

    hits = [r for r in rows if _splits(r)]
    largest = max(rows, key=lambda r: (r["files"], r["sheet"]))
    best = hits[0] if hits else rows[0]
    expected = [n for n in best["names"]
                if any(n.startswith(stem) for stem in G2_EXPECTED_STEMS)]
    return {"gate": "G2", "name": "sheet-name split", "pass": bool(hits),
            "threshold": f">= {G2_MIN_CLUSTERS} differently-stemmed clusters "
                         f"over >= {G2_MIN_FILES} files",
            "best_group": best, "splitting_groups": [r["sheet"] for r in hits],
            "largest_sheet_group": largest,
            "expected_stems_found": expected, "per_sheet": rows}


def gate_g3(res: dict) -> dict:
    bad = [{"table": t["name"], "share": t["partition_share"]}
           for t in res["tables"] if t["partition_share"] < G3_PART_SHARE]
    dups = [{"table": t["name"], **d}
            for t in res["tables"] for d in t["duplicate_partition_keys"]]
    n = len(res["tables"])
    return {"gate": "G3", "name": "partitions",
            "pass": bool(n) and not bad,
            "tables": n, "tables_below": bad, "threshold": G3_PART_SHARE,
            "duplicate_keys": dups}


def gate_g4(res: dict) -> dict:
    """Two generators reading the same table.

    A FAIL here is a fact about the corpus, not about the inference: if no two
    generators ever read the same FOLDER, no table can possibly have two
    consumers, and the report must make that visible instead of implying the
    clusterer split something it should have shared. `reads` therefore lists
    which generator observes which data sources, and `overlapping_sources`
    names the sources with more than one reader — when that list is empty the
    gate could not have passed for any clustering whatsoever.
    """
    shared = [{"table": t["name"], "data_source_id": t["data_source_id"],
               "consumers": t["consumers"]}
              for t in res["tables"] if len(t["consumers"]) >= G4_MIN_CONSUMERS]
    reads = res.get("reads") or {}
    per_ds: dict[int, list[str]] = {}
    for gen, sources in reads.items():
        for ds in sources:
            per_ds.setdefault(ds, []).append(gen)
    overlap = {str(ds): sorted(gs) for ds, gs in sorted(per_ds.items()) if len(gs) > 1}
    srcs = (res.get("notes") or {}).get("sources") or {}

    def _name(ds):
        s = srcs.get(str(ds)) or {}
        return f"{ds} ({s.get('label') or s.get('key_name') or '?'})"

    return {"gate": "G4", "name": "reuse", "pass": bool(shared),
            "shared_tables": shared, "threshold": G4_MIN_CONSUMERS,
            "reads": reads, "overlapping_sources": overlap,
            "reads_named": {g: [_name(d) for d in ds] for g, ds in reads.items()},
            "detail": ("no data source has two readers — same-folder reuse does "
                       "not exist in this corpus yet, so this gate cannot pass "
                       "on any clustering") if not overlap else ""}


def gate_g5(full: dict, half: dict | None) -> dict:
    if half is None:
        return {"gate": "G5", "name": "stability", "pass": None,
                "detail": "not computed (--half runs one scope only)"}
    full_ids = {t["table_id"] for t in full["tables"]}
    half_ids = {t["table_id"] for t in half["tables"]}
    if not half_ids:
        return {"gate": "G5", "name": "stability", "pass": False,
                "detail": "the older half of the runs that carry observations produced no cluster",
                "overlap": 0.0, "threshold": G5_OVERLAP_MIN}
    overlap = len(full_ids & half_ids) / len(half_ids)
    lost = sorted({t["name"] for t in half["tables"] if t["table_id"] not in full_ids})
    return {"gate": "G5", "name": "stability", "pass": overlap >= G5_OVERLAP_MIN,
            "overlap": round(overlap, 4), "threshold": G5_OVERLAP_MIN,
            "half_tables": len(half_ids), "full_tables": len(full_ids),
            "unstable": lost}


# ───────────────────────────── report ────────────────────────────────────────

def render(res: dict, gates: list[dict], scope: str, db_path: Path,
           defaults: dict) -> str:
    L = []
    add = L.append
    add("=" * 78)
    add(f"lake_probe — Leg 0 gate   ({scope})")
    add(f"db: {db_path}  (opened read-only)")
    overrides = [f"{k}={os.environ[k]}" for k in defaults if k in os.environ]
    if overrides:
        add("!! THRESHOLD OVERRIDES IN EFFECT: " + ", ".join(overrides)
            + "  — this is NOT the registered run")
    n = res["notes"]
    nt = res.get("non_tables") or {"total": 0, "pivot": 0, "fragment": 0}
    add(f"chains: {n['chains']}   (file,sheet) units: {n['units']}   "
        f"units with profiles: {n['units_with_profiles']}   "
        f"files still without a schema after walk-back: {n['files_without_schema']}")
    add(f"classified out before clustering: {nt['total']} units "
        f"({nt['pivot']} pivot, {nt['fragment']} fragment)   "
        f"clustered: {n['units'] - nt['total']}   tables: {len(res['tables'])}")
    add("=" * 78)
    add("")
    srcs = res["notes"].get("sources") or {}
    for t in res["tables"]:
        src = srcs.get(str(t["data_source_id"])) or {}
        add(f"[{t['name']}]  {t['table_id']}   "
            f"ds={t['data_source_id']} ({src.get('label') or src.get('key_name') or '?'})  "
            f"confidence={t['confidence']:.2f}  named_by={t['named_by']}")
        add(f"    files={t['n_files']}  cols={t['n_columns']}  core={t['core_size']}  "
            f"header_row={t['header_row']}  runs={t['n_runs']}  "
            f"sheets={', '.join(t['sheets'])}")
        add(f"    containment: intra_min={t['intra_containment_min']:.2f}  "
            f"inter_max={t['inter_containment_max']:.2f}  gap={t['gap']:.2f}")
        add(f"    partitions ({t['partition_share']:.0%} of files): "
            + (", ".join(t["partitions"]) or "-"))
        add("    consumers: " + (", ".join(t["consumers"]) or "-"))
        for d in t["duplicate_partition_keys"]:
            add(f"    !! duplicate key {d['key']}: {', '.join(d['files'])} "
                f"— different (size, mtime), treat as a re-issue")
        for f in t["files"]:
            add(f"      - {f['file']} :: {f['sheet']}  cols={f['columns']} "
                f"part={f['partition'] or '-'} intra={f['intra_containment']:.2f} "
                f"inter={f['inter_containment_max']:.2f}")
        add("")
    add("-" * 78)
    nt = res.get("non_tables") or {}
    add(f"NON-TABLE SHEETS: {nt.get('total', 0)} units "
        f"({nt.get('pivot', 0)} pivot, {nt.get('fragment', 0)} fragment) over "
        f"{nt.get('distinct_files', 0)} distinct files — never clustered, never gated.")
    add("  These are the candidates for the \"Header not recognized\" hint.")
    for e in nt.get("per_data_source", []):
        add(f"    ds={e['data_source_id']}: {e['pivot']} pivot, {e['fragment']} fragment "
            f"over {e['distinct_files']} files")
        for ex in e["examples"]:
            add(f"        - {ex['file']} :: {ex['sheet']}  [{ex['kind']}] {ex['reason']}")
            add(f"          headers: {', '.join(ex['headers']) or '-'}")
    add("")
    missing = res["notes"].get("files_without_schema_detail") or []
    add(f"FILES WITHOUT A SCHEMA: {len(missing)}   "
        f"(another {res['notes'].get('files_resolved_from_another_observation', 0)} "
        f"files had a schema-less row somewhere but were resolved from another "
        f"observation — those are not gaps)")
    add(f"  backfill cap = {res['notes'].get('backfill_cap')} rows")
    cap = res["notes"].get("cap_diagnosis")
    if cap:
        add(f"  cap check: with the cap lifted the number is {cap['uncapped']} "
            f"-> {'THE CAP BIT' if cap['cap_bit'] else 'the cap did NOT bite'}")
    for m in missing:
        add(f"    - {m['file']}  ds={m['data_source_id']} ({m.get('data_source')})  "
            f"kind={'+'.join(m['kinds'])}  gen={', '.join(m['generators'])}  "
            f"rows={m['rows']}  deepest_level={m['deepest_level']}")
        add(f"      {m['reason']}")
    add("")
    add("-" * 78)
    for g in gates:
        mark = "PASS" if g["pass"] else ("SKIP" if g["pass"] is None else "FAIL")
        add(f"{mark}  {g['gate']} {g['name']}")
        for k, v in g.items():
            if k in ("gate", "name", "pass"):
                continue
            if isinstance(v, list) and not v:
                continue
            add(f"        {k}: {json.dumps(v, ensure_ascii=False, default=str)}"
                if not isinstance(v, (int, float, str)) else f"        {k}: {v}")
    add("-" * 78)
    hard = [g for g in gates if g["pass"] is False]
    if not hard:
        add("VERDICT: go — inference may default to `inferred` tables.")
    elif {g["gate"] for g in hard} & {"G1", "G2"}:
        add("VERDICT: NO-GO on the declared fallback — G1/G2 failed, so inference "
            "ships as a SUGGESTER (human confirms every table), per the plan.")
    else:
        add(f"VERDICT: go with caveats — failed: {', '.join(g['gate'] for g in hard)}")
    return "\n".join(L)


# ───────────────────────────── main ──────────────────────────────────────────

def observing_run_ids(db, models) -> set:
    """Run ids with at least one `parsed`/`cached` source file — see
    `half_run_ids` for why nothing weaker works."""
    SourceFile, SourceObservation = models.SourceFile, models.SourceObservation
    return {rid for (rid,) in
            db.query(SourceObservation.run_id)
            .join(SourceFile, SourceFile.observation_id == SourceObservation.id)
            .filter(SourceFile.kind.in_(("parsed", "cached")))
            .distinct().all()
            if rid is not None}


def half_run_ids(db, models) -> set:
    """The older half of the runs whose ENGINE observed the folder.

    Deliberately not "the older half of all runs". Production holds 393 runs
    and only 28 of them were made by an engine that reports observations —
    the rest predate Lineage v2. Halving all of them handed G5 nothing but
    pre-v2 runs and a FAIL reading "the first 50 % of runs produced no
    cluster", which is a statement about a deployment date, not about the
    stability of the clustering.

    "Has a source_observation" is not enough either, and production is what
    says so: the lineage migration back-filled observations for 84 old runs
    whose files are all `kind='legacy'` (no schema, never a donor), and
    `ingest_run_start` writes a `listed` observation for every run that sends
    an input manifest, before anything has been read. Neither can produce a
    single column. The population that carries evidence is the runs with at
    least one `parsed` or `cached` file — `cached` included, because that is
    precisely the row whose header the walk-back resolves.
    """
    observed = observing_run_ids(db, models)
    runs = [r for r in db.query(models.GenerationRun).all() if r.id in observed]
    runs.sort(key=lambda r: (r.started_at or datetime.min, r.id))
    return {r.id for r in runs[:max(1, math.ceil(len(runs) / 2))]}


def _require_schema(db, models) -> None:
    """Fail with one sentence instead of a stack trace when the file is not an
    OnePage database — or when a copy arrived WITHOUT its `-wal` sidecar and
    the tables are still sitting in it."""
    from sqlalchemy.exc import OperationalError
    try:
        db.query(models.SourceObservation).limit(1).all()
    except OperationalError as exc:
        raise SystemExit(
            f"lake_probe: {db.bind.url} does not look like an OnePage database "
            f"({exc.orig}). If you copied it from a running server, copy the "
            f"-wal and -shm files too, or checkpoint first: "
            f"sqlite3 onepage.db 'PRAGMA wal_checkpoint(TRUNCATE)'.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Leg 0 validation gate (read-only).")
    ap.add_argument("db", type=Path, help="path to a COPY of onepage.db")
    ap.add_argument("--json", dest="json_out", type=Path, default=None)
    ap.add_argument("--half", action="store_true",
                    help="analyse only the first 50 %% of runs (by started_at)")
    ap.add_argument("--depth", type=int, default=200,
                    help="observations per chain to walk back through")
    args = ap.parse_args(argv)

    db_path = args.db.resolve()
    if not db_path.exists():
        print(f"lake_probe: no such database: {db_path}", file=sys.stderr)
        return 2

    models, lineage, catalog = load_app(db_path)
    db = ro_session(db_path)
    try:
        _require_schema(db, models)
        if args.half:
            ids = half_run_ids(db, models)
            res = catalog.infer(db, run_ids=ids, depth=args.depth)
            gates = [gate_g1(res), gate_g2(res), gate_g3(res), gate_g4(res),
                     gate_g5(res, None)]
            scope = f"first 50 % of runs ({len(ids)} runs)"
            payload = {"scope": "half", "result": res,
                       "gates": gates, "half": None}
        else:
            units, notes = catalog.load_corpus(db, depth=args.depth)
            # Did `_MAX_BACKFILL_ROWS` cause the missing schemas, or is the
            # history simply not there? Answer it by measurement, not by
            # reasoning about it: lift the cap and count again.
            if notes["files_without_schema"]:
                cap = lineage._MAX_BACKFILL_ROWS
                try:
                    lineage._MAX_BACKFILL_ROWS = 10 ** 9
                    _u2, n2 = catalog.load_corpus(db, depth=args.depth)
                finally:
                    lineage._MAX_BACKFILL_ROWS = cap
                notes["cap_diagnosis"] = {
                    "cap": cap, "capped": notes["files_without_schema"],
                    "uncapped": n2["files_without_schema"],
                    "cap_bit": n2["files_without_schema"] < notes["files_without_schema"]}
            res = catalog.analyse(units, notes)
            ids = half_run_ids(db, models)
            half = catalog.infer(db, run_ids=ids, depth=args.depth)
            g5 = gate_g5(res, half)
            g5["runs_total"] = db.query(models.GenerationRun).count()
            g5["runs_with_observed_files"] = len(observing_run_ids(db, models))
            g5["runs_in_half"] = len(ids)
            gates = [gate_g1(res), gate_g2(res), gate_g3(res), gate_g4(res), g5]
            scope = "all runs"
            payload = {"scope": "full", "result": res, "gates": gates,
                       "half": {"tables": [{"table_id": t["table_id"], "name": t["name"],
                                            "n_files": t["n_files"]}
                                           for t in half["tables"]],
                                "runs": len(ids)}}
    finally:
        db.close()

    print(render(res, gates, scope, db_path, catalog.DEFAULTS))
    if args.json_out:
        args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                            default=str), encoding="utf-8")
        print(f"\nJSON written to {args.json_out}")
    return 0 if all(g["pass"] is not False for g in gates) else 1


if __name__ == "__main__":
    sys.exit(main())
