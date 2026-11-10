from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.config import settings
from app.database import get_db
from app.models import DASH_PAGE, AuditLog, Dashboard, ShareToken, User, ROLE_ADMINISTRATOR
from app.services import detector, encoding, perflog, storage

router = APIRouter()


def _log(db: Session, actor: User, action: str, resource_type: str, resource_id: str, detail: dict, ip: str = "") -> None:
    entry = AuditLog(
        actor_id=actor.id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        ip_address=ip,
    )
    entry.set_detail(detail)
    db.add(entry)


class ShareLinkCreate(BaseModel):
    expires_in_days: Optional[int] = None
    label: Optional[str] = None


def _require_manage(dashboard: Dashboard, user: User) -> None:
    if user.role != ROLE_ADMINISTRATOR and dashboard.owner_id != user.id:
        raise HTTPException(status_code=403)


def _require_share(dashboard: Dashboard, user: User, db: Session) -> None:
    """Share links follow the «can update data» right (owner decision
    2026-09-04): a colleague trusted to refresh a report may also hand out a
    read-only link to it. Owner and administrator as before.

    ADR-18: an RLS report never has share links at all, including for the owner and
    the administrator — a link is anonymous, there is nobody to filter it for, and one
    forwarded url would cancel all of row-level security."""
    if dashboard.rls_enabled:
        raise HTTPException(status_code=403,
                            detail="RLS: share links are disabled for this report")
    # "Highly confidential": a link is anonymous, there is nobody to present the password,
    # and one forwarded url would cancel the whole second lock. Refused for everyone,
    # including the owner and the administrator, exactly as with RLS above.
    if dashboard.confidential:
        raise HTTPException(status_code=403,
                            detail="confidential: share links are disabled for this report")
    if user.role == ROLE_ADMINISTRATOR or dashboard.owner_id == user.id:
        return
    from app.models import DashboardPermission

    perm = db.query(DashboardPermission).filter(
        DashboardPermission.dashboard_id == dashboard.id,
        DashboardPermission.user_id == user.id,
    ).first()
    if not (perm and perm.can_update_data):
        raise HTTPException(status_code=403)


def _get_dash_or_404(dashboard_id: str, db: Session) -> Dashboard:
    dash = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dash:
        raise HTTPException(status_code=404)
    return dash


def _validate_token(token: str, db: Session) -> tuple[ShareToken, Dashboard]:
    tok = db.query(ShareToken).filter(ShareToken.token == token).first()
    if not tok:
        raise HTTPException(status_code=404, detail="Share link not found")
    if tok.expires_at and tok.expires_at < datetime.utcnow():
        raise HTTPException(status_code=410, detail="Share link expired")
    dash = db.query(Dashboard).filter(Dashboard.id == tok.dashboard_id).first()
    if not dash:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    # ADR-18: the policy was enabled after the link was issued — existing tokens
    # stop working right here, before any disk read (fail closed).
    if dash.rls_enabled:
        raise HTTPException(status_code=403,
                            detail="RLS: share links are disabled for this report")
    # The mode was enabled after the link was issued — issued tokens die right here,
    # before any disk read (fail closed).
    if dash.confidential:
        raise HTTPException(status_code=403,
                            detail="confidential: share links are disabled for this report")
    return tok, dash


def _human_expires(expires_at: datetime | None) -> str | None:
    if not expires_at:
        return None
    delta = expires_at - datetime.utcnow()
    if delta.total_seconds() <= 0:
        return "expired"
    days = delta.days
    if days > 0:
        return f"{days} day{'s' if days != 1 else ''}"
    hours = delta.seconds // 3600
    if hours > 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return "< 1 hour"


# Render/asset machinery shared with the owner path (files.py): MIME map,
# render cell cache + single-flight, slot-asset serving. One-way import —
# files.py never imports share.py.
from app.api import files as files_api  # noqa: E402
from app.api.files import _mime  # noqa: E402


# ---------------------------------------------------------------------------
# Public share viewer (no auth required)
# ---------------------------------------------------------------------------

@router.get("/s/{token}", response_class=HTMLResponse)
async def share_view(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    tok = db.query(ShareToken).filter(ShareToken.token == token).first()
    if not tok:
        return _share_error("Link not found", "This share link does not exist or has been revoked.", 404)

    if tok.expires_at and tok.expires_at < datetime.utcnow():
        return _share_error("Link expired", "This share link has expired.", 410)

    dash = db.query(Dashboard).filter(Dashboard.id == tok.dashboard_id).first()
    if not dash:
        return _share_error("Dashboard not found", "The dashboard this link pointed to no longer exists.", 404)
    if dash.rls_enabled:
        return _share_error(
            "Link disabled",
            "This report is filtered per viewer, so it cannot be shared by link. Open it from OnePage instead.",
            403)
    if dash.confidential:
        return _share_error(
            "Link disabled",
            "This report is marked highly confidential and opens only with a password inside OnePage.",
            403)

    slots = storage.list_slot_files(dash.id, dash.current_version)
    slots_json = json.dumps(slots)

    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(env.get_template("pages/share.html").render,
        dashboard=dash,
        token=token,
        expires_in=_human_expires(tok.expires_at),
        slots=slots,
        slots_json=slots_json,
    ))


