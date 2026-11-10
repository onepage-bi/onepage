"""Catalog API: the read page (`/catalog`), the table detail drawer, the two
human write paths (confirm/reject/reset, rename/reassign-owner) and the
"suggest inputs" patch for legacy generators.

ADR-12 (`docs/PERF_DECISIONS.md`): inference is a SUGGESTER. Every row in
`catalog_tables` is born `inferred`; this module is the ONLY place a human's
decision (`confirmed`/`rejected`) or edit (`display_name`/`owner_id`) is
written — `app.services.catalog.rebuild()` never touches those fields once a
human has. This module never writes a manifest either: `suggest-inputs`
hands back a patch a human splices in and loads as a new generator version.

Scope mirrors `app/api/quality.py` exactly, because it is the same reach
model applied to a different table:
  * **Read reach** (`GET /catalog`, `GET …/tables`, `GET …/tables/{id}`) =
    the data sources reachable through `_generator_dashboards(db, user)` —
    an administrator sees everything, everyone else sees their own
    dashboards plus their `can_update_data` grants, narrowed to sources
    those dashboards' own runs have actually OBSERVED
    (`_sources_observed_by`). A source with no reach is invisible: the list
    omits it and the detail route 404s.
  * **Write reach** (confirm/reject/reset, PATCH) = the same computation,
    but returns 403 rather than 404 on a table that DOES exist — unlike the
    read routes, a write target's existence is not itself the secret; only
    the permission to act on it is.
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from jinja2 import Environment
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.database import get_db
from app.models import (
    CatalogConsumer, CatalogNonTable, CatalogPartition, CatalogTable,
    DataSource, Generator, GeneratorVersion, ROLE_ADMINISTRATOR, User,
)
from app.services import catalog, genmanifest
from app.api.generators import (
    _log, _reachable_generator_dashboards, _sources_observed_by,
)


def _require_catalog_enabled() -> None:
    if not catalog.enabled():
        raise HTTPException(status_code=404, detail="Catalog is disabled")


# Every route on this router 404s when the kill switch is off — the page and
# every JSON endpoint alike, one dependency instead of a repeated guard.
router = APIRouter(dependencies=[Depends(_require_catalog_enabled)])

_MAX_DISPLAY_NAME = 255


# ─────────────────────────────── reach ────────────────────────────────────

def _reachable_source_ids(db: Session, user: User) -> set[int] | None:
    """`None` means "unrestricted" (administrator); otherwise the set of
    data_source_ids reachable through a dashboard this user may run that has
    actually observed that source — identical computation to `/data-quality`
    and to `list_input_overrides`."""
    if user.role == ROLE_ADMINISTRATOR:
        return None
    dash_ids = _reachable_generator_dashboards(db, user)
    if not dash_ids:
        return set()
    return {sid for _did, sid in _sources_observed_by(db, dash_ids).all() if sid is not None}


def _catalog_write_reach(db: Session, user: User, source_id: int) -> bool:
    """Admin, or a dashboard this user can update that has OBSERVED
    `source_id` — same boundary as `_quality_reach` in `app/api/quality.py`."""
    if user.role == ROLE_ADMINISTRATOR:
        return True
    dash_ids = _reachable_generator_dashboards(db, user)
    if not dash_ids:
        return False
    return _sources_observed_by(db, dash_ids, source_id).first() is not None


# ─────────────────────────────── shaping ──────────────────────────────────

def _empty_stats() -> dict:
    return {"tables": 0, "inferred": 0, "confirmed": 0, "rejected": 0, "nontables": 0}


def _owner_names(db: Session, owner_ids: set[int]) -> dict[int, str]:
    owner_ids = {i for i in owner_ids if i is not None}
    if not owner_ids:
        return {}
    return {u.id: (u.display_name or u.person_id)
            for u in db.query(User).filter(User.id.in_(owner_ids)).all()}


def _summaries(db: Session, rows: list[CatalogTable]) -> list[dict]:
    if not rows:
        return []
    table_ids = [r.table_id for r in rows]
    ds_ids = {r.data_source_id for r in rows}

    parts_by_table: dict[str, list[CatalogPartition]] = {}
    for p in (db.query(CatalogPartition)
              .filter(CatalogPartition.table_id.in_(table_ids)).all()):
        parts_by_table.setdefault(p.table_id, []).append(p)

    consumers_by_table: dict[str, list[str]] = {}
    for cc, gname in (db.query(CatalogConsumer, Generator.name)
                      .join(Generator, Generator.id == CatalogConsumer.generator_id)
                      .filter(CatalogConsumer.table_id.in_(table_ids)).all()):
        consumers_by_table.setdefault(cc.table_id, []).append(gname)

    sources = {ds.id: {"id": ds.id, "key_name": ds.key_name, "label": ds.label}
               for ds in db.query(DataSource).filter(DataSource.id.in_(ds_ids)).all()}

    owners = _owner_names(db, {r.owner_id for r in rows})

    # Staleness (`rebuild`'s own contract, see catalog.py docstring): within
    # one source, every table the LATEST rebuild still recognises carries the
    # SAME `last_seen_at` stamp — anything older lagged behind that pass.
    max_seen: dict[int, datetime] = {}
    for r in rows:
        if r.last_seen_at is None:
            continue
        cur = max_seen.get(r.data_source_id)
        if cur is None or r.last_seen_at > cur:
            max_seen[r.data_source_id] = r.last_seen_at

    out = []
    for r in rows:
        parts = sorted(parts_by_table.get(r.table_id, []), key=lambda p: p.key, reverse=True)
        latest = parts[0] if parts else None
        src = sources.get(r.data_source_id) or {"id": r.data_source_id, "key_name": None, "label": None}
        out.append({
            "table_id": r.table_id,
            "name": r.name,
            "display_name": r.display_name,
            "status": r.status,
            "confidence": r.confidence,
            "n_files": r.n_files,
            "n_columns": len(r.get_columns()),
            "partitions": len(parts),
            "latest_partition": {"key": latest.key, "last_seen_at": _iso(latest.last_seen_at)}
                                if latest else None,
            "consumers": sorted(consumers_by_table.get(r.table_id, [])),
            "owner": {"id": r.owner_id, "name": owners.get(r.owner_id)} if r.owner_id else None,
            "source": {"id": src["id"], "key_name": src.get("key_name")},
            "stale": bool(r.last_seen_at and max_seen.get(r.data_source_id)
                          and r.last_seen_at < max_seen[r.data_source_id]),
            "version": r.version,
            "first_seen_at": _iso(r.first_seen_at),
            "last_seen_at": _iso(r.last_seen_at),
        })
    return out


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _list_tables(db: Session, user: User, source_id: int | None, status: str | None) -> dict:
    allowed = _reachable_source_ids(db, user)
    if allowed is not None and not allowed:
        return {"tables": [], "stats": _empty_stats()}
    if source_id is not None and allowed is not None and source_id not in allowed:
        return {"tables": [], "stats": _empty_stats()}

    scope_q = db.query(CatalogTable)
    if allowed is not None:
        scope_q = scope_q.filter(CatalogTable.data_source_id.in_(sorted(allowed)))
    if source_id is not None:
        scope_q = scope_q.filter(CatalogTable.data_source_id == source_id)
    scope_rows = scope_q.all()

    stats = _empty_stats()
    stats["tables"] = len(scope_rows)
    for r in scope_rows:
        if r.status in stats:
            stats[r.status] += 1
    # Deliberately NOT derived from scope_rows: a source whose inference
    # produced zero catalog_tables (every sheet a pivot/fragment) has no
    # CatalogTable row at all, so a `ds_ids = {r.data_source_id for r in
    # scope_rows}` count would silently drop its non-tables to zero — the
    # single worst case ("inference could not read this folder") reported as
    # "nothing to see". Scope the non-tables count the same way the table
    # scope itself is computed: by reach/source_id, not by what happened to
    # produce a table.
    if source_id is not None:
        nt_q = db.query(CatalogNonTable).filter(CatalogNonTable.data_source_id == source_id)
    elif allowed is not None:
        nt_q = (db.query(CatalogNonTable)
               .filter(CatalogNonTable.data_source_id.in_(sorted(allowed)))
               if allowed else None)
    else:
        nt_q = db.query(CatalogNonTable)
    stats["nontables"] = nt_q.count() if nt_q is not None else 0

    rows = scope_rows
    if status is not None:
        if status not in catalog.STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of {catalog.STATUSES}")
        rows = [r for r in rows if r.status == status]
    rows.sort(key=lambda r: (r.data_source_id, r.name))
    return {"tables": _summaries(db, rows), "stats": stats}


def _table_or_404(db: Session, table_id: str) -> CatalogTable:
    row = db.query(CatalogTable).filter(CatalogTable.table_id == table_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Table not found")
    return row


def _assert_read_reach(db: Session, user: User, row: CatalogTable) -> None:
    allowed = _reachable_source_ids(db, user)
    if allowed is not None and row.data_source_id not in allowed:
        raise HTTPException(status_code=404, detail="Table not found")


def _is_legacy(db: Session, generator_id: int) -> bool:
    """A generator with no declared `inputs` (the engine reads whatever Excel
    it finds) — the population "Suggest inputs" targets."""
    cur = (db.query(GeneratorVersion)
           .filter(GeneratorVersion.generator_id == generator_id,
                   GeneratorVersion.is_current == True)  # noqa: E712
           .first())
    if cur is None:
        return False
    return genmanifest.is_legacy_inputs(genmanifest.inputs_for(cur.get_manifest()))


def _detail(db: Session, row: CatalogTable) -> dict:
    parts = sorted(
        db.query(CatalogPartition).filter(CatalogPartition.table_id == row.table_id).all(),
        key=lambda p: p.key, reverse=True)
    consumers = []
    for cc, gname in (db.query(CatalogConsumer, Generator.name)
                      .join(Generator, Generator.id == CatalogConsumer.generator_id)
                      .filter(CatalogConsumer.table_id == row.table_id).all()):
        consumers.append({"generator_id": cc.generator_id, "generator_name": gname,
                          "matched_input": cc.matched_input, "last_run_id": cc.last_run_id,
                          "legacy": _is_legacy(db, cc.generator_id)})
    consumers.sort(key=lambda c: c["generator_name"])

    nontables = (db.query(CatalogNonTable)
                .filter(CatalogNonTable.data_source_id == row.data_source_id)
                .order_by(CatalogNonTable.file_name, CatalogNonTable.sheet).all())

    src = db.query(DataSource).filter(DataSource.id == row.data_source_id).first()
    owners = _owner_names(db, {row.owner_id} if row.owner_id else set())
    confirmer = _owner_names(db, {row.confirmed_by} if row.confirmed_by else set())

    newest_mtime = max((p.mtime_ms for p in parts if p.mtime_ms is not None), default=None)
    freshness = None
    if newest_mtime is not None:
        now_ms = int(datetime.utcnow().timestamp() * 1000)
        freshness = {"newest_mtime_ms": newest_mtime,
                    "days_since": round(max(0, now_ms - newest_mtime) / 86400000, 2)}

    return {
        "table_id": row.table_id,
        "data_source_id": row.data_source_id,
        "source": {"id": row.data_source_id,
                  "key_name": getattr(src, "key_name", None),
                  "label": getattr(src, "label", None)},
        "name": row.name,
        "name_stem": row.name_stem,
        "display_name": row.display_name,
        "status": row.status,
        "confidence": row.confidence,
        "version": row.version,
        "owner": {"id": row.owner_id, "name": owners.get(row.owner_id)} if row.owner_id else None,
        "confirmed_by": {"id": row.confirmed_by, "name": confirmer.get(row.confirmed_by)}
                        if row.confirmed_by else None,
        "confirmed_at": _iso(row.confirmed_at),
        "superseded_by": row.superseded_by,
        "header_row": row.header_row,
        "sheets": row.get_sheets(),
        "columns": [{"name": c.get("norm"), "display": c.get("display"),
                    "present_ratio": c.get("present_ratio"), "type": c.get("type")}
                   for c in row.get_columns()],
        "core_columns": row.get_core_cols(),
        "n_files": row.n_files,
        "partitions": [{"key": p.key, "file": p.file_name, "sheet": p.sheet,
                        "rows": p.rows, "size": p.size, "mtime_ms": p.mtime_ms,
                        "last_run_id": p.last_run_id, "last_seen_at": _iso(p.last_seen_at)}
                       for p in parts],
        "consumers": consumers,
        "evidence": row.get_evidence()[:catalog.MAX_EVIDENCE],
        "freshness": freshness,
        "first_seen_at": _iso(row.first_seen_at),
        "last_seen_at": _iso(row.last_seen_at),
        "nontables": [{"file": n.file_name, "sheet": n.sheet, "kind": n.kind,
                      "reason": n.reason, "headers": json.loads(n.headers_json or "[]")}
                     for n in nontables],
    }


# ─────────────────────────────── page ─────────────────────────────────────

@router.get("/catalog", response_class=HTMLResponse)
async def catalog_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = await run_in_threadpool(_list_tables, db, user, None, None)
    env: Environment = request.app.state.jinja
    html_out = await run_in_threadpool(
        env.get_template("pages/catalog.html").render,
        user=user, request=request,
        tables_json=json.dumps(result["tables"], ensure_ascii=False),
        stats_json=json.dumps(result["stats"], ensure_ascii=False),
    )
    return HTMLResponse(html_out)


# ─────────────────────────────── list / detail ────────────────────────────

@router.get("/api/catalog/tables")
async def api_list_tables(
    source_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await run_in_threadpool(_list_tables, db, user, source_id, status)


@router.get("/api/catalog/tables/{table_id}")
async def api_table_detail(
    table_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _table_or_404(db, table_id)
    _assert_read_reach(db, user, row)
    return await run_in_threadpool(_detail, db, row)


# ─────────────────────────────── mutations ────────────────────────────────

class ConfirmBody(BaseModel):
    version: int | None = None


def _bump(row: CatalogTable) -> None:
    row.version = (row.version or 1) + 1
    row.updated_at = datetime.utcnow()


def _version_conflict(db: Session, row: CatalogTable) -> None:
    raise HTTPException(status_code=409, detail={
        "detail": "version conflict", "current": _detail(db, row),
    })


@router.post("/api/catalog/tables/{table_id}/confirm")
async def confirm_table(
    table_id: str,
    body: ConfirmBody = ConfirmBody(),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _table_or_404(db, table_id)
    if not _catalog_write_reach(db, user, row.data_source_id):
        raise HTTPException(status_code=403, detail="No permission to confirm this table")
    if body.version is not None and body.version != row.version:
        _version_conflict(db, row)
    row.status = catalog.STATUS_CONFIRMED
    row.confirmed_by = user.id
    row.confirmed_at = datetime.utcnow()
    _bump(row)
    _log(db, user, "catalog_confirm", "catalog_table", table_id,
        {"source_id": row.data_source_id, "name": row.name})
    db.commit()
    return _detail(db, row)


@router.post("/api/catalog/tables/{table_id}/reject")
async def reject_table(
    table_id: str,
    body: ConfirmBody = ConfirmBody(),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _table_or_404(db, table_id)
    if not _catalog_write_reach(db, user, row.data_source_id):
        raise HTTPException(status_code=403, detail="No permission to reject this table")
    if body.version is not None and body.version != row.version:
        _version_conflict(db, row)
    row.status = catalog.STATUS_REJECTED
    row.confirmed_by = user.id
    row.confirmed_at = datetime.utcnow()
    _bump(row)
    _log(db, user, "catalog_reject", "catalog_table", table_id,
        {"source_id": row.data_source_id, "name": row.name})
    db.commit()
    return _detail(db, row)


@router.post("/api/catalog/tables/{table_id}/reset")
async def reset_table(
    table_id: str,
    body: ConfirmBody = ConfirmBody(),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Back to `inferred` — undoes a confirm or a reject, but never touches
    `display_name`/`owner_id`: those stay human-owned once set, exactly as
    `catalog.rebuild()` leaves them (`version` is what keeps them sticky)."""
    row = _table_or_404(db, table_id)
    if not _catalog_write_reach(db, user, row.data_source_id):
        raise HTTPException(status_code=403, detail="No permission to reset this table")
    if body.version is not None and body.version != row.version:
        _version_conflict(db, row)
    row.status = catalog.STATUS_INFERRED
    row.confirmed_by = None
    row.confirmed_at = None
    _bump(row)
    _log(db, user, "catalog_reset", "catalog_table", table_id,
        {"source_id": row.data_source_id, "name": row.name})
    db.commit()
    return _detail(db, row)


