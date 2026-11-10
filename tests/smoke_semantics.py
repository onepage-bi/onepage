"""Smoke test for generator semantics (ADR-13): semantics.json at the bundle
root is validated at registration (shape + read-only SQL + dry run over empty
tables), frozen per version, and served to anyone who may run the generator.

    .venv/bin/python tests/smoke_semantics.py
"""
import io
import json
import os
import sys
import tempfile
import zipfile

TMP = tempfile.mkdtemp(prefix="op-smoke-sem-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
})
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.services import semantics  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


SEM = {
    "schema_version": 1,
    "generator": "sem-test",
    "glossary": {"SL": "Service level — share of the ordered quantity delivered"},
    "rules": {"reporting_start": "2026-06-01"},
    "tables": {
        "orders": {
            "description": "Orders",
            "sources": [{"file": "SL_*.xlsx", "sheet": "Orders"}],
            "business_key": "order_no",
            "columns": {
                "order_no": {"source": "Number", "type": "text", "role": "key"},
                "client_code": {"source": "Customer", "occurrence": 0, "type": "text", "role": "attr"},
                "client": {"source": "Customer", "occurrence": 1, "type": "text", "role": "dim"},
                "ship_date": {"source": "Ship date", "type": "datetime", "role": "time"},
                "order_qty": {"source": "Order, pcs", "type": "number", "role": "measure"},
                "fact_qty": {"source": "Actual", "type": "number", "role": "measure"},
            },
        },
        "client_map": {
            "description": "Retail chain aliases",
            "sources": [{"object": "config.CLIENT_MAP"}],
            "columns": {"client": {"type": "text", "role": "key"}, "display": {"type": "text", "role": "attr"}},
            "rows": [{"client": "romashka", "display": "Daisy"}],
        },
    },
    "views": {
        "lines": "SELECT o.*, least(order_qty, fact_qty) AS fact_clipped, coalesce(m.display, o.client) AS client_display "
                 "FROM orders o LEFT JOIN client_map m ON m.client = lower(o.client) "
                 "WHERE ship_date >= DATE '{reporting_start}'"
    },
    "kpis": {
        "sl": {
            "name": "Service level", "definition": "Σ min(actual, order) / Σ order", "aggregation": "ratio",
            "question_forms": ["what is the SL", "service level"], "target": 0.97,
            "cell_sql": "SELECT client_display, CAST(ship_date AS DATE) AS day, SUM(fact_clipped) AS n, SUM(order_qty) AS d FROM lines GROUP BY 1, 2",
        }
    },
}


