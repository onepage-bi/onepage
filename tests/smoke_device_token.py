"""Smoke for device keys (`app/auth/devicetoken.py`, `app/auth/deps.py`,
`app/api/me.py`, the `DeviceToken` model, migration 0008).

The main thing pinned here is NOT "the key works" but **"a key cannot write"**.
A key lives on a person's machine for months, with no browser and no SSO; if it
ever starts letting a mutating route through, that is a silent hole on every
machine at once. The check in `deps.py` knows about the METHOD, not a list of paths,
so the test hits methods rather than specific addresses: that way a new route
ends up closed by itself, without changing the test.

The stand bootstrap idiom comes from `tests/smoke_favorites.py`.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="op-dt-smoke-")
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
    assert r.status_code in (200, 303), f"create {title}: {r.status_code} {r.text[:200]}"
    loc = r.headers.get("location") or (r.history[-1].headers.get("location") if r.history else None)
    return loc.rstrip("/").rsplit("/", 1)[-1]


def main():
    # The prefix constant is duplicated in deps.py for a cheap pre-filter before
    # importing the key module, so the two can drift apart. Catch that here.
    from app.auth import deps as deps_mod
    from app.auth import devicetoken
    check("prefix in deps.py matches devicetoken.PREFIX",
          deps_mod._DT_PREFIX == devicetoken.PREFIX,
          f"{deps_mod._DT_PREFIX!r} vs {devicetoken.PREFIX!r}")

    with TestClient(app) as c:
        login(c, "admin1")
        dash = make_dashboard(c, "Mirror Me")
        c.patch(f"/api/dashboards/{dash}", json={"is_published": True})

        # ── issuing ──────────────────────────────────────────────────
        r = c.post("/api/me/device-tokens", json={"label": "laptop"})
        check("a key is issued", r.status_code == 200 and r.json().get("ok"), f"{r.status_code} {r.text[:200]}")
        body = r.json()
        raw = body.get("token", "")
        tok_id = body.get("id")
        check("the key is returned in plain text once", raw.startswith(devicetoken.PREFIX), raw[:12])
        check("an expiry date is given", bool(body.get("expires_at")), str(body))

        # ── the page without which there is nowhere to get a key ─────
        r = c.get("/me")
        check("the account page responds", r.status_code == 200, r.status_code)
        check("the page says the key is read-only",
              "only reads your reports" in r.text, "no read-only sentence")
        check("the page includes its own script",
              "/static/js/account.js" in r.text, "script not included")
        # The sidebar link is the only way to reach the page.
        check("the sidebar links to /me", 'href="/me"' in r.text, "no link")

        r = c.get("/api/me/device-tokens")
        items = r.json()["items"]
        check("the key shows in its owner's list", any(t["id"] == tok_id for t in items), str(items))
        check("the list does NOT contain the key string", all("token" not in t for t in items), str(items))

        # Neither the key nor its hash may reach the audit log.
        from app.database import SessionLocal
        from app.models import AuditLog, DeviceToken
        db = SessionLocal()
        try:
            rows = db.query(AuditLog).filter(AuditLog.action == "device_token_issue").all()
            check("issue audit recorded", len(rows) == 1, str(len(rows)))
            det = (rows[0].detail or "") if rows else ""
            check("no plain key in the audit", raw not in det, det[:120])
            check("no key hash in the audit", devicetoken.hash_token(raw) not in det, det[:120])
            stored = db.query(DeviceToken).filter(DeviceToken.id == tok_id).first()
            check("only the hash is stored in the DB", stored.token_hash == devicetoken.hash_token(raw), "")
        finally:
            db.close()

    # From here on: a separate client WITHOUT a cookie; the key must work on its own.
    with TestClient(app) as t:
        hdr = {"Authorization": f"Bearer {raw}"}

        r = t.get("/api/sync/manifest", headers=hdr)
        check("the key reads the manifest", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
        if r.status_code == 200:
            ids = [i["id"] for i in r.json()["items"]]
            check("and the report is in it", dash in ids, str(ids))

        r = t.get(f"/render/{dash}/", headers=hdr)
        check("the key reads the rendered report", r.status_code == 200, r.status_code)
        r = t.get(f"/api/dashboards/{dash}/files", headers=hdr)
        check("the key reads the slot list", r.status_code == 200, r.status_code)

        # ── THE MAIN POINT: a key cannot write. Hit the METHODS ──────
        r = t.patch(f"/api/dashboards/{dash}", json={"title": "Hijacked"}, headers=hdr)
        check("PATCH with a key refused", r.status_code in (401, 403), r.status_code)
        r = t.post("/api/me/device-tokens", json={"label": "second"}, headers=hdr)
        check("a key cannot issue a second key", r.status_code in (401, 403), r.status_code)
        r = t.delete(f"/api/me/device-tokens/{tok_id}", headers=hdr)
        check("a key cannot revoke a key", r.status_code in (401, 403), r.status_code)
        r = t.put(f"/api/dashboards/{dash}/theme", json={"vars": {"--op-accent": "#000"}}, headers=hdr)
        check("PUT with a key refused", r.status_code in (401, 403), r.status_code)
        r = t.post(f"/api/dashboards/{dash}/version", headers=hdr)
        check("POST of a new version with a key refused", r.status_code in (401, 403), r.status_code)

        # A header of the wrong shape is simply unauthenticated.
        r = t.get("/api/sync/manifest", headers={"Authorization": "Bearer opdt_nottherightkey0000000000000000000000000"})
        check("a foreign string is not let in", r.status_code == 401, r.status_code)
        r = t.get("/api/sync/manifest", headers={"Authorization": raw})
        check("without the word Bearer it is not let in", r.status_code == 401, r.status_code)
        r = t.get("/api/sync/manifest")
        check("without any key it is not let in", r.status_code == 401, r.status_code)

        # A key corrupted by one character does not pass.
        r = t.get("/api/sync/manifest",
                  headers={"Authorization": f"Bearer {raw[:-1]}{'A' if raw[-1] != 'A' else 'B'}"})
        check("a key with one changed letter does not pass", r.status_code == 401, r.status_code)

        # ── expired key ──────────────────────────────────────────────
        from app.database import SessionLocal
        from app.models import DeviceToken
        db = SessionLocal()
        try:
            row = db.query(DeviceToken).filter(DeviceToken.id == tok_id).first()
            keep = row.expires_at
            row.expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
        finally:
            db.close()
        r = t.get("/api/sync/manifest", headers=hdr)
        check("an expired key is not let in", r.status_code == 401, r.status_code)
        db = SessionLocal()
        try:
            row = db.query(DeviceToken).filter(DeviceToken.id == tok_id).first()
            row.expires_at = keep
            db.commit()
        finally:
            db.close()
        r = t.get("/api/sync/manifest", headers=hdr)
        check("after restoring the expiry the key works again", r.status_code == 200, r.status_code)

    # ── revocation by a live login, and the key dies ─────────────────
    with TestClient(app) as c:
        login(c, "admin1")
        r = c.delete(f"/api/me/device-tokens/{tok_id}")
        check("the key is revoked by a live login", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        r = c.delete(f"/api/me/device-tokens/{tok_id}")
        check("second revocation -> 404", r.status_code == 404, r.status_code)

    with TestClient(app) as t:
        r = t.get("/api/sync/manifest", headers={"Authorization": f"Bearer {raw}"})
        check("a revoked key is not let in", r.status_code == 401, r.status_code)

    # ── someone else's key cannot be revoked ────────────────────────
    with TestClient(app) as c:
        login(c, "admin1")
        c.post("/api/admin/users", json={"person_id": "other1", "role": "visitor", "display_name": "Other"})
        r = c.post("/api/me/device-tokens", json={"label": "mine"})
        mine_id = r.json()["id"]
        login(c, "other1")
        r = c.delete(f"/api/me/device-tokens/{mine_id}")
        check("someone else's key cannot be revoked (404, not 403)", r.status_code == 404, r.status_code)

        # Cap on the number of keys.
        login(c, "other1")
        codes = [c.post("/api/me/device-tokens", json={"label": f"m{i}"}).status_code
                 for i in range(devicetoken.MAX_PER_USER + 1)]
        check(f"the cap of {devicetoken.MAX_PER_USER} keys holds",
              codes[-1] == 409 and codes[:-1] == [200] * devicetoken.MAX_PER_USER, str(codes))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
