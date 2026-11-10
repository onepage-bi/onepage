"""Employee directory (/admin/employees): CSV import, suggest typeahead,
permission gates, sensitive-field whitelist."""
import os
import sys
import tempfile

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


# Cyrillic names on purpose: suggest must case-fold Cyrillic in Python
# (SQLite lower() does not), so the ru name fields and the queries stay Russian.
CSV = "﻿" + "\n".join([
    "id;person_id;employeeid;emailaddress;firstnameru;lastnameru;firstnameen;lastnameen;"
    "deptnameru;jobtitleru;jobgrade;managerid;birthdate;mobilephone;gender;vacationbalance;isactive",
    "101;ivanov1;E101;ivanov@corp.example;Иван;Иванов;Ivan;Ivanov;Logistics;Analyst;36;900;1990-01-01;+7 701 000 00 00;M;12;1",
    "102;petrova2;E102;petrova@corp.example;Мария;Петрова;Maria;Petrova;Finance;Economist;34;900;1991-02-02;+7 702 000 00 00;F;5;1",
])

CSV_V2 = "\n".join([
    "id,person_id,emailaddress,firstnameru,lastnameru,deptnameru",
    "101,ivanov1,ivanov@corp.example,Иван,Иванов,Warehouse",
    "103,new3,new3@corp.example,Пётр,Новиков,IT",
])

_ctx = TestClient(app)
client = _ctx.__enter__()  # run lifespan (create tables, seed admin)

# ── access gates ────────────────────────────────────────────────────────
client.post("/dev-login", data={"person_id": "admin1"})
r = client.post("/api/admin/users", json={"person_id": "plainuser", "role": "visitor"})
assert r.status_code == 200, r.text
r = client.post("/dev-login", data={"person_id": "plainuser"}, follow_redirects=False)
assert r.status_code == 302, r.status_code
r = client.get("/admin/employees", follow_redirects=False)
check("plain user: /admin/employees denied", r.status_code in (303, 401, 403), str(r.status_code))
r = client.post("/api/admin/employees/import", files={"file": ("e.csv", CSV.encode(), "text/csv")})
check("plain user: import denied", r.status_code in (401, 403), str(r.status_code))
r = client.get("/api/employees/suggest", params={"q": "ив"})
check("plain user: suggest allowed (grant-access is an owner tool)", r.status_code == 200, str(r.status_code))
check("suggest on empty directory: []", r.json() == {"items": []}, str(r.json()))

# ── admin import ────────────────────────────────────────────────────────
client.post("/dev-login", data={"person_id": "admin1"})
r = client.get("/admin/employees")
check("admin: page 200", r.status_code == 200, str(r.status_code))
check("page: status card present", "Refresh from export" in r.text)

r = client.post("/api/admin/employees/import", files={"file": ("e.csv", CSV.encode("utf-8"), "text/csv")})
check("import: 200", r.status_code == 200, r.text[:200])
body = r.json()
check("import: total 2, added 2", body["total"] == 2 and body["added"] == 2, str(body))

from app.database import SessionLocal  # noqa: E402
from app.models import Employee, EmployeeSync  # noqa: E402
db = SessionLocal()
emp = db.get(Employee, 101)
check("row: names ru/en stored", emp.lastname_ru == "Иванов" and emp.lastname_en == "Ivanov")
check("row: dept + grade stored", emp.deptname_ru == "Logistics" and emp.jobgrade == "36")
cols = {c.name for c in Employee.__table__.columns}
check("model: no sensitive columns at all",
      not ({"birthdate", "mobilephone", "gender"} & cols) and not any("vacation" in c for c in cols),
      str(sorted(cols)))
sync = db.query(EmployeeSync).order_by(EmployeeSync.id.desc()).first()
check("sync row recorded", sync is not None and sync.total == 2 and sync.source == "csv")
db.close()

# ── suggest ─────────────────────────────────────────────────────────────
r = client.get("/api/employees/suggest", params={"q": "иванов"})
items = r.json()["items"]
check("suggest: Cyrillic lastname, case-folded", len(items) == 1 and items[0]["person_id"] == "ivanov1", str(items))
r = client.get("/api/employees/suggest", params={"q": "PETROVA"})
items = r.json()["items"]
check("suggest: English lastname uppercase", len(items) == 1 and items[0]["person_id"] == "petrova2", str(items))
r = client.get("/api/employees/suggest", params={"q": "мария петрова"})
check("suggest: two terms both match", len(r.json()["items"]) == 1)
r = client.get("/api/employees/suggest", params={"q": "и"})
check("suggest: 1-char query returns nothing", r.json() == {"items": []})
r = client.get("/api/employees/suggest", params={"q": "ivanov1"})
check("suggest: by person_id", len(r.json()["items"]) == 1)

# ── re-import: upsert + deactivate ─────────────────────────────────────
r = client.post("/api/admin/employees/import", files={"file": ("e2.csv", CSV_V2.encode(), "text/csv")})
body = r.json()
check("re-import: comma delimiter, +1 new, 1 updated, 1 deactivated",
      body["added"] == 1 and body["updated"] == 1 and body["deactivated"] == 1, str(body))
