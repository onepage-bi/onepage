"""Smoke test for the Leg 0 gate (tools/lake_probe.py).

The fixture is the pilot folder in miniature, built through the REAL API the
same way tests/smoke_quality.py builds its own: two generators registered from
bundles, runs started with an `input_manifest`, observations POSTed with
schemas/consumed/profiles. Nothing is hand-written into source_files or
quality_results — if the probe can read this database it can read production's.

What the folder contains, and why:

  * three 1C exports whose sheet is called `TDSheet` in all seven files —
    `DataSalesOrder_*` (8 columns), `DataRealisation_*` (9 columns, 2 of them
    shared with the first) and `StockTransfer_*` (11 unrelated columns). This is
    the case the whole plan exists for: merging by SHEET NAME produces one
    28-column phantom, and G2 fails unless the probe splits it into three.
  * `real_forecast_SOP7.xlsx` and `old1_real_forecast_SOP7.xlsx` — the same
    table under a decorated file name. One table, two partitions.
  * `RDC Data.xlsx` — its own table, and `header_row=2`, so it also exercises
    the hard split.
  * `Pivot Report.xlsx` (`Row Labels / Grand Total / 2024 / 2025`) and
    `Fragment.xlsx` (one numeric header) — the two shapes production is full
    of. Neither may become a table; both must be reported as non-tables.
  * `twin_a1.xlsx` / `twin_a2.xlsx` — same file stem, same sheet name,
    incompatible header rows, so they can never share a table. Their names
    collide twice over and must still come out unique.
  * `junk.txt` — unreadable, no schema, must not break anything.

Four EMPTY runs (started, never observed) are stamped before everything else:
production is full of them — every run from before Lineage v2 shipped — and
"the first 50 % of runs" used to select nothing but those, which failed G5 with
a statement about a migration date rather than about stability.

Each generator runs TWICE: the second run reports every file as `cached` with
no schema, which is what the engine really does for unchanged files, so the
walk-back through `lineage.collect_chain_files` is exercised rather than
assumed. Runs are stamped explicitly (SQLite `func.now()` has second
resolution) because `--half` selects "the first 50 % of runs by started_at".

The probe is run as a SUBPROCESS against the finished database file, and the
file's bytes are hashed before and after: a read-only tool that writes is a
failed read-only tool, whatever its output says.
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="op-lake-smoke-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
})

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

FAILURES = []
NOW = datetime.utcnow()
NOW_MS = int(time.time() * 1000)
FOLDER = "OPS_RDC_Exports"


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


# ───────────────────────────── fixture data ──────────────────────────────────

A_COLS = ["Item", "Order", "Date", "Quantity", "Amount",
          "Warehouse", "Counterparty", "Manager"]                       # 8
B_COLS = ["Item", "Date", "Sale", "Batch", "Price",
          "VAT", "Buyer", "Area", "Shipment"]               # 9, shares 2 with A
C_COLS = ["Transfer No", "From", "To", "Transfer date", "Position",
          "Qty", "Unit", "Assignee", "Status", "Note",
          "Transport"]                                             # 11, disjoint
F_COLS = ["SKU", "Week", "Forecast", "Actual", "Bias", "MAPE", "Channel", "Plant"]
R_COLS = ["Region", "Stock", "Days", "Owner", "Comment", "Segment", "Flag"]
P_COLS = ["Row Labels", "Grand Total", "2024", "2025"]        # a pivot corner
G_COLS = ["11002126"]                                          # a fragment
X_COLS = ["Alpha", "Beta", "Gamma", "Delta"]                   # twin_a1
Y_COLS = ["Epsilon", "Zeta", "Eta", "Theta"]                   # twin_a2

# (file name, sheet, columns, header_row, rows)
CORPUS = [
    ("DataSalesOrder_03-06.26.xlsx", "TDSheet", A_COLS, 0, 4000),
    ("DataSalesOrder_07.26.xlsx", "TDSheet", A_COLS, 0, 1200),
    ("DataSalesOrder_08.26.xlsx", "TDSheet", A_COLS, 0, 900),
    ("DataRealisation_07.26.xlsx", "TDSheet", B_COLS, 0, 1500),
    ("DataRealisation_08.26.xlsx", "TDSheet", B_COLS, 0, 1100),
    ("StockTransfer_07.26.xlsx", "TDSheet", C_COLS, 0, 300),
    ("StockTransfer_08.26.xlsx", "TDSheet", C_COLS, 0, 280),
    ("real_forecast_SOP7.xlsx", "Sheet1", F_COLS, 0, 640),
    ("old1_real_forecast_SOP7.xlsx", "Sheet1", F_COLS, 0, 610),
    ("RDC Data.xlsx", "Sheet1", R_COLS, 2, 77),
    ("Pivot Report.xlsx", "Pivot", P_COLS, 4, 60),
    ("Fragment.xlsx", "Sheet1", G_COLS, 7, 3),
    ("twin_a1.xlsx", "Data", X_COLS, 0, 11),
    ("twin_a2.xlsx", "Data", Y_COLS, 5, 12),
]
JUNK = "junk.txt"


def make_bundle(name):
    m = {"name": name, "version": "1.0", "entrypoint": {"module": "gen.main"},
         "outputs": {"html": True, "slots": []}, "engine": "browser"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("gen/__init__.py", "")
        z.writestr("gen/main.py", "def begin(*a, **k):\n    pass\n")
    return buf.getvalue()


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login: {r.status_code}"


def register(c, gen_name, title):
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", make_bundle(gen_name), "application/zip")})
    assert r.status_code == 201, f"register {gen_name}: {r.status_code} {r.text[:300]}"
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": title, "generator_id": gid})
    assert r.status_code == 201, f"create {title}: {r.status_code} {r.text[:300]}"
    return gid, r.json()["id"]


def manifest_files():
    out = [{"name": n, "size": 1000 + i * 7, "mtime_ms": NOW_MS - i * 1000}
           for i, (n, *_r) in enumerate(CORPUS)]
    out.append({"name": JUNK, "size": 12, "mtime_ms": NOW_MS})
    return out


def observed_files(with_schema: bool):
    """The engine's view of the folder. `with_schema=False` is the second run:
    every file unchanged, so schema_json is NULL and the reader must walk back."""
    files = []
    for name, sheet, cols, hdr, rows in CORPUS:
        f = {"dir": 0, "name": name, "kind": "parsed" if with_schema else "cached"}
        if with_schema:
            f["schema"] = {"sheets": [{"name": sheet, "header": list(cols),
                                       "rows": rows, "header_row": hdr}]}
            f["consumed"] = {"sheets": {sheet: {"columns": cols[:2], "all": False}}}
            if name.startswith("DataSalesOrder"):
                # A date profile — the only place `types` and the narrow-date
                # partition fallback get any input at all.
                f["profiles"] = [{"sheet": sheet, "column": "Date", "type": "date",
                                  "nulls": 0, "distinct": 30,
                                  "min": "2026-07-01", "max": "2026-07-31",
                                  "top": [], "sampled": False}]
        files.append(f)
    files.append({"dir": 0, "name": JUNK, "kind": "unreadable"})
    return files


def run_once(c, dash, with_schema, when):
    r = c.post(f"/api/dashboards/{dash}/generator/runs",
               json={"engine": "browser",
                     "input_manifest": {"dirs": [FOLDER], "files": manifest_files()}})
    assert r.status_code == 201, f"run start: {r.status_code} {r.text[:300]}"
    run = r.json()["run_id"]
    r = c.post(f"/api/dashboards/{dash}/generator/runs/{run}/observations",
               json={"seq": 1, "final": True, "files": observed_files(with_schema)})
    assert r.status_code == 200, f"observations: {r.status_code} {r.text[:400]}"
    r = c.post(f"/api/dashboards/{dash}/generator/runs/{run}/error",
               json={"message": "smoke: closing run"})
    assert r.status_code == 200
    stamp(run, when)
    return run


def stamp(run_id, when):
    from app.database import SessionLocal
    from app.models import GenerationRun, SourceObservation
    db = SessionLocal()
    db.query(GenerationRun).filter(GenerationRun.id == run_id).update(
        {"started_at": when}, synchronize_session=False)
    db.query(SourceObservation).filter(SourceObservation.run_id == run_id).update(
        {"created_at": when}, synchronize_session=False)
    db.commit()
    db.close()


def run_empty(c, dash, when):
    """A run that started and observed nothing — the pre-Lineage-v2 shape.

    No `input_manifest`: that is what makes it observation-less. An engine
    that sends one gets a `listed` observation at run START, so "has an
    observation" is a statement about the ENGINE's vintage, which is exactly
    the population G5's half must skip.
    """
    r = c.post(f"/api/dashboards/{dash}/generator/runs", json={"engine": "browser"})
    assert r.status_code == 201, f"empty run start: {r.status_code} {r.text[:300]}"
    run = r.json()["run_id"]
    r = c.post(f"/api/dashboards/{dash}/generator/runs/{run}/error",
               json={"message": "smoke: legacy run, no observations"})
    assert r.status_code == 200
    stamp(run, when)
    return run


def build_fixture():
    with TestClient(app) as c:
        login(c, "admin1")
        _g1, d1 = register(c, "gen-rdcrct", "RDC RCT")
        _g2, d2 = register(c, "gen-ops-rdc-health", "OPS RDC Health")
        # The legacy half: four runs older than every observed one. Enough of
        # them that "the first 50 % of ALL runs" selects nothing else.
        for i, dash in enumerate((d1, d2, d1, d2)):
            run_empty(c, dash, NOW - timedelta(hours=3, minutes=i))
        # Interleaved so the first 50 % of runs still contains both readers.
        run_once(c, d1, True, NOW - timedelta(minutes=40))
        run_once(c, d2, True, NOW - timedelta(minutes=35))
        run_once(c, d1, False, NOW - timedelta(minutes=20))
        run_once(c, d2, False, NOW - timedelta(minutes=15))


# ───────────────────────────── running the probe ─────────────────────────────

def sha_file(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def run_probe(db_path, extra=()):
    out = os.path.join(TMP, "probe-%d.json" % (len(extra) + int(time.time() * 1000) % 100000))
    proc = subprocess.run(
        [sys.executable, os.path.join(REPO, "tools", "lake_probe.py"), db_path,
         "--json", out, *extra],
        capture_output=True, text=True, cwd=REPO,
        env={**os.environ, "OP_DB_PATH": db_path, "PYTHONPATH": REPO})
    if not os.path.exists(out):
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise AssertionError(f"lake_probe produced no JSON (rc={proc.returncode})")
    return proc, json.load(open(out, encoding="utf-8"))


def gate(payload, name):
    for g in payload["gates"]:
        if g["gate"] == name:
            return g
    return {}


def table_named(tables, prefix):
    hits = [t for t in tables if t["name"].startswith(prefix)]
    return hits[0] if len(hits) == 1 else None


def main():
    build_fixture()
    db_path = os.environ["OP_DB_PATH"]

    # The app was writing until a moment ago; fold the WAL into the main file
    # so the byte comparison below is about the PROBE, not about a checkpoint.
    import sqlite3
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()

    before = sha_file(db_path)
    proc, payload = run_probe(db_path)
    after = sha_file(db_path)
    print(proc.stdout)
    if proc.stderr.strip():
        print("stderr:", proc.stderr[:2000], file=sys.stderr)

    check("probe never writes to the database", before == after,
          f"{before[:12]} -> {after[:12]}")

    tables = payload["result"]["tables"]
    check("probe exits 0 when every gate passes",
          proc.returncode == 0 or [g for g in payload["gates"] if g["pass"] is False],
          f"rc={proc.returncode}")

    for g in ("G1", "G2", "G4", "G5"):
        info = gate(payload, g)
        check(f"{g} passes on the fixture", info.get("pass") is True,
              json.dumps(info, ensure_ascii=False)[:400])

    # ── the three TDSheet tables ────────────────────────────────────────────
    td = [t for t in tables if "TDSheet" in t["sheets"]]
    check("TDSheet splits into exactly three tables", len(td) == 3,
          str(sorted(t["name"] for t in td)))
    sales = table_named(tables, "datasalesorder")
    real = table_named(tables, "datarealisation")
    move = table_named(tables, "stocktransfer")
    check("cluster stems: datasalesorder / datarealisation / stocktransfer",
          all(x is not None for x in (sales, real, move)),
          str(sorted(t["name"] for t in tables)))
    if sales:
        check("datasalesorder holds its 3 files and 8 columns",
              sales["n_files"] == 3 and sales["n_columns"] == 8, str(sales["n_files"]))
        check("datasalesorder partitions are month keys",
              sorted(sales["partitions"]) == ["2026-03_2026-06", "2026-07", "2026-08"],
              str(sales["partitions"]))
        check("datasalesorder core = all 8 columns", sales["core_size"] == 8,
              str(sales["core_size"]))
    if real:
        check("datarealisation holds 2 files, 9 columns",
              real["n_files"] == 2 and real["n_columns"] == 9,
              f"{real['n_files']}/{real['n_columns']}")
    if move:
        check("stocktransfer holds 2 files, 11 columns",
              move["n_files"] == 2 and move["n_columns"] == 11,
              f"{move['n_files']}/{move['n_columns']}")

    # ── forecast: one table, two partitions ─────────────────────────────────
    fc = [t for t in tables if "forecast" in t["name"]]
    check("both forecast files land in ONE table", len(fc) == 1,
          str([t["name"] for t in fc]))
    if len(fc) == 1:
        check("forecast table has 2 files and 2 distinct partitions",
              fc[0]["n_files"] == 2 and len(fc[0]["partitions"]) == 2,
              str(fc[0]["partitions"]))
        check("the decorated file keeps `old1` as its partition label",
              any(p and "old1" in p for p in fc[0]["partitions"]),
              str(fc[0]["partitions"]))

    # ── RDC Data: its own table (and header_row=2 survives) ─────────────────
    rdc = table_named(tables, "rdc_data")
    check("RDC Data is a table of its own", rdc is not None and rdc["n_files"] == 1,
          str(rdc and rdc["n_files"]))
    if rdc:
        check("RDC Data keeps header_row=2", rdc["header_row"] == 2, str(rdc["header_row"]))

    # ── non-table sheets: classified out, never a table ─────────────────────
    nt = payload["result"].get("non_tables") or {}
    rows = nt.get("units") or []
    kinds = {(r["file"], r["sheet"]): r["kind"] for r in rows}
    check("the pivot sheet is classified as a pivot",
          kinds.get(("Pivot Report.xlsx", "Pivot")) == "pivot", str(kinds))
    check("the one-column sheet is classified as a fragment",
          kinds.get(("Fragment.xlsx", "Sheet1")) == "fragment", str(kinds))
    check("pivot and fragment counts are reported",
          nt.get("pivot") == 1 and nt.get("fragment") == 1,
          json.dumps({k: v for k, v in nt.items() if k != "units"})[:300])
    check("no table was built from a non-table sheet",
          not [t for t in tables
               if {"Pivot", "Fragment.xlsx"} & (set(t["sheets"]) | {t["display_name"]})],
          str([(t["name"], t["sheets"]) for t in tables]))
    check("the non-table section keeps the file names",
          all(r.get("file") and r.get("reason") for r in rows), str(rows)[:300])

    # ── colliding names are made unique ─────────────────────────────────────
    names = [t["name"] for t in tables]
    check("every table has a unique name", len(names) == len(set(names)), str(sorted(names)))
    # `name` (not `display_name`) is the inference-owned identifier: display_name
    # is a prettified first-cut label, not guaranteed to keep the raw "twin_"
    # prefix (see catalog.py's `name_clusters` -> display_name derivation).
    twins = sorted(t["name"] for t in tables if t["name"].startswith("twin_"))
    check("the two `twin_*` tables exist and are named apart",
          len(twins) == 2 and twins[0] != twins[1], str(twins))
    check("the number token stayed OUT of the twin names and became a partition",
          all(not t["name_stem"].endswith(("a1", "a2")) for t in tables
              if t["name"].startswith("twin_"))
          and sorted(p for t in tables if t["name"].startswith("twin_")
                     for p in t["partitions"]) == ["a1", "a2"],
          str([(t["name"], t["name_stem"], t["partitions"]) for t in tables
               if t["name"].startswith("twin_")]))

    # ── the walk-back actually happened ─────────────────────────────────────
    notes = payload["result"]["notes"]
    check("every parsed/cached file resolved a schema (walk-back works)",
          notes["files_without_schema"] == 0, str(notes))
    check("profiles were read where they exist",
          notes["units_with_profiles"] == 3, str(notes))

    # ── consumers ───────────────────────────────────────────────────────────
    check("every table is consumed by both generators",
          all(len(t["consumers"]) == 2 for t in tables),
          str([(t["name"], t["consumers"]) for t in tables]))

    # ── stability: two probe runs, same ids; and the --half scope ───────────
    _p2, payload2 = run_probe(db_path)
    ids1 = sorted(t["table_id"] for t in tables)
    ids2 = sorted(t["table_id"] for t in payload2["result"]["tables"])
    check("table_ids are stable across probe runs", ids1 == ids2, f"{ids1} vs {ids2}")

    _p3, half = run_probe(db_path, extra=("--half",))
    half_ids = sorted(t["table_id"] for t in half["result"]["tables"])
    check("--half over the first 50 % of runs yields the same table_ids",
          half_ids == ids1, f"{half_ids} vs {ids1}")
    check("G5 reports a full overlap", gate(payload, "G5").get("overlap") == 1.0,
          str(gate(payload, "G5")))
    check("G5 halves the runs that OBSERVED something, not all runs "
          "(4 legacy runs in the fixture, 4 observed -> 2 selected)",
          payload["half"]["runs"] == 2, str(payload["half"]["runs"]))
    check("the half scope still produced tables", payload["half"]["tables"] != [],
          str(payload["half"]))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
