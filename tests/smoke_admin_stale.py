""""Inactive employee accounts" (/admin, users) — GET /api/admin/users/stale
and the deactivation it drives.

Synthetic only: no real names, no real person_ids.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="op-test-")
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


_ctx = TestClient(app)
client = _ctx.__enter__()  # runs lifespan: creates tables, seeds admin1

client.post("/dev-login", data={"person_id": "admin1"})

from app.database import SessionLocal  # noqa: E402
from app.models import Employee, User  # noqa: E402

NOW = datetime.utcnow()

# ── seed three synthetic accounts ────────────────────────────────────────
# alpha1   — active OnePage user, Employee row exists but employment ended:
#            must be a stale-user finding.
# beta2    — active OnePage user, Employee row exists and still active:
#            must NOT appear anywhere.
# gamma3   — active OnePage user, no Employee row at all: not an individual
#            finding, but counted in "no_employee_record".
# delta4/epsilon5 — a real person can carry TWO Employee rows for one
#            person_id (a closed position plus a current one; the CSV
#            export is `WHERE isactive='1'` unioned with history, so this
#            pair is routine, not an edge case) both stamped with the SAME
#            synced_at from one import. The still-active row must win no
#            matter which row the SQL scan happens to return last — tested
#            in both insertion orders so a "last one wins" regression would
#            fail regardless of which order SQLite picks.
db = SessionLocal()
for oid, role in (("alpha1", "visitor"), ("beta2", "visitor"), ("gamma3", "visitor"),
                   ("delta4", "visitor"), ("epsilon5", "visitor")):
    u = User(person_id=oid, role=role, is_active=True)
    db.add(u)
db.commit()
alpha = db.query(User).filter(User.person_id == "alpha1").first()
alpha.last_login = NOW - timedelta(days=40)
db.add(Employee(id=9001, person_id="alpha1", lastname_ru="Testov", is_active=False,
                 deptname_ru="Test Department", synced_at=NOW))
db.add(Employee(id=9002, person_id="beta2", lastname_ru="Checker", is_active=True,
                 synced_at=NOW))
# delta4: closed position inserted first (lower id), current position
# inserted second (higher id) — a naive ">= wins" scan happening to see
# rows in id order would let the active row win here already.
db.add(Employee(id=9003, person_id="delta4", lastname_ru="Doubler", is_active=False,
                 deptname_ru="Old Department", synced_at=NOW))
db.add(Employee(id=9004, person_id="delta4", lastname_ru="Doubler", is_active=True,
                 deptname_ru="New Department", synced_at=NOW))
# epsilon5: same shape but reversed insertion order — current position
# (active) inserted first (lower id), closed position inserted second
# (higher id). This is the order that broke the old "last row wins" logic.
db.add(Employee(id=9005, person_id="epsilon5", lastname_ru="Twofold", is_active=True,
                 deptname_ru="New Department", synced_at=NOW))
db.add(Employee(id=9006, person_id="epsilon5", lastname_ru="Twofold", is_active=False,
                 deptname_ru="Old Department", synced_at=NOW))
db.commit()
db.close()

# ── access gate: a plain visitor cannot read the stale-users endpoint ────
client.post("/dev-login", data={"person_id": "gamma3"})
r = client.get("/api/admin/users/stale")
check("plain user: stale-users endpoint denied", r.status_code in (401, 403), str(r.status_code))

# ── admin view ────────────────────────────────────────────────────────
client.post("/dev-login", data={"person_id": "admin1"})
r = client.get("/api/admin/users/stale")
check("admin: stale-users endpoint 200", r.status_code == 200, r.text[:200])
body = r.json()

items = body.get("items", [])
by_oid = {it["person_id"]: it for it in items}
check("stale list includes alpha1 (inactive Employee row)", "alpha1" in by_oid, str(by_oid))
check("stale list excludes beta2 (Employee still active)", "beta2" not in by_oid, str(by_oid))
check("stale list excludes gamma3 (no Employee row at all)", "gamma3" not in by_oid, str(by_oid))
check("stale list excludes delta4 (dup Employee rows, one still active, active row inserted last)",
      "delta4" not in by_oid, str(by_oid))
check("stale list excludes epsilon5 (dup Employee rows, one still active, active row inserted first)",
      "epsilon5" not in by_oid, str(by_oid))
check("stale list is exactly one row", len(items) == 1, str(items))
if "alpha1" in by_oid:
    check("stale row carries last_seen", by_oid["alpha1"]["last_seen"] is not None, str(by_oid["alpha1"]))
    check("stale row carries dept from Employee", by_oid["alpha1"]["dept"] == "Test Department",
          str(by_oid["alpha1"]))
# admin1 (the bootstrap admin) also has no Employee row, so
# no_employee_record covers both it and gamma3 — never alpha1/beta2, which
# do have Employee rows.
db = SessionLocal()
no_emp_users = {u.person_id for u in db.query(User).filter(User.is_active == True).all()  # noqa: E712
                if u.person_id not in ("alpha1", "beta2", "delta4", "epsilon5")}
db.close()
check("no_employee_record matches the actual no-Employee active-user count",
      body.get("no_employee_record") == len(no_emp_users),
      f"{body.get('no_employee_record')} vs {len(no_emp_users)} ({no_emp_users})")

# ── deactivation reuses the existing PATCH path + session revocation ────
alpha_client = TestClient(app)
r = alpha_client.post("/dev-login", data={"person_id": "alpha1"})  # alpha1 logs in for real
check("alpha1: dev-login ok", r.status_code in (200, 302), str(r.status_code))

db = SessionLocal()
alpha_id = db.query(User).filter(User.person_id == "alpha1").first().id
db.close()

from sqlalchemy import text as _sql_text  # noqa: E402
from app.database import engine as _engine  # noqa: E402
with _engine.connect() as _c:
    sessions_before = _c.execute(
        _sql_text("SELECT count(*) FROM sessions WHERE user_id = :uid"), {"uid": alpha_id}
    ).scalar()
check("alpha1 has a live session before deactivation", sessions_before and sessions_before > 0,
      str(sessions_before))

r = client.patch(f"/api/admin/users/{alpha_id}", json={"is_active": False})
check("deactivate via existing PATCH endpoint: 200", r.status_code == 200, r.text[:200])

db = SessionLocal()
alpha_after = db.get(User, alpha_id)
check("alpha1 is now inactive", alpha_after.is_active is False)
db.close()

r = client.get("/api/admin/users/stale")
body2 = r.json()
check("alpha1 no longer appears in stale list once deactivated",
      "alpha1" not in {it["person_id"] for it in body2.get("items", [])}, str(body2))

# alpha1's own session(s) must be gone (SqliteSessionStore.delete_for_user,
# the same call update_user already makes on is_active=False).
with _engine.connect() as _c:
    sessions_after = _c.execute(
        _sql_text("SELECT count(*) FROM sessions WHERE user_id = :uid"), {"uid": alpha_id}
    ).scalar()
check("alpha1's sessions were revoked on deactivation", sessions_after == 0, str(sessions_after))

from app.models import AuditLog  # noqa: E402
db = SessionLocal()
audit = (db.query(AuditLog).filter(AuditLog.action == "update_user",
                                    AuditLog.resource_id == str(alpha_id))
         .order_by(AuditLog.id.desc()).first())
check("deactivation is audited as update_user (existing update_user audit path)",
      audit is not None, str(audit))
if audit:
    detail = audit.get_detail()
    check("audit detail carries is_active: False", detail.get("is_active") is False, str(detail))
db.close()

_ctx.__exit__(None, None, None)

if FAILURES:
    print(f"\n{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("\nALL PASS")
