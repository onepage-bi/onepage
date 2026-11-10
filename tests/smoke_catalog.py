"""Smoke test for canonical-table inference (app/services/catalog.py).

The fixture is the pilot folder in miniature, built through the REAL API the
way tests/smoke_quality.py and tests/smoke_lake_probe.py build theirs: two
generators registered from bundles, runs started with an `input_manifest`,
observations POSTed with schemas/consumed/profiles. Nothing is hand-written
into source_files — if `rebuild` can read this database it can read
production's.

Two folders, because one folder cannot test a budget:

  * `OPS_RDC_Exports` — the TDSheet union (`DataSalesOrder_*`,
    `DataRealisation_*`, `Transfers_*`, three months each, one sheet name
    for all nine files), the forecast pair (`real_forecast_SOP7.xlsx` and its
    decorated twin `old1_real_forecast_SOP7.xlsx` — ONE table, two
    partitions), `RDC Data.xlsx` (header_row 2, so it also exercises the hard
    split), and the two shapes that must never become tables: a pivot corner
    and a one-column fragment.
  * `Stock_Extra` — a second source, read by ONE of the two generators. It
    makes the sixth table, it makes the consumer lists differ, and it is what
    the time budget has to skip.

What is asserted, beyond the counts: `table_id`s are identical across two
rebuilds (an id that moves makes every confirmation and every ownership row
meaningless); a human's `display_name` and `confirmed` status survive the next
rebuild; an inferred row that vanishes from the inference is reported stale
with its `last_seen_at` untouched rather than deleted; and `suggest_inputs`
produces a patch that `genmanifest.validate` accepts when spliced into a
legacy manifest — together with the file that patch would stop reading.
"""
import io
import json
import os
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="op-catalog-smoke-")
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
from app.services import catalog, genmanifest  # noqa: E402

FAILURES = []
NOW = datetime.utcnow()
NOW_MS = int(time.time() * 1000)
MAIN = "OPS_RDC_Exports"
EXTRA = "Stock_Extra"


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


# ───────────────────────────── fixture data ──────────────────────────────────

A_COLS = ["Item", "Order", "Date", "Quantity", "Amount",
          "Store", "Counterparty", "Manager"]                     # 8
B_COLS = ["Item", "Date", "Sale", "Batch", "Price",
          "VAT", "Buyer", "Sales region", "Shipment"]             # 9, shares 2 with A
C_COLS = ["Transfer No", "From", "To", "Transfer date", "Position",
          "Units", "UoM", "Responsible", "Status", "Remark",
          "Transport"]                                             # 11, disjoint
F_COLS = ["SKU", "Week", "Forecast", "Actual", "Bias", "MAPE", "Channel", "Plant"]
R_COLS = ["Region", "Stock", "Days", "Owner", "Comment", "Segment", "Flag"]
S_COLS = ["Warehouse", "SKU", "Qty", "Value", "Updated"]
P_COLS = ["Row Labels", "Grand Total", "2024", "2025"]        # a pivot corner
G_COLS = ["11002126"]                                          # a fragment

# (file name, sheet, columns, header_row, rows) — folder 0
MAIN_CORPUS = [
    ("DataSalesOrder_06.26.xlsx", "TDSheet", A_COLS, 0, 4000),
    ("DataSalesOrder_07.26.xlsx", "TDSheet", A_COLS, 0, 1200),
    ("DataSalesOrder_08.26.xlsx", "TDSheet", A_COLS, 0, 900),
    ("DataRealisation_06.26.xlsx", "TDSheet", B_COLS, 0, 1700),
    ("DataRealisation_07.26.xlsx", "TDSheet", B_COLS, 0, 1500),
    ("DataRealisation_08.26.xlsx", "TDSheet", B_COLS, 0, 1100),
    ("Transfers_06.26.xlsx", "TDSheet", C_COLS, 0, 320),
    ("Transfers_07.26.xlsx", "TDSheet", C_COLS, 0, 300),
    ("Transfers_08.26.xlsx", "TDSheet", C_COLS, 0, 280),
    ("real_forecast_SOP7.xlsx", "Sheet1", F_COLS, 0, 640),
    ("old1_real_forecast_SOP7.xlsx", "Sheet1", F_COLS, 0, 610),
    ("RDC Data.xlsx", "Sheet1", R_COLS, 2, 77),
    ("Pivot Report.xlsx", "Pivot", P_COLS, 4, 60),
    ("Fragment.xlsx", "Sheet1", G_COLS, 7, 3),
]
# folder 1 — the second source
EXTRA_CORPUS = [
    ("stock_07.26.xlsx", "STOCK", S_COLS, 0, 5000),
    ("stock_08.26.xlsx", "STOCK", S_COLS, 0, 5200),
]


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


