"""Smoke test for Data Lineage v2, tasks A3 (ingest + observations endpoint),
A6 (input_overrides CRUD + manifest scoping) and A4 (schemadiff + outputs_json
on publish).

Runs against a throwaway SQLite DB + uploads dir, same convention as
tests/smoke_datasources.py. schemadiff.diff() is also exercised directly as a
pure function (no DB/HTTP involved) since that is its whole contract.
"""
import io
import json
import os
import sys
import tempfile
import time
import zipfile

TMP = tempfile.mkdtemp(prefix="op-obs-smoke-")
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
from app.services import schemadiff  # noqa: E402

FAILURES = []


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


DAY_MS = 86400_000
NOW_MS = int(time.time() * 1000)


def main():
    with TestClient(app) as c:
        body(c)


def body(c):
    login(c, "admin1")

    # ── Two reports, two generators, sharing a folder ──────────────────
    ids, gids = {}, {}
    for gen_name, title in (("gen-stock", "RDC Stock Health"), ("gen-ops", "OPS RDC Health")):
        r = c.post("/api/admin/generators",
                   files={"bundle": ("b.zip", make_bundle(gen_name), "application/zip")})
        assert r.status_code == 201, f"register {gen_name}: {r.status_code} {r.text[:300]}"
        gid = r.json()["id"]
        r = c.post("/api/dashboards/generated", json={"title": title, "generator_id": gid})
        assert r.status_code == 201, f"create {title}: {r.status_code} {r.text[:300]}"
        ids[title] = r.json()["id"]
        gids[title] = gid

    stock, ops = ids["RDC Stock Health"], ids["OPS RDC Health"]
    gid_stock, gid_ops = gids["RDC Stock Health"], gids["OPS RDC Health"]

    # ═══════════════════════════════════════════════════════════════════
    # A3 — size guard, ingest attribution, last_known, observations POST
    # ═══════════════════════════════════════════════════════════════════

    # ── size guard: too many folders -> 413, before any row is written ──
    r = c.post(f"/api/dashboards/{stock}/generator/runs", json={
        "engine": "browser",
        "input_manifest": {"dirs": [f"d{i}" for i in range(9)], "files": []},
    })
    check("9 folders -> 413", r.status_code == 413, f"{r.status_code} {r.text[:200]}")

    from app.database import SessionLocal
    from app.models import DataSource, GenerationRun, SourceFile, SourceObservation
    db = SessionLocal()
    check("413 left no DataSource rows behind", db.query(DataSource).count() == 0,
          str(db.query(DataSource).count()))
    db.close()

    # ── happy-path run start: attribution + fingerprint ─────────────────
    im1 = {
        "dirs": ["Stock_RDC", "Reasons_Catalog"],
        "files": [
            {"name": "w31.xlsx", "size": 10_000, "mtime_ms": NOW_MS - DAY_MS},
            {"name": "w32.xlsx", "size": 30_000, "mtime_ms": NOW_MS},
            {"name": "SL_NWD.xls", "size": 2_000, "mtime_ms": NOW_MS - 40 * DAY_MS, "dir": 1},
            {"name": "own.xlsx", "size": 99_000, "mtime_ms": NOW_MS, "aux": True},
        ],
    }
    r = c.post(f"/api/dashboards/{stock}/generator/runs",
               json={"engine": "browser", "input_manifest": im1})
    check("run start -> 201", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    start1 = r.json()
    run1 = start1["run_id"]
    check("observation_ids has both dirs",
          set(str(k) for k in start1["observation_ids"]) == {"0", "1"},
          str(start1["observation_ids"]))
    check("last_known empty on the very first run", start1["last_known"] == {}, str(start1["last_known"]))

    db = SessionLocal()
    ds_stock = db.query(DataSource).filter(DataSource.kind == "folder",
                                           DataSource.key_name == "Stock_RDC").first()
    ds_reasons = db.query(DataSource).filter(DataSource.kind == "folder",
                                             DataSource.key_name == "Reasons_Catalog").first()
    check("DataSource rows created for both folders", ds_stock is not None and ds_reasons is not None)
    check("first_seen_at stamped on create", ds_stock.first_seen_at is not None)

    obs0 = db.query(SourceObservation).filter(SourceObservation.run_id == run1,
                                              SourceObservation.data_source_id == ds_stock.id).first()
    obs1 = db.query(SourceObservation).filter(SourceObservation.run_id == run1,
                                              SourceObservation.data_source_id == ds_reasons.id).first()
    check("dir 0 (omitted) attributed 2 files, dir omitted read as index 0",
          obs0 is not None and obs0.file_count == 2, str(obs0 and obs0.file_count))
    check("dir 1 attributed 1 file", obs1 is not None and obs1.file_count == 1,
          str(obs1 and obs1.file_count))
    check("aux file excluded from every observation",
          obs0.total_bytes == 40_000 and obs1.total_bytes == 2_000,
          f"{obs0.total_bytes}, {obs1.total_bytes}")

    import hashlib
    expected_fp = hashlib.sha256("\n".join(sorted(["w31.xlsx".casefold(), "w32.xlsx".casefold()])).encode()).hexdigest()
    check("fingerprint = sha256 of sorted casefolded names", obs0.fingerprint == expected_fp,
          f"{obs0.fingerprint} != {expected_fp}")

    sf_names = {sf.name for sf in db.query(SourceFile).filter(SourceFile.observation_id == obs0.id).all()}
    check("SourceFile rows created kind=listed",
          sf_names == {"w31.xlsx", "w32.xlsx"}, str(sf_names))
    check("every SourceFile starts kind=listed",
          all(sf.kind == "listed" for sf in db.query(SourceFile)
              .filter(SourceFile.observation_id == obs0.id).all()))
    db.close()

    # Close run 1 so a second run can start (run-lock).
    r = c.post(f"/api/dashboards/{stock}/generator/runs/{run1}/error",
               json={"message": "smoke: closing run 1"})
    assert r.status_code == 200

    # ── last_known from the PREVIOUS observation ─────────────────────────
    im2 = {"dirs": ["Stock_RDC"], "files": [{"name": "w33.xlsx", "size": 5_000, "mtime_ms": NOW_MS}]}
    r = c.post(f"/api/dashboards/{stock}/generator/runs",
               json={"engine": "browser", "input_manifest": im2})
    check("run 2 start -> 201", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    start2 = r.json()
    run2 = start2["run_id"]
    check("last_known carries run 1's files (size, mtime_ms)",
          start2["last_known"].get("w31.xlsx") == [10_000, NOW_MS - DAY_MS]
          and start2["last_known"].get("w32.xlsx") == [30_000, NOW_MS],
          str(start2["last_known"]))

    # ── observations POST: happy path ────────────────────────────────────
    obs_url = f"/api/dashboards/{stock}/generator/runs/{run2}/observations"
    r = c.post(obs_url, json={
        "seq": 1, "final": False,
        "files": [{"dir": 0, "name": "w33.xlsx", "kind": "parsed",
                   "schema": {"sheets": [{"name": "Sheet1", "header": ["Date", "Total"]}]}}],
    })
    check("observations happy path -> ok", r.status_code == 200 and r.json().get("ok") is True,
          f"{r.status_code} {r.text[:300]}")
    check("files_updated == 1", r.json().get("files_updated") == 1, str(r.json()))

    db = SessionLocal()
    obs2 = db.query(SourceObservation).filter(SourceObservation.run_id == run2).first()
    sf = db.query(SourceFile).filter(SourceFile.observation_id == obs2.id, SourceFile.name == "w33.xlsx").first()
    check("kind upgraded from listed to parsed", sf.kind == "parsed", str(sf.kind))
    check("schema_json stored", json.loads(sf.schema_json)["sheets"][0]["header"] == ["Date", "Total"])
    db.close()

    # ── 403: someone other than the run's starter ────────────────────────
    from app.models import User
    db = SessionLocal()
    db.add(User(person_id="stranger", display_name="Stranger", role="superuser", is_active=True))
    db.commit()
    db.close()
    c2 = TestClient(app)
    login(c2, "stranger")
    r = c2.post(obs_url, json={"seq": 2, "final": False, "files": []})
    check("non-starter -> 403", r.status_code == 403, f"{r.status_code} {r.text[:200]}")

    # ── 409: replayed/lower seq ───────────────────────────────────────────
    r = c.post(obs_url, json={"seq": 1, "final": False, "files": []})
    check("replayed seq -> 409", r.status_code == 409, f"{r.status_code} {r.text[:200]}")

    # ── 400: unknown file ─────────────────────────────────────────────────
    r = c.post(obs_url, json={
        "seq": 2, "final": False,
        "files": [{"dir": 0, "name": "not_in_the_manifest.xlsx", "kind": "parsed"}],
    })
    check("unknown file -> 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    # ── 413: oversize batch ────────────────────────────────────────────────
    huge_header = ["c" * 60] * 300  # under-the-cell-cap size still adds up fast
    huge_files = [{"dir": 0, "name": "w33.xlsx", "kind": "parsed",
                   "schema": {"sheets": [{"name": f"S{i}", "header": huge_header} for i in range(50)]}}
                  for _ in range(40)]
    r = c.post(obs_url, json={"seq": 3, "final": False, "files": huge_files})
    check("oversize batch -> 413", r.status_code == 413, f"{r.status_code} {r.text[:200]}")

    # ── accepted within the grace window after the run closed ─────────────
    r = c.post(f"/api/dashboards/{stock}/generator/runs/{run2}/error",
               json={"message": "smoke: closing run 2"})
    assert r.status_code == 200
    r = c.post(obs_url, json={
        "seq": 4, "final": True,
        "files": [{"dir": 0, "name": "w33.xlsx", "kind": "cached"}],
    })
    check("accepted shortly after error (grace window)", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")
    check("final=True in the response", r.json().get("final") is True, str(r.json()))

    # ── final=True rolled the summary up onto DataSource ───────────────────
    db = SessionLocal()
    ds_stock = db.query(DataSource).filter(DataSource.id == ds_stock.id).first()
    obs2 = db.query(SourceObservation).filter(SourceObservation.run_id == run2).first()
    check("DataSource.last_seen_at set", ds_stock.last_seen_at is not None)
    check("DataSource.fingerprint == the run's observation fingerprint",
          ds_stock.fingerprint == obs2.fingerprint, f"{ds_stock.fingerprint} vs {obs2.fingerprint}")
    check("DataSource.last_file_count == the run's observation file_count",
          ds_stock.last_file_count == obs2.file_count, str(ds_stock.last_file_count))
    db.close()

    # A run truly closed beyond the grace window would 409 — not reproduced
    # here (would need to sleep >60s); covered by code review of the
    # `_OBSERVATIONS_GRACE_S` comparison instead of a slow test.

    # ═══════════════════════════════════════════════════════════════════
    # A6 — overrides CRUD + manifest scoping + data-sources version 409
    # ═══════════════════════════════════════════════════════════════════

    # Ops also reads Stock_RDC, so the source is genuinely shared.
    r = c.post(f"/api/dashboards/{ops}/generator/runs",
               json={"engine": "browser",
                     "input_manifest": {"dirs": ["Stock_RDC"],
                                        "files": [{"name": "ops.xlsx", "size": 1_000, "mtime_ms": NOW_MS}]}})
    assert r.status_code == 201, r.text[:300]
    run_ops = r.json()["run_id"]
    c.post(f"/api/dashboards/{ops}/generator/runs/{run_ops}/error", json={"message": "smoke"})

    db = SessionLocal()
    source_id = db.query(DataSource).filter(DataSource.kind == "folder",
                                            DataSource.key_name == "Stock_RDC").first().id
    db.close()

    r = c.get(f"/api/data-sources/{source_id}/overrides")
    check("overrides GET -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    rows = r.json() if r.status_code == 200 else []
    by_gen = {row["generator_id"]: row for row in rows}
    check("both generators reach this source (each has observed it)",
          gid_stock in by_gen and gid_ops in by_gen, str(sorted(by_gen)))
    check("no override yet -> exclude_rules [], version 0",
          by_gen.get(gid_stock, {}).get("exclude_rules") == []
          and by_gen.get(gid_stock, {}).get("version") == 0,
          str(by_gen.get(gid_stock)))

    # "archive/**" is refused: the walk is one level deep, a pattern with "/"
    # could only match inside a subfolder it will never see.
    r = c.put(f"/api/data-sources/{source_id}/overrides/{gid_stock}",
             json={"exclude_rules": ["archive/**"]})
    check('"archive/**" -> 400', r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    r = c.put(f"/api/data-sources/{source_id}/overrides/{gid_stock}",
             json={"exclude_rules": ["~$*", "*_template.xlsx"]})
    check("override PUT -> ok", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    ov1 = r.json()
    check("version bumped from 0 to 1 on first write", ov1["version"] == 1, str(ov1))

    r = c.put(f"/api/data-sources/{source_id}/overrides/{gid_stock}",
             json={"exclude_rules": ["~$*"], "version": 99})
    check("stale version on override -> 409", r.status_code == 409, f"{r.status_code} {r.text[:200]}")

    # ── manifest scoping: gen-ops must NOT see gen-stock's override ──────
    r = c.get(f"/api/dashboards/{stock}/generator/manifest")
    check("stock manifest -> 200", r.status_code == 200, str(r.status_code))
    stock_man = r.json()
    check("stock manifest carries its own override",
          stock_man.get("source_rules", {}).get("Stock_RDC") == ["~$*", "*_template.xlsx"],
          str(stock_man.get("source_rules")))
    check("stock manifest carries normalized inputs[]",
          isinstance(stock_man.get("inputs"), list), str(stock_man.get("inputs")))
    check("stock manifest declares force_deps python-calamine",
          stock_man.get("force_deps") == ["python-calamine"], str(stock_man.get("force_deps")))

    r = c.get(f"/api/dashboards/{ops}/generator/manifest")
    ops_man = r.json() if r.status_code == 200 else {}
    check("gen-ops does NOT inherit gen-stock's override on the same source",
          "Stock_RDC" not in (ops_man.get("source_rules") or {}), str(ops_man.get("source_rules")))

    # ── reach: a stranger cannot read or write overrides ──────────────────
    r = c2.get(f"/api/data-sources/{source_id}/overrides")
    check("stranger sees no overrides (no reachable generator)", r.status_code == 200 and r.json() == [],
          f"{r.status_code} {r.text[:200]}")
    r = c2.put(f"/api/data-sources/{source_id}/overrides/{gid_stock}",
              json={"exclude_rules": ["nope.xlsx"]})
    check("stranger cannot write an override -> 404", r.status_code == 404,
          f"{r.status_code} {r.text[:200]}")

    # ── PUT /api/data-sources: 409 on a stale version ──────────────────────
    # The DataSource row already exists (ingest_run_start created it with the
    # model default version=1) before anyone claims it — a claim is an EDIT of
    # that row, not its creation, so it bumps like any other write.
    r = c.put("/api/data-sources", json={
        "kind": "folder", "key_name": "Stock_RDC", "declared_path": r"\\share\Stock_RDC"})
    check("claim source -> 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    claimed_version = r.json().get("version")
    check("claiming an ingest-created row bumps its version", claimed_version == 2, str(claimed_version))

    r = c.put("/api/data-sources", json={
        "kind": "folder", "key_name": "Stock_RDC", "label": "RDC weekly stock",
        "version": claimed_version + 5})
    check("stale version on data source -> 409", r.status_code == 409, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 409:
        check("409 carries current state", "current" in (r.json().get("detail") or {}),
              str(r.json()))

    r = c.put("/api/data-sources", json={
        "kind": "folder", "key_name": "Stock_RDC", "label": "RDC weekly stock",
        "version": claimed_version})
    check("matching version -> 200, bumps again",
          r.status_code == 200 and r.json().get("version") == claimed_version + 1,
          f"{r.status_code} {r.text[:300]}")
    check("excludes field ignored (deprecated column)",
          "excludes" not in r.json(), str(r.json()))

    # ═══════════════════════════════════════════════════════════════════
    # A4 — schemadiff (pure function) + outputs_json on publish
    # ═══════════════════════════════════════════════════════════════════

    # Cyrillic on purpose: schemadiff must case-fold Cyrillic headers.
    inputs = [
        {"match": "Sales_*.xlsx", "role": "data", "required": True,
         "sheets": [{"name": "*", "columns": ["Дата ", "ИТОГО", "Статус"]}]},
        {"match": "Missing_*.xlsx", "role": "reference", "required": True, "sheets": []},
    ]
    files = [
        {"name": "Sales_31.xlsx", "matched_input": "Sales_*.xlsx",
         "schema": {"sheets": [{"name": "Q1", "header": [" дата", "итого", "extra col"]}]},
         "consumed": None},
    ]
    d = schemadiff.diff(inputs, files)
    check("unmatched required pattern -> unmatched_patterns + error severity",
          "Missing_*.xlsx" in d["unmatched_patterns"] and d["severity"] == "error", str(d))
    sales_diff = d["per_pattern"]["Sales_*.xlsx"]["sheets"]["*"]
    check("Cyrillic + trailing-space + case-insensitive column match: 'Дата ' observed as ' дата'",
          "дата" not in sales_diff["missing_columns"], str(sales_diff))
    check("declared column absent from header -> missing_columns",
          sales_diff["missing_columns"] == ["Статус"], str(sales_diff))
    check("observed header column not declared -> extra_columns",
          "extra col" in sales_diff["extra_columns"], str(sales_diff))

    # role=data, missing column, pattern DID match -> warn (not error, since
    # the required-but-unmatched pattern already forced 'error' above; test
    # the warn path in isolation with no unmatched required pattern).
    d2 = schemadiff.diff(
        [{"match": "Sales_*.xlsx", "role": "data", "required": False,
          "sheets": [{"name": "*", "columns": ["ONE MORE COLUMN"]}]}],
        [{"name": "Sales_1.xlsx", "matched_input": "Sales_*.xlsx",
          "schema": {"sheets": [{"name": "S", "header": ["other"]}]}}],
    )
    check("missing column on role=data, no unmatched required -> warn", d2["severity"] == "warn", str(d2))

    d3 = schemadiff.diff(
        [{"match": "Any_*.xlsx", "role": "data", "sheets": []}],
        [{"name": "Any_1.xlsx", "matched_input": "Any_*.xlsx", "schema": None}],
    )
    check("matched, nothing declared to compare -> ok", d3["severity"] == "ok", str(d3))

    # consumed_but_undeclared
    d4 = schemadiff.diff(
        [{"match": "Sales_*.xlsx", "role": "data",
          "sheets": [{"name": "S", "columns": ["a"]}]}],
        [{"name": "Sales_1.xlsx", "matched_input": "Sales_*.xlsx", "schema": None,
          "consumed": {"sheets": {"S": {"columns": ["a", "b"]}}}}],
    )
    check("consumed column not declared -> consumed_but_undeclared",
          d4["consumed_but_undeclared"] == [{"file": "Sales_1.xlsx", "sheet": "S", "columns": ["b"]}],
          str(d4["consumed_but_undeclared"]))

    # aliases: declared "SKU Description" observed as "SKU_Discription"
    d5 = schemadiff.diff(
        [{"match": "Sales_*.xlsx", "role": "data",
          "sheets": [{"name": "S", "columns": ["SKU Description", "Qty"]}]}],
        [{"name": "Sales_1.xlsx", "matched_input": "Sales_*.xlsx",
          "schema": {"sheets": [{"name": "S", "header": ["sku_discription ", "Other"]}]}}],
        aliases={"Sales_*.xlsx/S": {"SKU Description": "SKU_Discription"}},
    )
    s5 = d5["per_pattern"]["Sales_*.xlsx"]["sheets"]["S"]
    check("aliased declared column -> aliased, not missing; target not extra",
          s5["aliased"] == ["SKU Description"] and s5["missing_columns"] == ["Qty"]
          and s5["extra_columns"] == ["Other"], str(s5))
    d6 = schemadiff.diff(
        [{"match": "Sales_*.xlsx", "role": "data",
          "sheets": [{"name": "S", "columns": ["SKU Description"]}]}],
        [{"name": "Sales_1.xlsx", "matched_input": "Sales_*.xlsx",
          "schema": {"sheets": [{"name": "S", "header": ["Other"]}]}}],
        aliases={"Sales_*.xlsx/S": {"SKU Description": "SKU_Discription"}},
    )
    s6 = d6["per_pattern"]["Sales_*.xlsx"]["sheets"]["S"]
    check("alias whose target is not observed -> still missing",
          s6["aliased"] == [] and s6["missing_columns"] == ["SKU Description"], str(s6))
    check("diff() without aliases keeps 'aliased': []", sales_diff.get("aliased") == [], str(sales_diff))

    # ── publish with outputs_json + observed diff producing a warning ─────
    r = c.post("/api/admin/generators",
              files={"bundle": ("b.zip", make_bundle("gen-diff", {
                  "inputs": [{"match": "Sales_*.xlsx", "role": "data", "required": True,
                              "sheets": [{"name": "*", "columns": ["Date", "Total"]}]}],
              }), "application/zip")})
    assert r.status_code == 201, r.text[:300]
    gid_diff = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "Diff Report", "generator_id": gid_diff})
    assert r.status_code == 201, r.text[:300]
    dash_diff = r.json()["id"]

    r = c.post(f"/api/dashboards/{dash_diff}/generator/runs", json={
        "engine": "browser",
        "input_manifest": {"dirs": ["Sales"],
                           "files": [{"name": "Sales_31.xlsx", "size": 1_000, "mtime_ms": NOW_MS,
                                      "matched_input": "Sales_*.xlsx"}]},
    })
    assert r.status_code == 201, r.text[:300]
    run_diff = r.json()["run_id"]

    r = c.post(f"/api/dashboards/{dash_diff}/generator/runs/{run_diff}/observations", json={
        "seq": 1, "final": True,
        "files": [{"dir": 0, "name": "Sales_31.xlsx", "kind": "parsed",
                   "schema": {"sheets": [{"name": "Sheet1", "header": ["Date", "Other"]}]}}],
    })
    check("observations for diff run -> ok", r.status_code == 200, f"{r.status_code} {r.text[:300]}")

    outputs_json = json.dumps({"write_back": [
        {"name": "recomputed.xlsx", "size": 42, "targets": ["Sales"], "failed": []},
    ]})
    html = "<html><head><title>D</title></head><body>ready</body></html>"
    r = c.post(f"/api/dashboards/{dash_diff}/generator/runs/{run_diff}/publish",
              data={"outputs_json": outputs_json},
              files={"html_file": ("dashboard.html", html.encode(), "text/html")})
    check("publish with outputs_json -> ok", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    warnings = r.json().get("warnings") or []
    # Compared case-insensitively, reported as the author wrote it.
    check("publish warns about the missing declared column",
          any("Total" in w for w in warnings), str(warnings))

    db = SessionLocal()
    run_row = db.query(GenerationRun).filter(GenerationRun.id == run_diff).first()
    check("outputs_json stored on the run",
          run_row.outputs_json is not None
          and json.loads(run_row.outputs_json)["write_back"][0]["name"] == "recomputed.xlsx",
          str(run_row.outputs_json))
    db.close()

    # outputs_json shape validation
    r = c.post(f"/api/dashboards/{dash_diff}/generator/runs", json={"engine": "browser"})
    if r.status_code == 201:
        bad_run = r.json()["run_id"]
        html2 = "<html><head><title>D</title></head><body>ready</body></html>"
        r = c.post(f"/api/dashboards/{dash_diff}/generator/runs/{bad_run}/publish",
                  data={"outputs_json": '{"nope": true}'},
                  files={"html_file": ("dashboard.html", html2.encode(), "text/html")})
        check("outputs_json missing write_back -> 400", r.status_code == 400,
              f"{r.status_code} {r.text[:200]}")

    # ═══════════════════════════════════════════════════════════════════
    # P1.3 — checks/profiles ingest + baseline seeding
    # ═══════════════════════════════════════════════════════════════════
    from app.models import QualityResult, ValueBaseline

    branch_folder = "Branches_Data"
    branch_file = "Branches_1.xlsx"
    kv_id = "known_values:Branches_*.xlsx:Sheet1:Branch"
    nn_id = "not_null:Branches_*.xlsx:Sheet1:Sum"

    r = c.post(f"/api/dashboards/{stock}/generator/runs", json={
        "engine": "browser",
        "input_manifest": {"dirs": [branch_folder],
                           "files": [{"name": branch_file, "size": 1_000, "mtime_ms": NOW_MS}]},
    })
    check("checks/profiles run start -> 201", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    run3 = r.json()["run_id"]
    obs3_url = f"/api/dashboards/{stock}/generator/runs/{run3}/observations"

    long_value = "a" * 100
    batch1_checks = [
        {"check_id": kv_id, "status": "pass", "count": 10, "failed": 0,
         "distinct": [long_value, "Acme North LLC"], "sampled": False},
        {"check_id": nn_id, "status": "fail", "count": 10, "failed": 2, "sampled": True,
         "examples": ["e1", "e2", "e3", "e4", "e5", "e6"], "reason": "x" * 100},
        {"check_id": "bad"},                                            # too few colon parts
        {"check_id": "known_values:Branches_*.xlsx:Sheet1:Bad2", "status": "nope"},  # bad status
    ]
    batch1_profiles = [
        {"sheet": "Sheet1", "column": "Branch", "type": "text", "nulls": 0, "distinct": 2,
         "top": [["Acme North LLC", 5], ["Acme Central LLC", 5]], "sampled": False},
        {"sheet": "Sheet1"},                                            # missing column
    ]
    r = c.post(obs3_url, json={
        "seq": 1, "final": False,
        "files": [{"dir": 0, "name": branch_file, "kind": "parsed",
                   "checks": batch1_checks, "profiles": batch1_profiles}],
    })
    check("checks+profiles batch -> ok", r.status_code == 200, f"{r.status_code} {r.text[:300]}")

    db = SessionLocal()
    ds_branch = db.query(DataSource).filter(DataSource.kind == "folder",
                                            DataSource.key_name == branch_folder).first()
    obs3 = db.query(SourceObservation).filter(SourceObservation.run_id == run3).first()
    sf3 = db.query(SourceFile).filter(SourceFile.observation_id == obs3.id,
                                      SourceFile.name == branch_file).first()
    rows = db.query(QualityResult).filter(QualityResult.source_file_id == sf3.id).all()
    check("malformed items dropped: 2 valid checks + 1 valid profile = 3 rows",
          len(rows) == 3, str([r.check_id for r in rows]))
    kv_row = next((r for r in rows if r.check_id == kv_id), None)
    check("known_values row stored", kv_row is not None)
    if kv_row is not None:
        m = kv_row.get_metric()
        check("distinct trimmed to 64 chars each",
              m.get("distinct") == [long_value[:64], "Acme North LLC"], str(m))
        check("known_values check_type", kv_row.check_type == "known_values")
    nn_row = next((r for r in rows if r.check_id == nn_id), None)
    check("not_null row stored", nn_row is not None)
    if nn_row is not None:
        m = nn_row.get_metric()
        check("examples capped at 5", m.get("examples") == ["e1", "e2", "e3", "e4", "e5"], str(m))
        check("reason capped at 64 chars", m.get("reason") == ("x" * 100)[:64], str(m))
        check("sampled flag stored", nn_row.sampled is True)
    prof_row = next((r for r in rows if r.check_type == "profile"), None)
    check("profile row stored with check_type=profile",
          prof_row is not None and prof_row.check_id == "profile:Sheet1:Branch",
          str(prof_row and prof_row.check_id))
    if prof_row is not None:
        pm = prof_row.get_metric()
        check("profile metric carries trimmed top", pm.get("top") == [["Acme North LLC", 5], ["Acme Central LLC", 5]],
              str(pm))
    db.close()

    # ── replay: same seq -> 409, no duplicate rows ────────────────────────
    r = c.post(obs3_url, json={
        "seq": 1, "final": False,
        "files": [{"dir": 0, "name": branch_file, "kind": "parsed",
                   "checks": batch1_checks, "profiles": batch1_profiles}],
    })
    check("checks batch replay (same seq) -> 409", r.status_code == 409, f"{r.status_code} {r.text[:200]}")
    db = SessionLocal()
    check("replay left row count unchanged",
          db.query(QualityResult).filter(QualityResult.source_file_id == sf3.id).count() == 3)
    db.close()

    # ── second batch, different seq: replaces the file's quality rows ─────
    # ref_lookup (ADR-16) rides the same batch: its `missing` must land in
    # metric_json the way `distinct` does, and it must NOT seed a baseline —
    # the platform reference table, not this run, is what "known" means.
    rl_id = "ref_lookup:Branches_*.xlsx:Sheet1:Branch"
    batch2_checks = [
        {"check_id": kv_id, "status": "pass", "count": 12, "failed": 0,
         "distinct": ["Acme North LLC", "Acme Central LLC"], "sampled": False},
        {"check_id": rl_id, "status": "fail", "count": 12, "failed": 3,
         "examples": ["Acme East LLC"], "missing": [long_value, "Acme East LLC"],
         "missing_overflow": True, "sampled": False},
    ]
    r = c.post(obs3_url, json={
        "seq": 2, "final": True,
        "files": [{"dir": 0, "name": branch_file, "kind": "parsed", "checks": batch2_checks}],
    })
    check("second checks batch (final) -> ok", r.status_code == 200, f"{r.status_code} {r.text[:300]}")

    db = SessionLocal()
    rows2 = db.query(QualityResult).filter(QualityResult.source_file_id == sf3.id).all()
    check("second batch replaced rows (only the resubmitted checks remain)",
          len(rows2) == 2 and {r.check_id for r in rows2} == {kv_id, rl_id},
          str([r.check_id for r in rows2]))
    rl_row = next((r for r in rows2 if r.check_id == rl_id), None)
    check("ref_lookup row stored with check_type=ref_lookup",
          rl_row is not None and rl_row.check_type == "ref_lookup" and rl_row.failed == 3,
          str(rl_row and (rl_row.check_type, rl_row.failed)))
    if rl_row is not None:
        rm = rl_row.get_metric()
        check("ref_lookup missing stored, each value trimmed to 64 chars",
              rm.get("missing") == [long_value[:64], "Acme East LLC"], str(rm))
        check("ref_lookup missing_overflow stored", rm.get("missing_overflow") is True, str(rm))
        check("ref_lookup metric keeps distinct empty (it is not known_values)",
              rm.get("distinct") is None and rm.get("distinct_overflow") is False, str(rm))
    check("ref_lookup seeds NO ValueBaseline",
          db.query(ValueBaseline).filter(
              ValueBaseline.data_source_id == ds_branch.id,
              ValueBaseline.generator_id == gid_stock,
              ValueBaseline.match == "Branches_*.xlsx",
              ValueBaseline.sheet == "Sheet1",
              ValueBaseline.column == "Branch").count() == 1)

    baseline = db.query(ValueBaseline).filter(
        ValueBaseline.data_source_id == ds_branch.id, ValueBaseline.generator_id == gid_stock,
        ValueBaseline.match == "Branches_*.xlsx", ValueBaseline.sheet == "Sheet1",
        ValueBaseline.column == "Branch").first()
    check("baseline seeded on final with sorted distinct",
          baseline is not None and baseline.get_values() == sorted(["Acme North LLC", "Acme Central LLC"]),
          str(baseline and baseline.get_values()))
    check("baseline seeded_run_id == run3, version 1",
          baseline is not None and baseline.seeded_run_id == run3 and baseline.version == 1,
          str(baseline and (baseline.seeded_run_id, baseline.version)))
    db.close()

    # ── second run, same column with drifted values: baseline untouched ───
    # run3 is `final` in its observations but still "running" (only
    # publish/error close a run) — close it so run4 clears the run-lock.
    r = c.post(f"/api/dashboards/{stock}/generator/runs/{run3}/error",
               json={"message": "smoke: closing run 3"})
    assert r.status_code == 200
    r = c.post(f"/api/dashboards/{stock}/generator/runs", json={
        "engine": "browser",
        "input_manifest": {"dirs": [branch_folder],
                           "files": [{"name": branch_file, "size": 1_100, "mtime_ms": NOW_MS + DAY_MS}]},
    })
    check("second checks run start -> 201", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    run4 = r.json()["run_id"]
    r = c.post(f"/api/dashboards/{stock}/generator/runs/{run4}/observations", json={
        "seq": 1, "final": True,
        "files": [{"dir": 0, "name": branch_file, "kind": "parsed",
                   "checks": [{"check_id": kv_id, "status": "pass", "count": 13, "failed": 0,
                              "distinct": ["Acme North LLC", "Acme North LLC CONTOSO", "Acme Central LLC"],
                              "sampled": False}]}],
    })
    check("second run's checks batch (final) -> ok", r.status_code == 200, f"{r.status_code} {r.text[:300]}")

    db = SessionLocal()
    baseline2 = db.query(ValueBaseline).filter(ValueBaseline.id == baseline.id).first()
    check("baseline unchanged after second run (still version 1, original values)",
          baseline2.version == 1
          and baseline2.get_values() == sorted(["Acme North LLC", "Acme Central LLC"]),
          str((baseline2.version, baseline2.get_values())))
    check("baseline still attributed to the seeding run, not the second one",
          baseline2.seeded_run_id == run3, str(baseline2.seeded_run_id))
    db.close()

    # ═══════════════════════════════════════════════════════════════════
    # P2.3 — publish integration (quality warnings + quality_url)
    # ═══════════════════════════════════════════════════════════════════
    r = c.post(f"/api/dashboards/{stock}/generator/runs/{run4}/error",
               json={"message": "smoke: closing run 4"})
    assert r.status_code == 200
    r = c.post(f"/api/dashboards/{stock}/generator/runs", json={
        "engine": "browser",
        "input_manifest": {"dirs": [branch_folder],
                           "files": [{"name": branch_file, "size": 1_200, "mtime_ms": NOW_MS + 2 * DAY_MS}]},
    })
    check("publish-quality run start -> 201", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    run5 = r.json()["run_id"]
    new_kv_value = "Acme North LLC CONTOSO"
    r = c.post(f"/api/dashboards/{stock}/generator/runs/{run5}/observations", json={
        "seq": 1, "final": True,
        "files": [{"dir": 0, "name": branch_file, "kind": "parsed",
                   "checks": [
                       {"check_id": nn_id, "status": "fail", "count": 10, "failed": 3, "sampled": False},
                       {"check_id": kv_id, "status": "pass", "count": 13, "failed": 0, "sampled": False,
                        "distinct": ["Acme North LLC", "Acme Central LLC", new_kv_value]},
                   ]}],
    })
    check("publish-quality run observations -> ok", r.status_code == 200, f"{r.status_code} {r.text[:300]}")

    html5 = "<html><head><title>Q</title></head><body>ready</body></html>"
    r = c.post(f"/api/dashboards/{stock}/generator/runs/{run5}/publish",
              files={"html_file": ("dashboard.html", html5.encode(), "text/html")})
    check("publish with failing checks + new values -> ok", r.status_code == 200,
          f"{r.status_code} {r.text[:300]}")
    pub5 = r.json()
    warnings5 = pub5.get("warnings") or []
    check("publish warnings include the failing not_null check",
          any("Check not_null" in w and "Sum" in w for w in warnings5), str(warnings5))
    check("publish warnings include the new known_values value",
          any("New values" in w and new_kv_value in w for w in warnings5), str(warnings5))
    check("publish response carries quality_url", bool(pub5.get("quality_url")), str(pub5))

    # ── accept the new value directly on the baseline, then publish again ──
    db = SessionLocal()
    bl = db.query(ValueBaseline).filter(ValueBaseline.id == baseline.id).first()
    bl.set_values(sorted(bl.get_values() + [new_kv_value]))
    bl.version += 1
    db.commit()
    db.close()

    r = c.post(f"/api/dashboards/{stock}/generator/runs", json={
        "engine": "browser",
        "input_manifest": {"dirs": [branch_folder],
                           "files": [{"name": branch_file, "size": 1_300, "mtime_ms": NOW_MS + 3 * DAY_MS}]},
    })
    check("second publish-quality run start -> 201", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    run6 = r.json()["run_id"]
    r = c.post(f"/api/dashboards/{stock}/generator/runs/{run6}/observations", json={
        "seq": 1, "final": True,
        "files": [{"dir": 0, "name": branch_file, "kind": "parsed",
                   "checks": [
                       {"check_id": kv_id, "status": "pass", "count": 13, "failed": 0, "sampled": False,
                        "distinct": ["Acme North LLC", "Acme Central LLC", new_kv_value]},
                   ]}],
    })
    check("second publish-quality observations -> ok", r.status_code == 200, f"{r.status_code} {r.text[:300]}")

    html6 = "<html><head><title>Q</title></head><body>ready</body></html>"
    r = c.post(f"/api/dashboards/{stock}/generator/runs/{run6}/publish",
              files={"html_file": ("dashboard.html", html6.encode(), "text/html")})
    check("second publish -> ok", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    pub6 = r.json()
    warnings6 = pub6.get("warnings") or []
    check("new-values warning gone after accepting the value on the baseline",
          not any("New values appeared" in w for w in warnings6), str(warnings6))
    # quality_url is a pointer to something to act on: present while the
    # not_null check still fails, absent once nothing quality-related remains.
    check("second publish keeps quality_url while a check still fails",
          bool(pub6.get("quality_url")) == any(w.startswith("Check ") for w in warnings6), str(pub6))
    check("quality_url is URL-encoded", "?generator=" in (pub6.get("quality_url") or "?generator=")
          and " " not in (pub6.get("quality_url") or ""), str(pub6.get("quality_url")))

    stage_cases(c, stock, branch_folder, branch_file)
    schema_reason_cases(c, stock, branch_folder, branch_file)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


def schema_reason_cases(c, dash_id, folder, file_name):
    """A schema that says WHY it is empty must survive the round trip.

    The production defect: `apply_batch` stores schema_json only for a schema
    that `is not None`, so a worker that sent `schema: null` on a sniff failure
    wrote the same row as a worker that sent nothing at all — NULL, which
    `lineage.collect_chain_files` walks back through the chain as "unchanged".
    Seven files ended up with no schema anywhere in their chain and no error to
    explain it. The worker now sends {"sheets": [], "error": "..."} instead
    (static/js/gen-worker.js) and _lineage.sniff attaches a per-sheet "reason"
    (static/py/_lineage.py); what is pinned here is the SERVER half — that
    those two fields reach schema_json intact, that an explicit empty schema is
    NOT read as "unchanged", and that omitting the key still is.
    """
    from app.database import SessionLocal
    from app.models import SourceFile, SourceObservation

    def start_run(size):
        r = c.post(f"/api/dashboards/{dash_id}/generator/runs", json={
            "engine": "browser",
            "input_manifest": {"dirs": [folder],
                               "files": [{"name": file_name, "size": size,
                                          "mtime_ms": NOW_MS + 20 * DAY_MS}]},
        })
        assert r.status_code == 201, f"schema run start: {r.status_code} {r.text[:300]}"
        return r.json()["run_id"]

    def schema_of(run_id):
        db = SessionLocal()
        try:
            obs = (db.query(SourceObservation)
                   .filter(SourceObservation.run_id == run_id).first())
            sf = (db.query(SourceFile)
                  .filter(SourceFile.observation_id == obs.id,
                          SourceFile.name == file_name).first())
            return sf.schema_json
        finally:
            db.close()

    def close(run_id):
        r = c.post(f"/api/dashboards/{dash_id}/generator/runs/{run_id}/error",
                   json={"message": "smoke: closing schema-reason run"})
        assert r.status_code == 200

    # ── an explicit empty schema WITH an error is stored, not skipped ────
    run = start_run(3_000)
    url = f"/api/dashboards/{dash_id}/generator/runs/{run}/observations"
    r = c.post(url, json={"seq": 1, "files": [
        {"dir": 0, "name": file_name, "kind": "parsed",
         "schema": {"sheets": [], "error": "MemoryError: sheet too large"}}]})
    check("empty schema + error -> ok", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    stored = schema_of(run)
    check("an explicit empty schema is STORED, not read as unchanged",
          stored is not None, str(stored))
    check("the top-level error survives to schema_json",
          json.loads(stored or "{}").get("error") == "MemoryError: sheet too large",
          str(stored))

    # ── a per-sheet reason survives _truncate_schema ──────────────────────
    r = c.post(url, json={"seq": 2, "files": [
        {"dir": 0, "name": file_name, "kind": "parsed",
         "schema": {"sheets": [{"name": "Data", "header": [], "header_row": None,
                                "rows": 5000,
                                "reason": "no header in first 20 rows"}],
                    "error": None}}]})
    check("per-sheet reason -> ok", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    sh = (json.loads(schema_of(run) or "{}").get("sheets") or [{}])[0]
    check("a per-sheet reason survives the truncation pass",
          sh.get("reason") == "no header in first 20 rows", str(sh))
    check("header_row None survives alongside the reason",
          sh.get("header_row") is None and sh.get("header") == [] and sh.get("rows") == 5000,
          str(sh))

    # ── a 200-char reason is not the thing that makes a batch oversize ────
    long_reason = "\u00e9" * 200  # two UTF-8 bytes per char, like Cyrillic
    r = c.post(url, json={"seq": 3, "files": [
        {"dir": 0, "name": file_name, "kind": "parsed",
         "schema": {"sheets": [{"name": "Data", "header": [], "header_row": None,
                                "reason": long_reason}], "error": long_reason}}]})
    check("a 200-char reason/error is accepted", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")
    got = json.loads(schema_of(run) or "{}")
    check("a 200-char reason/error is kept whole",
          got.get("error") == long_reason
          and (got.get("sheets") or [{}])[0].get("reason") == long_reason, str(got)[:200])

    # ── omitting the key is still the ONLY way to say "unchanged" ─────────
    r = c.post(url, json={"seq": 4, "files": [
        {"dir": 0, "name": file_name, "kind": "cached"}]})
    check("a batch that omits `schema` -> ok", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")
    check("omitting `schema` leaves the stored one alone (the 'unchanged' contract)",
          json.loads(schema_of(run) or "{}").get("error") == long_reason,
          str(schema_of(run))[:200])

    # ── and an explicit null still means unchanged too: that is exactly why
    #    the worker must never send it for a failure (documented, pinned so a
    #    future reader sees the trap rather than rediscovering it in prod).
    r = c.post(url, json={"seq": 5, "files": [
        {"dir": 0, "name": file_name, "kind": "cached", "schema": None}]})
    check("an explicit null schema -> ok", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("an explicit null schema is INDISTINGUISHABLE from unchanged (why the worker must not send it)",
          json.loads(schema_of(run) or "{}").get("error") == long_reason,
          str(schema_of(run))[:200])
    close(run)


def stage_cases(c, dash_id, folder, file_name):
    """P4.1 — `stages` on an observations batch.

    A stage is the generator narrating its own pipeline; the server's whole
    job is to store the narration without letting it become a payload. So the
    cases here are: it lands, a replay replaces rather than doubles, every
    limit in the contract truncates instead of rejecting, and a batch that
    says nothing about stages leaves the ones already stored alone.
    """
    from app.database import SessionLocal
    from app.models import RunStage
    from app.services import observations

    def start_run(size):
        r = c.post(f"/api/dashboards/{dash_id}/generator/runs", json={
            "engine": "browser",
            "input_manifest": {"dirs": [folder],
                               "files": [{"name": file_name, "size": size,
                                          "mtime_ms": NOW_MS + 10 * DAY_MS}]},
        })
        assert r.status_code == 201, f"stage run start: {r.status_code} {r.text[:300]}"
        return r.json()["run_id"]

    def stages_of(run_id):
        db = SessionLocal()
        try:
            return (db.query(RunStage).filter(RunStage.run_id == run_id)
                    .order_by(RunStage.order).all())
        finally:
            db.close()

    run = start_run(2_000)
    url = f"/api/dashboards/{dash_id}/generator/runs/{run}/observations"

    # ── the happy path: two stages, one reading a file, one derived ───────
    payload = [
        {"order": 0, "name": "raw", "rows": 1000, "columns": ["Branch", "Sum"],
         "profiles": [{"column": "Branch", "type": "text", "nulls": 0, "count": 1000,
                       "distinct": 3, "min": "A", "max": "Z",
                       "top": [["Acme North LLC", 400]], "sampled": False}],
         # The engine measures with perf_counter and sends a FLOAT here.
         "inputs": None, "read_files": [file_name], "ms": 12.4},
        {"order": 1, "name": "aggregated", "rows": 3, "columns": ["Branch", "Total"],
         "profiles": [], "inputs": ["raw"], "read_files": [], "ms": 4},
    ]
    r = c.post(url, json={"seq": 1, "files": [], "stages": payload})
    check("stages ride a batch with no files at all", r.status_code == 200,
          f"{r.status_code} {r.text[:300]}")
    rows = stages_of(run)
    check("both stages stored in call order",
          [(x.order, x.name) for x in rows] == [(0, "raw"), (1, "aggregated")], str(rows))
    if len(rows) == 2:
        check("rows/ms/columns carried over (a float ms is rounded, not dropped)",
              rows[0].rows == 1000 and rows[0].ms == 12
              and rows[0].get_columns() == ["Branch", "Sum"], str(rows[0].columns_json))
        check("read_files carried over", rows[0].get_read_files() == [file_name],
              str(rows[0].read_files_json))
        check("a NULL inputs stays NULL (means 'the previous stage')",
              rows[0].get_inputs() is None and rows[1].get_inputs() == ["raw"],
              f"{rows[0].inputs_json} / {rows[1].inputs_json}")
        prof = rows[0].get_profiles()
        check("the stage profile keeps the contract's fields",
              len(prof) == 1 and prof[0]["column"] == "Branch" and prof[0]["type"] == "text"
              and prof[0]["count"] == 1000 and prof[0]["top"] == [["Acme North LLC", 400]],
              str(prof))

    # ── replay: the engine re-sends the whole list, rows must not double ──
    r = c.post(url, json={"seq": 2, "files": [], "stages": payload})
    check("re-sent stage list -> ok", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("a replayed stage list replaces, never doubles", len(stages_of(run)) == 2,
          str(len(stages_of(run))))

    # ── a batch that says nothing about stages leaves them alone ─────────
    r = c.post(url, json={"seq": 3, "files": [{"dir": 0, "name": file_name, "kind": "parsed"}]})
    check("a batch without `stages` -> ok", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("...and leaves the stored stages untouched",
          [(x.order, x.name) for x in stages_of(run)] == [(0, "raw"), (1, "aggregated")],
          str(stages_of(run)))

    # ── an empty list is a statement: this run has no stages ─────────────
    r = c.post(url, json={"seq": 4, "files": [], "stages": []})
    check("an explicit empty stage list clears the run's stages",
          r.status_code == 200 and stages_of(run) == [], f"{r.status_code} {stages_of(run)}")

    # ── limits and garbage ───────────────────────────────────────────────
    over = [{"order": i, "name": f"s{i}", "columns": [f"c{j}" for j in range(300)],
             "profiles": [{"column": f"c{j}", "type": "number"} for j in range(100)],
             "read_files": [f"f{j}.xlsx" for j in range(400)]}
            for i in range(40)]
    r = c.post(url, json={"seq": 5, "files": [], "stages": over})
    check("an over-budget stage list is truncated, not rejected", r.status_code == 200,
          f"{r.status_code} {r.text[:300]}")
    rows = stages_of(run)
    check(f"at most {observations.MAX_STAGES_PER_RUN} stages per run",
          len(rows) == observations.MAX_STAGES_PER_RUN, str(len(rows)))
    if rows:
        check("columns capped at MAX_STAGE_COLUMNS",
              len(rows[0].get_columns()) == observations.MAX_STAGE_COLUMNS,
              str(len(rows[0].get_columns())))
        check("profiles capped at MAX_STAGE_PROFILES",
              len(rows[0].get_profiles()) == observations.MAX_STAGE_PROFILES,
              str(len(rows[0].get_profiles())))
        check("read_files capped at MAX_STAGE_READ_FILES",
              len(rows[0].get_read_files()) == observations.MAX_STAGE_READ_FILES,
              str(len(rows[0].get_read_files())))

    # ── names trimmed, duplicate orders collapse, bad values dropped ─────
    r = c.post(url, json={"seq": 6, "files": [], "stages": [
        {"order": 0, "name": "x" * 200, "rows": -5, "ms": "soon",
         "columns": ["y" * 200, 42, "  ", "ok"], "inputs": "raw"},
        {"order": 0, "name": "second claimant"},
        {"order": 99, "name": "out of range order"},
        {"name": ""},              # no usable name -> dropped
        "not a dict",              # not a dict -> dropped
    ]})
    check("odd stage values -> ok", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    rows = stages_of(run)
    by_order = {x.order: x for x in rows}
    check("the last claimant of an order wins",
          by_order.get(0) is not None and by_order[0].name == "second claimant", str(rows))
    check("an out-of-range order falls back to the next free slot (not its "
          "position, which order 0's two claimants already used up)",
          1 in by_order and by_order[1].name == "out of range order", str(sorted(by_order)))

    # ── a fallback item never clobbers a later item's explicit claim ─────
    r = c.post(url, json={"seq": 7, "files": [], "stages": [
        {"name": "a"},                       # no order -> would collide at
        {"order": 0, "name": "b"},           # position 0 under the old rule
        {"name": "c"},
    ]})
    assert r.status_code == 200, r.text[:300]
    rows = stages_of(run)
    by_name = {x.name: x.order for x in rows}
    check("every named stage survives: none dropped, no order collision",
          set(by_name) == {"a", "b", "c"} and len(set(by_name.values())) == 3,
          str(by_name))
    check("the explicit claimant keeps its requested order",
          by_name.get("b") == 0, str(by_name))

    r = c.post(url, json={"seq": 8, "files": [], "stages": [
        {"name": "t" * 200, "rows": -5, "ms": "soon",
         "columns": ["y" * 200, 42, "  ", "ok"], "inputs": "raw"},
    ]})
    assert r.status_code == 200, r.text[:300]
    row = stages_of(run)[0]
    check("name and column names trimmed to 64 chars",
          row.name == "t" * 64 and row.get_columns() == ["y" * 64, "ok"],
          f"{len(row.name)} / {row.get_columns()}")
    check("a negative rows and a non-numeric ms become NULL",
          row.rows is None and row.ms is None, f"{row.rows} / {row.ms}")
    check("a non-list `inputs` is read as 'not declared' (NULL), not as an error",
          row.get_inputs() is None, str(row.inputs_json))

    # ── an oversized rows/ms never overflows the SQLite INTEGER column ───
    r = c.post(url, json={"seq": 9, "files": [], "stages": [
        {"name": "huge", "rows": 2 ** 70, "ms": 2 ** 70},
        {"name": "huge_float_ms", "ms": 1e30},
    ]})
    check("oversized numeric fields -> ok, not a 500", r.status_code == 200,
          f"{r.status_code} {r.text[:300]}")
    rows = stages_of(run)
    by_name = {x.name: x for x in rows}
    check("an oversized `rows` becomes NULL instead of overflowing",
          by_name["huge"].rows is None, str(by_name["huge"].rows))
    check("an oversized `ms` becomes NULL instead of overflowing",
          by_name["huge"].ms is None and by_name["huge_float_ms"].ms is None,
          f"{by_name['huge'].ms} / {by_name['huge_float_ms'].ms}")

    # ── the run's stages are its own ─────────────────────────────────────
    r = c.post(f"/api/dashboards/{dash_id}/generator/runs/{run}/error",
               json={"message": "smoke: closing the stage run"})
    assert r.status_code == 200
    run_b = start_run(2_100)
    r = c.post(f"/api/dashboards/{dash_id}/generator/runs/{run_b}/observations",
               json={"seq": 1, "final": True, "files": [], "stages": [{"name": "only"}]})
    check("a second run's stages -> ok", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("replace-all is scoped to ONE run",
          len(stages_of(run)) == 2 and [x.name for x in stages_of(run_b)] == ["only"],
          f"{stages_of(run)} / {stages_of(run_b)}")
    r = c.post(f"/api/dashboards/{dash_id}/generator/runs/{run_b}/error",
               json={"message": "smoke: closing the second stage run"})
    assert r.status_code == 200


if __name__ == "__main__":
    main()
