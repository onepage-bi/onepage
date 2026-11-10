"""Server lake cache (ADR-14) — row-level Parquet partitions of a report's source folder, stored
ON THE SERVER, written by the report's generator run, readable by anyone with
dashboard access.

Storage layout: `data/uploads/_lake/{dashboard_id}/{key}.parquet` +
`manifest.json` (see `app/services/storage.py`). Only the LATEST run is kept —
a manifest publish deletes every `.parquet` whose key it does not list.

This is data-plane storage of the same class as a dashboard slot file: sniffed
(magic bytes), size-capped BEFORE anything reaches the disk, and the stored
name is built from a server-validated `key` only — never from anything a
client names.

Share-link sessions (`/s/{token}`) must NOT reach any of this: every route
below depends on the signed-in auth chain (`get_current_user` /
`require_dashboard_access`), never the share-token deps.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.auth.deps import (
    can_update_dashboard_data, get_current_user, get_dashboard_or_404, require_dashboard_access,
)
from app.config import settings
from app.database import get_db
from app.models import AuditLog, Dashboard, ROLE_ADMINISTRATOR, User
from app.services import explore_answers, lake_exec, rls, storage

router = APIRouter()

# `key` is a hex digest (sha1/sha256-shaped) minted by the generator engine —
# never a client-chosen name, so it doubles as the whole filename.
_KEY_RE = re.compile(r"^[a-f0-9]{16,64}$")

_MAX_FILES = 2000
_MAX_STR = 300
_CHUNK = 256 * 1024

# First 4 bytes of every Parquet file, regardless of version — the ONLY sniff
# that matters here since the server never parses the content.
_PARQUET_MAGIC = b"PAR1"


def _log(db: Session, actor: User, action: str, resource_type: str, resource_id: str,
         detail: dict, ip: str = "") -> None:
    entry = AuditLog(
        actor_id=actor.id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        ip_address=ip,
    )
    entry.set_detail(detail)
    db.add(entry)


def _assert_can_update_data(dashboard: Dashboard, user: User, db: Session) -> None:
    # Plain-function call, like generators.py's own helper: the dependency
    # shape would re-resolve the dashboard from the path, which is not where
    # this one comes from (it's already a resolved Depends value here).
    if not can_update_dashboard_data(dashboard=dashboard, user=user, db=db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")


def _assert_rls_full_access(dashboard: Dashboard, user: User, db: Session) -> None:
    """ADR-18: the lake is the same rows of the report, only in parquet and without any
    filtering, so for an RLS report it is closed to everyone except a subject with
    see_all (admin, owner, TMD). For an ordinary report — zero queries."""
    # "Highly confidential": the chat and server-side queries read the same rows
    # by another path, so they are closed to EVERYONE, including the owner (owner's decision
    # 2026-10-20). The check comes before RLS: it is cheaper and wider.
    if dashboard.confidential:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="confidential: the report index is closed in this mode")
    if not dashboard.rls_enabled:
        return
    if not rls.subject(db, dashboard, user).see_all:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="RLS: the report index is not available to a scoped viewer")


def _validate_key(key: str) -> str:
    if not _KEY_RE.match(key):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="key must match ^[a-f0-9]{16,64}$",
        )
    return key


# ---------------------------------------------------------------------------
# Manifest body
# ---------------------------------------------------------------------------

class LakeFileEntry(BaseModel):
    name: str = Field(max_length=_MAX_STR)
    size: int = Field(ge=0)
    mtime_ms: int = Field(ge=0)
    sheet: str | None = Field(default=None, max_length=_MAX_STR)
    header_row: int | None = None
    key: str = Field(max_length=64)
    table: str = Field(max_length=_MAX_STR)
    partition: str | None = Field(default=None, max_length=_MAX_STR)
    bytes: int = Field(ge=0)

    @field_validator("key")
    @classmethod
    def _key_shape(cls, v: str) -> str:
        if not _KEY_RE.match(v):
            raise ValueError("key must match ^[a-f0-9]{16,64}$")
        return v


class LakeManifestBody(BaseModel):
    run_id: int | None = None
    parser_version: str = Field(max_length=_MAX_STR)
    files: list[LakeFileEntry] = Field(max_length=_MAX_FILES)


# ---------------------------------------------------------------------------
# POST …/lake/parts — upload one Parquet partition
# ---------------------------------------------------------------------------

async def _read_capped(file: UploadFile, cap: int) -> bytes:
    """Read with the ceiling enforced DURING the read — an oversized body is
    never fully buffered and never touches the disk (the same discipline as
    presentations._read_capped and generators.py's publish path)."""
    buf = bytearray()
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > cap:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"lake part too large (max {cap // (1024 * 1024)} MB)",
            )
    return bytes(buf)