def corpus_for(with_extra):
    rows = [(0, r) for r in MAIN_CORPUS]
    if with_extra:
        rows += [(1, r) for r in EXTRA_CORPUS]
    return rows


def manifest_files(with_extra):
    return [{"name": r[0], "size": 1000 + i * 7, "mtime_ms": NOW_MS - i * 1000,
             **({"dir": d} if d else {})}
            for i, (d, r) in enumerate(corpus_for(with_extra))]


def observed_files(with_extra, with_schema):
    """The engine's view. `with_schema=False` is the second run: every file
    unchanged, so schema_json is NULL and the reader must walk back."""
    files = []
    for d, (name, sheet, cols, hdr, rows) in corpus_for(with_extra):
        f = {"dir": d, "name": name, "kind": "parsed" if with_schema else "cached"}
        if with_schema:
            f["schema"] = {"sheets": [{"name": sheet, "header": list(cols),
                                       "rows": rows, "header_row": hdr}]}
            f["consumed"] = {"sheets": {sheet: {"columns": cols[:2], "all": False}}}
        files.append(f)
    return files


def run_once(c, dash, with_extra, with_schema, when):
    dirs = [MAIN, EXTRA] if with_extra else [MAIN]
    r = c.post(f"/api/dashboards/{dash}/generator/runs",
               json={"engine": "browser",
                     "input_manifest": {"dirs": dirs,
                                        "files": manifest_files(with_extra)}})
    assert r.status_code == 201, f"run start: {r.status_code} {r.text[:300]}"
    run = r.json()["run_id"]
    r = c.post(f"/api/dashboards/{dash}/generator/runs/{run}/observations",
               json={"seq": 1, "final": True,
                     "files": observed_files(with_extra, with_schema)})
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


def build_fixture():
    """-> (generator id of the two-folder reader, generator id of the other)"""
    with TestClient(app) as c:
        login(c, "admin1")
        g1, d1 = register(c, "gen-rdcrct", "RDC RCT")
        g2, d2 = register(c, "gen-ops-rdc-health", "OPS RDC Health")
        # gen-rdcrct reads BOTH folders, gen-ops-rdc-health only the main one:
        # that is what makes the consumer lists differ, and what gives the
        # budget something to skip.
        run_once(c, d1, True, True, NOW - timedelta(minutes=40))
        run_once(c, d2, False, True, NOW - timedelta(minutes=35))
        run_once(c, d1, True, False, NOW - timedelta(minutes=20))
        return g1, g2


# ───────────────────────────── helpers ───────────────────────────────────────

def session():
    from app.database import SessionLocal
    return SessionLocal()


def tables(db):
    from app.models import CatalogTable
    return {t.name: t for t in db.query(CatalogTable).all()}


def by_prefix(rows, prefix):
    hits = [t for name, t in rows.items() if name.startswith(prefix)]
    return hits[0] if len(hits) == 1 else None


def partitions(db, table_id):
    from app.models import CatalogPartition
    return db.query(CatalogPartition).filter(
        CatalogPartition.table_id == table_id).all()


