"""Smoke: both share routes write the `share_link_create` audit entry and do NOT log the token.

The "create a link" quest is verified against this entry (phase 3), so it
must appear on both paths: `/share-tokens` and the `/share-links` alias
that viewer.js uses.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-quests-share-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_QUESTS_ENABLED": "true",
})
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def make_dashboard(c, title):
    html = b"<html><body><h1>" + title.encode() + b"</h1></body></html>"
    r = c.post("/api/dashboards", data={"title": title, "description": ""},
               files={"html_file": ("d.html", html, "text/html")})
    assert r.status_code in (200, 303), f"create: {r.status_code} {r.text[:300]}"
    loc = r.headers.get("location") or (r.history[-1].headers.get("location") if r.history else None)
    return loc.rstrip("/").rsplit("/", 1)[-1]


def main():
    with TestClient(app) as c:
        r = c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)
        assert r.status_code == 302, r.status_code
        dash = make_dashboard(c, "Share Audit Report")

        r = c.post(f"/api/dashboards/{dash}/share-tokens",
                   json={"expires_in_days": 7, "label": "for the board"})
        check("POST /share-tokens ok", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        tok1 = r.json()["token"]

        r = c.post(f"/api/dashboards/{dash}/share-links", json={"label": "viewer path"})
        check("POST /share-links ok", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        tok2 = r.json()["id"]

        from app.database import SessionLocal
        from app.models import AuditLog
        db = SessionLocal()
        try:
            rows = db.query(AuditLog).filter(AuditLog.action == "share_link_create").all()
            check("both routes wrote share_link_create", len(rows) == 2, str(len(rows)))
            details = [r_.get_detail() for r_ in rows]
            raw = [r_.detail or "" for r_ in rows]
            check("resource_type/id point at the dashboard",
                  all(r_.resource_type == "dashboard" and r_.resource_id == dash for r_ in rows),
                  str([(r_.resource_type, r_.resource_id) for r_ in rows]))
            check("token_id present and 8 chars",
                  all(isinstance(d.get("token_id"), str) and len(d["token_id"]) == 8 for d in details),
                  str(details))
            check("token_id is the real prefix",
                  {d["token_id"] for d in details} == {tok1[:8], tok2[:8]}, str(details))
            check("full token never stored",
                  not any(tok1 in s or tok2 in s for s in raw), str(raw))
            check("labels kept", {d.get("label") for d in details} == {"for the board", "viewer path"},
                  str(details))
            check("expires_in_days kept", {d.get("expires_in_days") for d in details} == {7, None},
                  str(details))
        finally:
            db.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
