from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.i18n import _
from app.auth.deps import get_current_user, get_dashboard_or_404
from app.database import get_db
from app.models import Dashboard, DashboardPermission, User, ROLE_ADMINISTRATOR

router = APIRouter()


class PermGrantBody(BaseModel):
    can_update_data: bool = False


def _can_manage_permissions(user: User, dashboard: Dashboard, db: Session | None = None) -> bool:
    """Owner, administrator — and, since 2026-09-04 (owner decision), any
    colleague holding `can_update_data` on the report: the Access tab is part
    of the dock they now get in full."""
    if user.role == ROLE_ADMINISTRATOR or dashboard.owner_id == user.id:
        return True
    if db is None:
        return False
    perm = db.query(DashboardPermission).filter(
        DashboardPermission.dashboard_id == dashboard.id,
        DashboardPermission.user_id == user.id,
    ).first()
    return bool(perm and perm.can_update_data)


@router.get("/api/dashboards/{dashboard_id}/permissions")
async def list_permissions(
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _can_manage_permissions(user, dashboard, db):
        raise HTTPException(status_code=403)
    perms = db.query(DashboardPermission).filter(
        DashboardPermission.dashboard_id == dashboard.id
    ).all()
    return [
        {
            "user_id": p.user_id,
            "person_id": p.user.person_id if p.user else None,
            "display_name": p.user.display_name if p.user else None,
            "can_update_data": p.can_update_data,
            "granted_at": p.granted_at.isoformat(),
        }
        for p in perms
    ]




def _target_or_autocreate(db: Session, actor: User, person_id: str) -> User:
    """The grant target. A colleague who has never signed in is created on
    the spot when the employees directory vouches for them (visitor, named
    from the directory) — a 404 here read as "access cannot be granted". Their
    first SSO login then just works."""
    from app.services import identity
    target = identity.find_or_create(db, person_id, create=False)
    if target:
        return target
    ov = identity.resolve(db, person_id)[0]
    from app.models import Employee
    emp = db.query(Employee).filter(
        Employee.person_id == ov,
        Employee.is_active == True,  # noqa: E712
    ).first()
    if emp is None:
        raise HTTPException(
            status_code=404,
            detail=_("«{p1}» not found in users or employee reference table", p1=ov))
    return identity.find_or_create(db, person_id, role="visitor",
                                   display_name=identity.employee_name(emp))

@router.post("/api/dashboards/{dashboard_id}/permissions")
async def grant_permission(
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    person_id: str = Form(...),
    can_update_data: bool = Form(False),
):
    if not _can_manage_permissions(user, dashboard, db):
        raise HTTPException(status_code=403)

    target = _target_or_autocreate(db, user, person_id)

    existing = db.query(DashboardPermission).filter(
        DashboardPermission.dashboard_id == dashboard.id,
        DashboardPermission.user_id == target.id,
    ).first()

    if existing:
        existing.can_update_data = can_update_data
        existing.granted_by = user.id
    else:
        db.add(DashboardPermission(
            dashboard_id=dashboard.id,
            user_id=target.id,
            granted_by=user.id,
            can_update_data=can_update_data,
        ))
    db.commit()
    return {"ok": True}


@router.post("/api/dashboards/{dashboard_id}/permissions/{person_id}")
async def grant_permission_by_person_id(
    dashboard_id: str,
    person_id: str,
    body: PermGrantBody,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dashboard = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dashboard:
        raise HTTPException(status_code=404)
    if not _can_manage_permissions(user, dashboard, db):
        raise HTTPException(status_code=403)

    target = _target_or_autocreate(db, user, person_id)

    existing = db.query(DashboardPermission).filter(
        DashboardPermission.dashboard_id == dashboard_id,
        DashboardPermission.user_id == target.id,
    ).first()

    if existing:
        existing.can_update_data = body.can_update_data
        existing.granted_by = user.id
    else:
        db.add(DashboardPermission(
            dashboard_id=dashboard_id,
            user_id=target.id,
            granted_by=user.id,
            can_update_data=body.can_update_data,
        ))
    db.commit()
    return {"ok": True}


@router.delete("/api/dashboards/{dashboard_id}/permissions/{target_user_id}")
async def revoke_permission(
    target_user_id: int,
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _can_manage_permissions(user, dashboard, db):
        raise HTTPException(status_code=403)

    perm = db.query(DashboardPermission).filter(
        DashboardPermission.dashboard_id == dashboard.id,
        DashboardPermission.user_id == target_user_id,
    ).first()
    if not perm:
        raise HTTPException(status_code=404)

    db.delete(perm)
    db.commit()
    return {"ok": True}