db = SessionLocal()
check("re-import: dept updated", db.get(Employee, 101).deptname_ru == "Warehouse")
check("re-import: petrova deactivated", db.get(Employee, 102).is_active is False)
# A column absent from the FILE keeps its previous value (partial export
# must not wipe the directory); an empty CELL nulls it.
check("re-import: column absent from file keeps old value", db.get(Employee, 101).jobgrade == "36")
db.close()
r = client.get("/api/employees/suggest", params={"q": "петрова"})
check("suggest: inactive not suggested", r.json() == {"items": []}, str(r.json()))

# ── duplicate id inside ONE file: last row wins, no IntegrityError ─────
DUP = "id;person_id;emailaddress;firstnameru;lastnameru\n105;dup5;a@corp.example;Первый;Дубль\n105;dup5;b@corp.example;Второй;Дубль\n"
r = client.post("/api/admin/employees/import", files={"file": ("d.csv", DUP.encode(), "text/csv")})
check("duplicate id in one file → 200, counted once", r.status_code == 200 and r.json()["total"] == 1, r.text[:200])
db = SessionLocal()
check("duplicate id: the LAST row wins", db.get(Employee, 105).firstname_ru == "Второй")
check("search_blob written at import", "дубль" in (db.get(Employee, 105).search_blob or ""))
db.close()
r = client.get("/api/employees/suggest", params={"q": "ДУБЛЬ втор"})
check("suggest: SQL-prefiltered blob still case-folds Cyrillic", len(r.json()["items"]) == 1, r.text[:200])

# ── float-formatted ids (DBeaver/Excel export: '9499.0') ───────────────
FLOAT = "id;person_id;employeeid;managerid;firstnameru;lastnameru\n9499.0;flt9;E9499.0;900.0;Float;Export\n"
r = client.post("/api/admin/employees/import", files={"file": ("f.csv", FLOAT.encode(), "text/csv")})
check("id '9499.0' imports as 9499", r.status_code == 200 and r.json()["total"] == 1, r.text[:200])
db = SessionLocal()
_e = db.get(Employee, 9499)
check("float ids normalised in employeeid/managerid too", _e is not None and _e.employeeid == "E9499.0" and _e.managerid == "900",
      _e and (_e.employeeid, _e.managerid))
db.close()
r = client.post("/api/admin/employees/import", files={"file": ("f.csv", b"id;person_id\n12.5;x\n", "text/csv")})
check("id '12.5' is still a 422", r.status_code == 422, str(r.status_code))

# ── grant access to someone who has never signed in ────────────────────
# The directory vouches for them: the grant CREATES the user (visitor,
# named from the directory) instead of a 404 "access cannot be granted".
from app.models import Dashboard as _D, User as _U2  # noqa: E402
db = SessionLocal()
_admin = db.query(_U2).filter(_U2.person_id == "admin1").first()
_dash = _D(id="perm-test-dash", title="T", owner_id=_admin.id, dash_type="static")
db.add(_dash); db.commit(); db.close()
r = client.post("/api/dashboards/perm-test-dash/permissions/flt9", json={"can_update_data": False})
check("grant to a directory-only person → 200 (user auto-created)", r.status_code == 200, r.text[:200])
db = SessionLocal()
_u = db.query(_U2).filter(_U2.person_id == "flt9").first()
check("auto-created as visitor with the directory name",
      _u is not None and _u.role == "visitor" and (_u.display_name or "").startswith("Export"),
      _u and (_u.role, _u.display_name))
db.close()
r = client.post("/api/dashboards/perm-test-dash/permissions/nosuch9", json={"can_update_data": False})
check("grant to nobody (not in the directory either) → 404 with an honest detail",
      r.status_code == 404 and "employee reference table" in r.text, r.text[:200])

# ── bad files ───────────────────────────────────────────────────────────
r = client.post("/api/admin/employees/import", files={"file": ("x.csv", "a;b\n1;2\n".encode(), "text/csv")})
check("wrong columns → 422", r.status_code == 422, str(r.status_code))
r = client.post("/api/admin/employees/import", files={"file": ("x.csv", b"\xff\xfe\x00\x01\x00", "text/csv")})
check("undecodable file → 4xx", r.status_code in (415, 422), str(r.status_code))
big = ("id;person_id\n" + "1;a\n" * 1).encode() + b"x" * (10 * 1024 * 1024)
r = client.post("/api/admin/employees/import", files={"file": ("x.csv", big, "text/csv")})
check("oversize → 413", r.status_code == 413, str(r.status_code))

# ── cp1251 fallback ─────────────────────────────────────────────────────
r = client.post("/api/admin/employees/import",
                files={"file": ("e.csv", CSV.replace("﻿", "").encode("cp1251"), "text/csv")})
check("cp1251 export imports", r.status_code == 200, r.text[:200])

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES")
    sys.exit(1)
print("ALL PASS")