def make_bundle(name="sem-test", sem=SEM):
    m = {"name": name, "version": "1.0", "entrypoint": {"module": "semtest.main"},
         "outputs": {"html": True, "slots": []}, "engine": "auto"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("semtest/__init__.py", "")
        z.writestr("semtest/main.py", "def run(file_iter, out_dir, progress):\n    pass\n")
        if sem is not None:
            z.writestr("semantics.json", json.dumps(sem, ensure_ascii=False))
    return buf.getvalue()


def post_bundle(c, url, zip_bytes):
    return c.post(url, files={"bundle": ("bundle.zip", zip_bytes, "application/zip")})


# ── pure validator ───────────────────────────────────────────────────────
check("valid semantics: no structural errors", semantics.validate(SEM, "sem-test") == [], semantics.validate(SEM, "sem-test"))
check("valid semantics: dry run clean", semantics.dry_run(SEM) == [], semantics.dry_run(SEM))

bad = json.loads(json.dumps(SEM))
bad["tables"]["orders"]["columns"]["client"].pop("occurrence")
bad["tables"]["orders"]["columns"]["client_code"].pop("occurrence")
errs = semantics.validate(bad, "sem-test")
check("duplicate source header without occurrence is an error", any("occurrence" in e for e in errs), errs)

bad = json.loads(json.dumps(SEM))
bad["kpis"]["sl"]["cell_sql"] = "SELECT client_display, SUM(fact_clippd) AS n, SUM(order_qty) AS d FROM lines GROUP BY 1"
errs = semantics.dry_run(bad)
check("dry run catches a misspelled column", errs and "fact_clippd" in errs[0], errs)

bad = json.loads(json.dumps(SEM))
bad["kpis"]["sl"]["cell_sql"] = "SELECT client_display, COUNT(*) AS cnt FROM lines GROUP BY 1"
errs = semantics.dry_run(bad)
check("dry run requires n/d or value", errs and "must return" in errs[0], errs)

for sql in ("SELECT * FROM read_csv('/etc/passwd')", "INSTALL httpfs; SELECT 1", "ATTACH 'x.db'; SELECT 1",
            "SELECT 1; DROP TABLE orders", "COPY orders TO 'out.csv'"):
    bad = json.loads(json.dumps(SEM))
    bad["kpis"]["sl"]["cell_sql"] = sql
    errs = semantics.validate(bad, "sem-test")
    check(f"rejects {sql[:28]!r}", bool(errs), errs)

for sql in ('SELECT * FROM "read_csv"(\'/etc/passwd\')', "SELECT * FROM read_csv_auto('/etc/passwd')",
            "SELECT * FROM duckdb_settings()", "SELECT 1 -- don't\n; DROP TABLE orders"):
    bad = json.loads(json.dumps(SEM))
    bad["kpis"]["sl"]["cell_sql"] = sql
    check(f"worker guard is THE guard: rejects {sql[:30]!r}", bool(semantics.validate(bad, "sem-test")))

# Placeholders resolve BEFORE the guard: a rule value cannot smuggle a second
# statement past validation (the text that was checked is the text that runs).
bad = json.loads(json.dumps(SEM))
bad["rules"]["reporting_start"] = "2026-06-01'; CREATE TABLE pwned AS SELECT 42; SELECT '"
errs = semantics.validate_full(bad, "sem-test")
check("rule value cannot smuggle a statement (rejected)", bool(errs), errs)
frozen = semantics.parse_bytes(json.dumps(SEM).encode(), "sem-test")
check("frozen SQL carries the resolved rule, no placeholder left",
      "DATE '2026-06-01'" in frozen["views"]["lines"]["sql"] and "{reporting_start}" not in frozen["views"]["lines"]["sql"],
      frozen["views"]["lines"]["sql"])
bad = json.loads(json.dumps(SEM))
bad["views"]["lines"] = bad["views"]["lines"].replace("{reporting_start}", "{nope}")
check("unknown placeholder fails the dry run loudly", bool(semantics.validate_full(bad, "sem-test")))

for pat in ("*.xlsx (usually SL_<week>.xlsx)", "orders for the week", "SL_*"):
    bad = json.loads(json.dumps(SEM))
    bad["tables"]["orders"]["sources"][0]["file"] = pat
    errs = semantics.validate(bad, "sem-test")
    check(f"prose/odd source pattern {pat!r} is rejected", any("sources[0].file" in e for e in errs) == (pat != "SL_*"), errs)
good = json.loads(json.dumps(SEM))
good["tables"]["orders"]["sources"][0]["file"] = "RDC Data.xlsx"
check("a real workbook name with a space is a valid source", semantics.validate(good, "sem-test") == [])

check("name mismatch is an error", any("does not match" in e for e in semantics.validate(SEM, "other")))
check("unknown top-level key is an error", any("unknown field" in e for e in semantics.validate({**SEM, "extra": 1}, "sem-test")))

# ── version (optional top-level, default 1, unsupported -> error) ────────
check("no `version` -> valid (defaults to 1)", semantics.validate(SEM, "sem-test") == [])
check("normalize() fills version=1 when absent", semantics.normalize(SEM).get("version") == 1)
check("explicit version=1 -> valid", semantics.validate({**SEM, "version": 1}, "sem-test") == [])
future_errs = semantics.validate({**SEM, "version": 999}, "sem-test")
check("version above SUPPORTED_VERSION is an error naming both numbers",
      future_errs and "999" in future_errs[0] and str(semantics.SUPPORTED_VERSION) in future_errs[0], future_errs)
check("non-integer version is an error", any("version" in e for e in semantics.validate({**SEM, "version": "1"}, "sem-test")))
check("version=0 is an error", any("version" in e for e in semantics.validate({**SEM, "version": 0}, "sem-test")))

# ── kpis.<key>.owner / approved_at (optional annotation, not computed with) ─
owned_sem = json.loads(json.dumps(SEM))
owned_sem["kpis"]["sl"]["owner"] = "SL report owner"
owned_sem["kpis"]["sl"]["approved_at"] = "2026-06-01"
check("owner + approved_at on a KPI pass validation", semantics.validate(owned_sem, "sem-test") == [],
      semantics.validate(owned_sem, "sem-test"))
frozen_owned = semantics.parse_bytes(json.dumps(owned_sem).encode(), "sem-test")
check("owner/approved_at survive parse_bytes into the frozen KPI",
      frozen_owned["kpis"]["sl"]["owner"] == "SL report owner"
      and frozen_owned["kpis"]["sl"]["approved_at"] == "2026-06-01", frozen_owned["kpis"]["sl"])

bad_owner = json.loads(json.dumps(SEM))
bad_owner["kpis"]["sl"]["owner"] = "x" * 121
errs = semantics.validate(bad_owner, "sem-test")
check("owner over 120 chars is an error", any("owner" in e for e in errs), errs)

bad_owner = json.loads(json.dumps(SEM))
bad_owner["kpis"]["sl"]["owner"] = ""
errs = semantics.validate(bad_owner, "sem-test")
check("empty owner is an error", any("owner" in e for e in errs), errs)

for bad_date in ("2026-13-40", "01.06.2026", "2026-06", 20260601):
    bad = json.loads(json.dumps(SEM))
    bad["kpis"]["sl"]["approved_at"] = bad_date
    errs = semantics.validate(bad, "sem-test")
    check(f"approved_at {bad_date!r} is rejected", any("approved_at" in e for e in errs), errs)

# ── binding to catalogue tables ──────────────────────────────────────────
CATALOG = [
    {"table_id": "t_1", "name": "sl_orders",
     "columns": [{"name": "number"}, {"name": "customer"}, {"name": "customer__2"}, {"name": "ship date"}, {"name": "order, pcs"}],
     "partitions": [{"file": "SL_23.xlsx", "sheet": "Orders", "key": "2026-23"}]},
    {"table_id": "t_2", "name": "other",
     "columns": [{"name": "x"}], "partitions": [{"file": "Other.xlsx", "sheet": "Orders", "key": None}]},
]
b = semantics.bind(SEM, CATALOG)
m = b["orders"]["matches"]
check("bind: orders matches exactly the SL_* table", [x["table"] for x in m] == ["sl_orders"], m)
check("bind: duplicate header occurrence 1 -> '__2' key", m and m[0]["columns"]["client"] == "customer__2", m and m[0]["columns"])
check("bind: occurrence 0 -> bare key", m and m[0]["columns"]["client_code"] == "customer")
check("bind: column missing in catalogue -> None (engine fills NULL)", m and m[0]["columns"]["fact_qty"] is None)
check("bind: inline-row table is flagged, not matched", b["client_map"]["inline"] is True and b["client_map"]["matches"] == [])
check("bind: the match names the partition FILES the pattern admitted", m and m[0]["files"] == ["SL_23.xlsx"], m and m[0].get("files"))
check("source_key maps engine pseudo-source", semantics.source_key("__source_file") == "__file")

# ── registration + delivery ──────────────────────────────────────────────
with TestClient(app) as c:
    r = c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)
    assert r.status_code == 302

    r = post_bundle(c, "/api/admin/generators", make_bundle())
    check("bundle with semantics registers -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    gid = r.json().get("id")

    from app.database import SessionLocal
    from app.models import GeneratorVersion
    _db = SessionLocal()
    ver = _db.query(GeneratorVersion).filter_by(generator_id=gid, version_number=1).first()
    stored = json.loads(ver.semantics_json) if ver and ver.semantics_json else None
    _db.close()
    check("semantics frozen in the version row", stored is not None and "sl" in stored.get("kpis", {}), stored and list(stored))
    check("frozen copy is normalized (views as objects, occurrence explicit)",
          stored and isinstance(stored["views"]["lines"], dict)
          and stored["tables"]["orders"]["columns"]["order_no"].get("occurrence") == 0, stored and stored["views"])

    r = post_bundle(c, "/api/admin/generators", make_bundle(name="no-sem", sem=None))
    check("bundle without semantics still registers", r.status_code == 201, r.status_code)
    _db = SessionLocal()
    ver = _db.query(GeneratorVersion).filter_by(generator_id=r.json().get("id")).first()
    check("…with NULL semantics", ver is not None and ver.semantics_json is None)
    _db.close()

    broken = json.loads(json.dumps(SEM))
    broken["kpis"]["sl"]["cell_sql"] = "SELECT nope AS n, 1 AS d FROM lines"
    r = post_bundle(c, f"/api/admin/generators/{gid}/versions", make_bundle(sem=broken))
    check("broken semantics -> 400 naming the problem", r.status_code == 400 and "nope" in r.text, f"{r.status_code} {r.text[:200]}")

    r = c.post("/api/dashboards/generated", json={"generator_id": gid, "title": "Sem test"})
    check("generated draft created", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")
    did = r.json().get("id") or r.json().get("dashboard_id")
    r = c.get(f"/api/dashboards/{did}/generator/semantics")
    check("GET semantics serves the frozen file", r.status_code == 200 and r.json().get("kpis", {}).get("sl", {}).get("name") == "Service level",
          f"{r.status_code} {r.text[:200]}")

    owned_sem_named = {**owned_sem, "generator": "sem-owner"}
    r = post_bundle(c, "/api/admin/generators", make_bundle(name="sem-owner", sem=owned_sem_named))
    check("bundle with owner/approved_at registers -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    gid2 = r.json().get("id")
    r = c.post("/api/dashboards/generated", json={"generator_id": gid2, "title": "Sem owner test"})
    did2 = r.json().get("id") or r.json().get("dashboard_id")
    r = c.get(f"/api/dashboards/{did2}/generator/semantics")
    sl = r.json().get("kpis", {}).get("sl", {})
    check("GET semantics: owner/approved_at reach the API", r.status_code == 200
          and sl.get("owner") == "SL report owner" and sl.get("approved_at") == "2026-06-01",
          f"{r.status_code} {sl}")

    future_bundle = json.loads(json.dumps(SEM))
    future_bundle["version"] = 999
    r = post_bundle(c, "/api/admin/generators", make_bundle(name="sem-future", sem=future_bundle))
    check("unsupported semantics version -> 400 naming the version",
          r.status_code == 400 and "999" in r.text, f"{r.status_code} {r.text[:200]}")

# ── semantics_gate timing gate (report-layer perf, ADR-13 addendum) ─────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from semantics_gate import kpi_offenders  # noqa: E402

offenders = kpi_offenders({"fast": 10, "slow": 2500}, 2000)
check("kpi_offenders flags only the slow KPI", offenders == ["slow"], offenders)
check("kpi_offenders: nothing over budget -> empty", kpi_offenders({"a": 5, "b": 1999}, 2000) == [])
check("kpi_offenders: exactly-at-budget does not offend", kpi_offenders({"a": 2000}, 2000) == [])

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES: {FAILURES}")
    sys.exit(1)
print("ALL PASS")
