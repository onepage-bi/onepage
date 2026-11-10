"""
Folder CRUD — shared, single-level dashboard folders.
Create/rename/delete: superuser + administrator (upload_dashboard permission).
Moving a dashboard into a folder rides on PATCH /api/dashboards/{id} (folder_id).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.deps import require_permission
from app.database import get_db
from app.models import Dashboard, Folder, User

router = APIRouter()

_MAX_NAME = 100


class FolderCreate(BaseModel):
    name: str


class FolderUpdate(BaseModel):
    name: str | None = None
    position: int | None = None


def _log(db: Session, actor: User, action: str, folder_id: int, detail: dict) -> None:
    from app.models import AuditLog
    entry = AuditLog(actor_id=actor.id, action=action,
                     resource_type="folder", resource_id=str(folder_id))
    entry.set_detail(detail)
    db.add(entry)


def _clean_name(raw: str, db: Session, exclude_id: int | None = None) -> str:
    name = " ".join(raw.split())
    if not name:
        raise HTTPException(status_code=400, detail="Folder name required")
    if len(name) > _MAX_NAME:
        raise HTTPException(status_code=400, detail=f"Folder name too long (max {_MAX_NAME})")
    q = db.query(Folder).filter(Folder.name == name)
    if exclude_id is not None:
        q = q.filter(Folder.id != exclude_id)
    if q.first():
        raise HTTPException(status_code=409, detail="Folder with this name already exists")
    return name


@router.post("/api/folders")
async def create_folder(
    body: FolderCreate,
    user: User = Depends(require_permission("upload_dashboard")),
    db: Session = Depends(get_db),
):
    name = _clean_name(body.name, db)
    max_pos = db.query(func.max(Folder.position)).scalar() or 0
    folder = Folder(name=name, position=max_pos + 1, created_by=user.id)
    db.add(folder)
    db.flush()
    _log(db, user, "create_folder", folder.id, {"name": name})
    db.commit()
    return {"ok": True, "id": folder.id, "name": folder.name}


@router.patch("/api/folders/{folder_id}")
async def update_folder(
    folder_id: int,
    body: FolderUpdate,
    user: User = Depends(require_permission("upload_dashboard")),
    db: Session = Depends(get_db),
):
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    if body.name is not None:
        folder.name = _clean_name(body.name, db, exclude_id=folder_id)
    if body.position is not None:
        folder.position = body.position
    _log(db, user, "update_folder", folder_id, {"name": body.name, "position": body.position})
    db.commit()
    return {"ok": True}


@router.delete("/api/folders/{folder_id}")
async def delete_folder(
    folder_id: int,
    user: User = Depends(require_permission("upload_dashboard")),
    db: Session = Depends(get_db),
):
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    # Contained dashboards fall back to the root ("Unsorted") section. This is
    # also the whole FK story of a folder — `dashboards.folder_id` is the only
    # column pointing at `folders.id`, and it is nullable — so the DELETE below
    # is already safe under foreign_keys=ON without an integrity.detach() call.
    moved = db.query(Dashboard).filter(Dashboard.folder_id == folder_id) \
        .update({Dashboard.folder_id: None})
    _log(db, user, "delete_folder", folder_id, {"name": folder.name, "dashboards_moved": moved})
    db.delete(folder)
    db.commit()
    return {"ok": True, "dashboards_moved": moved}
