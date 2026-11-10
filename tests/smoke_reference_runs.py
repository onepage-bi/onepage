"""ADR-16 phase 4 — reference tables as a RUN parameter.

What this pins (the engine's half of reference tables lives in
tests/gen_engine_dom.js; this file is the server's):

  * `GET …/generator/manifest` answers `reference[]` for every platform input,
    including a table nobody has created yet (`exists: false`);
  * `POST …/generator/runs` validates `input_manifest.reference` — unknown
    slug, pinning without `allow_pin`, a version that does not exist — and
    writes `run_reference_uses` rows for what it accepts (`applied: false` →
    `version_number` NULL);
  * publish APPENDS the resolved dictionaries to the version note without
    disturbing `generated: run N`;
  * `publish_warnings` renders `run_summary()["unmapped"]` — pinned BOTH
    directly (the function) and end-to-end through publish, because that call
    site sits inside a `try/except (ValueError, TypeError)` where a bug would
    otherwise vanish in silence (plan risk 7).
"""
import io
import json
import os
import sys
import tempfile
import zipfile

TMP = tempfile.mkdtemp(prefix="op-smoke-ref-runs-")
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
from app.services import genmanifest, quality  # noqa: E402
from app.services import reference as refsvc  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


SLUG = "nwd_bv_map"
# The version note is an English DB string (app/api/generators.py; a stored
# note is not translated, see "Not translated" in CLAUDE.md).
NOTE_NO_REF = "no reference table"
NOTE_REF = "reference table"
COLUMNS = [{"name": "BV", "type": "text"},
           {"name": "Code", "type": "text"},
           {"name": "Name", "type": "text"}]
PLAT = {"match": f"{SLUG}.xlsx", "role": "reference", "source": "platform", "table": SLUG,
        "sheets": [{"name": "data", "columns": COLUMNS}]}
DATA = {"match": "SL_*.xlsx", "role": "data",
        "sheets": [{"name": "Orders", "columns": [{"name": "BV", "type": "text"}]}]}
REF_CHECK = {"match": "SL_*.xlsx", "sheet": "Orders", "column": "BV",
             "type": "ref_lookup", "table": SLUG}
MANIFEST = {
    "name": "ref-runs", "version": "1.0",
    "entrypoint": {"module": "reftest.main"},
    "outputs": {"html": True},
    "inputs": [DATA, PLAT],
    "checks": [REF_CHECK],
}


def make_bundle(manifest):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("reftest/__init__.py", "")
        z.writestr("reftest/main.py", "def run(file_iter, out_dir, progress):\n    pass\n")
    return buf.getvalue()


def start_run(c, dash_id, reference=None):
    im = {"files": [], "dirs": ["SL"], "total_bytes": 0, "count": 0, "skipped": []}
    if reference is not None:
        im["reference"] = reference
    return c.post(f"/api/dashboards/{dash_id}/generator/runs",
                  json={"engine": "browser", "input_manifest": im})


def publish(c, dash_id, run_id):
    html = b"<html><body>ready</body></html>"
    return c.post(f"/api/dashboards/{dash_id}/generator/runs/{run_id}/publish",
                  files={"html_file": ("dashboard.html", html, "text/html")})


