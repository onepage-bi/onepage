"""Navigators — hub pages with links to reports and other systems.

What it is. A person writes ONE html (like a report) and puts it here: inside are links to
OnePage reports, to Power BI, to network folders — everything their people need
to get where they are going. The page's content is the author's business; the platform
stores it, serves it according to rights and makes the links WORK.

Why this is a special kind of report and not an entity of its own. A navigator needs exactly the
same access, versions, folders, favourites, share links and audit as a report — and
a second access implementation would create a second place where it could diverge
from the first. So a navigator is a `Dashboard` with `dash_type = DASH_PAGE`, and all
viewing goes through the ordinary `/d/{id}`; exactly two things are separate here: its own list and
its own upload, which sets the type forcibly (it cannot be detected from the markup —
a navigator has not a single feature that distinguishes it from a static report).

The main thing the section exists for lives NOT here but in serving:
`detector.inject_link_bridge` + handling of `op_open` in `static/js/viewer.js`.
A link from the page is opened by the shell outside the sandbox, because
a tab opened from inside inherits the sandbox and gets no cookie — and
Power BI Report Server shows a white screen (owner's observation, 29.10).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, joinedload, load_only

from app.auth.deps import get_current_user, require_permission
from app.config import settings
from app.database import get_db
from app.models import (
    DASH_PAGE, ROLE_ADMINISTRATOR, Dashboard, DashboardPermission,
    DashboardVersion, User,
)
from app.services import detector, storage


def _require_navigators_enabled() -> None:
    """Section flag — the same rollback contract as Quests and Catalog:
    off means 404 for the whole router, not an empty page."""
    if not settings.navigators_enabled:
        raise HTTPException(status_code=404)


router = APIRouter(dependencies=[Depends(_require_navigators_enabled)])


def _visible(db: Session, user: User):
    """Navigators this person is allowed to open.

    The WHERE is verbatim the same as the report list's (`dashboards.index`): owner
    OR public OR granted and published. The copy here is deliberate and
    narrow — moving it into a shared helper would mean touching the home page's hot path
    for a list that is opened once a day.
    """
    q = (db.query(Dashboard)
         .options(joinedload(Dashboard.owner).load_only(User.person_id))
         .filter(Dashboard.dash_type == DASH_PAGE))
    if user.role != ROLE_ADMINISTRATOR:
        perm_sq = (select(DashboardPermission.dashboard_id)
                   .where(DashboardPermission.user_id == user.id)
                   .scalar_subquery())
        q = q.filter(or_(
            Dashboard.owner_id == user.id,
            Dashboard.is_public == True,  # noqa: E712
            and_(Dashboard.id.in_(perm_sq), Dashboard.is_published == True),  # noqa: E712
        ))
    return q.order_by(Dashboard.updated_at.desc()).all()


@router.get("/navigators", response_class=HTMLResponse)
async def navigators_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = await run_in_threadpool(_visible, db, user)
    can_add = user.role in (ROLE_ADMINISTRATOR, "superuser")
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(
        env.get_template("pages/navigators.html").render,
        user=user, request=request, items=items, can_add=can_add))


@router.post("/api/navigators")
async def create_navigator(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    html_file: UploadFile = File(...),
    user: User = Depends(require_permission("upload_dashboard")),
    db: Session = Depends(get_db),
):
    """Navigator upload. The type is set FORCIBLY.

    `detector.detect_type` is deliberately not called here: a navigator does not
    differ from a static report in its markup, and guessing would one day turn
    a page into a report (or vice versa) without a single message to the person.
    """
    from app.api.dashboards import _log, _make_slug, _validate_html_upload

    html_bytes = await html_file.read()
    _validate_html_upload(html_bytes)

    dash_id = str(uuid.uuid4())
    version = 1
    storage.init_dashboard(dash_id)
    html_path = storage.save_html(dash_id, version, html_bytes)

    dash = Dashboard(
        id=dash_id, slug=_make_slug(title, db), title=title,
        description=description or None,
        dash_type=DASH_PAGE, owner_id=user.id,
        slot_count=0, current_version=version,
        # A navigator is published immediately: it has no run that will one day
        # make it ready, and a draft without data means nothing here.
        is_published=True,
    )
    db.add(dash)
    db.add(DashboardVersion(dashboard_id=dash_id, version_number=version,
                            html_path=html_path, created_by=user.id))
    dash.render_size = storage.estimate_render_size(dash_id, version)
    _log(db, user, "upload", "dashboard", dash_id,
         {"title": title, "type": DASH_PAGE})
    db.commit()
    return RedirectResponse(f"/d/{dash_id}", status_code=303)
