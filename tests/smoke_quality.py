"""Smoke test for the Data Quality evaluator (app/services/quality.py).

Fixtures are built through the REAL API — a generator registered with
`inputs` + `checks`, runs started with an `input_manifest`, observations
posted with schema/consumed/checks/profiles, `final` closing the run and
seeding the baselines. Nothing is hand-written into quality_results or
value_baselines except the two things a human is supposed to write by hand
(an ack, an accepted baseline), because those are the endpoints of another
task (P2.2) and this test must not depend on them.

Three runs of one generator carry the story: run 1 is the reference (a
baseline is seeded, so a new value is not news yet), run 2 breaks things (a
renamed column, rows 100 -> 40, files 4 -> 1, a new branch name, a required
pattern gone), run 3 changes nothing (the engine posts `cached` files with no
schema and no checks — the carry-forward has to hold the findings steady).

Observations are stamped with explicit `created_at` values after each run:
SQLite's `func.now()` has second resolution and the whole test runs inside
one second, so without stamping the chain order — which every "previous
level" comparison depends on — would be undefined.
"""
import io
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="op-dq-smoke-")
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
from app.services import genmanifest, observations, quality  # noqa: E402

FAILURES = []

DAY_MS = 86400_000
NOW_MS = int(time.time() * 1000)
NOW = datetime.utcnow()

BRANCH_DIR = "Branches_Data"
LEGACY_DIR = "Legacy_Folder"
NOCONS_DIR = "NoHooks_Folder"


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def make_bundle(name, manifest_override=None):
    m = {
        "name": name,
        "version": "1.0",
        "entrypoint": {"module": "gen.main"},
        "outputs": {"html": True, "slots": []},
        "engine": "browser",
    }
    if manifest_override:
        m.update(manifest_override)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("gen/__init__.py", "")
        z.writestr("gen/main.py", "def begin(*a, **k):\n    pass\n")
    return buf.getvalue()


def register(c, gen_name, title, manifest_override=None):
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", make_bundle(gen_name, manifest_override), "application/zip")})
    assert r.status_code == 201, f"register {gen_name}: {r.status_code} {r.text[:400]}"
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": title, "generator_id": gid})
    assert r.status_code == 201, f"create {title}: {r.status_code} {r.text[:300]}"
    return gid, r.json()["id"]


def start_run(c, dash, folder, files, skipped=None):
    manifest = {"dirs": [folder], "files": files}
    if skipped is not None:
        manifest["skipped"] = skipped
    r = c.post(f"/api/dashboards/{dash}/generator/runs",
               json={"engine": "browser", "input_manifest": manifest})
    assert r.status_code == 201, f"run start: {r.status_code} {r.text[:400]}"
    return r.json()["run_id"]


def post_obs(c, dash, run, files, seq=1, final=True):
    r = c.post(f"/api/dashboards/{dash}/generator/runs/{run}/observations",
               json={"seq": seq, "final": final, "files": files})
    assert r.status_code == 200, f"observations: {r.status_code} {r.text[:400]}"
    return r.json()


def close_run(c, dash, run):
    r = c.post(f"/api/dashboards/{dash}/generator/runs/{run}/error",
               json={"message": "smoke: closing run"})
    assert r.status_code == 200, f"close run: {r.status_code} {r.text[:200]}"


def stamp(run_id, when):
    """Pin the run's start and its observations' created_at, so the chain has
    a defined order at second-resolution timestamps."""
    from app.database import SessionLocal
    from app.models import GenerationRun, SourceObservation
    db = SessionLocal()
    db.query(GenerationRun).filter(GenerationRun.id == run_id).update(
        {"started_at": when}, synchronize_session=False)
    db.query(SourceObservation).filter(SourceObservation.run_id == run_id).update(
        {"created_at": when}, synchronize_session=False)
    db.commit()
    db.close()


def evaluate(**kw):
    from app.database import SessionLocal
    from app.models import Dashboard
    db = SessionLocal()
    try:
        dashes = db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all()
        return quality.evaluate(db, dashes, **kw)
    finally:
        db.close()


def badge():
    from app.database import SessionLocal
    from app.models import Dashboard
    db = SessionLocal()
    try:
        dashes = db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all()
        return quality.badge_count(db, dashes)
    finally:
        db.close()


def pick(res, **kw):
    out = []
    for f in res["findings"]:
        if all(f.get(k) == v for k, v in kw.items()):
            out.append(f)
    return out


def one(res, **kw):
    hits = pick(res, **kw)
    return hits[0] if len(hits) == 1 else None


def sheet_schema(header, rows, header_row=0, name="Sheet1"):
    return {"sheets": [{"name": name, "header": header, "rows": rows,
                        "header_row": header_row}]}


CONSUMED = {"sheets": {"Sheet1": {"columns": ["Branch", "Sum"], "all": False}}}
KV_ID = "known_values:Branches_*.xlsx:Sheet1:Branch"
NN_ID = "not_null:Branches_*.xlsx:Sheet1:Sum"
ASHFORD = "Acme North LLC"
CONTOSO = "Acme North LLC CONTOSO"


def test_prune():
    """quality.prune (P4.3): built straight against the DB (not the API) so
    the fixture can place observations at exact points in the retention
    window. Since `foreign_keys=ON` (2026-09-06, see CLAUDE.md) the runs need
    a real dashboard and a real generator version to hang on, so the fixture
    creates both instead of pointing at made-up ids."""
    from app.database import SessionLocal
    from app.models import (
        Annotation, Dashboard, DataSource, Generator, GeneratorVersion, GenerationRun,
        QualityAck, QualityResult, RunStage, SourceFile, SourceObservation,
    )

    db = SessionLocal()
    try:
        ds = DataSource(kind="folder", key_name="Prune_Test_Source")
        db.add(ds)
        db.add(Dashboard(id="dash-prune-smoke", title="prune smoke"))
        gen = Generator(name="prune-smoke-generator")
        db.add(gen)
        db.flush()
        gver = GeneratorVersion(generator_id=gen.id, version_number=1, source_type="bundle",
                                bundle_path="b", manifest_json="{}", checksum_sha256="x")
        db.add(gver)
        db.flush()
        source_id = ds.id
        gver_id = gver.id

        NOW = datetime(2026, 1, 1)

        def mk_obs(when):
            run = GenerationRun(dashboard_id="dash-prune-smoke", generator_version_id=gver_id,
                                 engine="browser", status="success", started_at=when)
            db.add(run)
            db.flush()
            obs = SourceObservation(run_id=run.id, data_source_id=source_id,
                                     file_count=2, total_bytes=100, created_at=when)
            db.add(obs)
            db.flush()
            return obs

        def mk_file(obs, name, *, schema=True):
            sf = SourceFile(observation_id=obs.id, name=name, size=10, mtime_ms=1,
                             kind="parsed" if schema else "cached",
                             schema_json=json.dumps({"sheets": []}) if schema else None)
            db.add(sf)
            db.flush()
            db.add(QualityResult(source_file_id=sf.id, observation_id=obs.id,
                                  check_id="not_null:*:Sheet1:Col", check_type="not_null",
                                  sheet="Sheet1", column="Col", status="pass", count=1, failed=0))
            db.flush()
            return sf

        # 30 daily observations -> the "newest 30", kept in full by count alone.
        recent_obs = [mk_obs(NOW - timedelta(days=i)) for i in range(30)]

        # 3 more observations, same ISO week, well past the 30-count window but
        # inside the 180-day weekly-thinning window: only the newest of the
        # three should survive in full.
        week_days = [40, 41, 42]
        while len({(NOW - timedelta(days=d)).isocalendar()[:2] for d in week_days}) != 1:
            week_days = [d + 1 for d in week_days]  # nudge until all 3 share an ISO week
        week_obs = [mk_obs(NOW - timedelta(days=d)) for d in week_days]

        # One observation far outside the retention window entirely.
        old_obs = mk_obs(NOW - timedelta(days=300))

        all_obs = recent_obs + week_obs + [old_obs]
        for obs in all_obs:
            mk_file(obs, "rotating.xlsx", schema=True)
        # "always_same.xlsx" is only ever sniffed on the oldest observation;
        # every later one (including the kept newest 30) reports it as
        # unchanged (schema NULL) -- the donor row is the one that matters.
        donor_sf = mk_file(old_obs, "always_same.xlsx", schema=True)
        for obs in recent_obs + week_obs:
            mk_file(obs, "always_same.xlsx", schema=False)

        # Rows prune() must never touch.
        db.add(QualityAck(finding_id="deadbeefcafebabe", data_source_id=source_id,
                           created_by=1))
        db.add(Annotation(data_source_id=source_id, match="*", sheet="Sheet1",
                           column=None, description="note", updated_by=1))
        db.add(RunStage(run_id=recent_obs[0].run_id, order=0, name="stage1"))
        db.commit()

        obs_count_before = (db.query(SourceObservation)
                            .filter(SourceObservation.data_source_id == source_id).count())
        sf_count_before = (db.query(SourceFile)
                           .join(SourceObservation, SourceFile.observation_id == SourceObservation.id)
                           .filter(SourceObservation.data_source_id == source_id).count())
        acks_before = db.query(QualityAck).count()
        annos_before = db.query(Annotation).count()
        stages_before = db.query(RunStage).count()
        rotating_old_id = (db.query(SourceFile.id)
                           .filter(SourceFile.observation_id == old_obs.id,
                                   SourceFile.name == "rotating.xlsx").scalar())

        stats = quality.prune(db, keep_per_source=30, keep_weekly_days=180, now=NOW)
        check("prune: touched the fixture source", stats["sources"] >= 1, str(stats))
        check("prune: some observations counted as pruned",
              stats["observations_pruned"] == 3, str(stats))
        check("prune: some file rows deleted", stats["files_deleted"] > 0, str(stats))

        obs_count_after = (db.query(SourceObservation)
                           .filter(SourceObservation.data_source_id == source_id).count())
        check("prune: source_observations are NEVER deleted",
              obs_count_after == obs_count_before,
              f"{obs_count_after} vs {obs_count_before}")
        check("prune: quality_acks untouched", db.query(QualityAck).count() == acks_before)
        check("prune: annotations untouched", db.query(Annotation).count() == annos_before)
        check("prune: run_stages untouched", db.query(RunStage).count() == stages_before)

        donor_still_there = db.query(SourceFile).filter(SourceFile.id == donor_sf.id).first()
        check("prune: schema-donor file row survives outside every retention window",
              donor_still_there is not None)

        rotating_old_gone = db.query(SourceFile).filter(SourceFile.id == rotating_old_id).first()
        check("prune: a non-donor file row of a fully-pruned observation is deleted",
              rotating_old_gone is None)

        rotating_left = (db.query(SourceFile)
                         .join(SourceObservation, SourceFile.observation_id == SourceObservation.id)
                         .filter(SourceObservation.data_source_id == source_id,
                                 SourceFile.name == "rotating.xlsx").count())
        check("prune: weekly thinning keeps exactly one extra week-of-40..42 observation "
              "(30 daily + 1 weekly keeper = 31 'rotating.xlsx' rows)",
              rotating_left == 31, str(rotating_left))

        # Idempotent: pruning again changes nothing further.
        stats2 = quality.prune(db, keep_per_source=30, keep_weekly_days=180, now=NOW)
        obs_count_idem = (db.query(SourceObservation)
                          .filter(SourceObservation.data_source_id == source_id).count())
        check("prune: re-running is a no-op on observation count",
              obs_count_idem == obs_count_before)
        check("prune: re-running finds nothing left to delete for this source",
              stats2["files_deleted"] == 0, str(stats2))
    finally:
        db.close()


