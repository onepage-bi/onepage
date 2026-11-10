"""Smoke test for the lineage graph (app/services/lineage.py + GET /api/data-sources/lineage).

Seeds, through the ORM, the three shapes the graph has to survive:

  * a DECLARING generator — `inputs` with a Cyrillic sheet/column declaration
    plus a `required_files` name that becomes a reference pattern by sugar,
    observed with one declared column missing, one undeclared column present,
    an undeclared extra sheet, and a run that wrote a file BACK into the
    folder it reads;
  * a LEGACY generator — no `inputs` at all, which must still draw a pattern
    (the synthetic `*`), because "reads every Excel file here" is behaviour,
    not an absence;
  * a GREENPLUM generator — no observations are possible at all (agent engine,
    subprocess CLI), so its source wires straight into the generator.

The assertions that matter are the STRUCTURAL ones: every edge endpoint
exists, layers increase along every edge except the one that deliberately
points backwards (`writes_back`), and the node `data` keys are a superset of
what tests/fixtures/lineage_sample.json pins for each node kind — that
fixture is what the UI is being built against, so the server may add keys and
may never drop one.
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

TMP = tempfile.mkdtemp(prefix="op-lineage-smoke-")
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

DAY_MS = 86400_000
NOW_MS = int(time.time() * 1000)
NOW = datetime.utcnow()

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "lineage_sample.json")

ID_RE = re.compile(r"^(src|pat|sheet|stage|gen|out|rep):")
LAYER_BY_KIND = {"folder": 0, "greenplum": 0, "pattern": 1, "sheet": 2,
                 "stage": 2.5, "generator": 3, "output": 4, "write_back": 4,
                 "report": 5}


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def make_bundle(manifest, extra_files=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        z.writestr("gen/__init__.py", "")
        z.writestr("gen/main.py", "def begin(*a, **k):\n    pass\n")
        for name, body in extra_files:
            z.writestr(name, body)
    return buf.getvalue()


def register(c, manifest, title, extra_files=()):
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", make_bundle(manifest, extra_files), "application/zip")})
    assert r.status_code == 201, f"register {manifest['name']}: {r.status_code} {r.text[:400]}"
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": title, "generator_id": gid})
    assert r.status_code == 201, f"create {title}: {r.status_code} {r.text[:300]}"
    return gid, r.json()["id"]


def seed_run(db, dash_id, gid, *, status="success", started_at=None, outputs=None):
    from app.models import GenerationRun, GeneratorVersion
    ver = (db.query(GeneratorVersion)
           .filter(GeneratorVersion.generator_id == gid, GeneratorVersion.is_current == True)  # noqa: E712
           .first())
    run = GenerationRun(dashboard_id=dash_id, generator_version_id=ver.id, engine="browser",
                        status=status, started_at=started_at or NOW,
                        finished_at=started_at or NOW,
                        outputs_json=json.dumps(outputs, ensure_ascii=False) if outputs else None)
    db.add(run)
    db.flush()
    return run


def seed_observation(db, run, key_name, dir_index, files, *, created_at=None, kind="folder"):
    """files: [(name, size, mtime_ms, schema|None, consumed|None)]."""
    from app.models import DataSource, SourceFile, SourceObservation
    ds = db.query(DataSource).filter(DataSource.kind == kind,
                                     DataSource.key_name == key_name).first()
    if ds is None:
        ds = DataSource(kind=kind, key_name=key_name)
        db.add(ds)
        db.flush()
    newest_ms = max((f[2] for f in files), default=None)
    obs = SourceObservation(
        run_id=run.id, data_source_id=ds.id, dir_index=dir_index, file_count=len(files),
        total_bytes=sum(f[1] for f in files),
        newest_mtime=datetime.utcfromtimestamp(newest_ms / 1000.0) if newest_ms else None,
        fingerprint=f"fp-{key_name}-{run.id}", created_at=created_at or NOW)
    db.add(obs)
    db.flush()
    for name, size, mtime_ms, schema, consumed in files:
        db.add(SourceFile(
            observation_id=obs.id, name=name, size=size, mtime_ms=mtime_ms, kind="parsed",
            schema_json=json.dumps(schema, ensure_ascii=False) if schema else None,
            consumed_json=json.dumps(consumed, ensure_ascii=False) if consumed else None))
    db.commit()
    return ds, obs


# ── manifests ──────────────────────────────────────────────────────────────
M_RDC = {
    "name": "gen-rdc", "version": "1.0", "engine": "browser",
    "entrypoint": {"module": "gen.main"},
    "outputs": {"html": True, "slots": [{"index": 0, "name": "stock.bin.gz", "type": "binary"}]},
    "required_files": ["SL_NWD.xls"],
    "inputs": [{
        "match": "Stock_*.xlsx", "role": "data", "required": False,
        "sheets": [{"name": "Warehouse", "columns": ["SKU", "Qty", "Reason"]}],
    }],
}
M_OPS = {
    "name": "gen-ops", "version": "1.0", "engine": "browser",
    "entrypoint": {"module": "gen.main"},
    "outputs": {"html": True, "slots": []},
    # required_files alone must not make the manifest "declared" — the engine
    # still reads every Excel file (regression 2026-08-21, ops-rdc-health).
    "required_files": ["Stock_w31.xlsx"],
}
M_DWH = {
    "name": "gen-dwh", "version": "1.0", "engine": "agent",
    "entrypoint": {"module": "gen.main"},
    "outputs": {"html": True, "slots": []},
    "sources": {"type": "greenplum", "connection": "gp_dwh",
                "queries": [{"sql_file": "sql/rejections.sql",
                             "output_name": "rejections.csv.gz", "format": "csv_gz"}]},
}

SCHEMA_W31 = {"sheets": [{"name": "Warehouse",
                          "header": ["SKU", "Qty", "Qty new"],
                          "rows": 184000, "truncated": False}],
              "truncated": False, "error": None}
SCHEMA_W32 = {"sheets": [{"name": "Warehouse", "header": ["SKU", "Qty", "Qty new"],
                          "rows": 12, "truncated": False},
                         {"name": "Service", "header": ["x"], "rows": 1, "truncated": False}],
              "truncated": False, "error": None}
CONSUMED_W31 = {"sheets": {"Warehouse": {"columns": ["SKU", "Qty"], "all": False}}}
SCHEMA_SL = {"sheets": [{"name": "Reasons", "header": ["Reason_ID", "Reason_Name"],
                         "rows": 10, "truncated": False}],
             "truncated": False, "error": None}


def main():
    with TestClient(app) as c:
        body(c)


def body(c):
    login(c, "admin1")
    from app.database import SessionLocal
    from app.models import Dashboard

    gid_rdc, d_stock = register(c, M_RDC, "RDC Stock Health")
    gid_ops, d_ops = register(c, M_OPS, "OPS RDC Health")
    gid_dwh, d_gp = register(c, M_DWH, "DWH Rejection Analysis",
                             extra_files=[("sql/rejections.sql", "select 1;\n")])

    db = SessionLocal()
    run_stock = seed_run(db, d_stock, gid_rdc, started_at=NOW, outputs={
        "write_back": [{"name": "sl_unassigned_reasons.xlsx", "size": 4096,
                        "targets": ["Stock_RDC"], "failed": []}]})
    seed_observation(db, run_stock, "Stock_RDC", 0, [
        ("Stock_w31.xlsx", 10 * 1048576, NOW_MS - DAY_MS, SCHEMA_W31, CONSUMED_W31),
        ("Stock_w32.xlsx", 20 * 1048576, NOW_MS, SCHEMA_W32, None),
        ("junk.txt", 512, NOW_MS, None, None),
    ])
    seed_observation(db, run_stock, "Reasons_Catalog", 1, [
        ("SL_NWD.xls", 2 * 1048576, NOW_MS - 40 * DAY_MS, SCHEMA_SL, None),
    ])
    # The legacy generator reads the same folder five minutes later — the
    # shared-folder case, and the one that makes `*` count three files.
    run_ops = seed_run(db, d_ops, gid_ops, started_at=NOW + timedelta(minutes=5))
    seed_observation(db, run_ops, "Stock_RDC", 0, [
        ("Stock_w31.xlsx", 10 * 1048576, NOW_MS - DAY_MS, None, None),
        ("Stock_w32.xlsx", 20 * 1048576, NOW_MS, None, None),
        ("junk.txt", 512, NOW_MS, None, None),
    ], created_at=NOW + timedelta(minutes=5))
    # The agent-engine report: constant input_manifest, no observations ever.
    seed_run(db, d_gp, gid_dwh, started_at=NOW)
    db.commit()
    db.close()

    from app.services import lineage
    db = SessionLocal()
    dashboards = db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all()
    graph = lineage.build_graph(db, dashboards)
    db.close()

    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = graph["edges"]
    by_kind = {}
    for n in graph["nodes"]:
        by_kind.setdefault(n["kind"], []).append(n)

    # ── ids and structure ───────────────────────────────────────────────
    check("every node id follows the scheme",
          all(ID_RE.match(i) for i in nodes), str([i for i in nodes if not ID_RE.match(i)]))
    check("no duplicate node ids", len(nodes) == len(graph["nodes"]))
    dangling = [e for e in edges if e["from"] not in nodes or e["to"] not in nodes]
    check("every edge endpoint exists", not dangling, str(dangling[:3]))
    check("edge kinds are the declared vocabulary",
          {e["kind"] for e in edges} <= {"matches", "contains", "reads", "writes",
                                         "writes_back", "publishes",
                                         "read_order", "feeds"},
          str({e["kind"] for e in edges}))
    check("layer matches kind",
          all(n["layer"] == LAYER_BY_KIND[n["kind"]] for n in graph["nodes"]),
          str([(n["id"], n["kind"], n["layer"]) for n in graph["nodes"]
               if n["layer"] != LAYER_BY_KIND.get(n["kind"])][:3]))
    # `feeds` is the one edge WITHIN a band (stage -> stage), `writes_back`
    # the one that points backwards; everything else climbs exactly one band.
    bad_layers = [e for e in edges
                  if e["kind"] != "feeds"
                  and (nodes[e["to"]]["layer"] <= nodes[e["from"]]["layer"])
                  != (e["kind"] == "writes_back")]
    check("layers increase along every edge except writes_back", not bad_layers,
          str(bad_layers[:3]))
    bad_feeds = [e for e in edges if e["kind"] == "feeds"
                 and not (nodes[e["from"]]["kind"] == nodes[e["to"]]["kind"] == "stage")]
    check("`feeds` only ever joins two stages", not bad_feeds, str(bad_feeds[:3]))

    # ── layer 0/1: sources and patterns ─────────────────────────────────
    src_stock = next(n for n in by_kind["folder"] if n["data"]["key_name"] == "Stock_RDC")
    src_reasons = next(n for n in by_kind["folder"] if n["data"]["key_name"] == "Reasons_Catalog")
    check("shared folder flagged", src_stock["data"]["shared"] is True)
    check("folder volume comes from datasources.build (latest observation)",
          src_stock["data"]["files"] == 3, str(src_stock["data"]["files"]))
    check("stale reference folder flagged data_stale",
          src_reasons["data"]["data_stale"] is True, str(src_reasons["data"]["data_age_days"]))

    pat_stock = nodes.get("pat:gen-rdc:Stock_*.xlsx")
    pat_sl = nodes.get("pat:gen-rdc:SL_NWD.xls")
    pat_legacy = nodes.get("pat:gen-ops:*")
    check("declared pattern node present", pat_stock is not None, str(sorted(nodes)))
    check("required_files sugar became a reference pattern",
          pat_sl is not None and pat_sl["data"]["role"] == "reference"
          and pat_sl["data"]["required"] is True and pat_sl["data"]["from_required_files"] is True,
          str(pat_sl))
    check("legacy generator gets the synthetic '*' pattern",
          pat_legacy is not None and pat_legacy["data"].get("legacy") is True, str(pat_legacy))
    check("required_files-only manifest keeps the required pattern next to '*'",
          nodes.get("pat:gen-ops:Stock_w31.xlsx") is not None
          and nodes["pat:gen-ops:Stock_w31.xlsx"]["data"].get("from_required_files") is True,
          str([k for k in nodes if k.startswith("pat:gen-ops")]))
    check("legacy generator node says so",
          nodes["gen:gen-ops"]["data"].get("legacy_inputs") is True,
          str(nodes["gen:gen-ops"]["data"]))
    check("matched_files counts only the files the pattern admits",
          pat_stock["data"]["matched_files"] == 2, str(pat_stock["data"]["matched_files"]))
    check("reference pattern matched its one file",
          pat_sl["data"]["matched_files"] == 1, str(pat_sl["data"]["matched_files"]))
    check("legacy '*' admits everything the required pattern did not claim",
          pat_legacy["data"]["matched_files"] == 2, str(pat_legacy["data"]["matched_files"]))
    check("required pattern of a legacy manifest claims its own file",
          nodes["pat:gen-ops:Stock_w31.xlsx"]["data"]["matched_files"] == 1)
    check("pattern wired to the folder that holds its files",
          {"from": src_stock["id"], "to": pat_stock["id"], "kind": "matches"} in edges)
    check("reference pattern wired to the reference folder",
          {"from": src_reasons["id"], "to": pat_sl["id"], "kind": "matches"} in edges)
    check("undeclared extra sheet is a diff line, not a node",
          pat_stock["data"].get("diff", {}).get("extra_sheets") == ["Service"],
          str(pat_stock["data"].get("diff")))

    # ── layer 2: sheets and the column tri-state ────────────────────────
    sheet = nodes.get("sheet:gen-rdc:Stock_*.xlsx:Warehouse")
    check("declared sheet node present", sheet is not None, str(sorted(nodes)))
    cols = {col["name"]: col for col in sheet["data"]["columns"]}
    check("declared+observed+consumed column",
          cols["SKU"] == {"name": "SKU", "declared": True, "observed": True,
                              "consumed": True}, str(cols.get("SKU")))
    check("declared but never observed -> consumed is null, not False",
          cols["Reason"]["declared"] is True and cols["Reason"]["observed"] is False
          and cols["Reason"]["consumed"] is None, str(cols.get("Reason")))
    # The hooks recorded exactly which columns of this sheet were read, so a
    # column that is not among them was demonstrably not read — False, not
    # null, whether or not the author declared it.
    check("observed, undeclared, not read -> consumed False",
          cols["Qty new"]["declared"] is False and cols["Qty new"]["observed"] is True
          and cols["Qty new"]["consumed"] is False, str(cols.get("Qty new")))
    check("sheet diff lists the missing and the extra column",
          sheet["data"]["diff"] == {"missing_columns": ["Reason"],
                                    "extra_columns": ["Qty new"]},
          str(sheet["data"]["diff"]))
    check("observed row count carried", sheet["data"]["observed_rows"] == 184000,
          str(sheet["data"]["observed_rows"]))
    check("sheet reads the generator",
          {"from": sheet["id"], "to": "gen:gen-rdc", "kind": "reads"} in edges)
    check("pattern contains the sheet",
          {"from": pat_stock["id"], "to": sheet["id"], "kind": "contains"} in edges)
    # A pattern that declares NO sheets shows the ones actually observed.
    check("undeclared pattern surfaces its observed sheet as a node",
          "sheet:gen-rdc:SL_NWD.xls:Reasons" in nodes, str(sorted(nodes)))
    check("a sheetless pattern reads the generator directly",
          {"from": pat_legacy["id"], "to": "gen:gen-ops", "kind": "reads"} in edges)

    # ── layers 3/4/5 ────────────────────────────────────────────────────
    check("generator node carries version and engine",
          nodes["gen:gen-rdc"]["data"]["version"] == 1
          and nodes["gen:gen-rdc"]["data"]["engine"] == "browser", str(nodes["gen:gen-rdc"]))
    check("html output always present", "out:gen-rdc:html" in nodes)
    check("declared slot becomes an output node",
          nodes.get("out:gen-rdc:0", {}).get("label") == "stock.bin.gz", str(nodes.get("out:gen-rdc:0")))
    wb_id = "out:gen-rdc:wb:sl_unassigned_reasons.xlsx"
    check("write_back output node from the run's outputs_json", wb_id in nodes, str(sorted(nodes)))
    check("write_back edge points back at the source folder",
          {"from": wb_id, "to": src_stock["id"], "kind": "writes_back"} in edges, str(edges))
    check("outputs publish into the report",
          {"from": "out:gen-rdc:html", "to": f"rep:{d_stock}", "kind": "publishes"} in edges)
    check("write_back does NOT publish",
          not any(e["from"] == wb_id and e["kind"] == "publishes" for e in edges))
    check("report node carries status and engine",
          nodes[f"rep:{d_stock}"]["data"]["status"] == "success"
          and nodes[f"rep:{d_stock}"]["data"]["dashboard_id"] == d_stock,
          str(nodes[f"rep:{d_stock}"]["data"]))

    # ── GreenPlum: straight into the generator, no pattern in between ────
    gp = by_kind.get("greenplum") or []
    check("greenplum source present", len(gp) == 1, str(gp))
    if gp:
        check("greenplum reads the generator directly",
              {"from": gp[0]["id"], "to": "gen:gen-dwh", "kind": "reads"} in edges, str(edges))
        check("greenplum carries its queries",
              gp[0]["data"]["queries"] == ["sql/rejections.sql"], str(gp[0]["data"]))
        check("no pattern node hangs off the agent generator",
              not any(n["id"].startswith("pat:gen-dwh:") for n in graph["nodes"]),
              str([n["id"] for n in graph["nodes"] if n["id"].startswith("pat:gen-dwh")]))

    check("meta carries the window and the threshold",
          set(graph["meta"]) >= {"generated_at", "runs_considered", "stale_days"}
          and graph["meta"]["runs_considered"] == 3, str(graph["meta"]))

    # ── The bound: sheets go first, the spine survives ──────────────────
    orig_max = lineage.MAX_NODES
    lineage.MAX_NODES = 1
    try:
        db = SessionLocal()
        small = lineage.build_graph(db, db.query(Dashboard)
                                    .filter(Dashboard.generator_id.isnot(None)).all())
        db.close()
    finally:
        lineage.MAX_NODES = orig_max
    small_ids = {n["id"] for n in small["nodes"]}
    check("over the node cap: meta says truncated", small["meta"].get("truncated") is True,
          str(small["meta"]))
    check("over the node cap: sheet nodes are the ones dropped",
          not any(n["kind"] == "sheet" for n in small["nodes"])
          and "gen:gen-rdc" in small_ids and src_stock["id"] in small_ids,
          str(sorted(small_ids)))
    check("over the node cap: the pattern reconnects to its generator",
          {"from": pat_stock["id"], "to": "gen:gen-rdc", "kind": "reads"} in small["edges"],
          str(small["edges"]))
    check("over the node cap: no edge dangles",
          not [e for e in small["edges"]
               if e["from"] not in small_ids or e["to"] not in small_ids],
          str([e for e in small["edges"]
               if e["from"] not in small_ids or e["to"] not in small_ids][:3]))

    # ── The fixture the UI is built against pins the keys ────────────────
    with open(FIXTURE, encoding="utf-8") as fh:
        fixture = json.load(fh)
    fx_by_kind = {}
    for n in fixture["nodes"]:
        fx_by_kind.setdefault(n["kind"], set()).update(n["data"])
    for kind, want in sorted(fx_by_kind.items()):
        got = set()
        for n in by_kind.get(kind, []):
            got.update(n["data"])
        check(f"fixture keys for kind {kind!r} are produced", want <= got,
              f"missing {sorted(want - got)}")
    fx_cols = set()
    for n in fixture["nodes"]:
        for col in (n["data"].get("columns") or []):
            fx_cols.update(col)
    got_cols = {k for n in by_kind.get("sheet", []) for col in n["data"]["columns"] for k in col}
    check("fixture column-entry keys are produced", fx_cols <= got_cols,
          f"missing {sorted(fx_cols - got_cols)}")
    check("fixture node envelope keys are produced",
          set(fixture["nodes"][0]) <= set(graph["nodes"][0]), str(sorted(graph["nodes"][0])))
    check("fixture edge keys are produced",
          set(fixture["edges"][0]) <= set(edges[0]), str(sorted(edges[0])))
    check("fixture meta keys are produced",
          set(fixture["meta"]) <= set(graph["meta"]), str(sorted(graph["meta"])))

    # ── The endpoint ────────────────────────────────────────────────────
    r = c.get("/api/data-sources/lineage")
    check("endpoint -> 200 for an admin", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    if r.status_code == 200:
        payload = r.json()
        check("endpoint returns the same node set",
              {n["id"] for n in payload["nodes"]} == set(nodes),
              str(sorted({n["id"] for n in payload["nodes"]} ^ set(nodes))[:5]))

    from app.models import User
    db = SessionLocal()
    db.add(User(person_id="anna", display_name="Anna", role="user", is_active=True))
    db.commit()
    db.close()
    c2 = TestClient(app)
    login(c2, "anna")
    r = c2.get("/api/data-sources/lineage")
    check("endpoint -> 200 for a user with no reach", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        strangers = r.json()
        rep_ids = {n["id"] for n in strangers["nodes"] if n["kind"] == "report"}
        check("a stranger sees no report node they cannot run",
              rep_ids == set(), str(sorted(rep_ids)))
        check("nor any source behind one",
              not any(n["kind"] in ("folder", "greenplum") for n in strangers["nodes"]),
              str([n["id"] for n in strangers["nodes"]][:5]))

    chain_cases(c)
    export_cases(c)
    export_unit_cases()
    stage_cases(c)
    schema_reason_cases(c)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


M_STG = {
    "name": "gen-stg", "version": "1.0", "engine": "browser",
    "entrypoint": {"module": "gen.main"},
    "outputs": {"html": True, "slots": []},
    "inputs": [
        {"match": "Sales_*.xlsx", "role": "data", "required": False,
         "sheets": [{"name": "Data", "columns": ["SKU", "Amount"]}]},
        {"match": "Ref.xls", "role": "reference", "required": False, "sheets": []},
        {"match": "Other_*.xlsx", "role": "data", "required": False, "sheets": []},
    ],
}

SCHEMA_SALES = {"sheets": [{"name": "Data", "header": ["SKU", "Amount"],
                            "rows": 1000, "truncated": False}],
                "truncated": False, "error": None}
SCHEMA_REF = {"sheets": [{"name": "Lookup", "header": ["Code", "Name"],
                          "rows": 7, "truncated": False}],
              "truncated": False, "error": None}
SCHEMA_OTHER = {"sheets": [{"name": "Other", "header": ["X"], "rows": 2, "truncated": False}],
                "truncated": False, "error": None}


def seed_stages(db, run, rows):
    """rows: [(order, name, rows, columns, inputs|None, read_files)]."""
    from app.models import RunStage
    for order, name, nrows, columns, inputs, read_files in rows:
        db.add(RunStage(
            run_id=run.id, order=order, name=name, rows=nrows,
            columns_json=json.dumps(columns, ensure_ascii=False),
            profiles_json=json.dumps([], ensure_ascii=False),
            inputs_json=None if inputs is None else json.dumps(inputs, ensure_ascii=False),
            read_files_json=json.dumps(read_files, ensure_ascii=False),
            ms=3))
    db.commit()


def stage_cases(c):
    """P4.1 — the generator node expands into the chain its own code narrated.

    The shape under test is the one the contract fixes: a file reaches the
    STAGE that read it (dashed `read_order`), stages feed each other in
    declared or call order, the terminal stage feeds the generator, and a
    file no stage claimed keeps its old edge straight into the generator.
    """
    from app.database import SessionLocal
    from app.models import Dashboard
    from app.services import lineage, lineage_export

    gid, dash = register(c, M_STG, "Staged Sales")
    db = SessionLocal()
    run = seed_run(db, dash, gid, started_at=NOW)
    seed_observation(db, run, "Sales_Folder", 0, [
        ("Sales_1.xlsx", 1048576, NOW_MS, SCHEMA_SALES, None),
        ("Ref.xls", 4096, NOW_MS, SCHEMA_REF, None),
        ("Other_9.xlsx", 8192, NOW_MS, SCHEMA_OTHER, None),
    ])
    seed_stages(db, run, [
        # A file-reading first stage with no declared inputs...
        (0, "raw", 1000, ["SKU", "Amount"], None, ["Sales_1.xlsx"]),
        # ...a second one that declares EXPLICITLY that it has no upstream
        # (an [] that must not fall back to "the previous stage")...
        (1, "reference", 7, ["Code", "Name"], [], ["Ref.xls"]),
        # ...and the join that consumes both.
        (2, "joined", 1000, ["SKU", "Name"], ["raw", "reference"], []),
    ])
    # A LATER run of the same generator that reported no stages at all: the
    # graph must keep showing the last pipeline it knows, not blank it.
    seed_run(db, dash, gid, started_at=NOW + timedelta(minutes=10))
    db.commit()

    dashboards = db.query(Dashboard).filter(Dashboard.generator_id.isnot(None)).all()
    graph = lineage.build_graph(db, dashboards)
    db.close()

    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = {(e["from"], e["to"]): e["kind"] for e in graph["edges"]}
    s0, s1, s2 = (f"stage:{gid}:{i}" for i in range(3))
    gen_id = "gen:gen-stg"

    check("a stage node per call, keyed by generator id and order",
          all(i in nodes for i in (s0, s1, s2)),
          str(sorted(i for i in nodes if i.startswith("stage:"))))
    if s0 in nodes:
        check("stage node carries label, order, rows, columns and inputs",
              nodes[s0]["label"] == "raw" and nodes[s0]["kind"] == "stage"
              and nodes[s0]["layer"] == lineage.LAYER_STAGE
              and nodes[s0]["data"]["order"] == 0 and nodes[s0]["data"]["rows"] == 1000
              and nodes[s0]["data"]["columns"] == ["SKU", "Amount"]
              and nodes[s0]["data"]["inputs"] is None,
              str(nodes.get(s0)))
        check("the generator counts its stages", nodes[gen_id]["data"].get("stages") == 3,
              str(nodes[gen_id]["data"]))

    sheet_sales = f"sheet:gen-stg:Sales_*.xlsx:Data"
    sheet_ref = f"sheet:gen-stg:Ref.xls:Lookup"
    sheet_other = f"sheet:gen-stg:Other_*.xlsx:Other"
    check("the sheet of a read file feeds the STAGE that read it, dashed",
          edges.get((sheet_sales, s0)) == lineage.EDGE_READ_ORDER, str(edges.get((sheet_sales, s0))))
    check("...and no longer feeds the generator directly",
          (sheet_sales, gen_id) not in edges, str([k for k in edges if k[0] == sheet_sales]))
    check("an undeclared observed sheet feeds its stage too",
          edges.get((sheet_ref, s1)) == lineage.EDGE_READ_ORDER, str([k for k in edges if k[0] == sheet_ref]))
    check("a file no stage claimed still reads straight into the generator",
          edges.get((sheet_other, gen_id)) == "reads", str([k for k in edges if k[0] == sheet_other]))

    check("declared inputs become stage->stage edges",
          edges.get((s0, s2)) == lineage.EDGE_FEEDS and edges.get((s1, s2)) == lineage.EDGE_FEEDS,
          str([k for k in edges if k[1] == s2]))
    check("an explicit empty `inputs` does NOT fall back to the previous stage",
          (s0, s1) not in edges, str([k for k in edges if k[1] == s1]))
    check("only the terminal stage feeds the generator",
          edges.get((s2, gen_id)) == "reads" and (s0, gen_id) not in edges
          and (s1, gen_id) not in edges, str([k for k in edges if k[1] == gen_id]))

    # ── The graph still reads as a whole: export walks THROUGH the stages ──
    md = lineage_export.to_markdown(graph)
    check("export md still reaches the generator behind its stages",
          "#### → gen-stg" in md, md[:400])
    check("export md lists the stages in call order",
          md.index("**raw**") < md.index("**reference**") < md.index("**joined**"),
          str([x for x in md.splitlines() if "stage " in x]))
    check("export md names a stage's columns and the files it read",
          "SKU, Amount" in md and "reads Sales_1.xlsx" in md,
          str([x for x in md.splitlines() if "raw" in x]))

    mm = lineage_export.to_mermaid(graph)
    check("export mermaid emits an entity per stage",
          all(re.search(r"^\s{4}" + name + r"\s*\{", mm, re.M) for name in ("raw", "reference", "joined")),
          str([x for x in mm.splitlines() if "{" in x]))
    check("export mermaid wires stages with feeds/builds",
          " : feeds" in mm and " : builds" in mm,
          str([x for x in mm.splitlines() if "||--" in x]))
    entity_open = len(re.findall(r"^\s{4}\S+\s*\{\s*$", mm, re.M))
    entity_close = len(re.findall(r"^\s{4}\}\s*$", mm, re.M))
    check("export mermaid braces stay balanced with stages in the graph",
          entity_open == entity_close, f"{entity_open} opens vs {entity_close} closes")

    # ── The focus closure keeps a stage's whole neighbourhood ─────────────
    scoped = lineage_export.subgraph(graph, s2)
    scoped_ids = {n["id"] for n in scoped["nodes"]}
    check("focusing a stage reaches its upstream stages and the generator",
          {s0, s1, s2, gen_id} <= scoped_ids, str(sorted(scoped_ids)))

    r = c.get(f"/api/data-sources/lineage?format=json&focus={s2}")
    check("the endpoint serves a stage-focused subgraph",
          r.status_code == 200 and {n["id"] for n in r.json()["nodes"]} == scoped_ids,
          f"{r.status_code} {r.text[:200]}")


def chain_cases(c):
    """collect_chain_files: every level of a 3-run chain resolves its schema.

    Run 1 (oldest) sniffs A and B; run 2 sees A unchanged (NULL) and B gone;
    run 3 sees A unchanged again, B back with a NEW header, and a legacy row.
    """
    from app.database import SessionLocal
    from app.models import Dashboard, SourceFile
    from app.services import lineage

    m = dict(M_RDC, name="gen-chain", inputs=[{
        "match": "Chain_*.xlsx", "role": "data", "required": False,
        "sheets": [{"name": "Warehouse", "columns": ["SKU"]}],
    }])
    m.pop("required_files", None)
    gid, dash_id = register(c, m, "Chain Report")
    schema_b2 = {"sheets": [{"name": "Warehouse", "header": ["SKU", "New"],
                             "rows": 3, "truncated": False}], "truncated": False, "error": None}
    db = SessionLocal()
    t0 = NOW - timedelta(hours=3)
    r1 = seed_run(db, dash_id, gid, started_at=t0)
    seed_observation(db, r1, "Chain_Dir", 0, [
        ("Chain_A.xlsx", 100, NOW_MS - DAY_MS, SCHEMA_W31, CONSUMED_W31),
        ("Chain_B.xlsx", 100, NOW_MS - DAY_MS, SCHEMA_SL, None),
    ], created_at=t0)
    r2 = seed_run(db, dash_id, gid, started_at=t0 + timedelta(hours=1))
    _, obs2 = seed_observation(db, r2, "Chain_Dir", 0, [
        ("Chain_A.xlsx", 100, NOW_MS - DAY_MS, None, None),
    ], created_at=t0 + timedelta(hours=1))
    r3 = seed_run(db, dash_id, gid, started_at=t0 + timedelta(hours=2))
    _ds, obs3 = seed_observation(db, r3, "Chain_Dir", 0, [
        ("Chain_A.xlsx", 100, NOW_MS - DAY_MS, None, None),
        ("Chain_B.xlsx", 100, NOW_MS, schema_b2, None),
    ], created_at=t0 + timedelta(hours=2))
    db.add(SourceFile(observation_id=obs3.id, name="old.xls", size=1, mtime_ms=NOW_MS,
                      kind="legacy", schema_json=None))
    # A legacy row WITH a schema at run 2 must not become a donor for the
    # same-named non-legacy file of run 3 (NULL schema, no older sniff).
    db.add(SourceFile(observation_id=obs2.id, name="Chain_L.xlsx", size=1, mtime_ms=NOW_MS,
                      kind="legacy", schema_json=json.dumps(SCHEMA_SL)))
    db.add(SourceFile(observation_id=obs3.id, name="Chain_L.xlsx", size=1, mtime_ms=NOW_MS,
                      kind="data", schema_json=None))
    db.commit()

    dashboards = db.query(Dashboard).filter(Dashboard.id == dash_id).all()
    runs = lineage.datasources._recent_runs(db, [dash_id], limit=10)
    check("_recent_runs(limit=) widens the window", len(runs.get(dash_id, [])) == 3,
          str(len(runs.get(dash_id, []))))
    check("_recent_runs default limit still _RUNS_PER_DASH",
          len(lineage.datasources._recent_runs(db, [dash_id]).get(dash_id, []))
          == min(3, lineage.datasources._RUNS_PER_DASH))
    chains = lineage._collect_observations(db, dashboards, runs, {dash_id: "gen-chain"})
    key = ("gen-chain", _ds.id)
    levels = lineage.collect_chain_files(db, chains)[key]
    check("chain has three levels, newest first", len(levels) == 3
          and [l["obs"].run_id for l in levels] == [r3.id, r2.id, r1.id])
    by_lvl = [{f["name"]: f for f in l["files"]} for l in levels]
    check("file A resolves on all three levels from the oldest sniff",
          all(by_lvl[i]["Chain_A.xlsx"]["schema"] == SCHEMA_W31 for i in range(3)),
          str([by_lvl[i]["Chain_A.xlsx"]["schema"] for i in range(3)]))
    check("a file absent mid-chain does not leak into the newer level",
          "Chain_B.xlsx" not in by_lvl[1]
          and by_lvl[0]["Chain_B.xlsx"]["schema"] == schema_b2)
    check("legacy rows are not backfilled", by_lvl[0]["old.xls"]["schema"] is None)
    check("file dicts carry size/mtime_ms/source_file_id",
          {"size", "mtime_ms", "source_file_id"} <= set(by_lvl[0]["Chain_A.xlsx"])
          and by_lvl[0]["Chain_A.xlsx"]["source_file_id"] is not None)
    check("depth= truncates the chain", len(lineage.collect_chain_files(db, chains, depth=2)[key]) == 2)
    check("a legacy row with a schema is not a donor",
          by_lvl[0]["Chain_L.xlsx"]["schema"] is None)
    flat = lineage._collect_files(db, chains)[key]
    check("_collect_files is the newest level", flat == levels[0]["files"])

    # The row cap must never touch the newest level of ANY chain on the page:
    # a second dashboard with a lower observation id competes for the cap.
    m2 = dict(m, name="gen-chain2")
    gid2, dash_id2 = register(c, m2, "Chain Report 2")
    r0 = seed_run(db, dash_id2, gid2, started_at=t0 - timedelta(hours=1))
    _ds2, _obs0 = seed_observation(db, r0, "Chain_Dir2", 0, [
        (f"Chain_{i}.xlsx", 100, NOW_MS, SCHEMA_SL, None) for i in range(4)
    ], created_at=t0 - timedelta(hours=1))
    db.commit()
    dashboards2 = db.query(Dashboard).filter(Dashboard.id.in_([dash_id, dash_id2])).all()
    runs2 = lineage.datasources._recent_runs(db, [dash_id, dash_id2], limit=10)
    chains2 = lineage._collect_observations(db, dashboards2, runs2,
                                            {dash_id: "gen-chain", dash_id2: "gen-chain2"})
    saved = lineage._MAX_BACKFILL_ROWS
    lineage._MAX_BACKFILL_ROWS = 1
    try:
        capped = lineage.collect_chain_files(db, chains2)
    finally:
        lineage._MAX_BACKFILL_ROWS = saved
    check("row cap leaves the newest level of every chain complete",
          len(capped[("gen-chain2", _ds2.id)][0]["files"]) == 4
          and len(capped[key][0]["files"]) == 4,
          str({k: len(v[0]["files"]) for k, v in capped.items()}))
    check("row cap only trims older levels",
          sum(len(l["files"]) for l in capped[key][1:]) == 1)
    db.close()


def export_cases(c):
    """GET /api/data-sources/lineage?format=... — P3.5.

    md/mermaid for a focused node must carry every label the focus's closure
    reaches (per lineage_export.subgraph, the same reach() the page itself
    computes) and NONE of the labels that belong only to a DIFFERENT,
    disjoint closure (gen-dwh's greenplum branch never touches Stock_RDC,
    so "RDC Stock Health" etc. must not leak into a gen-dwh export).
    """
    from app.database import SessionLocal
    from app.models import QualityResult, SourceFile, SourceObservation
    from app.services import lineage_export

    # A profile row on an already-observed Stock file (sheet/column matching
    # M_RDC's declared "Warehouse"/"SKU"), so the markdown table has
    # something real to surface.
    db = SessionLocal()
    sf = (db.query(SourceFile)
          .filter(SourceFile.name == "Stock_w31.xlsx")
          .order_by(SourceFile.id.asc())
          .first())
    if sf is not None:
        obs = db.get(SourceObservation, sf.observation_id)
        db.add(QualityResult(
            source_file_id=sf.id, observation_id=obs.id,
            check_id="profile:Warehouse:SKU", check_type="profile",
            sheet="Warehouse", column="SKU", status="pass",
            metric_json=json.dumps({"type": "string", "nulls": 0, "distinct": 3,
                                    "top": [["A1", 2], ["A2", 1]]}, ensure_ascii=False)))
        db.commit()
    db.close()

    r_no_format = c.get("/api/data-sources/lineage")
    graph = r_no_format.json()
    r_json = c.get("/api/data-sources/lineage?format=json")
    check("export: format=json unchanged without focus",
          [n["id"] for n in r_json.json()["nodes"]] == [n["id"] for n in graph["nodes"]]
          and r_json.json()["edges"] == graph["edges"],
          str(r_json.status_code))

    r_bad = c.get("/api/data-sources/lineage?format=xml")
    check("export: unknown format -> 422", r_bad.status_code == 422, str(r_bad.status_code))

    gen_dwh_id = "gen:gen-dwh"
    check("export: dwh generator node present in fixture graph",
          gen_dwh_id in {n["id"] for n in graph["nodes"]}, str(sorted(n["id"] for n in graph["nodes"])))

    scoped = lineage_export.subgraph(graph, gen_dwh_id)
    scoped_labels = {n["label"] for n in scoped["nodes"]}
    outside_labels = {n["label"] for n in graph["nodes"]} - scoped_labels

    r_md = c.get(f"/api/data-sources/lineage?format=md&focus={gen_dwh_id}")
    check("export: md focused -> 200 text/markdown",
          r_md.status_code == 200 and r_md.headers["content-type"].startswith("text/markdown"),
          f"{r_md.status_code} {r_md.headers.get('content-type')}")
    check("export: md has a downloadable attachment filename",
          "attachment" in r_md.headers.get("content-disposition", "")
          and r_md.headers["content-disposition"].endswith('.md"'),
          r_md.headers.get("content-disposition"))
    md = r_md.text
    check("export: md contains every label of the focus closure",
          all(lbl in md for lbl in scoped_labels), str([l for l in scoped_labels if l not in md]))
    # Single/short glyphs (the legacy pattern's literal "*") are not a
    # meaningful "label leaked" signal — they collide with Markdown's own
    # syntax (`**bold**`). Only real names count.
    leaked = [lbl for lbl in outside_labels if len(lbl) > 2 and lbl in md]
    check("export: md contains no label from outside the closure", not leaked, str(leaked))

    gen_rdc_id = "gen:gen-rdc"
    r_md2 = c.get(f"/api/data-sources/lineage?format=md&focus={gen_rdc_id}")
    check("export: md surfaces the column profile's top values",
          "A1 (2), A2 (1)" in r_md2.text, r_md2.text[:1500])

    r_mm = c.get(f"/api/data-sources/lineage?format=mermaid&focus={gen_dwh_id}")
    check("export: mermaid focused -> 200 text/plain",
          r_mm.status_code == 200 and r_mm.headers["content-type"].startswith("text/plain"),
          f"{r_mm.status_code} {r_mm.headers.get('content-type')}")
    check("export: mermaid has a downloadable .mmd attachment filename",
          "attachment" in r_mm.headers.get("content-disposition", "")
          and r_mm.headers["content-disposition"].endswith('.mmd"'),
          r_mm.headers.get("content-disposition"))
    mm = r_mm.text
    check("export: mermaid starts with erDiagram", mm.startswith("erDiagram"))
    # Only entity blocks are brace pairs — a relationship line's `||--o{` is
    # crow's-foot notation, a lone `{` with no closing brace by design.
    entity_open = len(re.findall(r"^\s{4}\S+\s*\{\s*$", mm, re.M))
    entity_close = len(re.findall(r"^\s{4}\}\s*$", mm, re.M))
    check("export: mermaid entity braces are balanced", entity_open == entity_close,
          f"{entity_open} opens vs {entity_close} closes")
    entity_ids = re.findall(r"^\s{4}(\S+)\s*\{", mm, re.M)
    rel_ids = [x for pair in re.findall(r"^\s{4}(\S+)\s*\|\|--o\{\s*(\S+)\s*:", mm, re.M) for x in pair]
    check("export: mermaid entity/relationship ids carry no raw whitespace",
          all(not re.search(r"\s", tok) for tok in entity_ids + rel_ids),
          str([t for t in entity_ids + rel_ids if re.search(r"\s", t)]))

    # An EXPLICIT format=json is the Export menu's JSON option, not the
    # page's own bare fetch — it has to download too (fix round: it used to
    # navigate the tab to raw JSON with no Content-Disposition at all).
    check("export: explicit format=json also downloads",
          "attachment" in r_json.headers.get("content-disposition", "")
          and r_json.headers["content-disposition"].endswith('.json"'),
          r_json.headers.get("content-disposition"))
    check("export: bare fetch (no format=) carries no download header",
          "content-disposition" not in r_no_format.headers,
          str(dict(r_no_format.headers)))


def export_unit_cases():
    """lineage_export in isolation, against small synthetic graphs — cheaper
    and more targeted than round-tripping the full fixture for bugs that are
    about the export functions' own logic, not about lineage.build_graph."""
    from app.services import lineage_export

    # -- glob declared sheet name vs. physical profiled sheet name --------
    # A declared sheet is often a glob ("*" by default, or an author's
    # "Warehouse*"), but QualityResult.sheet is always the PHYSICAL sheet name
    # the engine profiled. Before the fix, to_markdown/to_mermaid keyed the
    # lookup by norm(declared_label) and so never found a profile for any
    # manifest whose declared name was not the literal sheet name.
    graph = {
        "nodes": [
            {"id": "src:folder:1", "layer": 0, "kind": "folder", "label": "Stock",
             "data": {"declared_path": "/stock"}},
            {"id": "pat:1", "layer": 1, "kind": "pattern", "label": "Stock_*.xlsx", "data": {}},
            {"id": "sheet:1", "layer": 2, "kind": "sheet", "label": "Warehouse*",
             "data": {"columns": [{"name": "SKU", "declared": True, "observed": True}]}},
            {"id": "gen:1", "layer": 3, "kind": "generator", "label": "gen-rdc", "data": {}},
        ],
        "edges": [
            {"from": "src:folder:1", "to": "pat:1", "kind": "contains"},
            {"from": "pat:1", "to": "sheet:1", "kind": "contains"},
            {"from": "sheet:1", "to": "gen:1", "kind": "reads"},
        ],
        "meta": {},
    }
    profiles = {("gen-rdc", "Stock_*.xlsx", "Warehouse", "sku"):
                {"type": "number", "top": [["A1", 2], ["A2", 1]]}}
    md = lineage_export.to_markdown(graph, profiles=profiles)
    check("export unit: glob declared sheet name still finds the physical profile",
          "A1 (2), A2 (1)" in md, md)
    mm = lineage_export.to_mermaid(graph, profiles=profiles)
    # A "string" fallback type would mean the glob-vs-physical lookup MISSED
    # (unmatched columns default to "string"); "number" proves it hit.
    check("export unit: mermaid column typed from the glob-matched profile",
          re.search(r"\n\s+number \S+ ", mm) is not None, mm)

    # -- mermaid column typing through a stage node (P4.1) -----------------
    # to_markdown was fixed to walk THROUGH stage nodes via _generator_after
    # (sheet -> stage -> generator), but to_mermaid still used a one-hop
    # sheet->generator lookup and silently degraded every staged column to
    # "string". Same fixture as above, with a stage node spliced between
    # the sheet and the generator the way lineage.build_graph does once a
    # generator has stages.
    graph1b = {
        "nodes": [
            {"id": "src:folder:1", "layer": 0, "kind": "folder", "label": "Stock",
             "data": {"declared_path": "/stock"}},
            {"id": "pat:1", "layer": 1, "kind": "pattern", "label": "Stock_*.xlsx", "data": {}},
            {"id": "sheet:1", "layer": 2, "kind": "sheet", "label": "Warehouse*",
             "data": {"columns": [{"name": "SKU", "declared": True, "observed": True}]}},
            {"id": "stage:gen-rdc:0", "layer": 3, "kind": "stage", "label": "raw", "data": {}},
            {"id": "gen:1", "layer": 4, "kind": "generator", "label": "gen-rdc", "data": {}},
        ],
        "edges": [
            {"from": "src:folder:1", "to": "pat:1", "kind": "contains"},
            {"from": "pat:1", "to": "sheet:1", "kind": "contains"},
            {"from": "sheet:1", "to": "stage:gen-rdc:0", "kind": "read_order"},
            {"from": "stage:gen-rdc:0", "to": "gen:1", "kind": "builds"},
        ],
        "meta": {},
    }
    mm1b = lineage_export.to_mermaid(graph1b, profiles=profiles)
    check("export unit: mermaid column typed through a stage node, not degraded to string",
          re.search(r"\n\s+number \S+ ", mm1b) is not None, mm1b)

    # -- Markdown table cells with a literal "|" -----------------------
    graph2 = {
        "nodes": [
            {"id": "src:folder:2", "layer": 0, "kind": "folder", "label": "F", "data": {}},
            {"id": "pat:2", "layer": 1, "kind": "pattern", "label": "P*.xlsx", "data": {}},
            {"id": "sheet:2", "layer": 2, "kind": "sheet", "label": "S",
             "data": {"columns": [{"name": "A|B", "declared": True, "observed": True}]}},
            {"id": "gen:2", "layer": 3, "kind": "generator", "label": "g2", "data": {}},
        ],
        "edges": [
            {"from": "src:folder:2", "to": "pat:2", "kind": "contains"},
            {"from": "pat:2", "to": "sheet:2", "kind": "contains"},
            {"from": "sheet:2", "to": "gen:2", "kind": "reads"},
        ],
        "meta": {},
    }
    profiles2 = {("g2", "P*.xlsx", "S", "a|b"): {"type": "string", "top": [["X|Y", 1]]}}
    md2 = lineage_export.to_markdown(graph2, profiles=profiles2)
    md2_rows = [ln for ln in md2.splitlines() if ln.startswith("| ")]
    check("export unit: a literal '|' in a column name is escaped, not a new column",
          any("A\\|B" in ln for ln in md2_rows), md2)
    check("export unit: a literal '|' in a profile top-value is escaped too",
          "X\\|Y" in md2, md2)
    # Every data row must still have exactly 3 cells (4 UNESCAPED pipes,
    # i.e. not preceded by "\") once a literal "|" in the content is escaped.
    def unescaped_pipes(ln):
        return len(re.findall(r"(?<!\\)\|", ln))
    bad_rows = [ln for ln in md2_rows if unescaped_pipes(ln) != 4 and "column" not in ln and "---" not in ln]
    check("export unit: escaping keeps the table well-formed (no extra columns)",
          not bad_rows, str(bad_rows))

    # -- Mermaid identifiers for all-Cyrillic labels: transliterated, -------
    # not stripped to a bare "sheet"/"sheet_2".
    # Cyrillic on purpose (this block and the two below): exercises
    # transliteration of export ids.
    graph3 = {
        "nodes": [
            {"id": "s1", "layer": 2, "kind": "sheet", "label": "Склад", "data": {"columns": []}},
            {"id": "s2", "layer": 2, "kind": "sheet", "label": "Отчёт", "data": {"columns": []}},
        ],
        "edges": [],
        "meta": {},
    }
    mm3 = lineage_export.to_mermaid(graph3)
    ids3 = re.findall(r"^\s{4}(\S+)\s*\{", mm3, re.M)
    check("export unit: Cyrillic labels transliterate to readable Latin ids",
          ids3 == ["sklad", "otchet"], str(ids3))
    check("export unit: distinct Cyrillic labels get distinct, ASCII-only ids",
          len(set(ids3)) == len(ids3) and all(re.fullmatch(r"[A-Za-z0-9_]+", i) for i in ids3),
          str(ids3))
    check("export unit: the original Cyrillic label survives in the quoted _label",
          '"Склад"' in mm3 and '"Отчёт"' in mm3, mm3)

    # -- Mermaid identifiers: two DIFFERENT labels that transliterate to the
    # same base get a stable hash suffix, not a positional counter (a
    # counter would depend on iteration order, which shifts under a
    # ?focus= subgraph export; the hash is a pure function of the label).
    graph4 = {
        "nodes": [
            {"id": "g1", "layer": 3, "kind": "generator", "label": "Отчёт", "data": {}},
            {"id": "g2", "layer": 3, "kind": "generator", "label": "отчет", "data": {}},
        ],
        "edges": [],
        "meta": {},
    }
    mm4 = lineage_export.to_mermaid(graph4)
    ids4 = re.findall(r"^\s{4}(\S+)\s*\{", mm4, re.M)
    check("export unit: colliding transliterations get two distinct ids",
          len(ids4) == 2 and len(set(ids4)) == 2, str(ids4))
    check("export unit: the collision suffix is a stable hash of the label, "
          "not a positional counter",
          ids4[0] == "otchet" and re.fullmatch(r"otchet_[0-9a-f]{6}", ids4[1]), str(ids4))
    from app.services.lineage_export import _slug, _translit
    check("export unit: _translit is deterministic and keyed by content, not order",
          _slug("отчет", set()) == _slug("отчет", set()) == "otchet")
    check("export unit: _translit maps common Cyrillic letters GOST-like",
          _translit("Филиал") == "filial", _translit("Филиал"))


