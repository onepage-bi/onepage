from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from jinja2 import Environment
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, aliased, joinedload

from app.i18n import _
from app.auth.deps import get_current_user, require_permission
from app.database import SessionLocal, get_db
from app.models import (
    AuditLog, Dashboard, GlossaryTerm, User, ROLES, ROLE_LABELS, ROLE_ADMINISTRATOR,
    Generator, GeneratorVersion, GenerationRun, RUN_ERROR, RUN_LOST,
)
from app.tools.audit_export import InvalidRange, iter_export_rows, resolve_range, row_to_jsonl


class UserCreate(BaseModel):
    person_id: str
    role: str
    display_name: str = ""


class UserUpdate(BaseModel):
    role: Optional[str] = None
    display_name: Optional[str] = None
    is_active: Optional[bool] = None



router = APIRouter()


@router.get("/admin/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    user: User = Depends(require_permission("view_admin")),
    db: Session = Depends(get_db),
):
    entries = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(500).all()
    actor_ids = {e.actor_id for e in entries if e.actor_id}
    actors = {u.id: u for u in db.query(User).filter(User.id.in_(actor_ids)).all()} if actor_ids else {}
    events = []
    for e in entries:
        actor_user = actors.get(e.actor_id)
        target = f"{e.resource_type}/{e.resource_id}" if e.resource_id else e.resource_type
        events.append({
            "actor": actor_user.person_id if actor_user else "—",
            "action": e.action,
            "target_label": target,
            "ip": e.ip_address,
            "payload": e.get_detail(),
            "created_at": e.created_at,
        })
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(env.get_template("pages/audit.html").render,
        user=user, events=events, request=request,
    ))


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    user: User = Depends(require_permission("view_admin")),
    db: Session = Depends(get_db),
):
    users = db.query(User).order_by(User.created_at.desc()).all()
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(env.get_template("pages/admin.html").render,
        user=user, users=users, roles=ROLES, role_labels=ROLE_LABELS, request=request,
    ))


@router.get("/admin/generators", response_class=HTMLResponse)
async def generators_page(
    request: Request,
    user: User = Depends(require_permission("view_admin")),
    db: Session = Depends(get_db),
):
    """Admin overview (RFC_GENERATORS_RESEARCH.md E.3): versions + failed runs
    by dashboard/owner, silhouette of /admin/errors, source = generation_runs.
    Registration itself happens via the JS on this page hitting the
    /api/admin/generators* endpoints (app/api/generators.py) — this route only
    renders."""
    generator_rows = (
        db.query(Generator)
        .options(joinedload(Generator.versions))
        .order_by(Generator.name)
        .all()
    )

    # One GROUP BY query for run counts per generator, joined through the
    # owning dashboard — no N+1 across the generator list.
    summary_rows = (
        db.query(Dashboard.generator_id, GenerationRun.status, func.count(GenerationRun.id))
        .join(GenerationRun, GenerationRun.dashboard_id == Dashboard.id)
        .filter(Dashboard.generator_id.isnot(None))
        .group_by(Dashboard.generator_id, GenerationRun.status)
        .all()
    )
    runs_summary: dict[int, dict[str, int]] = {}
    for gen_id, status, count in summary_rows:
        runs_summary.setdefault(gen_id, {})[status] = count

    generators = []
    for g in generator_rows:
        versions = sorted(g.versions, key=lambda v: v.version_number, reverse=True)
        current = next((v for v in versions if v.is_current), None)
        generators.append({
            "id": g.id,
            "name": g.name,
            "is_archived": g.is_archived,
            "created_at": g.created_at,
            "git_repo": g.git_repo,
            "git_branch": g.git_branch,
            "git_token_set": bool(g.git_token),
            "version_count": len(versions),
            "current": current,
            "runs": runs_summary.get(g.id, {}),
        })

    # Failed runs (last 50): one query, Dashboard/owner/starter/version joined
    # in — no N+1 per row.
    Owner = aliased(User)
    Starter = aliased(User)
    fail_rows = (
        db.query(
            GenerationRun.started_at,
            GenerationRun.engine,
            GenerationRun.status,
            GenerationRun.error_message,
            Dashboard.id.label("dashboard_id"),
            Dashboard.title.label("dashboard_title"),
            Owner.person_id.label("owner_person_id"),
            Starter.person_id.label("started_by_person_id"),
            GeneratorVersion.version_number,
        )
        .join(Dashboard, GenerationRun.dashboard_id == Dashboard.id)
        .outerjoin(Owner, Dashboard.owner_id == Owner.id)
        .outerjoin(Starter, GenerationRun.started_by == Starter.id)
        .outerjoin(GeneratorVersion, GenerationRun.generator_version_id == GeneratorVersion.id)
        .filter(GenerationRun.status.in_([RUN_ERROR, RUN_LOST]))
        .order_by(GenerationRun.started_at.desc())
        .limit(50)
        .all()
    )
    fails = [{
        "started_at": r.started_at,
        "dashboard_id": r.dashboard_id,
        "dashboard_title": r.dashboard_title,
        "owner": r.owner_person_id or "—",
        "started_by": r.started_by_person_id or "—",
        "version_number": r.version_number,
        "engine": r.engine,
        "status": r.status,
        "error_message": r.error_message or "",
    } for r in fail_rows]

    # Platform glossary (H2): admin sees every term regardless of reach —
    # this page is the company-wide registry, not a per-user view.
    glossary_rows = db.query(GlossaryTerm).order_by(GlossaryTerm.term).all()
    gen_name_by_id = {g["id"]: g["name"] for g in generators}
    glossary_terms = [{
        "id": t.id,
        "term": t.term,
        "definition": t.definition,
        "generator_id": t.generator_id,
        "generator_name": gen_name_by_id.get(t.generator_id) if t.generator_id else None,
    } for t in glossary_rows]

    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(env.get_template("pages/admin_generators.html").render,
        user=user, request=request, generators=generators, fails=fails,
        glossary_terms=glossary_terms,
    ))