with TestClient(app) as c:
    from app.database import SessionLocal
    from app.models import DashboardVersion, ReferenceTable, RunReferenceUse, User

    r = c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)
    assert r.status_code == 302, r.status_code

    r = c.post("/api/admin/generators",
               files={"bundle": ("bundle.zip", make_bundle(MANIFEST), "application/zip")})
    check("register generator with a platform input -> 201", r.status_code == 201,
          f"{r.status_code} {r.text[:300]}")
    gid = r.json().get("id")

    r = c.post("/api/dashboards/generated", json={"title": "Ref runs", "generator_id": gid})
    check("create generated dashboard -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    dash_id = r.json().get("id")

    # ── manifest endpoint: the table does not exist yet ──────────────────
    r = c.get(f"/api/dashboards/{dash_id}/generator/manifest")
    ref = r.json().get("reference") if r.status_code == 200 else None
    check("manifest: reference[] listed for the platform input",
          isinstance(ref, list) and len(ref) == 1 and ref[0]["slug"] == SLUG,
          f"{r.status_code} {str(ref)[:300]}")
    # ADR-16 addendum: registering the generator created the table from the
    # manifest — structure only, no rows. `exists` is therefore true and
    # `current_version` 0, which is what the run panel draws as "empty".
    check("manifest: the declared table was auto-created, empty",
          ref and ref[0]["exists"] is True and ref[0]["current_version"] == 0
          and ref[0]["versions"] == [] and ref[0]["allow_pin"] is False
          and ref[0]["title"] == SLUG,
          str(ref))

    # A run may not name a table the manifest does not declare, whether or not
    # that table exists on the platform.
    r = start_run(c, dash_id, [{"slug": "other_map", "version": None, "applied": True}])
    check("run: unknown slug -> 400", r.status_code == 400 and "other_map" in r.text,
          f"{r.status_code} {r.text[:200]}")
    r = start_run(c, dash_id, [{"slug": "BAD SLUG", "applied": True}])
    check("run: malformed slug -> 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    r = start_run(c, dash_id, [{"slug": SLUG, "applied": True}] * 2)
    check("run: the same table twice -> 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    r = start_run(c, dash_id, "nope")
    check("run: reference is not a list -> 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    # A table with no rows yet is not a reason to refuse a run — the run just
    # records "no reference table".
    r = start_run(c, dash_id, [{"slug": SLUG, "version": None, "applied": False}])
    check("run: declared table with no rows yet -> 201",
          r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    run_id = r.json().get("run_id")
    _db = SessionLocal()
    check("run: an unapplied table records one RunReferenceUse",
          _db.query(RunReferenceUse).filter_by(run_id=run_id).count() == 1)
    _db.close()
    r = publish(c, dash_id, run_id)
    check("publish without any applied reference -> ok", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    _db = SessionLocal()
    note = _db.query(DashboardVersion).filter_by(
        dashboard_id=dash_id).order_by(DashboardVersion.version_number.desc()).first().note
    check("note: \"no reference table\" when the dictionary was declined",
          note == f"generated: run {run_id} · {NOTE_NO_REF} {SLUG}", str(note))
    _db.close()

    # ── give the auto-created table its schema owner and two versions ────
    _db = SessionLocal()
    admin = _db.query(User).filter_by(person_id="admin1").first()
    table = _db.query(ReferenceTable).filter_by(slug=SLUG).one()
    check("auto-created table carries the manifest's schema",
          table.key_column == COLUMNS[0]["name"]
          and table.get_columns() == COLUMNS, str(table.get_columns()))
    table.title = "BV NWD→CONTOSO"
    table.responsible_user_id = admin.id
    refsvc.create_version(_db, table, [["A1", "K1", "First"]], admin.id,
                          {"added": 1, "changed": 0, "removed": 0})
    refsvc.create_version(_db, table, [["A1", "K1", "First"], ["A2", "K2", "Second"]],
                          admin.id, {"added": 1, "changed": 0, "removed": 0})
    _db.commit()
    table_id = table.id
    _db.close()

    r = c.get(f"/api/dashboards/{dash_id}/generator/manifest")
    ref = (r.json().get("reference") or [None])[0] if r.status_code == 200 else None
    check("manifest: existing table -> exists, title, current_version, responsible",
          ref and ref["exists"] is True and ref["title"] == "BV NWD→CONTOSO"
          and ref["current_version"] == 2 and ref["responsible"] == "admin1"
          and ref["allow_pin"] is False,
          str(ref))
    check("manifest: versions newest-first with n/created_at/row_count",
          ref and [v["n"] for v in ref["versions"]] == [2, 1]
          and ref["versions"][0]["row_count"] == 2
          and ref["versions"][0]["created_at"],
          str(ref and ref["versions"]))

    # ── pinning is gated by allow_pin ────────────────────────────────────
    r = start_run(c, dash_id, [{"slug": SLUG, "version": 1, "applied": True}])
    check("run: pin while allow_pin is off -> 400",
          r.status_code == 400 and "pinning" in r.text, f"{r.status_code} {r.text[:200]}")

    r = start_run(c, dash_id, [{"slug": SLUG, "version": None, "applied": True}])
    check("run: applied without a pin -> 201", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    run_id = r.json().get("run_id")
    _db = SessionLocal()
    use = _db.query(RunReferenceUse).filter_by(run_id=run_id).one_or_none()
    check("run: RunReferenceUse resolves to current_version, applied True",
          use is not None and use.table_id == table_id and use.version_number == 2
          and use.applied is True,
          str(use and (use.table_id, use.version_number, use.applied)))
    _db.close()

    r = publish(c, dash_id, run_id)
    check("publish with a dictionary -> ok", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    _db = SessionLocal()
    note = _db.query(DashboardVersion).filter_by(
        dashboard_id=dash_id).order_by(DashboardVersion.version_number.desc()).first().note
    check("note: 'generated: run N' kept AND the dictionary appended",
          note == f"generated: run {run_id} · {NOTE_REF} {SLUG} v2", str(note))
    _db.close()

    # ── "no reference table" is a recorded decision ────────────────────
    r = start_run(c, dash_id, [{"slug": SLUG, "version": None, "applied": False}])
    check("run: applied false -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    run_id = r.json().get("run_id")
    _db = SessionLocal()
    use = _db.query(RunReferenceUse).filter_by(run_id=run_id).one_or_none()
    check("run: applied false -> version_number NULL",
          use is not None and use.applied is False and use.version_number is None,
          str(use and (use.applied, use.version_number)))
    _db.close()
    publish(c, dash_id, run_id)
    _db = SessionLocal()
    note = _db.query(DashboardVersion).filter_by(
        dashboard_id=dash_id).order_by(DashboardVersion.version_number.desc()).first().note
    check("note: \"no reference table\" spelled out", note.endswith(f"· {NOTE_NO_REF} {SLUG}"), str(note))
    _db.close()

    # ── allow_pin on: an old version may be chosen deliberately ──────────
    _db = SessionLocal()
    _db.query(ReferenceTable).filter_by(id=table_id).one().allow_pin = True
    _db.commit()
    _db.close()

    r = c.get(f"/api/dashboards/{dash_id}/generator/manifest")
    check("manifest: allow_pin follows the table",
          (r.json().get("reference") or [{}])[0].get("allow_pin") is True, r.text[:200])

    r = start_run(c, dash_id, [{"slug": SLUG, "version": 99, "applied": True}])
    check("run: version that does not exist -> 400",
          r.status_code == 400 and "99" in r.text, f"{r.status_code} {r.text[:200]}")
    r = start_run(c, dash_id, [{"slug": SLUG, "version": 0, "applied": True}])
    check("run: version 0 -> 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    r = start_run(c, dash_id, [{"slug": SLUG, "version": 1, "applied": True}])
    check("run: pin an old version once allow_pin is on -> 201",
          r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    run_id = r.json().get("run_id")
    _db = SessionLocal()
    use = _db.query(RunReferenceUse).filter_by(run_id=run_id).one_or_none()
    check("run: the pinned version is what is recorded",
          use is not None and use.version_number == 1 and use.applied is True,
          str(use and (use.version_number, use.applied)))
    _db.close()

    # ── unmapped -> publish warning (the try/except must not eat it) ─────
    real_run_summary = quality.run_summary

    def fake_run_summary(db, run):
        out = real_run_summary(db, run)
        out["unmapped"] = [{
            "slug": SLUG, "title": "BV NWD→CONTOSO", "values": ["A7", "A8"],
            "more": 3, "total": 5, "responsible": "Alice",
        }]
        return out

    quality.run_summary = fake_run_summary
    try:
        r = publish(c, dash_id, run_id)
    finally:
        quality.run_summary = real_run_summary
    warns = r.json().get("warnings") if r.status_code == 200 else []
    check("publish: the unmapped line reaches the run panel",
          any("Reference table «BV NWD→CONTOSO»: 5 new values (A7, A8, 3 more)"
              " — owner Alice" == w for w in (warns or [])),
          f"{r.status_code} {str(warns)[:400]}")

    # Direct smoke of the function itself — the publish path above is guarded
    # by try/except (ValueError, TypeError), so a raise there would show up as
    # «no warnings» rather than as a failure.
    direct = genmanifest.publish_warnings({}, {}, quality={
        "unmapped": [{"slug": SLUG, "title": "T", "values": ["x"], "more": 0,
                      "total": 1, "responsible": None}]})
    check("publish_warnings: unmapped without a responsible -> «not assigned»",
          direct == ["Reference table «T»: 1 new values (x) — owner not assigned"],
          str(direct))
    check("publish_warnings: no unmapped key -> silent",
          genmanifest.publish_warnings({}, {}, quality={"failed_checks": [], "new_values": []}) == [])
    check("publish_warnings: unmapped None -> silent (defensive)",
          genmanifest.publish_warnings({}, {}, quality={"unmapped": None}) == [])
    many = genmanifest.publish_warnings({}, {}, quality={"unmapped": [
        {"slug": f"t{i}", "title": f"T{i}", "values": [], "more": 0, "total": i,
         "responsible": None} for i in range(7)]})
    check("publish_warnings: at most 5 unmapped lines + a tail",
          len(many) == 6 and many[-1] == "… and 2 more", str(many))

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURES: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
