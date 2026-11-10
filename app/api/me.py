"""Per-account preferences of the signed-in user. Today: interface language.

Kept apart from /api/admin/users/{id} on purpose — this endpoint edits the
CALLER's row and needs no `manage_users`, so a visitor can switch language.

The write is rare (a language change is a deliberate, occasional act), which is
why the client simply reloads afterwards instead of re-labelling the DOM.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from jinja2 import Environment
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import i18n
from app.i18n import _
from app.auth.deps import get_current_user
from app.config import settings
from app.database import get_db
from app.models import AuditLog, User

router = APIRouter()


class LangIn(BaseModel):
    lang: str


def set_lang_cookie(response: Response, lang: str) -> None:
    """Mirror the choice into `op_lang` so the very next request — including the
    anonymous pages that never see `users.lang` — is already in that language.
    Not HttpOnly: the /login switcher writes it from JS, and a UI preference is
    nothing to guard. SameSite/Secure follow the session cookie."""
    secure = settings.saml_https
    response.set_cookie(
        i18n.COOKIE_NAME, lang,
        samesite="none" if secure else "lax",
        secure=secure,
        max_age=i18n.COOKIE_MAX_AGE,
    )


@router.get("/api/me")
async def read_me(user: User = Depends(get_current_user)):
    return {
        "person_id": user.person_id,
        "display_name": user.display_name,
        "role": user.role,
        "lang": user.lang or i18n.current_lang(),
    }


@router.put("/api/me/lang")
async def set_my_lang(
    body: LangIn,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.lang not in i18n.LANGS:
        raise HTTPException(status_code=400, detail=i18n.gettext("Unknown UI language"))

    # `user` here is the detached row auth builds; the update goes through the
    # request session like every other write.
    row = db.query(User).filter(User.id == user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail=i18n.gettext("User not found"))
    row.lang = body.lang
    entry = AuditLog(actor_id=user.id, action="set_lang", resource_type="user",
                     resource_id=user.person_id,
                     ip_address=request.client.host if request.client else "")
    # Audit detail is a log, never UI — it stays untranslated by design.
    entry.set_detail({"lang": body.lang})
    db.add(entry)
    db.commit()

    set_lang_cookie(response, body.lang)
    return {"ok": True, "lang": body.lang}


@router.get("/me", response_class=HTMLResponse)
async def account_page(request: Request, user: User = Depends(get_current_user)):
    """Personal page: for now it only holds the keys for the desktop program.

    A separate page rather than a block in the admin panel: a person issues a key
    to THEMSELVES, and admin rights have nothing to do with it. Rendered through
    the threadpool like every page in the project — Jinja is CPU work under the GIL.
    """
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(
        env.get_template("pages/account.html").render,
        user=user, request=request,
    ))


# ---------------------------------------------------------------------------
# Device keys: how the desktop program reads the platform
# ---------------------------------------------------------------------------
# A person issues them to THEMSELVES — no separate right is needed, and there must
# not be one: a key widens access by not a single byte, it only lets you use what
# you already have without a browser. Hence the handlers live here, next to language, not in admin.
#
# These handlers are non-GET, so the key itself cannot call them (`deps.py` refuses
# it on every method except GET and HEAD). That is, a key cannot issue a second
# key and cannot extend its own access: that always takes a live sign-in.

class DeviceTokenIn(BaseModel):
    label: str = ""


@router.get("/api/me/device-tokens")
async def list_device_tokens(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.auth import devicetoken
    return {"items": [{
        "id": t.id,
        "label": t.label,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "expires_at": t.expires_at.isoformat() if t.expires_at else None,
        "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
    } for t in devicetoken.active_for(db, user)],
        "max_per_user": devicetoken.MAX_PER_USER,
        "ttl_days": devicetoken.DEFAULT_TTL_DAYS}


@router.post("/api/me/device-tokens")
async def create_device_token(
    body: DeviceTokenIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.auth import devicetoken

    live = devicetoken.active_for(db, user)
    if len(live) >= devicetoken.MAX_PER_USER:
        # The cap is not about security but about keeping the key list
        # manageable: you cannot revoke what you cannot make sense of.
        raise HTTPException(status_code=409, detail=_(
            "You already have {p1} keys — revoke one before issuing another.",
            p1=devicetoken.MAX_PER_USER))

    raw, row = devicetoken.issue(db, user, label=body.label)
    db.flush()
    entry = AuditLog(
        actor_id=user.id, action="device_token_issue",
        resource_type="device_token", resource_id=str(row.id),
        ip_address=(request.client.host if request.client else ""),
    )
    # Only the label and the expiry go to the audit. Neither the key nor its hash:
    # the hash is irreversible, but it is a verifier, and it has no place in a log.
    entry.set_detail({"label": row.label, "expires_at": row.expires_at.isoformat()})
    db.add(entry)
    db.commit()
    # The plaintext is returned RIGHT HERE and nowhere else: it cannot be recovered
    # from the database by construction, and the person must be told so on screen.
    return {"ok": True, "id": row.id, "token": raw,
            "expires_at": row.expires_at.isoformat()}


@router.delete("/api/me/device-tokens/{token_id}")
async def delete_device_token(
    token_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.auth import devicetoken

    if not devicetoken.revoke(db, user, token_id):
        # Someone else's or an already revoked key — 404, not 403: it is not our
        # business to disclose that other people's keys exist.
        raise HTTPException(status_code=404, detail="Not found")
    entry = AuditLog(
        actor_id=user.id, action="device_token_revoke",
        resource_type="device_token", resource_id=str(token_id),
        ip_address=(request.client.host if request.client else ""),
    )
    entry.set_detail({})
    db.add(entry)
    db.commit()
    return {"ok": True}
