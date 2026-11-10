"""Smoke test for ADR-16 reference tables — app/api/reference.py (phase 2).

Boot idiom + dashboard-via-upload idiom from tests/smoke_favorites.py. Access
is exercised with admin + a plain visitor user and `consumers == []` (no
generator registered) — the fallback the phase-2 task explicitly allows,
since building a full generator-bundle + run just to get one consumer row
would not add coverage over app/services/reference.py's own service-level
smoke (tests/smoke_reference_service.py already pins diff/coerce/norm_key).
"""
import io
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-ref-api-smoke-")
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


def build_xlsx(header, rows) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append(header)
    for r in rows:
        ws.append(r)
        # openpyxl turns a leading "=" into a FORMULA; a fixture that means
        # "the analyst typed this text" has to pin the cell type itself.
        for j, v in enumerate(r, start=1):
            if isinstance(v, str) and v.startswith("="):
                ws.cell(row=ws.max_row, column=j).data_type = "s"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def do_import(c, slug, content, *, dry_run, expected_version, delete_missing="0", note=None):
    data = {
        "dry_run": "1" if dry_run else "0",
        "expected_version": str(expected_version),
        "delete_missing": delete_missing,
    }
    if note is not None:
        data["note"] = note
    return c.post(f"/api/reference/{slug}/import", data=data,
                  files={"file": ("data.xlsx", content,
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})


HEADER = ["BV", "Code", "Name", "Qty"]
SLUG = "nwd_bv_map"


def main():
    with TestClient(app) as c:
        login(c, "admin1")
        r = c.post("/api/admin/users", json={"person_id": "vis1", "role": "visitor", "display_name": "Vis One"})
        check("second user created", r.status_code == 200 and r.json().get("ok") is True, f"{r.status_code} {r.text[:200]}")

        # ── create ───────────────────────────────────────────────────
        r = c.post("/api/reference", json={
            "slug": SLUG, "title": "NWD BV → code and name",
            "columns": [
                {"name": "BV", "type": "text"},
                {"name": "Code", "type": "text"},
                {"name": "Name", "type": "text"},
                {"name": "Qty", "type": "number"},
            ],
        })
        check("admin create with columns -> 201", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
        body = r.json()
        check("create response has current_version 0", body.get("current_version") == 0, str(body))
        check("create response consumers == 0 (no generator registered)", body.get("consumers") == 0, str(body))

        r = c.post("/api/reference", json={
            "slug": SLUG, "title": "dup",
            "columns": [{"name": "K", "type": "text"}, {"name": "V", "type": "text"}],
        })
        check("dup slug -> 409", r.status_code == 409, f"{r.status_code} {r.text[:200]}")

        # ── plain user: no reach to create ──────────────────────────
        login(c, "vis1")
        r = c.post("/api/reference", json={
            "slug": "other_table", "title": "x",
            "columns": [{"name": "K", "type": "text"}, {"name": "V", "type": "text"}],
        })
        check("plain user with no generator reach -> 403 on create", r.status_code == 403, f"{r.status_code} {r.text[:200]}")

        # zero consumers -> only creator/admin can read, not an unrelated visitor
        r = c.get(f"/api/reference/{SLUG}")
        check("plain user (not creator, zero consumers) -> 403 on read", r.status_code == 403, f"{r.status_code}")

        login(c, "admin1")

        # ── import: dry-run -> commit -> v1 ─────────────────────────
        wb1 = build_xlsx(HEADER, [
            ["BV-1", "K1", "Name One", 1],
            ["BV 2", "K2", "Name Two", 2],
            ["BV-3", "K3", "Name Three", 3],
        ])
        r = do_import(c, SLUG, wb1, dry_run=True, expected_version=0)
        check("dry-run import #1 -> 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        d1 = r.json()
        check("dry-run #1: added 3", d1.get("added") == 3, str(d1))
        check("dry-run #1: can_commit true", d1.get("can_commit") is True, str(d1))
        check("dry-run #1: table unaffected (still v0)", d1.get("version") == 0, str(d1))

        # table's current_version must not have moved during dry-run
        r = c.get(f"/api/reference/{SLUG}")
        check("table still v0 after dry-run", r.json().get("current_version") == 0, r.text[:200])

        r = do_import(c, SLUG, wb1, dry_run=False, expected_version=0, note="initial load")
        check("commit import #1 -> 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        c1 = r.json()
        check("commit #1: version 1", c1.get("version") == 1, str(c1))
        check("commit #1: added 3, dry_run false", c1.get("added") == 3 and c1.get("dry_run") is False, str(c1))

        # ── import #2: changed value + nbsp-variant key + bad number ──
        wb2 = build_xlsx(HEADER, [
            ["BV-1", "K1", "Name One CHANGED", 1],   # deliberate value change
            ["BV\xa02", "K2", "Name Two", 2],         # nbsp in place of the space -> same norm_key as "BV 2"
            ["BV-3", "K3", "Name Three", "not-a-number"],  # bad number cell
        ])
        r = do_import(c, SLUG, wb2, dry_run=True, expected_version=1)
        check("dry-run import #2 -> 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        d2 = r.json()
        check("dry-run #2: added 0 (nbsp variant matched existing key, not new)", d2.get("added") == 0, str(d2))
        check("dry-run #2: at least 1 changed", d2.get("changed", 0) >= 1, str(d2))
        check("dry-run #2: exactly 1 row error (bad number)", len(d2.get("errors") or []) == 1, str(d2))
        check("dry-run #2: can_commit false", d2.get("can_commit") is False, str(d2))

        r = do_import(c, SLUG, wb2, dry_run=False, expected_version=1)
        check("commit import #2 with errors -> 400", r.status_code == 400, f"{r.status_code} {r.text[:300]}")
        detail = r.json().get("detail") or {}
        check("400 payload carries the same shape (errors present)", len(detail.get("errors") or []) == 1, str(detail))

        r = c.get(f"/api/reference/{SLUG}")
        check("table still v1 after failed commit", r.json().get("current_version") == 1, r.text[:200])

        # ── import #3: fixed data + delete_missing removes BV-3 ────────
        wb3 = build_xlsx(HEADER, [
            ["BV-1", "K1", "Name One CHANGED", 1],
            ["BV 2", "K2", "Name Two", 2],
        ])
        r = do_import(c, SLUG, wb3, dry_run=False, expected_version=1, delete_missing="1", note="fix + prune")
        check("commit import #3 (delete_missing) -> 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        c3 = r.json()
        check("commit #3: version 2", c3.get("version") == 2, str(c3))
        check("commit #3: removed 1 (BV-3 dropped)", c3.get("removed") == 1, str(c3))
        check("commit #3: changed >= 1 (BV-1 value)", c3.get("changed", 0) >= 1, str(c3))

        r = c.get(f"/api/reference/{SLUG}?limit=10")
        rows_now = r.json().get("rows") or []
        keys_now = {row[0] for row in rows_now}
        check("BV-3 no longer present after delete_missing", "BV-3" not in keys_now, str(keys_now))
        check("row_total is 2 after delete_missing", r.json().get("row_total") == 2, str(r.json().get("row_total")))

        # ── expected_version stale -> 409 ───────────────────────────
        r = do_import(c, SLUG, wb3, dry_run=False, expected_version=1)
        check("stale expected_version -> 409", r.status_code == 409, f"{r.status_code} {r.text[:300]}")
        check("409 payload carries current version", r.json().get("detail", {}).get("current") == 2, r.text[:200])

        # ── key > 64 chars -> error row ──────────────────────────────
        long_key = "X" * 70
        wb_long = build_xlsx(HEADER, [[long_key, "K", "N", 1]])
        r = do_import(c, SLUG, wb_long, dry_run=True, expected_version=2)
        check("dry-run with 65+ char key -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        dl = r.json()
        check("65+ char key -> row error, can_commit false",
              len(dl.get("errors") or []) >= 1 and dl.get("can_commit") is False, str(dl))

        # ── oversized upload -> 413 ──────────────────────────────────
        big = b"PK\x03\x04" + os.urandom(8 * 1024 * 1024)
        r = c.post(f"/api/reference/{SLUG}/import",
                   data={"dry_run": "1", "expected_version": "2", "delete_missing": "0"},
                   files={"file": ("big.xlsx", big, "application/octet-stream")})
        check("8 MB upload -> 413", r.status_code == 413, f"{r.status_code} {r.text[:200]}")

        # ── non-zip upload -> 415 ────────────────────────────────────
        r = c.post(f"/api/reference/{SLUG}/import",
                   data={"dry_run": "1", "expected_version": "2", "delete_missing": "0"},
                   files={"file": ("plain.xlsx", b"this is not a zip file at all", "text/plain")})
        check("non-zip upload -> 415", r.status_code == 415, f"{r.status_code} {r.text[:200]}")

        # ── a key repeated in the file blocks the commit ──────────────
        # diff_rows drops BOTH copies of an ambiguous key; letting the commit
        # through would silently skip the row (delete_missing=0) or silently
        # DELETE the existing one (delete_missing=1).
        wb_dup = build_xlsx(HEADER, [
            ["BV-1", "K1", "Name One CHANGED", 1],
            ["BV 2", "K2", "Name Two", 2],
            ["bv-1", "K9", "Duplicate spelling", 9],   # same norm_key as BV-1
        ])
        r = do_import(c, SLUG, wb_dup, dry_run=True, expected_version=2)
        dd = r.json()
        check("dry-run with duplicate key: duplicates reported",
              dd.get("duplicates") == ["bv-1"], str(dd))
        check("dry-run with duplicate key: can_commit false",
              dd.get("can_commit") is False, str(dd))
        r = do_import(c, SLUG, wb_dup, dry_run=False, expected_version=2, delete_missing="1")
        check("commit with duplicate key -> 400", r.status_code == 400, f"{r.status_code} {r.text[:300]}")
        r = c.get(f"/api/reference/{SLUG}?limit=10")
        check("table untouched after refused duplicate commit (still v2)",
              r.json().get("current_version") == 2, r.text[:200])
        check("BV-1 survived the refused duplicate+delete_missing commit",
              "BV-1" in {row[0] for row in (r.json().get("rows") or [])}, r.text[:300])

        # ── restoring "v0" is not a way to blank the table ─────────────
        r = c.post(f"/api/reference/{SLUG}/versions/0/restore")
        check("restore v0 -> 404 (v0 is 'never imported', not a version)",
              r.status_code == 404, f"{r.status_code} {r.text[:200]}")

        # ── row ceiling is enforced while parsing, not after ───────────
        from app.services import reference as _refsvc
        wb_many = build_xlsx(HEADER, [[f"K{i}", "c", "n", 1]
                                      for i in range(_refsvc.MAX_ROWS + 5)])
        r = do_import(c, SLUG, wb_many, dry_run=True, expected_version=2)
        check("dry-run over MAX_ROWS -> 400 (capped during parse)",
              r.status_code == 400, f"{r.status_code} {r.text[:200]}")

        # ── keys endpoint: sorted normalised ─────────────────────────
        r = c.get(f"/api/reference/{SLUG}/versions/current/keys")
        check("keys endpoint -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        kd = r.json()
        check("keys: version matches current (2)", kd.get("version") == 2, str(kd))
        keys = kd.get("keys") or []
        check("keys: sorted", keys == sorted(keys), str(keys))
        check("keys: normalised (bv 2, not 'BV 2')", "bv 2" in keys and "BV 2" not in keys, str(keys))

        # ── xlsx export round-trips ───────────────────────────────────
        r = c.get(f"/api/reference/{SLUG}/versions/current/xlsx")
        check("xlsx export -> 200", r.status_code == 200, f"{r.status_code}")
        check("xlsx export: attachment header names the file",
              f"{SLUG}-v2.xlsx" in (r.headers.get("content-disposition") or ""),
              r.headers.get("content-disposition"))
        from openpyxl import load_workbook
        wb_loaded = load_workbook(io.BytesIO(r.content))
        ws_loaded = wb_loaded["data"]
        header_row = [cell.value for cell in next(ws_loaded.iter_rows(min_row=1, max_row=1))]
        check("xlsx export: header has _key_norm trailing column",
              header_row == HEADER + ["_key_norm"], str(header_row))
        data_rows = list(ws_loaded.iter_rows(min_row=2, values_only=True))
        check("xlsx export: 2 data rows", len(data_rows) == 2, str(data_rows))
        qty_values = [row[3] for row in data_rows]
        check("xlsx export: numeric cell stays numeric (not str)",
              all(isinstance(v, (int, float)) for v in qty_values), str(qty_values))

        # ── template with keys ────────────────────────────────────────
        # NEWLINE-separated, matching quality._template_url. A reference key
        # may legitimately hold a comma ('Acme "North", LLC'), so splitting on
        # one produced ONE bogus row holding the whole joined blob.
        r = c.get(f"/api/reference/{SLUG}/template",
                  params={"unmapped": "1", "keys": "New-1\nNew-2"})
        check("template with keys -> 200", r.status_code == 200, f"{r.status_code}")
        wb_tpl = load_workbook(io.BytesIO(r.content))
        ws_tpl = wb_tpl["data"]
        tpl_rows = list(ws_tpl.iter_rows(min_row=2, values_only=True))
        check("template: one row per key", len(tpl_rows) == 2, str(tpl_rows))
        check("template: key filled, rest empty",
              tpl_rows[0][0] == "New-1" and all(v is None for v in tpl_rows[0][1:-1]), str(tpl_rows))

        r = c.get(f"/api/reference/{SLUG}/template",
                  params={"unmapped": "1", "keys": 'Acme "North", LLC\nOther'})
        tpl_c = list(load_workbook(io.BytesIO(r.content))["data"]
                     .iter_rows(min_row=2, values_only=True))
        check("template: a key containing a comma stays ONE key",
              len(tpl_c) == 2 and tpl_c[0][0] == 'Acme "North", LLC', str(tpl_c))

        r = c.get(f"/api/reference/{SLUG}/template")
        wb_tpl0 = load_workbook(io.BytesIO(r.content))
        check("template header-only (no unmapped) -> 0 data rows",
              len(list(wb_tpl0["data"].iter_rows(min_row=2))) == 0)

        # ── a value that starts with "=" exports as text, not a formula ──
        wb_eq = build_xlsx(HEADER, [
            ["BV-1", "K1", "=cmd|'/c calc'!A1", 1],
            ["BV 2", "K2", "Name Two", 2],
        ])
        r = do_import(c, SLUG, wb_eq, dry_run=False, expected_version=2)
        check("commit import with a leading-'=' value -> 200", r.status_code == 200,
              f"{r.status_code} {r.text[:300]}")
        v_eq = r.json().get("version")
        r = c.get(f"/api/reference/{SLUG}/versions/{v_eq}/xlsx")
        from openpyxl import load_workbook as _lw
        ws_eq = _lw(io.BytesIO(r.content))["data"]
        cells_eq = [row[2] for row in ws_eq.iter_rows(min_row=2)]
        check("export: leading-'=' value is a string cell, never a formula",
              all(cell.data_type == "s" for cell in cells_eq),
              str([(cell.value, cell.data_type) for cell in cells_eq]))
        check("export: the '=' text round-trips verbatim",
              "=cmd|'/c calc'!A1" in [cell.value for cell in cells_eq],
              str([cell.value for cell in cells_eq]))

        # ── restore v1 -> new version equal to v1's rows ────────────────
        r = c.post(f"/api/reference/{SLUG}/versions/1/restore")
        check("restore v1 -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        rr = r.json()
        check("restore creates version 4", rr.get("version") == 4, str(rr))
        check("restore: row_count matches v1 (3 rows)", rr.get("row_count") == 3, str(rr))

        from app.database import SessionLocal
        from app.models import ReferenceTable, ReferenceTableVersion
        db = SessionLocal()
        try:
            table_row = db.query(ReferenceTable).filter(ReferenceTable.slug == SLUG).first()
            v1 = db.query(ReferenceTableVersion).filter(
                ReferenceTableVersion.table_id == table_row.id, ReferenceTableVersion.version_number == 1).first()
            v4 = db.query(ReferenceTableVersion).filter(
                ReferenceTableVersion.table_id == table_row.id, ReferenceTableVersion.version_number == 4).first()
            check("restore: v4 rows_json == v1 rows_json", v1.rows_json == v4.rows_json,
                  f"{v1.rows_json} != {v4.rows_json}")
            check("table.current_version is now 4", table_row.current_version == 4, str(table_row.current_version))
        finally:
            db.close()

        # ── PUT update (title/allow_pin/responsible) ───────────────────
        r = c.put(f"/api/reference/{SLUG}", json={"title": "NWD BV (renamed)", "allow_pin": True})
        check("PUT update -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        ub = r.json()
        check("PUT update: title changed", ub.get("title") == "NWD BV (renamed)", str(ub))
        check("PUT update: allow_pin true", ub.get("allow_pin") is True, str(ub))

        # The typeahead (static/js/user-suggest.js) fills the "Responsible"
        # field with an PERSON_ID, not a numeric users.id — a body that only
        # understood the number silently assigned NOBODY every time.
        r = c.put(f"/api/reference/{SLUG}", json={"responsible_person_id": "admin1"})
        check("PUT responsible by person_id -> 200", r.status_code == 200,
              f"{r.status_code} {r.text[:200]}")
        resp = (r.json() or {}).get("responsible")
        check("PUT responsible by person_id actually assigns somebody",
              isinstance(resp, dict) and resp.get("id"), str(resp))
        r = c.put(f"/api/reference/{SLUG}", json={"responsible_person_id": "nope-nobody"})
        check("PUT responsible with an unknown person_id -> 400", r.status_code == 400,
              f"{r.status_code} {r.text[:200]}")
        r = c.put(f"/api/reference/{SLUG}", json={"responsible_user_id": None})
        check("PUT responsible cleared -> 200 and nobody",
              r.status_code == 200 and (r.json() or {}).get("responsible") is None,
              f"{r.status_code} {r.text[:200]}")

        # ── versions listing ────────────────────────────────────────
        r = c.get(f"/api/reference/{SLUG}/versions")
        check("versions listing -> 200", r.status_code == 200, f"{r.status_code}")
        vs = r.json().get("versions") or []
        check("versions listing has 4 entries (v1, v2, v3, v4-restore)", len(vs) == 4, str(vs))
        check("versions listing newest first", vs[0]["number"] == 4, str(vs))

        # ── responsible user may READ what they may WRITE ─────────────
        # The real shape needs a consuming dashboard the responsible user
        # cannot reach; `_consumers` is stubbed rather than building a whole
        # generator bundle + run, which is what the module docstring above
        # already rules out of this smoke's scope.
        from app.api import reference as refapi
        from app.database import SessionLocal as _SL
        from app.models import User as _User
        _db = _SL()
        try:
            _t = refapi.refsvc.get_table(_db, SLUG)
            _vis = _db.query(_User).filter(_User.person_id == "vis1").first()
            _t.responsible_user_id = _vis.id
            _real = refapi._consumers
            refapi._consumers = lambda db, slug: [
                {"dashboard_id": "no-such-dashboard", "title": "x",
                 "generator_id": 1, "generator_name": "g", "last_run": None}]
            try:
                check("responsible user can write an unreachable-consumer table",
                      refapi._can_write(_db, _vis, _t) is True)
                check("responsible user can also READ it (no write-without-read)",
                      refapi._can_read(_db, _vis, _t) is True)
            finally:
                refapi._consumers = _real
                _t.responsible_user_id = None
        finally:
            _db.close()

        # ── 404s ─────────────────────────────────────────────────────
        r = c.get("/api/reference/does-not-exist")
        check("unknown slug -> 404", r.status_code == 404, str(r.status_code))

        # ── audit rows exist ─────────────────────────────────────────
        db = SessionLocal()
        try:
            from app.models import AuditLog
            actions = {row.action for row in
                       db.query(AuditLog).filter(AuditLog.action.like("reference%")).all()}
            check("audit has reference_create", "reference_create" in actions, str(actions))
            check("audit has reference_import", "reference_import" in actions, str(actions))
            check("audit has reference_restore", "reference_restore" in actions, str(actions))
            check("audit has reference_update", "reference_update" in actions, str(actions))
        finally:
            db.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
