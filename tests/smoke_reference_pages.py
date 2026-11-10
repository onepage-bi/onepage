"""Smoke test for ADR-16 reference tables — phase 6 pages: `GET /reference`
and `GET /reference/{slug}` (app/api/reference.py, bottom of file).

Boot idiom shared with tests/smoke_reference_api.py. Only the PAGE routes are
exercised here — the JSON/xlsx API is already pinned by that sibling file.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-ref-pages-smoke-")
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


SLUG = "ref_pages_tbl"


def main():
    with TestClient(app) as c:
        login(c, "admin1")
        r = c.post("/api/admin/users", json={"person_id": "vis1", "role": "visitor", "display_name": "Vis One"})
        check("second user created", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

        # ── empty list still renders (no tables yet) ────────────────────
        r = c.get("/reference")
        check("GET /reference -> 200", r.status_code == 200, str(r.status_code))
        check("GET /reference contains the page heading", "Reference tables" in r.text)
        check("GET /reference does not break the sidebar", '<aside class="sidebar">' in r.text, r.text[:200])
        check("GET /reference sidebar marks Reference tables active",
              'class="nav-item active" href="/reference"' in r.text
              or 'nav-item active' in r.text and 'href="/reference"' in r.text)

        r = c.post("/api/reference", json={
            "slug": SLUG, "title": "Reference page test table",
            "columns": [{"name": "K", "type": "text"}, {"name": "V", "type": "text"}],
        })
        check("create table for page test -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")

        r = c.get("/reference")
        check("GET /reference (populated) -> 200", r.status_code == 200, str(r.status_code))
        check("GET /reference lists the new table's slug", SLUG in r.text, r.text[:400])

        # ── detail page: readable by admin (creator) ─────────────────────
        r = c.get(f"/reference/{SLUG}")
        check("GET /reference/{slug} -> 200 for admin", r.status_code == 200, str(r.status_code))
        check("GET /reference/{slug} contains the title", "Reference page test table" in r.text, r.text[:400])
        check("GET /reference/{slug} does not break the sidebar", '<aside class="sidebar">' in r.text)

        # ── detail page: unknown slug -> 404 ──────────────────────────────
        r = c.get("/reference/does-not-exist-slug")
        check("GET /reference/{unknown} -> 404", r.status_code == 404, str(r.status_code))

        # ── detail page: a table with zero consumers is not readable by an
        #    unrelated visitor (same access rule as the JSON API) -> 403 ───
        login(c, "vis1")
        r = c.get(f"/reference/{SLUG}")
        check("GET /reference/{slug} -> 403 for an unrelated visitor", r.status_code == 403, str(r.status_code))

        # a visitor with no reach also sees an empty list, not an error
        r = c.get("/reference")
        check("GET /reference -> 200 for a plain visitor (empty list, not an error)",
              r.status_code == 200, str(r.status_code))
        check("GET /reference (visitor) does not leak the unreachable table's slug",
              SLUG not in r.text, r.text[:400])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
