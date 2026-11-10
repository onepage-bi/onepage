from __future__ import annotations

import asyncio
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment
from sqlalchemy.orm import Session

from app.auth import local as local_auth
from app.auth import oidc
from app.auth.deps import get_current_user_optional
from app.auth.saml import get_saml_redirect_url, get_sp_metadata, process_saml_response
from app import i18n
from app.i18n import _
from app.api.me import set_lang_cookie
from app.auth.session import COOKIE_NAME, session_store
from app.config import settings
from app.database import get_db
from app.models import AuditLog, User, ROLE_VISITOR

router = APIRouter()

# Holds the id of the AuthnRequest between /saml/login and /saml/acs. Short
# lived: it is only meant to survive one redirect out to ADFS and back.
SAML_REQUEST_COOKIE = "op_saml_req"
_SAML_REQUEST_TTL_S = 10 * 60


def _make_saml_request(request: Request) -> dict:
    # OP_SAML_HTTPS=true means we're behind an HTTPS reverse proxy — trust that,
    # not request.url.scheme (which will be "http" inside the container).
    https = settings.saml_https or request.url.scheme == "https"
    return {
        "https": "on" if https else "off",
        "http_host": request.headers.get("host", "localhost"),
        "server_port": str(443 if https else (request.url.port or 80)),
        "script_name": "",
        "get_data": dict(request.query_params),
        "post_data": {},
    }


def _get_or_create_user(db: Session, person_id: str) -> User:
    # The IdP sends a UPN; accounts are keyed by the directory's numeric
    # person_id. `identity.find_or_create` joins the two so a login lands on
    # the account that holds the person's rights (2026-08-28 — before it,
    # every SSO login of a colleague added by an admin made a second,
    # rightless account).
    from app.services import identity
    created = db.query(User).filter(User.person_id == identity.resolve(db, person_id)[0]).first() is None
    user = identity.find_or_create(db, person_id, role=ROLE_VISITOR)
    db.commit()
    db.refresh(user)
    if created and user.person_id != person_id:
        print(f"[auth] Login {person_id!r} resolved to account {user.person_id!r}")
    elif created:
        print(f"[auth] Auto-created visitor: {person_id}")
    return user


def _attach_scopes(db: Session, user: User) -> None:
    # ADR-18: rows of the positions view that had no account when the view
    # was imported get bound to this account now. Never blocks the login.
    try:
        from app.services import scopes
        n = scopes.attach_on_login(db, user)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"[auth] scope attach failed for {user.person_id!r}: {e}")
        return
    if n:
        entry = AuditLog(actor_id=user.id, action="rls_scope_attach",
                         resource_type="user", resource_id=user.person_id)
        entry.set_detail({"rows": n})
        db.add(entry)
        db.commit()
        print(f"[auth] Attached {n} scope row(s) to {user.person_id!r} on login")


def _login_user(response: Response, db: Session, user: User) -> None:
    user.last_login = datetime.utcnow()
    db.commit()
    sid = session_store.create(user.id, user.person_id, user.role)
    secure = settings.saml_https
    # The dashboard iframe is sandboxed without allow-same-origin (opaque origin),
    # so its /render requests are treated as cross-site — SameSite=Strict/Lax would
    # withhold the session cookie and the iframe would 401 ("Session expired").
    # SameSite=None lets the cookie reach the iframe; browsers require Secure with it,
    # so fall back to Lax on plain-http dev where Secure isn't set.
    samesite = "none" if secure else "lax"
    response.set_cookie(
        COOKIE_NAME, sid,
        httponly=True,
        samesite=samesite,
        secure=secure,
        max_age=8 * 3600,
    )
    # Refresh op_lang from the profile at every login: a second browser (or one
    # whose cookie was cleared) picks the account's language up immediately, and
    # the anonymous pages that follow a logout stay in it. No profile language
    # yet → keep whatever LangMiddleware negotiated from the browser for this
    # very request, rather than forcing ru on an English one.
    lang = user.lang if user.lang in i18n.LANGS else i18n.current_lang()
    i18n.set_lang(lang)
    set_lang_cookie(response, lang)