@router.post("/api/admin/users")
async def create_user(
    body: UserCreate,
    user: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    if body.role not in ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")
    person_id = body.person_id.strip()
    if not person_id:
        raise HTTPException(status_code=400, detail="person_id required")

    from app.services import identity
    if identity.find_or_create(db, person_id, create=False) is not None:
        raise HTTPException(status_code=409, detail="User already exists")

    canon, upn = identity.resolve(db, person_id)
    new_user = User(
        person_id=canon,
        upn=upn,
        role=body.role,
        display_name=body.display_name.strip() or None,
    )
    db.add(new_user)

    log = AuditLog(actor_id=user.id, action="create_user", resource_type="user")
    log.set_detail({"person_id": person_id, "role": body.role})
    db.add(log)
    db.commit()
    return {"ok": True, "id": new_user.id}


@router.patch("/api/admin/users/{user_id}")
async def update_user(
    user_id: int,
    body: UserUpdate,
    actor: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404)

    if body.role and body.role != ROLE_ADMINISTRATOR and target.role == ROLE_ADMINISTRATOR:
        admin_count = db.query(User).filter(
            User.role == ROLE_ADMINISTRATOR, User.is_active == True
        ).count()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote the last administrator")

    if body.role is not None:
        if body.role not in ROLES:
            raise HTTPException(status_code=400, detail="Invalid role")
        target.role = body.role
    if body.display_name is not None:
        target.display_name = body.display_name.strip() or None
    if body.is_active is not None:
        target.is_active = body.is_active

    log = AuditLog(actor_id=actor.id, action="update_user", resource_type="user", resource_id=str(user_id))
    log.set_detail({"role": body.role, "is_active": body.is_active})
    db.add(log)
    db.commit()

    if body.is_active is False:
        # Revoke live sessions AFTER the commit (defense-in-depth: per-request
        # is_active checks already 401 them, this kills the sessions too). A
        # failed commit must not leave the user active yet force-logged-out.
        from app.auth.session import session_store
        session_store.delete_for_user(user_id)
    return {"ok": True}


@router.delete("/api/admin/users/{user_id}")
async def delete_user(
    user_id: int,
    actor: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404)
    if target.id == actor.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    # Everything that points at the account goes first: its own rows (grants,
    # favourites, quest progress, LLM access — a NOT NULL user_id means the row
    # exists only because the person does) are deleted, while the references
    # that merely NAME them (a dashboard's owner, an audit line's actor, a
    # run's starter) are set to NULL and the row survives. Without this the
    # DELETE below is refused outright under foreign_keys=ON — and even before
    # it was on, SQLAlchemy tried to NULL `dashboard_permissions.user_id` and
    # hit the NOT NULL. See app/services/integrity.py for the policy.
    from app.services import integrity
    cascaded = integrity.detach(db, "users", "id", target.id)

    log = AuditLog(actor_id=actor.id, action="delete_user", resource_type="user", resource_id=str(user_id))
    log.set_detail({"person_id": target.person_id, "cascaded": cascaded})
    db.add(log)
    db.expire(target)   # its relationship collections were emptied by raw SQL
    db.delete(target)
    db.commit()
    from app.auth.session import session_store
    session_store.delete_for_user(user_id)
    return {"ok": True}


@router.get("/api/admin/users/stale")
async def stale_users(
    actor: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    """The "Accounts of inactive employees" card — see
    app/services/account_hygiene.py for the join rules. Read-only; the
    actual deactivation goes through the existing PATCH endpoint below so it
    gets the same audit trail and session revocation as any other."""
    from app.services import account_hygiene
    return account_hygiene.stale_users(db)


@router.get("/api/admin/audit-log")
async def audit_log(
    actor: User = Depends(require_permission("view_audit_log")),
    db: Session = Depends(get_db),
    limit: int = 200,
):
    entries = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).all()
    return [
        {
            "id": e.id,
            "actor_id": e.actor_id,
            "action": e.action,
            "resource_type": e.resource_type,
            "resource_id": e.resource_id,
            "detail": e.get_detail(),
            "ip_address": e.ip_address,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]


@router.get("/api/admin/audit-log/export")
def audit_log_export(
    actor: User = Depends(require_permission("view_audit_log")),
    db: Session = Depends(get_db),
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    """JSONL export of audit_log for a calendar range [from, to] (both dates
    inclusive, YYYY-MM-DD), at most 366 days. The same row format as
    the CLI `python -m app.tools.audit_export` (app/tools/audit_export.py) —
    both paths share resolve_range()/iter_export_rows(), so the manual export
    and the button on /admin/audit cannot diverge.

    The export itself is also written to audit_log (action="audit_export") — BEFORE
    streaming starts, in one commit with the range in detail, so the record of
    the export itself is not lost even if the client aborts the download
    halfway.

    Synchronous `def`: FastAPI runs the route body (validation + audit write) in
    the threadpool, not on the event loop; the StreamingResponse streaming itself
    also goes to the threadpool row by row via Starlette's iterate_in_threadpool (see
    app/services/encoding.py) — the audit_log ORM cursor never blocks
    the event loop.

    Streaming does NOT reuse the request-scoped `db` from `Depends(get_db)`:
    since FastAPI 0.106 the exit code of yield dependencies runs BEFORE
    the response body is sent, so by the time the generator below actually
    starts SELECTing, `db` is already closed — and, being a closed-but-reusable
    Session, it would silently take a new connection from the pool and never return it
    (a connection leak on every export). Hence the generator has its own
    short-lived `SessionLocal()`, closed in `finally`; the `db` from
    Depends is used only for writing the `audit_export` row above."""
    try:
        start, end = resolve_range(date_from, date_to)
    except InvalidRange as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    log = AuditLog(actor_id=actor.id, action="audit_export", resource_type="audit_log")
    log.set_detail({"from": date_from, "to": date_to})
    db.add(log)
    db.commit()

    def gen():
        stream_db = SessionLocal()
        try:
            for row in iter_export_rows(stream_db, start, end):
                yield row_to_jsonl(row)
        finally:
            stream_db.close()

    stamp = f"{date_from}_{date_to}"
    return StreamingResponse(
        gen(), media_type="application/x-ndjson",
        # identity: keep GZipMiddleware from compressing row by row on the event loop —
        # the export is admin-only and intranet, extra CPU in the loop costs more than bytes.
        headers={"Content-Disposition": f'attachment; filename="audit-{stamp}.jsonl"',
                 "Content-Encoding": "identity"},
    )


# ── LLM runtime config (the "enable DeepSeek" switch) ────────────────
# Overlay over the OP_LLM_* env defaults, persisted by
# app/services/runtime_config.py. Keys are write-only: accepted here,
# stored 0600, echoed back only as *_set booleans, audited by field name.

class LlmConfigBody(BaseModel):
    llm_provider: Optional[str] = None
    llm_browser: Optional[bool] = None
    llm_model: Optional[str] = None
    llm_endpoint: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_browser_key: Optional[str] = None
    # Field names to reset back to the env default.
    reset: Optional[list[str]] = None


@router.get("/api/admin/llm")
async def get_llm_config(
    actor: User = Depends(require_permission("manage_users")),
):
    from app.config import settings
    from app.services import nl2sql, runtime_config
    runtime_config.ensure_fresh(force=True)
    return {
        "effective": {
            "provider": nl2sql.provider(),
            "browser": bool(settings.llm_browser),
            "model": nl2sql.model_for(),
            "endpoint": nl2sql.endpoint_for(),
            "api_key_set": bool((settings.llm_api_key or "").strip()),
            "browser_key_set": bool((settings.llm_browser_key or "").strip()),
        },
        "overrides": runtime_config.current_overrides(),
    }


@router.put("/api/admin/llm")
async def put_llm_config(
    body: LlmConfigBody,
    request: Request,
    actor: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    from app.services import runtime_config
    if body.llm_provider is not None and body.llm_provider not in ("ollama", "deepseek"):
        raise HTTPException(status_code=400, detail=_("provider: ollama or deepseek"))
    changes: dict = {}
    for name in runtime_config.OVERRIDE_FIELDS:
        value = getattr(body, name, None)
        if value is not None:
            changes[name] = value
    for name in (body.reset or []):
        if name in runtime_config.OVERRIDE_FIELDS:
            changes[name] = None
    result = await run_in_threadpool(runtime_config.set_overrides, changes)
    log = AuditLog(actor_id=actor.id, action="llm_config_update", resource_type="llm")
    log.set_detail({"changed": result["changed"]})  # names only, never values
    db.add(log)
    db.commit()
    return result


# ── DeepSeek access allowlist ──────────────────────────────────────────
# Who may USE the paid provider (llm_access rows) and who may GRANT it
# (administrator, narrowed by OP_LLM_ACCESS_ADMINS — env-only on purpose).

@router.get("/api/admin/llm/access")
async def list_llm_access(
    actor: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    from app.models import LlmAccess
    from app.services import llm_access
    rows = (
        db.query(LlmAccess, User)
        .join(User, User.id == LlmAccess.user_id)
        .order_by(User.person_id)
        .all()
    )
    granters = {u.id: u.person_id for u in db.query(User).filter(
        User.id.in_({r.granted_by for r, _ in rows if r.granted_by})).all()} if rows else {}
    return {
        "can_assign": llm_access.can_assign(actor),
        "items": [{
            "person_id": u.person_id,
            "display_name": u.display_name,
            "granted_by": granters.get(r.granted_by),
            "granted_at": r.created_at.isoformat() if r.created_at else None,
        } for r, u in rows],
    }


def _require_assigner(actor: User) -> None:
    from app.services import llm_access
    if not llm_access.can_assign(actor):
        raise HTTPException(
            status_code=403,
            detail=_("DeepSeek access is granted by designated admin only"),
        )


@router.put("/api/admin/llm/access/{person_id}")
async def grant_llm_access(
    person_id: str,
    request: Request,
    actor: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    from app.models import LlmAccess
    _require_assigner(actor)
    target = db.query(User).filter(User.person_id == person_id.strip()).first()
    if not target:
        raise HTTPException(status_code=404, detail=_("No such OnePage user – add them first"))
    if not db.query(LlmAccess.id).filter(LlmAccess.user_id == target.id).first():
        db.add(LlmAccess(user_id=target.id, granted_by=actor.id))
        log = AuditLog(actor_id=actor.id, action="llm_access_grant", resource_type="llm",
                       ip_address=request.client.host if request.client else "")
        log.set_detail({"person_id": target.person_id})
        db.add(log)
        db.commit()
    return {"ok": True}


@router.delete("/api/admin/llm/access/{person_id}")
async def revoke_llm_access(
    person_id: str,
    request: Request,
    actor: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    from app.models import LlmAccess
    _require_assigner(actor)
    target = db.query(User).filter(User.person_id == person_id.strip()).first()
    if target:
        deleted = db.query(LlmAccess).filter(LlmAccess.user_id == target.id).delete()
        if deleted:
            log = AuditLog(actor_id=actor.id, action="llm_access_revoke", resource_type="llm",
                           ip_address=request.client.host if request.client else "")
            log.set_detail({"person_id": target.person_id})
            db.add(log)
        db.commit()
    return {"ok": True}
