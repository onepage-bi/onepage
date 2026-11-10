"""Seed one legacy generator + two runs whose observations describe the
explore fixture folder, then rebuild the catalog.

`legacy` = the bundle manifest declares no `inputs`/`required_files`, which is
what ADR-12 calls a legacy consumer; that is exactly the first-run shape the
/explore flow has to work for (tables are inferred, the user confirms them).
"""
import io
import json
import zipfile

FOLDER = "Explore_Fixture"

# Cyrillic on purpose: the fixture mimics a Russian 1C export, so the column
# headers below are the real binding contract for the semantics `source`s, and
# tests/smoke_lake_exec.py / tests/smoke_explore_answers.py build partitions
# with the same headers. The KPI names and question_forms stay Russian too:
# the form matcher (nl2sql.match_forms) is exercised with Russian questions.
ORDER_COLS = ["Номер", "Дата", "Клиент", "Склад", "Сумма"]
REAL_COLS = ["Номер заказа", "Дата отгрузки", "Склад", "Сумма"]
FCST_COLS = ["Date", "Код продукта", "Сектор", "BV Name", "Qty"]

# (file, sheet, columns, header_row, rows)
CORPUS_A = [
    ("DataSalesOrder_07.26.xlsx", "TDSheet", ORDER_COLS, 2, 6),
    ("DataRealisation_07.26.xlsx", "TDSheet", REAL_COLS, 2, 4),
    ("real_forecast_SOP7.xlsx", "Sheet1", FCST_COLS, 0, 3),
]
CORPUS_B = [
    ("DataSalesOrder_08.26.xlsx", "TDSheet", ORDER_COLS, 2, 4),
    ("DataRealisation_08.26.xlsx", "TDSheet", REAL_COLS, 2, 2),
    ("real_forecast_SOP7.xlsx", "Sheet1", FCST_COLS, 0, 3),
]


# ADR-13: the fixture generator ships semantics over the fixture's own
# exports, so the Chrome run exercises the real worker path — `sem` views
# over indexed partitions, a declared join view, ratio + value KPIs, intent
# -> SQL — not a stub.
SEMANTICS = {
    "schema_version": 1, "generator": "explore-fixture-gen",
    "glossary": {"realised order": "An order that has a row in the realisation export"},
    "rules": {"reporting_start": "2026-07-01"},
    "tables": {
        "orders": {"description": "Orders (DataSalesOrder)",
                   "sources": [{"file": "DataSalesOrder_*.xlsx", "sheet": "TDSheet"}],
                   "business_key": "order_no",
                   "columns": {"order_no": {"source": "Номер", "type": "text", "role": "key"},
                               "day": {"source": "Дата", "type": "date", "role": "time"},
                               "client": {"source": "Клиент", "type": "text", "role": "dim"},
                               "sklad": {"source": "Склад", "type": "text", "role": "dim"},
                               "amount": {"source": "Сумма", "type": "number", "role": "measure"}}},
        "realisations": {"description": "Realisations (DataRealisation)",
                         "sources": [{"file": "DataRealisation_*.xlsx", "sheet": "TDSheet"}],
                         "columns": {"order_no": {"source": "Номер заказа", "type": "text", "role": "key"},
                                     "ship_date": {"source": "Дата отгрузки", "type": "date", "role": "time"},
                                     "sklad": {"source": "Склад", "type": "text", "role": "dim"},
                                     "amount": {"source": "Сумма", "type": "number", "role": "measure"}}},
    },
    "views": {"lines": "SELECT o.*, (r.order_no IS NOT NULL) AS realised FROM orders o "
                       "LEFT JOIN (SELECT DISTINCT order_no FROM realisations) r USING (order_no) "
                       "WHERE o.day >= DATE '{reporting_start}'"},
    "kpis": {
        "realised_share": {"name": "Доля реализованных заказов", "aggregation": "ratio",
                           "definition": "Orders with a realisation / all orders since 2026-07-01",
                           "question_forms": ["доля реализованных", "сколько заказов реализовано"],
                           "cell_sql": "SELECT sklad, client, day, SUM(CASE WHEN realised THEN 1 ELSE 0 END) AS n, COUNT(*) AS d FROM lines GROUP BY 1, 2, 3"},
        "order_amount": {"name": "Сумма заказов", "aggregation": "sum",
                         "definition": "Σ order amount",
                         "question_forms": ["сумма заказов", "на сколько заказали"],
                         "cell_sql": "SELECT sklad, client, day, SUM(amount) AS value FROM lines GROUP BY 1, 2, 3"},
    },
}


def _bundle(name, semantics=None):
    m = {"name": name, "version": "1.0", "entrypoint": {"module": "gen.main"},
         "outputs": {"html": True, "slots": []}, "engine": "browser"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("gen/__init__.py", "")
        z.writestr("gen/main.py", "def begin(*a, **k):\n    pass\n")
        if semantics is not None:
            z.writestr("semantics.json", json.dumps(semantics, ensure_ascii=False))
    return buf.getvalue()


def _manifest(corpus, base_mtime):
    return {"dirs": [FOLDER],
            "files": [{"name": f[0], "size": 5000 + i * 11, "mtime_ms": base_mtime + i * 1000,
                       "dir": 0} for i, f in enumerate(corpus)]}


def _observed(corpus):
    out = []
    for name, sheet, cols, hdr, rows in corpus:
        out.append({
            "dir": 0, "name": name, "kind": "parsed",
            "schema": {"sheets": [{"name": sheet, "header": list(cols), "rows": rows,
                                   "header_row": hdr}]},
            "consumed": {"sheets": {sheet: {"columns": cols[:2], "all": False}}},
        })
    return out


def _run(c, dash, corpus, base_mtime):
    r = c.post(f"/api/dashboards/{dash}/generator/runs",
               json={"engine": "browser", "input_manifest": _manifest(corpus, base_mtime)})
    assert r.status_code == 201, (r.status_code, r.text[:300])
    run = r.json()["run_id"]
    r = c.post(f"/api/dashboards/{dash}/generator/runs/{run}/observations",
               json={"seq": 1, "final": True, "files": _observed(corpus)})
    assert r.status_code == 200, (r.status_code, r.text[:400])
    c.post(f"/api/dashboards/{dash}/generator/runs/{run}/error", json={"message": "seed: closing run"})
    return run


def seed(c, confirm=True):
    """`c` is anything with .post/.get returning a response with .json()/.status_code
    (a TestClient or the tiny HTTP shim in the Chrome harness)."""
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", _bundle("explore-fixture-gen", SEMANTICS), "application/zip")})
    assert r.status_code == 201, (r.status_code, r.text[:300])
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "Explore Fixture", "generator_id": gid})
    assert r.status_code == 201, (r.status_code, r.text[:300])
    dash = r.json()["id"]

    _run(c, dash, CORPUS_A, 1785000000000)
    _run(c, dash, CORPUS_B, 1787600000000)

    r = c.post("/api/catalog/rebuild", json={})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    stats = r.json()

    confirmed = []
    if confirm:
        r = c.get("/api/catalog/tables")
        for t in r.json()["tables"]:
            rr = c.post(f"/api/catalog/tables/{t['table_id']}/confirm",
                        json={"version": t.get("version")})
            confirmed.append((t["name"], rr.status_code))
    return {"dashboard": dash, "generator": gid, "rebuild": stats, "confirmed": confirmed}
