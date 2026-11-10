"""Sales-territory scopes (/admin/scopes, ADR-18) — the admin UI that feeds
`user_scopes`, which `app/services/rls.py` reads (never writes) to filter
RLS-enabled dashboards. Owner of this file: app/services/scopes.py does the
parsing/resolution/recompute; this module is the thin HTTP/permission/audit
layer around it, mirroring app/api/employees.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from jinja2 import Environment
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.i18n import _
from app.auth.deps import require_permission
from app.database import get_db
from app.models import AuditLog, SalesScopeSync, User, UserScope
from app.services import scopes as scopes_service

router = APIRouter()


def _log(db: Session, actor: User, action: str, detail: dict, ip: str = "") -> None:
    entry = AuditLog(
        actor_id=actor.id, action=action,
        resource_type="scopes", resource_id="",
        ip_address=ip,
    )
    entry.set_detail(detail)
    db.add(entry)


@router.get("/admin/scopes", response_class=HTMLResponse)
async def scopes_page(
    request: Request,
    user: User = Depends(require_permission("view_admin")),
    db: Session = Depends(get_db),
):
    last_sync = db.query(SalesScopeSync).order_by(SalesScopeSync.id.desc()).first()
    actor = None
    if last_sync and last_sync.actor_id:
        actor = db.get(User, last_sync.actor_id)
    see_all_count = db.query(UserScope).filter(UserScope.see_all.is_(True)).count()
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(
        env.get_template("pages/admin_scopes.html").render,
        user=user, request=request,
        last_sync=last_sync, last_actor=(actor.person_id if actor else None),
        see_all_count=see_all_count,
    ))


@router.post("/api/admin/scopes/import")
async def import_scopes(
    request: Request,
    file: UploadFile = File(...),
    dry_run: str = Form("0"),
    actor: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    raw = await file.read(scopes_service.MAX_IMPORT_BYTES + 1)
    if len(raw) > scopes_service.MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail=_("File is larger than 10 MB"))
    is_dry_run = dry_run.strip().lower() in ("1", "true", "yes", "on")

    def _run() -> dict:
        try:
            parsed = scopes_service.parse_csv(raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not parsed["rows"] and not parsed["stats"]["skipped"]:
            raise HTTPException(status_code=422, detail=_("File has no data rows"))
        # apply_import already commits its own writes (rows + sync row) when
        # dry_run is False; when dry_run is True it writes nothing at all —
        # either way the audit row below is the only thing left to commit.
        counts = scopes_service.apply_import(db, parsed, actor, is_dry_run)
        _log(db, actor, "scopes_import", {
            "total": counts["total"], "persons": counts["persons"],
            "resolved": counts["resolved"], "unresolved": counts["unresolved"],
            "nodes": counts["nodes"], "dry_run": is_dry_run,
        }, request.client.host if request.client else "")
        db.commit()
        return counts

    return await run_in_threadpool(_run)


class ManualGrant(BaseModel):
    person_id: str
    see_all: bool | None = None
    node: str | None = None
    role: str | None = None
    remove: bool | None = None


@router.post("/api/admin/scopes/manual")
async def manual_scope(
    request: Request,
    body: ManualGrant,
    actor: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    payload = body.model_dump(exclude_unset=True, exclude={"person_id"})

    def _run() -> dict:
        try:
            result = scopes_service.apply_manual_grant(db, body.person_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _log(db, actor, "scopes_manual", {"person_id": result["person_id"], **payload},
             request.client.host if request.client else "")
        db.commit()
        return result

    return await run_in_threadpool(_run)


@router.get("/api/admin/scopes/status")
async def scopes_status(
    user: User = Depends(require_permission("view_admin")),
    db: Session = Depends(get_db),
):
    last_sync = db.query(SalesScopeSync).order_by(SalesScopeSync.id.desc()).first()
    see_all_count = db.query(UserScope).filter(UserScope.see_all.is_(True)).count()
    if last_sync is None:
        return {
            "last_sync": None, "persons": 0, "resolved": 0, "unresolved": 0,
            "nodes": 0, "see_all_users": see_all_count,
        }
    return {
        "last_sync": last_sync.created_at.isoformat(),
        "persons": last_sync.persons, "resolved": last_sync.resolved,
        "unresolved": last_sync.unresolved, "nodes": last_sync.nodes,
        "see_all_users": see_all_count,
    }


@router.get("/api/admin/scopes/lookup")
async def scopes_lookup(
    q: str = "",
    user: User = Depends(require_permission("view_admin")),
    db: Session = Depends(get_db),
):
    q = q.strip()
    if not q:
        return {"found": False, "see_all": False, "roles": [], "key_count": 0, "keys_sample": []}
    return await run_in_threadpool(scopes_service.lookup, db, q)