# ---------------------------------------------------------------------------
# Pages (HTML)
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user=Depends(get_current_user_optional)):
    if user:
        return RedirectResponse("/", status_code=302)
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(env.get_template("pages/login.html").render,
        user=user,
        dev_bypass_allowed=settings.dev_bypass_auth,
        local_auth_allowed=local_auth.enabled(),
        sso_allowed=_sso_allowed(), oidc_allowed=oidc.enabled(),
        request=request,
    ))


def _sso_allowed() -> bool:
    # The SSO button is the product's only door, so it stays whenever nothing
    # else is configured; on a password-only stand it would lead to a 500.
    #
    # Since python3-saml became an optional requirement, "nothing else is
    # configured" is no longer enough: without the package the button leads to
    # a 503, and a fresh clone has neither the package nor local users. An
    # unusable door is worse than no door — the login page then offers what
    # actually works.
    from app.auth.saml import SAML_AVAILABLE
    # The door is shown only if it can be entered. The old rule
    # kept the button "just in case" when nothing else was configured,
    # — and on a fresh clone it led to a 500: the library is there, the settings
    # are not, OneLogin_Saml2_Error flies out of the constructor. No fallback door
    # is needed any more: if NOT A SINGLE sign-in method works, the server refuses
    # to start (preflight.check_login_possible) instead of showing a decoy button.
    return SAML_AVAILABLE and bool(settings.saml_idp_sso_url.strip())


@router.get("/access-denied", response_class=HTMLResponse)
async def access_denied(request: Request, user=Depends(get_current_user_optional)):
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(env.get_template("pages/access_denied.html").render, user=user, request=request))


@router.get("/logout")
async def logout(request: Request, response: Response):
    sid = request.cookies.get(COOKIE_NAME)
    if sid:
        session_store.delete(sid)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ---------------------------------------------------------------------------
# OIDC
# ---------------------------------------------------------------------------
# The third door. Both halves — SAML and OIDC — end in
# `identity.find_or_create`: one door to account creation, otherwise the two
# paths will one day start creating different people for the same person_id.


def _oidc_redirect_uri(request: Request) -> str:
    # The address comes from the site setting, not from the request Host: Host
    # can be forged, and redirect_uri is where the provider will send the code.
    base = settings.saml_sp_base_url.rstrip("/")
    return f"{base}/oidc/callback"


@router.get("/oidc/login")
async def oidc_login(request: Request):
    if not oidc.enabled():
        raise HTTPException(status_code=404)
    try:
        ep = await run_in_threadpool(oidc.discover)
    except oidc.OidcError as e:
        raise HTTPException(status_code=503, detail=str(e))

    state, nonce, verifier = oidc.new_state()
    url = oidc.authorize_url(ep, _oidc_redirect_uri(request), state, nonce, verifier)
    resp = RedirectResponse(url, status_code=302)
    # Lax, not None: the provider returns the person by an ORDINARY link navigation
    # (a top-level GET), and Lax sends such a cookie. None without Secure is
    # rejected by the browser, and the http stand has no Secure.
    resp.set_cookie(
        oidc.STATE_COOKIE, oidc.pack_state(state, nonce, verifier),
        httponly=True,
        samesite="lax",
        secure=settings.saml_https,
        max_age=oidc.STATE_TTL_S,
        path="/oidc/",
    )
    return resp


