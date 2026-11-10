"""Smoke test for "My reports" favourites (app/api/favorites.py, models.py
DashboardFavorite, app/api/dashboards.py index()).

Boot idiom from the onepage-testing skill (tests/smoke_quality.py lines
20-60). A dashboard is created through the real upload endpoint (a minimal
static HTML file) so the test exercises the real create path, not a
hand-inserted row.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-fav-smoke-")
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
    # redirect target is /d/{id}
    loc = r.headers.get("location") or (r.history[-1].headers.get("location") if r.history else None)
    dash_id = loc.rstrip("/").rsplit("/", 1)[-1] if loc else None
    assert dash_id, f"no dashboard id from create {title}: {r.headers}"
    return dash_id


def main():
    with TestClient(app) as c:
        login(c, "admin1")

        # A second, unprivileged user (visitor — no upload_dashboard), for
        # cross-user isolation checks.
        r = c.post("/api/admin/users", json={"person_id": "vis1", "role": "visitor", "display_name": "Vis One"})
        check("second user created", r.status_code == 200 and r.json().get("ok") is True, f"{r.status_code} {r.text[:200]}")

        dash1 = make_dashboard(c, "Report One")
        dash2 = make_dashboard(c, "Report Two")

        # ── idempotent PUT ───────────────────────────────────────────
        r = c.put(f"/api/dashboards/{dash1}/favorite")
        check("PUT favorite ok", r.status_code == 200 and r.json().get("ok") is True, f"{r.status_code} {r.text}")
        r = c.put(f"/api/dashboards/{dash1}/favorite")
        check("PUT favorite idempotent (repeat ok)", r.status_code == 200 and r.json().get("ok") is True, r.text)

        r = c.get("/")
        check("GET / after favoriting has fav position for dash1",
              f'"id": "{dash1}"' in r.text or f'"id":"{dash1}"' in r.text, "html snippet not checked directly")
        check("GET / HTML #dash-data carries \"fav\":", '"fav":' in r.text, "no fav key in payload")
        check("GET / HTML carries presentations key", '"presentations":' in r.text, "no presentations key")

        # ── idempotent DELETE ────────────────────────────────────────
        r = c.delete(f"/api/dashboards/{dash1}/favorite")
        check("DELETE favorite ok", r.status_code == 200 and r.json().get("ok") is True, r.text)
        r = c.delete(f"/api/dashboards/{dash1}/favorite")
        check("DELETE favorite idempotent (repeat ok)", r.status_code == 200 and r.json().get("ok") is True, r.text)

        # ── order endpoint renumbers only own rows ──────────────────
        c.put(f"/api/dashboards/{dash1}/favorite")
        c.put(f"/api/dashboards/{dash2}/favorite")
        # dash1 should be position 1, dash2 position 2 (arrival order)
        r = c.put("/api/favorites/order", json={"ids": [dash2, dash1]})
        check("order endpoint ok", r.status_code == 200 and r.json().get("ok") is True, r.text)

        from app.database import SessionLocal
        from app.models import DashboardFavorite, User
        db = SessionLocal()
        try:
            admin = db.query(User).filter(User.person_id == "admin1").first()
            rows = {f.dashboard_id: f.position for f in
                    db.query(DashboardFavorite).filter(DashboardFavorite.user_id == admin.id).all()}
            check("reordered: dash2 first", rows.get(dash2) == 1, str(rows))
            check("reordered: dash1 second", rows.get(dash1) == 2, str(rows))
        finally:
            db.close()

        # Unknown/foreign id in the order list is ignored, not an error.
        r = c.put("/api/favorites/order", json={"ids": [dash1, "not-a-real-id", dash2]})
        check("order endpoint ignores unknown ids", r.status_code == 200, r.text)

        # ── a second user cannot see/alter the first user's favourites ─
        login(c, "vis1")
        r = c.get(f"/d/{dash1}")
        check("vis1 cannot even view dash1 (private, unpublished)", r.status_code == 403, r.status_code)
        r = c.put(f"/api/dashboards/{dash1}/favorite")
        check("vis1 PUT favorite on inaccessible dashboard -> 403", r.status_code == 403, r.status_code)

        db = SessionLocal()
        try:
            from app.models import User as U2
            vis = db.query(U2).filter(U2.person_id == "vis1").first()
            n = db.query(DashboardFavorite).filter(DashboardFavorite.user_id == vis.id).count()
            check("vis1 has zero favourite rows after the failed attempt", n == 0, str(n))
            admin = db.query(U2).filter(U2.person_id == "admin1").first()
            n2 = db.query(DashboardFavorite).filter(DashboardFavorite.user_id == admin.id).count()
            check("admin1's favourites untouched by vis1's attempt", n2 == 2, str(n2))
        finally:
            db.close()

        # 404 for a dashboard that does not exist
        r = c.put("/api/dashboards/does-not-exist/favorite")
        check("PUT favorite on unknown dashboard -> 404", r.status_code == 404, r.status_code)

        # ── audit rows written ──────────────────────────────────────
        login(c, "admin1")
        db = SessionLocal()
        try:
            from app.models import AuditLog
            actions = {row.action for row in
                       db.query(AuditLog).filter(AuditLog.resource_type == "dashboard",
                                                  AuditLog.action.like("favorite.%")).all()}
            check("audit has favorite.add", "favorite.add" in actions, str(actions))
            check("audit has favorite.remove", "favorite.remove" in actions, str(actions))
            check("audit has favorite.order", "favorite.order" in actions, str(actions))
        finally:
            db.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
