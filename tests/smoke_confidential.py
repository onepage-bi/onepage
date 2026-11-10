""""Highly confidential": a second lock on a report (owner decision 2026-10-20).

A password on top of the access list, not instead of it. The test checks exactly
what the mode exists for: without the password the report is served by NO route,
and the ways out (link, file, mirror, chat) stay closed even with the password.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-conf-")
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
PW = "correct horse battery"


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
    did = (r.headers.get("location") or "").rstrip("/").rsplit("/", 1)[-1]
    assert did, f"{r.status_code} {r.headers}"
    return did


def main():
    with TestClient(app) as c:
        login(c, "admin1")
        for ov, role in (("owner1", "superuser"), ("view1", "visitor"),
                         ("stranger1", "visitor")):
            r = c.post("/api/admin/users", json={"person_id": ov, "role": role,
                                                 "display_name": ov})
            assert r.status_code == 200, r.text

        login(c, "owner1")
        did = make_dashboard(c, "Secret report")
        c.patch(f"/api/dashboards/{did}", json={"is_published": True})
        c.post(f"/api/dashboards/{did}/permissions/view1", json={"can_update_data": False})

        # The link is issued BEFORE the mode is turned on: it must die when the mode
        # is turned on, not keep working "from old memory".
        r = c.post(f"/api/dashboards/{did}/share-links", json={})
        token = (r.json() or {}).get("id", "") if r.status_code == 200 else ""
        check("link issued before the mode is on", bool(token), f"{r.status_code} {r.text[:160]}")

        # ── turning it on ────────────────────────────────────────────────
        r = c.put(f"/api/dashboards/{did}/confidential", json={"password": "short"})
        check("a short password is rejected", r.status_code == 422, r.status_code)

        r = c.put(f"/api/dashboards/{did}/confidential", json={"password": PW})
        check("the owner turns the mode on", r.status_code == 200 and r.json().get("enabled") is True,
              f"{r.status_code} {r.text[:160]}")

        # ── the owner is locked out too ──────────────────────────────────
        r = c.get(f"/render/{did}/")
        check("owner: rendering is locked until the password", r.status_code == 403, r.status_code)
        t = c.get(f"/d/{did}").text
        check("owner: the page shows the password form", 'id="conf-form"' in t)
        check("the lock page shows nothing from the report", "<h1>Secret report</h1>" not in t)

        # ── the ways out are closed to everyone, owner included ──────────
        r = c.post(f"/api/dashboards/{did}/share-links", json={})
        check("a link cannot be issued even to the owner", r.status_code == 403, r.status_code)
        r = c.get(f"/s/{token}", follow_redirects=False)
        check("the earlier link is dead", r.status_code == 403, r.status_code)
        r = c.get(f"/api/dashboards/{did}/standalone")
        check("single-file export is closed", r.status_code == 409, r.status_code)
        r = c.get(f"/api/dashboards/{did}/source")
        check("source is closed until the password", r.status_code == 403, r.status_code)

        # ── password ─────────────────────────────────────────────────────
        r = c.post(f"/api/dashboards/{did}/unlock", json={"password": "wrong"})
        check("wrong password: 403", r.status_code == 403, r.status_code)
        r = c.get(f"/render/{did}/")
        check("after a failure the lock stays", r.status_code == 403, r.status_code)

        r = c.post(f"/api/dashboards/{did}/unlock", json={"password": PW})
        check("correct password accepted", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        r = c.get(f"/render/{did}/")
        check("after the password the report is served", r.status_code == 200, r.status_code)
        t = c.get(f"/d/{did}").text
        check("after the password: the regular viewer", 'data-tab="settings"' in t and 'id="conf-form"' not in t)
        check("no Share tab in this mode", 'data-tab="share"' not in t)
        check("no single-file button in this mode", 'id="btn-download-standalone"' not in t)

        # An unlocked lock does not make an export legitimate.
        r = c.get(f"/api/dashboards/{did}/standalone")
        check("export stays closed after the password", r.status_code == 409, r.status_code)

        # ── changing the password voids existing unlocks ─────────────────
        r = c.put(f"/api/dashboards/{did}/confidential", json={"password": PW + "2"})
        check("password changed", r.status_code == 200, r.status_code)
        r = c.get(f"/render/{did}/")
        check("password change re-locked this same session", r.status_code == 403, r.status_code)
        r = c.post(f"/api/dashboards/{did}/unlock", json={"password": PW + "2"})
        check("the new password works", r.status_code == 200, r.status_code)

        # ── the access list is still in force ────────────────────────────
        login(c, "stranger1")
        r = c.post(f"/api/dashboards/{did}/unlock", json={"password": PW + "2"})
        check("a stranger cannot get in even with the right password", r.status_code == 403, r.status_code)
        r = c.get(f"/d/{did}")
        check("a stranger does not even see the lock page", r.status_code == 403, r.status_code)

        login(c, "view1")
        r = c.get(f"/render/{did}/")
        check("listed person: not let in without the password", r.status_code == 403, r.status_code)
        r = c.post(f"/api/dashboards/{did}/unlock", json={"password": PW + "2"})
        check("listed person: the password lets them in", r.status_code == 200, r.status_code)
        r = c.get(f"/render/{did}/")
        check("listed person: report served", r.status_code == 200, r.status_code)
        r = c.put(f"/api/dashboards/{did}/confidential", json={"password": None})
        check("a non-owner cannot turn the mode off", r.status_code == 403, r.status_code)

        # ── attempt counter ──────────────────────────────────────────────
        login(c, "stranger1")  # reset the session
        login(c, "view1")
        codes = [c.post(f"/api/dashboards/{did}/unlock", json={"password": "nope"}).status_code
                 for _ in range(7)]
        check("guessing hits 429", 429 in codes, str(codes))

        # ── mirror ───────────────────────────────────────────────────────
        login(c, "owner1")
        r = c.get("/api/sync/manifest")
        item = next((x for x in (r.json().get("items") or []) if x.get("id") == did), None)
        check("the manifest names the report", item is not None, r.text[:200])
        if item is not None:
            check("the mirror does not take the report to disk",
                  item.get("mirrorable") is False and item.get("skip_reason") == "confidential",
                  str({k: item.get(k) for k in ("mirrorable", "skip_reason")}))

        # ── turning it off ───────────────────────────────────────────────
        r = c.put(f"/api/dashboards/{did}/confidential", json={"password": None})
        check("the owner turns the mode off", r.status_code == 200 and r.json().get("enabled") is False,
              f"{r.status_code} {r.text[:160]}")
        r = c.get(f"/render/{did}/")
        check("after turning off the report opens without a password", r.status_code == 200, r.status_code)
        r = c.get(f"/s/{token}", follow_redirects=False)
        check("the old link came back to life with the mode off", r.status_code == 200, r.status_code)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("confidential: all checks passed")


if __name__ == "__main__":
    main()