def main():
    g1, g2 = build_fixture()
    db = session()

    from app.models import (CatalogConsumer, CatalogNonTable, CatalogPartition,
                            CatalogTable, DataSource)

    # ── rebuild #1 ──────────────────────────────────────────────────────────
    stats = catalog.rebuild(db, budget_s=60.0)
    rows = tables(db)
    check("rebuild covers both observed sources", stats["sources"] == 2, str(stats))
    check("rebuild yields exactly 6 tables", len(rows) == 6, str(sorted(rows)))
    check("every table starts as `inferred` (ADR-12: suggester, not automat)",
          all(t.status == "inferred" for t in rows.values()),
          str({n: t.status for n, t in rows.items()}))

    sales = by_prefix(rows, "datasalesorder")
    real = by_prefix(rows, "datarealisation")
    move = by_prefix(rows, "transfers")
    fc = by_prefix(rows, "real_forecast")
    rdc = by_prefix(rows, "rdc_data")
    stock = by_prefix(rows, "stock")
    check("TDSheet split into three differently-named tables",
          all(x is not None for x in (sales, real, move)), str(sorted(rows)))
    check("the forecast pair, RDC Data and the second folder are the other three",
          all(x is not None for x in (fc, rdc, stock)), str(sorted(rows)))

    if sales:
        check("datasalesorder holds 3 files and all 8 columns are core",
              sales.n_files == 3 and len(sales.get_core_cols()) == 8,
              f"{sales.n_files}/{len(sales.get_core_cols())}")
        check("column list carries norm + display + present_ratio + type",
              all({"norm", "display", "present_ratio", "type"} <= set(c)
                  for c in sales.get_columns())
              and any(c["display"] == "Item" for c in sales.get_columns()),
              str(sales.get_columns()[:2]))
        check("evidence points at real source_file/run rows",
              len(sales.get_evidence()) == 3
              and all(e.get("source_file_id") and e.get("run_id")
                      for e in sales.get_evidence()),
              str(sales.get_evidence())[:300])
        keys = sorted(p.key for p in partitions(db, sales.table_id))
        check("datasalesorder partition keys are the three months",
              keys == ["2026-06", "2026-07", "2026-08"], str(keys))
    if rdc:
        check("RDC Data keeps header_row=2", rdc.header_row == 2, str(rdc.header_row))
    if fc:
        keys = sorted(p.key for p in partitions(db, fc.table_id))
        check("the forecast pair is ONE table with the decoration as a partition",
              fc.n_files == 2 and keys == ["old1_sop7", "sop7"], str(keys))

    # ── consumers ───────────────────────────────────────────────────────────
    def consumers(t):
        return sorted(c.generator_id for c in db.query(CatalogConsumer)
                      .filter(CatalogConsumer.table_id == t.table_id).all())

    check("a main-folder table is consumed by BOTH generators",
          sales is not None and consumers(sales) == sorted([g1, g2]),
          str(sales and consumers(sales)))
    check("the second folder's table is consumed by one generator only",
          stock is not None and consumers(stock) == [g1],
          str(stock and consumers(stock)))

    # ── the pivot and the fragment: non-tables, never tables ────────────────
    nts = {(n.file_name, n.sheet): n for n in db.query(CatalogNonTable).all()}
    check("the pivot corner landed in catalog_nontables",
          nts.get(("Pivot Report.xlsx", "Pivot")) is not None
          and nts[("Pivot Report.xlsx", "Pivot")].kind == "pivot", str(sorted(nts)))
    check("the one-column sheet landed there as a fragment",
          nts.get(("Fragment.xlsx", "Sheet1")) is not None
          and nts[("Fragment.xlsx", "Sheet1")].kind == "fragment", str(sorted(nts)))
    check("non-tables keep their headers and a reason",
          all(n.headers_json and n.reason for n in nts.values()),
          str([(n.file_name, n.reason) for n in nts.values()]))
    check("no table was built from a non-table sheet",
          not [t for t in rows.values()
               if {"Pivot"} & set(t.get_sheets())
               or (t.display_name or "").startswith(("Pivot", "Fragment"))],
          str([(t.name, t.get_sheets()) for t in rows.values()]))

    # ── rebuild #2: same ids, no duplicates ─────────────────────────────────
    ids1 = sorted(t.table_id for t in rows.values())
    n_parts1 = db.query(CatalogPartition).count()
    stats2 = catalog.rebuild(db, budget_s=60.0)
    rows2 = tables(db)
    ids2 = sorted(t.table_id for t in rows2.values())
    check("a second rebuild mints the same table_ids", ids1 == ids2, f"{ids1} vs {ids2}")
    check("a second rebuild creates no rows", stats2["created"] == 0, str(stats2))
    check("partitions are replaced, not doubled",
          db.query(CatalogPartition).count() == n_parts1,
          f"{n_parts1} -> {db.query(CatalogPartition).count()}")
    # 5 tables in the main folder, both generators; 1 table in the extra
    # folder, one generator.
    check("consumers are replaced, not doubled",
          db.query(CatalogConsumer).count() == 5 * 2 + 1,
          str(db.query(CatalogConsumer).count()))

    # ── a human edit survives the next rebuild ──────────────────────────────
    human = rows2["rdc_data"] if "rdc_data" in rows2 else rdc
    human.display_name = "RDC warehouse stock"
    human.status = "confirmed"
    human.confirmed_at = datetime.utcnow()
    human.version = 2                      # what the API bumps on a human edit
    db.commit()
    catalog.rebuild(db, budget_s=60.0)
    db.expire_all()
    again = db.query(CatalogTable).filter(CatalogTable.table_id == human.table_id).first()
    check("a confirmed status survives a rebuild", again.status == "confirmed",
          str(again.status))
    check("a human display_name survives a rebuild",
          again.display_name == "RDC warehouse stock", str(again.display_name))
    check("inference fields are still refreshed on that row",
          again.n_files == 1 and again.confidence is not None, str(again.n_files))

    # ── a table that disappears is stale, not deleted ───────────────────────
    ds_main = db.query(DataSource).filter(DataSource.key_name == MAIN).first()
    ghost_seen = datetime(2020, 1, 1)
    db.add(CatalogTable(table_id="t_ghost000000", data_source_id=ds_main.id,
                        name="ghost", display_name="ghost.xlsx", status="inferred",
                        first_seen_at=ghost_seen, last_seen_at=ghost_seen, version=1))
    db.commit()
    stats3 = catalog.rebuild(db, ds_main.id, budget_s=60.0)
    db.expire_all()
    ghost = db.query(CatalogTable).filter(CatalogTable.table_id == "t_ghost000000").first()
    check("a table that vanished from the inference is reported stale",
          any(s["table_id"] == "t_ghost000000" for s in stats3["stale"]), str(stats3["stale"]))
    check("...and is neither deleted nor re-stamped as fresh",
          ghost is not None and ghost.last_seen_at == ghost_seen,
          str(ghost and ghost.last_seen_at))

    # ── the time budget ─────────────────────────────────────────────────────
    stats4 = catalog.rebuild(db, budget_s=0.0)
    check("a tiny budget skips sources instead of overrunning",
          len(stats4["skipped"]) > 0, str(stats4))
    check("...and still does the first one (a zero budget is not a no-op)",
          stats4["sources"] == 1, str(stats4))

    # ── the suggester ───────────────────────────────────────────────────────
    patch = catalog.suggest_inputs(db, g1)
    matches = [e["match"] for e in patch["inputs"]]
    check("suggest_inputs covers all six tables of this generator's sources",
          len(patch["inputs"]) == 6, str(matches))
    check("the globs generalise the date tokens",
          "DataSalesOrder_*.xlsx" in matches and "Transfers_*.xlsx" in matches,
          str(matches))
    check("the forecast glob keeps the SOP word and wildcards its number",
          "real_forecast_SOP*.xlsx" in matches, str(matches))
    check("a single-file table gets an exact match, not a wildcard",
          "RDC Data.xlsx" in matches, str(matches))
    check("every entry carries a role and its sheets' core columns",
          all(e["role"] == "data" and e["sheets"] and e["sheets"][0]["columns"]
              for e in patch["inputs"]),
          json.dumps(patch["inputs"][:1], ensure_ascii=False)[:400])

    legacy = {"name": "gen-rdcrct", "version": "1.0",
              "entrypoint": {"module": "gen.main"},
              "outputs": {"html": True, "slots": []}, "engine": "browser"}
    errors = genmanifest.validate({**legacy, "inputs": patch["inputs"]})
    check("the patch validates when spliced into a legacy manifest",
          errors == [], str(errors)[:500])

    check("the decorated forecast file is reported as one the patch would drop",
          "old1_real_forecast_SOP7.xlsx" in patch["unmatched"], str(patch["unmatched"]))
    check("the pivot and the fragment are reported as dropped too",
          {"Pivot Report.xlsx", "Fragment.xlsx"} <= set(patch["unmatched"]),
          str(patch["unmatched"]))
    check("every clustered file is reported as matched",
          {"DataSalesOrder_06.26.xlsx", "Transfers_08.26.xlsx", "RDC Data.xlsx",
           "stock_07.26.xlsx"} <= set(patch["matched"]), str(patch["matched"]))
    check("matched and unmatched are disjoint",
          not (set(patch["matched"]) & set(patch["unmatched"])),
          str(sorted(set(patch["matched"]) & set(patch["unmatched"]))))
    check("both sources are reported separately",
          sorted(s["key_name"] for s in patch["sources"]) == sorted([MAIN, EXTRA]),
          str([s["key_name"] for s in patch["sources"]]))

    # The other generator never read the second folder, so its patch must not
    # mention that folder's table.
    patch2 = catalog.suggest_inputs(db, g2)
    check("the suggester is scoped to the sources the generator actually read",
          all("stock" not in e["match"] for e in patch2["inputs"])
          and len(patch2["inputs"]) == 5,
          str([e["match"] for e in patch2["inputs"]]))

    # ── a rejected table never comes back as a suggestion ───────────────────
    if stock:
        db.query(CatalogTable).filter(CatalogTable.table_id == stock.table_id).update(
            {"status": "rejected", "version": 2}, synchronize_session=False)
        db.commit()
        catalog.rebuild(db, budget_s=60.0)
        db.expire_all()
        after = db.query(CatalogTable).filter(
            CatalogTable.table_id == stock.table_id).first()
        check("a rejected table is not resurrected by a rebuild",
              after is not None and after.status == "rejected", str(after and after.status))
        check("a rejected table drops out of the suggestion",
              all("stock" not in e["match"] for e in catalog.suggest_inputs(db, g1)["inputs"]),
              str([e["match"] for e in catalog.suggest_inputs(db, g1)["inputs"]]))

    # ── best_glob never emits a bare wildcard-only stem ─────────────────────
    check("a pure-date file name globs its stem away",
          catalog.file_glob("07.26.xlsx") == "*.xlsx", catalog.file_glob("07.26.xlsx"))
    bg = catalog.best_glob(["07.26.xlsx", "DataSalesOrder_07.26.xlsx"])
    check("best_glob rejects the bare '*.xlsx' candidate in favour of the specific one",
          bg == "DataSalesOrder_*.xlsx", str(bg))
    bg2 = catalog.best_glob(["2026.xlsx", "Sales_2026.xlsx"])
    check("...same for a bare-year stem",
          bg2 == "Sales_*.xlsx", str(bg2))
    bg3 = catalog.best_glob(["07.26.xlsx", "03.26.xlsx"])
    check("...and when EVERY candidate is wildcard-only, best_glob honestly gives up (None)",
          bg3 is None, str(bg3))

    # ── display_name is never a raw member file name ────────────────────────
    check("a confirmed/inferred table's display_name is not a bare file name "
          "(one partition's identity presented as the table's)",
          all(not (t.display_name or "").lower().endswith((".xlsx", ".xls", ".xlsb", ".csv"))
              for t in rows2.values()),
          str({n: t.display_name for n, t in rows2.items()}))
    if sales:
        check("sales' display_name is derived from the inference-owned `name`, title-cased",
              sales.display_name == "Datasalesorder", sales.display_name)

    # ── superseded_by: core-column drift links the old row to its successor ─
    from app.models import CatalogTable as _CT
    ds_super = ds_main.id
    now2 = datetime.utcnow()
    old_row = _CT(table_id="t_drift_old000", data_source_id=ds_super,
                  name="drifter", display_name="Drifter", status="confirmed",
                  first_seen_at=now2, last_seen_at=now2, version=2,
                  core_cols_json='["a","b","c"]')
    db.add(old_row)
    db.commit()
    fake_res = {"tables": [{
        "table_id": "t_drift_new111", "data_source_id": ds_super,
        "name": "drifter", "name_stem": "drifter", "display_name": "drifter.xlsx",
        "sheets": ["Sheet1"], "core": ["a", "b"], "columns": [], "header_row": 0,
        "confidence": 0.9, "evidence": [], "n_files": 1,
        "files": [{"partition": "p1", "file": "drifter_p1.xlsx", "sheet": "Sheet1",
                  "size": 10, "mtime_ms": 1, "rows": 1, "run_id": None, "generators": []}],
    }], "non_tables": {"units": []}}
    stats_drift = {"sources": 0, "tables": 0, "created": 0, "updated": 0,
                   "partitions": 0, "consumers": 0, "nontables": 0,
                   "stale": [], "skipped": [], "seconds": 0.0}
    catalog._persist_source(db, ds_super, fake_res, now2, stats_drift)
    db.commit()
    db.expire_all()
    old_after = db.query(_CT).filter(_CT.table_id == "t_drift_old000").first()
    check("core-column drift under the same name links old row -> new via superseded_by",
          old_after is not None and old_after.superseded_by == "t_drift_new111",
          str(old_after and old_after.superseded_by))
    check("the confirmed old row is NOT deleted (only marked stale/superseded)",
          old_after is not None and old_after.status == "confirmed", str(old_after and old_after.status))

    db.close()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
