"""Repro/regression for the reviewer's finding on C3: a warm run leaves
schema_json NULL on unchanged files (gen-engine's `last_known` skip); the
viewer's #gen-inputs dock must resolve that NULL back through the previous
observation of the SAME source before diffing, not treat NULL as "no schema
seen" (which manufactures a false 'missing cols' warning on the common warm
path).

Two runs against the same data source: run 1 (cold) records a full
schema_json for Stock_2026.xlsx; run 2 (warm) records the same file with
schema_json=NULL. GET /d/{id} must show the dock without a 'missing cols'
badge/chip and without a 'sheet not observed' note for that file.
"""
import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="op-lineage-backfill-")
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

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def make_bundle():
    m = {
        "name": "backfill-gen",
        "version": "1.0",
        "entrypoint": {"module": "gen.main"},
        "outputs": {"html": True, "slots": []},
        "engine": "browser",
        "inputs": [
            {"match": "Stock_*.xlsx", "role": "data",
             "sheets": [{"name": "Data", "columns": ["SKU", "Qty"]}]},
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("gen/__init__.py", "")
        z.writestr("gen/main.py", "def begin(*a, **k):\n    pass\n")
    return buf.getvalue()


def main():
    with TestClient(app) as c:
        body(c)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")


def body(c):
    login(c, "admin1")
    r = c.post("/api/admin/generators", files={"bundle": ("b.zip", make_bundle(), "application/zip")})
    assert r.status_code == 201, r.text[:300]
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "Backfill Dash", "generator_id": gid})
    assert r.status_code == 201, r.text[:300]
    dash_id = r.json()["id"]

    from app.database import SessionLocal
    from app.models import DataSource, GenerationRun, GeneratorVersion, SourceFile, SourceObservation

    db = SessionLocal()
    try:
        ver = (db.query(GeneratorVersion)
               .filter(GeneratorVersion.generator_id == gid, GeneratorVersion.is_current == True)  # noqa: E712
               .first())
        ds = DataSource(kind="folder", key_name="Stock")
        db.add(ds)
        db.flush()

        now = datetime.utcnow()
        schema = {"sheets": [{"name": "Data", "header": ["SKU", "Qty"]}]}

        # Run 1: cold read, full schema.
        run1 = GenerationRun(dashboard_id=dash_id, generator_version_id=ver.id, engine="browser",
                             status="success", started_at=now - timedelta(hours=2),
                             finished_at=now - timedelta(hours=2))
        db.add(run1)
        db.flush()
        obs1 = SourceObservation(run_id=run1.id, data_source_id=ds.id, dir_index=0,
                                 file_count=1, total_bytes=100, fingerprint="fp1",
                                 created_at=now - timedelta(hours=2))
        db.add(obs1)
        db.flush()
        db.add(SourceFile(observation_id=obs1.id, name="Stock_2026.xlsx", size=100, mtime_ms=1,
                           kind="parsed", matched_input="Stock_*.xlsx",
                           schema_json=json.dumps(schema)))
        db.commit()

        # Run 2: warm read, same file unchanged -> schema_json NULL.
        run2 = GenerationRun(dashboard_id=dash_id, generator_version_id=ver.id, engine="browser",
                             status="success", started_at=now, finished_at=now)
        db.add(run2)
        db.flush()
        obs2 = SourceObservation(run_id=run2.id, data_source_id=ds.id, dir_index=0,
                                 file_count=1, total_bytes=100, fingerprint="fp1",
                                 created_at=now)
        db.add(obs2)
        db.flush()
        db.add(SourceFile(observation_id=obs2.id, name="Stock_2026.xlsx", size=100, mtime_ms=1,
                           kind="parsed", matched_input="Stock_*.xlsx", schema_json=None))
        db.commit()
    finally:
        db.close()

    r = c.get(f"/d/{dash_id}")
    check("viewer page 200", r.status_code == 200, str(r.status_code))
    html = r.text
    check("dock has declared inputs partial", 'class="sd-diff"' in html)
    check("no false 'missing cols' badge after a warm run", "missing cols" not in html, "found badge")
    check("no false 'sheet not observed' note after backfill", "sheet not observed" not in html, "found note")
    check("no stray column chips (sku/qty) from a phantom missing-columns diff",
          'class="sd-chip sd-chip-missing' not in html, "found missing-column chip")
    check("folder name still shown via Reads folders line", "Reads folders" in html and "Stock" in html)


if __name__ == "__main__":
    main()
