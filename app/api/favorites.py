""""My reports" favourites — a per-user pin + own drag-and-drop order on the
workspace home. Mirrors the folders/dashboards API idiom: same auth deps,
same module-local `_log`, idempotent PUT/DELETE (a repeat toggle is a no-op,
not a 409)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_dashboard_access
from app.database import get_db
from app.models import AuditLog, Dashboard, DashboardFavorite, User

router = APIRouter()

_MAX_ORDER_IDS = 500


def _log(db: Session, actor: User, action: str, resource_type: str, resource_id: str, detail: dict, ip: str = "") -> None:
    entry = AuditLog(
        actor_id=actor.id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        ip_address=ip,
    )
    entry.set_detail(detail)
    db.add(entry)


@router.put("/api/dashboards/{dashboard_id}/favorite")
async def add_favorite(
    dashboard: Dashboard = Depends(require_dashboard_access),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = db.query(DashboardFavorite).filter(
        DashboardFavorite.user_id == user.id,
        DashboardFavorite.dashboard_id == dashboard.id,
    ).first()
    if existing is None:
        max_pos = db.query(func.max(DashboardFavorite.position)).filter(
            DashboardFavorite.user_id == user.id
        ).scalar() or 0
        db.add(DashboardFavorite(user_id=user.id, dashboard_id=dashboard.id, position=max_pos + 1))
        _log(db, user, "favorite.add", "dashboard", dashboard.id, {})
        db.commit()
    return {"ok": True}


@router.delete("/api/dashboards/{dashboard_id}/favorite")
async def remove_favorite(
    dashboard: Dashboard = Depends(require_dashboard_access),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = db.query(DashboardFavorite).filter(
        DashboardFavorite.user_id == user.id,
        DashboardFavorite.dashboard_id == dashboard.id,
    ).first()
    if existing is not None:
        db.delete(existing)
        _log(db, user, "favorite.remove", "dashboard", dashboard.id, {})
        db.commit()
    return {"ok": True}


class FavoriteOrder(BaseModel):
    ids: list[str] = Field(default_factory=list)


@router.put("/api/favorites/order")
async def reorder_favorites(
    body: FavoriteOrder,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if len(body.ids) > _MAX_ORDER_IDS:
        raise HTTPException(status_code=400, detail=f"Too many ids (max {_MAX_ORDER_IDS})")

    # Only the caller's OWN rows, and only the ones they still hold — an id
    # for someone else's favourite (or a stale/unknown id) is silently
    # ignored rather than erroring, so a client-side drag never 400s on a
    # slightly-stale snapshot.
    rows = {
        f.dashboard_id: f
        for f in db.query(DashboardFavorite).filter(DashboardFavorite.user_id == user.id).all()
    }
    pos = 0
    for dashboard_id in body.ids:
        row = rows.get(dashboard_id)
        if row is None:
            continue
        pos += 1
        row.position = pos
    _log(db, user, "favorite.order", "dashboard", str(user.id), {"count": pos})
    db.commit()
    return {"ok": True}
