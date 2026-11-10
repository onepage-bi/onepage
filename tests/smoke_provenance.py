"""GET /d/{id}/provenance + GET /api/dashboards/{id}/provenance — "where did
these numbers come from" for one report version.

Pins:
  * access = require_dashboard_access: an outsider gets 403 on both routes;
  * a plain (non-generator) dashboard's provenance has an empty run/generator
    section but a real version number/date;
  * a generator-backed dashboard's provenance carries the run, the frozen
    generator version (name, checksum_sha256, created_at, author), its
    deps-lock (name+version), semantics (sha256 + kpi count), the reference
    tables the run resolved, the RLS policy (enabled/slot_count/policy_hash),
    the lake manifest (run_id/created_at/partitions) and the source
    observations (folder key_name + fingerprint + file_count, never a
    filename);
  * `?v=` re-renders an OLDER version's provenance, and an unknown version
    number is a 404;
  * scoping WITHIN a dashboard a viewer can already open: the run /
    generator-version / deps / semantics / source-observations / lake-
    manifest blocks require `can_update_dashboard_data` — same rule as
    /generator/runs, /generator/manifest and /data-sources — so a viewer
    with plain read access only gets the version record and the resolved
    reference tables; the lake-manifest block additionally requires RLS
    full access (ADR-18 `see_all`), so a `can_update_data` grantee who is
    merely a scoped RLS viewer still doesn't get it.

Synthetic data only — no real folder/file names, no people.
"""
import io
import json
import os
import sys
import tempfile
import zipfile

TMP = tempfile.mkdtemp(prefix="op-smoke-provenance-")
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
from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Dashboard, DashboardPermission, GeneratorVersion, ReferenceTable, User,
)
from app.services import storage  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  <-- {extra}"))
    if not cond:
        FAILURES.append(label)


def login(c, ov):
    r = c.post("/dev-login", data={"person_id": ov}, follow_redirects=False)
    assert r.status_code == 302, f"{ov}: {r.status_code} {r.text[:200]}"


def create_user(c, ov, role="visitor"):
    r = c.post("/api/admin/users", json={"person_id": ov, "role": role, "display_name": ov})
    assert r.status_code == 200, f"{ov}: {r.status_code} {r.text[:200]}"


SLUG = "prov_map"
COLUMNS = [{"name": "Code", "type": "text"}, {"name": "Name", "type": "text"}]
PLAT = {"match": f"{SLUG}.xlsx", "role": "reference", "source": "platform", "table": SLUG,
        "sheets": [{"name": "data", "columns": COLUMNS}]}
DATA = {"match": "SL_*.xlsx", "role": "data",
        "sheets": [{"name": "Orders", "columns": [{"name": "Code", "type": "text"}]}]}
MANIFEST = {
    "name": "prov-gen", "version": "1.0",
    "entrypoint": {"module": "provtest.main"},
    "outputs": {"html": True},
    "inputs": [DATA, PLAT],
    "deps": [{"name": "pandas", "version": "2.2.0"}],
}
SEMANTICS = {
    "status": "ok",
    "kpis": {
        "revenue": {"cell_sql": "select 1 as value"},
        "orders": {"cell_sql": "select 1 as value"},
    },
}


def make_bundle(manifest):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("provtest/__init__.py", "")
        z.writestr("provtest/main.py", "def run(file_iter, out_dir, progress):\n    pass\n")
    return buf.getvalue()


def start_run(c, dash_id, reference=None):
    im = {
        "dirs": ["SL_Orders"],
        "files": [
            {"name": "SL_2026_09.xlsx", "dir": 0, "size": 1024, "mtime_ms": 1_700_000_000_000},
        ],
        "skipped": [], "total_bytes": 1024, "count": 1,
    }
    if reference is not None:
        im["reference"] = reference
    return c.post(f"/api/dashboards/{dash_id}/generator/runs",
                  json={"engine": "browser", "input_manifest": im})


def publish(c, dash_id, run_id):
    html = b"<html><body>ready</body></html>"
    return c.post(f"/api/dashboards/{dash_id}/generator/runs/{run_id}/publish",
                  files={"html_file": ("dashboard.html", html, "text/html")})


