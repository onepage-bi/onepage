"""Regression: slot -> lake conversion at publish (ADR-14b addendum).

Pins:
  * a generator whose semantics claim `sources: [{"slot": "..."}]` gets its
    slot files converted into lake Parquet partitions DURING publish
    (server-side, best-effort — the publish response carries a `lake`
    summary and never fails because of it);
  * the manifest declares `parser_version: "slot-1"` (the conversion's own
    version space — NOT the engine's PARSER_VERSION) and `lake/exec`
    accepts it;
  * the chat path answers real numbers over the converted slot (status ->
    KPI ready on the synthetic `slot__<table>`; intent -> sql -> rows);
  * a declared column the slot does not carry degrades into the honest
    unbound-column note, not a silent NULL;
  * csv, xlsx and json slots convert; an unconvertible slot is an `errors`
    entry, not a crash;
  * a republish with new slot data replaces the lake (old parts pruned,
    exec instance rebuilt, new numbers);
  * semantics validation refuses a path-shaped slot name at registration.
"""
import io
import json
import os
import sys
import tempfile
import zipfile

TMP = tempfile.mkdtemp(prefix="op-lake-slots-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_EXPLORE_ENABLED": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_SAML_SP_BASE_URL": "http://testserver",
})

sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


SEM = {
    "schema_version": 1, "generator": "vanstock-like",
    "glossary": {"stock": "Quantity in the warehouse on the snapshot date"},
    "tables": {
        "stock": {"description": "Stock by warehouse (report slot)",
                  "sources": [{"slot": "stock.csv"}],
                  "columns": {
                      "warehouse": {"source": "Warehouse", "type": "text", "role": "dim"},
                      "product": {"source": "Product", "type": "text", "role": "dim"},
                      "qty": {"source": "Stock, pcs", "type": "number", "role": "measure"},
                      "day": {"source": "Date", "type": "date", "role": "time"}}},
        "prices": {"description": "Price list (xlsx slot)",
                   "sources": [{"slot": "prices.xlsx"}],
                   "columns": {
                       "product": {"source": "Product", "type": "text", "role": "key"},
                       "price": {"source": "Price", "type": "number", "role": "measure"}}},
        "hubs": {"description": "Hub lookup table (json slot)",
                 "sources": [{"slot": "hubs.json"}],
                 "columns": {
                     "warehouse": {"source": "Warehouse", "type": "text", "role": "key"},
                     "hub": {"source": "Hub", "type": "text", "role": "attr"}}},
    },
    "views": {"lines": "SELECT s.*, h.hub FROM stock s LEFT JOIN hubs h USING (warehouse)"},
    "kpis": {
        "stock_total": {"name": "Total stock", "aggregation": "sum",
                        "definition": "Σ Stock, pcs", "question_forms": ["what is the stock", "how much is in stock"],
                        "cell_sql": "SELECT warehouse, hub, product, day, SUM(qty) AS value FROM lines GROUP BY 1, 2, 3, 4"},
    },
}

# The three expected-error substrings below are what app/services/lake_slots.py
# and static/py/_explore.py emit today: those server messages are still
# Russian in the source, so Cyrillic is kept on purpose to match them.
ERR_UNCONVERTIBLE = "cannot be converted"   # "... is not convertible (csv/xlsx/json)"
ERR_AMBIGUOUS = "available:"           # "(available: a, b)"
NOTE_NOT_FOUND = "«Threshold» not found in any dimension"   # "'Threshold' not found in any dimension"

CSV_V1 = ('Warehouse,Product,"Stock, pcs",Date\n'
          "S1,P1,10,2026-08-01\nS1,P2,20,2026-08-01\nS2,P1,5,2026-08-02\n").encode("utf-8")
CSV_V2 = ('Warehouse,Product,"Stock, pcs",Date\n'
          "S1,P1,100,2026-08-10\nS2,P1,50,2026-08-10\n").encode("utf-8")
JSON_SLOT = json.dumps([{"Warehouse": "S1", "Hub": "RegHUB"}, {"Warehouse": "S2", "Hub": "RDC"}],
                       ensure_ascii=False).encode("utf-8")