@router.get("/oidc/callback")
async def oidc_callback(request: Request, db: Session = Depends(get_db)):
    if not oidc.enabled():
        raise HTTPException(status_code=404)
    ip = request.client.host if request.client else ""

    async def _fail(message: str, status_code: int = 400, who: str = ""):
        entry = AuditLog(actor_id=None, action="oidc_login_failed",
                         resource_type="user", resource_id=who[:64], ip_address=ip)
        entry.set_detail({"reason": message[:200]})
        db.add(entry)
        db.commit()
        env: Environment = request.app.state.jinja
        resp = HTMLResponse(
            await run_in_threadpool(
                env.get_template("pages/login.html").render,
                user=None, dev_bypass_allowed=settings.dev_bypass_auth,
                local_auth_allowed=local_auth.enabled(), sso_allowed=_sso_allowed(),
                oidc_allowed=oidc.enabled(), error=message, request=request),
            status_code=status_code)
        resp.delete_cookie(oidc.STATE_COOKIE, path="/oidc/")
        return resp

    # The provider refused (the person clicked "no", the administrator did not grant access).
    if request.query_params.get("error"):
        return await _fail(_("SSO sign-in was declined: {p1}",
                             p1=str(request.query_params.get("error"))[:80]))

    code = request.query_params.get("code") or ""
    got_state = request.query_params.get("state") or ""
    cookie = request.cookies.get(oidc.STATE_COOKIE) or ""
    if not code:
        return await _fail(_("The provider did not return an authorization code."))
    try:
        want_state, nonce, verifier = oidc.unpack_state(cookie)
    except oidc.OidcError as e:
        return await _fail(str(e))
    # CONSTANT-time comparison: state is how the callback proves
    # that the sign-in started in this browser.
    if not secrets.compare_digest(got_state, want_state):
        return await _fail(_("This callback does not belong to the sign-in you started."))

    try:
        ep = await run_in_threadpool(oidc.discover)
        tokens = await run_in_threadpool(
            oidc.exchange_code, ep, code, _oidc_redirect_uri(request), verifier)
        claims = oidc.read_id_token(tokens.get("id_token") or "")
        oidc.verify_claims(claims, ep, nonce)
        who = oidc.person_id_from(claims)
    except oidc.OidcError as e:
        return await _fail(str(e), 400)

    user = await run_in_threadpool(_get_or_create_user, db, who)
    if not user.is_active:
        return await _fail(
            _("This account is not registered in OnePage. Ask an administrator."),
            403, who)
    _attach_scopes(db, user)

    entry = AuditLog(actor_id=user.id, action="oidc_login", resource_type="user",
                     resource_id=str(user.id), ip_address=ip)
    entry.set_detail({"claim": settings.oidc_claim_person_id})
    db.add(entry)

    resp = RedirectResponse("/", status_code=302)
    _login_user(resp, db, user)
    resp.delete_cookie(oidc.STATE_COOKIE, path="/oidc/")
    return resp


# ---------------------------------------------------------------------------
# SAML
# ---------------------------------------------------------------------------

@router.get("/saml/login")
async def saml_login(request: Request):
    req = _make_saml_request(request)
    try:
        redirect_url, request_id = get_saml_redirect_url(req)
    except RuntimeError as e:
        # The package is missing — say so plainly, not with a traceback.
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:  # noqa: BLE001
        # The package is there, the settings are not: python3-saml raises ITS OWN error from
        # the constructor (idp_sso_not_found and friends). For a person this is a 503
        # "SSO is not configured", not a 500 "something broke".
        raise HTTPException(
            status_code=503,
            detail=f"SSO is not configured on this instance: {e}")
    resp = RedirectResponse(redirect_url, status_code=302)
    if request_id:
        # Carries the AuthnRequest id across the round trip to ADFS so the ACS
        # can prove the response answers THIS attempt. Not a secret — a nonce.
        # SameSite must match the session cookie: ADFS posts back cross-site, so
        # a Lax cookie would not be sent and the binding would silently vanish.
        resp.set_cookie(
            SAML_REQUEST_COOKIE, request_id,
            httponly=True,
            samesite="none" if settings.saml_https else "lax",
            secure=settings.saml_https,
            max_age=_SAML_REQUEST_TTL_S,
        )
    return resp


