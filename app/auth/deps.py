from __future__ import annotations

from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app import i18n
from app.auth.session import session_store
from app.database import get_db
from app.models import Dashboard, DashboardPermission, User, has_permission, ROLE_ADMINISTRATOR
from app.services import perflog

# A copy of `devicetoken.PREFIX`: a cheap filter before importing the key module.
_DT_PREFIX = "opdt_"


# The desktop program (report mirror) arrives without a cookie, with the key in
# `Authorization: Bearer opdt_…`. This path is STRICTLY READ-ONLY, and that holds
# not by convention but by the method check here: a new mutating route cannot
# accidentally become reachable with a key, because there is one check and it knows
# about the method, not about a list of paths. OriginGuard is no help here — it lets a
# request without `Origin` through, rightly treating it as non-browser.
#
# The browser path pays NOTHING for this: the function is called only when the
# cookie is absent, i.e. for a request from a browser it does not run at all.
# A short DB session is acceptable here precisely because the path is rare — the program
# syncs once an hour; it must not be dragged into the cookie path, which is hot.
def _device_token_user(request: Request) -> Optional[User]:
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    raw = auth[7:].strip()
    if not raw.startswith(_DT_PREFIX):
        return None
    if request.method not in ("GET", "HEAD"):
        return None

    from app.auth import devicetoken
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        row = devicetoken.resolve(db, raw)
        if row is None:
            return None
        u = db.get(User, row.user_id)
        if u is None or not u.is_active:
            return None
        user = User(id=u.id, person_id=u.person_id, display_name=u.display_name,
                    role=u.role, is_active=True, lang=u.lang)
        if devicetoken.touch(db, row):
            db.commit()
        if u.lang:
            i18n.set_lang(u.lang)
        rec = perflog.request_record.get()
        if rec is not None:
            rec["uid"] = user.id
            rec.setdefault("stages", {})["device_token"] = 1
        return user
    finally:
        db.close()


# Hot-path auth. Async deps on purpose: FastAPI would run sync deps in the
# threadpool (two hops per request); the work here is a single sub-ms SQLite
# read (session JOIN users) on a dedicated connection, safe in the event loop
# (the touch/expiry writes are queued to a background thread — see session.py).
async def get_current_user_optional(
    request: Request,
    op_session: Optional[str] = Cookie(default=None),
) -> Optional[User]:
    if not op_session:
        return _device_token_user(request)
    hit = session_store.get_with_user(op_session)
    if hit is None:
        return None
    _sess, row = hit
    # Detached User (no ORM session): routes only read id/person_id/role/
    # display_name; timestamps intentionally left unset (raw rows carry str).
    user = User(id=row.id, person_id=row.person_id, display_name=row.display_name,
                role=row.role, is_active=bool(row.is_active), lang=row.lang)
    # The profile wins over the op_lang cookie LangMiddleware already applied.
    # No reset token: the language is meant to hold for the rest of the request,
    # and the middleware resets the ContextVar when the response is done.
    if row.lang:
        i18n.set_lang(row.lang)
    rec = perflog.request_record.get()
    if rec is not None:
        rec["uid"] = user.id
    return user


async def get_current_user(
    user: Optional[User] = Depends(get_current_user_optional),
) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def require_role(*roles: str):
    async def dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user
    return dep


def require_permission(permission: str):
    async def dep(user: User = Depends(get_current_user)) -> User:
        if not has_permission(user.role, permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user
    return dep


# Async like the auth chain above: one indexed SQLite read, in-loop.
async def get_dashboard_or_404(
    dashboard_id: str,
    db: Session = Depends(get_db),
) -> Dashboard:
    dash = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dash:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return dash


def _may_view(dashboard: Dashboard, user: User, db: Session) -> bool:
    """Allow if: admin, owner, public, or has explicit permission on published dashboard."""
    if user.role == ROLE_ADMINISTRATOR:
        return True
    if dashboard.owner_id == user.id:
        return True
    if dashboard.is_public:
        return True
    perm = db.query(DashboardPermission).filter(
        DashboardPermission.dashboard_id == dashboard.id,
        DashboardPermission.user_id == user.id,
    ).first()
    return bool(perm and dashboard.is_published)


async def require_dashboard_view(
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dashboard:
    """The right to VIEW, without the second lock.

    The only legitimate consumer is the `/d/{id}` page: it has to
    render the password form, and for that it must pass the access check and NOT
    get a 403 for a closed lock. Everything else goes through
    `require_dashboard_access` below, which does not serve a locked report.
    """
    if _may_view(dashboard, user, db):
        return dashboard
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")


async def require_dashboard_access(
    request: Request = None,
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dashboard:
    """The right to view AND an opened "strictly confidential" lock.

    The lock check sits INSIDE the shared dependency on purpose: that way a new
    serving handler is closed by default and its author need not remember the mode —
    the same logic by which a device key is checked by METHOD rather than by a
    list of paths. An ordinary report pays not a single query for this:
    `is_unlocked` returns on its first line when `conf_hash` is empty.
    """
    if not _may_view(dashboard, user, db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    from app.services import confidential
    if confidential.enabled(dashboard):
        # `request` may be None: the dependency is also called as a plain function
        # (`presentations._may_show`, `add_item` — there the report id does not come
        # from the path). Without a request there is no session, hence no unlock — refuse.
        # This is a deliberate fail closed: a confidential report does not get into
        # a presentation and is not shown in one, because a deck is a distribution
        # surface, not password-protected reading.
        from app.auth.session import get_session_id_from_cookie
        sid = (get_session_id_from_cookie(request.headers.get("cookie", ""))
               if request is not None else None)
        confidential.assert_unlocked(db, sid, dashboard)
    return dashboard


def can_update_dashboard_data(
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> bool:
    if user.role == ROLE_ADMINISTRATOR:
        return True
    if dashboard.owner_id == user.id:
        return True
    perm = db.query(DashboardPermission).filter(
        DashboardPermission.dashboard_id == dashboard.id,
        DashboardPermission.user_id == user.id,
    ).first()
    return bool(perm and perm.can_update_data)
