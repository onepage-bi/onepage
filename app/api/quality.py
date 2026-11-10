"""Data Quality API: the read page, the badge, and the two human write paths
(acknowledge a finding, accept new values into a baseline).

Everything here defers to `app.services.quality` for computation — this
module is authorisation, validation, and shaping the evaluator's output into
HTTP. Scope for the read routes is `_generator_dashboards(db, user)`, the
exact reach the Data Sources page and the run-start endpoint already use: an
administrator sees everything, everyone else sees their own dashboards plus
their `can_update_data` grants.

**Write reach.** An ack or a baseline accept touches ONE source. Reach for
that is "admin, or you can run a dashboard that has actually OBSERVED this
source" — the same rule `PUT …/overrides/{generator_id}` already enforces
via `_reachable_generator_dashboards` + `_sources_observed_by`. Reused here
rather than re-derived, so "can you touch this source" means the same thing
on every route that asks it. Deliberately only ever a 404 ("no report you
can run reads this source") and never a 403: under a reach model there is no
state between "you have no standing to even see this" and "you may act" —
the plan allowed either, and 404 is what the sibling endpoint already does.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.database import get_db
from app.models import (
    DataSource, Generator, GeneratorVersion, QualityAck, ROLE_ADMINISTRATOR, User, ValueBaseline,
)
from app.services import genmanifest, quality
from app.api.generators import (
    _generator_dashboards, _log, _reachable_generator_dashboards, _sources_observed_by,
)
from sqlalchemy.exc import IntegrityError

router = APIRouter()

_FINDING_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_MAX_NOTE = 500
_MAX_ACCEPT = 500
_MAX_ACCEPT_LEN = 64
_MAX_MATCH = 128
_MAX_SHEET = 64
_MAX_COLUMN = 64

# Open-count per user, refreshed at most this often — the bell polls on every
# page nav, and `evaluate()` walks a lineage chain per (generator, source)
# pair. Cleared wholesale (not just for the acting user) by the write routes
# below: an ack or an accepted baseline can change what OTHER users with
# reach to the same source see as open, and the table is small enough that
# clearing it beats trying to know in advance who else it affects.
_BADGE_TTL_S = 60.0
_badge_cache: dict[int, tuple[float, int]] = {}


def _quality_reach(db: Session, user: User, source_id: int, generator_id: int | None = None) -> bool:
    """Same boundary as `put_input_override`: admin, or a dashboard this user
    can update that has OBSERVED `source_id` (narrowed to `generator_id` when
    given, for the baseline route where the accept is generator-specific)."""
    if user.role == ROLE_ADMINISTRATOR:
        return True
    dash_ids = _reachable_generator_dashboards(db, user, generator_id)
    if not dash_ids:
        return False
    return _sources_observed_by(db, dash_ids, source_id).first() is not None


@router.get("/data-quality", response_class=HTMLResponse)
async def data_quality_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The Data Quality page. Template is a minimal placeholder here — P3
    fleshes it out — but the data it needs is already the real evaluator
    output, scoped exactly like `/data-quality`'s JSON sibling below."""
    dashboards = _generator_dashboards(db, user)
    result = await run_in_threadpool(quality.evaluate, db, dashboards)

    env: Environment = request.app.state.jinja
    html_out = await run_in_threadpool(
        env.get_template("pages/data_quality.html").render,
        user=user, request=request,
        findings_json=json.dumps(result["findings"], ensure_ascii=False),
        sources_json=json.dumps(result["sources"], ensure_ascii=False),
        history_json=json.dumps(result["history"], ensure_ascii=False),
        stats_json=json.dumps(result["stats"], ensure_ascii=False),
        pending_json=json.dumps(result["pending_values"], ensure_ascii=False),
    )
    return HTMLResponse(html_out)


