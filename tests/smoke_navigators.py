"""Navigators — landing pages with outbound links that work (2026-10-29).

What this pins is not the list or the upload but the REASON the section
exists: a link inside an uploaded html must be opened by the shell, outside
the sandbox. A tab opened from inside a report inherits the sandbox and the
opaque origin, gets no cookie, and Power BI Report Server shows a blank
screen. We check that the link interceptor is present in a navigator and
absent from an ordinary report.
"""
import os
import sys
import time
import tempfile

TMP = tempfile.mkdtemp(prefix="op-nav-")
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
PAGE_HTML = ('<html><head><title>Navigator</title></head><body>'
             '<a href="https://pbirs.example.com/reports/powerbi/x">Price Monitoring</a>'
             '</body></html>').encode("utf-8")
PAGE_HTML_V2 = ('<html><head><title>Навигатор v2</title></head><body>'
                '<a href="https://pbirs.example.com/v2">Price Monitoring v2</a>'
                '</body></html>').encode("utf-8")
REPORT_HTML = b"<html><head><title>r</title></head><body><h1>report</h1></body></html>"


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  {extra}"))
    if not cond:
        FAILURES.append(label)


def login(c, ov):
    r = c.post("/dev-login", data={"person_id": ov}, follow_redirects=False)
    assert r.status_code == 302, r.text[:200]


