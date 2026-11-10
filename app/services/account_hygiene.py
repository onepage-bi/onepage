"""Accounts of inactive employees — /admin (users) + the employees import
response share this single computation so the two pages never disagree on
who counts as stale.

Scope, by design (owner, 2026-09-06):
- Only ACTIVE OnePage users are candidates — a deactivated account is
  already handled.
- Joined to the HR directory (`Employee`, mirrored from Greenplum) by
  `person_id`. A user whose person_id has NO Employee row at all is not a
  hygiene finding by itself (could be a service/admin account never in HR,
  or HR simply hasn't been imported yet) — it is surfaced only as a count,
  never as an individual row, so nobody is nudged to deactivate an account
  the directory just doesn't know about.
- A user IS a finding when the directory says that person's employment has
  ended (`Employee.is_active is False`) while their OnePage account is
  still active.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Employee, User


def stale_users(db: Session) -> dict:
    """{"items": [...], "no_employee_record": N} — see module docstring."""
    active_users = db.query(User).filter(User.is_active == True).all()  # noqa: E712
    if not active_users:
        return {"items": [], "no_employee_record": 0}

    person_ids = {u.person_id for u in active_users if u.person_id}
    by_person_id: dict[str, list[Employee]] = {}
    if person_ids:
        for e in (db.query(Employee)
                  .filter(Employee.person_id.in_(person_ids)).all()):
            # A Greenplum export can carry more than one row for the same
            # person_id across syncs (rare, but the column isn't unique) —
            # e.g. a closed position plus a current one, both landing with
            # the same synced_at stamp from one import. Keep every row: a
            # person is stale only if NONE of their rows is active, never
            # decided by picking a single "latest" row (synced_at ties do
            # not order reliably, and even when it did, one closed position
            # must not shadow another still-open one).
            by_person_id.setdefault(e.person_id, []).append(e)

    items: list[dict] = []
    no_record = 0
    for u in active_users:
        rows = by_person_id.get(u.person_id) if u.person_id else None
        if not rows:
            no_record += 1
            continue
        if any(e.is_active for e in rows):
            continue
        # All rows for this person_id are inactive — pick the most
        # recently synced one just to source display details.
        emp = max(rows, key=lambda e: e.synced_at or 0)
        items.append({
            "id": u.id,
            "person_id": u.person_id,
            "display_name": u.display_name,
            "role": u.role,
            "last_seen": u.last_login.isoformat() if u.last_login else None,
            "dept": emp.deptname_ru or emp.deptname_en,
            "inactivationdate": emp.inactivationdate,
        })

    # Longest-idle (or never logged in) first — that is the account most
    # worth an admin's attention.
    items.sort(key=lambda r: r["last_seen"] or "")
    return {"items": items, "no_employee_record": no_record}