def make_xlsx():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Product", "Price"])
    ws.append(["P1", 99.5])
    ws.append(["P2", 10])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def bundle(name, sem):
    m = {"name": name, "version": "1.0", "entrypoint": {"module": "g.main"},
         "outputs": {"html": True, "slots": []}, "engine": "browser"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("g/__init__.py", "")
        z.writestr("g/main.py", "def begin(*a, **k):\n    pass\n")
        if sem is not None:
            z.writestr("semantics.json", json.dumps(sem, ensure_ascii=False))
    return buf.getvalue()


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302


def publish(c, dash, files):
    r = c.post(f"/api/dashboards/{dash}/generator/runs", json={"engine": "browser"})
    assert r.status_code == 201, r.text[:200]
    run = r.json()["run_id"]
    parts = {"html_file": ("d.html", b"<!doctype html><html><body>ok</body></html>", "text/html")}
    for i, (name, content, ctype) in enumerate(files):
        parts[f"slot_{i}"] = (name, content, ctype)
    r = c.post(f"/api/dashboards/{dash}/generator/runs/{run}/publish", files=parts)
    assert r.status_code == 200, (r.status_code, r.text[:400])
    return r.json()


with TestClient(app) as c:
    login(c, "admin1")

    # ---- registration: a path-shaped slot name is refused ---------------
    bad = json.loads(json.dumps(SEM))
    bad["tables"]["stock"]["sources"] = [{"slot": "../evil.csv"}]
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", bundle("bad-slots", bad), "application/zip")})
    check("registration: path-shaped slot name -> 4xx", r.status_code in (400, 422),
          f"{r.status_code} {r.text[:200]}")

    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", bundle("vanstock-like", SEM), "application/zip")})
    check("registration: slot semantics accepted", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "Van Stock-like", "generator_id": gid})
    assert r.status_code == 201
    dash = r.json()["id"]
    EXEC = f"/api/dashboards/{dash}/lake/exec"

    # ---- publish converts every claimed slot ---------------------------
    out = publish(c, dash, [
        ("stock.csv", CSV_V1, "text/csv"),
        ("prices.xlsx", make_xlsx(),
         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("hubs.json", JSON_SLOT, "application/json"),
    ])
    lake = out.get("lake") or {}
    check("publish: lake summary says 3 slots converted, no errors",
          lake.get("files") == 3 and not lake.get("errors"), json.dumps(lake, ensure_ascii=False))

    r = c.get(f"/api/dashboards/{dash}/lake/manifest")
    man = r.json() if r.status_code == 200 else {}
    check("manifest: parser_version is the conversion's own (slot-1)",
          man.get("parser_version") == "slot-1", str(man.get("parser_version")))
    check("manifest: synthetic slot__ tables, empty partition, real keys",
          sorted(f["table"] for f in man.get("files", [])) == ["slot__hubs", "slot__prices", "slot__stock"]
          and all(f["partition"] == "slot" and len(f["key"]) == 40 for f in man.get("files", [])),
          json.dumps(man.get("files"), ensure_ascii=False)[:300])

    # ---- the chat path answers over the converted slots ----------------
    r = c.post(EXEC, json={"op": "status"})
    st = r.json() if r.status_code == 200 else {}
    check("exec status: slot-1 manifest accepted", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    ready = {k["key"]: k.get("ready") for g in (st.get("semantics") or {}).get("generators", [])
             for k in g.get("kpis", [])}
    check("exec status: the KPI over slot tables is ready", ready.get("stock_total") is True, ready)

    r = c.post(EXEC, json={"op": "intent",
                           "intent": {"generator_id": gid, "kpi": "stock_total", "dims": ["warehouse"]}})
    it = r.json() if r.status_code == 200 else {}
    check("intent over slot data: SQL built", r.status_code == 200 and it.get("error") is None
          and it.get("sql"), r.text[:300])
    r = c.post(EXEC, json={"op": "sql", "sql": it.get("sql"), "limit": 100})
    out = r.json() if r.status_code == 200 else {}
    got = {row[0]: row[-1] for row in out.get("rows") or []}
    check("sql over slot data: S1=30, S2=5", got == {"S1": 30.0, "S2": 5.0}, json.dumps(out)[:300])

    # the json slot joined in: hub dim answers too
    r = c.post(EXEC, json={"op": "intent",
                           "intent": {"generator_id": gid, "kpi": "stock_total", "dims": ["hub"]}})
    sql_hub = (r.json() or {}).get("sql")
    r = c.post(EXEC, json={"op": "sql", "sql": sql_hub, "limit": 100})
    got = {row[0]: row[-1] for row in (r.json().get("rows") or [])}
    check("join with the json slot: RegHUB=30, RDC=5", got == {"RegHUB": 30.0, "RDC": 5.0},
          json.dumps(got, ensure_ascii=False))

    # ---- a declared column the slot lacks -> honest note ---------------
    login(c, "admin1")
    sem2 = json.loads(json.dumps(SEM))
    sem2["generator"] = "vanstock-like2"
    sem2["tables"]["stock"]["columns"]["sku"] = {"source": "SKU", "type": "text", "role": "dim",
                                                 "optional": True}
    sem2["kpis"]["stock_total"]["cell_sql"] = (
        "SELECT warehouse, hub, product, sku, day, SUM(qty) AS value FROM lines GROUP BY 1, 2, 3, 4, 5")
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", bundle("vanstock-like2", sem2), "application/zip")})
    assert r.status_code == 201, r.text[:300]
    gid2 = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "VS2", "generator_id": gid2})
    dash2 = r.json()["id"]
    publish(c, dash2, [("stock.csv", CSV_V1, "text/csv"),
                       ("prices.xlsx", make_xlsx(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                       ("hubs.json", JSON_SLOT, "application/json")])
    r = c.post(f"/api/dashboards/{dash2}/lake/exec",
               json={"op": "intent", "intent": {"generator_id": gid2, "kpi": "stock_total",
                                                "dims": ["sku"]}})
    it2 = r.json() if r.status_code == 200 else {}
    check("declared-but-absent column: dropped with the honest note",
          it2.get("error") is None and any("sku" in n for n in it2.get("notes") or []),
          json.dumps(it2, ensure_ascii=False)[:300])

    # ---- republish: new data, old parts pruned, instance rebuilt -------
    old_keys = {f["key"] for f in man.get("files", [])}
    publish(c, dash, [("stock.csv", CSV_V2, "text/csv"),
                      ("prices.xlsx", make_xlsx(),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                      ("hubs.json", JSON_SLOT, "application/json")])
    r = c.post(EXEC, json={"op": "intent",
                           "intent": {"generator_id": gid, "kpi": "stock_total", "dims": ["warehouse"]}})
    r = c.post(EXEC, json={"op": "sql", "sql": r.json().get("sql"), "limit": 100})
    got = {row[0]: row[-1] for row in (r.json().get("rows") or [])}
    check("republish: the exec instance answers the NEW slot's numbers",
          got == {"S1": 100.0, "S2": 50.0}, json.dumps(got))
    lakedir = os.path.join(f"{TMP}/uploads", "_lake", dash)
    on_disk = {f[:-8] for f in os.listdir(lakedir) if f.endswith(".parquet")}
    r = c.get(f"/api/dashboards/{dash}/lake/manifest")
    new_keys = {f["key"] for f in r.json().get("files", [])}
    check("republish: stale parts pruned (only the new manifest's keys on disk)",
          on_disk == new_keys and old_keys - new_keys, f"disk={on_disk} new={new_keys}")

    # ---- unconvertible slot = errors entry, publish still ok -----------
    sem3 = json.loads(json.dumps(SEM))
    sem3["generator"] = "vanstock-bin"
    sem3["tables"] = {"stock": {"description": "x", "sources": [{"slot": "stock.bin"}],
                                "columns": {"warehouse": {"source": "Warehouse", "type": "text", "role": "dim"},
                                            "qty": {"source": "Stock, pcs", "type": "number", "role": "measure"}}}}
    sem3["views"] = {"lines": "SELECT * FROM stock"}
    sem3["kpis"] = {"stock_total": {"name": "Stock", "aggregation": "sum", "definition": "x",
                                    "cell_sql": "SELECT warehouse, SUM(qty) AS value FROM lines GROUP BY 1"}}
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", bundle("vanstock-bin", sem3), "application/zip")})
    assert r.status_code == 201, r.text[:300]
    r = c.post("/api/dashboards/generated", json={"title": "VS bin", "generator_id": r.json()["id"]})
    dash3 = r.json()["id"]
    out3 = publish(c, dash3, [("stock.bin", b"\x00\x01\x02", "application/octet-stream")])
    lake3 = out3.get("lake") or {}
    check("unconvertible slot: publish ok, errors entry names the format",
          out3.get("ok") is True and lake3.get("files") == 0
          and any(ERR_UNCONVERTIBLE in (e.get("error") or "") for e in lake3.get("errors") or []),
          json.dumps(lake3, ensure_ascii=False))

    # ---- Van Stock shape: a gzip csv slot converts like a plain one ----
    import gzip
    sem4 = json.loads(json.dumps(SEM))
    sem4["generator"] = "vanstock-gz"
    sem4["tables"] = {"van_stock_daily": {"description": "day × warehouse × SKU",
        "sources": [{"slot": "van_stock_daily.csv.gz"}],
        "columns": {"day": {"source": "day", "type": "date", "role": "time"},
                    "rdc_name": {"source": "rdc_name", "type": "text", "role": "dim"},
                    "stock_qty": {"source": "stock_qty", "type": "number", "role": "measure"}}}}
    sem4["views"] = {"lines": "SELECT * FROM van_stock_daily"}
    sem4["kpis"] = {"stock_total": {"name": "Stock", "aggregation": "sum", "definition": "x",
        "cell_sql": "SELECT rdc_name, day, SUM(stock_qty) AS value FROM lines GROUP BY 1, 2"}}
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", bundle("vanstock-gz", sem4), "application/zip")})
    assert r.status_code == 201, r.text[:300]
    gid4 = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "VS gz", "generator_id": gid4})
    dash4 = r.json()["id"]
    gz = gzip.compress("day,rdc_name,stock_qty\n2026-08-20,RDC-1,1.5\n2026-08-20,RDC-2,0.25\n".encode())
    out4 = publish(c, dash4, [("van_stock_daily.csv.gz", gz, "application/gzip")])
    check("csv.gz slot: converted at publish", (out4.get("lake") or {}).get("files") == 1,
          json.dumps(out4.get("lake"), ensure_ascii=False))
    r = c.get(f"/api/dashboards/{dash4}/lake/manifest")
    m4 = (r.json().get("files") or [{}])[0]
    check("csv.gz slot: engine table name comes from slot_table_name()",
          m4.get("table") == "slot__van_stock_daily" and m4.get("partition") == "slot",
          json.dumps(m4, ensure_ascii=False)[:200])
    r = c.post(f"/api/dashboards/{dash4}/lake/exec",
               json={"op": "intent", "intent": {"generator_id": gid4, "kpi": "stock_total",
                                                "dims": ["rdc_name"]}})
    r = c.post(f"/api/dashboards/{dash4}/lake/exec",
               json={"op": "sql", "sql": r.json().get("sql"), "limit": 100})
    got = {row[0]: row[-1] for row in (r.json().get("rows") or [])}
    check("csv.gz slot: numbers answer (RDC-1=1.5, RDC-2=0.25)",
          got == {"RDC-1": 1.5, "RDC-2": 0.25}, json.dumps(got))

    # ---- extension-less slot declaration (the Van Stock 2026-08-24 miss) --
    import gzip as _gzip
    sem5 = json.loads(json.dumps(SEM))
    sem5["generator"] = "vanstock-stem"
    sem5["tables"] = {"van_stock_daily": {"description": "x",
        "sources": [{"slot": "van_stock_daily"}],
        "columns": {"rdc_name": {"source": "rdc_name", "type": "text", "role": "dim"},
                    "stock_qty": {"source": "stock_qty", "type": "number", "role": "measure"}}}}
    sem5["views"] = {"lines": "SELECT * FROM van_stock_daily"}
    sem5["kpis"] = {"stock_total": {"name": "Stock", "aggregation": "sum", "definition": "x",
        "cell_sql": "SELECT rdc_name, SUM(stock_qty) AS value FROM lines GROUP BY 1"}}
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", bundle("vanstock-stem", sem5), "application/zip")})
    assert r.status_code == 201, r.text[:300]
    sem5_gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "VS stem", "generator_id": sem5_gid})
    dash5 = r.json()["id"]
    gz5 = _gzip.compress("rdc_name,stock_qty\nRDC-1,7\n".encode())
    out5 = publish(c, dash5, [("van_stock_daily.csv.gz", gz5, "application/gzip")])
    check("slot named without extension: unique stem match converts anyway",
          (out5.get("lake") or {}).get("files") == 1
          and not (out5.get("lake") or {}).get("errors"),
          json.dumps(out5.get("lake"), ensure_ascii=False))
    r = c.get(f"/api/dashboards/{dash5}/lake/manifest")
    check("stem match: __file/name carry the DECLARED slot value (binding filter matches)",
          (r.json().get("files") or [{}])[0].get("name") == "van_stock_daily",
          r.text[:200])
    r = c.post(f"/api/dashboards/{dash5}/lake/exec",
               json={"op": "intent", "intent": {"generator_id": sem5_gid, "kpi": "stock_total",
                                                "dims": ["rdc_name"]}})
    r = c.post(f"/api/dashboards/{dash5}/lake/exec",
               json={"op": "sql", "sql": r.json().get("sql"), "limit": 100})
    got5 = {row[0]: row[-1] for row in (r.json().get("rows") or [])}
    check("stem match: ROWS actually flow through the sem view's file filter",
          got5 == {"RDC-1": 7.0}, json.dumps(got5))
    # two slots sharing the stem -> honest error naming what exists
    out6 = publish(c, dash5, [("van_stock_daily.csv.gz", gz5, "application/gzip"),
                              ("van_stock_daily.json", b"[]", "application/json")])
    lk6 = out6.get("lake") or {}
    check("ambiguous stem: error lists the published slots, nothing guessed",
          lk6.get("files") == 0 and any(ERR_AMBIGUOUS in (e.get("error") or "")
                                        for e in lk6.get("errors") or []),
          json.dumps(lk6, ensure_ascii=False))

    # ---- residue probe: the DATA settles a question word (2026-08-24) ----
    # 'S2' exists only in warehouse -> auto-filter + note; 'BOTH' exists in
    # warehouse AND product -> clarify instead of SQL; junk -> silence.
    sem7 = json.loads(json.dumps(SEM))
    sem7["generator"] = "vanstock-probe"
    sem7["tables"] = {"stock": {"description": "x", "sources": [{"slot": "stock.csv"}],
        "columns": {"warehouse": {"source": "Warehouse", "type": "text", "role": "dim"},
                    "product": {"source": "Product", "type": "text", "role": "dim"},
                    "qty": {"source": "Stock, pcs", "type": "number", "role": "measure"}}}}
    sem7["views"] = {"lines": "SELECT * FROM stock"}
    sem7["kpis"] = {"stock_total": {"name": "Stock", "aggregation": "sum", "definition": "x",
        "cell_sql": "SELECT warehouse, product, SUM(qty) AS value FROM lines GROUP BY 1, 2"}}
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", bundle("vanstock-probe", sem7), "application/zip")})
    assert r.status_code == 201, r.text[:300]
    gid7 = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "VS probe", "generator_id": gid7})
    dash7 = r.json()["id"]
    csv7 = ('Warehouse,Product,"Stock, pcs"\n'
            "S1,P1,10\nS2,BOTH,5\nBOTH,P2,7\n").encode("utf-8")
    publish(c, dash7, [("stock.csv", csv7, "text/csv")])
    EX7 = f"/api/dashboards/{dash7}/lake/exec"

    r = c.post(EX7, json={"op": "intent", "intent": {"generator_id": gid7, "kpi": "stock_total",
                                                     "dims": [], "probe_words": ["S2"]}})
    it7 = r.json()
    check("probe: a word living in ONE dim becomes the filter, with the note",
          it7.get("error") is None and it7.get("sql") and "warehouse" in it7["sql"]
          and any("S2" in n and "warehouse" in n for n in it7.get("notes") or []),
          json.dumps(it7, ensure_ascii=False)[:300])
    r = c.post(EX7, json={"op": "sql", "sql": it7.get("sql"), "limit": 100})
    val7 = ((r.json().get("rows") or [[None]])[0] or [None])[-1]
    check("probe: the filtered number is the S2 number", val7 == 5.0, r.json().get("rows"))

    r = c.post(EX7, json={"op": "intent", "intent": {"generator_id": gid7, "kpi": "stock_total",
                                                     "dims": [], "probe_words": ["BOTH"]}})
    it8 = r.json()
    opts = {(o["dim"], o["value"]) for cl in it8.get("clarify") or [] for o in cl["options"]}
    check("probe: a word living in TWO dims -> clarify instead of SQL",
          it8.get("sql") is None and it8.get("error") is None
          and opts == {("warehouse", "BOTH"), ("product", "BOTH")},
          json.dumps(it8, ensure_ascii=False)[:300])

    r = c.post(EX7, json={"op": "intent", "intent": {"generator_id": gid7, "kpi": "stock_total",
                                                     "dims": [], "probe_words": ["Threshold"]}})
    it9 = r.json()
    check("probe: a word found nowhere -> SQL still runs, no clarify chips, but a note says it was not found",
          it9.get("sql") and not it9.get("clarify")
          and any(NOTE_NOT_FOUND in n for n in it9.get("notes") or []),
          json.dumps(it9, ensure_ascii=False)[:300])

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES"); sys.exit(1)
print("ALL PASS")
