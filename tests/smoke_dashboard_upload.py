"""Smoke test for the HTML upload guard and the type-change confirmation
(app/api/dashboards.py: _validate_html_upload, replace_html 409).

Prod incident 2026-08-26 (Material Plan): the replace form submitted with no
file selected published a 0-byte version (blank report), and the repair
attempt uploaded an interactive shell over a static export — blank again,
this time for want of slot data. Both silent. These pins keep them loud.

Boot idiom from tests/smoke_favorites.py.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-upguard-smoke-")
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

STATIC_HTML = b"<html><body><h1>Report</h1></body></html>"
STATIC_HTML2 = b"<html><body><h1>Report v2</h1></body></html>"
INTERACTIVE_HTML = (b"<html><body><input type=\"file\" id=\"f0\" />"
                    b"<div id=\"app\"></div></body></html>")


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def create(c, title, html):
    return c.post("/api/dashboards", data={"title": title, "description": ""},
                  files={"html_file": ("d.html", html, "text/html")},
                  follow_redirects=False)


def replace(c, dash_id, html, name="d.html", extra=None):
    return c.post(f"/api/dashboards/{dash_id}/version",
                  data=extra or {},
                  files={"html_file": (name, html, "text/html")})


def main():
    with TestClient(app) as c:
        login(c, "admin1")

        # ── create-path guard ──────────────────────────────────────────
        r = create(c, "Empty", b"")
        check("create: 0-byte html -> 400", r.status_code == 400, str(r.status_code))
        r = create(c, "Blank", b"   \n\t  ")
        check("create: whitespace-only html -> 400", r.status_code == 400, str(r.status_code))
        r = create(c, "Garbage", b"\x00\x01\x02 just bytes, no markup at all 123")
        check("create: tagless bytes -> 400", r.status_code == 400, str(r.status_code))
        r = create(c, "Good", STATIC_HTML)
        check("create: real html -> 303", r.status_code == 303, f"{r.status_code} {r.text[:200]}")
        dash_id = r.headers["location"].rstrip("/").rsplit("/", 1)[-1]

        # ── replace-path guard ─────────────────────────────────────────
        r = replace(c, dash_id, b"")
        check("replace: 0-byte html -> 400", r.status_code == 400, str(r.status_code))
        check("replace: 400 message says the report was not replaced",
              "not replaced" in (r.json().get("detail") or ""), r.text[:200])
        r = replace(c, dash_id, b"PK\x03\x04 zip bytes with no markup")
        check("replace: non-html bytes -> 400", r.status_code == 400, str(r.status_code))

        r = replace(c, dash_id, STATIC_HTML2)
        check("replace: same-type html -> ok, version 2",
              r.status_code == 200 and r.json().get("version") == 2, r.text[:200])

        # ── type-change confirmation ───────────────────────────────────
        r = replace(c, dash_id, INTERACTIVE_HTML)
        check("replace: type change static->interactive -> 409", r.status_code == 409, str(r.status_code))
        d = (r.json().get("detail") or {}) if r.status_code == 409 else {}
        check("409 detail: code/from/to",
              d.get("code") == "type_change" and d.get("from") == "static" and d.get("to") == "interactive",
              str(d))

        r = replace(c, dash_id, INTERACTIVE_HTML, extra={"confirm_type_change": "1"})
        check("replace: confirmed type change -> ok, version 3",
              r.status_code == 200 and r.json().get("version") == 3, r.text[:200])

        # After a confirmed flip, replacing with the SAME (new) type needs
        # no confirmation again.
        r = replace(c, dash_id, INTERACTIVE_HTML)
        check("replace: same type after flip -> no confirm needed",
              r.status_code == 200 and r.json().get("version") == 4, r.text[:200])

        # A failed (guarded) replace must not have advanced the version.
        r = replace(c, dash_id, b"")
        check("guarded replace leaves current_version alone", r.status_code == 400, str(r.status_code))
        r = replace(c, dash_id, INTERACTIVE_HTML)
        check("next good replace is version 5",
              r.status_code == 200 and r.json().get("version") == 5, r.text[:200])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES:")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
