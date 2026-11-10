"""Password sign-in (OP_LOCAL_USERS, app/auth/local.py).

What must hold:
  * the route and the form exist only when the list is set;
  * a right password on a known account opens a session, wrong ones do not,
    and a name that is not in the list fails the same way (no enumeration);
  * the list grants a password, not an account: an id without a `users`
    row is refused even with the right password;
  * every failure and success leaves an audit row;
  * the SSO button disappears when there is no IdP and a password list.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())
from app.auth import local as local_auth  # noqa: E402

H_ADMIN = local_auth.make_hash("correct horse", iterations=2000)
H_GHOST = local_auth.make_hash("ghost pw", iterations=2000)

TMP = tempfile.mkdtemp(prefix="op-local-login-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "false",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_SAML_SP_BASE_URL": "http://testserver",
    "OP_LOCAL_USERS": f" admin1={H_ADMIN} , ghost={H_GHOST},",
})

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import AuditLog  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


# --- pure helpers ----------------------------------------------------------
check("hash verifies", local_auth.verify("correct horse", H_ADMIN))
check("hash rejects other password", not local_auth.verify("correct horse ", H_ADMIN))
check("garbage stored hash rejects", not local_auth.verify("x", "nope"))
check("parse trims and skips blanks", set(local_auth.parse(os.environ["OP_LOCAL_USERS"])) == {"admin1", "ghost"})
try:
    local_auth.parse("admin1")
    check("parse refuses entry without hash", False)
except ValueError:
    check("parse refuses entry without hash", True)
check("enabled()", local_auth.enabled())

with TestClient(app) as c:
    r = c.get("/login")
    check("login page shows password form", 'action="/local-login"' in r.text)
    check("login page hides SSO without IdP", "/saml/login" not in r.text)
    check("dev bypass absent", "/dev-login" not in r.text)

    r = c.post("/local-login", data={"person_id": "admin1", "password": "wrong"}, follow_redirects=False)
    check("wrong password -> 401", r.status_code == 401, r.status_code)
    check("wrong password -> no cookie", "op_session" not in r.cookies and not r.cookies)
    check("error shown", "Wrong username or password" in r.text)

    r = c.post("/local-login", data={"person_id": "nobody", "password": "wrong"}, follow_redirects=False)
    check("unknown name -> same 401", r.status_code == 401, r.status_code)

    r = c.post("/local-login", data={"person_id": "ghost", "password": "ghost pw"}, follow_redirects=False)
    check("hash without users row -> 403", r.status_code == 403, r.status_code)

    r = c.post("/local-login", data={"person_id": " admin1 ", "password": "correct horse"}, follow_redirects=False)
    check("right password -> 302", r.status_code == 302, r.status_code)
    check("session cookie set", bool(r.cookies))
    r = c.get("/api/me")
    check("session works", r.status_code == 200 and r.json().get("person_id") == "admin1", r.status_code)

    db = SessionLocal()
    rows = db.query(AuditLog.action, AuditLog.resource_id).filter(AuditLog.action.like("local_login%")).all()
    db.close()
    check("audit rows", sorted(rows) == sorted([("local_login_failed", "admin1"), ("local_login_failed", "nobody"),
                                                 ("local_login_failed", "ghost"), ("local_login", "admin1")]), rows)

print()
print("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}")
sys.exit(1 if FAILURES else 0)
