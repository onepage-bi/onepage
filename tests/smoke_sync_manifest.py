"""Smoke for `GET /api/sync/manifest` — the answer the desktop app uses to
decide what it holds is out of date.

Stand-seeding idiom from `tests/smoke_favorites.py`: reports are created
through the REAL upload route, not by inserting a row, so the test exercises
the path reports actually arrive by.

What matters to pin here, beyond the response shape:

  * **the RLS boundary** — a report with a policy must not reach the mirror
    with a single file, and the reason must be named by code
    (`skip_reason == "rls"`), not a silent omission: otherwise a person
    concludes the report got lost;
  * **`updated_at` with a time** — exactly the gap the route was added for
    (the home page has `%Y-%m-%d`, and two edits in one day look the same);
  * **rights arrive in the answer**, because offline there is nobody to ask;
  * **access matches the home page** — someone else's non-public report is
    visible neither there nor here.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-sync-smoke-")
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


def make_dashboard(c, title):
    html = b"<html><body><h1>" + title.encode() + b"</h1></body></html>"
    r = c.post("/api/dashboards", data={"title": title, "description": ""},
               files={"html_file": ("d.html", html, "text/html")})
    assert r.status_code in (200, 303), f"create {title}: {r.status_code} {r.text[:300]}"
    loc = r.headers.get("location") or (r.history[-1].headers.get("location") if r.history else None)
    dash_id = loc.rstrip("/").rsplit("/", 1)[-1] if loc else None
    assert dash_id, f"no dashboard id from create {title}: {r.headers}"
    return dash_id


def by_id(payload, dash_id):
    for it in payload["items"]:
        if it["id"] == dash_id:
            return it
    return None


def main():
    with TestClient(app) as c:
        login(c, "admin1")
        plain = make_dashboard(c, "Plain Report")
        secured = make_dashboard(c, "Secured Report")

        for ov, role in (("upd1", "visitor"), ("view1", "visitor")):
            r = c.post("/api/admin/users", json={"person_id": ov, "role": role, "display_name": ov})
            assert r.status_code == 200, f"create {ov}: {r.text[:200]}"

        # The RLS policy goes straight into the DB: registering a real generator
        # with an `rls` block is a separate big path, the test only needs the flag.
        from app.database import SessionLocal
        from app.models import Dashboard
        db = SessionLocal()
        try:
            d = db.get(Dashboard, secured)
            d.rls_json = '{"slots": {"0": {"column": "territory"}}}'
            db.commit()
            published = bool(db.get(Dashboard, plain).is_published)
        finally:
            db.close()

        # ── response shape ───────────────────────────────────────────
        r = c.get("/api/sync/manifest")
        check("manifest answers 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        p = r.json()
        check("has items", isinstance(p.get("items"), list), str(p)[:200])
        check("has a rights expiry", isinstance(p.get("rights_ttl_s"), int) and p["rights_ttl_s"] > 0, str(p.get("rights_ttl_s")))
        check("says whether the snapshot is external", isinstance(p.get("snapshot_external"), bool), str(p.get("snapshot_external")))
        check("names the user", (p.get("user") or {}).get("person_id") == "admin1", str(p.get("user")))

        row = by_id(p, plain)
        check("plain report is in the answer", row is not None, "missing from items")
        if row:
            # The main reason the route exists: a time, not just a date.
            check("updated_at carries a time, not just a date",
                  isinstance(row["updated_at"], str) and "T" in row["updated_at"], str(row.get("updated_at")))
            check("owner may update data", row["can_update_data"] is True, str(row))
            if published:
                check("plain report is mirrorable", row["mirrorable"] is True, str(row))
                check("a mirrorable report has a fingerprint", bool(row.get("fingerprint")), str(row.get("fingerprint")))
                check("a mirrorable report lists its slots", isinstance(row.get("slots"), list), str(row.get("slots")))
            else:
                check("a draft is not mirrored", row["mirrorable"] is False, str(row))
                check("and the reason is named: draft", row["skip_reason"] == "draft", str(row))

        # ── RLS boundary: the main thing checked here ────────────────
        srow = by_id(p, secured)
        check("RLS report is listed (it is not hidden)", srow is not None, "missing from items")
        if srow:
            check("RLS report is NOT mirrorable", srow["mirrorable"] is False, str(srow))
            check("reason is named by the code rls", srow["skip_reason"] == "rls", str(srow.get("skip_reason")))
            check("RLS report has no fingerprint", "fingerprint" not in srow, str(srow.keys()))
            check("RLS report lists no slots", "slots" not in srow, str(srow.keys()))
            check("rls_enabled flag is set", srow["rls_enabled"] is True, str(srow))

        # ── rights arrive in the answer ──────────────────────────────
        r = c.post(f"/api/dashboards/{plain}/permissions/upd1", json={"can_update_data": True})
        check("can_update_data grant issued", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        r = c.post(f"/api/dashboards/{plain}/permissions/view1", json={"can_update_data": False})
        check("read-only grant issued", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        c.patch(f"/api/dashboards/{plain}", json={"is_published": True})

        # The main branch: a published report is mirrorable and carries
        # everything needed to fetch it. A fresh upload is a draft, so without
        # this publish the `mirrorable` branch would never run in the test.
        p1b = c.get("/api/sync/manifest").json()
        rowb = by_id(p1b, plain)
        check("after publishing the report is mirrorable", rowb and rowb["mirrorable"] is True, str(rowb))
        if rowb:
            check("a fingerprint appeared", bool(rowb.get("fingerprint")), str(rowb.get("fingerprint")))
            check("slots are listed (even if empty)", isinstance(rowb.get("slots"), list), str(rowb.get("slots")))
            check("no skip reason any more", rowb["skip_reason"] == "", str(rowb.get("skip_reason")))
            fp_before = rowb["fingerprint"]

            # The fingerprint must change on an edit that does NOT move the
            # version number — exactly why it is in the answer.
            # The shape is exactly what the route expects (`vars`/`remap`): a
            # payload with other keys is sanitised to nothing, answers 200 and
            # writes NOTHING — the test was fooled by that once already.
            r = c.put(f"/api/dashboards/{plain}/theme", json={"vars": {"--op-accent": "#ff0000"}})
            if r.status_code == 200 and not r.json().get("empty"):
                rowc = by_id(c.get("/api/sync/manifest").json(), plain)
                check("a theme edit does not move the version number",
                      rowc["version"] == rowb["version"], f"{rowc['version']} vs {rowb['version']}")
                check("but it changes the fingerprint",
                      rowc["fingerprint"] != fp_before, f"{rowc['fingerprint']} == {fp_before}")
            else:
                print(f"SKIP  theme edit was not written ({r.status_code} {r.text[:80]}) — fingerprint branch not checked")

        login(c, "upd1")
        p2 = c.get("/api/sync/manifest").json()
        row2 = by_id(p2, plain)
        check("the grantee sees the report", row2 is not None, str([i['id'] for i in p2['items']]))
        if row2:
            check("and the right arrived in the answer", row2["can_update_data"] is True, str(row2))

        login(c, "view1")
        p3 = c.get("/api/sync/manifest").json()
        row3 = by_id(p3, plain)
        check("the reader sees the report", row3 is not None, str([i['id'] for i in p3['items']]))
        if row3:
            check("the reader has no update right", row3["can_update_data"] is False, str(row3))
        check("the reader does not see someone else's non-public RLS report",
              by_id(p3, secured) is None, "visible, but must not be")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