def test_schema_unreadable(c):
    """schema_unreadable (commit 633eaaa's per-sheet `reason` / top-level
    `error` on schema_json): a data-role sheet whose header genuinely could
    not be found is a warning; a schema the ENGINE chose not to read because
    of its own observation-batch cap is always informational, hint included,
    even on the same data-role pattern — the two must not collapse into one
    severity. Also covers the ack path: acking the warning stops it counting
    as open without removing it from the list."""
    gid_su, dash_su = register(c, "gen-schema-unreadable", "Schema Unreadable Report", {
        "inputs": [{"match": "Bad_*.xlsx", "role": "data"}],
    })
    files = [{"name": "Bad_1.xlsx", "size": 100, "mtime_ms": NOW_MS, "matched_input": "Bad_*.xlsx"},
             {"name": "Bad_2.xlsx", "size": 100, "mtime_ms": NOW_MS, "matched_input": "Bad_*.xlsx"}]
    run = start_run(c, dash_su, "Bad_Folder", files)
    post_obs(c, dash_su, run, [
        {"dir": 0, "name": "Bad_1.xlsx", "kind": "parsed",
         "schema": {"sheets": [{"name": "Sheet1", "header": [], "header_row": None,
                                "rows": 5, "reason": "no header in first 20 rows"}],
                    "error": None}},
        {"dir": 0, "name": "Bad_2.xlsx", "kind": "parsed",
         "schema": {"sheets": [], "error": "observe budget exceeded — schema not read"}},
    ])
    close_run(c, dash_su, run)
    stamp(run, NOW - timedelta(minutes=5))

    res = evaluate()
    warn = one(res, generator="gen-schema-unreadable", kind="schema_unreadable", file="Bad_1.xlsx")
    check("a data-role sheet with 'no header in first 20 rows' is schema_unreadable/warn",
          warn is not None and warn["severity"] == "warn", str(warn))
    check("its message names the file, sheet and reason",
          warn is not None
          and warn["message"] == "Header not found in «Bad_1.xlsx» / Sheet1: no header in first 20 rows",
          str(warn and warn["message"]))

    info = one(res, generator="gen-schema-unreadable", kind="schema_unreadable", file="Bad_2.xlsx")
    check("a capacity (observe-budget) reason is schema_unreadable/info even on a data input",
          info is not None and info["severity"] == "info", str(info))
    check("the capacity finding's sheet is None (workbook-level, no per-sheet detail)",
          info is not None and info["sheet"] is None, str(info))
    check("the capacity finding's message carries the hint",
          info is not None and "increase the limit" in info["message"], str(info and info["message"]))

    open_before = res["stats"]["open"]
    r = c.post("/api/data-quality/acks", json={
        "finding_id": warn["id"], "source_id": warn["source_id"],
        "note": "known bad file",
    })
    check("POST ack on schema_unreadable is 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    ack_id = r.json()["id"]

    res2 = evaluate()
    acked = one(res2, generator="gen-schema-unreadable", kind="schema_unreadable", file="Bad_1.xlsx")
    check("acked schema_unreadable finding stays listed and is marked",
          acked is not None and acked["acked"] is True
          and acked["ack"]["note"] == "known bad file", str(acked and acked.get("ack")))
    check("acked schema_unreadable finding stops counting as open",
          res2["stats"]["open"] == open_before - 1, f"{res2['stats']['open']} vs {open_before - 1}")

    r = c.delete(f"/api/data-quality/acks/{ack_id}")
    check("DELETE ack on schema_unreadable 204", r.status_code == 204, str(r.status_code))


def test_schema_capacity_not_missing_sheet(c):
    """Regression: gen-worker.js now sends `{sheets: [], error: "..."}`
    instead of a NULL schema for a capacity-limited file (observe budget /
    batch cap / probe unavailable) — commit fixed after this test was added.
    Such a schema must be scored as "not observed this run", same as a NULL
    schema, NOT as "sheet observed, empty": the latter used to make every
    declared column of every declared sheet report schema_missing_sheet/warn
    right next to the schema_unreadable/info finding that already explains
    why. A `data`-role, `required` pattern with declared sheets/columns is
    the failing shape from the original report."""
    gid, dash = register(c, "gen-schema-capacity", "Schema Capacity Report", {
        "inputs": [{"match": "sales_*.xlsx", "role": "data", "required": True,
                   "sheets": [{"name": "TDSheet", "columns": ["SKU", "Qty", "Date"]}]}],
    })
    files = [{"name": "sales_08.xlsx", "size": 100, "mtime_ms": NOW_MS, "matched_input": "sales_*.xlsx"}]
    run = start_run(c, dash, "Sales_Folder", files)
    post_obs(c, dash, run, [
        {"dir": 0, "name": "sales_08.xlsx", "kind": "parsed",
         "schema": {"sheets": [], "error": "lineage probe unavailable"}},
    ])
    close_run(c, dash, run)
    stamp(run, NOW - timedelta(minutes=5))

    res = evaluate()
    missing_sheet = one(res, generator="gen-schema-capacity", kind="schema_missing_sheet")
    check("a capacity-limited schema does NOT mint a schema_missing_sheet finding",
          missing_sheet is None, str(missing_sheet))
    missing_col = one(res, generator="gen-schema-capacity", kind="schema_missing_column")
    check("...nor a schema_missing_column finding for any of its declared columns",
          missing_col is None, str(missing_col))
    info = one(res, generator="gen-schema-capacity", kind="schema_unreadable", file="sales_08.xlsx")
    check("the run-level schema_unreadable/info finding is still the only signal",
          info is not None and info["severity"] == "info", str(info))


REF_SLUG = "nwd_bv"
REF_CHECK_ID = "ref_lookup:SL_*.xlsx:Sheet1:BV"
REF_MANIFEST = {
    "inputs": [
        {"match": "SL_*.xlsx", "role": "data", "required": True,
         "sheets": [{"name": "Sheet1", "columns": ["BV", "Sum"]}]},
        {"match": f"{REF_SLUG}.xlsx", "role": "reference", "source": "platform",
         "table": REF_SLUG,
         "sheets": [{"name": "data", "columns": [{"name": "BV", "type": "text"},
                                                 {"name": "Code", "type": "text"}]}]},
    ],
    "checks": [{"match": "SL_*.xlsx", "sheet": "Sheet1", "column": "BV",
                "type": "ref_lookup", "table": REF_SLUG}],
}


def make_reference_table(slug, title):
    """The table as the reference API would leave it: schema frozen, no rows
    yet (current_version 0), plus the data_sources row it is seen through."""
    from app.database import SessionLocal
    from app.models import ReferenceTable
    from app.services import reference
    db = SessionLocal()
    try:
        t = ReferenceTable(slug=slug, title=title, key_column="BV",
                           kind="simple", current_version=0)
        t.set_columns([{"name": "BV", "type": "text"}, {"name": "Code", "type": "text"}])
        db.add(t)
        db.flush()
        reference.ensure_data_source(db, t)
        db.commit()
        return t.id
    finally:
        db.close()


def import_keys(table_id, rows):
    """What an owner's Excel import does: a new version with these rows."""
    from app.database import SessionLocal
    from app.models import ReferenceTable
    from app.services import reference
    db = SessionLocal()
    try:
        t = db.query(ReferenceTable).filter(ReferenceTable.id == table_id).one()
        reference.create_version(db, t, rows, None,
                                 {"added": len(rows), "changed": 0, "removed": 0})
        db.commit()
        return t.current_version
    finally:
        db.close()


def mark_run(run_id, status):
    """Force a run's terminal status. `close_run` posts /error (the cheap way
    to release the run lock), but ADR-16 freshness asks «what was the PUBLISHED
    report built on», and publish is what marks a run success — so a run that
    stands in for a published one has to say so."""
    from app.database import SessionLocal
    from app.models import GenerationRun
    db = SessionLocal()
    try:
        db.query(GenerationRun).filter(GenerationRun.id == run_id).update(
            {"status": status}, synchronize_session=False)
        db.commit()
    finally:
        db.close()


def set_reference_use(run_id, table_id, version_number, applied):
    from app.database import SessionLocal
    from app.models import RunReferenceUse
    db = SessionLocal()
    try:
        row = (db.query(RunReferenceUse)
               .filter(RunReferenceUse.run_id == run_id,
                       RunReferenceUse.table_id == table_id).first())
        if row is None:
            row = RunReferenceUse(run_id=run_id, table_id=table_id)
            db.add(row)
        row.version_number = version_number
        row.applied = applied
        db.commit()
    finally:
        db.close()


def test_reference(c):
    """ADR-16: `unmapped_values` + `rerun_needed`.

    The pin that matters is the one in the middle: importing the missing keys
    closes the finding WITHOUT a new run, because the evaluator re-filters the
    run's `missing` against the table's CURRENT keys at read time.
    """
    table_id = make_reference_table(REF_SLUG, "BV NWD→CONTOSO")
    gid_r, dash_r = register(c, "gen-ref", "SL Reference Report", REF_MANIFEST)

    files = [{"name": "SL_1.xlsx", "size": 900, "mtime_ms": NOW_MS,
              "matched_input": "SL_*.xlsx"}]
    run = start_run(c, dash_r, "SL_Folder", files)
    post_obs(c, dash_r, run, [{
        "dir": 0, "name": "SL_1.xlsx", "kind": "parsed",
        "schema": sheet_schema(["BV", "Sum"], 10),
        "checks": [{"check_id": REF_CHECK_ID, "status": "fail", "count": 10,
                    "failed": 3, "missing": ["1001", "1002", "1003"],
                    "sampled": False}],
    }])
    close_run(c, dash_r, run)
    from app.models import RUN_ERROR, RUN_SUCCESS
    mark_run(run, RUN_SUCCESS)      # this run stands in for a published one
    stamp(run, NOW - timedelta(minutes=20))

    res = evaluate()
    um = one(res, generator="gen-ref", kind="unmapped_values")
    # ADR-16 addendum: a table that exists but has never been imported into
    # (the shape came from the manifest, the rows are still missing) reports
    # ONE "table is empty" info, not "100% misses" — every key misses an
    # empty table, so the count would be noise. The values are still in
    # `detail` for whoever wants the list.
    check("ref_lookup against an EMPTY table is one 'table is empty' info",
          um is not None and um["severity"] == "info"
          and "is empty" in um["message"]
          and um["detail"]["current_version"] == 0
          and um["detail"]["values"] == ["1001", "1002", "1003"]
          and um["detail"]["total"] == 3 and um["detail"]["more"] == 0,
          str(um))
    check("unmapped_values names the table and links to it",
          um is not None and um["detail"]["table"] == REF_SLUG
          and um["detail"]["title"] == "BV NWD→CONTOSO"
          and um["detail"]["reference_url"] == "/reference/nwd_bv"
          and um["detail"]["template_url"].startswith(
              "/api/reference/nwd_bv/template?unmapped=1&keys="),
          str(um and um["detail"]))
    check("a platform input is never reported as a missing pattern "
          "(the server materialises the workbook; it is not in the folder)",
          not pick(res, generator="gen-ref", kind="schema_missing_pattern"),
          str(pick(res, generator="gen-ref", kind="schema_missing_pattern")))
    check("the folder input of the same generator still diffs normally",
          not pick(res, generator="gen-ref", kind="schema_missing_pattern",
                   file="SL_*.xlsx"),
          "SL_*.xlsx was observed, so it must not be unmatched either")
    check("unmapped_values id is keyed by the table slug",
          um is not None and um["id"] == quality.finding_id(
              "unmapped_values", um["source"], "gen-ref", "SL_*.xlsx", "Sheet1",
              "BV", REF_SLUG),
          str(um and um["id"]))

    # publish's own view of the same run
    from app.database import SessionLocal
    from app.models import GenerationRun
    db = SessionLocal()
    run_row = db.query(GenerationRun).filter(GenerationRun.id == run).first()
    summary = quality.run_summary(db, run_row)
    db.close()
    check("run_summary.unmapped carries slug/title/values/total/responsible",
          summary["unmapped"] == [{"slug": REF_SLUG, "title": "BV NWD→CONTOSO",
                                   "exists": True, "current_version": 0,
                                   "values": ["1001", "1002", "1003"], "more": 0,
                                   "total": 3, "responsible": None}],
          str(summary["unmapped"]))
    check("publish note for an EMPTY table says 'upload Excel', not a count",
          any("is empty" in line for line in
              genmanifest.publish_warnings({}, {}, quality={"unmapped": summary["unmapped"]})),
          str(genmanifest.publish_warnings({}, {}, quality={"unmapped": summary["unmapped"]})))

    open_with_unmapped = badge()

    # ── the import closes it, with NO new run ───────────────────────────
    version = import_keys(table_id, [["1001", "A"], ["1002", "B"], ["1003", "C"]])
    check("import created version 1", version == 1, str(version))
    res2 = evaluate()
    check("importing the keys closes the finding without a rerun",
          one(res2, generator="gen-ref", kind="unmapped_values") is None,
          str(pick(res2, generator="gen-ref", kind="unmapped_values")))
    check("badge drops by exactly the closed warn finding",
          badge() == open_with_unmapped - 1, f"{badge()} vs {open_with_unmapped}")

    # ── rerun_needed ────────────────────────────────────────────────────
    set_reference_use(run, table_id, None, False)
    res3 = evaluate()
    rn = one(res3, generator="gen-ref", kind="rerun_needed")
    check("a run taken without the table, once the table has data, is an info",
          rn is not None and rn["severity"] == "info"
          and rn["detail"]["used_version"] is None
          and rn["detail"]["current_version"] == 1
          and rn["detail"]["dashboard_id"] == dash_r,
          str(rn))

    set_reference_use(run, table_id, 1, True)
    check("a run on the CURRENT version raises nothing",
          one(evaluate(), generator="gen-ref", kind="rerun_needed") is None)

    import_keys(table_id, [["1001", "A"], ["1002", "B"], ["1003", "C"], ["1004", "D"]])
    res4 = evaluate()
    rn2 = one(res4, generator="gen-ref", kind="rerun_needed")
    check("a run on an older version is a warn naming both versions",
          rn2 is not None and rn2["severity"] == "warn"
          and rn2["detail"]["used_version"] == 1
          and rn2["detail"]["current_version"] == 2,
          str(rn2))
    check("rerun_needed is counted by the badge",
          any(f["kind"] == "rerun_needed" and not f["acked"]
              for f in res4["findings"]))

    # A run that FAILED published nothing, so it is not the answer to "what is
    # this report built on" — letting it win would let a failed re-run silently
    # close the warning about the version the published report actually shows.
    run_f = start_run(c, dash_r, "SL_Folder", files)
    close_run(c, dash_r, run_f)
    stamp(run_f, NOW - timedelta(minutes=5))
    set_reference_use(run_f, table_id, 2, True)      # newest run, on v2
    rn3 = one(evaluate(), generator="gen-ref", kind="rerun_needed")
    check("a FAILED later run does not close rerun_needed",
          rn3 is not None and rn3["detail"]["used_version"] == 1
          and rn3["detail"]["run_id"] == run,
          str(rn3 and rn3["detail"]))
    mark_run(run_f, RUN_SUCCESS)
    check("the same run, once successful, does close it",
          one(evaluate(), generator="gen-ref", kind="rerun_needed") is None)
    mark_run(run_f, RUN_ERROR)   # leave the rest of the file as it was


    # ── a table nobody created is an info, not silence ──────────────────
    m = json.loads(json.dumps(REF_MANIFEST).replace(REF_SLUG, "no_such_table"))
    gid_n, dash_n = register(c, "gen-ref-missing", "Missing Table Report", m)
    run_n = start_run(c, dash_n, "SL2_Folder",
                      [{"name": "SL_2.xlsx", "size": 900, "mtime_ms": NOW_MS,
                        "matched_input": "SL_*.xlsx"}])
    post_obs(c, dash_n, run_n, [{
        "dir": 0, "name": "SL_2.xlsx", "kind": "parsed",
        "schema": sheet_schema(["BV", "Sum"], 5),
        "checks": [{"check_id": REF_CHECK_ID, "status": "fail", "count": 5,
                    "failed": 1, "missing": ["Z9"], "sampled": False}],
    }])
    close_run(c, dash_n, run_n)
    stamp(run_n, NOW - timedelta(minutes=19))
    res5 = evaluate()
    miss = one(res5, generator="gen-ref-missing", kind="unmapped_values")
    # Registering `gen-ref-missing` CREATED `no_such_table` from its manifest
    # (ADR-16 addendum), so this is now the empty-table case, not the
    # never-created one. `_ref_filter`'s `exists: False` branch still exists —
    # it is what a table deleted by hand leaves behind — and is pinned as a
    # pure unit below.
    check("a ref_lookup against a table auto-created but empty is an info finding",
          miss is not None and miss["severity"] == "info"
          and miss["detail"]["exists"] is True
          and miss["detail"]["current_version"] == 0
          and "is empty" in miss["message"],
          str(miss))
    gone = quality._ref_filter({"deleted_tbl": {"values": ["Z9"], "overflow": False}},
                               {"tables": {}, "keys": {}, "by_id": {}})["deleted_tbl"]
    check("a table that does not exist gets no reference/template link",
          gone["exists"] is False and "reference_url" not in gone
          and "template_url" not in gone, str(gone))

    # ── the template link must stay a URL ───────────────────────────────
    # 500 Cyrillic keys at MAX_KEY_CHARS percent-encode to ~190 KB — past
    # nginx's 8 KB request line, so the link would 414 for exactly the tables
    # that need it most, and it is stored in every finding's detail besides.
    # Cyrillic on purpose: multi-byte keys are what blow up the percent-encoded URL.
    long_keys = [("клиент-%03d-" % i) + "я" * 50 for i in range(500)]
    url, in_url = quality._template_url("nwd_bv", long_keys)
    check("template_url stays inside the request-line budget",
          len(url) <= quality.MAX_TEMPLATE_URL_BYTES + 200 and in_url < 500,
          f"{len(url)} bytes, {in_url} keys")
    check("template_url still carries at least one key",
          in_url >= 1 and "keys=" in url, f"{in_url}")
    short_url, short_n = quality._template_url("nwd_bv", ["A", "B"])
    check("a short list is carried whole and NEWLINE-separated",
          short_n == 2 and "keys=A%0AB" in short_url, short_url)
    filt = quality._ref_filter(
        {"nwd_bv": {"values": long_keys, "overflow": False}},
        {"tables": {"nwd_bv": {"title": "T", "current_version": 1,
                               "responsible": None}},
         "keys": {"nwd_bv": set()}, "by_id": {}})["nwd_bv"]
    check("_ref_filter reports the template as partial and states the true total",
          filt["total"] == 500 and filt["template_partial"] is True
          and filt["template_keys"] == in_url,
          str((filt["total"], filt.get("template_partial"), filt.get("template_keys"))))

    # ── overflow: an incomplete list must say so ────────────────────────
    run_o = start_run(c, dash_r, "SL_Folder", files)
    post_obs(c, dash_r, run_o, [{
        "dir": 0, "name": "SL_1.xlsx", "kind": "parsed",
        "schema": sheet_schema(["BV", "Sum"], 10),
        "checks": [{"check_id": REF_CHECK_ID, "status": "fail", "count": 10,
                    "failed": 900, "missing": ["9001"], "missing_overflow": True,
                    "sampled": False}],
    }])
    close_run(c, dash_r, run_o)
    stamp(run_o, NOW - timedelta(minutes=18))
    res6 = evaluate()
    ovf = one(res6, generator="gen-ref", kind="unmapped_values")
    check("missing_overflow downgrades to an info that says the list is partial",
          ovf is not None and ovf["severity"] == "info"
          and ovf["detail"]["overflow"] is True
          and "incomplete" in ovf["message"], str(ovf))

    # ── the WARN case: a POPULATED table with a value it has no row for ──
    # Last in the file on purpose: it posts the NEWEST observation for this
    # folder, and every check above reads «the latest run».
    run_w = start_run(c, dash_r, "SL_Folder", files)
    post_obs(c, dash_r, run_w, [{
        "dir": 0, "name": "SL_1.xlsx", "kind": "parsed",
        "schema": sheet_schema(["BV", "Sum"], 10),
        "checks": [{"check_id": REF_CHECK_ID, "status": "fail", "count": 10,
                    "failed": 1, "missing": ["9999"], "sampled": False}],
    }])
    close_run(c, dash_r, run_w)
    stamp(run_w, NOW - timedelta(minutes=2))
    umw = one(evaluate(), generator="gen-ref", kind="unmapped_values")
    check("a value missing from a POPULATED table is still a warn",
          umw is not None and umw["severity"] == "warn"
          and umw["detail"]["values"] == ["9999"], str(umw))


def main():
    with TestClient(app) as c:
        body(c)


def body(c):
    login(c, "admin1")
    from app.database import SessionLocal
    from app.models import QualityAck, RunStage, User, ValueBaseline

    # ═══════════════════════════════════════════════════════════════════
    # Fixtures: one generator with inputs + checks, one legacy, one whose
    # runs record no consumption at all.
    # ═══════════════════════════════════════════════════════════════════
    gid_q, dash_q = register(c, "gen-quality", "Branch Quality", {
        "inputs": [
            {"match": "Branches_*.xlsx", "role": "data", "required": True,
             "sheets": [{"name": "Sheet1",
                         "columns": ["Branch", "Sum", "SKU Description"]}]},
            {"match": "Ref_*.xlsx", "role": "reference", "required": True},
        ],
        "checks": [
            {"match": "Branches_*.xlsx", "sheet": "Sheet1", "column": "Branch",
             "type": "known_values", "label": "Branch reference"},
            {"match": "Branches_*.xlsx", "sheet": "Sheet1", "column": "Sum",
             "type": "not_null"},
        ],
    })
    gid_leg, dash_leg = register(c, "gen-legacy", "Legacy Report")
    gid_nc, dash_nc = register(c, "gen-nohooks", "No Hooks Report", {
        "inputs": [{"match": "Plain_*.xlsx", "role": "data",
                    "sheets": [{"name": "Sheet1", "columns": ["Branch"]}]}],
    })

    # ── run 1: the reference ────────────────────────────────────────────
    branch_files = [{"name": f"Branches_{i}.xlsx", "size": 1_000 + i,
                     "mtime_ms": NOW_MS, "matched_input": "Branches_*.xlsx"}
                    for i in (1, 2, 3)]
    ref_file = {"name": "Ref_1.xlsx", "size": 500, "mtime_ms": NOW_MS,
                "matched_input": "Ref_*.xlsx"}
    run1 = start_run(c, dash_q, BRANCH_DIR, branch_files + [ref_file])
    obs_files = []
    for i in (1, 2, 3):
        obs_files.append({
            "dir": 0, "name": f"Branches_{i}.xlsx", "kind": "parsed",
            "schema": sheet_schema(["Branch", "Sum", "SKU Description"], 100),
            "consumed": CONSUMED,
            "checks": [
                {"check_id": KV_ID, "status": "pass", "count": 100, "failed": 0,
                 "distinct": [ASHFORD], "sampled": False},
                {"check_id": NN_ID, "status": "fail" if i == 1 else "pass",
                 "count": 100, "failed": 2 if i == 1 else 0,
                 "examples": ["row 7", "row 9"] if i == 1 else [], "sampled": False},
            ],
            "profiles": [{"sheet": "Sheet1", "column": "Branch", "type": "text",
                          "nulls": 0, "distinct": 1, "top": [[ASHFORD, 100]],
                          "sampled": False}],
        })
    obs_files.append({"dir": 0, "name": "Ref_1.xlsx", "kind": "parsed",
                      "schema": sheet_schema(["Code", "Name"], 10, name="Ref")})
    post_obs(c, dash_q, run1, obs_files)
    close_run(c, dash_q, run1)
    stamp(run1, NOW - timedelta(minutes=30))

    res1 = evaluate()
    q1 = [f for f in res1["findings"] if f["generator"] == "gen-quality"]
    check("run 1: baseline seeded, so no new_values warning",
          not [f for f in q1 if f["kind"] == "new_values" and f["severity"] == "warn"],
          str([f["message"] for f in q1 if f["kind"] == "new_values"]))
    db = SessionLocal()
    base = db.query(ValueBaseline).filter(ValueBaseline.generator_id == gid_q).first()
    check("run 1: baseline row holds the observed distinct set",
          base is not None and base.get_values() == [ASHFORD],
          str(base and base.get_values()))
    db.close()

    fail1 = one(res1, generator="gen-quality", kind="check_fail", file="Branches_1.xlsx")
    check("run 1: check_fail for the failing not_null", fail1 is not None,
          str([f["kind"] for f in q1]))
    if fail1:
        check("run 1: check_fail is a warn with its counts and examples",
              fail1["severity"] == "warn" and fail1["detail"]["failed"] == 2
              and fail1["detail"]["count"] == 100
              and fail1["detail"]["examples"] == ["row 7", "row 9"],
              str(fail1["detail"]))
        check("run 1: check_fail detail carries type/label/sampled",
              fail1["detail"]["type"] == "not_null" and fail1["detail"]["sampled"] is False,
              str(fail1["detail"]))
    check("run 1: a passing check produces nothing",
          not pick(res1, generator="gen-quality", kind="check_fail", file="Branches_2.xlsx"))
    check("run 1: no schema findings — everything declared was found",
          not [f for f in q1 if f["kind"].startswith("schema_")],
          str([f["message"] for f in q1 if f["kind"].startswith("schema_")]))

    dead = one(res1, generator="gen-quality", kind="dead_declaration",
               column="SKU Description")
    check("dead_declaration: declared, present in the file, never consumed",
          dead is not None and dead["severity"] == "info"
          and dead["detail"]["runs_checked"] == 1,
          str(dead and dead["detail"]))
    check("dead_declaration is not raised for a consumed column",
          not pick(res1, generator="gen-quality", kind="dead_declaration", column="Sum"))

    # ── run 2: the breakage ─────────────────────────────────────────────
    run2 = start_run(c, dash_q, BRANCH_DIR, [
        {"name": "Branches_1.xlsx", "size": 2_000, "mtime_ms": NOW_MS,
         "matched_input": "Branches_*.xlsx"}])
    post_obs(c, dash_q, run2, [{
        "dir": 0, "name": "Branches_1.xlsx", "kind": "parsed",
        # "SKU Description" was renamed in the workbook, rows fell 100 -> 40.
        "schema": sheet_schema(["Branch", "Sum", "SKU_Discription"], 40),
        "consumed": CONSUMED,
        "checks": [
            {"check_id": KV_ID, "status": "pass", "count": 40, "failed": 0,
             "distinct": [ASHFORD, CONTOSO], "sampled": False},
            {"check_id": NN_ID, "status": "fail", "count": 40, "failed": 3,
             "examples": ["row 4"], "sampled": False},
        ],
    }])
    close_run(c, dash_q, run2)
    stamp(run2, NOW - timedelta(minutes=20))

    # A generator that calls onepage.stage() after reading its file (P4.1):
    # the sheet's read edge lands on the stage, not straight on the
    # generator — impact analysis must still resolve it back to gen-quality.
    db_stage = SessionLocal()
    db_stage.add(RunStage(run_id=run2, order=0, name="clean",
                          read_files_json=json.dumps(["Branches_1.xlsx"])))
    db_stage.commit()
    db_stage.close()

    res2 = evaluate()
    q2 = [f for f in res2["findings"] if f["generator"] == "gen-quality"]

    nv = one(res2, generator="gen-quality", kind="new_values", column="Branch")
    check("run 2: the new branch name is a new_values warning",
          nv is not None and nv["severity"] == "warn" and nv["detail"]["new"] == [CONTOSO],
          str(nv and nv["detail"]))
    check("run 2: pending_values carries the accept payload",
          len(res2["pending_values"]) == 1
          and res2["pending_values"][0]["new"] == [CONTOSO]
          and res2["pending_values"][0]["match"] == "Branches_*.xlsx"
          and res2["pending_values"][0]["baseline_version"] == 1
          and res2["pending_values"][0]["generator_id"] == gid_q,
          str(res2["pending_values"]))

    rd = one(res2, generator="gen-quality", kind="rows_drop", file="Branches_1.xlsx")
    check("run 2: rows 100 -> 40 (99 -> 39 data rows, header excluded) is a rows_drop",
          rd is not None and rd["detail"]["rows"] == 39 and rd["detail"]["previous"] == 99,
          str(rd and rd["detail"]))
    fcd = one(res2, generator="gen-quality", kind="file_count_drop")
    check("run 2: 4 files -> 1 is a file_count_drop",
          fcd is not None and fcd["detail"]["file_count"] == 1
          and fcd["detail"]["previous"] == 4, str(fcd and fcd["detail"]))

    mc = one(res2, generator="gen-quality", kind="schema_missing_column",
             column="SKU Description")
    check("run 2: the renamed column is a schema_missing_column",
          mc is not None and mc["severity"] == "warn", str(mc))
    if mc:
        check("run 2: detail.suggest names the near-miss header",
              mc["detail"].get("suggest") == "SKU_Discription", str(mc["detail"]))
        # The column is DECLARED by gen-quality and was never seen being
        # read (the hooks recorded Branch/Sum only), so the impact verdict
        # must be the weak one — a claim, not evidence.
        check("run 2: detail.affected grades the evidence honestly",
              [(a["generator"], a["confidence"]) for a in mc["detail"].get("affected", [])]
              == [("gen-quality", "declared")],
              str(mc["detail"].get("affected")))
        check("run 2: affected lists the report that reads it",
              any(any(rep["id"] == dash_q for rep in a["reports"])
                  for a in mc["detail"].get("affected", [])),
              str(mc["detail"].get("affected")))
    check("run 2: the observed spelling is an info-level extra column",
          (one(res2, generator="gen-quality", kind="schema_extra_column",
               column="SKU_Discription") or {}).get("severity") == "info")
    check("run 2: the vanished required pattern is an error",
          (one(res2, generator="gen-quality", kind="schema_missing_pattern",
               file="Ref_*.xlsx") or {}).get("severity") == "error")
    rfg = one(res2, generator="gen-quality", kind="required_file_gone", file="Ref_*.xlsx")
    check("run 2: required_file_gone (had 1, has 0) is an error",
          rfg is not None and rfg["severity"] == "error" and rfg["detail"]["previous"] == 1,
          str(rfg and rfg["detail"]))
    check("run 2: since_run of a first-seen finding is this run",
          mc is not None and mc["since_run"] == run2, str(mc and mc["since_run"]))
    check("run 2: source status is the worst open severity",
          {s["key"]: s["status"] for s in res2["sources"]}.get(f"folder:{BRANCH_DIR}") == "error",
          str([(s["key"], s["status"]) for s in res2["sources"]]))
    check("history series is aligned run-by-run, newest first",
          res2["history"][f"folder:{BRANCH_DIR}|gen-quality"]["runs"] == [run2, run1]
          and res2["history"][f"folder:{BRANCH_DIR}|gen-quality"]["file_count"] == [1, 4]
          and res2["history"][f"folder:{BRANCH_DIR}|gen-quality"]["rows"]["Branches_1.xlsx/Sheet1"] == [40, 100],
          str(res2["history"].get(f"folder:{BRANCH_DIR}|gen-quality")))

    # ── ids are a property of the finding, not of the evaluation ─────────
    res2b = evaluate()
    check("finding ids are stable across two evaluate() calls",
          [f["id"] for f in res2["findings"]] == [f["id"] for f in res2b["findings"]],
          "ids moved between two identical evaluations")
    check("finding_id is the documented sha1 prefix",
          mc is not None and mc["id"] == quality.finding_id(
              "schema_missing_column", f"folder:{BRANCH_DIR}", "gen-quality",
              "Branches_*.xlsx", "Sheet1", "SKU Description"),
          str(mc and mc["id"]))

    # ── run_summary: what publish tells the analyst about THIS run ───────
    db = SessionLocal()
    from app.models import GenerationRun
    run2_row = db.query(GenerationRun).filter(GenerationRun.id == run2).first()
    summary = quality.run_summary(db, run2_row)
    db.close()
    check("run_summary lists the failed check with its counts",
          len(summary["failed_checks"]) == 1
          and summary["failed_checks"][0]["file"] == "Branches_1.xlsx"
          and summary["failed_checks"][0]["column"] == "Sum"
          and summary["failed_checks"][0]["failed"] == 3
          and summary["failed_checks"][0]["type"] == "not_null",
          str(summary["failed_checks"]))
    check("run_summary lists the new values (capped at 5 + a `more` count)",
          summary["new_values"] == [{"file": "Branches_*.xlsx", "sheet": "Sheet1",
                                     "column": "Branch", "values": [CONTOSO], "more": 0}],
          str(summary["new_values"]))
    check("run_summary links to the page filtered by generator",
          summary["quality_url"] == "/data-quality?generator=gen-quality",
          summary["quality_url"])

    # ── an ack silences the count, not the row ──────────────────────────
    open_before = res2["stats"]["open"]
    db = SessionLocal()
    admin_id = db.query(User).filter(User.person_id == "admin1").first().id
    db.add(QualityAck(finding_id=rd["id"], data_source_id=rd["source_id"],
                      note="known, the month is closed", created_by=admin_id))
    db.commit()
    db.close()

    res3 = evaluate()
    acked = one(res3, generator="gen-quality", kind="rows_drop", file="Branches_1.xlsx")
    check("acked finding stays listed and is marked",
          acked is not None and acked["acked"] is True
          and acked["ack"]["note"] == "known, the month is closed", str(acked and acked["ack"]))
    check("acked finding stops counting as open",
          res3["stats"]["open"] == open_before - 1 and res3["stats"]["acked"] == 1,
          f"{res3['stats']} vs open_before={open_before}")

    # ── run 3: nothing changed; the engine re-checks nothing ────────────
    run3 = start_run(c, dash_q, BRANCH_DIR, [
        {"name": "Branches_1.xlsx", "size": 2_000, "mtime_ms": NOW_MS,
         "matched_input": "Branches_*.xlsx"}])
    post_obs(c, dash_q, run3, [{"dir": 0, "name": "Branches_1.xlsx", "kind": "cached"}])
    close_run(c, dash_q, run3)
    stamp(run3, NOW - timedelta(minutes=10))

    res4 = evaluate()
    ids2 = {f["id"] for f in res3["findings"]}
    ids3 = {f["id"] for f in res4["findings"]}
    check("run 3 (unchanged): no finding the previous run did not already have",
          ids3 <= ids2, str([f["message"] for f in res4["findings"] if f["id"] not in ids2]))
    check("run 3: check results carried forward from the last run that checked",
          one(res4, generator="gen-quality", kind="check_fail", file="Branches_1.xlsx") is not None)
    mc3 = one(res4, generator="gen-quality", kind="schema_missing_column",
              column="SKU Description")
    check("run 3: the schema finding survives on a carried-forward schema",
          mc3 is not None, "missing column vanished when the file was cached")
    check("run 3: since_run walks back to the run the finding first appeared on",
          mc3 is not None and mc3["since_run"] == run2, str(mc3 and mc3["since_run"]))
    check("run 3: the one-off drops cleared (nothing dropped against run 2)",
          not pick(res4, generator="gen-quality", kind="file_count_drop")
          and not pick(res4, generator="gen-quality", kind="rows_drop"),
          str([f["kind"] for f in res4["findings"] if f["generator"] == "gen-quality"]))

    # ── accepting the values (what P2.2's endpoint will write) ──────────
    db = SessionLocal()
    b = db.query(ValueBaseline).filter(ValueBaseline.generator_id == gid_q).first()
    b.set_values([ASHFORD, CONTOSO])
    b.version += 1
    b.accepted_by = admin_id
    b.accepted_at = datetime.utcnow()
    db.commit()
    db.close()

    res5 = evaluate()
    check("accepted values: the new_values warning is gone",
          not [f for f in res5["findings"]
               if f["kind"] == "new_values" and f["severity"] == "warn"],
          str([f["message"] for f in res5["findings"] if f["kind"] == "new_values"]))
    check("accepted values: nothing is pending any more",
          res5["pending_values"] == [], str(res5["pending_values"]))

    # ═══════════════════════════════════════════════════════════════════
    # Legacy generator: freshness + rows trends, and nothing declared
    # ═══════════════════════════════════════════════════════════════════
    old_ms = NOW_MS - 40 * DAY_MS
    runl1 = start_run(c, dash_leg, LEGACY_DIR,
                      [{"name": "old.xlsx", "size": 1_000, "mtime_ms": old_ms}])
    post_obs(c, dash_leg, runl1, [{"dir": 0, "name": "old.xlsx", "kind": "parsed",
                                   "schema": sheet_schema(["A", "B"], 100)}])
    close_run(c, dash_leg, runl1)
    stamp(runl1, NOW - timedelta(minutes=25))

    runl2 = start_run(c, dash_leg, LEGACY_DIR,
                      [{"name": "old.xlsx", "size": 1_100, "mtime_ms": old_ms}])
    post_obs(c, dash_leg, runl2, [{"dir": 0, "name": "old.xlsx", "kind": "parsed",
                                   "schema": sheet_schema(["A", "B"], 30)}])
    close_run(c, dash_leg, runl2)
    stamp(runl2, NOW - timedelta(minutes=5))

    res6 = evaluate()
    leg = [f for f in res6["findings"] if f["generator"] == "gen-legacy"]
    stale = one(res6, generator="gen-legacy", kind="file_stale")
    check("legacy: a folder nobody refreshed is stale",
          stale is not None and stale["severity"] == "warn"
          and stale["detail"]["stale_after_days"] == quality.STALE_DAYS_DEFAULT,
          str(stale and stale["detail"]))
    check("legacy: rows 100 -> 30 (99 -> 29 data rows, header excluded) is still a rows_drop",
          (one(res6, generator="gen-legacy", kind="rows_drop") or {}).get("detail", {}).get("previous") == 99,
          str([f["kind"] for f in leg]))
    check("legacy: nothing declared, so no schema findings at all",
          not [f for f in leg if f["kind"].startswith("schema_")]
          and not [f for f in leg if f["kind"] in ("check_fail", "new_values", "dead_declaration")],
          str([f["kind"] for f in leg]))
    check("stats.stale counts the stale sources",
          res6["stats"]["stale"] == 1, str(res6["stats"]))

    # ═══════════════════════════════════════════════════════════════════
    # No read hooks at all: "never consumed" is unanswerable, not a finding
    # ═══════════════════════════════════════════════════════════════════
    runn = start_run(c, dash_nc, NOCONS_DIR,
                     [{"name": "Plain_1.xlsx", "size": 900, "mtime_ms": NOW_MS,
                       "matched_input": "Plain_*.xlsx"}])
    post_obs(c, dash_nc, runn, [{"dir": 0, "name": "Plain_1.xlsx", "kind": "parsed",
                                 "schema": sheet_schema(["Branch"], 50)}])
    close_run(c, dash_nc, runn)
    stamp(runn, NOW - timedelta(minutes=4))

    # A second run of the same file whose header moved down two rows, and a
    # file the folder holds that no input admits (the engine's own skip
    # reason, recorded on the observation).
    # skipped goes through the real ingest path now (observations.py P?):
    # the engine reports it on input_manifest.skipped, not written by hand.
    runn2 = start_run(c, dash_nc, NOCONS_DIR,
                      [{"name": "Plain_1.xlsx", "size": 950, "mtime_ms": NOW_MS,
                        "matched_input": "Plain_*.xlsx"}],
                      skipped=[{"name": "~$draft.xlsx",
                                "reason": "not declared in inputs"}])
    post_obs(c, dash_nc, runn2, [{"dir": 0, "name": "Plain_1.xlsx", "kind": "parsed",
                                  "schema": sheet_schema(["Branch"], 50, header_row=2)}])
    close_run(c, dash_nc, runn2)
    stamp(runn2, NOW - timedelta(minutes=3))

    res7 = evaluate()
    hs = one(res7, generator="gen-nohooks", kind="header_shift")
    check("a header row that moved between runs is an info finding",
          hs is not None and hs["detail"] == {"header_row": 2, "previous": 0},
          str(hs and hs["detail"]))
    uf = one(res7, generator="gen-nohooks", kind="schema_unexpected_file")
    check("a file no input admits is reported as unexpected, at info",
          uf is not None and uf["severity"] == "info" and uf["file"] == "~$draft.xlsx",
          str(uf))
    check("no consumed_json anywhere -> no dead_declaration (the tri-state's null)",
          not pick(res7, generator="gen-nohooks", kind="dead_declaration"),
          str([f["kind"] for f in res7["findings"] if f["generator"] == "gen-nohooks"]))

    # ═══════════════════════════════════════════════════════════════════
    # Shape + badge
    # ═══════════════════════════════════════════════════════════════════
    check("every finding carries the documented keys",
          all(set(f) == {"id", "severity", "kind", "source", "source_id", "generator",
                         "dashboard_ids", "file", "sheet", "column", "message",
                         "since_run", "first_seen_at", "detail", "acked", "ack"}
              for f in res7["findings"]),
          str(sorted(res7["findings"][0])) if res7["findings"] else "no findings")
    check("every kind is one of KINDS",
          all(f["kind"] in quality.KINDS for f in res7["findings"]),
          str({f["kind"] for f in res7["findings"]} - set(quality.KINDS)))
    check("every severity is one of SEVERITY",
          all(f["severity"] in quality.SEVERITY for f in res7["findings"]))
    check("findings are sorted worst-first",
          [quality.SEVERITY.index(f["severity"]) for f in res7["findings"]]
          == sorted(quality.SEVERITY.index(f["severity"]) for f in res7["findings"]))
    check("stats counts the sources by their status",
          res7["stats"]["sources_ok"] + res7["stats"]["sources_warn"]
          + res7["stats"]["sources_error"] == len(res7["sources"]), str(res7["stats"]))
    check("an info-only source is ok, not amber",
          {s["key"]: s["status"] for s in res7["sources"]}.get(f"folder:{NOCONS_DIR}") == "ok"
          and {s["key"]: s["open"] for s in res7["sources"]}.get(f"folder:{NOCONS_DIR}") == 2,
          str([(s["key"], s["status"], s["open"]) for s in res7["sources"]]))
    check("sources carry their thresholds",
          all("thresholds" in s and "min_files" in s and "stale_after_days" in s
              for s in res7["sources"]), str(res7["sources"][:1]))
    check("badge_count == stats.open", badge() == res7["stats"]["open"],
          f"{badge()} vs {res7['stats']['open']}")

    # ═══════════════════════════════════════════════════════════════════
    # Two checks on ONE column, a skipped check, an empty sheet, a sheet
    # that is not there, and a re-parsed file whose checks were dropped.
    # ═══════════════════════════════════════════════════════════════════
    MULTI_DIR = "Multi_Folder"
    gid_m, dash_m = register(c, "gen-multi", "Multi Check Report", {
        "inputs": [{"match": "Multi_*.xlsx", "role": "data", "required": True,
                    "sheets": [{"name": "Sheet*", "columns": ["A", "B", "Gone"]},
                               {"name": "Missing", "columns": ["C"]}]}],
        "checks": [
            {"match": "Multi_*.xlsx", "sheet": "Sheet1", "column": "B",
             "type": "not_null"},
            {"match": "Multi_*.xlsx", "sheet": "Sheet1", "column": "B",
             "type": "regex", "pattern": "^[0-9]+$"},
            {"match": "Multi_*.xlsx", "sheet": "Sheet1", "column": "A",
             "type": "unique"},
        ],
    })
    NN_B = "not_null:Multi_*.xlsx:Sheet1:B"
    RE_B = "regex:Multi_*.xlsx:Sheet1:B"
    UN_A = "unique:Multi_*.xlsx:Sheet1:A"
    multi_schema = {"sheets": [
        {"name": "Sheet1", "header": ["A", "B", "Extra"], "rows": 5, "header_row": 0},
        {"name": "Sheet2", "header": ["A", "B"], "rows": 0, "header_row": 0},
    ]}
    # The hooks saw the whole sheet opened, no column list: calamine can only
    # prove that much, and the declaration is a GLOB — the confidence must
    # still be downgraded.
    multi_consumed = {"sheets": {"Sheet1": {"all": True, "columns": []}}}

    runm1 = start_run(c, dash_m, MULTI_DIR,
                      [{"name": "Multi_1.xlsx", "size": 100, "mtime_ms": NOW_MS,
                        "matched_input": "Multi_*.xlsx"}])
    post_obs(c, dash_m, runm1, [{
        "dir": 0, "name": "Multi_1.xlsx", "kind": "parsed",
        "schema": multi_schema, "consumed": multi_consumed,
        "checks": [
            {"check_id": NN_B, "status": "fail", "count": 5, "failed": 2,
             "examples": ["row 2"], "sampled": False},
            {"check_id": RE_B, "status": "fail", "count": 5, "failed": 1,
             "examples": ["row 5"], "sampled": False},
            {"check_id": UN_A, "status": "skipped", "count": 5, "failed": 0,
             "reason": "cardinality_exceeded", "sampled": False},
        ],
    }])
    close_run(c, dash_m, runm1)
    stamp(runm1, NOW - timedelta(minutes=2))

    resm = evaluate()
    fails = pick(resm, generator="gen-multi", kind="check_fail", column="B")
    check("two different checks on one column are two findings with two ids",
          len(fails) == 2 and len({f["id"] for f in fails}) == 2
          and {f["detail"]["type"] for f in fails} == {"not_null", "regex"},
          str([(f["detail"]["type"], f["id"]) for f in fails]))
    check("finding ids are unique within one evaluation",
          len({f["id"] for f in resm["findings"]}) == len(resm["findings"]),
          str(sorted(f["id"] for f in resm["findings"])))
    skipped = one(resm, generator="gen-multi", kind="check_fail", column="A")
    check("a check the engine could not complete is an info, not a failure",
          skipped is not None and skipped["severity"] == "info"
          and skipped["detail"]["reason"] == "cardinality_exceeded",
          str(skipped and skipped["detail"]))
    rz = one(resm, generator="gen-multi", kind="rows_zero", sheet="Sheet2")
    check("an empty sheet of a declared data input is an error",
          rz is not None and rz["severity"] == "error" and rz["detail"]["rows"] == 0,
          str(rz and rz["detail"]))
    ms = one(resm, generator="gen-multi", kind="schema_missing_sheet", sheet="Missing")
    check("a declared sheet nobody delivered is a schema_missing_sheet",
          ms is not None and ms["severity"] == "warn"
          and ms["detail"]["columns_declared"] == 1, str(ms and ms["detail"]))
    check("a missing sheet does not also report each of its columns",
          not pick(resm, generator="gen-multi", kind="schema_missing_column",
                   sheet="Missing"))
    gone = one(resm, generator="gen-multi", kind="schema_missing_column", column="Gone")
    check("a column that is not in the file can only be 'declared'",
          gone is not None
          and [(a["generator"], a["confidence"]) for a in gone["detail"]["affected"]]
          == [("gen-multi", "declared")],
          str(gone and gone["detail"].get("affected")))
    ex = one(resm, generator="gen-multi", kind="schema_extra_column", column="Extra")
    check("a GLOB-declared sheet still downgrades to sheet_level evidence",
          ex is not None and ex["sheet"] == "Sheet*"
          and [(a["generator"], a["confidence"]) for a in ex["detail"]["affected"]]
          == [("gen-multi", "sheet_level")],
          str(ex and (ex["sheet"], ex["detail"].get("affected"))))

    # ── the file was re-read and CHANGED, but the checks were dropped ────
    # (the worker's CHECK_BUDGET_MS, or the engine stripping an oversized
    # payload) — last run's verdict is not this run's file.
    runm2 = start_run(c, dash_m, MULTI_DIR,
                      [{"name": "Multi_1.xlsx", "size": 4_000,
                        "mtime_ms": NOW_MS + 60_000,
                        "matched_input": "Multi_*.xlsx"}])
    post_obs(c, dash_m, runm2, [{"dir": 0, "name": "Multi_1.xlsx", "kind": "parsed",
                                 "schema": multi_schema, "consumed": multi_consumed}])
    close_run(c, dash_m, runm2)
    stamp(runm2, NOW - timedelta(minutes=1))

    resm2 = evaluate()
    check("a changed file with no checks reports no check result at all",
          not pick(resm2, generator="gen-multi", kind="check_fail"),
          str([f["message"] for f in pick(resm2, generator="gen-multi", kind="check_fail")]))
    check("the schema findings of the same file still stand",
          one(resm2, generator="gen-multi", kind="schema_missing_sheet",
              sheet="Missing") is not None)
    check("badge_count still equals stats.open with impact analysis skipped",
          badge() == resm2["stats"]["open"], f"{badge()} vs {resm2['stats']['open']}")

    # ═══════════════════════════════════════════════════════════════════
    # P2.2: the API layer — page, JSON, badge, acks, baselines, owner_id
    # ═══════════════════════════════════════════════════════════════════
    r = c.get("/data-quality")
    check("GET /data-quality 200 with the dq-root mount",
          r.status_code == 200 and 'id="dq-root"' in r.text,
          f"{r.status_code}")
    check("page carries a pending_values JSON blob for the new_values accept flow",
          'data-pending="' in r.text, r.text[:200])
    check("page loads static/js/data-quality.js with the asset_v cache-buster",
          re.search(r'/static/js/data-quality\.js\?v=[^"\']+', r.text) is not None,
          r.text[:200])
    check("page carries the page-local [hidden] rule (author display beats it otherwise)",
          "[hidden] { display: none !important; }" in r.text, r.text[:200])

    r = c.get("/api/data-quality")
    check("GET /api/data-quality 200", r.status_code == 200, str(r.status_code))
    api_res = r.json()
    check("api result has the documented shape",
          set(api_res) == {"sources", "findings", "pending_values", "history", "stats"},
          str(sorted(api_res)))

    r = c.get("/api/data-quality/badge")
    check("badge endpoint matches quality.badge_count",
          r.status_code == 200 and r.json()["open"] == badge(),
          f"{r.json()} vs {badge()}")
    r2 = c.get("/api/data-quality/badge")
    check("badge is cached (same value on an immediate repeat)",
          r2.json()["open"] == r.json()["open"])

    # ── ack via the API: open -> acked -> DELETE -> open again ──────────
    open_before_api = api_res["stats"]["open"]
    ack_target = stale  # gen-legacy's file_stale, still reproducing at this point
    r = c.post("/api/data-quality/acks", json={
        "finding_id": ack_target["id"], "source_id": ack_target["source_id"],
        "note": "known, offline until Monday",
    })
    check("POST ack 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    ack_id = r.json()["id"]

    res_acked = evaluate()
    acked_row = one(res_acked, generator="gen-legacy", kind="file_stale")
    check("finding is acked after the API call",
          acked_row is not None and acked_row["acked"] is True
          and acked_row["ack"]["note"] == "known, offline until Monday",
          str(acked_row and acked_row.get("ack")))
    check("badge dropped by one after the ack (cache cleared)",
          badge() == open_before_api - 1, f"{badge()} vs {open_before_api - 1}")

    r = c.delete(f"/api/data-quality/acks/{ack_id}")
    check("DELETE ack 204", r.status_code == 204, str(r.status_code))
    res_unacked = evaluate()
    unacked_row = one(res_unacked, generator="gen-legacy", kind="file_stale")
    check("finding is open again after DELETE",
          unacked_row is not None and unacked_row["acked"] is False,
          str(unacked_row))
    check("badge is back up after the delete",
          badge() == open_before_api, f"{badge()} vs {open_before_api}")

    # ── baseline accept via the API: 200, then a stale version -> 409 ───
    BASE_DIR = "Owner_Folder"
    gid_o, dash_o = register(c, "gen-owner", "Owner Report", {
        "inputs": [{"match": "Owner_*.xlsx", "role": "data", "required": True,
                    "sheets": [{"name": "Sheet1", "columns": ["Region"]}]}],
        "checks": [{"match": "Owner_*.xlsx", "sheet": "Sheet1", "column": "Region",
                    "type": "known_values"}],
    })
    KV_O = "known_values:Owner_*.xlsx:Sheet1:Region"
    REGION_A, REGION_B = "South", "North"

    runo1 = start_run(c, dash_o, BASE_DIR, [
        {"name": "Owner_1.xlsx", "size": 500, "mtime_ms": NOW_MS,
         "matched_input": "Owner_*.xlsx"}])
    post_obs(c, dash_o, runo1, [{
        "dir": 0, "name": "Owner_1.xlsx", "kind": "parsed",
        "schema": sheet_schema(["Region"], 20),
        "checks": [{"check_id": KV_O, "status": "pass", "count": 20, "failed": 0,
                    "distinct": [REGION_A], "sampled": False}],
    }])
    close_run(c, dash_o, runo1)
    stamp(runo1, NOW - timedelta(minutes=45))

    runo2 = start_run(c, dash_o, BASE_DIR, [
        {"name": "Owner_1.xlsx", "size": 520, "mtime_ms": NOW_MS,
         "matched_input": "Owner_*.xlsx"}])
    post_obs(c, dash_o, runo2, [{
        "dir": 0, "name": "Owner_1.xlsx", "kind": "parsed",
        "schema": sheet_schema(["Region"], 21),
        "checks": [{"check_id": KV_O, "status": "pass", "count": 21, "failed": 0,
                    "distinct": [REGION_A, REGION_B], "sampled": False}],
    }])
    close_run(c, dash_o, runo2)
    stamp(runo2, NOW - timedelta(minutes=44))

    res_o = evaluate()
    nv_o = one(res_o, generator="gen-owner", kind="new_values", column="Region")
    check("gen-owner: the second region is a fresh new_values warning",
          nv_o is not None and nv_o["detail"]["new"] == [REGION_B], str(nv_o))
    pend_o = [p for p in res_o["pending_values"] if p["generator"] == "gen-owner"][0]
    source_o = pend_o["source_id"]

    r = c.post(f"/api/data-sources/{source_o}/baselines/{gid_o}", json={
        "match": "Owner_*.xlsx", "sheet": "Sheet1", "column": "Region",
        "accept": [REGION_B], "version": pend_o["baseline_version"] + 1,
    })
    check("baseline accept with the WRONG version -> 409",
          r.status_code == 409
          and r.json()["detail"]["current"]["version"] == pend_o["baseline_version"],
          f"{r.status_code} {r.text[:300]}")

    r = c.post(f"/api/data-sources/{source_o}/baselines/{gid_o}", json={
        "match": "Owner_*.xlsx", "sheet": "Sheet1", "column": "Region",
        "accept": [REGION_B], "version": pend_o["baseline_version"],
    })
    check("baseline accept with the right version -> 200",
          r.status_code == 200 and set(r.json()["values"]) == {REGION_A, REGION_B}
          and r.json()["version"] == pend_o["baseline_version"] + 1,
          f"{r.status_code} {r.text[:300]}")

    res_o2 = evaluate()
    check("accepted value: new_values finding is gone",
          not pick(res_o2, generator="gen-owner", kind="new_values"),
          str([f["message"] for f in pick(res_o2, generator="gen-owner", kind="new_values")]))
    check("accepted value: nothing pending for gen-owner any more",
          not [p for p in res_o2["pending_values"] if p["generator"] == "gen-owner"],
          str(res_o2["pending_values"]))

    # ── owner_id: round-trips through PUT /api/data-sources, validated ──
    r = c.put("/api/data-sources", json={
        "kind": "folder", "key_name": BASE_DIR, "owner_id": admin_id})
    check("owner_id round-trips through PUT /api/data-sources",
          r.status_code == 200 and r.json()["owner_id"] == admin_id,
          f"{r.status_code} {r.text[:300]}")

    r = c.put("/api/data-sources", json={
        "kind": "folder", "key_name": BASE_DIR, "owner_id": 999_999})
    check("owner_id: an unknown user id -> 422",
          r.status_code == 422, f"{r.status_code} {r.text[:200]}")

    r = c.put("/api/data-sources", json={
        "kind": "folder", "key_name": BASE_DIR, "owner_id": None})
    check("owner_id: explicit null clears it back to unclaimed",
          r.status_code == 200 and r.json()["owner_id"] is None,
          f"{r.status_code} {r.text[:300]}")

    # ── an unrelated user: empty scope, 404 on writes to what it cannot reach ──
    db = SessionLocal()
    db.add(User(person_id="bob", display_name="Bob", role="user", is_active=True))
    db.commit()
    db.close()

    c3 = TestClient(app)
    login(c3, "bob")

    r = c3.get("/api/data-quality")
    check("unrelated user: empty findings/sources (no reach at all)",
          r.status_code == 200 and r.json()["findings"] == [] and r.json()["sources"] == [],
          str(r.json())[:200])

    r = c3.post("/api/data-quality/acks", json={
        "finding_id": ack_target["id"], "source_id": ack_target["source_id"]})
    check("unrelated user: ack on a source they cannot reach -> 404 (reach model, not 403)",
          r.status_code == 404, f"{r.status_code} {r.text[:200]}")

    r = c3.post(f"/api/data-sources/{source_o}/baselines/{gid_o}", json={
        "match": "Owner_*.xlsx", "sheet": "Sheet1", "column": "Region",
        "accept": [REGION_A], "version": 1})
    check("unrelated user: baseline accept on a source they cannot reach -> 404",
          r.status_code == 404, f"{r.status_code} {r.text[:200]}")

    # ── cross-source ack forgery: reach on YOUR source must not suppress
    # a finding that actually lives on a source you cannot reach ──────────
    from app.models import DashboardPermission
    db = SessionLocal()
    bob_row = db.query(User).filter(User.person_id == "bob").first()
    db.add(DashboardPermission(dashboard_id=dash_o, user_id=bob_row.id, can_update_data=True))
    db.commit()
    db.close()

    victim_finding_id = ack_target["id"]
    victim_source_id = ack_target["source_id"]
    open_before_forgery = badge()

    r = c3.post("/api/data-quality/acks", json={
        "finding_id": victim_finding_id, "source_id": victim_source_id})
    check("bob: honest ack naming the victim's own source -> 404 (no reach there)",
          r.status_code == 404, f"{r.status_code} {r.text[:200]}")

    r = c3.post("/api/data-quality/acks", json={
        "finding_id": victim_finding_id, "source_id": source_o})
    check("bob: forged ack (his own reachable source, someone else's finding_id) -> 201 accepted "
          "(reach is on source_o, which is legitimately his)",
          r.status_code == 201, f"{r.status_code} {r.text[:200]}")

    res_after_forgery = evaluate()
    victim_row = [f for f in res_after_forgery["findings"] if f["id"] == victim_finding_id]
    check("victim finding is NOT acked by the cross-source forged ack",
          victim_row and victim_row[0]["acked"] is False,
          str(victim_row))
    check("open count is unchanged by the cross-source forged ack",
          badge() == open_before_forgery, f"{badge()} vs {open_before_forgery}")

    # ── a sampled known_values scan must not seed a baseline (a PARTIAL
    # read is not "the accepted reference set") ─────────────────────────
    SAMP_DIR = "Sampled_Folder"
    gid_s, dash_s = register(c, "gen-sampled", "Sampled Report", {
        "inputs": [{"match": "Sampled_*.xlsx", "role": "data", "required": True,
                    "sheets": [{"name": "Sheet1", "columns": ["Branch"]}]}],
        "checks": [{"match": "Sampled_*.xlsx", "sheet": "Sheet1", "column": "Branch",
                    "type": "known_values"}],
    })
    KV_S = "known_values:Sampled_*.xlsx:Sheet1:Branch"
    runs1 = start_run(c, dash_s, SAMP_DIR, [
        {"name": "Sampled_1.xlsx", "size": 500, "mtime_ms": NOW_MS,
         "matched_input": "Sampled_*.xlsx"}])
    post_obs(c, dash_s, runs1, [{
        "dir": 0, "name": "Sampled_1.xlsx", "kind": "parsed",
        "schema": sheet_schema(["Branch"], 300_000),
        "checks": [{"check_id": KV_S, "status": "pass", "count": 200_000, "failed": 0,
                    "distinct": [ASHFORD], "sampled": True}],
    }])
    close_run(c, dash_s, runs1)
    stamp(runs1, NOW - timedelta(minutes=50))

    evaluate()
    db = SessionLocal()
    base_s = db.query(ValueBaseline).filter(ValueBaseline.generator_id == gid_s).first()
    check("a sampled known_values scan does not seed a ValueBaseline",
          base_s is None, str(base_s and base_s.get_values()))
    db.close()

    # A later, UNSAMPLED run of the same check must seed it normally.
    runs2 = start_run(c, dash_s, SAMP_DIR, [
        {"name": "Sampled_1.xlsx", "size": 500, "mtime_ms": NOW_MS,
         "matched_input": "Sampled_*.xlsx"}])
    post_obs(c, dash_s, runs2, [{
        "dir": 0, "name": "Sampled_1.xlsx", "kind": "parsed",
        "schema": sheet_schema(["Branch"], 150),
        "checks": [{"check_id": KV_S, "status": "pass", "count": 149, "failed": 0,
                    "distinct": [ASHFORD, CONTOSO], "sampled": False}],
    }])
    close_run(c, dash_s, runs2)
    stamp(runs2, NOW - timedelta(minutes=49))

    evaluate()
    db = SessionLocal()
    base_s2 = db.query(ValueBaseline).filter(ValueBaseline.generator_id == gid_s).first()
    check("an unsampled run seeds the baseline normally",
          base_s2 is not None and set(base_s2.get_values()) == {ASHFORD, CONTOSO},
          str(base_s2 and base_s2.get_values()))
    db.close()

    # ── schema_unreadable: 633eaaa's per-sheet reason / top-level error ──
    test_schema_unreadable(c)

    # ── capacity-limited schema must not fake "sheet observed, empty" ────
    test_schema_capacity_not_missing_sheet(c)

    # ── retention (P4.3): quality.prune ─────────────────────────────────
    test_prune()

    # ── the pure helpers, on their own ──────────────────────────────────
    check("levenshtein is a real edit distance",
          quality.levenshtein("kitten", "sitting") == 3 and quality.levenshtein("a", "a") == 0)
    check("nearest refuses a candidate that is merely another column",
          quality.nearest("Branch", ["Amount", "Date"]) is None,
          str(quality.nearest("Branch", ["Amount", "Date"])))
    check("finding_id treats None and '' as the same absent field",
          quality.finding_id("k", "s", "g", "f", None, None)
          == quality.finding_id("k", "s", "g", "f", "", ""))

    # ── _sheets_of derives data rows, it does not reuse sniff's sheet
    # height (a header-only export must show 0 data rows, not 1) ────────
    header_only = quality._sheets_of([{"name": "f.xlsx",
                                       "schema": sheet_schema(["A", "B"], 1)}])
    check("_sheets_of: a header-only sheet (height 1, header_row 0) has 0 data rows",
          header_only[("f.xlsx", "Sheet1")]["data_rows"] == 0
          and header_only[("f.xlsx", "Sheet1")]["rows"] == 1,
          str(header_only))
    title_row = quality._sheets_of([{"name": "f.xlsx",
                                     "schema": sheet_schema(["A", "B"], 2, header_row=1)}])
    check("_sheets_of: a title row above the header shifts data_rows by header_row+1 too",
          title_row[("f.xlsx", "Sheet1")]["data_rows"] == 0,
          str(title_row))
    normal = quality._sheets_of([{"name": "f.xlsx",
                                  "schema": sheet_schema(["A", "B"], 41)}])
    check("_sheets_of: 41 physical rows with a header is 40 data rows",
          normal[("f.xlsx", "Sheet1")]["data_rows"] == 40, str(normal))

    # ── observations._skipped_for: dir attribution for input_manifest.skipped
    skip_list = [{"name": "a.tmp", "reason": "not an Excel file", "dir": 0},
                 {"name": "b.tmp", "reason": "not an Excel file", "dir": 1}]
    check("_skipped_for: multi-folder run attributes by explicit dir tag",
          json.loads(observations._skipped_for(skip_list, 1, 2)) == [skip_list[1]],
          str(observations._skipped_for(skip_list, 1, 2)))
    check("_skipped_for: single-folder run attributes an untagged entry to dir 0",
          json.loads(observations._skipped_for(
              [{"name": "a.tmp", "reason": "not an Excel file"}], 0, 1))
          == [{"name": "a.tmp", "reason": "not an Excel file"}])
    check("_skipped_for: multi-folder run cannot attribute an untagged entry",
          observations._skipped_for(
              [{"name": "a.tmp", "reason": "not an Excel file"}], 0, 2) is None)

    # ── K2: observability follows the RESOLVED engine, not the declared one
    # A folder run (engine "local" at start) comes from a browser/auto
    # manifest without `sources`: it reads files under the same R2 contract
    # as the browser and sends observations. Marking it "unobservable" would
    # throw away real evidence, while "auto" + sources is, conversely, blind.
    check("resolved_engine: browser/auto without sources is an observable path",
          genmanifest.resolved_engine({"engine": "auto"}) == "browser"
          and genmanifest.resolved_engine({"engine": "browser"}) == "browser")
    check("resolved_engine: sources under auto resolve to agent",
          genmanifest.resolved_engine({"engine": "auto", "sources": {"queries": []}}) == "agent"
          and genmanifest.resolved_engine({"engine": "agent"}) == "agent")
    empty_graph = {"nodes": [], "edges": []}
    imp_local = quality._Impact(empty_graph, {}, {"g": "browser"}, {}, {})
    imp_agent = quality._Impact(empty_graph, {}, {"g": "agent"}, {}, {})
    check("_confidence: a folder/browser run is NOT unobservable",
          imp_local._confidence("g", None, None, False) == "declared",
          imp_local._confidence("g", None, None, False))
    check("_confidence: the agent SQL path is still unobservable",
          imp_agent._confidence("g", None, None, False) == "unobservable",
          imp_agent._confidence("g", None, None, False))

    test_reference(c)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