@router.post("/api/dashboards/{dashboard_id}/lake/parts")
async def upload_lake_part(
    request: Request,
    key: str = Form(...),
    file: UploadFile = File(...),
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_can_update_data(dashboard, user, db)
    _assert_rls_full_access(dashboard, user, db)
    _validate_key(key)

    cap = settings.lake_part_max_mb * 1024 * 1024
    data = await _read_capped(file, cap)
    if len(data) < 4 or data[:4] != _PARQUET_MAGIC:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only Parquet files (magic bytes PAR1) are accepted",
        )

    storage.save_lake_part(dashboard.id, key, data)

    total_cap = settings.lake_max_mb * 1024 * 1024
    total = storage.lake_dir_bytes(dashboard.id)
    if total > total_cap:
        # Over budget: the part just written is not kept.
        storage.delete_lake_part(dashboard.id, key)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"dashboard lake dir would exceed {settings.lake_max_mb} MB",
        )

    return {"ok": True, "key": key, "bytes": len(data)}


# ---------------------------------------------------------------------------
# PUT …/lake/manifest — publish the manifest, prune stale parts
# ---------------------------------------------------------------------------

@router.put("/api/dashboards/{dashboard_id}/lake/manifest")
async def publish_lake_manifest(
    body: LakeManifestBody,
    request: Request,
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_can_update_data(dashboard, user, db)
    _assert_rls_full_access(dashboard, user, db)

    required_keys = {f.key for f in body.files}
    existing_keys = storage.list_lake_part_keys(dashboard.id)
    missing = sorted(required_keys - existing_keys)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"missing_keys": missing},
        )

    manifest = {
        "run_id": body.run_id,
        "parser_version": body.parser_version,
        "files": [f.model_dump() for f in body.files],
        "created_at": datetime.utcnow().isoformat(),
        "by": user.person_id,
    }
    payload = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    storage.save_lake_manifest(dashboard.id, payload)

    # Only the LATEST run is kept: prune every part the new manifest does not
    # list.
    stale = existing_keys - required_keys
    for k in stale:
        storage.delete_lake_part(dashboard.id, k)

    total_bytes = sum(f.bytes for f in body.files)
    _log(db, user, "lake_publish", "dashboard", dashboard.id,
         {"files": len(body.files), "bytes": total_bytes},
         request.client.host if request.client else "")
    db.commit()
    return manifest


# ---------------------------------------------------------------------------
# GET …/lake/manifest
# ---------------------------------------------------------------------------

