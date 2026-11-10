"""Presentations — an ordered list of slides shown one after another.

A presentation is deliberately NOT a new rendering surface. A `dashboard` slide
is shown AS-IS by routing to that report's own `/d/{id}`; only `image` and `pdf`
slides carry bytes of their own, and those are server-stored files of the same
class as dashboard slot files (sniffed, size-capped, owner-only, audited — see
docs/SECURITY_UPLOADS.md).

v1 scope: a presentation is visible and editable by its owner only. Every route
answers **404** for a non-owner, never 403 — a stranger must not learn that an id
exists.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_dashboard_access
from app.database import get_db
from app.models import (
    AuditLog, Dashboard, GenerationRun, Presentation, PresentationItem, SourceObservation, User,
    PRES_DASHBOARD, PRES_HTML, PRES_IMAGE, PRES_PDF, ROLE_ADMINISTRATOR,
    RUN_SUCCESS,
)
from app.services import storage

router = APIRouter()

# Hard caps, enforced DURING the read — nothing reaches the disk before the
# whole body has been counted (the publish-path discipline, generators.py).
_MAX_IMAGE = 5 * 1024 * 1024   # 5 MB — png/jpeg slide
_MAX_PDF = 20 * 1024 * 1024    # 20 MB — pdf slide
_MAX_HTML = 10 * 1024 * 1024   # 10 MB — self-contained HTML slide (Claude deck)
_CHUNK = 256 * 1024

_MAX_NOTE = 2000
_MAX_SECTION = 64
_STALE_AFTER = timedelta(days=7)

# Magic bytes, in the order they are tested. The declared Content-Type and the
# filename are advisory only: an .png that starts with "<svg" is a 415.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", PRES_IMAGE, "png", "image/png"),
    (b"\xff\xd8\xff", PRES_IMAGE, "jpg", "image/jpeg"),
    (b"%PDF-", PRES_PDF, "pdf", "application/pdf"),
)


def _log(db: Session, actor: User, action: str, resource_type: str, resource_id: str,
         detail: dict, ip: str = "") -> None:
    entry = AuditLog(
        actor_id=actor.id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        ip_address=ip,
    )
    entry.set_detail(detail)
    db.add(entry)


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------

class PresentationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class PresentationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ItemCreate(BaseModel):
    kind: str
    dashboard_id: str | None = None
    note: str | None = Field(default=None, max_length=_MAX_NOTE)
    start_section: str | None = Field(default=None, max_length=_MAX_SECTION)


class ItemUpdate(BaseModel):
    # None is a real value here (= clear the field); presence is read from
    # model_fields_set, like DashboardUpdate.folder_id.
    note: str | None = Field(default=None, max_length=_MAX_NOTE)
    start_section: str | None = Field(default=None, max_length=_MAX_SECTION)


class OrderBody(BaseModel):
    ids: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _own(db: Session, user: User, presentation_id: str) -> Presentation:
    """Owner-only lookup. A presentation someone else owns is indistinguishable
    from one that does not exist."""
    pres = db.query(Presentation).filter(
        Presentation.id == presentation_id,
        Presentation.owner_id == user.id,
    ).first()
    if pres is None:
        raise HTTPException(status_code=404, detail="Presentation not found")
    return pres


def _own_item(db: Session, pres: Presentation, item_id: str) -> PresentationItem:
    item = db.query(PresentationItem).filter(
        PresentationItem.id == item_id,
        PresentationItem.presentation_id == pres.id,
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Slide not found")
    return item


def _touch(pres: Presentation) -> None:
    """`onupdate` only fires when the row itself changes, and an item edit does
    not change it — so bump explicitly (utcnow matches SQLite's func.now())."""
    pres.updated_at = datetime.utcnow()


async def _may_show(db: Session, user: User, dashboard: Dashboard) -> bool:
    """The same rule as the require_dashboard_access dependency — called as a
    plain function so it is one rule, not a second copy of it."""
    try:
        await require_dashboard_access(dashboard=dashboard, user=user, db=db)
        return True
    except HTTPException:
        return False


def _file_url(item: PresentationItem) -> str | None:
    if item.kind == PRES_DASHBOARD:
        return None
    return f"/presentations/{item.presentation_id}/file/{item.id}"


def _items_with_titles(db: Session, pres: Presentation) -> list[tuple[PresentationItem, str | None]]:
    """Slides in order, each paired with its dashboard title — ONE extra query
    for the whole presentation, never one per slide."""
    items = db.query(PresentationItem).filter(
        PresentationItem.presentation_id == pres.id
    ).order_by(PresentationItem.position, PresentationItem.created_at).all()
    dash_ids = [i.dashboard_id for i in items if i.dashboard_id]
    titles: dict[str, str] = {}
    if dash_ids:
        rows = db.query(Dashboard.id, Dashboard.title).filter(
            Dashboard.id.in_(set(dash_ids))
        ).all()
        titles = {r[0]: r[1] for r in rows}
    return [(i, titles.get(i.dashboard_id) if i.dashboard_id else None) for i in items]


def _item_json(item: PresentationItem, dashboard_title: str | None) -> dict:
    return {
        "id": item.id,
        "position": item.position,
        "kind": item.kind,
        "dashboard_id": item.dashboard_id,
        "dashboard_title": dashboard_title,
        "original_name": item.original_name,
        "size_bytes": item.size_bytes,
        "note": item.note,
        "start_section": item.start_section,
        "file_url": _file_url(item),
    }


def _looks_like_html(head: bytes) -> bool:
    """A whole-document HTML file: optional BOM/whitespace/comments, then
    `<!doctype html` or `<html`. Deliberately NOT a generic "contains a tag"
    check — a CSV with an angle bracket must stay a 415, not become a slide."""
    text = head[:4096]
    if text.startswith(b"\xef\xbb\xbf"):
        text = text[3:]
    try:
        t = text.decode("utf-8", errors="replace").lstrip().lower()
    except Exception:  # pragma: no cover - decode with replace never raises
        return False
    while t.startswith("<!--"):
        end = t.find("-->")
        if end < 0:
            return False
        t = t[end + 3:].lstrip()
    return t.startswith("<!doctype html") or t.startswith("<html")


def _sniff(head: bytes) -> tuple[str, str, str]:
    """(kind, ext, content_type) from the leading bytes, or 415.

    Nothing else is accepted. An HTML slide IS accepted (decks made in Claude),
    but it is a script: it is only ever rendered inside a sandboxed iframe and
    served with `Content-Security-Policy: sandbox`, so even a direct navigation
    runs it in an opaque origin — the dashboard-upload model, not an exception
    to it. SVG stays refused (scriptable, and image slides are trusted as
    images downstream)."""
    for magic, kind, ext, ctype in _MAGIC:
        if head.startswith(magic):
            return kind, ext, ctype
    if _looks_like_html(head):
        return PRES_HTML, "html", "text/html; charset=utf-8"
    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail="Only PNG, JPEG, PDF and HTML files can be added as slides",
    )