@router.post("/saml/acs")
async def saml_acs(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    from urllib.parse import parse_qs
    post_data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    req = {**_make_saml_request(request), "post_data": post_data}

    # Absent cookie -> None -> the pre-existing behaviour (no InResponseTo
    # check). Losing it must not lock anyone out: it goes missing for an
    # IdP-initiated login, a bookmarked ACS, or a session started in another
    # browser. What it does buy is that a response carrying SOMEONE ELSE'S
    # InResponseTo can no longer be posted into this browser.
    request_id = request.cookies.get(SAML_REQUEST_COOKIE) or None

    try:
        identity = process_saml_response(req, request_id=request_id)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    person_id = identity["person_id"]
    user = _get_or_create_user(db, person_id)
    _attach_scopes(db, user)

    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(SAML_REQUEST_COOKIE)
    _login_user(resp, db, user)
    return resp


@router.get("/saml/metadata")
async def saml_metadata():
    from fastapi.responses import Response as FastResponse
    return FastResponse(content=get_sp_metadata(), media_type="application/xml")


# ---------------------------------------------------------------------------
# Dev bypass
# ---------------------------------------------------------------------------

@router.get("/dev-login", response_class=HTMLResponse)
async def dev_login_page(request: Request):
    if not settings.dev_bypass_auth:
        raise HTTPException(status_code=404)
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(env.get_template("pages/login.html").render,
        user=None, dev_bypass_allowed=True, request=request,
    ))


@router.post("/dev-login")
async def dev_login(
    request: Request,
    person_id: str = Form(...),
    db: Session = Depends(get_db),
):
    if not settings.dev_bypass_auth:
        raise HTTPException(status_code=404)

    person_id = person_id.strip()
    from app.services import identity
    user = identity.find_or_create(db, person_id, create=False)
    if user is None:
        env: Environment = request.app.state.jinja
        return HTMLResponse(
            await run_in_threadpool(env.get_template("pages/login.html").render,
                user=None, dev_bypass_allowed=True,
                error=f"User '{person_id}' not found. Ask an administrator to add you.",
                request=request,
            ),
            status_code=400,
        )
    _attach_scopes(db, user)

    resp = RedirectResponse("/", status_code=302)
    _login_user(resp, db, user)
    return resp


# ---------------------------------------------------------------------------
# Password sign-in (OP_LOCAL_USERS) — stands outside the perimeter only
# ---------------------------------------------------------------------------

@router.post("/local-login")
async def local_login(
    request: Request,
    person_id: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if not local_auth.enabled():
        raise HTTPException(status_code=404)
    person_id = person_id.strip()
    ip = request.client.host if request.client else ""

    async def _fail(message: str, status_code: int = 401):
        # One PBKDF2 round already cost ~0.3 s; the pause makes online
        # guessing slower still without a rate-limit table.
        await asyncio.sleep(0.5)
        entry = AuditLog(actor_id=None, action="local_login_failed", resource_type="user",
                         resource_id=person_id[:64], ip_address=ip)
        entry.set_detail({"status": status_code})
        db.add(entry)
        db.commit()
        env: Environment = request.app.state.jinja
        return HTMLResponse(
            await run_in_threadpool(env.get_template("pages/login.html").render,
                user=None, dev_bypass_allowed=settings.dev_bypass_auth,
                local_auth_allowed=True, sso_allowed=_sso_allowed(), oidc_allowed=oidc.enabled(),
                error=message, request=request,
            ),
            status_code=status_code,
        )

    ok = await run_in_threadpool(local_auth.check, person_id, password)
    if not ok:
        return await _fail(_("Wrong username or password."))
    from app.services import identity
    user = identity.find_or_create(db, person_id, create=False)
    if user is None or not user.is_active:
        return await _fail(_("This account is not registered in OnePage. Ask an administrator."), 403)
    _attach_scopes(db, user)

    resp = RedirectResponse("/", status_code=302)
    _login_user(resp, db, user)
    entry = AuditLog(actor_id=user.id, action="local_login", resource_type="user",
                     resource_id=user.person_id, ip_address=ip)
    entry.set_detail({})
    db.add(entry)
    db.commit()
    return resp
