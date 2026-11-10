"""Regression: a foreign origin must not be able to change state.

The production session cookie is SameSite=None — required, or the sandboxed
render iframe never gets it — so the browser stops filtering cross-site writes
for us. Nothing replaced that: no token, no Origin check, no CORS middleware.

Two shapes matter, and both are CORS-"simple" so no preflight ever protects them:

  * form/multipart posts, forgeable from any page with a plain <form>;
  * a JSON body sent with NO Content-Type — FastAPI still parses it as JSON
    (`if not content_type_value: json_body = await request.json()`), and
    fetch() with an untyped Blob produces exactly that.

The attacker is closer than a hostile website: an uploaded dashboard is
same-site JS running while an administrator views it. Its iframe has an opaque
origin, so it sends `Origin: null` — pinned below as the case that must fail.
"""
import os
import sqlite3
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-csrf-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_SAML_SP_BASE_URL": "http://testserver",
})

sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


SELF = "http://testserver"
EVIL = "https://evil.example"
HTML = b"<html><head><title>d</title></head><body><h1>r</h1></body></html>"


def users_named(person_id):
    """Straight from the DB — there is no list-users API to ask."""
    con = sqlite3.connect(f"{TMP}/onepage.db")
    try:
        return con.execute("SELECT COUNT(*) FROM users WHERE person_id = ?",
                           (person_id,)).fetchone()[0]
    finally:
        con.close()


with TestClient(app) as c:
    c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)

    r = c.post("/api/dashboards", data={"title": "d", "description": ""},
               files={"html_file": ("d.html", HTML, "text/html")},
               follow_redirects=False)
    check("setup: dashboard created", r.status_code == 303, f"{r.status_code} {r.text[:150]}")
    dash_id = r.headers["location"].rsplit("/", 1)[1]

    # ---- the attack shapes must be refused ------------------------------
    r = c.post("/api/admin/users", json={"person_id": "attacker", "role": "administrator"},
               headers={"origin": "null"})
    check("Origin: null (sandboxed dashboard iframe) -> 403", r.status_code == 403,
          f"{r.status_code} {r.text[:150]}")

    r = c.post("/api/admin/users", json={"person_id": "attacker2", "role": "administrator"},
               headers={"origin": EVIL})
    check("foreign Origin -> 403", r.status_code == 403, f"{r.status_code} {r.text[:150]}")

    r = c.post("/api/admin/users", json={"person_id": "attacker3", "role": "administrator"},
               headers={"referer": EVIL + "/page"})
    check("foreign Referer -> 403", r.status_code == 403, f"{r.status_code} {r.text[:150]}")

    # The no-Content-Type trick: a CORS-simple request FastAPI still reads as JSON.
    r = c.post("/api/admin/users",
               content=b'{"person_id":"attacker4","role":"administrator"}',
               headers={"origin": EVIL})
    check("untyped JSON body from a foreign origin -> 403", r.status_code == 403,
          f"{r.status_code} {r.text[:150]}")

    sent_ct = "content-type" in {k.lower() for k in
                                 c.build_request("POST", "/x", content=b"{}").headers}
    r = c.post("/api/admin/users",
               content=b'{"person_id":"attacker5","role":"administrator"}')
    if sent_ct:
        print("SKIP untyped-body case: the test client always labels the body")
    else:
        check("untyped JSON body (no Origin at all) -> 403", r.status_code == 403,
              f"{r.status_code} {r.text[:150]}")

    # Form-shaped privilege grant — forgeable from a plain <form>.
    r = c.post(f"/api/dashboards/{dash_id}/permissions",
               data={"person_id": "attacker", "can_update_data": "true"},
               headers={"origin": EVIL})
    check("cross-origin form permission grant -> 403", r.status_code == 403,
          f"{r.status_code} {r.text[:150]}")

    for name in ("attacker", "attacker2", "attacker3", "attacker4", "attacker5"):
        n = users_named(name)
        check(f"no account named {name} was created", n == 0, f"found {n}")

    # ---- legitimate traffic must be untouched ---------------------------
    r = c.post("/api/admin/users", json={"person_id": "colleague", "role": "superuser"},
               headers={"origin": SELF})
    check("own Origin -> allowed", r.status_code in (200, 201),
          f"{r.status_code} {r.text[:150]}")
    check("the allowed request really created the account",
          users_named("colleague") == 1)

    r = c.post("/api/admin/users", json={"person_id": "colleague2", "role": "superuser"})
    check("no Origin (curl, health probe, this test client) -> allowed",
          r.status_code in (200, 201), f"{r.status_code} {r.text[:150]}")

    r = c.put(f"/api/dashboards/{dash_id}/localstorage", json={"k": "v"},
              headers={"origin": SELF})
    check("same-origin data write -> allowed", r.status_code == 200,
          f"{r.status_code} {r.text[:150]}")

    # Reads are never guarded — the render hot path must not pay for this.
    r = c.get(f"/render/{dash_id}/", headers={"origin": EVIL})
    check("GET /render with a foreign origin -> untouched", r.status_code == 200,
          f"{r.status_code}")

    # ADFS posts the assertion from the IdP's origin; the guard must let it by
    # and leave the verdict to signature validation in the handler.
    # SAML is unconfigured here, so the handler raises — and that is the proof:
    # reaching it at all means the guard did not refuse the foreign origin.
    try:
        r = c.post("/saml/acs", data={"SAMLResponse": "not-a-real-assertion"},
                   headers={"origin": "https://adfs.corp.example"})
        reached_handler = r.status_code != 403
        detail = f"{r.status_code} {r.text[:150]}"
    except Exception as exc:  # noqa: BLE001 - any handler-side failure proves passage
        reached_handler = "saml" in type(exc).__module__.lower() or "Saml" in repr(exc)
        detail = repr(exc)[:150]
    check("/saml/acs exempt from the origin check", reached_handler, detail)

# The operator switch must actually take the guard out of the stack: if a
# deployment answers under a name nobody wrote down, this is the hour-buying
# escape hatch, and an env var that quietly does nothing would be worse than
# not having one.
import subprocess  # noqa: E402

probe = subprocess.run(
    [sys.executable, "-c", """
import os, sys, tempfile
T = tempfile.mkdtemp()
os.environ.update({"OP_DB_PATH": T+"/d.db", "OP_UPLOADS_DIR": T+"/u",
                   "OP_PERF_DIR": T+"/p", "OP_DEV_BYPASS_AUTH": "true",
                   "OP_INITIAL_ADMIN": "admin1", "OP_CACHE_WARM": "0",
                   "OP_SAML_SP_BASE_URL": "http://testserver",
                   "OP_ORIGIN_GUARD": "false"})
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)
    r = c.post("/api/admin/users",
               json={"person_id": "offswitch", "role": "superuser"},
               headers={"origin": "https://evil.example"})
    print(r.status_code)
"""],
    capture_output=True, text=True, cwd=os.getcwd(),
)
off_status = (probe.stdout.strip().splitlines() or [""])[-1]
check("OP_ORIGIN_GUARD=false really removes the guard", off_status in ("200", "201"),
      f"{off_status} {probe.stderr[-200:]}")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
    sys.exit(1)
print("ALL PASS")