async def _read_capped(file: UploadFile) -> tuple[bytes, str, str, str]:
    """Read the upload with the cap that its OWN first bytes justify.

    The kind is decided from the first chunk, the cap follows from the kind, and
    the read aborts the moment the cap is exceeded — so an oversized body is
    never fully buffered and never touches the disk."""
    first = await file.read(_CHUNK)
    if not first:
        raise HTTPException(status_code=400, detail="Empty file")
    kind, ext, ctype = _sniff(first)
    cap = {PRES_IMAGE: _MAX_IMAGE, PRES_PDF: _MAX_PDF}.get(kind, _MAX_HTML)

    buf = bytearray(first)
    while len(buf) <= cap:
        chunk = await file.read(_CHUNK)
        if not chunk:
            return bytes(buf), kind, ext, ctype
        buf.extend(chunk)
    raise HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=f"{kind} slide too large (max {cap // (1024 * 1024)} MB)",
    )


# ---------------------------------------------------------------------------
# Presentations CRUD
# ---------------------------------------------------------------------------

@router.post("/api/presentations", status_code=201)
async def create_presentation(
    body: PresentationCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pres = Presentation(owner_id=user.id, title=body.title.strip() or "Presentation")
    db.add(pres)
    db.flush()
    _log(db, user, "presentation_create", "presentation", pres.id,
         {"title": pres.title}, request.client.host if request.client else "")
    db.commit()
    return {"id": pres.id}


@router.get("/api/presentations")
async def list_presentations(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Own presentations, newest first. The slide count comes from one grouped
    query, not one COUNT per row."""
    from sqlalchemy import func as sa_func

    rows = db.query(Presentation).filter(
        Presentation.owner_id == user.id
    ).order_by(Presentation.updated_at.desc()).all()
    counts = dict(
        db.query(PresentationItem.presentation_id, sa_func.count(PresentationItem.id))
        .filter(PresentationItem.presentation_id.in_([p.id for p in rows] or [""]))
        .group_by(PresentationItem.presentation_id).all()
    )
    return [{
        "id": p.id,
        "title": p.title,
        "items_count": counts.get(p.id, 0),
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    } for p in rows]


@router.get("/api/presentations/pickable")
async def pickable_dashboards(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reports this user may put on a slide, favourites first.

    Declared BEFORE `/api/presentations/{presentation_id}` on purpose: FastAPI
    matches in declaration order, so a literal segment that comes after a path
    parameter of the same shape is never reached.

    The access filter is the same WHERE the workspace home builds (owned OR
    public OR granted+published, administrators see everything) — the picker
    must not offer a report that `add_item`'s reach-check would then refuse.
    """
    from sqlalchemy import and_, or_, select

    from app.models import DashboardFavorite, DashboardPermission

    q = db.query(Dashboard.id, Dashboard.title, Dashboard.updated_at)
    if user.role != ROLE_ADMINISTRATOR:
        perm_sq = (
            select(DashboardPermission.dashboard_id)
            .where(DashboardPermission.user_id == user.id)
            .scalar_subquery()
        )
        q = q.filter(or_(
            Dashboard.owner_id == user.id,
            Dashboard.is_public == True,  # noqa: E712
            and_(Dashboard.id.in_(perm_sq), Dashboard.is_published == True),  # noqa: E712
        ))
    rows = q.order_by(Dashboard.updated_at.desc()).all()
    fav = {
        did: pos for did, pos in
        db.query(DashboardFavorite.dashboard_id, DashboardFavorite.position)
          .filter(DashboardFavorite.user_id == user.id).all()
    }
    items = [{"id": r[0], "title": r[1], "fav": fav.get(r[0])} for r in rows]
    # Favourites first, in the user's own order; everything else keeps the
    # recency order the query already produced.
    items.sort(key=lambda d: (d["fav"] is None, d["fav"] if d["fav"] is not None else 0))
    return items


@router.get("/api/presentations/{presentation_id}")
async def get_presentation(
    presentation_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pres = _own(db, user, presentation_id)
    return {
        "id": pres.id,
        "title": pres.title,
        "updated_at": pres.updated_at.isoformat() if pres.updated_at else None,
        "items": [_item_json(i, t) for i, t in _items_with_titles(db, pres)],
    }


@router.patch("/api/presentations/{presentation_id}")
async def update_presentation(
    presentation_id: str,
    body: PresentationUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pres = _own(db, user, presentation_id)
    pres.title = body.title.strip() or pres.title
    _log(db, user, "presentation_update", "presentation", pres.id,
         {"title": pres.title}, request.client.host if request.client else "")
    db.commit()
    return {"ok": True, "title": pres.title}


@router.delete("/api/presentations/{presentation_id}")
async def delete_presentation(
    presentation_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete the presentation, its slides and their files. The directory goes
    too — an emptied-out `_presentations/{id}/` left behind is litter that would
    accumulate one entry per deleted presentation forever."""
    pres = _own(db, user, presentation_id)
    items = db.query(PresentationItem).filter(
        PresentationItem.presentation_id == pres.id
    ).all()
    n_files = 0
    for item in items:
        if storage.delete_presentation_file(item.file_path):
            n_files += 1
    storage.delete_presentation_dir(pres.id)
    _log(db, user, "presentation_delete", "presentation", pres.id,
         {"title": pres.title, "items": len(items), "files": n_files},
         request.client.host if request.client else "")
    # The slide rows are also covered by the ORM's delete-orphan cascade; doing
    # it through integrity.detach keeps every parent-delete in the app on ONE
    # rule and is what makes the DELETE below safe under foreign_keys=ON no
    # matter what a later release hangs off a presentation.
    from app.services import integrity
    integrity.detach(db, "presentations", "id", pres.id)
    db.expire(pres)
    db.delete(pres)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Slides
# ---------------------------------------------------------------------------

def _next_position(db: Session, pres: Presentation) -> int:
    from sqlalchemy import func as sa_func
    top = db.query(sa_func.max(PresentationItem.position)).filter(
        PresentationItem.presentation_id == pres.id
    ).scalar()
    return 0 if top is None else int(top) + 1


@router.post("/api/presentations/{presentation_id}/items", status_code=201)
async def add_item(
    presentation_id: str,
    body: ItemCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add a dashboard slide. Image/PDF slides arrive through …/upload instead.

    The dashboard must pass the SAME access rule the viewer enforces: a
    presentation cannot become a side door to a report its owner may not open."""
    pres = _own(db, user, presentation_id)
    if body.kind != PRES_DASHBOARD:
        raise HTTPException(
            status_code=400,
            detail="Only kind=dashboard can be added here; use /upload for image and pdf",
        )
    if not body.dashboard_id:
        raise HTTPException(status_code=400, detail="dashboard_id is required")

    dash = db.query(Dashboard).filter(Dashboard.id == body.dashboard_id).first()
    if dash is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    # Plain call, not Depends — the dependency shape would re-resolve the
    # dashboard from the path, which is not where this id comes from.
    await require_dashboard_access(dashboard=dash, user=user, db=db)

    item = PresentationItem(
        presentation_id=pres.id,
        position=_next_position(db, pres),
        kind=PRES_DASHBOARD,
        dashboard_id=dash.id,
        note=body.note,
        start_section=body.start_section,
    )
    db.add(item)
    _touch(pres)
    db.flush()
    _log(db, user, "presentation_item_add", "presentation", pres.id,
         {"item_id": item.id, "kind": PRES_DASHBOARD, "dashboard_id": dash.id},
         request.client.host if request.client else "")
    db.commit()
    return _item_json(item, dash.title)


@router.post("/api/presentations/{presentation_id}/upload", status_code=201)
async def upload_item(
    presentation_id: str,
    request: Request,
    file: UploadFile = File(...),
    note: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add an image or PDF slide.

    Browser clients must send a **FormData** body: OriginGuard rejects a
    state-changing request that carries a body with no Content-Type (an untyped
    body is what a cross-origin CSRF write looks like), and FormData makes the
    browser set `multipart/form-data` itself. Do not hand-build the body.

    The file is sniffed by magic bytes and capped (5 MB image / 20 MB pdf)
    BEFORE anything is written; the declared content-type and the filename are
    never trusted, and the stored name is built from ids alone.
    """
    pres = _own(db, user, presentation_id)
    if note is not None and len(note) > _MAX_NOTE:
        raise HTTPException(status_code=422, detail=f"note must be ≤ {_MAX_NOTE} characters")

    data, kind, ext, ctype = await _read_capped(file)

    item = PresentationItem(
        presentation_id=pres.id,
        position=_next_position(db, pres),
        kind=kind,
        original_name=(file.filename or "")[:255] or None,
        content_type=ctype,
        size_bytes=len(data),
        note=note,
    )
    db.add(item)
    db.flush()  # mint the id the stored filename is built from
    item.file_path = storage.save_presentation_file(pres.id, item.id, ext, data)
    _touch(pres)
    _log(db, user, "presentation_item_upload", "presentation", pres.id,
         {"item_id": item.id, "kind": kind, "content_type": ctype,
          "size": len(data), "original_name": item.original_name},
         request.client.host if request.client else "")
    db.commit()
    return _item_json(item, None)


@router.put("/api/presentations/{presentation_id}/order")
async def reorder_items(
    presentation_id: str,
    body: OrderBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Renumber the owner's own slides in the order given. Ids that are not this
    presentation's are ignored; slides the body omits keep their relative order
    after the listed ones."""
    pres = _own(db, user, presentation_id)
    items = db.query(PresentationItem).filter(
        PresentationItem.presentation_id == pres.id
    ).order_by(PresentationItem.position, PresentationItem.created_at).all()
    by_id = {i.id: i for i in items}

    pos = 0
    seen: set[str] = set()
    for iid in body.ids:
        item = by_id.get(iid)
        if item is None or iid in seen:
            continue
        item.position = pos
        seen.add(iid)
        pos += 1
    for item in items:
        if item.id not in seen:
            item.position = pos
            pos += 1

    _touch(pres)
    _log(db, user, "presentation_reorder", "presentation", pres.id,
         {"count": len(seen)}, request.client.host if request.client else "")
    db.commit()
    return {"ok": True, "items": [_item_json(i, t) for i, t in _items_with_titles(db, pres)]}


@router.patch("/api/presentations/{presentation_id}/items/{item_id}")
async def update_item(
    presentation_id: str,
    item_id: str,
    body: ItemUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pres = _own(db, user, presentation_id)
    item = _own_item(db, pres, item_id)
    if "note" in body.model_fields_set:
        item.note = body.note
    if "start_section" in body.model_fields_set:
        item.start_section = body.start_section
    _touch(pres)
    _log(db, user, "presentation_item_update", "presentation", pres.id,
         {"item_id": item.id, "fields": sorted(body.model_fields_set)},
         request.client.host if request.client else "")
    db.commit()
    return _item_json(item, None)


@router.delete("/api/presentations/{presentation_id}/items/{item_id}")
async def delete_item(
    presentation_id: str,
    item_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pres = _own(db, user, presentation_id)
    item = _own_item(db, pres, item_id)
    removed = storage.delete_presentation_file(item.file_path)
    _log(db, user, "presentation_item_delete", "presentation", pres.id,
         {"item_id": item.id, "kind": item.kind, "file_removed": removed},
         request.client.host if request.client else "")
    db.delete(item)
    _touch(pres)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Slide file delivery (page-level route, owner only)
# ---------------------------------------------------------------------------

@router.get("/presentations/{presentation_id}/file/{item_id}")
def get_item_file(
    presentation_id: str,
    item_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Serve a slide's stored bytes to its owner.

    Sync `def` on purpose: the stat + read below are disk I/O, so Starlette runs
    the whole handler in the threadpool (repo convention — get_slot_file,
    download_run_aux).

    The filename in Content-Disposition is the item id plus its extension, never
    the uploader's own name; `nosniff` keeps the browser on the stored type, and
    `no-store` keeps a private slide out of shared caches.
    """
    pres = db.query(Presentation).filter(
        Presentation.id == presentation_id,
        Presentation.owner_id == user.id,
    ).first()
    if pres is None:
        raise HTTPException(status_code=404, detail="Presentation not found")
    item = db.query(PresentationItem).filter(
        PresentationItem.id == item_id,
        PresentationItem.presentation_id == pres.id,
    ).first()
    if item is None or not item.file_path:
        raise HTTPException(status_code=404, detail="Slide file not found")
    path = storage.presentation_file_path(item.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Slide file missing on disk")
    headers = {
            "Content-Disposition": f'inline; filename="{path.name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            # PNG/JPEG/PDF are already compressed: an explicit identity
            # encoding keeps GZipMiddleware from gzipping 20 MB on the event
            # loop for zero wire gain («No compression on the event loop, ever»).
            "Content-Encoding": "identity",
    }
    if item.kind == PRES_HTML:
        # The stored file is user-authored script. CSP `sandbox` gives the
        # document an opaque origin even when the URL is opened directly, so
        # it can never read our cookies or DOM — the same isolation the slide
        # page's <iframe sandbox> provides, enforced by the response itself.
        headers["Content-Security-Policy"] = "sandbox allow-scripts"
    return FileResponse(
        path,
        media_type=item.content_type or "application/octet-stream",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Pre-flight check: is every slide still worth showing?
# ---------------------------------------------------------------------------

def _data_at(db: Session, dashboard_id: str) -> datetime | None:
    """Newest input file the report was last built from — the same number the
    workspace home shows ("data as of"): MAX(SourceObservation.newest_mtime)
    over the dashboard's runs (lineage v2). Runs made before observations
    existed fall back to the run's own `input_manifest` (`files[].mtime_ms`),
    then to the run timestamp."""
    newest = (db.query(func.max(SourceObservation.newest_mtime))
                .join(GenerationRun, SourceObservation.run_id == GenerationRun.id)
                .filter(GenerationRun.dashboard_id == dashboard_id)
                .scalar())
    if newest is not None:
        return newest
    run = db.query(GenerationRun).filter(
        GenerationRun.dashboard_id == dashboard_id,
        GenerationRun.status == RUN_SUCCESS,
    # id breaks the tie: two runs can share a started_at second, and "latest"
    # then has to mean the one written last, not whichever the planner returns.
    ).order_by(GenerationRun.started_at.desc(), GenerationRun.id.desc()).first()
    if run is None:
        return None
    try:
        obj = json.loads(run.input_manifest) if run.input_manifest else {}
    except (json.JSONDecodeError, ValueError):
        obj = {}
    newest_ms = 0
    for f in (obj.get("files") or []) if isinstance(obj, dict) else []:
        if isinstance(f, dict) and isinstance(f.get("mtime_ms"), (int, float)):
            newest_ms = max(newest_ms, int(f["mtime_ms"]))
    if newest_ms:
        return datetime.utcfromtimestamp(newest_ms / 1000.0)
    return run.finished_at or run.started_at


def _freshness(db: Session, dash: Dashboard, now: datetime | None = None) -> dict:
    """When this report was published, how fresh its data is, and whether that
    is stale — ONE rule, shared by `/check` and the show-mode bar so the two
    can never disagree about the same slide.

    A generator-backed report that has never observed any data is stale by
    definition; a hand-uploaded one has no data date and is never stale."""
    now = now or datetime.utcnow()
    data_at = _data_at(db, dash.id)
    if data_at is None:
        stale = dash.generator_id is not None
    else:
        stale = (now - data_at) > _STALE_AFTER
    return {
        "pub_at": dash.updated_at.date().isoformat() if dash.updated_at else None,
        "data_at": data_at.date().isoformat() if data_at else None,
        "pub_dm": dash.updated_at.strftime("%d.%m") if dash.updated_at else None,
        "data_dm": data_at.strftime("%d.%m") if data_at else None,
        "stale": stale,
    }


@router.get("/api/presentations/{presentation_id}/check")
async def check_presentation(
    presentation_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Per slide: when the report was published, how fresh its data is, and
    whether that is stale. A slide whose dashboard the owner can no longer open
    is reported as `{"access": false}` — the answer to "will this presentation
    work when I stand up to give it", asked before the room is watching."""
    pres = _own(db, user, presentation_id)
    now = datetime.utcnow()
    out = []
    for item, dash_title in _items_with_titles(db, pres):
        if item.kind != PRES_DASHBOARD:
            out.append({
                "position": item.position, "kind": item.kind,
                "title": item.original_name or item.kind,
                "pub_at": None, "data_at": None, "stale": False,
            })
            continue
        dash = db.query(Dashboard).filter(Dashboard.id == item.dashboard_id).first()
        if dash is None or not await _may_show(db, user, dash):
            out.append({
                "position": item.position, "kind": item.kind,
                "title": dash_title, "access": False,
            })
            continue
        fresh = _freshness(db, dash, now)
        out.append({
            "position": item.position,
            "kind": item.kind,
            "title": dash.title,
            "pub_at": fresh["pub_at"],
            "data_at": fresh["data_at"],
            "stale": fresh["stale"],
        })
    return {"id": pres.id, "slides": out}


# ---------------------------------------------------------------------------
# Show mode context (consumed by the show template in phase 6.4)
# ---------------------------------------------------------------------------

def show_context(db: Session, user: User, presentation_id: str, i: int) -> dict | None:
    """Everything the show template needs for slide `i` (0-based), or None when
    this user may not show this presentation (v1: owner only) or `i` is out of
    range.

    A dashboard slide is not re-rendered here: its url is the report's OWN
    viewer url, carrying `pres`/`i` so the viewer can draw the show chrome.
    """
    pres = db.query(Presentation).filter(
        Presentation.id == presentation_id,
        Presentation.owner_id == (user.id if user else None),
    ).first()
    if pres is None:
        return None
    pairs = _items_with_titles(db, pres)
    n = len(pairs)
    if n == 0 or i < 0 or i >= n:
        return None

    def url_for(k: int) -> str:
        item, _t = pairs[k]
        if item.kind == PRES_DASHBOARD and item.dashboard_id:
            return f"/d/{item.dashboard_id}?pres={pres.id}&i={k}"
        return f"/presentations/{pres.id}/slide/{k}"

    item, dash_title = pairs[i]
    # Freshness of THIS slide only — the bar answers "is what the room is
    # looking at current?", which is a question about one report, not about the
    # whole deck (that is what "Verify before show" is for).
    fresh = None
    if item.kind == PRES_DASHBOARD and item.dashboard_id:
        dash = db.query(Dashboard).filter(Dashboard.id == item.dashboard_id).first()
        if dash is not None:
            fresh = _freshness(db, dash)
    return {
        "id": pres.id,
        "title": pres.title,
        "n": n,
        "i": i,
        "prev_url": url_for(i - 1) if i > 0 else None,
        "next_url": url_for(i + 1) if i < n - 1 else None,
        "item": _item_json(item, dash_title),
        "freshness": fresh,
        "builder_url": f"/presentations/{pres.id}",
    }


# ---------------------------------------------------------------------------
# Pages: the builder, the deck list, and the show-mode slide for image/pdf
# ---------------------------------------------------------------------------

@router.get("/presentations", response_class=HTMLResponse)
async def presentations_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The owner's decks. Two queries: the rows, and one grouped slide count —
    never one COUNT per deck."""
    rows = db.query(Presentation).filter(
        Presentation.owner_id == user.id
    ).order_by(Presentation.updated_at.desc()).all()
    counts = dict(
        db.query(PresentationItem.presentation_id, func.count(PresentationItem.id))
        .filter(PresentationItem.presentation_id.in_([p.id for p in rows] or [""]))
        .group_by(PresentationItem.presentation_id).all()
    )
    items = [{
        "id": p.id,
        "title": p.title,
        "items_count": counts.get(p.id, 0),
        "updated_at": p.updated_at.strftime("%d.%m.%Y") if p.updated_at else "",
    } for p in rows]
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(
        env.get_template("pages/presentations.html").render,
        user=user, request=request, presentations=items,
    ))


@router.get("/presentations/{presentation_id}", response_class=HTMLResponse)
async def presentation_page(
    presentation_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The builder. Owner only — `_own` answers 404 for everyone else, so the
    page leaks no more than the API does."""
    pres = _own(db, user, presentation_id)
    payload = {
        "id": pres.id,
        "title": pres.title,
        "items": [_item_json(i, t) for i, t in _items_with_titles(db, pres)],
    }
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(
        env.get_template("pages/presentation.html").render,
        user=user, request=request, presentation=pres, pres_payload=payload,
    ))


@router.get("/presentations/{presentation_id}/show", response_class=HTMLResponse)
async def presentation_show_page(
    presentation_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    i: int = Query(0, ge=0),
):
    """The show SHELL: one page that stays put while slides change inside its
    iframe — fullscreen entered on this document survives "Next", which a
    top-level navigation per slide could never do. The slides themselves are
    the same pages as before (the report's own viewer, the image/pdf page)."""
    ctx = show_context(db, user, presentation_id, i)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Presentation not found")
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(
        env.get_template("pages/presentation_show.html").render,
        user=user, request=request, pres_ctx=ctx,
        first_url=(f"/d/{ctx['item']['dashboard_id']}?pres={ctx['id']}&i={i}"
                   if ctx["item"]["kind"] == PRES_DASHBOARD and ctx["item"].get("dashboard_id")
                   else f"/presentations/{ctx['id']}/slide/{i}"),
    ))


@router.get("/presentations/{presentation_id}/slide/{index}", response_class=HTMLResponse)
async def presentation_slide_page(
    presentation_id: str,
    index: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Show mode for an image/pdf slide.

    A dashboard slide is never rendered here — it is the report's own viewer,
    so this route redirects to it rather than growing a second way to show a
    report."""
    ctx = show_context(db, user, presentation_id, index)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Slide not found")
    item = ctx["item"]
    if item["kind"] == PRES_DASHBOARD and item["dashboard_id"]:
        return RedirectResponse(
            f"/d/{item['dashboard_id']}?pres={presentation_id}&i={index}", status_code=307
        )
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(
        env.get_template("pages/presentation_slide.html").render,
        user=user, request=request, pres_ctx=ctx,
    ))
