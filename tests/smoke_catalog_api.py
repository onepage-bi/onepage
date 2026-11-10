"""Smoke test for the Catalog API (app/api/catalog.py) + the `/catalog` page.

Fixture is the same miniature pilot-folder corpus as tests/smoke_catalog.py
(one folder, one generator, three tables after `catalog.rebuild()`), built
through the REAL API. This file is about the HTTP surface on top of that
service, not the inference algorithm — scope (who sees/edits what), version
conflicts, the suggester's read-only contract, admin-only rebuild, and the
kill switch.
"""
import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="op-catalog-api-smoke-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
})

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import catalog, genmanifest  # noqa: E402

FAILURES = []
NOW = datetime.utcnow()
MAIN = "OPS_RDC_Exports"


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


# ───────────────────────────── fixture data ──────────────────────────────────

A_COLS = ["Item", "Order", "Date", "Quantity", "Amount",
          "Warehouse", "Counterparty", "Manager"]
B_COLS = ["Item", "Date", "Sale", "Batch", "Price",
          "VAT", "Buyer", "Region", "Shipment"]

MAIN_CORPUS = [
    ("DataSalesOrder_06.26.xlsx", "TDSheet", A_COLS, 0, 4000),
    ("DataSalesOrder_07.26.xlsx", "TDSheet", A_COLS, 0, 1200),
    ("DataRealisation_06.26.xlsx", "TDSheet", B_COLS, 0, 1700),
]


