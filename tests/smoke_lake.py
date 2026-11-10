"""Regression: the server lake cache (Phase 9.1)
 — row-level Parquet
partitions of a report's source folder, stored ON THE SERVER, written by the
report's generator run, readable by anyone with dashboard access.

Pins:
  * access — `POST/PUT` need `can_update_dashboard_data` (403 otherwise);
    `GET` needs `require_dashboard_access` (403/404 otherwise); `DELETE`
    needs owner-or-administrator;
  * `key` shape (`^[a-f0-9]{16,64}$`, else 422);
  * magic-byte sniff (`PAR1`, else 415) and the two size ceilings (per-part
    `OP_LAKE_PART_MAX_MB`, whole-dir `OP_LAKE_MAX_MB`), both enforced BEFORE
    or immediately after the write — an over-cap part is never kept;
  * manifest publish: every listed key must already exist as a part (422
    listing what's missing), and publishing prunes every `.parquet` the new
    manifest does not list (only the LATEST run is kept);
  * part delivery headers: identity encoding, nosniff, private cache,
    `ETag: "<key>"`, 304 on `If-None-Match`;
  * DELETE removes the whole dir and is audited.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-lake-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_SAML_SP_BASE_URL": "http://testserver",
    # Small caps so 413s are reachable with tiny payloads.
    "OP_LAKE_PART_MAX_MB": "1",
    "OP_LAKE_MAX_MB": "2",
})

sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


PAR1 = b"PAR1" + b"\x00" * 60  # never parsed by the server — the magic bytes are enough
NOT_PARQUET = b"CSV," + b"1,2,3\n" * 10

MB = 1024 * 1024


def mk_part(size):
    return PAR1 + b"\x00" * max(0, size - len(PAR1))


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def lake_dir(dash_id):
    return os.path.join(f"{TMP}/uploads", "_lake", dash_id)


def parts_on_disk(dash_id):
    d = lake_dir(dash_id)
    if not os.path.isdir(d):
        return set()
    return {f[:-len(".parquet")] for f in os.listdir(d) if f.endswith(".parquet")}


K1 = "1111111111111111"
K2 = "2222222222222222"
K3 = "3333333333333333"
K_MISSING = "4444444444444444"
K_BAD_SHORT = "abc"
K_BAD_CHARS = "zzzzzzzzzzzzzzzz"

with TestClient(app) as c:
    from app.database import SessionLocal
    from app.models import AuditLog, Dashboard, DashboardPermission, User

    # ---- users -------------------------------------------------------
    login(c, "admin1")
    for person_id, role in (
        ("owner1", "superuser"),
        ("editor1", "visitor"),
        ("noupdate1", "visitor"),
        ("visitor_pub", "visitor"),
        ("visitor_priv", "visitor"),
    ):
        r = c.post("/api/admin/users", json={"person_id": person_id, "role": role})
        assert r.status_code == 200, f"create {person_id}: {r.status_code} {r.text}"

    db = SessionLocal()
    owner = db.query(User).filter(User.person_id == "owner1").first()
    editor = db.query(User).filter(User.person_id == "editor1").first()

    dash = Dashboard(id="lakedash1", slug="lakedash1", title="Lake dash",
                      dash_type="static", owner_id=owner.id, is_public=True)
    db.add(dash)
    dash_priv = Dashboard(id="lakedash2", slug="lakedash2", title="Lake dash private",
                           dash_type="static", owner_id=owner.id, is_public=False)
    db.add(dash_priv)
    db.flush()
    db.add(DashboardPermission(dashboard_id=dash.id, user_id=editor.id,
                                granted_by=owner.id, can_update_data=True))
    dash_id = dash.id
    dash_priv_id = dash_priv.id
    db.commit()
    db.close()

    PARTS = f"/api/dashboards/{dash_id}/lake/parts"
    MANIFEST = f"/api/dashboards/{dash_id}/lake/manifest"
    LAKE = f"/api/dashboards/{dash_id}/lake"
    PARTS_PRIV = f"/api/dashboards/{dash_priv_id}/lake/parts"
    MANIFEST_PRIV = f"/api/dashboards/{dash_priv_id}/lake/manifest"

    # ---- access: POST/PUT need can_update_dashboard_data --------------
    login(c, "noupdate1")
    r = c.post(PARTS, files={"file": ("p.parquet", mk_part(64), "application/octet-stream")},
               data={"key": K1})
    check("POST part: viewer without update rights -> 403", r.status_code == 403,
          f"{r.status_code} {r.text[:150]}")

    r = c.put(MANIFEST, json={"parser_version": "1", "files": []})
    check("PUT manifest: viewer without update rights -> 403", r.status_code == 403,
          f"{r.status_code} {r.text[:150]}")

    # ---- bad key shape --------------------------------------------------
    login(c, "owner1")
    for label, bad in (("too short", K_BAD_SHORT), ("bad chars", K_BAD_CHARS)):
        r = c.post(PARTS, files={"file": ("p.parquet", mk_part(64), "application/octet-stream")},
                   data={"key": bad})
        check(f"POST part: {label} key -> 422", r.status_code == 422,
              f"{r.status_code} {r.text[:150]}")

    # ---- non-PAR1 -> 415 -------------------------------------------------
    r = c.post(PARTS, files={"file": ("p.parquet", NOT_PARQUET, "application/octet-stream")},
               data={"key": K1})
    check("POST part: non-PAR1 body -> 415", r.status_code == 415,
          f"{r.status_code} {r.text[:150]}")
    check("non-PAR1 upload left nothing on disk", parts_on_disk(dash_id) == set(),
          parts_on_disk(dash_id))

    # ---- oversize single part (> OP_LAKE_PART_MAX_MB) -> 413 -------------
    r = c.post(PARTS, files={"file": ("big.parquet", mk_part(MB + 100_000), "application/octet-stream")},
               data={"key": K1})
    check("POST part: over per-part cap -> 413", r.status_code == 413,
          f"{r.status_code} {r.text[:150]}")
    check("oversize part not written", K1 not in parts_on_disk(dash_id), parts_on_disk(dash_id))

    # ---- owner: 200, two parts under both caps ----------------------------
    r = c.post(PARTS, files={"file": ("p1.parquet", mk_part(900_000), "application/octet-stream")},
               data={"key": K1})
    check("POST part: owner, key1 -> 200", r.status_code == 200 and r.json()["key"] == K1,
          f"{r.status_code} {r.text[:150]}")

    r = c.post(PARTS, files={"file": ("p2.parquet", mk_part(900_000), "application/octet-stream")},
               data={"key": K2})
    check("POST part: owner, key2 -> 200 (total ~1.8 MB, under 2 MB dir cap)",
          r.status_code == 200 and r.json()["key"] == K2, f"{r.status_code} {r.text[:150]}")

    # ---- editor with can_update_data: also allowed -------------------------
    login(c, "editor1")
    r = c.post(PARTS, files={"file": ("p3.parquet", mk_part(64), "application/octet-stream")},
               data={"key": K3})
    check("POST part: editor with can_update_data -> 200", r.status_code == 200,
          f"{r.status_code} {r.text[:150]}")
    login(c, "owner1")

    # ---- total dir cap: a third ~900 KB part pushes the dir over 2 MB -----
    before = parts_on_disk(dash_id)
    r = c.post(PARTS, files={"file": ("p4.parquet", mk_part(900_000), "application/octet-stream")},
               data={"key": K_MISSING})
    check("POST part: pushes dir over OP_LAKE_MAX_MB -> 413", r.status_code == 413,
          f"{r.status_code} {r.text[:150]}")
    check("part that busted the dir cap was not kept",
          K_MISSING not in parts_on_disk(dash_id), parts_on_disk(dash_id))
    check("the other parts are untouched by the failed write",
          parts_on_disk(dash_id) == before, (parts_on_disk(dash_id), before))

    # ---- manifest: referencing a key with no part -> 422 -------------------
    r = c.put(MANIFEST, json={
        "parser_version": "gen-1.0",
        "files": [
            {"name": "a.xlsx", "size": 1, "mtime_ms": 1, "key": K1, "table": "t", "bytes": 900_000},
            {"name": "b.xlsx", "size": 1, "mtime_ms": 1, "key": K_MISSING, "table": "t", "bytes": 1},
        ],
    })
    check("PUT manifest: missing key -> 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
    check("422 lists the missing key",
          r.status_code == 422 and K_MISSING in str(r.json().get("detail", "")),
          r.text[:200])

    # ---- manifest: publish with key1+key2+key3, all present ---------------
    r = c.put(MANIFEST, json={
        "run_id": 7,
        "parser_version": "gen-1.0",
        "files": [
            {"name": "a.xlsx", "size": 900_000, "mtime_ms": 1000, "sheet": "Sheet1",
             "header_row": 1, "key": K1, "table": "t_a", "partition": "2026", "bytes": 900_000},
            {"name": "b.xlsx", "size": 900_000, "mtime_ms": 1000, "key": K2, "table": "t_b", "bytes": 900_000},
            {"name": "c.xlsx", "size": 64, "mtime_ms": 1000, "key": K3, "table": "t_c", "bytes": 64},
        ],
    })
    check("PUT manifest: all keys present -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        mj = r.json()
        check("manifest carries created_at + by",
              bool(mj.get("created_at")) and mj.get("by") == "owner1", mj)
        check("manifest carries run_id + parser_version + 3 files",
              mj.get("run_id") == 7 and mj.get("parser_version") == "gen-1.0"
              and len(mj.get("files") or []) == 3, mj)

    db = SessionLocal()
    logged = db.query(AuditLog).filter(
        AuditLog.action == "lake_publish", AuditLog.resource_id == dash_id
    ).first()
    db.close()
    check("lake_publish audited with files/bytes, no content",
          logged is not None and '"files": 3' in (logged.detail or "")
          and "a.xlsx" not in (logged.detail or ""), logged.detail if logged else None)

    # ---- manifest replace deletes stale parts ------------------------------
    r = c.put(MANIFEST, json={
        "parser_version": "gen-1.1",
        "files": [
            {"name": "a.xlsx", "size": 900_000, "mtime_ms": 2000, "key": K1, "table": "t_a", "bytes": 900_000},
        ],
    })
    check("PUT manifest: re-publish with only key1 -> 200", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")
    check("re-publish pruned key2 and key3 off disk",
          parts_on_disk(dash_id) == {K1}, parts_on_disk(dash_id))

    # republish with key1+key2 again so later GET-part tests have both.
    r = c.post(PARTS, files={"file": ("p2.parquet", mk_part(900_000), "application/octet-stream")},
               data={"key": K2})
    check("re-upload key2 -> 200", r.status_code == 200, r.text[:150])
    r = c.put(MANIFEST, json={
        "parser_version": "gen-1.2",
        "files": [
            {"name": "a.xlsx", "size": 900_000, "mtime_ms": 3000, "key": K1, "table": "t_a", "bytes": 900_000},
            {"name": "b.xlsx", "size": 900_000, "mtime_ms": 3000, "key": K2, "table": "t_b", "bytes": 900_000},
        ],
    })
    check("PUT manifest: republish key1+key2 -> 200", r.status_code == 200, r.text[:200])

    # ---- GET manifest: require_dashboard_access ----------------------------
    login(c, "visitor_pub")  # no explicit permission — allowed via is_public
    r = c.get(MANIFEST)
    check("GET manifest: visitor with access (public dash) -> 200",
          r.status_code == 200 and r.json().get("parser_version") == "gen-1.2",
          f"{r.status_code} {r.text[:150]}")

    r = c.get(f"/api/dashboards/{dash_id}/lake/parts/{K1}")
    check("GET part: visitor with access -> 200", r.status_code == 200, f"{r.status_code}")
    if r.status_code == 200:
        check("GET part: body bytes match", r.content == mk_part(900_000), len(r.content))
        h = r.headers
        check("GET part: Content-Encoding identity", h.get("content-encoding") == "identity", dict(h))
        check("GET part: nosniff", h.get("x-content-type-options") == "nosniff", dict(h))
        check("GET part: Cache-Control private", "private" in (h.get("cache-control") or ""), dict(h))
        check("GET part: ETag is the key", h.get("etag") == f'"{K1}"', h.get("etag"))

        r2 = c.get(f"/api/dashboards/{dash_id}/lake/parts/{K1}",
                    headers={"If-None-Match": f'"{K1}"'})
        check("GET part: If-None-Match -> 304", r2.status_code == 304, r2.status_code)

    r = c.get(f"/api/dashboards/{dash_id}/lake/parts/{K_MISSING}")
    check("GET part: valid-shaped key with no file -> 404", r.status_code == 404, r.status_code)

    r = c.get(f"/api/dashboards/{dash_id}/lake/parts/{K_BAD_SHORT}")
    check("GET part: bad key shape -> 422", r.status_code == 422, r.status_code)

    # ---- no access: 403 on GET manifest/part -------------------------------
    login(c, "visitor_priv")
    r = c.get(MANIFEST_PRIV)
    check("GET manifest: no dashboard access -> 403", r.status_code == 403, r.status_code)
    r = c.get(f"/api/dashboards/{dash_priv_id}/lake/parts/{K1}")
    check("GET part: no dashboard access -> 403", r.status_code == 403, r.status_code)

    # ---- share-link sessions must not reach any of this ---------------------
    # (no share-token dependency is wired into any lake route — verified by
    # reading app/api/lake.py's Depends chain, which uses only the signed-in
    # deps; no separate test client exists for share tokens here.)

    # ---- DELETE: owner or administrator only ---------------------------------
    login(c, "noupdate1")
    r = c.delete(LAKE)
    check("DELETE lake: non-owner, non-admin -> 403", r.status_code == 403, r.status_code)

    login(c, "owner1")
    r = c.delete(LAKE)
    check("DELETE lake: owner -> 200", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
    check("DELETE lake: directory removed", not os.path.isdir(lake_dir(dash_id)),
          os.path.isdir(lake_dir(dash_id)))

    r = c.get(MANIFEST)
    check("GET manifest after delete -> 404", r.status_code == 404, r.status_code)

    db = SessionLocal()
    logged = db.query(AuditLog).filter(
        AuditLog.action == "lake_delete", AuditLog.resource_id == dash_id
    ).first()
    db.close()
    check("lake_delete audited", logged is not None, logged)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
else:
    print("ALL PASS")