def main():
    with TestClient(app) as c:
        login(c, "admin1")
        for ov, role in (("owner1", "superuser"), ("view1", "visitor")):
            r = c.post("/api/admin/users", json={"person_id": ov, "role": role,
                                                 "display_name": ov})
            assert r.status_code == 200, r.text

        # ── upload ───────────────────────────────────────────────────────
        login(c, "owner1")
        r = c.post("/api/navigators", data={"title": "Where to go", "description": "landing page"},
                   files={"html_file": ("nav.html", PAGE_HTML, "text/html")},
                   follow_redirects=False)
        nav_id = (r.headers.get("location") or "").rstrip("/").rsplit("/", 1)[-1]
        check("navigator uploads", r.status_code == 303 and bool(nav_id),
              f"{r.status_code} {r.headers}")

        r = c.post("/api/dashboards", data={"title": "Ordinary report", "description": ""},
                   files={"html_file": ("r.html", REPORT_HTML, "text/html")},
                   follow_redirects=False)
        rep_id = (r.headers.get("location") or "").rstrip("/").rsplit("/", 1)[-1]
        assert rep_id

        # ── type and separate lists ──────────────────────────────────────
        from app.database import SessionLocal
        from app.models import DASH_PAGE, Dashboard
        db = SessionLocal()
        try:
            d = db.query(Dashboard).filter(Dashboard.id == nav_id).first()
            check("navigator type is forced, not guessed",
                  d is not None and d.dash_type == DASH_PAGE, d and d.dash_type)
            check("navigator is published at once (it never has a run)",
                  d is not None and d.is_published is True)
            check("navigator has no slots", d is not None and d.slot_count == 0)
        finally:
            db.close()

        body = c.get("/").text
        check("report list does NOT show the navigator", nav_id not in body)
        check("report list shows the ordinary report", rep_id in body)
        nav_page = c.get("/navigators").text
        check("navigator list shows the navigator", nav_id in nav_page)
        check("navigator list does NOT show the ordinary report", rep_id not in nav_page)

        # ── link interceptor ─────────────────────────────────────────────
        doc = c.get(f"/render/{nav_id}/").text
        check("link interceptor injected into the navigator", "op_open" in doc, doc[:200])
        check("interceptor also replaces window.open",
              "window.open = function" in doc)
        check("the page's original link is intact",
              "pbirs.example.com" in doc)
        # The bridge passes a network path RAW: a.href would already have turned
        # "\\server" into http://server, making the "copy the path" branch unreachable.
        check("the bridge passes a UNC path raw, not through a.href",
              "href.slice(0, 2) === '\\\\\\\\' ? href" in doc)
        rep_doc = c.get(f"/render/{rep_id}/").text
        check("interceptor is NOT injected into an ordinary report", "op_open" not in rep_doc)

        # ── access: same as a report ─────────────────────────────────────
        login(c, "view1")
        check("an outsider cannot open the navigator",
              c.get(f"/render/{nav_id}/").status_code == 403)
        check("an outsider does not see it in the list", nav_id not in c.get("/navigators").text)
        login(c, "owner1")
        c.post(f"/api/dashboards/{nav_id}/permissions/view1", json={"can_update_data": False})
        login(c, "view1")
        check("granted access opens the navigator",
              c.get(f"/render/{nav_id}/").status_code == 200)

        # ── a page opens as the page only ────────────────────────────────
        # A navigator's reader needs the page; the platform panels are noise:
        # present mode is switched on before first paint, `?full=1` opens it
        # expanded, the "Settings" and "Page only" buttons toggle.
        page = c.get(f"/d/{nav_id}").text
        check("the navigator opens as the page only", "op-page-only" in page)
        check("the navigator has a \"Page only\" button", 'id="btn-page-only"' in page)
        check("the button over the page is \"Settings\", not Exit",
              "ic-settings" in page.split('id="present-exit"', 1)[1][:300])
        full = c.get(f"/d/{nav_id}?full=1").text
        check("?full=1 opens expanded", "op-page-only" in full)  # the script is there, the URL decides
        rep_page = c.get(f"/d/{rep_id}").text
        check("an ordinary report is not touched by navigator mode",
              "op-page-only" not in rep_page and 'id="btn-page-only"' not in rep_page)

        # ── new version and restore keep the type ────────────────────────
        # detect_type has no signature for a navigator, so a re-detect turned it
        # into `static`: 409 type_change first, then a silent flip on restore.
        login(c, "owner1")
        r = c.post(f"/api/dashboards/{nav_id}/version",
                   files={"html_file": ("nav2.html", PAGE_HTML_V2, "text/html")})
        check("a new navigator version uploads without a type_change 409",
              r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        db = SessionLocal()
        try:
            d = db.query(Dashboard).filter(Dashboard.id == nav_id).first()
            check("after a new version the type is still page",
                  d is not None and d.dash_type == DASH_PAGE and d.current_version == 2,
                  d and (d.dash_type, d.current_version))
        finally:
            db.close()
        # A version bump is served stale-while-revalidate for a moment (see
        # smoke_dashboard_restore): wait for convergence, not for request #1.
        def serves(marker, tries=12, pause=0.4):
            for _ in range(tries):
                body = c.get(f"/render/{nav_id}/").text
                if marker in body:
                    return True
                time.sleep(pause)
            return False
        check("the new version's own link is served", serves("pbirs.example.com/v2"))
        check("the new version carries the bridge", "op_open" in c.get(f"/render/{nav_id}/").text)
        check("the navigator is still listed among navigators",
              nav_id in c.get("/navigators").text)

        r = c.post(f"/api/dashboards/{nav_id}/versions/1/restore")
        check("restore of a navigator version works", r.status_code == 200, r.text[:160])
        db = SessionLocal()
        try:
            d = db.query(Dashboard).filter(Dashboard.id == nav_id).first()
            check("after restore the type is still page",
                  d is not None and d.dash_type == DASH_PAGE and d.current_version == 3,
                  d and (d.dash_type, d.current_version))
        finally:
            db.close()
        check("restored navigator carries the bridge", "op_open" in c.get(f"/render/{nav_id}/").text)

        # An ordinary report is untouched by this: its type is still detected.
        r = c.post(f"/api/dashboards/{rep_id}/version",
                   files={"html_file": ("r2.html", REPORT_HTML, "text/html")})
        check("an ordinary report still takes a new version", r.status_code == 200, r.text[:160])

        # ── share link: the same bridge as for the owner ─────────────────
        # An anonymous reader via /s/ must reach Power BI too: without the
        # bridge a link opened a sandboxed tab without cookies — a white screen.
        login(c, "owner1")
        r = c.post(f"/api/dashboards/{nav_id}/share-links", json={"expires_in_days": 1})
        check("share link for a navigator is issued", r.status_code == 200, r.text[:160])
        tok = (r.json().get("url") or "").rsplit("/", 1)[-1] if r.status_code == 200 else ""
        anon = TestClient(app)
        share_page = anon.get(f"/s/{tok}").text
        check("share shell loads the link rules", "page-links.js" in share_page)
        check("share shell receives op_open", "op_open" in share_page)
        check("share shell can say what became of a link",
              'id="page-open-note"' in share_page)
        share_doc = anon.get(f"/share/{tok}/render/").text
        check("share render of a navigator carries the bridge", "op_open" in share_doc,
              share_doc[:200])
        check("the Power BI link is intact in the share render", "pbirs.example.com" in share_doc)
        r = c.post(f"/api/dashboards/{rep_id}/share-links", json={"expires_in_days": 1})
        rep_tok = (r.json().get("url") or "").rsplit("/", 1)[-1]
        rep_share = anon.get(f"/s/{rep_tok}").text
        check("an ordinary report via share has no navigator note box",
              'id="page-open-note"' not in rep_share)
        check("share render of an ordinary report does NOT get the bridge",
              "op_open" not in anon.get(f"/share/{rep_tok}/render/").text)

        # ── mirror ───────────────────────────────────────────────────────
        login(c, "owner1")
        r = c.get("/api/sync/manifest")
        item = next((x for x in (r.json().get("items") or []) if x.get("id") == nav_id), None)
        check("the mirror skips the navigator and says why",
              item is not None and item.get("mirrorable") is False
              and item.get("skip_reason") == "navigator",
              str(item)[:160] if item else "not in the manifest")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("navigators: all checks passed")


if __name__ == "__main__":
    main()