def _share_error(title: str, message: str, status: int) -> HTMLResponse:
    html = f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>{title} — OnePage</title>
  <link rel="stylesheet" href="/static/css/main.css">
  <style>
    body {{ display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
    .err-box {{ text-align:center; max-width:360px; }}
    .err-box h2 {{ margin:0 0 10px; font-size:18px; color:var(--ink); }}
    .err-box p  {{ margin:0; font-size:13px; color:var(--ink-muted); }}
    .err-box a  {{ display:inline-block; margin-top:20px; }}
  </style>
</head>
<body>
  <div class="err-box">
    <h2>{title}</h2>
    <p>{message}</p>
    <a href="/login" class="btn">Sign in</a>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=status)


# ---------------------------------------------------------------------------
# Public render endpoints for share tokens (iframe src in share.html)
# ---------------------------------------------------------------------------

def _build_share_body(dash: Dashboard) -> bytes:
    """Injected share document (no owner-only controllers). Blocking; may raise
    FileNotFoundError."""
    html_bytes = storage.read_html(dash.id, dash.current_version)
    html = html_bytes.decode("utf-8", errors="replace")

    if dash.dash_type == "snapshot":
        snap = storage.read_snapshot_json(dash.id, dash.current_version)
        if snap:
            externalized = False
            if settings.snapshot_external:
                fp = files_api.snapshot_fp12(storage.version_fingerprint(dash.id, dash.current_version))
                html, externalized = detector.externalize_snapshot(html, f"snapshot.js?v={fp}")
            if not externalized:
                html = detector.inject_snapshot(html, snap.decode("utf-8"))
    elif dash.dash_type == "interactive":
        slots = storage.list_slot_files(dash.id, dash.current_version)
        html = detector.inject_bootstrap(html, json.dumps(slots))
    elif dash.dash_type == DASH_PAGE:
        # Navigator: the same link bridge as the owner path (files.py). Without
        # it a link from the share page opens a sandboxed tab without cookies —
        # the Power BI white screen; the share.html shell receives `op_open`.
        html = detector.inject_link_bridge(html)

    return html.encode("utf-8")


# Same cell cache + single-flight as the owner render path: the share body is
# user-independent per (dashboard, version, fingerprint), so N visitors of a
# posted link cost ONE build + one encode per coding — not N full rebuilds.
# The "share" discriminator keeps these cells apart from the owner document
# (which additionally injects annotate/theme/textedit controllers).
@router.get("/share/{token}/render/")
def share_render(request: Request, token: str, db: Session = Depends(get_db)):
    _, dash = _validate_token(token, db)
    key = (dash.id, dash.current_version,
           storage.version_fingerprint(dash.id, dash.current_version), "share")

    cell = files_api.render_cache.get(key)
    if cell is None:
        with files_api.build_lock(key):
            cell = files_api.render_cache.get(key)
            if cell is None:
                try:
                    body = _build_share_body(dash)
                except FileNotFoundError:
                    raise HTTPException(status_code=404, detail="Dashboard file not found")
                compressible = encoding.is_compressible(body)
                files_api.render_cache.put_raw(key, body, compressible)
                cell = files_api.render_cache.get(key) or {"raw": body, "_c": compressible}
    else:
        rec = perflog.request_record.get()
        if rec is not None:
            rec.setdefault("stages", {})["cache_hit"] = 1

    return files_api.respond_from_cache(cell, key, request.headers.get("accept-encoding", ""))


# Same serving helper as the owner path (stat ETag/304, pre-compressed
# passthrough with decode fallback, raw slots compressed off the event loop).
@router.get("/share/{token}/files/{slot_index}")
def share_get_slot(request: Request, token: str, slot_index: int, db: Session = Depends(get_db)):
    _, dash = _validate_token(token, db)
    return files_api.serve_slot_download(request, dash.id, dash.current_version, slot_index)


# Content-addressed snapshot data for share views (OP_SNAPSHOT_EXTERNAL) —
# registered BEFORE the {asset_name} catch-all below; token is the only auth.
@router.get("/share/{token}/render/snapshot.js")
def share_snapshot_js(request: Request, token: str, v: str = "", db: Session = Depends(get_db)):
    _, dash = _validate_token(token, db)
    snap = storage.read_snapshot_json(dash.id, dash.current_version)
    if snap is None:
        return share_render_asset(request, token, "snapshot.js", db)
    current = files_api.snapshot_fp12(storage.version_fingerprint(dash.id, dash.current_version))
    cache = ("private, max-age=31536000, immutable" if v == current
             else "no-store, must-revalidate")
    return encoding.encoded_response(
        files_api.snapshot_js_body(snap), request.headers.get("accept-encoding", ""),
        media_type="text/javascript; charset=utf-8",
        headers={"Cache-Control": cache},
    )


# Relative-asset fallback for share views (same serving helper as
# files.render_asset — ETag/304, Accept-Encoding-aware .gz/.br fallbacks;
# the share document lives at /share/{token}/render/, token is the only auth).
# Registered after /share/{token}/files/{slot_index}.
@router.get("/share/{token}/render/{asset_name}")
def share_render_asset(request: Request, token: str, asset_name: str, db: Session = Depends(get_db)):
    _, dash = _validate_token(token, db)
    return files_api.serve_slot_asset(request, dash.id, dash.current_version, asset_name)


# ---------------------------------------------------------------------------
# Token management API  (can_manage only) — /share-tokens (original)
# ---------------------------------------------------------------------------

@router.post("/api/dashboards/{dashboard_id}/share-tokens")
async def create_share_token(
    dashboard_id: str,
    request: Request,
    body: ShareLinkCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dash = _get_dash_or_404(dashboard_id, db)
    _require_share(dash, user, db)

    expires_at = (datetime.utcnow() + timedelta(days=body.expires_in_days)) if body.expires_in_days else None
    label = body.label.strip() if body.label and body.label.strip() else None

    tok = ShareToken(
        token=str(uuid.uuid4()),
        dashboard_id=dashboard_id,
        created_by=user.id,
        expires_at=expires_at,
        label=label,
    )
    db.add(tok)
    # Share-link audit: the "share" quest is verified by this record
    # (app/services/quests.py). Only the token prefix goes into the detail —
    # the full token grants access to the report and must not end up in the log.
    _log(db, user, "share_link_create", "dashboard", dashboard_id,
         {"token_id": tok.token[:8], "label": tok.label,
          "expires_in_days": body.expires_in_days},
         request.client.host if request.client else "")
    db.commit()

    base_url = str(request.base_url).rstrip('/')
    return {
        "token": tok.token,
        "url": f"{base_url}/s/{tok.token}",
        "expires_at": tok.expires_at.isoformat() if tok.expires_at else None,
        "label": tok.label,
    }


@router.get("/api/dashboards/{dashboard_id}/share-tokens")
async def list_share_tokens(
    dashboard_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dash = _get_dash_or_404(dashboard_id, db)
    _require_share(dash, user, db)

    tokens = (
        db.query(ShareToken)
        .filter(ShareToken.dashboard_id == dashboard_id)
        .order_by(ShareToken.created_at.desc())
        .all()
    )
    now = datetime.utcnow()
    return [
        {
            "token": t.token,
            "label": t.label,
            "created_at": t.created_at.isoformat(),
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            "expired": bool(t.expires_at and t.expires_at < now),
        }
        for t in tokens
    ]


@router.delete("/api/dashboards/{dashboard_id}/share-tokens/{token}")
async def revoke_share_token(
    dashboard_id: str,
    token: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dash = _get_dash_or_404(dashboard_id, db)
    _require_share(dash, user, db)

    tok = db.query(ShareToken).filter(
        ShareToken.token == token,
        ShareToken.dashboard_id == dashboard_id,
    ).first()
    if tok:
        db.delete(tok)
        db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Share-links aliases — same data, format expected by viewer.js
# ---------------------------------------------------------------------------

@router.post("/api/dashboards/{dashboard_id}/share-links")
async def create_share_link(
    dashboard_id: str,
    request: Request,
    body: ShareLinkCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dash = _get_dash_or_404(dashboard_id, db)
    _require_share(dash, user, db)

    expires_at = (datetime.utcnow() + timedelta(days=body.expires_in_days)) if body.expires_in_days else None
    label = body.label.strip() if body.label and body.label.strip() else None

    tok = ShareToken(
        token=str(uuid.uuid4()),
        dashboard_id=dashboard_id,
        created_by=user.id,
        expires_at=expires_at,
        label=label,
    )
    db.add(tok)
    _log(db, user, "share_link_create", "dashboard", dashboard_id,
         {"token_id": tok.token[:8], "label": tok.label,
          "expires_in_days": body.expires_in_days},
         request.client.host if request.client else "")
    db.commit()

    return {
        "id": tok.token,
        "url": f"/s/{tok.token}",
        "label": tok.label,
        "expires_in": _human_expires(tok.expires_at),
    }


@router.get("/api/dashboards/{dashboard_id}/share-links")
async def list_share_links(
    dashboard_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dash = _get_dash_or_404(dashboard_id, db)
    _require_share(dash, user, db)

    tokens = (
        db.query(ShareToken)
        .filter(ShareToken.dashboard_id == dashboard_id)
        .order_by(ShareToken.created_at.desc())
        .all()
    )
    now = datetime.utcnow()
    return [
        {
            "id": t.token,
            "url": f"/s/{t.token}",
            "label": t.label,
            "expires_in": _human_expires(t.expires_at) if not (t.expires_at and t.expires_at < now) else "expired",
        }
        for t in tokens
    ]


@router.delete("/api/dashboards/{dashboard_id}/share-links/{link_id}")
async def revoke_share_link(
    dashboard_id: str,
    link_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dash = _get_dash_or_404(dashboard_id, db)
    _require_share(dash, user, db)

    tok = db.query(ShareToken).filter(
        ShareToken.token == link_id,
        ShareToken.dashboard_id == dashboard_id,
    ).first()
    if tok:
        db.delete(tok)
        db.commit()
    return {"ok": True}