M_SCHEMA = {
    "name": "gen-schema", "version": "1.0", "engine": "browser",
    "entrypoint": {"module": "gen.main"},
    "outputs": {"html": True, "slots": []},
    "inputs": [{"match": "Bad_*.xlsx", "role": "data", "required": False,
               "sheets": [{"name": "Sheet1", "columns": []}]}],
}


def schema_reason_cases(c):
    """The per-sheet `reason` / top-level `error` commit 633eaaa put on
    schema_json (see static/py/_lineage.py sniff()) has to survive all the
    way to the sheet and pattern node data: a majority reason across the
    files that observed a sheet with no header, and a per-error file tally on
    the pattern that read them. Built as its own generator/dashboard so the
    rest of the fixture's counts (`runs_considered`, matched_files, ...) stay
    untouched."""
    from app.database import SessionLocal
    from app.models import Dashboard
    from app.services import lineage

    gid, dash = register(c, M_SCHEMA, "Schema Reasons Report")

    db = SessionLocal()
    run = seed_run(db, dash, gid, started_at=NOW)
    # Bad_1/Bad_1b: same reason on Sheet1 twice (the majority); Bad_3: a
    # different reason once, so the majority pick is deterministic and not
    # merely "the last one seen". Bad_2 never opened at all (sheets == []):
    # its top-level `error` has nothing to do with Sheet1's own reason and
    # must land on the PATTERN, not leak onto the sheet.
    seed_observation(db, run, "Bad_Folder", 0, [
        ("Bad_1.xlsx", 100, NOW_MS,
         {"sheets": [{"name": "Sheet1", "header": [], "header_row": None,
                     "rows": 5, "reason": "no header in first 20 rows"}], "error": None}, None),
        ("Bad_1b.xlsx", 100, NOW_MS,
         {"sheets": [{"name": "Sheet1", "header": [], "header_row": None,
                     "rows": 5, "reason": "no header in first 20 rows"}], "error": None}, None),
        ("Bad_3.xlsx", 100, NOW_MS,
         {"sheets": [{"name": "Sheet1", "header": [], "header_row": None,
                     "rows": 5, "reason": "sheet is empty"}], "error": None}, None),
        ("Bad_2.xlsx", 100, NOW_MS,
         {"sheets": [], "error": "workbook is corrupt"}, None),
    ])
    dashboards = db.query(Dashboard).filter(Dashboard.id == dash).all()
    graph = lineage.build_graph(db, dashboards)
    db.close()

    nodes = {n["id"]: n for n in graph["nodes"]}
    sheet = nodes.get("sheet:gen-schema:Bad_*.xlsx:Sheet1")
    check("a sheet whose header was never found gets header_missing",
          sheet is not None and sheet["data"].get("header_missing") is True, str(sheet))
    check("its reason is the majority across the files that reported one",
          sheet is not None and sheet["data"].get("reason") == "no header in first 20 rows",
          str(sheet and sheet["data"]))

    pat = nodes.get("pat:gen-schema:Bad_*.xlsx")
    check("a file that never opened at all tallies onto the pattern's schema_errors",
          pat is not None and pat["data"].get("schema_errors") == {"workbook is corrupt": 1},
          str(pat and pat["data"]))
    check("the unopenable file's error does not leak onto the sheet's own reason",
          sheet is not None and sheet["data"].get("reason") != "workbook is corrupt", str(sheet))


if __name__ == "__main__":
    main()
