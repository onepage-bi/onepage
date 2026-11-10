"""One person = one account (2026-08-28).

SSO hands us a UPN (`viktor_sidorov@example.com`, sometimes `10000001@example.com`),
while the employees directory — and therefore every account an admin creates
by granting access (`permissions._target_or_autocreate`) or by hand — keys
people by the numeric `person_id` (`10000002`). Before this module the two
never met: the login path created a SECOND account (visitor, no rights) next
to the one that held the person's permissions, and "nothing works
for me" was the symptom.

`resolve()` maps whatever the IdP sent to the canonical `person_id`:

* `10000001@example.com` → `10000001` (numeric local part, no lookup needed)
* `name@example.com`     → the active employee whose `email` matches
                           (case-insensitive) → their `person_id`
* anything else          → itself (an outsider with no directory row keeps
                           the UPN as their id, exactly as before)

`merge_duplicates()` folds the accounts the old behaviour created into the
canonical ones: every foreign key to `users.id` is moved (row by row, a
unique-constraint clash keeps the canonical row and drops the duplicate's),
the higher role wins, a missing display name is filled, the duplicate's
sessions are dropped so the person re-logs into the right account, and the
duplicate itself is DEACTIVATED and renamed `merged:<old id>` — never deleted,
so the audit trail and «who was this» keep working.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Base, Employee, User, ROLE_ADMINISTRATOR, ROLE_SUPERUSER, ROLE_VISITOR,
)

MERGED_PREFIX = "merged:"
_ROLE_RANK = {ROLE_VISITOR: 0, ROLE_SUPERUSER: 1, ROLE_ADMINISTRATOR: 2}


def _employee_by_email(db: Session, email: str) -> Employee | None:
    emp = (db.query(Employee)
           .filter(Employee.is_active == True,  # noqa: E712
                   Employee.email.isnot(None))
           .filter(text("lower(employees.email) = :m")).params(m=email.lower())
           .first())
    if emp is not None:
        return emp
    # Prod 2026-08-28: the directory spells contractors' mailboxes with an
    # `_External` suffix the IdP does not send (`viktor_sidorov@` vs
    # `Viktor_Sidorov_External@example.com`). Compared with the suffix
    # removed on both sides — and only when exactly ONE active employee
    # matches, so it can never guess between namesakes.
    local, _, domain = email.lower().partition("@")
    stem = local.replace("_external", "")
    cands = (db.query(Employee)
             .filter(Employee.is_active == True,  # noqa: E712
                     Employee.email.isnot(None))
             .filter(text("replace(lower(employees.email), '_external', '') = :m"))
             .params(m=f"{stem}@{domain}").all())
    return cands[0] if len(cands) == 1 else None


def employee_name(emp: Employee | None) -> str | None:
    if emp is None:
        return None
    return " ".join(x for x in [emp.lastname_ru or emp.lastname_en,
                                emp.firstname_ru or emp.firstname_en] if x) or None


def resolve(db: Session, raw: str) -> tuple[str, str | None]:
    """→ (canonical person_id, upn or None). Pure lookup, never writes."""
    raw = (raw or "").strip()
    if not raw or "@" not in raw or raw.startswith(MERGED_PREFIX):
        return raw, None
    local, _, _domain = raw.partition("@")
    if local.isdigit():
        return local, raw
    emp = _employee_by_email(db, raw)
    if emp is not None and (emp.person_id or "").strip():
        return emp.person_id.strip(), raw
    return raw, raw


def find_or_create(db: Session, raw: str, *, role: str = ROLE_VISITOR,
                   display_name: str | None = None, create: bool = True) -> User | None:
    """The one lookup every login and every grant goes through.

    Order: the canonical account → an account already created under the raw
    UPN by the pre-2026-08-28 login path (kept until `merge_duplicates` folds
    it) → create, named from the directory when it knows the person."""
    canon, upn = resolve(db, raw)
    user = db.query(User).filter(User.person_id == canon).first()
    if user is None and upn:
        # UPNs differ only by case between the IdP and what people typed
        # (`Polina_Petrova@example.com` vs `polina_petrova@example.com`).
        low = upn.lower()
        user = (db.query(User).filter(func.lower(User.person_id) == low).first()
                or db.query(User).filter(func.lower(User.upn) == low).first())
    if user is None:
        if not create:
            return None
        if display_name is None:
            emp = (db.query(Employee).filter(Employee.person_id == canon).first()
                   if canon.isdigit() else None)
            display_name = employee_name(emp)
        user = User(person_id=canon, upn=upn, role=role, display_name=display_name, is_active=True)
        db.add(user)
        db.flush()
        return user
    if upn and user.upn != upn:
        user.upn = upn
        db.flush()
    return user


# ── merging the duplicates the old login created ─────────────────────────

def _fk_columns_to_users() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            for fk in col.foreign_keys:
                if fk.column.table.name == "users" and fk.column.name == "id":
                    out.append((table.name, col.name))
    return out


def duplicates(db: Session) -> list[dict[str, Any]]:
    """Every account whose person_id resolves to ANOTHER existing account.

    Sequential on purpose: when two UPN accounts resolve to the same number
    and no numeric account exists (`10000005@example.com` + `boris_zaytsev@…`
    on prod), the FIRST one becomes the canonical account and the second
    merges INTO it — two renames would collide on the unique person_id."""
    users = db.query(User).order_by(User.id).all()
    by_ov: dict[str, User] = {u.person_id: u for u in users}
    found = []
    for u in users:
        if u.person_id.startswith(MERGED_PREFIX):
            continue
        canon, upn = resolve(db, u.person_id)
        if canon == u.person_id:
            continue
        target = by_ov.get(canon)
        found.append({
            "dup_id": u.id, "dup_person_id": u.person_id, "dup_role": u.role,
            "canonical": canon,
            "target_id": target.id if target else None,
            "target_role": target.role if target else None,
            "action": "merge" if target else "rename",
        })
        if target is None:
            by_ov[canon] = u   # after its rename this IS the canonical account
    return found


def unresolved(db: Session) -> list[dict[str, Any]]:
    """UPN accounts the directory cannot place — for the admin to look at."""
    out = []
    for u in db.query(User).order_by(User.id).all():
        if "@" not in u.person_id or u.person_id.startswith(MERGED_PREFIX):
            continue
        canon, _ = resolve(db, u.person_id)
        if canon != u.person_id:
            continue
        local = u.person_id.partition("@")[0].lower()
        stem = local.replace("_external", "").split("_")[-1]
        cands = (db.query(Employee)
                 .filter(text("lower(coalesce(employees.email,'')) LIKE :m OR lower(coalesce(employees.lastname_en,'')) LIKE :s"))
                 .params(m=f"%{local}%", s=f"%{stem}%").limit(5).all())
        out.append({"id": u.id, "person_id": u.person_id, "role": u.role,
                    "candidates": [{"person_id": e.person_id, "email": e.email, "active": e.is_active,
                                    "name": employee_name(e)} for e in cands]})
    return out


def merge_manual(db: Session, mapping: dict[str, str], *, apply: bool = False) -> dict[str, Any]:
    """The admin's own «UPN → person_id» pairs for the people the directory
    spells differently (`petr_ivanov@` vs `Pyotr_Ivanov@`). Same
    machinery as `merge_duplicates`: the numeric account, if it exists, wins
    and the UPN account merges into it; otherwise the UPN account is renamed
    to the number and keeps every right it has."""
    plan = []
    for upn, canon in mapping.items():
        canon = str(canon).strip()
        u = db.query(User).filter(func.lower(User.person_id) == upn.strip().lower()).first()
        if u is None:
            plan.append({"dup_id": None, "dup_person_id": upn, "dup_role": None, "canonical": canon,
                         "target_id": None, "target_role": None, "action": "skip", "skipped": "no such account"})
            continue
        if u.person_id == canon:
            continue
        target = db.query(User).filter(User.person_id == canon).first()
        plan.append({"dup_id": u.id, "dup_person_id": u.person_id, "dup_role": u.role, "canonical": canon,
                     "target_id": target.id if target else None,
                     "target_role": target.role if target else None,
                     "action": "merge" if target else "rename"})
    return _apply(db, plan, apply=apply)


def merge_duplicates(db: Session, *, apply: bool = False) -> dict[str, Any]:
    """Dry run by default. With `apply=True` the changes are committed."""
    return _apply(db, duplicates(db), apply=apply)


def _apply(db: Session, plan: list[dict[str, Any]], *, apply: bool) -> dict[str, Any]:
    fks = _fk_columns_to_users()
    report: list[dict[str, Any]] = []
    for item in plan:
        moved: dict[str, int] = {}
        dropped: dict[str, int] = {}
        if item["action"] == "skip":
            report.append({**item, "moved": moved, "dropped": dropped})
            continue
        dup = db.get(User, item["dup_id"])
        if item["action"] == "rename":
            # No canonical account yet: this one BECOMES it (same person,
            # rights included) — only the id changes, nothing to move.
            if apply:
                dup.upn = dup.person_id
                dup.person_id = item["canonical"]
                if not dup.display_name and item["canonical"].isdigit():
                    emp = db.query(Employee).filter(Employee.person_id == item["canonical"]).first()
                    dup.display_name = employee_name(emp)
                db.flush()
            report.append({**item, "moved": moved, "dropped": dropped})
            continue
        target = db.query(User).filter(User.person_id == item["canonical"]).first() or db.get(User, item["target_id"])
        if target is None or target.id == dup.id:
            report.append({**item, "moved": moved, "dropped": dropped, "skipped": "no target"})
            continue
        for table, col in fks:
            rows = db.execute(text(f'SELECT rowid FROM "{table}" WHERE "{col}" = :d'),
                              {"d": dup.id}).fetchall()
            if not rows:
                continue
            if not apply:
                moved[f"{table}.{col}"] = len(rows)
                continue
            for (rid,) in rows:
                try:
                    with db.begin_nested():
                        db.execute(text(f'UPDATE "{table}" SET "{col}" = :t WHERE rowid = :r'),
                                   {"t": target.id, "r": rid})
                    moved[f"{table}.{col}"] = moved.get(f"{table}.{col}", 0) + 1
                except IntegrityError as exc:
                    # the canonical account already holds an equivalent row
                    # (a permission on the same dashboard, a favourite …).
                    # ONLY that: since foreign_keys=ON (2026-09-06) the same
                    # exception class also carries "FOREIGN KEY constraint
                    # failed", and silently DELETING the row on that would
                    # turn a bug into data loss — let it out instead. (The
                    # move itself cannot break a foreign key: it points the
                    # column at an account that exists, and SQLite re-checks
                    # only the constraint on the column being written.)
                    if "FOREIGN KEY" in str(exc.orig or exc).upper():
                        raise
                    db.execute(text(f'DELETE FROM "{table}" WHERE rowid = :r'), {"r": rid})
                    dropped[f"{table}.{col}"] = dropped.get(f"{table}.{col}", 0) + 1
        if apply:
            if _ROLE_RANK.get(dup.role, 0) > _ROLE_RANK.get(target.role, 0):
                target.role = dup.role
            if not target.display_name and dup.display_name:
                target.display_name = dup.display_name
            if "@" in dup.person_id:
                target.upn = dup.person_id
            try:
                db.execute(text("DELETE FROM sessions WHERE user_id = :d"), {"d": dup.id})
            except Exception:
                pass  # the sessions table lives in the same file, but never block on it
            dup.is_active = False
            dup.person_id = f"{MERGED_PREFIX}{dup.person_id}"
            dup.upn = None
        report.append({**item, "moved": moved, "dropped": dropped})
    if apply:
        db.commit()
    return {"applied": apply, "count": len(report), "items": report}
