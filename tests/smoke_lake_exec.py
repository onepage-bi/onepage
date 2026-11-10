"""Regression: server-side /explore execution over the lake (ADR-14b,
`POST /api/dashboards/{id}/lake/exec`).

Pins:
  * ops mirror the browser worker 1:1 — `status` (tables/stats/semantics/
    manifest), `intent` (intent -> SQL, never executed), `sql` (guarded run),
    `export` (xlsx bytes);
  * real numbers: the engine indexes the published Parquet parts natively and
    a ratio KPI + a sum KPI answer correctly over two partitions;
  * the ENGINE's guard is the trust boundary: read_parquet/ATTACH in `sql`
    come back as an engine error, never rows, and never a 500;
  * access = `require_dashboard_access` (a visitor without reach gets
    403/404), exactly like GET …/lake/parts;
  * parser_version mismatch -> 409 {code: "parser"};
  * no manifest -> 404; `OP_LAKE_EXEC=false` -> 404;
  * a manifest REPUBLISH rebuilds the instance (new data answers, old frames
    do not survive).
"""
import json
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="op-lake-exec-")
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
sys.path.insert(0, os.path.join(os.getcwd(), "tests", "chrome"))
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
import seed_explore  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


# Rows per source file: header source -> list of values (as text — the way
# Excel frames register), plus the engine's reserved partition/file columns.
ORDERS = {
    "DataSalesOrder_07.26.xlsx": [
        ("O1", "01.07.2026", "ClientA", "S1", "100"),
        ("O2", "02.07.2026", "ClientA", "S2", "200"),
        ("O3", "03.07.2026", "ClientB", "S1", "300,5"),
    ],
    "DataSalesOrder_08.26.xlsx": [
        ("O4", "05.08.2026", "ClientA", "S1", "50"),
        ("O5", "06.08.2026", "ClientB", "S2", "150"),
    ],
}
REALS = {
    "DataRealisation_07.26.xlsx": [("O1", "04.07.2026", "S1", "100"), ("O3", "05.07.2026", "S1", "300,5")],
    "DataRealisation_08.26.xlsx": [("O4", "07.08.2026", "S1", "50")],
}


# The server timeout message (app/services/lake_exec.py, static/py/_explore.py):
# "Query stopped: over N s. ..." / "The query ran longer than N s and was stopped."
ERR_TIMEOUT = "stopped"

ORDER_KEYS = [col.casefold() for col in seed_explore.ORDER_COLS]
REAL_KEYS = [col.casefold() for col in seed_explore.REAL_COLS]


def build_part(path, cols, rows, partition, fname):
    import duckdb
    con = duckdb.connect(":memory:")
    selects = []
    for r in rows:
        cells = ", ".join(
            ["'" + str(v).replace("'", "''") + "' AS \"" + c + "\"" for c, v in zip(cols, r)]
            + [f"'{partition}' AS __partition", "'" + fname + "' AS __file"])
        selects.append("SELECT " + cells)
    con.execute("COPY (" + " UNION ALL ".join(selects) + ") TO '" + path + "' (FORMAT PARQUET)")
    con.close()