class PatchBody(BaseModel):
    display_name: str | None = None
    owner_id: int | None = None
    version: int = Field(..., description="Version this client last saw — required, bumped on save")


@router.patch("/api/catalog/tables/{table_id}")
async def patch_table(
    table_id: str,
    body: PatchBody,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _table_or_404(db, table_id)
    if not _catalog_write_reach(db, user, row.data_source_id):
        raise HTTPException(status_code=403, detail="No permission to edit this table")
    if body.version != row.version:
        _version_conflict(db, row)

    fields_set = body.model_fields_set
    changed: dict = {}
    if "display_name" in fields_set:
        if body.display_name is None:
            raise HTTPException(status_code=422, detail="display_name cannot be cleared to null")
        name = body.display_name.strip()[:_MAX_DISPLAY_NAME]
        if not name:
            raise HTTPException(status_code=422, detail="display_name must not be blank")
        row.display_name = name
        changed["display_name"] = name
    if "owner_id" in fields_set:
        if body.owner_id is not None:
            owner = db.query(User).filter(User.id == body.owner_id).first()
            if owner is None:
                raise HTTPException(status_code=422, detail="owner_id: no such user")
        row.owner_id = body.owner_id
        changed["owner_id"] = body.owner_id

    # Bump even on a no-op PATCH (an empty body still means "I looked at this
    # row and want display_name pinned from now on") — this is the mechanism
    # that freezes display_name against `rebuild()`'s `version <= 1` check.
    _bump(row)
    _log(db, user, "catalog_update", "catalog_table", table_id,
        {"source_id": row.data_source_id, **changed})
    db.commit()
    return _detail(db, row)


# ─────────────────────────────── suggest inputs ───────────────────────────

@router.get("/api/catalog/suggest-inputs/{generator_id}")
async def api_suggest_inputs(
    generator_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """`catalog.suggest_inputs()` plus a ready-to-paste `inputs` JSON block.
    Pure read — never writes a manifest (ADR-12). Scoped to generators this
    user can actually run; an admin sees any generator's suggestion.

    The gate below only proves the caller can run ONE dashboard of this
    generator — a generator can back several dashboards over different
    folders owned by different people. The payload itself is narrowed
    separately via `allowed_source_ids` (`_reachable_source_ids`) so a
    reader never sees another owner's source contents just because they
    share a generator."""
    if user.role != ROLE_ADMINISTRATOR:
        dash_ids = _reachable_generator_dashboards(db, user, generator_id)
        if not dash_ids:
            raise HTTPException(status_code=404,
                                detail="No report you can run uses this generator")
    generator = db.query(Generator).filter(Generator.id == generator_id).first()
    if generator is None:
        raise HTTPException(status_code=404, detail="Generator not found")

    allowed = _reachable_source_ids(db, user)
    result = await run_in_threadpool(
        catalog.suggest_inputs, db, generator_id, allowed_source_ids=allowed)
    result["inputs_json"] = json.dumps(result["inputs"], ensure_ascii=False, indent=2)
    result["generator_name"] = generator.name
    _log(db, user, "catalog_suggest_inputs", "generator", str(generator_id),
        {"source_ids": [s["data_source_id"] for s in result["sources"]]})
    db.commit()
    return result


# ─────────────────────────────── rebuild ───────────────────────────────────

@router.post("/api/catalog/rebuild")
async def api_rebuild(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Admin-only, for the "Recalculate" button — everyone else already gets
    this for free from the `_gc` daily tick and the post-run debounce."""
    if user.role != ROLE_ADMINISTRATOR:
        raise HTTPException(status_code=403, detail="Administrators only")
    stats = await run_in_threadpool(catalog.rebuild, db, None, budget_s=30.0)
    _log(db, user, "catalog_rebuild", "catalog", "*", stats)
    db.commit()
    return stats
