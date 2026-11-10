"""Smoke test for the Data Sources tab and the source registry.

Runs against a throwaway SQLite DB + uploads dir. Exercises the whole path a
source takes: observed via source_observations/source_files -> aggregated with
volume and freshness -> claimed in the registry with a declared path -> given
override rules -> those rules reaching the engine through the manifest
endpoint.

Observations are seeded THROUGH THE ORM directly (GenerationRun +
SourceObservation + SourceFile rows), not through the run-start endpoint —
wiring that endpoint to actually ingest observations is another agent's task.
This also lets each observation carry a `created_at` we control, which is what
the freshness-vs-recency (data_stale vs unread) split needs.

The interesting assertions are the ones about ATTRIBUTION (a source's current
volume comes from its single LATEST observation, not the sum of every reader's
observation of it), about the DATA/READ freshness split, and about
AUTHORISATION (a source may only be edited by someone who can run a report
that reads it).
"""
import io
import json
import os
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="op-ds-smoke-")
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


def make_bundle(name, inputs=None):
    m = {
        "name": name,
        "version": "1.0",
        "entrypoint": {"module": "gen.main"},
        "outputs": {"html": True, "slots": []},
        "engine": "browser",
    }
    if inputs is not None:
        m["inputs"] = inputs
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("gen/__init__.py", "")
        z.writestr("gen/main.py", "def begin(*a, **k):\n    pass\n")
    return buf.getvalue()


DAY_MS = 86400_000
NOW_MS = int(time.time() * 1000)
NOW = datetime.utcnow()


def register(c, gen_name, title, inputs=None):
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", make_bundle(gen_name, inputs), "application/zip")})
    assert r.status_code == 201, f"register {gen_name}: {r.status_code} {r.text[:300]}"
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": title, "generator_id": gid})
    assert r.status_code == 201, f"create {title}: {r.status_code} {r.text[:300]}"
    return gid, r.json()["id"]


def seed_run(db, dash_id, gid, *, status="error", started_at=None):
    """A GenerationRun row with no input_manifest — the observation tables are
    the only record of what it read. `status="error"` by default so the
    run-lock TTL / "still running" machinery never gets in this test's way."""
    from app.models import GenerationRun, GeneratorVersion
    ver = (db.query(GeneratorVersion)
           .filter(GeneratorVersion.generator_id == gid, GeneratorVersion.is_current == True)  # noqa: E712
           .first())
    run = GenerationRun(dashboard_id=dash_id, generator_version_id=ver.id, engine="browser",
                        status=status, started_at=started_at or NOW,
                        finished_at=started_at or NOW)
    db.add(run)
    db.flush()
    return run


def seed_observation(db, run, kind, key_name, dir_index, files, *, created_at=None):
    """files: [(name, size, mtime_ms)]. Creates (or reuses) the DataSource row
    an observation must point to, then the observation and its file rows."""
    from app.models import DataSource, SourceFile, SourceObservation
    ds = db.query(DataSource).filter(DataSource.kind == kind, DataSource.key_name == key_name).first()
    if ds is None:
        ds = DataSource(kind=kind, key_name=key_name)
        db.add(ds)
        db.flush()
    total_bytes = sum(f[1] for f in files)
    newest_ms = max((f[2] for f in files), default=None)
    newest_dt = datetime.utcfromtimestamp(newest_ms / 1000.0) if newest_ms else None
    obs = SourceObservation(run_id=run.id, data_source_id=ds.id, dir_index=dir_index,
                            file_count=len(files), total_bytes=total_bytes,
                            newest_mtime=newest_dt, fingerprint=f"fp-{key_name}-{run.id}",
                            created_at=created_at or NOW)
    db.add(obs)
    db.flush()
    for name, size, mtime_ms in files:
        db.add(SourceFile(observation_id=obs.id, name=name, size=size, mtime_ms=mtime_ms, kind="parsed"))
    db.commit()
    return ds, obs


REF_SLUG = "nwd_bv"
REF_INPUTS = [
    {"match": "SL_*.xlsx", "role": "data", "required": True,
     "sheets": [{"name": "Sheet1", "columns": ["BV"]}]},
    {"match": f"{REF_SLUG}.xlsx", "role": "reference", "source": "platform",
     "table": REF_SLUG,
     "sheets": [{"name": "data", "columns": [{"name": "BV", "type": "text"},
                                             {"name": "Code", "type": "text"}]}]},
]