with TestClient(app) as c:
    login(c, "admin1")

    # ── A. a plain (non-generator) dashboard ─────────────────────────────
    r = c.post("/api/dashboards", data={"title": "Plain report", "description": ""},
               files={"html_file": ("d.html", b"<html><body>hi</body></html>", "text/html")},
               follow_redirects=False)
    plain_loc = r.headers.get("location") or ""
    plain_id = plain_loc.rstrip("/").rsplit("/", 1)[-1]
    check("plain dashboard: create -> 303", r.status_code == 303, f"{r.status_code} {r.headers}")

    r = c.get(f"/api/dashboards/{plain_id}/provenance")
    check("plain: JSON 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    pj = r.json() if r.status_code == 200 else {}
    check("plain: version.number == 1, is_current", pj.get("version", {}).get("number") == 1
          and pj.get("version", {}).get("is_current") is True, str(pj.get("version")))
    check("plain: no run", pj.get("run") is None, str(pj.get("run")))
    check("plain: no generator_version", pj.get("generator_version") is None)
    check("plain: deps empty", pj.get("deps") == [])
    check("plain: semantics None", pj.get("semantics") is None)
    check("plain: reference_uses empty", pj.get("reference_uses") == [])
    check("plain: rls disabled", pj.get("rls") == {"enabled": False, "slot_count": None, "policy_hash": None},
          str(pj.get("rls")))
    check("plain: no lake manifest", pj.get("lake_manifest") is None)
    check("plain: no source observations", pj.get("source_observations") == [])

    r = c.get(f"/d/{plain_id}/provenance")
    check("plain: page 200", r.status_code == 200, f"{r.status_code}")
    check("plain: page mentions «not associated with a generator run»", "not associated with a generator run" in r.text)

    r = c.get(f"/api/dashboards/{plain_id}/provenance?v=99")
    check("plain: unknown version -> 404", r.status_code == 404, f"{r.status_code} {r.text[:200]}")

    r = c.get(f"/d/{plain_id}/provenance?v=99")
    check("plain: unknown version, page -> 404", r.status_code == 404, f"{r.status_code}")

    # ── B. access control: an outsider gets 403 on both routes ──────────
    create_user(c, "outsider1", role="visitor")
    login(c, "outsider1")
    r = c.get(f"/api/dashboards/{plain_id}/provenance")
    check("outsider: JSON -> 403", r.status_code == 403, f"{r.status_code} {r.text[:200]}")
    r = c.get(f"/d/{plain_id}/provenance")
    check("outsider: page -> 403", r.status_code == 403, f"{r.status_code}")
    login(c, "admin1")

    # ── C. a generator-backed dashboard, full provenance ─────────────────
    r = c.post("/api/admin/generators",
               files={"bundle": ("bundle.zip", make_bundle(MANIFEST), "application/zip")})
    check("register generator -> 201", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    gid = r.json().get("id")

    r = c.post("/api/dashboards/generated", json={"title": "Generated report", "generator_id": gid})
    check("create generated dashboard -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    gen_dash_id = r.json().get("id")

    # Bolt on a semantics_json freeze directly (registering a fully valid
    # semantics.json through the bundle is out of scope for this test — the
    # reader in app/services/provenance.py doesn't care how the column got
    # populated, only that it did).
    _db = SessionLocal()
    admin = _db.query(User).filter_by(person_id="admin1").first()
    gv = _db.query(GeneratorVersion).filter_by(generator_id=gid, is_current=True).one()
    gv.semantics_json = json.dumps(SEMANTICS, ensure_ascii=False)
    expected_gv_sha = gv.created_at  # placeholder, real sha computed below
    _db.commit()
    gv_created_at, gv_checksum = gv.created_at, gv.checksum_sha256
    _db.close()

    import hashlib as _hashlib
    expected_sem_sha = _hashlib.sha256(
        json.dumps(SEMANTICS, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    # Table auto-created (empty) from the manifest's platform input — run
    # without a dictionary first ("no dictionary" is a legitimate, recorded
    # outcome, mirrors smoke_reference_runs.py).
    r = start_run(c, gen_dash_id, [{"slug": SLUG, "version": None, "applied": False}])
    check("run: applied false -> 201", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    run_id = r.json().get("run_id")

    r = publish(c, gen_dash_id, run_id)
    check("publish -> ok", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    # RLS policy + lake manifest bolted on directly — same rationale as the
    # semantics_json freeze above: this test pins provenance's READING of
    # rls_json / the lake manifest file, not their own write paths (those are
    # smoke_rls.py's and app/api/lake.py's job).
    _db = SessionLocal()
    dash = _db.query(Dashboard).filter_by(id=gen_dash_id).one()
    dash.rls_json = json.dumps({"slots": [
        {"index": 0, "column": "territory"},
        {"index": 1, "column": "territory"},
    ]}, ensure_ascii=False)
    _db.commit()
    _db.close()
    from app.services import rls as rls_svc
    _db = SessionLocal()
    dash = _db.query(Dashboard).filter_by(id=gen_dash_id).one()
    expected_policy_hash = rls_svc.policy_hash(dash)
    _db.close()

    lake_manifest = {
        "run_id": run_id, "parser_version": "1", "by": "admin1",
        "created_at": "2026-09-01T00:00:00",
        "files": [
            {"key": "a" * 16, "size": 10, "mtime_ms": 1, "table": "t", "bytes": 10},
            {"key": "b" * 16, "size": 10, "mtime_ms": 1, "table": "t", "bytes": 10},
        ],
    }
    storage.save_lake_manifest(gen_dash_id, json.dumps(lake_manifest, ensure_ascii=False).encode("utf-8"))

    r = c.get(f"/api/dashboards/{gen_dash_id}/provenance")
    check("generator report: JSON 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    d = r.json() if r.status_code == 200 else {}

    check("gen: version.number == 2 (placeholder v1 + one publish)",
          d.get("version", {}).get("number") == 2, str(d.get("version")))

    run_j = d.get("run") or {}
    check("gen: run.id matches, engine browser, started_by admin1",
          run_j.get("id") == run_id and run_j.get("engine") == "browser"
          and run_j.get("started_by") == "admin1" and run_j.get("status") == "success",
          str(run_j))

    gv_j = d.get("generator_version") or {}
    check("gen: generator_version name/version/checksum/author",
          gv_j.get("name") == "prov-gen" and gv_j.get("version_number") == 1
          and gv_j.get("checksum_sha256") == gv_checksum and gv_j.get("created_by") == "admin1",
          str(gv_j))

    check("gen: deps carries name+version (pandas frozen bundled in the lock)",
          d.get("deps") == [{"name": "pandas", "version": "2.2.0"}], str(d.get("deps")))

    sem_j = d.get("semantics") or {}
    check("gen: semantics sha256 matches + kpi_count == 2",
          sem_j.get("sha256") == expected_sem_sha and sem_j.get("kpi_count") == 2, str(sem_j))

    refs = d.get("reference_uses") or []
    check("gen: reference_uses has the declared table, unapplied",
          len(refs) == 1 and refs[0]["slug"] == SLUG and refs[0]["applied"] is False
          and refs[0]["version_number"] is None,
          str(refs))

    check("gen: rls enabled, 2 slots, policy_hash matches",
          d.get("rls") == {"enabled": True, "slot_count": 2, "policy_hash": expected_policy_hash},
          str(d.get("rls")))

    lm_j = d.get("lake_manifest") or {}
    check("gen: lake_manifest run_id/created_at/partitions",
          lm_j.get("run_id") == run_id and lm_j.get("created_at") == "2026-09-01T00:00:00"
          and lm_j.get("partitions") == 2, str(lm_j))

    obs = d.get("source_observations") or []
    check("gen: one source observation, folder name + fingerprint + count, no filenames",
          len(obs) == 1 and obs[0]["source"] == "SL_Orders" and obs[0]["file_count"] == 1
          and obs[0].get("fingerprint") and "SL_2026_09.xlsx" not in json.dumps(obs),
          str(obs))

    r = c.get(f"/d/{gen_dash_id}/provenance")
    check("gen: page 200", r.status_code == 200, f"{r.status_code}")
    check("gen: page shows the deps/semantics/generator sections",
          "prov-gen" in r.text and "pandas" in r.text and expected_sem_sha in r.text
          and SLUG in r.text and expected_policy_hash in r.text)

    # ── C2. a viewer with dashboard access but NOT can_update_data: the
    # run/generator/deps/semantics/source-observations/lake blocks are
    # scoped exactly like /generator/runs, /generator/manifest and
    # /data-sources — a viewer who can only READ the report must not learn
    # who ran its generator, on what deps, or from which source folders.
    # The version record and the resolved reference tables stay visible.
    create_user(c, "reader1", role="visitor")
    _db = SessionLocal()
    reader = _db.query(User).filter_by(person_id="reader1").first()
    owner_admin = _db.query(User).filter_by(person_id="admin1").first()
    _db.add(DashboardPermission(dashboard_id=gen_dash_id, user_id=reader.id,
                                 granted_by=owner_admin.id, can_update_data=False))
    _db.commit()
    _db.close()

    login(c, "reader1")
    r = c.get(f"/api/dashboards/{gen_dash_id}/provenance")
    check("reader (no can_update_data): JSON 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    rd = r.json() if r.status_code == 200 else {}
    check("reader: version still visible", rd.get("version", {}).get("number") == 2, str(rd.get("version")))
    check("reader: reference_uses still visible (not gated)",
          rd.get("reference_uses") == refs, str(rd.get("reference_uses")))
    check("reader: rls.enabled visible but slot_count/policy_hash masked",
          rd.get("rls") == {"enabled": True, "slot_count": None, "policy_hash": None}, str(rd.get("rls")))
    check("reader: run hidden (run_restricted, not «no run»)",
          rd.get("run") is None and rd.get("run_restricted") is True, str(rd))
    check("reader: generator_version/deps/semantics hidden",
          rd.get("generator_version") is None and rd.get("deps") == [] and rd.get("semantics") is None,
          str(rd))
    check("reader: source_observations hidden", rd.get("source_observations") == [], str(rd))
    check("reader: lake_manifest hidden (gated on can_update_data too)",
          rd.get("lake_manifest") is None, str(rd.get("lake_manifest")))

    r = c.get(f"/d/{gen_dash_id}/provenance")
    check("reader: page 200", r.status_code == 200, f"{r.status_code}")
    check("reader: page does not leak generator name/checksum/policy hash",
          "prov-gen" not in r.text and gv_checksum not in r.text and expected_policy_hash not in r.text,
          "leak")
    check("reader: page explains the run is restricted",
          "visible only to those who can update" in r.text, "missing restricted note")
    login(c, "admin1")

    # ── C3. a scoped RLS viewer (can_update_data granted, but no see_all):
    # run/generator/deps/observations/rls-detail ARE visible (can_update_
    # data lets them run the generator too), but the lake index stays
    # closed — ADR-18 / app.api.lake._assert_rls_full_access: the lake is
    # unfiltered rows, only a see_all subject may read it. No `user_scopes`
    # row is created for this user, so `rls.subject(...).see_all` is False
    # by default-deny.
    create_user(c, "scoped_editor1", role="visitor")
    _db = SessionLocal()
    scoped = _db.query(User).filter_by(person_id="scoped_editor1").first()
    owner_admin = _db.query(User).filter_by(person_id="admin1").first()
    _db.add(DashboardPermission(dashboard_id=gen_dash_id, user_id=scoped.id,
                                 granted_by=owner_admin.id, can_update_data=True))
    _db.commit()
    _db.close()

    login(c, "scoped_editor1")
    r = c.get(f"/api/dashboards/{gen_dash_id}/provenance")
    check("scoped editor (can_update_data, no see_all): JSON 200", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")
    sd = r.json() if r.status_code == 200 else {}
    check("scoped editor: run/generator_version/deps/observations visible",
          (sd.get("run") or {}).get("id") == run_id
          and (sd.get("generator_version") or {}).get("name") == "prov-gen"
          and sd.get("deps") == [{"name": "pandas", "version": "2.2.0"}]
          and len(sd.get("source_observations") or []) == 1,
          str(sd))
    check("scoped editor: rls slot_count/policy_hash visible (can_update_data grants it)",
          sd.get("rls") == {"enabled": True, "slot_count": 2, "policy_hash": expected_policy_hash},
          str(sd.get("rls")))
    check("scoped editor: lake_manifest still hidden — no see_all",
          sd.get("lake_manifest") is None, str(sd.get("lake_manifest")))
    login(c, "admin1")

    # ── D. ?v= re-renders an OLDER version (v1 = the placeholder, no run) ─
    r = c.get(f"/api/dashboards/{gen_dash_id}/provenance?v=1")
    check("gen: ?v=1 -> 200, no run (placeholder version)",
          r.status_code == 200 and r.json().get("run") is None, f"{r.status_code} {r.text[:200]}")
    check("gen: ?v=1 version.number == 1, not current",
          r.json().get("version", {}).get("number") == 1
          and r.json().get("version", {}).get("is_current") is False)

    # ── E. the viewer links to the provenance page, zero extra requests ──
    r = c.get(f"/d/{gen_dash_id}")
    check("viewer: renders", r.status_code == 200, f"{r.status_code}")
    check("viewer: links to the provenance page",
          f"/d/{gen_dash_id}/provenance" in r.text)

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURES: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