with TestClient(app) as c:
    from app.database import SessionLocal
    from app.models import Dashboard, User

    login(c, "admin1")
    seeded = seed_explore.seed(c)
    dash_id = seeded["dashboard"]
    gid = seeded["generator"]

    snap = c.get("/api/explore/catalog").json()
    by_name = {t["name"]: t for t in snap["tables"]}
    sem_entry = next(e for e in snap["semantics"] if e["generator_id"] == gid)

    # (sem table, header sources, fixture rows)
    # Parquet columns carry the catalogue keys (casefolded headers) of the
    # seed's own source columns, so the binding resolves whatever language
    # the seed fixture is written in.
    plan = [
        ("orders", ORDER_KEYS, ORDERS),
        ("realisations", REAL_KEYS, REALS),
    ]
    files, key_n = [], 0
    EXEC = f"/api/dashboards/{dash_id}/lake/exec"
    for sem_table, cols, data in plan:
        m = sem_entry["bindings"][sem_table]["matches"][0]
        ct = by_name[m["table"]]
        for fname, rows in data.items():
            part = next(p for p in ct["partitions"] if p["file"] == fname)
            key_n += 1
            key = f"{key_n:016x}"
            path = os.path.join(TMP, key + ".parquet")
            build_part(path, cols, rows, part["key"], fname)
            r = c.post(f"/api/dashboards/{dash_id}/lake/parts",
                       files={"file": (key + ".parquet", open(path, "rb").read(),
                                       "application/octet-stream")},
                       data={"key": key})
            assert r.status_code in (200, 201), (r.status_code, r.text[:300])
            files.append({"name": fname, "size": 5000, "mtime_ms": part["mtime_ms"] or 1785000000000,
                          "sheet": part.get("sheet"), "header_row": 2, "key": key,
                          "table": m["table"], "partition": part["key"],
                          "bytes": os.path.getsize(path)})

    r = c.put(f"/api/dashboards/{dash_id}/lake/manifest",
              json={"run_id": 1, "parser_version": "4", "files": files})
    check("manifest published", r.status_code == 200, f"{r.status_code} {r.text[:300]}")

    # ---- status: tables + semantics ready ------------------------------
    r = c.post(EXEC, json={"op": "status"})
    check("exec status -> 200", r.status_code == 200, f"{r.status_code} {r.text[:400]}")
    st = r.json() if r.status_code == 200 else {}
    check("status: both catalog tables indexed with 2 partitions each",
          sorted((t["name"], len(t["partitions"])) for t in st.get("tables", []))
          == sorted((t, 2) for t in {f["table"] for f in files}), st.get("tables"))
    gens = (st.get("semantics") or {}).get("generators") or []
    ready = {k["key"]: k.get("ready") for g in gens for k in g.get("kpis", [])}
    check("status: both KPIs ready", ready.get("realised_share") is True and ready.get("order_amount") is True, ready)
    check("status: manifest echoed", (st.get("manifest") or {}).get("run_id") == 1)
    check("status: no index errors", st.get("index_errors") == [], st.get("index_errors"))

    # ---- intent -> SQL -> rows -----------------------------------------
    r = c.post(EXEC, json={"op": "intent",
                           "intent": {"generator_id": gid, "kpi": "order_amount", "dims": []}})
    check("intent: SQL built, no error", r.status_code == 200 and r.json().get("error") is None,
          r.text[:300])
    sql = r.json().get("sql") if r.status_code == 200 else None
    r = c.post(EXEC, json={"op": "sql", "sql": sql, "limit": 100})
    out = r.json() if r.status_code == 200 else {}
    total = out.get("rows") and out["rows"][0][-1]
    check("sum KPI over both partitions = 800.5", total == 800.5, out)
    check("result carries provenance (partitions_used)",
          isinstance(out.get("partitions_used"), list) and len(out["partitions_used"]) >= 2,
          out.get("partitions_used"))

    r = c.post(EXEC, json={"op": "intent",
                           "intent": {"generator_id": gid, "kpi": "realised_share", "dims": []}})
    sql2 = r.json().get("sql")
    r = c.post(EXEC, json={"op": "sql", "sql": sql2, "limit": 100})
    out2 = r.json() if r.status_code == 200 else {}
    row = (out2.get("rows") or [[None]])[0]
    check("ratio KPI: 3 of 5 orders realised", row and abs((row[-1] or 0) - 0.6) < 1e-9, out2)

    # ---- guard: hostile SQL is an engine error, not rows or a 500 ------
    r = c.post(EXEC, json={"op": "sql", "sql": "SELECT * FROM read_parquet('/etc/passwd')"})
    check("guard: read_parquet refused as engine error",
          r.status_code == 200 and r.json().get("error") and not r.json().get("rows"),
          r.text[:200])

    # ---- export --------------------------------------------------------
    r = c.post(EXEC, json={"op": "export", "sql": sql, "name": "ans.xlsx"})
    check("export: xlsx bytes", r.status_code == 200 and r.content[:2] == b"PK"
          and "spreadsheetml" in r.headers.get("content-type", ""), r.status_code)

    # ---- access --------------------------------------------------------
    login(c, "admin1")
    r = c.post("/api/admin/users", json={"person_id": "visitor9", "role": "visitor"})
    assert r.status_code == 200
    db = SessionLocal()
    db.query(Dashboard).filter(Dashboard.id == dash_id).update({"is_public": False})
    db.commit(); db.close()
    login(c, "visitor9")
    r = c.post(EXEC, json={"op": "status"})
    check("access: visitor without reach -> 403/404", r.status_code in (403, 404), r.status_code)
    db = SessionLocal()
    db.query(Dashboard).filter(Dashboard.id == dash_id).update({"is_public": True})
    db.commit(); db.close()
    r = c.post(EXEC, json={"op": "status"})
    check("access: public dashboard -> visitor may ask", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    # ---- parser_version mismatch ---------------------------------------
    login(c, "admin1")
    r = c.put(f"/api/dashboards/{dash_id}/lake/manifest",
              json={"run_id": 2, "parser_version": "999", "files": files})
    assert r.status_code == 200, r.text[:200]
    r = c.post(EXEC, json={"op": "status"})
    check("parser mismatch -> 409 {code: parser}",
          r.status_code == 409 and (r.json().get("detail") or {}).get("code") == "parser",
          f"{r.status_code} {r.text[:200]}")

    # ---- republish rebuilds the instance -------------------------------
    # A new part (different rows, new key) replaces orders 08.26: the total
    # must move — proof the old instance's frames did not survive.
    newkey = "aa" * 8
    path = os.path.join(TMP, newkey + ".parquet")
    build_part(path, ORDER_KEYS,
               [("O4", "05.08.2026", "ClientA", "S1", "100"),
                ("O5", "06.08.2026", "ClientB", "S2", "150")],
               files[1]["partition"], files[1]["name"])
    r = c.post(f"/api/dashboards/{dash_id}/lake/parts",
               files={"file": (newkey + ".parquet", open(path, "rb").read(),
                               "application/octet-stream")},
               data={"key": newkey})
    assert r.status_code in (200, 201), r.text[:200]
    newf = dict(files[1], key=newkey, bytes=os.path.getsize(path))
    r = c.put(f"/api/dashboards/{dash_id}/lake/manifest",
              json={"run_id": 3, "parser_version": "4",
                    "files": [files[0], newf, files[2], files[3]]})
    assert r.status_code == 200, r.text[:200]
    r = c.post(EXEC, json={"op": "sql", "sql": sql, "limit": 100})
    out3 = r.json() if r.status_code == 200 else {}
    t3 = out3.get("rows") and out3["rows"][0][-1]
    check("republish: instance rebuilt, new partition's numbers answer",
          r.status_code == 200 and t3 == 850.5, out3)

    # ---- OP_LAKE_EXEC_TIMEOUT_S: a stuck query is interrupted, not left
    # holding the instance lock forever, and the instance answers again
    # right after (interrupt(), not close()) -------------------------------
    from app.config import settings as _settings
    old_timeout = _settings.lake_exec_timeout_s
    _settings.lake_exec_timeout_s = 1
    slow_sql = ("WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL "
                "SELECT n+1 FROM t WHERE n < 2000000000) SELECT count(*) FROM t")

    # op "sql": the engine's OWN _execute() catches the interrupt and answers
    # with a clear message in the result body — 200, not a raised error —
    # exactly like the browser's existing 30 s deadline, just at OUR ceiling.
    t0 = time.monotonic()
    r = c.post(EXEC, json={"op": "sql", "sql": slow_sql})
    elapsed = time.monotonic() - t0
    body = r.json() if r.status_code == 200 else {}
    check("timeout: slow op sql -> 200 with a clear timeout message, no rows",
          r.status_code == 200 and ERR_TIMEOUT in (body.get("error") or "")
          and not body.get("rows"), f"{r.status_code} {r.text[:300]}")
    check("timeout: fired near OP_LAKE_EXEC_TIMEOUT_S, not the engine's own 30s default",
          elapsed < 15, elapsed)
    check("timeout: audit carries no SQL text",
          "RECURSIVE" not in r.text, r.text[:300])

    # op "export": export_xlsx does NOT catch its own interrupt (no try/except
    # around _execute) — this is exactly the gap lake_exec.py's own watchdog
    # closes, turning what would 500 into a clean 409-family 504.
    r = c.post(EXEC, json={"op": "export", "sql": slow_sql, "name": "slow.xlsx"})
    check("timeout: slow op export -> 504 {code: timeout}",
          r.status_code == 504 and (r.json().get("detail") or {}).get("code") == "timeout",
          f"{r.status_code} {r.text[:200]}")
    check("timeout: export's audit carries no SQL text",
          "RECURSIVE" not in r.text, r.text[:300])

    r = c.post(EXEC, json={"op": "sql", "sql": sql, "limit": 100})
    out4 = r.json() if r.status_code == 200 else {}
    t4 = out4.get("rows") and out4["rows"][0][-1]
    check("timeout: instance answers again right after (interrupt kept it alive or rebuilt it)",
          r.status_code == 200 and t4 == 850.5, out4)
    _settings.lake_exec_timeout_s = old_timeout

    # ---- no manifest / disabled ----------------------------------------
    login(c, "admin1")
    db = SessionLocal()
    owner = db.query(User).filter(User.person_id == "admin1").first()
    d2 = Dashboard(id="nolake1", slug="nolake1", title="No lake", dash_type="static",
                   owner_id=owner.id, is_public=True)
    db.add(d2); db.commit(); db.close()
    r = c.post("/api/dashboards/nolake1/lake/exec", json={"op": "status"})
    check("no manifest -> 404", r.status_code == 404, r.status_code)

    from app.config import settings as _settings
    _settings.lake_exec = False
    r = c.post(EXEC, json={"op": "status"})
    check("OP_LAKE_EXEC=false -> 404", r.status_code == 404, r.status_code)
    _settings.lake_exec = True

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES"); sys.exit(1)
print("ALL PASS")