@router.get("/api/data-quality")
async def api_data_quality(
    source_id: int | None = Query(default=None),
    generator: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The evaluator's output, scoped to this user's reach. `source_id`/
    `generator` narrow `findings`/`pending_values` for a drawer or a filtered
    table view — `sources`/`history`/`stats` stay whole so the page's tiles
    keep counting the user's entire scope, not just the current filter."""
    dashboards = _generator_dashboards(db, user)
    result = await run_in_threadpool(quality.evaluate, db, dashboards)
    if source_id is not None:
        result["findings"] = [f for f in result["findings"] if f["source_id"] == source_id]
        result["pending_values"] = [p for p in result["pending_values"] if p["source_id"] == source_id]
    if generator is not None:
        result["findings"] = [f for f in result["findings"] if f["generator"] == generator]
        result["pending_values"] = [p for p in result["pending_values"] if p["generator"] == generator]
    return result


@router.get("/api/data-quality/badge")
async def data_quality_badge(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """{"open": n} for the topbar bell. Cached per user for `_BADGE_TTL_S` —
    `evaluate(impact=False)` skips the impact-analysis lineage graph, but the
    schema/freshness/checks/drift blocks still cost a real chain walk."""
    now = monotonic()
    cached = _badge_cache.get(user.id)
    if cached is not None and now - cached[0] < _BADGE_TTL_S:
        return {"open": cached[1]}
    dashboards = _generator_dashboards(db, user)
    n = await run_in_threadpool(quality.badge_count, db, dashboards)
    _badge_cache[user.id] = (now, n)
    return {"open": n}


class AckBody(BaseModel):
    finding_id: str
    source_id: int
    until_run_id: int | None = None
    until_at: str | None = None    # ISO-8601
    note: str | None = Field(default=None, max_length=_MAX_NOTE)


def _parse_iso(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="until_at must be ISO-8601")
    if dt.tzinfo is not None:
        # Convert to the naive UTC wall clock the evaluator compares against
        # (`datetime.utcnow()`) instead of dropping the offset in place —
        # otherwise e.g. "+03:00" would silently expire the ack 3 hours late.
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@router.post("/api/data-quality/acks", status_code=201)
async def create_ack(
    body: AckBody,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """"I know" about one finding — bounded by `until_run_id` (expires the
    moment a newer run reproduces it) or `until_at` (a date), both NULL is
    open-ended. Neither bound is validated against the finding itself: the
    evaluator recomputes `since_run` fresh every read and applies the ack
    then, so a stale/wrong bound here just means the ack does less than the
    author intended, never that it silently hides something it should not."""
    fid = (body.finding_id or "").strip().lower()
    if not _FINDING_ID_RE.match(fid):
        raise HTTPException(status_code=422, detail="finding_id must be 16 hex chars")
    ds = db.query(DataSource).filter(DataSource.id == body.source_id).first()
    if ds is None or not _quality_reach(db, user, body.source_id):
        raise HTTPException(status_code=404, detail="No report you can run reads this source")

    until_at = _parse_iso(body.until_at) if body.until_at else None
    note = (body.note or "").strip()[:_MAX_NOTE] or None
    now = datetime.utcnow()

    # Reuse a still-active ack on the same (finding, source) instead of
    # inserting a duplicate row: two rows for the same finding meant DELETE
    # on one left the other silently keeping it acked (a double-click, or a
    # retried request, looked like "un-ack did nothing"). An ack whose bound
    # has already lapsed is NOT reused — it is dead, so a fresh ack should
    # get its own row and its own id rather than resurrecting an expired one.
    row = (db.query(QualityAck)
           .filter(QualityAck.finding_id == fid, QualityAck.data_source_id == body.source_id)
           .order_by(QualityAck.id.desc()).first())
    if row is not None and row.until_at is not None and row.until_at <= now:
        row = None
    if row is None:
        row = QualityAck(finding_id=fid, data_source_id=body.source_id, created_by=user.id)
        db.add(row)
    row.until_run_id = body.until_run_id
    row.until_at = until_at
    row.note = note
    row.created_by = user.id
    db.flush()
    _log(db, user, "quality_ack_create", "quality_ack", fid, {
        "source_id": body.source_id, "until_run_id": body.until_run_id,
        "until_at": body.until_at, "note": note,
    })
    db.commit()
    _badge_cache.clear()
    return {"id": row.id}


@router.delete("/api/data-quality/acks/{ack_id}", status_code=204)
async def delete_ack(
    ack_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.query(QualityAck).filter(QualityAck.id == ack_id).first()
    if row is None or row.data_source_id is None \
            or not _quality_reach(db, user, row.data_source_id):
        raise HTTPException(status_code=404, detail="Ack not found")
    _log(db, user, "quality_ack_delete", "quality_ack", row.finding_id,
        {"source_id": row.data_source_id})
    db.delete(row)
    db.commit()
    _badge_cache.clear()
    return Response(status_code=204)


class BaselineAcceptBody(BaseModel):
    match: str
    sheet: str
    column: str
    accept: list[str]
    version: int | None = None


@router.post("/api/data-sources/{source_id}/baselines/{generator_id}")
async def accept_baseline_values(
    source_id: int,
    generator_id: int,
    body: BaselineAcceptBody,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Merge `accept` into the (source, generator, match, sheet, column)
    baseline — creating it at version 1 if nothing was seeded yet, otherwise
    requiring `version` to match the row the client read (`409 {current}` on
    a stale accept, the same optimistic-lock shape as the overrides PUT)."""
    ds = db.query(DataSource).filter(DataSource.id == source_id).first()
    if ds is None:
        raise HTTPException(status_code=404, detail="Source not found")
    generator = db.query(Generator).filter(Generator.id == generator_id).first()
    if generator is None:
        raise HTTPException(status_code=404, detail="Generator not found")
    if not _quality_reach(db, user, source_id, generator_id):
        raise HTTPException(status_code=404,
                            detail="No report you can run reads this source through this generator")

    match = (body.match or "").strip()[:_MAX_MATCH]
    sheet = (body.sheet or "").strip()[:_MAX_SHEET]
    column = (body.column or "").strip()[:_MAX_COLUMN]
    if not match or not sheet or not column:
        raise HTTPException(status_code=422, detail="match, sheet and column are required")
    if len(body.accept) > _MAX_ACCEPT:
        raise HTTPException(status_code=422, detail=f"accept: at most {_MAX_ACCEPT} values")
    accept = [v.strip()[:_MAX_ACCEPT_LEN] for v in body.accept if (v or "").strip()]
    if not accept:
        raise HTTPException(status_code=422, detail="accept: at least one non-empty value")

    # `match` must be one of the generator's CURRENT declared inputs, same
    # check and same reasoning as `_clean_aliases` for overrides: a baseline
    # for a pattern the generator does not declare can never be matched by
    # the evaluator and would only persist as a silent no-op. `sheet` and
    # `column` are NOT checked against the manifest — same rationale as
    # aliases — sheets are globs and the observed name is what the drawer
    # offers, and a later manifest edit renaming a column should not turn an
    # existing accepted baseline into a 400 on an unrelated save.
    cur = (db.query(GeneratorVersion)
           .filter(GeneratorVersion.generator_id == generator_id,
                   GeneratorVersion.is_current == True)  # noqa: E712
           .first())
    inputs = genmanifest.inputs_for(cur.get_manifest()) if cur else []
    known = {str(i.get("match", "")).casefold() for i in inputs}
    if match.casefold() not in known:
        raise HTTPException(status_code=422,
                            detail=f"match: {match!r} is not one of this generator's inputs")

    row = (db.query(ValueBaseline)
           .filter(ValueBaseline.data_source_id == source_id,
                   ValueBaseline.generator_id == generator_id,
                   ValueBaseline.match == match, ValueBaseline.sheet == sheet,
                   ValueBaseline.column == column)
           .first())

    if row is None:
        row = ValueBaseline(data_source_id=source_id, generator_id=generator_id,
                            match=match, sheet=sheet, column=column)
        row.set_values(accept)
        added = row.get_values()
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            # Two concurrent first-accepts on the same (source, generator,
            # match, sheet, column) race the unique constraint — the loser
            # gets the documented 409 (there IS now a current row to read),
            # not a bare 500.
            db.rollback()
            row = (db.query(ValueBaseline)
                   .filter(ValueBaseline.data_source_id == source_id,
                           ValueBaseline.generator_id == generator_id,
                           ValueBaseline.match == match, ValueBaseline.sheet == sheet,
                           ValueBaseline.column == column)
                   .first())
            raise HTTPException(status_code=409, detail={
                "detail": "version conflict",
                "current": {"values": row.get_values() if row else [], "version": row.version if row else 1},
            })
    else:
        if body.version is not None and body.version != row.version:
            raise HTTPException(status_code=409, detail={
                "detail": "version conflict",
                "current": {"values": row.get_values(), "version": row.version},
            })
        merged = set(row.get_values()) | set(accept)
        # The engine never emits more than `genmanifest.MAX_KNOWN_VALUES`
        # distinct values (`_lineage.py:distinct_values`), so a baseline
        # cannot usefully compare against more than that either — cap the
        # MERGED set (not just this request) so repeated small accepts don't
        # grow the row past what the evaluator can ever match against.
        # Newest values win when the cap forces a trim (they are the ones
        # the human just accepted; oldest already-known values are the most
        # likely to be stale).
        if len(merged) > genmanifest.MAX_KNOWN_VALUES:
            keep_new = set(accept)
            keep_old = sorted(set(row.get_values()) - keep_new)
            budget = genmanifest.MAX_KNOWN_VALUES - len(keep_new & merged)
            merged = keep_new | set(keep_old[:max(budget, 0)])
        added = sorted(set(accept) - set(row.get_values()))
        row.set_values(merged)
        row.version += 1
    row.accepted_by = user.id
    row.accepted_at = datetime.utcnow()

    _log(db, user, "quality_baseline_accept", "value_baseline", f"{source_id}:{generator_id}", {
        "match": match, "sheet": sheet, "column": column, "added": added,
    })
    db.commit()
    _badge_cache.clear()
    return {"ok": True, "values": row.get_values(), "version": row.version}