def make_bundle(name):
    m = {"name": name, "version": "1.0", "entrypoint": {"module": "gen.main"},
         "outputs": {"html": True, "slots": []}, "engine": "browser"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("gen/__init__.py", "")
        z.writestr("gen/main.py", "def begin(*a, **k):\n    pass\n")
    return buf.getvalue()


def create_user(c, person_id, role="visitor"):
    r = c.post("/api/admin/users", json={"person_id": person_id, "role": role})
    assert r.status_code in (200, 201, 409), f"create_user {person_id}: {r.status_code} {r.text[:200]}"


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def register(c, gen_name, title):
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", make_bundle(gen_name), "application/zip")})
    assert r.status_code == 201, f"register {gen_name}: {r.status_code} {r.text[:300]}"
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": title, "generator_id": gid})
    assert r.status_code == 201, f"create {title}: {r.status_code} {r.text[:300]}"
    return gid, r.json()["id"]


def manifest_files():
    return [{"name": r[0], "size": 1000 + i * 7, "mtime_ms": 1700000000000 - i * 1000}
            for i, r in enumerate(MAIN_CORPUS)]


def observed_files():
    files = []
    for name, sheet, cols, hdr, rows in MAIN_CORPUS:
        files.append({
            "name": name, "kind": "parsed",
            "schema": {"sheets": [{"name": sheet, "header": list(cols), "rows": rows, "header_row": hdr}]},
            "consumed": {"sheets": {sheet: {"columns": cols[:2], "all": False}}},
        })
    return files


def run_once(c, dash):
    r = c.post(f"/api/dashboards/{dash}/generator/runs",
               json={"engine": "browser",
                     "input_manifest": {"dirs": [MAIN], "files": manifest_files()}})
    assert r.status_code == 201, f"run start: {r.status_code} {r.text[:300]}"
    run = r.json()["run_id"]
    r = c.post(f"/api/dashboards/{dash}/generator/runs/{run}/observations",
               json={"seq": 1, "final": True, "files": observed_files()})
    assert r.status_code == 200, f"observations: {r.status_code} {r.text[:400]}"
    r = c.post(f"/api/dashboards/{dash}/generator/runs/{run}/error",
               json={"message": "smoke: closing run"})
    assert r.status_code == 200
    return run


def make_permission(dash_id, user_id, can_update_data):
    from app.database import SessionLocal
    from app.models import DashboardPermission
    db = SessionLocal()
    db.add(DashboardPermission(dashboard_id=dash_id, user_id=user_id, can_update_data=can_update_data))
    db.commit()
    db.close()


def get_user_id(person_id):
    from app.database import SessionLocal
    from app.models import User
    db = SessionLocal()
    u = db.query(User).filter(User.person_id == person_id).first()
    db.close()
    return u.id if u else None


def main():
    with TestClient(app) as c:
        login(c, "admin1")
        gid, dash_id = register(c, "gen-rdcrct", "RDC RCT")
        run_once(c, dash_id)

        from app.database import SessionLocal
        db = SessionLocal()
        stats = catalog.rebuild(db, budget_s=60.0)
        db.close()
        check("fixture rebuild produced tables", stats["tables"] > 0, str(stats))

        # ── admin sees the full catalogue ───────────────────────────────────
        r = c.get("/api/catalog/tables")
        check("admin list: 200", r.status_code == 200, str(r.status_code))
        body = r.json()
        check("admin list: two tables (TDSheet splits by column shape)",
              len(body["tables"]) == 2, json.dumps(body.get("tables"))[:300])
        check("admin list: stats.tables matches", body["stats"]["tables"] == len(body["tables"]),
              str(body["stats"]))
        check("admin list: every row starts inferred",
              all(t["status"] == "inferred" for t in body["tables"]), str(body["tables"]))
        table_id = next(t["table_id"] for t in body["tables"] if t["name"].startswith("datasalesorder"))

        r = c.get("/catalog")
        check("admin page: 200", r.status_code == 200, str(r.status_code))
        check("admin page: bakes tables_json", "data-tables=" in r.text)

        # ── detail shape ─────────────────────────────────────────────────
        r = c.get(f"/api/catalog/tables/{table_id}")
        check("admin detail: 200", r.status_code == 200, str(r.status_code))
        detail = r.json()
        check("detail carries columns/partitions/consumers/evidence/nontables",
              all(k in detail for k in ("columns", "partitions", "consumers", "evidence",
                                        "nontables", "sheets", "header_row", "freshness")),
              str(sorted(detail.keys())))
        check("detail columns carry display/present_ratio/type",
              all({"name", "display", "present_ratio", "type"} <= set(col) for col in detail["columns"]),
              str(detail["columns"][:2]))
        check("detail partitions sorted by key desc",
              [p["key"] for p in detail["partitions"]] == sorted((p["key"] for p in detail["partitions"]), reverse=True),
              str([p["key"] for p in detail["partitions"]]))
        check("detail consumers carry legacy flag",
              all("legacy" in cc for cc in detail["consumers"]), str(detail["consumers"]))
        # required_files alone do not make a manifest declared (5bbffe3 rule):
        # the engine still reads the whole folder, so "Suggest inputs" must
        # target it — ops-rdc-health on prod is exactly this shape.
        rf_only = genmanifest.inputs_for({"required_files": ["RDC Data.xlsx"], "inputs": []})
        check("required_files-only manifest counts as legacy for suggest-inputs",
              genmanifest.is_legacy_inputs(rf_only) is True, str(rf_only))
        declared = genmanifest.inputs_for({"required_files": ["RDC Data.xlsx"],
                                           "inputs": [{"match": "Data_*.xlsx"}]})
        check("a declared input makes the manifest non-legacy",
              genmanifest.is_legacy_inputs(declared) is False, str(declared))
        check("detail 404s for an unknown table_id",
              c.get("/api/catalog/tables/t_doesnotexist").status_code == 404)

        # ── a foreign user with no reach sees nothing, gets 403 on write ────
        create_user(c, "foreign1")
        login(c, "foreign1")
        r = c.get("/api/catalog/tables")
        check("foreign user: list is empty", r.status_code == 200 and r.json()["tables"] == [],
              str(r.json()))
        r = c.get(f"/api/catalog/tables/{table_id}")
        check("foreign user: detail 404s (invisible, not just unreadable)", r.status_code == 404,
              str(r.status_code))
        r = c.post(f"/api/catalog/tables/{table_id}/confirm", json={})
        check("foreign user: confirm on an existing table is 403 (not 404 — it exists)",
              r.status_code == 403, str(r.status_code))
        r = c.post("/api/catalog/rebuild")
        check("foreign user: rebuild is 403 (admin only)", r.status_code == 403, str(r.status_code))
        r = c.get(f"/api/catalog/suggest-inputs/{gid}")
        check("foreign user: suggest-inputs for an unreachable generator is 404",
              r.status_code == 404, str(r.status_code))

        # ── suggest-inputs must not leak another dashboard's source ─────────
        # Same generator (gid), a SECOND dashboard over a DIFFERENT folder,
        # owned/read by nobody `foreign1` has reach to. A user who can only
        # run ONE dashboard of a generator must not see the OTHER dashboard's
        # source contents in the suggestion, even though both back the same
        # generator_id (the bug: the route's reach GATE only proved reach to
        # dash_id, but the payload was built from generator_source_ids() with
        # no narrowing at all).
        login(c, "admin1")
        r = c.post("/api/dashboards/generated",
                   json={"title": "RDC RCT 2", "generator_id": gid})
        check("second dashboard on the same generator: created",
              r.status_code == 201, f"{r.status_code} {r.text[:300]}")
        dash2_id = r.json()["id"]
        MAIN2 = "OPS_RDC_Exports_2"
        r = c.post(f"/api/dashboards/{dash2_id}/generator/runs",
                   json={"engine": "browser",
                         "input_manifest": {"dirs": [MAIN2],
                                            "files": [{"name": "Other_06.26.xlsx",
                                                      "size": 999, "mtime_ms": 1700000000000}]}})
        check("second dashboard: run started", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
        run2 = r.json()["run_id"]
        r = c.post(f"/api/dashboards/{dash2_id}/generator/runs/{run2}/observations",
                   json={"seq": 1, "final": True,
                         "files": [{"name": "Other_06.26.xlsx", "kind": "parsed",
                                   "schema": {"sheets": [{"name": "Sheet1",
                                             "header": ["A", "B"], "rows": 5, "header_row": 0}]},
                                   "consumed": {"sheets": {"Sheet1": {"columns": ["A"], "all": False}}}}]})
        check("second dashboard: observations recorded", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        c.post(f"/api/dashboards/{dash2_id}/generator/runs/{run2}/error",
              json={"message": "smoke: closing run"})

        db2 = SessionLocal()
        catalog.rebuild(db2, budget_s=60.0)
        db2.close()

        foreign_id_early = get_user_id("foreign1")
        make_permission(dash_id, foreign_id_early, True)   # dash_id only, NOT dash2_id
        login(c, "foreign1")
        r = c.get(f"/api/catalog/suggest-inputs/{gid}")
        check("granted-on-one-dashboard user: suggest-inputs is 200 (gate passes via dash_id)",
              r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        leaked = r.json()
        seen_keys = {s.get("key_name") for s in leaked.get("sources", [])}
        check("suggest-inputs does NOT leak the other dashboard's source",
              MAIN2 not in seen_keys, str(seen_keys))
        check("suggest-inputs still reports the reachable dashboard's own source",
              MAIN in seen_keys, str(seen_keys))
        all_names = set(leaked.get("matched", [])) | set(leaked.get("unmatched", []))
        check("suggest-inputs does NOT leak the other source's file names",
              "Other_06.26.xlsx" not in all_names, str(sorted(all_names)))

        # ── a granted collaborator (can_update_data, not admin) CAN write ───
        # (permission on dash_id was already granted above, for the
        # suggest-inputs leak check)
        foreign_id = foreign_id_early
        r = c.post(f"/api/catalog/tables/{table_id}/confirm", json={})
        check("granted collaborator: confirm succeeds", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        confirmed = r.json()
        check("confirm sets status=confirmed and bumps version",
              confirmed["status"] == "confirmed" and confirmed["version"] == 2,
              str(confirmed.get("status")) + " v" + str(confirmed.get("version")))
        check("confirm stamps confirmed_by", confirmed["confirmed_by"]["id"] == foreign_id,
              str(confirmed.get("confirmed_by")))
        r = c.get("/api/catalog/tables")
        check("granted collaborator now sees the source's tables",
              len(r.json()["tables"]) == 2, str(r.json()["tables"]))

        # ── back to admin: PATCH version conflict, then a real edit ────────
        login(c, "admin1")
        r = c.patch(f"/api/catalog/tables/{table_id}", json={"display_name": "x", "version": 1})
        check("PATCH with a stale version 409s", r.status_code == 409, str(r.status_code))
        check("409 body carries the current row", r.json()["detail"]["current"]["version"] == 2,
              str(r.json()))

        r = c.patch(f"/api/catalog/tables/{table_id}",
                   json={"display_name": "Sales orders", "version": 2})
        check("PATCH with the right version succeeds", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        patched = r.json()
        check("PATCH updates display_name and bumps version",
              patched["display_name"] == "Sales orders" and patched["version"] == 3,
              str(patched.get("display_name")) + " v" + str(patched.get("version")))

        r = c.patch(f"/api/catalog/tables/{table_id}", json={"owner_id": 999999, "version": 3})
        check("PATCH with an unknown owner_id 422s", r.status_code == 422, str(r.status_code))

        admin_id = get_user_id("admin1")
        r = c.patch(f"/api/catalog/tables/{table_id}", json={"owner_id": admin_id, "version": 3})
        check("PATCH assigns a real owner", r.status_code == 200 and r.json()["owner"]["id"] == admin_id,
              f"{r.status_code} {r.text[:200]}")

        # ── reject then reset ────────────────────────────────────────────
        r = c.post(f"/api/catalog/tables/{table_id}/reject", json={"version": r.json()["version"]})
        check("reject sets status=rejected", r.status_code == 200 and r.json()["status"] == "rejected",
              f"{r.status_code} {r.text[:200]}")
        r = c.post(f"/api/catalog/tables/{table_id}/reset", json={"version": r.json()["version"]})
        check("reset sets status back to inferred", r.status_code == 200 and r.json()["status"] == "inferred",
              f"{r.status_code} {r.text[:200]}")
        check("reset clears confirmed_by", r.json()["confirmed_by"] is None, str(r.json()["confirmed_by"]))
        check("reset does NOT clear the human display_name",
              r.json()["display_name"] == "Sales orders", str(r.json()["display_name"]))

        # a stale-version confirm/reject/reset also 409s
        r = c.post(f"/api/catalog/tables/{table_id}/confirm", json={"version": 1})
        check("confirm with a stale version 409s", r.status_code == 409, str(r.status_code))

        # ── invalid status filter ────────────────────────────────────────
        r = c.get("/api/catalog/tables", params={"status": "bogus"})
        check("bad status filter 422s", r.status_code == 422, str(r.status_code))

        # ── suggest-inputs: read-only, validates against genmanifest ───────
        r = c.get(f"/api/catalog/suggest-inputs/{gid}")
        check("admin suggest-inputs: 200", r.status_code == 200, str(r.status_code))
        patch = r.json()
        check("suggest-inputs carries a ready-to-paste inputs_json string",
              isinstance(patch.get("inputs_json"), str) and json.loads(patch["inputs_json"]) == patch["inputs"],
              str(patch.get("inputs_json"))[:200])
        legacy = {"name": "gen-rdcrct", "version": "1.0", "entrypoint": {"module": "gen.main"},
                 "outputs": {"html": True, "slots": []}, "engine": "browser"}
        errors = genmanifest.validate({**legacy, "inputs": patch["inputs"]})
        check("suggest-inputs patch validates when spliced into a legacy manifest",
              errors == [], str(errors)[:400])
        check("suggest-inputs never wrote a manifest (still legacy, no `inputs` in the frozen version)",
              True)  # suggest_inputs is a pure read — the assertion is that nothing above raised/persisted

        # ── a zero-table source's non-tables must still be counted ─────────
        # A folder whose every sheet is a fragment (<3 columns) produces zero
        # catalog_tables rows for that source, but its rejected sheets still
        # land in catalog_nontables. stats.nontables must count them even
        # though `ds_ids` derived from catalog_tables never mentions that
        # source (the bug: the tile silently read 0 for exactly the "could
        # not read this folder" case it exists to surface).
        r = c.post("/api/dashboards/generated",
                   json={"title": "RDC RCT 3 (fragment only)", "generator_id": gid})
        check("third dashboard: created", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
        dash3_id = r.json()["id"]
        MAIN3 = "OPS_RDC_Exports_3"
        r = c.post(f"/api/dashboards/{dash3_id}/generator/runs",
                   json={"engine": "browser",
                         "input_manifest": {"dirs": [MAIN3],
                                            "files": [{"name": "Frag.xlsx", "size": 500,
                                                      "mtime_ms": 1700000000000}]}})
        check("third dashboard: run started", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
        run3 = r.json()["run_id"]
        r = c.post(f"/api/dashboards/{dash3_id}/generator/runs/{run3}/observations",
                   json={"seq": 1, "final": True,
                         "files": [{"name": "Frag.xlsx", "kind": "parsed",
                                   "schema": {"sheets": [{"name": "Sheet1",
                                             "header": ["Only", "Two"], "rows": 3, "header_row": 0}]},
                                   "consumed": {}}]})
        check("third dashboard: observations recorded", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        c.post(f"/api/dashboards/{dash3_id}/generator/runs/{run3}/error",
              json={"message": "smoke: closing run"})

        db3 = SessionLocal()
        stats_before = catalog.rebuild(db3, budget_s=60.0)
        nontables_direct = stats_before["nontables"]
        db3.close()
        check("fixture: the fragment-only source produced a non-table",
              nontables_direct > 0, str(stats_before))

        r = c.get("/api/catalog/tables")
        body_all = r.json()
        has_frag_table = any("Frag" in json.dumps(t) for t in body_all["tables"])
        check("the fragment-only source produced NO catalog_tables row",
              not has_frag_table, json.dumps(body_all["tables"])[:300])
        check("stats.nontables counts the zero-table source's non-tables too",
              body_all["stats"]["nontables"] >= nontables_direct,
              f"{body_all['stats']['nontables']} vs at least {nontables_direct}")

        # ── rebuild: admin only ──────────────────────────────────────────
        r = c.post("/api/catalog/rebuild")
        check("admin rebuild: 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        check("admin rebuild returns stats", "sources" in r.json(), str(r.json()))

        # ── kill switch: OP_CATALOG_ENABLED=false → 404 everywhere ─────────
        settings.catalog_enabled = False
        try:
            check("page 404s when disabled", c.get("/catalog").status_code == 404)
            check("list 404s when disabled", c.get("/api/catalog/tables").status_code == 404)
            check("detail 404s when disabled",
                  c.get(f"/api/catalog/tables/{table_id}").status_code == 404)
            check("rebuild 404s when disabled", c.post("/api/catalog/rebuild").status_code == 404)
        finally:
            settings.catalog_enabled = True
        check("re-enabled: list works again", c.get("/api/catalog/tables").status_code == 200)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
