"""Share links follow the «can update data» right (owner decision 2026-09-04).

A colleague granted `can_update_data` on a report sees a slim right dock
(Share, plus Data when applicable) and can create/list/revoke share links;
a plain viewer still gets 403 and no dock; owner/admin unchanged.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-share-upd-")
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
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  {extra}"))
    if not cond:
        FAILURES.append(label)


def login(c, ov):
    r = c.post("/dev-login", data={"person_id": ov}, follow_redirects=False)
    assert r.status_code == 302, r.text[:200]


def make_dashboard(c, title):
    html = b"<html><body><h1>" + title.encode() + b"</h1></body></html>"
    r = c.post("/api/dashboards", data={"title": title, "description": ""},
               files={"html_file": ("d.html", html, "text/html")}, follow_redirects=False)
    loc = r.headers.get("location") or ""
    dash_id = loc.rstrip("/").rsplit("/", 1)[-1]
    assert dash_id, f"{r.status_code} {r.headers}"
    return dash_id


def main():
    with TestClient(app) as c:
        login(c, "admin1")
        for ov in ("upd1", "view1"):
            r = c.post("/api/admin/users", json={"person_id": ov, "role": "visitor", "display_name": ov})
            assert r.status_code == 200, r.text
        did = make_dashboard(c, "Report")
        c.patch(f"/api/dashboards/{did}", json={"is_published": True})
        r = c.post(f"/api/dashboards/{did}/permissions/upd1", json={"can_update_data": True})
        check("grant can_update_data", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        r = c.post(f"/api/dashboards/{did}/permissions/view1", json={"can_update_data": False})
        check("grant view only", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

        t = c.get(f"/d/{did}").text
        check("admin: full dock", 'data-tab="settings"' in t and 'data-tab="share"' in t)

        login(c, "upd1")
        r = c.get(f"/d/{did}")
        t = r.text
        check("updater: viewer 200", r.status_code == 200, r.status_code)
        check("updater: full dock (settings, access, versions, share, annotate, colors, text)",
              all(f'data-tab="{x}"' in t for x in ("settings", "access", "versions", "share", "annotate", "colors", "text")))
        check("updater: no Danger zone (delete is owner/admin only)", 'id="btn-delete"' not in t)
        check("updater: dock toggle present", 'id="dock-toggle"' in t)
        check("updater: replace form present", 'id="form-replace-html"' in t)
        r = c.patch(f"/api/dashboards/{did}", json={"description": "updated by a colleague"})
        check("updater: PATCH settings 200", r.status_code == 200, f"{r.status_code} {r.text[:100]}")
        r = c.post(f"/api/dashboards/{did}/permissions/view1", json={"can_update_data": False})
        check("updater: manage access 200", r.status_code == 200, f"{r.status_code} {r.text[:100]}")
        r = c.put(f"/api/dashboards/{did}/theme", json={"theme": "dark"})
        check("updater: theme write not 403", r.status_code != 403, r.status_code)
        r = c.delete(f"/api/dashboards/{did}")
        check("updater: delete dashboard 403", r.status_code == 403, r.status_code)
        r = c.get(f"/api/dashboards/{did}/source")
        check("updater: download source 200", r.status_code == 200, r.status_code)
        r = c.post(f"/api/dashboards/{did}/version", files={"html_file": ("d.html", b"<html><body><h1>v2</h1></body></html>", "text/html")})
        check("updater: replace html allowed", r.status_code in (200, 303), f"{r.status_code} {r.text[:120]}")
        r = c.post(f"/api/dashboards/{did}/share-links", json={"expires_in_days": 7, "label": "x"})
        check("updater: create share link 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        tok = r.json().get("id") if r.status_code == 200 else None
        r = c.get(f"/api/dashboards/{did}/share-links")
        check("updater: list share links 200", r.status_code == 200)
        if tok:
            r = c.delete(f"/api/dashboards/{did}/share-links/{tok}")
            check("updater: revoke own link", r.status_code in (200, 204), r.status_code)

        login(c, "view1")
        r = c.get(f"/d/{did}")
        check("viewer: page 200 without dock", r.status_code == 200 and 'class="v-dock"' not in r.text)
        check("viewer: PATCH settings 403", c.patch(f"/api/dashboards/{did}", json={"description": "x"}).status_code == 403)
        r = c.post(f"/api/dashboards/{did}/share-links", json={"expires_in_days": 7, "label": None})
        check("viewer: create share link 403", r.status_code == 403, r.status_code)
        check("viewer: download source 403", c.get(f"/api/dashboards/{did}/source").status_code == 403)
        r = c.post(f"/api/dashboards/{did}/version", files={"html_file": ("d.html", b"<html><body>x</body></html>", "text/html")})
        check("viewer: replace html 403", r.status_code == 403, r.status_code)
        check("viewer: list share links 403", c.get(f"/api/dashboards/{did}/share-links").status_code == 403)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