@router.get("/api/dashboards/{dashboard_id}/lake/manifest")
async def get_lake_manifest(
    dashboard: Dashboard = Depends(require_dashboard_access),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_rls_full_access(dashboard, user, db)
    raw = storage.read_lake_manifest(dashboard.id)
    if raw is None:
        raise HTTPException(status_code=404, detail="No lake manifest for this dashboard")
    return JSONResponse(content=json.loads(raw))


# ---------------------------------------------------------------------------
# GET …/lake/parts/{key}
# ---------------------------------------------------------------------------

@router.get("/api/dashboards/{dashboard_id}/lake/parts/{key}")
def get_lake_part(
    key: str,
    request: Request,
    dashboard: Dashboard = Depends(require_dashboard_access),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sync `def` on purpose (disk stat) — same convention as
    presentations.get_item_file / files.get_slot_file."""
    _assert_rls_full_access(dashboard, user, db)
    _validate_key(key)
    path = storage.lake_part_path(dashboard.id, key)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Lake part not found")

    etag = f'"{key}"'
    inm = request.headers.get("if-none-match")
    if inm and etag in [t.strip() for t in inm.split(",")]:
        return Response(status_code=304, headers={"ETag": etag})

    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={
            "Content-Encoding": "identity",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=0",
            "ETag": etag,
        },
    )


# ---------------------------------------------------------------------------
# DELETE …/lake — owner or administrator
# ---------------------------------------------------------------------------

@router.delete("/api/dashboards/{dashboard_id}/lake")
async def delete_lake(
    request: Request,
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role != ROLE_ADMINISTRATOR and dashboard.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    _assert_rls_full_access(dashboard, user, db)
    storage.delete_lake_dir(dashboard.id)
    _log(db, user, "lake_delete", "dashboard", dashboard.id, {},
         request.client.host if request.client else "")
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST …/lake/exec — run one explore op ON THE SERVER over this lake
# ---------------------------------------------------------------------------
# ADR-14b: reading the lake through a query is still reading the lake, so the
# gate is the same `require_dashboard_access` as GET …/parts. The ops mirror
# the browser worker's message shapes 1:1 (status/intent/sql/export) — the
# page's transport shim swaps `postMessage` for this endpoint and the UI code
# above it never learns the difference. SQL passes the ENGINE's own guard
# (`guard_sql` + enable_external_access=false); this endpoint adds no query
# surface of its own and never logs SQL text (filter literals may quote data
# values).

class LakeExecBody(BaseModel):
    op: str = Field(pattern=r"^(status|intent|sql|export)$")
    intent: dict | None = None
    sql: str | None = Field(default=None, max_length=20_000)
    limit: int | None = Field(default=None, ge=1, le=10_000)
    name: str | None = Field(default=None, max_length=200)
    # Optional — the chat widget's own question text, carried through only so
    # the server-side answer record (explore_answers) has something to show
    # a reader besides «op: sql». Never required: an op the widget issues
    # without a natural-language question (a manual re-run, say) still runs.
    question: str | None = Field(default=None, max_length=2000)
    # `final`: the widget marks the ONE `sql` op that stands for a completed
    # answer (runQuery / trendCard / whyCard's headline whyRun — see
    # static/js/explore.js). A dashboard's `sql`-tier answer is always
    # preceded by its own `intent` op building the SQL text, and a why-card
    # runs several more `sql` ops for sub-queries the user never sees as a
    # separate answer; without this flag every one of those would earn its
    # own explore_answers row for what is, from the user's chair, one
    # response. Recording is opt-in on this flag rather than default-on so a
    # background/internal call (grandTotals, a bare "show SQL" build with no
    # run) never gets recorded just for existing.
    final: bool = False


@router.post("/api/dashboards/{dashboard_id}/lake/exec")
async def lake_exec_op(
    body: LakeExecBody,
    request: Request,
    dashboard: Dashboard = Depends(require_dashboard_access),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not settings.explore_enabled or not settings.lake_exec:
        raise HTTPException(status_code=404, detail="Server-side explore execution is disabled")
    _assert_rls_full_access(dashboard, user, db)
    t0 = time.monotonic()
    payload = {"intent": body.intent, "sql": body.sql, "limit": body.limit}
    try:
        out = await run_in_threadpool(lake_exec.run_op, db, dashboard, body.op, payload)
    except lake_exec.LakeExecError as e:
        _log(db, user, "lake_exec", "dashboard", dashboard.id,
             {"op": body.op, "error": e.code,
              "ms": int((time.monotonic() - t0) * 1000)},
             request.client.host if request.client else "")
        db.commit()
        code = {"no_manifest": 404, "timeout": 504}.get(e.code, 409)
        raise HTTPException(status_code=code, detail={"code": e.code, "message": str(e)})
    # Server-side AI-answer record (without data): 'sql' is the only lake_exec
    # shape recorded, and only the ONE call the widget marks `final` — its
    # own `intent` op (SQL text only, no execution) and every non-final
    # `sql` sub-query (a why-card's prev-period/concentration calls,
    # grandTotals' background loop) are not a user-visible answer on their
    # own, so recording all of them would inflate "how many answers were
    # given" 2-3x over what the user actually saw. No SQL text is ever
    # recorded here either — only its hash — for the same reason this route
    # never logs SQL text: a filter literal can quote a real data value.
    if body.op == "sql" and body.final and not out.get("error") and dashboard.generator_id:
        run_id = None
        try:
            raw_man = storage.read_lake_manifest(dashboard.id)
            if raw_man:
                run_id = json.loads(raw_man).get("run_id")
        except (ValueError, TypeError, OSError):
            run_id = None
        rec = explore_answers.record(
            db, user.id, dashboard.generator_id, body.question or "", "lake", "server",
            dashboard_id=dashboard.id,
            intent=None,
            sql=body.sql,
            run_id=run_id,
        )
        # The id of that row rides back with the result: it is the only handle
        # 👍/👎 has on a lake answer (`POST /api/explore/answers/{id}/vote`).
        # None when nothing was recorded — the page keeps the vote local.
        if isinstance(out, dict):
            out["answer_id"] = rec.id if rec is not None else None
    _log(db, user, "lake_exec", "dashboard", dashboard.id,
         {"op": body.op, "ms": int((time.monotonic() - t0) * 1000)},
         request.client.host if request.client else "")
    db.commit()
    if body.op == "export":
        name = re.sub(r"[^\w.\- ]", "_", body.name or "explore.xlsx")[:100] or "explore.xlsx"
        return Response(
            content=out["bytes"],
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=0",
            },
        )
    return JSONResponse(content=out)
