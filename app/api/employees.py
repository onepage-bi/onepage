"""HR directory (/admin/employees) — mirror of Greenplum's
`warehouse.v_employees`, loaded by an administrator's CSV upload.

Design decisions (owner, 2026-08-23):
- The server never connects to Greenplum itself — no driver dependency, no
  DB credentials on the host. The administrator exports the view as CSV
  (DBeaver/psql, `WHERE isactive = '1'`) and uploads it here; later an
  op-agent can POST to the same import endpoint.
- Role-relevant subset only: birthdate / mobilephone / gender / vacation*
  columns are dropped at import by the whitelist below, whatever the file
  carries.
- This is a *reference table*, not a display table: the page shows sync status
  and a search box; the data's real consumers are the user-suggest
  typeahead (grant-access / add-user forms) and the future role model,
  joined to OnePage accounts by `person_id`.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from jinja2 import Environment
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.i18n import _
from app.config import settings
from app.auth.deps import get_current_user, require_permission
from app.database import get_db
from app.models import AuditLog, Employee, EmployeeSync, User

router = APIRouter()

MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_ROWS = 50_000
SUGGEST_LIMIT = 8

# CSV header (as Greenplum names it, lowercased) → Employee attribute.
# Anything not listed — including birthdate, mobilephone, gender and the
# vacation columns — never reaches the database.
COLUMN_MAP = {
    "id": "id",
    "person_id": "person_id",
    "employeeid": "employeeid",
    "emailaddress": "email",
    "firstnameru": "firstname_ru",
    "middleinitialru": "middleinitial_ru",
    "lastnameru": "lastname_ru",
    "firstnameen": "firstname_en",
    "lastnameen": "lastname_en",
    "companycode": "companycode",
    "companyname": "companyname",
    "deptcode": "deptcode",
    "deptnameru": "deptname_ru",
    "deptnameen": "deptname_en",
    "jobtitleru": "jobtitle_ru",
    "jobtitleen": "jobtitle_en",
    "jobgrade": "jobgrade",
    "managerid": "managerid",
    "hrbpid": "hrbpid",
    "officecitycode": "officecitycode",
    "officeregion": "officeregion",
    "officecityru": "officecity_ru",
    "employmenttype": "employmenttype",
    "hrstatus": "hrstatus",
    "positionstartdate": "positionstartdate",
    "hiredate": "hiredate",
    "inactivationdate": "inactivationdate",
}



def _effective_column_map() -> dict[str, str]:
    """COLUMN_MAP plus the headers declared by the site.

    The whitelist deliberately stays a whitelist: an alias may point to an already
    existing field, but cannot introduce a new one. Otherwise the setting would become
    a hole in the very list that decides which columns of the HR export
    reach the database at all (`birthdate`, `mobilephone`, `gender` do not).

    An unknown field on the right is not an import error but a configuration error, and
    it must not be kept quiet: the header simply would not arrive, the field would stay empty,
    and nobody would notice. So such an entry is skipped loudly.
    """
    extra = (settings.employee_column_aliases or "").strip()
    if not extra:
        return COLUMN_MAP
    merged = dict(COLUMN_MAP)
    known = set(COLUMN_MAP.values())
    for pair in extra.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        header, _, attr = pair.partition("=")
        header = header.strip().lower()
        attr = attr.strip()
        if not header or attr not in known:
            logging.getLogger("onepage.employees").warning(
                "employee column alias ignored: %r -> %r (the right side must "
                "name one of COLUMN_MAP's fields)", header, attr)
            continue
        merged[header] = attr
    return merged


def _log(db: Session, actor: User, action: str, detail: dict, ip: str = "") -> None:
    entry = AuditLog(
        actor_id=actor.id, action=action,
        resource_type="employees", resource_id="",
        ip_address=ip,
    )
    entry.set_detail(detail)
    db.add(entry)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise HTTPException(status_code=415, detail=_("File cannot be read as UTF-8 or CP1251"))


def _int_id(raw) -> int:
    """`id` as exported by DBeaver/Excel: '9499', '9499.0', ' 9499 '. Anything
    else is a 422 with the offending value — never a silently skipped row."""
    txt = str(raw).strip()
    try:
        return int(txt)
    except ValueError:
        pass
    try:
        f = float(txt.replace(",", "."))
        if f.is_integer():
            return int(f)
    except ValueError:
        pass
    raise HTTPException(status_code=422, detail=_("id is not a number: {p1}", p1=repr(raw)))


def _rows_from_csv(text: str) -> list[dict]:
    """Parse the export into dicts keyed by our attribute names.

    Delimiter is sniffed from the header line (`;` beats `,` when both
    appear — DBeaver's Russian-locale default). Requires `id` and
    `person_id` columns so a wrong file fails loudly, not as 0 rows.
    """
    first_line = text.split("\n", 1)[0]
    delim = ";" if first_line.count(";") >= first_line.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    headers = [h.strip().lower().lstrip("﻿") for h in (reader.fieldnames or [])]
    if "id" not in headers or "person_id" not in headers:
        raise HTTPException(
            status_code=422,
            detail=_("File does not have id and person_id columns – not a v_employees export"),
        )
    colmap = _effective_column_map()
    rows: list[dict] = []
    for rec in reader:
        if len(rows) >= MAX_IMPORT_ROWS:
            raise HTTPException(status_code=413, detail=_("More than {p1} rows", p1=MAX_IMPORT_ROWS))
        row: dict = {}
        for key, value in rec.items():
            if key is None:
                continue
            attr = colmap.get(key.strip().lower().lstrip("﻿"))
            if not attr:
                continue
            row[attr] = value.strip() if isinstance(value, str) else value
        if not row.get("id"):
            continue  # blank trailing line
        row["id"] = _int_id(row["id"])
        # Integer-looking columns suffer the same float export ('900.0').
        for k in ("employeeid", "managerid"):
            v = row.get(k)
            if isinstance(v, str) and v.endswith(".0") and v[:-2].isdigit():
                row[k] = v[:-2]
        rows.append(row)
    return rows


def _search_blob(e: Employee) -> str:
    parts = (
        e.lastname_ru, e.firstname_ru, e.middleinitial_ru,
        e.lastname_en, e.firstname_en, e.person_id, e.email,
    )
    # Python .lower() handles Cyrillic; SQLite's LIKE/lower() would not.
    return " ".join(p for p in parts if p).lower()


@router.get("/admin/employees", response_class=HTMLResponse)
async def employees_page(
    request: Request,
    user: User = Depends(require_permission("view_admin")),
    db: Session = Depends(get_db),
):
    last_sync = db.query(EmployeeSync).order_by(EmployeeSync.id.desc()).first()
    actor = None
    if last_sync and last_sync.actor_id:
        actor = db.get(User, last_sync.actor_id)
    active_count = db.query(func.count(Employee.id)).filter(Employee.is_active.is_(True)).scalar() or 0
    total_count = db.query(func.count(Employee.id)).scalar() or 0
    from app.services import account_hygiene
    stale_count = len(account_hygiene.stale_users(db)["items"])
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(
        env.get_template("pages/admin_employees.html").render,
        user=user, request=request,
        last_sync=last_sync, last_actor=(actor.person_id if actor else None),
        active_count=active_count, total_count=total_count,
        stale_count=stale_count,
    ))


@router.post("/api/admin/employees/import")
async def import_employees(
    request: Request,
    file: UploadFile = File(...),
    actor: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    raw = await file.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail=_("File is larger than 10 MB"))
    # Decoding + csv parsing of a 10 MB file is GIL-bound CPU — off the loop.
    rows = await run_in_threadpool(lambda: _rows_from_csv(_decode(raw)))
    if not rows:
        raise HTTPException(status_code=422, detail=_("File has no data rows"))

    def _sync() -> dict:
        existing = {e.id: e for e in db.query(Employee).all()}
        # A Greenplum id can repeat inside one export (two positions, two
        # exports concatenated): the LAST row wins, never a second INSERT.
        by_id: dict[int, dict] = {}
        for row in rows:
            by_id[row["id"]] = row
        rows_unique = list(by_id.values())
        seen: set[int] = set()
        added = updated = 0
        now = datetime.utcnow()
        for row in rows_unique:
            eid = row["id"]
            seen.add(eid)
            emp = existing.get(eid)
            if emp is None:
                emp = Employee(id=eid)
                db.add(emp)
                added += 1
            else:
                updated += 1
            for attr, value in row.items():
                if attr != "id":
                    setattr(emp, attr, value or None)
            # The export is the *active* snapshot (`WHERE isactive='1'`);
            # a row's own isactive column, if present, is not in COLUMN_MAP.
            emp.is_active = True
            emp.synced_at = now
            emp.search_blob = _search_blob(emp)
        deactivated = 0
        for eid, emp in existing.items():
            if eid not in seen and emp.is_active:
                emp.is_active = False
                emp.synced_at = now
                deactivated += 1
        sync = EmployeeSync(
            actor_id=actor.id, source="csv",
            total=len(rows_unique), added=added, updated=updated, deactivated=deactivated,
        )
        db.add(sync)
        _log(db, actor, "employees_import", {
            "total": len(rows), "added": added,
            "updated": updated, "deactivated": deactivated,
        }, request.client.host if request.client else "")
        db.commit()
        from app.services import account_hygiene
        stale_count = len(account_hygiene.stale_users(db)["items"])
        return {"total": len(rows_unique), "added": added, "updated": updated,
                "deactivated": deactivated, "at": now.isoformat(),
                "stale_users_count": stale_count}

    return await run_in_threadpool(_sync)


@router.get("/api/employees/suggest")
async def suggest_employees(
    q: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Typeahead over the directory — any signed-in user (the grant-access
    form on a dashboard is an owner's tool, not an admin's). Matches every
    whitespace-separated term against names ru/en + person_id + email,
    case-folded in Python because SQLite can't lower() Cyrillic. Returns
    directory-card fields only."""
    terms = [t for t in q.lower().split() if t]
    if not terms or all(len(t) < 2 for t in terms):
        return {"items": []}

    def _search() -> dict:
        items = []
        # `search_blob` is written lower-cased at import, so SQLite's
        # byte-wise LIKE does the narrowing for every term (Cyrillic
        # included) and only the candidates are hydrated — not the directory.
        q = db.query(Employee).filter(Employee.is_active.is_(True))
        for t in terms:
            q = q.filter(Employee.search_blob.like("%" + t.replace("%", "").replace("_", "") + "%"))
        for e in q.limit(SUGGEST_LIMIT * 4).all():
            blob = e.search_blob or _search_blob(e)
            if all(t in blob for t in terms):
                items.append({
                    "person_id": e.person_id,
                    "name_ru": " ".join(p for p in (e.lastname_ru, e.firstname_ru) if p),
                    "name_en": " ".join(p for p in (e.firstname_en, e.lastname_en) if p),
                    "dept": e.deptname_ru or e.deptname_en or "",
                    "title": e.jobtitle_ru or e.jobtitle_en or "",
                })
                if len(items) >= SUGGEST_LIMIT:
                    break
        return {"items": items}

    return await run_in_threadpool(_search)