def test_run_input_rules(c, c_stranger, dash_id, gid):
    """"Do not read" rules from the Run button's side.

    Half the job was already written (`input_overrides` + the drawer on the
    sources page) but lived where a person does not look for it: they look at
    Run, while the rule sits on the sources page — and it could only be set on
    something a run had ALREADY observed. These routes address the same record
    by folder NAME and create the source if no run has touched it yet.
    """
    from app.database import SessionLocal
    from app.models import AuditLog, DataSource, InputOverride

    r = c.get(f"/api/dashboards/{dash_id}/generator/input-rules")
    check("input-rules: GET returns the report's observed folders",
          r.status_code == 200 and "Stock_RDC" in r.json()["folders"],
          f"{r.status_code} {r.text[:200]}")
    before = r.json()["folders"]["Stock_RDC"]
    check("input-rules: a folder without rules has version 0",
          before["version"] == 0 and before["exclude_rules"] == [], str(before))

    r = c.put(f"/api/dashboards/{dash_id}/generator/input-rules",
              json={"folder": "Stock_RDC", "exclude_rules": ["w31.xlsx"], "version": 0})
    check("input-rules: a rule by folder name is saved",
          r.status_code == 200 and r.json()["exclude_rules"] == ["w31.xlsx"]
          and r.json()["version"] == 1, f"{r.status_code} {r.text[:200]}")

    # The key point: the same rule reaches the engine through the same channel
    # it reads the folder by — otherwise the selection screen would lie.
    r = c.get(f"/api/dashboards/{dash_id}/generator/manifest")
    check("input-rules: the rule reaches the engine via source_rules",
          r.status_code == 200
          and (r.json().get("source_rules") or {}).get("Stock_RDC") == ["w31.xlsx"],
          str(r.json().get("source_rules") if r.status_code == 200 else r.text[:200]))

    r = c.put(f"/api/dashboards/{dash_id}/generator/input-rules",
              json={"folder": "Stock_RDC", "exclude_rules": [], "version": 0})
    check("input-rules: stale version -> 409, not a silent overwrite",
          r.status_code == 409, f"{r.status_code} {r.text[:200]}")

    # A folder no run has touched yet: the source is created here, otherwise
    # an unwanted file could only be excluded AFTER a run that read it.
    r = c.put(f"/api/dashboards/{dash_id}/generator/input-rules",
              json={"folder": "Fresh_Folder", "exclude_rules": ["copy of *.xlsx"],
                    "version": 0})
    check("input-rules: a rule can be set on a folder with no run at all",
          r.status_code == 200 and r.json()["version"] == 1,
          f"{r.status_code} {r.text[:200]}")
    db = SessionLocal()
    src = (db.query(DataSource)
           .filter(DataSource.kind == "folder", DataSource.key_name == "Fresh_Folder").first())
    check("input-rules: the new folder's source is created once",
          src is not None
          and db.query(InputOverride).filter(
              InputOverride.generator_id == gid,
              InputOverride.data_source_id == src.id).count() == 1,
          str(src))
    row = (db.query(AuditLog)
           .filter(AuditLog.action == "input_override_update")
           .order_by(AuditLog.id.desc()).first())
    check("input-rules: the edit is audited and tagged with its origin",
          row is not None and json.loads(row.detail).get("via") == "run_preview"
          and json.loads(row.detail).get("folder") == "Fresh_Folder",
          str(row and row.detail))
    db.close()

    # Masks go through globmatch only: "archive/**" would be accepted and never
    # applied (the walk is flat).
    r = c.put(f"/api/dashboards/{dash_id}/generator/input-rules",
              json={"folder": "Stock_RDC", "exclude_rules": ["archive/**"]})
    check("input-rules: a mask with a subfolder is rejected (globmatch semantics)",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    r = c.put(f"/api/dashboards/{dash_id}/generator/input-rules",
              json={"folder": "Stock_RDC", "exclude_rules": ["*"]})
    check("input-rules: a mask of only asterisks is rejected",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    r = c.put(f"/api/dashboards/{dash_id}/generator/input-rules",
              json={"folder": "a/b", "exclude_rules": ["x.xlsx"]})
    check("input-rules: a path instead of a folder name is rejected",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    # The pre-run screen has its OWN ceiling, far above the lineage drawer's:
    # "uncheck all" in a folder of 171 exports produces 171 rules, and hitting
    # a limit of thirty-two here would block exactly what the button is for.
    # Nobody types masks by hand on this screen.
    r = c.put(f"/api/dashboards/{dash_id}/generator/input-rules",
              json={"folder": "Stock_RDC",
                    "exclude_rules": [f"f{i}.xlsx" for i in range(200)]})
    check("input-rules: two hundred names are accepted (excluding a whole folder)",
          r.status_code == 200 and len(r.json()["exclude_rules"]) == 200,
          f"{r.status_code} {r.text[:160]}")
    ver_bulk = r.json()["version"]
    r = c.put(f"/api/dashboards/{dash_id}/generator/input-rules",
              json={"folder": "Stock_RDC",
                    "exclude_rules": [f"f{i}.xlsx" for i in range(1001)],
                    "version": ver_bulk})
    check("input-rules: more than a thousand names is rejected",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    r = c.put(f"/api/dashboards/{dash_id}/generator/input-rules",
              json={"folder": "Stock_RDC", "exclude_rules": [], "version": ver_bulk})
    check("input-rules: a bulk exclusion is undone in bulk too",
          r.status_code == 200 and r.json()["exclude_rules"] == [],
          f"{r.status_code} {r.text[:160]}")

    # Exactly the same right as for a run: a reader neither sees nor changes it.
    r = c_stranger.get(f"/api/dashboards/{dash_id}/generator/input-rules")
    check("input-rules: without the update-data right GET -> 403",
          r.status_code == 403, f"{r.status_code} {r.text[:200]}")
    r = c_stranger.put(f"/api/dashboards/{dash_id}/generator/input-rules",
                       json={"folder": "Stock_RDC", "exclude_rules": []})
    check("input-rules: without the update-data right PUT -> 403",
          r.status_code == 403, f"{r.status_code} {r.text[:200]}")

    # ── "Anna renamed the file": a file-name alias ───────────────────────
    # The trouble masks cannot fix by construction: the file is right, the
    # name is wrong. An alias NAMES a file under an already-declared input — it
    # does not widen the mask, so no second meaning of "what was read" appears.
    # This test uses its own generator: `gen-stock` has no `inputs` at all, so
    # an alias would have nothing to refer to — the engine reads everything.
    gid_al, dash_al = register(c, "gen-file-alias", "File Alias Report", [
        {"match": "SL_*.xlsx", "role": "data", "required": True,
         "sheets": [{"name": "Sheet1", "columns": ["BV"]}]},
    ])

    r = c.put(f"/api/dashboards/{dash_al}/generator/input-rules",
              json={"folder": "Stock_RDC", "exclude_rules": [],
                    "file_aliases": {"SL_*.xlsx": ["report from Anna (3).xlsx"]},
                    "version": 0})
    check("input-rules: an alias is saved",
          r.status_code == 200
          and r.json()["file_aliases"] == {"SL_*.xlsx": ["report from Anna (3).xlsx"]},
          f"{r.status_code} {r.text[:200]}")
    ver = r.json()["version"]

    r = c.get(f"/api/dashboards/{dash_al}/generator/input-rules")
    check("input-rules: GET returns the aliases",
          r.status_code == 200
          and r.json()["folders"]["Stock_RDC"]["file_aliases"]
              == {"SL_*.xlsx": ["report from Anna (3).xlsx"]},
          f"{r.status_code} {r.text[:200]}")

    r = c.put(f"/api/dashboards/{dash_al}/generator/input-rules",
              json={"folder": "Stock_RDC", "exclude_rules": [],
                    "file_aliases": {"NO_SUCH_*.xlsx": ["x.xlsx"]}, "version": ver})
    check("input-rules: an alias for an undeclared input -> 422",
          r.status_code == 422, f"{r.status_code} {r.text[:200]}")

    r = c.put(f"/api/dashboards/{dash_al}/generator/input-rules",
              json={"folder": "Stock_RDC", "exclude_rules": [],
                    "file_aliases": {"SL_*.xlsx": ["sub/x.xlsx"]}, "version": ver})
    check("input-rules: a path instead of a file name -> 400",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    r = c.put(f"/api/dashboards/{dash_al}/generator/input-rules",
              json={"folder": "Stock_RDC", "exclude_rules": [],
                    "file_aliases": {"SL_*.xlsx": [f"f{i}.xlsx" for i in range(9)]},
                    "version": ver})
    check("input-rules: more than eight names per input -> 400",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    # A failed edit must not have changed anything.
    r = c.get(f"/api/dashboards/{dash_al}/generator/input-rules")
    check("input-rules: a refusal left the saved alias alone",
          r.json()["folders"]["Stock_RDC"]["file_aliases"]
              == {"SL_*.xlsx": ["report from Anna (3).xlsx"]},
          str(r.json()["folders"]["Stock_RDC"]))

    # Clearing aliases is {}, not "omit the field".
    r = c.put(f"/api/dashboards/{dash_al}/generator/input-rules",
              json={"folder": "Stock_RDC", "exclude_rules": [],
                    "file_aliases": {}, "version": ver})
    check("input-rules: aliases are cleared with an empty dict",
          r.status_code == 200 and r.json()["file_aliases"] == {},
          f"{r.status_code} {r.text[:200]}")


def test_reference_source(c):
    """ADR-16: a platform reference table is a SOURCE — it appears on Data
    Sources with `kind: "table"` and as a layer-0 node feeding the pattern of
    the workbook the platform materialises for the run (`<slug>.xlsx`), the
    same way a folder feeds its own pattern."""
    from app.database import SessionLocal
    from app.models import Dashboard, ReferenceTable
    from app.services import datasources, lineage, reference

    gid_ref, dash_ref = register(c, "gen-ref", "SL Reference Report", REF_INPUTS)

    db = SessionLocal()
    # ADR-16 addendum: registering the generator ALREADY created the table
    # from the manifest's platform input (empty, schema only) — this test
    # only gives it a human title and its first version.
    t = db.query(ReferenceTable).filter(ReferenceTable.slug == REF_SLUG).one()
    check("registering a generator creates the declared reference table",
          t.key_column == "BV" and [c["name"] for c in t.get_columns()] == ["BV", "Code"],
          str(t.get_columns()))
    t.title = "BV NWD→CONTOSO"
    reference.ensure_data_source(db, t)
    reference.create_version(db, t, [["1001", "A"], ["1002", "B"]], None,
                             {"added": 2, "changed": 0, "removed": 0})
    seed_run(db, dash_ref, gid_ref, status="success", started_at=NOW)
    db.commit()

    dashboards = db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all()
    g = datasources.build(db, dashboards)
    by = {s["key"]: s for s in g["sources"]}
    src = by.get(f"table:{REF_SLUG}")
    check("a platform input registers the reference table as a source",
          src is not None, str(sorted(by)))
    check("the table source is certain and carries its version and row count",
          src is not None and src["kind"] == "table" and src["certain"] is True
          and src["table"]["exists"] is True
          and src["table"]["current_version"] == 1
          and src["table"]["row_count"] == 2,
          str(src and src["table"]))
    check("the table source is labelled with the table's own title",
          src is not None and src["label"] == "BV NWD→CONTOSO", str(src and src["label"]))
    check("the table source counts its consuming report",
          src is not None and src["report_count"] == 1 and dash_ref in src["reports"],
          str(src and src["reports"]))
    check("stats counts reference tables", g["stats"]["tables"] == 1,
          str(g["stats"].get("tables")))

    graph = lineage.build_graph(db, dashboards)
    nodes = {n["id"]: n for n in graph["nodes"]}
    tnode = next((n for n in graph["nodes"]
                  if n["kind"] == "table" and n["data"].get("key_name") == REF_SLUG), None)
    check("lineage draws the reference table at layer 0",
          tnode is not None and tnode["layer"] == 0, str(tnode))
    pat_id = f"pat:gen-ref:{REF_SLUG}.xlsx"
    check("lineage has the platform workbook's pattern node", pat_id in nodes,
          str([i for i in nodes if i.startswith("pat:gen-ref")]))
    check("the table feeds that pattern and no folder does",
          tnode is not None
          and any(e["from"] == tnode["id"] and e["to"] == pat_id for e in graph["edges"])
          and not any(e["to"] == pat_id and e["from"] != tnode["id"]
                      for e in graph["edges"]),
          str([e for e in graph["edges"] if e["to"] == pat_id]))
    check("the platform pattern is not painted as unmatched",
          nodes.get(pat_id, {}).get("data", {}).get("unmatched") is False,
          str(nodes.get(pat_id, {}).get("data")))
    db.close()

    r = c.get("/data-sources")
    check("the page renders the reference source with its own filter chip",
          r.status_code == 200 and 'data-ds-chip="table"' in r.text
          and f'href="/reference/{REF_SLUG}"' in r.text
          and "reference table" in r.text,
          str(r.status_code))


def main():
    with TestClient(app) as c:
        body(c)


def body(c):
    login(c, "admin1")
    from app.database import SessionLocal
    from app.models import Dashboard, InputOverride

    # ── Two reports, two generators, an overlapping folder ──────────────
    gid_stock, stock = register(c, "gen-stock", "RDC Stock Health")
    gid_ops, ops = register(c, "gen-ops", "OPS RDC Health")

    db = SessionLocal()
    # Stock reads two folders in one run: Stock_RDC (dir 0) and
    # Reasons_Catalog (dir 1, a stale reference workbook).
    run_stock = seed_run(db, stock, gid_stock, started_at=NOW)
    seed_observation(db, run_stock, "folder", "Stock_RDC", 0,
                     [("w31.xlsx", 10 * 1048576, NOW_MS - DAY_MS),
                      ("w32.xlsx", 30 * 1048576, NOW_MS)],
                     created_at=NOW)
    ds_reasons, _ = seed_observation(db, run_stock, "folder", "Reasons_Catalog", 1,
                     [("SL_NWD.xls", 2 * 1048576, NOW_MS - 40 * DAY_MS)],
                     created_at=NOW)
    reasons_id = ds_reasons.id

    # Ops shares Stock_RDC — this is the overlap the page exists to surface.
    # Its observation is LATER and sees more of the folder (w31+w32 still
    # there, plus its own ops.xlsx): 45 MB. If the old sum-per-reader logic
    # were still in play this would show 40 + 45 = 85 MB; the union logic
    # must show exactly the latest observation's own total, 45 MB.
    run_ops = seed_run(db, ops, gid_ops, started_at=NOW + timedelta(minutes=5))
    seed_observation(db, run_ops, "folder", "Stock_RDC", 0,
                     [("w31.xlsx", 10 * 1048576, NOW_MS - DAY_MS),
                      ("w32.xlsx", 30 * 1048576, NOW_MS),
                      ("ops.xlsx", 5 * 1048576, NOW_MS)],
                     created_at=NOW + timedelta(minutes=5))
    run_ops_id = run_ops.id
    db.close()

    # ── The page ────────────────────────────────────────────────────────
    r = c.get("/data-sources")
    page = r.text
    check("/data-sources renders", r.status_code == 200, f"{r.status_code} {page[:200]}")
    check("page lists both folders",
          "Stock_RDC" in page and "Reasons_Catalog" in page, "missing a folder name")
    check("page lists both reports",
          "RDC Stock Health" in page and "OPS RDC Health" in page, "missing a report")
    check("nav entry present", 'href="/data-sources"' in page, "sidebar link missing")
    check("filter toolbar present",
          'id="ds-search"' in page and 'data-ds-chip="stale"' in page, "toolbar missing")
    check("editor drawer present",
          'id="ds-drawer"' in page and 'data-drawer-body' in page, "drawer missing")
    check("every source row has an edit affordance",
          page.count('data-src-edit=') == page.count('data-src-row='),
          "edit buttons do not match source rows")
    check("editor payload carries file names",
          'data-sources=' in page and 'file_names' in page, "sources_json missing names")
    check("schema-diff partial rendered on /data-sources (drawer template)",
          'class="sd-diff"' in page, "sd-diff root class missing")

    # C3: the same partial (partials/schema_diff.html) renders inside the
    # viewer's Data dock for a generator-backed dashboard — one macro, both
    # doors, so "what does this report read" reads identically everywhere.
    r = c.get(f"/d/{stock}")
    check("schema-diff partial rendered on /d/{id} (Data dock)",
          r.status_code == 200 and 'class="sd-diff"' in r.text,
          f"{r.status_code}; sd-diff present={'class=\"sd-diff\"' in r.text}")

    # ── Aggregation ─────────────────────────────────────────────────────
    from app.services import datasources
    db = SessionLocal()
    dashboards = db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all()
    g = datasources.build(db, dashboards)
    by = {s["key"]: s for s in g["sources"]}

    check("both folders aggregated", {"folder:Stock_RDC", "folder:Reasons_Catalog"} <= set(by),
          str(sorted(by)))
    s1 = by["folder:Stock_RDC"]
    check("shared folder counts both readers", s1["report_count"] == 2, str(s1["report_count"]))
    check("shared flag set", s1["shared"] is True)
    # Union, not sum: the latest (ops') observation alone is 45 MB.
    check("bytes = latest observation, not the sum of readers", s1["bytes"] == 45 * 1048576,
          f"{s1['bytes'] / 1048576:.0f} MB")
    check("file count = latest observation's own count", s1["files"] == 3, str(s1["files"]))

    s2 = by["folder:Reasons_Catalog"]
    check("dir=1 file attributed to the second folder", s2["files"] == 1, str(s2["files"]))
    check("freshness from mtime_ms", s2["data_age_days"] >= 39, str(s2["data_age_days"]))
    check("stale flagged past the threshold", s2["stale"] is True)
    check("data_stale alias matches stale", s2["data_stale"] == s2["stale"])
    check("fresh folder not flagged stale", s1["stale"] is False, str(s1["data_age_days"]))
    check("unclaimed folder is not certain", s1["certain"] is False)
    check("stats count the unidentified", g["stats"]["unidentified"] == 2,
          str(g["stats"]["unidentified"]))
    check("source_id/version present for an observed source",
          s1["source_id"] is not None and s1["version"] == 1,
          f"{s1['source_id']} {s1['version']}")
    check("fingerprint carried from the latest observation",
          s1["fingerprint"] == f"fp-Stock_RDC-{run_ops_id}", str(s1["fingerprint"]))
    db.close()

    # ── data_stale vs unread: two independent axes ──────────────────────
    gid_misc, misc = register(c, "gen-misc", "Misc Health")
    db = SessionLocal()
    run_misc = seed_run(db, misc, gid_misc, started_at=NOW)
    # Fresh data (mtime = now) that nobody has re-observed in weeks: unread,
    # not data_stale.
    seed_observation(db, run_misc, "folder", "Fresh_But_Unread", 0,
                     [("f.xlsx", 1024, NOW_MS)],
                     created_at=NOW - timedelta(days=20))
    # Stale data (mtime = 40 days old) that was just re-observed: data_stale,
    # not unread.
    seed_observation(db, run_misc, "folder", "Stale_But_Read", 1,
                     [("g.xlsx", 1024, NOW_MS - 40 * DAY_MS)],
                     created_at=NOW)
    db.close()

    db = SessionLocal()
    g2 = datasources.build(db, db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all())
    by2 = {s["key"]: s for s in g2["sources"]}
    fresh_unread = by2["folder:Fresh_But_Unread"]
    stale_read = by2["folder:Stale_But_Read"]
    check("fresh-but-unread: not data_stale", fresh_unread["data_stale"] is False,
          str(fresh_unread["data_age_days"]))
    check("fresh-but-unread: is unread", fresh_unread["unread"] is True,
          str(fresh_unread["read_age_days"]))
    check("stale-but-read: is data_stale", stale_read["data_stale"] is True,
          str(stale_read["data_age_days"]))
    check("stale-but-read: not unread", stale_read["unread"] is False,
          str(stale_read["read_age_days"]))
    check("stats separate the two axes",
          g2["stats"]["unread"] >= 1 and g2["stats"]["data_stale"] >= 2,
          str({"unread": g2["stats"]["unread"], "data_stale": g2["stats"]["data_stale"]}))
    db.close()

    # ── Override rules surface per generator ────────────────────────────
    db = SessionLocal()
    db.add(InputOverride(generator_id=gid_stock, data_source_id=reasons_id,
                         exclude_rules=json.dumps(["~$*", "*.tmp"])))
    db.commit()
    db.close()

    db = SessionLocal()
    g3 = datasources.build(db, db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all())
    reasons = {s["key"]: s for s in g3["sources"]}["folder:Reasons_Catalog"]
    check("override attached to the right generator",
          reasons["overrides"] == [{"generator": "gen-stock", "rules": ["~$*", "*.tmp"]}],
          str(reasons["overrides"]))
    check("excludes = flattened union of override rules",
          reasons["excludes"] == ["*.tmp", "~$*"], str(reasons["excludes"]))
    db.close()

    # ── Fallback: a dashboard with NO observations still lists its folder ──
    gid_legacy, legacy = register(c, "gen-legacy", "Legacy Report")
    db = SessionLocal()
    from app.models import GenerationRun, GeneratorVersion
    ver = (db.query(GeneratorVersion)
           .filter(GeneratorVersion.generator_id == gid_legacy, GeneratorVersion.is_current == True)  # noqa: E712
           .first())
    im = {
        "files": [{"name": "legacy.xlsx", "size": 1048576, "mtime_ms": NOW_MS}],
        "dirs": ["Legacy_Folder"],
        "count": 1, "total_bytes": 1048576, "skipped": [],
    }
    db.add(GenerationRun(dashboard_id=legacy, generator_version_id=ver.id, engine="browser",
                         status="error", started_at=NOW, finished_at=NOW,
                         input_manifest=json.dumps(im)))
    db.commit()
    db.close()

    db = SessionLocal()
    g4 = datasources.build(db, db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all())
    check("legacy dashboard's folder still shows up via the JSON fallback",
          "folder:Legacy_Folder" in {s["key"] for s in g4["sources"]},
          str(sorted(s["key"] for s in g4["sources"])))
    legacy_report = next(r for r in g4["reports"] if r["id"] == legacy)
    check("legacy report card carries its folder", legacy_report["dirs"] == ["Legacy_Folder"],
          str(legacy_report["dirs"]))
    check("observed dashboards unaffected by the fallback",
          "folder:Stock_RDC" in {s["key"] for s in g4["sources"]})
    db.close()

    # ── Mixed observed+fallback: same folder must still count ONCE ──────
    # gen-mix has NO observations at all (JSON-only run) but its manifest
    # names the very folder gen-stock/gen-ops already observe (Stock_RDC,
    # currently 45 MB / 3 files from the ops observation). The fallback path
    # must not add its own 12 MB on top of that — the union rule applies
    # across the observed/fallback split too, not only within each path.
    gid_mix, mix = register(c, "gen-mix", "Mix Report")
    db = SessionLocal()
    from app.models import GenerationRun as GR, GeneratorVersion as GV
    ver_mix = (db.query(GV)
              .filter(GV.generator_id == gid_mix, GV.is_current == True)  # noqa: E712
              .first())
    im_mix = {
        "files": [{"name": "mix.xlsx", "size": 12 * 1048576, "mtime_ms": NOW_MS}],
        "dirs": ["Stock_RDC"],
        "count": 1, "total_bytes": 12 * 1048576, "skipped": [],
    }
    db.add(GR(dashboard_id=mix, generator_version_id=ver_mix.id, engine="browser",
             status="error", started_at=NOW, finished_at=NOW,
             input_manifest=json.dumps(im_mix)))
    db.commit()
    db.close()

    db = SessionLocal()
    g_mix = datasources.build(db, db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all())
    stock_mixed = {s["key"]: s for s in g_mix["sources"]}["folder:Stock_RDC"]
    check("mixed observed+fallback: bytes stay at the observed union, not summed",
          stock_mixed["bytes"] == 45 * 1048576, f"{stock_mixed['bytes'] / 1048576:.0f} MB")
    check("mixed observed+fallback: files stay at the observed union, not summed",
          stock_mixed["files"] == 3, str(stock_mixed["files"]))
    check("mixed observed+fallback: the fallback dashboard is still counted as a reader",
          mix in stock_mixed["reports"], str(stock_mixed["reports"]))
    db.close()

    # ── A dashboard whose observations fall OUTSIDE the recent window must
    # still fall back to JSON, not vanish. One old browser-engine run (10
    # days ago) carries an observation; three newer agent-engine runs carry
    # only input_manifest JSON. _RUNS_PER_DASH=3 means the window holds only
    # the JSON runs, so the observation path finds nothing there — the
    # dashboard must fall back to JSON for itself rather than render bare.
    gid_mixeng, mixeng = register(c, "gen-mixeng", "Mixed Engine Report")
    db = SessionLocal()
    ver_me = (db.query(GV)
             .filter(GV.generator_id == gid_mixeng, GV.is_current == True)  # noqa: E712
             .first())
    old_run = GR(dashboard_id=mixeng, generator_version_id=ver_me.id, engine="browser",
                status="error", started_at=NOW - timedelta(days=10),
                finished_at=NOW - timedelta(days=10))
    db.add(old_run)
    db.flush()
    seed_observation(db, old_run, "folder", "Old_Window_Folder", 0,
                     [("o.xlsx", 1024, NOW_MS - 10 * DAY_MS)],
                     created_at=NOW - timedelta(days=10))
    im_agent = {
        "files": [{"name": "a.xlsx", "size": 2048, "mtime_ms": NOW_MS}],
        "dirs": ["Agent_Folder"],
        "count": 1, "total_bytes": 2048, "skipped": [],
    }
    for i in range(3):
        db.add(GR(dashboard_id=mixeng, generator_version_id=ver_me.id, engine="agent",
                 status="error",
                 started_at=NOW - timedelta(hours=i + 1), finished_at=NOW - timedelta(hours=i + 1),
                 input_manifest=json.dumps(im_agent)))
    db.commit()
    db.close()

    db = SessionLocal()
    g_me = datasources.build(db, db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all())
    me_keys = {s["key"] for s in g_me["sources"]}
    check("mixed-engine dashboard falls back to JSON when its window has no observations",
          "folder:Agent_Folder" in me_keys, str(sorted(me_keys)))
    me_report = next(r for r in g_me["reports"] if r["id"] == mixeng)
    check("mixed-engine report card carries the JSON folder, not an empty card",
          me_report["dirs"] == ["Agent_Folder"], str(me_report["dirs"]))
    db.close()

    # ── Claiming a source ───────────────────────────────────────────────
    # `excludes` in the PUT body is DEPRECATED (Data Lineage v2): junk rules
    # now live in input_overrides, keyed by (generator, source) — see the
    # manifest-scoping assertions above/below, seeded straight through the
    # ORM since the CRUD endpoint for overrides is a separate task (A6).
    db = SessionLocal()
    unidentified_before = datasources.build(
        db, db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all()
    )["stats"]["unidentified"]
    db.close()

    r = c.put("/api/data-sources", json={
        "kind": "folder", "key_name": "Stock_RDC",
        "declared_path": r"\\share\OPS\RDC\Stock_RDC",
        "label": "RDC weekly stock",
    })
    check("claim source -> 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    if r.status_code == 200:
        # `mix` (registered above, purely to test the union rule) also reads
        # Stock_RDC via the JSON fallback, so it's now a legitimate reader too.
        check("blast radius reported", sorted(r.json()["reports"]) == sorted([stock, ops, mix]),
              str(r.json().get("reports")))
        check("version bumped on the response", r.json()["version"] == 2, str(r.json().get("version")))

    db = SessionLocal()
    g = datasources.build(db, db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all())
    s1 = {s["key"]: s for s in g["sources"]}["folder:Stock_RDC"]
    check("declared path makes the source certain", s1["certain"] is True)
    check("declared path surfaced", s1["declared_path"].endswith("Stock_RDC"), str(s1["declared_path"]))
    check("unidentified count drops by one", g["stats"]["unidentified"] == unidentified_before - 1,
          f"{g['stats']['unidentified']} vs before {unidentified_before}")
    db.close()

    # ── Validation ──────────────────────────────────────────────────────
    r = c.put("/api/data-sources", json={"kind": "nope", "key_name": "x"})
    check("unknown kind -> 400", r.status_code == 400, str(r.status_code))
    r = c.put("/api/data-sources", json={"kind": "folder", "key_name": "NoSuchFolder"})
    check("source nobody reads -> 404", r.status_code == 404, str(r.status_code))
    r = c.put("/api/data-sources", json={
        "kind": "folder", "key_name": "Stock_RDC", "label": "stale claim", "version": 1})
    check("stale version -> 409", r.status_code == 409, str(r.status_code))

    # ── Data Quality thresholds on the source row ───────────────────────
    # Round-trip through PUT, then through the page payload. `version` is
    # omitted on purpose: these PUTs are about the columns, not the lock.
    r = c.put("/api/data-sources", json={
        "kind": "folder", "key_name": "Stock_RDC",
        "stale_after_days": 14, "min_files": 2,
        "thresholds": {"min_rows": {"Stock_*.xlsx/Data": 1000}, "drop_pct": 40},
    })
    check("thresholds PUT -> 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    if r.status_code == 200:
        j = r.json()
        check("thresholds round-trip on the response",
              j["stale_after_days"] == 14 and j["min_files"] == 2
              and j["thresholds"] == {"min_rows": {"Stock_*.xlsx/Data": 1000}, "drop_pct": 40},
              str({k: j[k] for k in ("stale_after_days", "min_files", "thresholds")}))
    # A label-only edit from an older drawer must not touch them.
    r = c.put("/api/data-sources", json={
        "kind": "folder", "key_name": "Stock_RDC", "label": "RDC weekly stock"})
    check("label-only PUT leaves thresholds alone",
          r.status_code == 200 and r.json()["min_files"] == 2
          and r.json()["thresholds"]["drop_pct"] == 40, r.text[:300])
    r = c.get("/data-sources")
    page_sources = {}
    if r.status_code == 200:
        import html as _html, re as _re
        m = _re.search(r'data-sources="([^"]*)"', r.text)
        if m:
            page_sources = {x["key"]: x for x in json.loads(_html.unescape(m.group(1)))}
    ps = page_sources.get("folder:Stock_RDC") or {}
    check("page payload carries the thresholds",
          ps.get("stale_after_days") == 14 and ps.get("min_files") == 2
          and (ps.get("thresholds") or {}).get("drop_pct") == 40,
          str({k: ps.get(k) for k in ("stale_after_days", "min_files", "thresholds")}))
    # Out-of-range / malformed -> 422, and nothing changes.
    for label, payload in (
        ("stale_after_days 0", {"stale_after_days": 0}),
        ("stale_after_days 3651", {"stale_after_days": 3651}),
        ("min_files -1", {"min_files": -1}),
        ("min_files 5001", {"min_files": 5001}),
        ("drop_pct 0", {"thresholds": {"drop_pct": 0}}),
        ("drop_pct 101", {"thresholds": {"drop_pct": 101}}),
        ("min_rows negative", {"thresholds": {"min_rows": {"a/b": -1}}}),
        ("min_rows non-int", {"thresholds": {"min_rows": {"a/b": "10"}}}),
        ("min_rows >32 keys", {"thresholds": {"min_rows": {f"m/s{i}": 1 for i in range(33)}}}),
        ("min_rows key >200 chars", {"thresholds": {"min_rows": {"x" * 201: 1}}}),
        ("thresholds unknown key", {"thresholds": {"max_rows": 5}}),
        ("thresholds not an object", {"thresholds": [1]}),
    ):
        r = c.put("/api/data-sources", json={"kind": "folder", "key_name": "Stock_RDC", **payload})
        check(f"{label} -> 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
    r = c.put("/api/data-sources", json={"kind": "folder", "key_name": "Stock_RDC"})
    check("rejected threshold edits changed nothing",
          r.status_code == 200 and r.json()["stale_after_days"] == 14
          and r.json()["thresholds"]["min_rows"] == {"Stock_*.xlsx/Data": 1000}, r.text[:300])
    # Explicit null clears back to "evaluator default".
    r = c.put("/api/data-sources", json={
        "kind": "folder", "key_name": "Stock_RDC", "stale_after_days": None, "thresholds": None})
    check("explicit null clears thresholds",
          r.status_code == 200 and r.json()["stale_after_days"] is None
          and r.json()["thresholds"] == {"min_rows": {}, "drop_pct": None}
          and r.json()["min_files"] == 2, r.text[:300])

    # ── column_aliases on the (generator, source) override ──────────────
    # A generator WITH declared inputs, observing Reasons_Catalog through its
    # own run — the reach rule the overrides routes enforce.
    gid_alias, dash_alias = register(c, "gen-alias", "Alias Report", inputs=[
        {"match": "SL_*.xls", "role": "reference",
         "sheets": [{"name": "Sheet1", "columns": ["SKU Description", "Qty"]}]},
    ])
    db = SessionLocal()
    run_alias = seed_run(db, dash_alias, gid_alias, started_at=NOW)
    seed_observation(db, run_alias, "folder", "Reasons_Catalog", 0,
                     [("SL_NWD.xls", 2 * 1048576, NOW_MS)], created_at=NOW)
    db.close()
    ov_url = f"/api/data-sources/{reasons_id}/overrides/{gid_alias}"
    r = c.put(ov_url, json={"exclude_rules": [],
                            "column_aliases": {"sl_*.xls/Sheet1": {"SKU Description": "SKU_Discription"}}})
    check("column_aliases PUT -> 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    if r.status_code == 200:
        check("aliases round-trip with the match canonicalised to the manifest's spelling",
              r.json()["column_aliases"] == {"SL_*.xls/Sheet1": {"SKU Description": "SKU_Discription"}},
              str(r.json()["column_aliases"]))
    r = c.get(f"/api/data-sources/{reasons_id}/overrides")
    got = {o["generator_id"]: o for o in (r.json() if r.status_code == 200 else [])}
    check("GET overrides returns the aliases",
          got.get(gid_alias, {}).get("column_aliases") == {"SL_*.xls/Sheet1": {"SKU Description": "SKU_Discription"}},
          str(got.get(gid_alias)))
    check("GET overrides: a generator without aliases reports {}",
          "column_aliases" in got.get(gid_stock, {}) and got[gid_stock]["column_aliases"] == {},
          str(got.get(gid_stock)))
    # Rules-only PUT (older drawer) leaves the aliases alone.
    r = c.put(ov_url, json={"exclude_rules": ["~$*"]})
    check("rules-only PUT keeps aliases",
          r.status_code == 200 and r.json()["column_aliases"] != {}, r.text[:300])
    for label, payload in (
        ("match not in inputs", {"Other_*.xlsx/Sheet1": {"a": "b"}}),
        ("key without /", {"SL_*.xls": {"a": "b"}}),
        ("empty sheet", {"SL_*.xls/": {"a": "b"}}),
        ("declared name >64", {"SL_*.xls/Sheet1": {"x" * 65: "b"}}),
        ("observed name empty", {"SL_*.xls/Sheet1": {"a": " "}}),
        ("too many pairs", {"SL_*.xls/Sheet1": {f"c{i}": f"d{i}" for i in range(65)}}),
        ("too many sheets", {f"SL_*.xls/S{i}": {"a": "b"} for i in range(33)}),
        ("pairs not an object", {"SL_*.xls/Sheet1": ["a", "b"]}),
    ):
        r = c.put(ov_url, json={"exclude_rules": [], "column_aliases": payload})
        check(f"aliases: {label} -> 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
    # A generator with NO inputs cannot hold aliases at all (nothing to bind to).
    r = c.put(f"/api/data-sources/{reasons_id}/overrides/{gid_stock}",
              json={"exclude_rules": [], "column_aliases": {"SL_*.xls/Sheet1": {"a": "b"}}})
    check("aliases on a generator without inputs -> 422", r.status_code == 422,
          f"{r.status_code} {r.text[:200]}")
    r = c.put(ov_url, json={"exclude_rules": [], "column_aliases": {}})
    check("empty column_aliases clears them",
          r.status_code == 200 and r.json()["column_aliases"] == {}, r.text[:300])

    # ── Annotations (P3.6) on the source — sheet + column notes ─────────
    ann_url = f"/api/data-sources/{reasons_id}/annotations"
    r = c.get(ann_url)
    check("GET annotations starts empty", r.status_code == 200 and r.json() == [], r.text[:300])
    r = c.put(ann_url, json={"items": [
        {"match": "SL_*.xls", "sheet": "Sheet1", "column": "SKU Description",
         "description": "The reference SKU name, kept in sync by hand.", "tags": ["core"]},
        {"match": "SL_*.xls", "sheet": "Sheet1", "column": None,
         "description": "Sheet-level note: this is the whole reasons catalog.", "tags": []},
    ]})
    check("PUT annotations -> 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    if r.status_code == 200:
        by_col = {a["column"]: a for a in r.json()}
        check("column annotation round-trips",
              by_col.get("SKU Description", {}).get("description")
              == "The reference SKU name, kept in sync by hand.", str(r.json()))
        check("sheet-level annotation round-trips with column=null (not '')",
              by_col.get(None, {}).get("description", "").startswith("Sheet-level"),
              str(r.json()))
    r = c.get(ann_url)
    check("GET annotations returns both rows after PUT",
          r.status_code == 200 and len(r.json()) == 2, r.text[:300])

    # PUT round-trip: sending fewer items deletes the missing one, but only
    # among known_keys — the rows this caller says it actually had in view.
    ann_known_keys = [
        {"match": "SL_*.xls", "sheet": "Sheet1", "column": "SKU Description"},
        {"match": "SL_*.xls", "sheet": "Sheet1", "column": None},
    ]
    r = c.put(ann_url, json={
        "items": [
            {"match": "SL_*.xls", "sheet": "Sheet1", "column": "SKU Description",
             "description": "Updated description.", "tags": ["core", "renamed"]},
        ],
        "known_keys": ann_known_keys,
    })
    check("PUT with one item -> 200 (delete-missing)", r.status_code == 200, f"{r.status_code}")
    if r.status_code == 200:
        check("delete-missing: sheet-level row is gone",
              len(r.json()) == 1 and r.json()[0]["description"] == "Updated description.",
              str(r.json()))
    r = c.put(ann_url, json={"items": []})
    check("PUT empty items with no known_keys deletes nothing",
          r.status_code == 200 and len(r.json()) == 1, r.text[:300])
    r = c.put(ann_url, json={"items": [], "known_keys": ann_known_keys})
    check("PUT empty items + known_keys clears the known rows",
          r.status_code == 200 and r.json() == [], r.text[:300])

    # ── Annotations: a save whose known_keys omits a row must not delete
    # it — the P3.6 fix-round bug: a drawer built from the CURRENT reachable
    # generator versions cannot see a stale/out-of-reach annotation, and
    # must not wipe it just because its own PUT doesn't repeat it.
    r = c.put(ann_url, json={"items": [
        {"match": "SL_*.xls", "sheet": "Sheet1", "column": "SKU Description",
         "description": "Kept alive.", "tags": []},
        {"match": "SL_*.xls", "sheet": "Sheet1", "column": "Old Dropped Column",
         "description": "Out of this caller's view.", "tags": []},
    ], "known_keys": [
        {"match": "SL_*.xls", "sheet": "Sheet1", "column": "SKU Description"},
        {"match": "SL_*.xls", "sheet": "Sheet1", "column": "Old Dropped Column"},
    ]})
    check("seed two annotations for the scoped-delete test", r.status_code == 200, str(r.status_code))
    r = c.put(ann_url, json={
        "items": [{"match": "SL_*.xls", "sheet": "Sheet1", "column": "SKU Description",
                   "description": "Kept alive, re-saved.", "tags": []}],
        "known_keys": [{"match": "SL_*.xls", "sheet": "Sheet1", "column": "SKU Description"}],
    })
    check("PUT -> 200 with a narrower known_keys", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        by_col = {a["column"]: a["description"] for a in r.json()}
        check("row outside known_keys survives an unrelated save",
              by_col.get("Old Dropped Column") == "Out of this caller's view.", str(r.json()))
        check("row inside known_keys was updated",
              by_col.get("SKU Description") == "Kept alive, re-saved.", str(r.json()))
    r = c.put(ann_url, json={"items": [], "known_keys": [
        {"match": "SL_*.xls", "sheet": "Sheet1", "column": "SKU Description"},
        {"match": "SL_*.xls", "sheet": "Sheet1", "column": "Old Dropped Column"},
    ]})
    check("cleanup: both rows clearable once both are in known_keys",
          r.status_code == 200 and r.json() == [], str(r.json()))

    # ── Annotations: 422 on over-limits ──────────────────────────────
    for label, items in (
        ("too many items", [
            {"match": "SL_*.xls", "sheet": "Sheet1", "column": f"C{i}", "description": "x"}
            for i in range(257)
        ]),
        ("empty description", [
            {"match": "SL_*.xls", "sheet": "Sheet1", "column": "A", "description": ""},
        ]),
        ("description >500 chars", [
            {"match": "SL_*.xls", "sheet": "Sheet1", "column": "A", "description": "x" * 501},
        ]),
        ("empty match", [
            {"match": "", "sheet": "Sheet1", "column": "A", "description": "d"},
        ]),
        ("empty sheet", [
            {"match": "SL_*.xls", "sheet": "", "column": "A", "description": "d"},
        ]),
        ("too many tags", [
            {"match": "SL_*.xls", "sheet": "Sheet1", "column": "A", "description": "d",
             "tags": [f"t{i}" for i in range(9)]},
        ]),
        ("tag too long", [
            {"match": "SL_*.xls", "sheet": "Sheet1", "column": "A", "description": "d",
             "tags": ["x" * 33]},
        ]),
        ("duplicate key in the same PUT", [
            {"match": "SL_*.xls", "sheet": "Sheet1", "column": "A", "description": "d1"},
            {"match": "SL_*.xls", "sheet": "Sheet1", "column": "A", "description": "d2"},
        ]),
    ):
        r = c.put(ann_url, json={"items": items})
        check(f"annotations: {label} -> 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
    r = c.get(ann_url)
    check("rejected annotation edits changed nothing", r.status_code == 200 and r.json() == [],
          r.text[:300])

    # ── Annotations: 404 on a source nobody reads ─────────────────────
    r = c.get(f"/api/data-sources/999999/annotations")
    check("GET annotations on unknown source -> 404", r.status_code == 404, str(r.status_code))
    r = c.put(f"/api/data-sources/999999/annotations", json={"items": []})
    check("PUT annotations on unknown source -> 404", r.status_code == 404, str(r.status_code))

    # ── The rules must reach the engine, scoped per generator ───────────
    # gen-stock's own run observed Reasons_Catalog (dir 1), so its override
    # (seeded above) must show up on gen-stock's manifest but nowhere on
    # gen-ops's — the v1 bug this replaces leaked every rule in the registry
    # to every generator that happened to share a folder basename.
    r = c.get(f"/api/dashboards/{stock}/generator/manifest")
    check("manifest endpoint -> 200", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        rules = r.json().get("source_rules") or {}
        check("manifest carries the override, scoped to its own generator",
              rules.get("Reasons_Catalog") == ["~$*", "*.tmp"], str(rules))
        check("folders the generator never observed are absent",
              "Stock_RDC" not in rules, str(rules))
    r = c.get(f"/api/dashboards/{ops}/generator/manifest")
    if r.status_code == 200:
        check("a generator that never observed the source sees no rule for it",
              "Reasons_Catalog" not in (r.json().get("source_rules") or {}),
              str(r.json().get("source_rules")))

    # ── Authorisation: a stranger cannot edit, nor even see ─────────────
    from app.database import SessionLocal as SL2
    from app.models import User
    db = SL2()
    db.add(User(person_id="anna", display_name="Anna", role="user", is_active=True))
    db.commit()
    db.close()

    c2 = TestClient(app)
    login(c2, "anna")
    r = c2.put("/api/data-sources", json={
        "kind": "folder", "key_name": "Stock_RDC", "label": "hijacked"})
    check("user with no reach cannot edit -> 404", r.status_code == 404,
          f"{r.status_code} {r.text[:200]}")
    r = c2.get("/data-sources")
    check("user with no reach sees an empty page",
          r.status_code == 200 and "Stock_RDC" not in r.text, str(r.status_code))

    # Annotations: same permission shape as overrides (GET empty, PUT 404).
    r = c2.get(ann_url)
    check("GET annotations: user with no reach sees empty list (not an error)",
          r.status_code == 200 and r.json() == [], f"{r.status_code} {r.text[:200]}")
    r = c2.put(ann_url, json={"items": [
        {"match": "SL_*.xls", "sheet": "Sheet1", "column": "A", "description": "hijacked"},
    ]})
    check("PUT annotations: user with no reach -> 404", r.status_code == 404,
          f"{r.status_code} {r.text[:200]}")

    # The claim survived the rejected edit.
    db = SessionLocal()
    g5 = datasources.build(db, db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all())
    s1 = {s["key"]: s for s in g5["sources"]}["folder:Stock_RDC"]
    check("rejected edit changed nothing", s1["label"] == "RDC weekly stock", str(s1["label"]))
    db.close()

    test_run_input_rules(c, c2, stock, gid_stock)

    test_reference_source(c)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
