"""Platform glossary (H2): terms operators use ("SL", "hub", "chain") that may
belong to one report generator or to the company as a whole.

Two write reaches meet here:
  * `GET /api/glossary` — every user reads: company-wide terms (`generator_id
    IS NULL`) unconditionally, plus terms scoped to any generator this user
    may RUN (`_generator_dashboards` reach, the same boundary Data Quality
    and Data Sources already use). Feeds two callers: the future glossary
    panel and `app.api.explore.api_explore_intent`, which folds the result
    into `nl2sql.build_intent_prompt`'s `extra_glossary`.
  * `POST/PUT/DELETE /api/admin/glossary` — authoring is `manage_generators`
    only (the same permission that gates registering generator code), not a
    reach check: a platform term is a company asset, edited by the people who
    already own the generator registry, not by whoever happens to run a
    report. Every write is audited via the shared `_log` helper.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.i18n import _
from app.auth.deps import get_current_user, require_permission
from app.database import get_db
from app.models import Generator, GlossaryTerm, User
from app.api.generators import _generator_dashboards, _log

router = APIRouter()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _term_json(t: GlossaryTerm) -> dict:
    return {
        "id": t.id,
        "term": t.term,
        "definition": t.definition,
        "generator_id": t.generator_id,
        "created_by": t.created_by,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


# ─────────────────────────────── read (every user, reach-scoped) ──────────

@router.get("/api/glossary")
async def api_glossary(
    generator_id: int | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Company-wide terms for everyone, plus terms scoped to a generator this
    user may run. `generator_id` narrows to one generator's own terms (still
    404 if the user has no reach to it — same "no state between invisible
    and allowed" convention as `app/api/quality.py`); omitted, it returns
    company-wide terms plus every generator-scoped term this user may see."""
    gen_ids = {d.generator_id for d in _generator_dashboards(db, user) if d.generator_id}
    if generator_id is not None:
        if generator_id not in gen_ids:
            raise HTTPException(status_code=404, detail=_("Generator unavailable"))
        q = db.query(GlossaryTerm).filter(
            or_(GlossaryTerm.generator_id.is_(None), GlossaryTerm.generator_id == generator_id))
    else:
        q = db.query(GlossaryTerm).filter(
            or_(GlossaryTerm.generator_id.is_(None),
                GlossaryTerm.generator_id.in_(sorted(gen_ids)) if gen_ids else False))
    rows = q.order_by(GlossaryTerm.term).all()
    return {"terms": [_term_json(t) for t in rows]}


# ─────────────────────────────── admin write (manage_generators) ──────────

class GlossaryBody(BaseModel):
    term: str = Field(min_length=1, max_length=120)
    definition: str = Field(min_length=1, max_length=500)
    generator_id: int | None = None


def _norm_term(raw: str) -> str:
    return " ".join(raw.strip().split())


def _check_generator(db: Session, generator_id: int | None) -> None:
    if generator_id is None:
        return
    if not db.query(Generator.id).filter(Generator.id == generator_id).first():
        raise HTTPException(status_code=404, detail=_("Generator not found"))


def _check_unique(db: Session, term: str, generator_id: int | None, exclude_id: int | None = None) -> None:
    # SQLite (and standard SQL) treats NULL != NULL, so the DB's own unique
    # constraint on (term, generator_id) never fires between two company-wide
    # rows of the same term — enforce that case here too, case-insensitively
    # so "SL" and "sl" cannot both claim the same company-wide slot.
    q = db.query(GlossaryTerm.id).filter(
        func.lower(GlossaryTerm.term) == term.lower(),
        GlossaryTerm.generator_id.is_(None) if generator_id is None else GlossaryTerm.generator_id == generator_id,
    )
    if exclude_id is not None:
        q = q.filter(GlossaryTerm.id != exclude_id)
    if q.first():
        raise HTTPException(status_code=409, detail=_("This term is already defined in this scope"))


@router.post("/api/admin/glossary", status_code=201)
async def create_glossary_term(
    body: GlossaryBody,
    request: Request,
    user: User = Depends(require_permission("manage_generators")),
    db: Session = Depends(get_db),
):
    term = _norm_term(body.term)
    if not term:
        raise HTTPException(status_code=422, detail="term must not be blank")
    _check_generator(db, body.generator_id)
    _check_unique(db, term, body.generator_id)
    row = GlossaryTerm(term=term, definition=body.definition.strip(),
                       generator_id=body.generator_id, created_by=user.id,
                       updated_at=datetime.now(timezone.utc))
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=_("This term is already defined in this scope"))
    _log(db, user, "glossary_create", "glossary_term", str(row.id),
        {"term": term, "generator_id": body.generator_id}, ip=_client_ip(request))
    db.commit()
    return _term_json(row)


@router.put("/api/admin/glossary/{term_id}")
async def update_glossary_term(
    term_id: int,
    body: GlossaryBody,
    request: Request,
    user: User = Depends(require_permission("manage_generators")),
    db: Session = Depends(get_db),
):
    row = db.query(GlossaryTerm).filter(GlossaryTerm.id == term_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=_("Term not found"))
    term = _norm_term(body.term)
    if not term:
        raise HTTPException(status_code=422, detail="term must not be blank")
    _check_generator(db, body.generator_id)
    _check_unique(db, term, body.generator_id, exclude_id=term_id)
    before = {"term": row.term, "definition": row.definition, "generator_id": row.generator_id}
    row.term = term
    row.definition = body.definition.strip()
    row.generator_id = body.generator_id
    row.updated_at = datetime.now(timezone.utc)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=_("This term is already defined in this scope"))
    _log(db, user, "glossary_update", "glossary_term", str(row.id),
        {"before": before, "after": {"term": term, "definition": row.definition,
                                     "generator_id": row.generator_id}},
        ip=_client_ip(request))
    db.commit()
    return _term_json(row)


@router.delete("/api/admin/glossary/{term_id}")
async def delete_glossary_term(
    term_id: int,
    request: Request,
    user: User = Depends(require_permission("manage_generators")),
    db: Session = Depends(get_db),
):
    row = db.query(GlossaryTerm).filter(GlossaryTerm.id == term_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=_("Term not found"))
    detail = {"term": row.term, "generator_id": row.generator_id}
    db.delete(row)
    _log(db, user, "glossary_delete", "glossary_term", str(term_id), detail, ip=_client_ip(request))
    db.commit()
    return {"ok": True}
