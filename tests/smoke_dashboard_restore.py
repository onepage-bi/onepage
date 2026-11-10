#!/usr/bin/env python3
"""Restoring a previous report version.

The "Restore" button was in the markup from the start and did nothing:
there was no handler, no server endpoint, and the page never even received the
version list, so the template loop was always empty. The test covers the whole
chain, because it can break again in any of the three places independently.

The main rule pinned here: **restore rolls forward**. The old version does not
retroactively become current; its content is laid on top as a new version.
History is not rewritten; the restore itself enters history and can be undone the same way.
Second: the data stays CURRENT. Slot files are copied from the current version,
not from the restored one, otherwise a single click would silently bring back
last week's numbers.
"""
import io
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="op-smoke-restore-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
})

sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient          # noqa: E402
from app.main import app                           # noqa: E402

FAILURES = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  ← {extra}"))
    if not ok:
        FAILURES.append(name)


def login(c, who="admin1"):
    c.post("/dev-login", data={"person_id": who})


def page(version_text):
    return ("<html><head><title>r</title></head><body>"
            f"<h1>{version_text}</h1></body></html>").encode()


with TestClient(app) as c:
    login(c)
    r = c.post("/api/dashboards", files={"html_file": ("r.html", io.BytesIO(page("v1")), "text/html")},
               data={"title": "Versions", "description": "", "is_published": "true"},
               follow_redirects=False)
    did = (r.headers.get("location") or "").rsplit("/", 1)[-1]
    check("report created", bool(did), r.status_code)

    for n in ("v2", "v3"):
        rv = c.post(f"/api/dashboards/{did}/version",
                    files={"html_file": ("r.html", io.BytesIO(page(n)), "text/html")})
        check(f"uploaded {n}", rv.status_code == 200, rv.text[:200])

    # ── the version list reaches the page ───────────────────────────────
    html = c.get(f"/d/{did}").text
    check("the page lists previous versions",
          'data-restore="1"' in html and 'data-restore="2"' in html,
          "no restore buttons in the panel")
    check("the current version is not offered for restore",
          'data-restore="3"' not in html)

    # ── restore rolls forward ───────────────────────────────────────────
    rr = c.post(f"/api/dashboards/{did}/versions/1/restore")
    check("restore answers", rr.status_code == 200, rr.text[:200])
    out = rr.json() if rr.status_code == 200 else {}
    check("restore creates a NEW version", out.get("version") == 4, out)
    check("restore says where it came from", out.get("restored_from") == 1, out)

    # A version change rotates the cache key, but the first requests after it are
    # normally served from the previous cell (stale-while-revalidate): the rebuild
    # runs in the background, debounced by 2 s. So wait for CONVERGENCE, not for a
    # specific request number: the contract is "the restored content is served
    # within a second or two", and that is exactly what the test checks.
    def serves(marker, tries=12, pause=0.4):
        for _ in range(tries):
            body = c.get(f"/render/{did}/").text
            if marker in body:
                return True
            time.sleep(pause)
        return False

    check("the restored content is what people now see", serves("v1"))

    # ── history intact: older versions remain and can be restored again ─
    again = c.post(f"/api/dashboards/{did}/versions/3/restore")
    check("history survives the restore", again.status_code == 200 and
          again.json().get("version") == 5, again.text[:200])
    check("and rolling forward again brings v3 back", serves("v3"))

    # ── refusals ────────────────────────────────────────────────────────
    cur = c.post(f"/api/dashboards/{did}/versions/5/restore")
    check("restoring the current version is refused", cur.status_code == 409, cur.status_code)
    missing = c.post(f"/api/dashboards/{did}/versions/99/restore")
    check("an unknown version is 404", missing.status_code == 404, missing.status_code)

    # ── someone else's report is off limits ────────────────────────────
    # dev-login does not create people itself (create=False), so the admin creates
    # the viewer; otherwise the "login" would silently keep the previous session.
    made = c.post("/api/admin/users", json={"person_id": "visitor9", "role": "visitor"})
    check("a viewer account exists for the check", made.status_code in (200, 201), made.text[:200])
    login(c, "visitor9")
    denied = c.post(f"/api/dashboards/{did}/versions/1/restore")
    check("a stranger cannot restore", denied.status_code in (403, 404), denied.status_code)
    page_html = c.get(f"/d/{did}")
    check("and does not even see the list",
          "data-restore" not in page_html.text or page_html.status_code != 200)

print()
if FAILURES:
    print("FAILURES: %d" % len(FAILURES))
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("smoke_dashboard_restore: all checks passed")
