"""Startup preflight — refuse to boot into a configuration that cannot work.

Every field in app/config.py has a default, so the app has always started no
matter how it was configured. That is how a first deploy ends up in one of
these states, each of which boots "successfully" and answers /health with ok:

  * SAML strict against an empty IdP certificate — SSO can never validate, and
    with the dev bypass off there is no other way in.
  * No administrator anywhere: OP_INITIAL_ADMIN empty, the users table empty,
    dev-login 404. A running system nobody can log into.
  * OP_SAML_HTTPS left false behind an https:// URL — the session cookie ships
    without Secure and falls back to SameSite=lax, so the sandboxed render
    iframe (opaque origin, cross-site request) never receives it and every
    dashboard shows "Session expired".
  * The data directory not writable — SQLite is written inside the container
    layer and disappears on the next `docker compose up`, silently.

Each check fires on a COMBINATION, never on a single field, so the dev config
and the benchmark stand (both OP_DEV_BYPASS_AUTH=true, no certificate)
stay unaffected.

All problems are collected and reported together: an operator should fix a
deploy in one pass, not discover the next fault after each restart.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app.config import settings


class PreflightError(RuntimeError):
    """Configuration cannot work. Raised from the app lifespan, so the process
    exits instead of serving a broken instance."""


def _looks_public(url: str) -> bool:
    """True when the SP base URL is an https:// address, i.e. this instance is
    configured as if it were reachable from outside."""
    return url.strip().lower().startswith("https://")


def _writable(path: Path) -> str | None:
    """Return a reason string if `path` (or the nearest existing ancestor) is
    not writable. Actually writes — os.access lies under ACLs and when running
    as root in a container over a read-only mount."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir():
        return f"{probe} exists but is not a directory"
    try:
        with tempfile.NamedTemporaryFile(dir=probe, prefix=".op-preflight-"):
            pass
    except OSError as exc:
        return f"{probe} is not writable ({exc.strerror or exc})"
    return None


def check_config() -> None:
    """Config-only + filesystem checks. Runs FIRST in the lifespan, before the
    upload dir is created and before any table is touched."""
    problems: list[str] = []

    # SSO is configured but the library is missing. Without this check the failure is visible only
    # on the login page — a 503 instead of the button, after the server has already
    # reported "up". python3-saml lives in requirements-saml.txt
    # because building it needs system libraries (see that file).
    if settings.saml_idp_sso_url.strip() or settings.saml_idp_cert.strip():
        from app.auth import saml as _saml
        if not _saml.SAML_AVAILABLE:
            problems.append(
                "SSO is configured (OP_SAML_IDP_SSO_URL / OP_SAML_IDP_CERT), but the "
                "python3-saml package is not installed: pip install -r requirements-saml.txt "
                "(needs system libxmlsec1-dev libxml2-dev pkg-config). "
                "Or remove the SAML settings and sign in with a password or via OIDC.")

    if settings.saml_strict and not settings.saml_idp_cert.strip():
        problems.append(
            "OP_SAML_STRICT is true but OP_SAML_IDP_CERT is empty.\n"
            "    Strict validation against an empty certificate can never "
            "succeed, so SSO login is impossible.\n"
            "    Fix: set OP_SAML_IDP_CERT to the IdP signing certificate "
            "(body only, no BEGIN/END lines), or set OP_SAML_STRICT=false "
            "while you are still wiring ADFS up."
        )

    if settings.saml_strict and not settings.saml_want_assertions_signed:
        problems.append(
            "OP_SAML_STRICT is true but OP_SAML_WANT_ASSERTIONS_SIGNED is "
            "false.\n"
            "    That combination looks secure and is not: strict mode only "
            "enables python3-saml's strict parsing, while assertion signing is "
            "what actually authenticates the login. With it off the SP accepts "
            "an UNSIGNED assertion, so anyone able to POST to /saml/acs signs "
            "in as any person_id.\n"
            "    Fix: leave OP_SAML_WANT_ASSERTIONS_SIGNED at its default true."
        )

    if _looks_public(settings.saml_sp_base_url) and not settings.saml_https:
        problems.append(
            f"OP_SAML_SP_BASE_URL is {settings.saml_sp_base_url!r} (https) but "
            "OP_SAML_HTTPS is false.\n"
            "    The session cookie would ship without Secure and fall back to "
            "SameSite=lax. The dashboard iframe is sandboxed without "
            "allow-same-origin, so its /render request is cross-site and would "
            "not carry the cookie — every dashboard would show "
            '"Session expired".\n'
            "    Fix: set OP_SAML_HTTPS=true when serving over https."
        )

    if settings.dev_bypass_auth and _looks_public(settings.saml_sp_base_url):
        problems.append(
            f"OP_DEV_BYPASS_AUTH is true while OP_SAML_SP_BASE_URL is "
            f"{settings.saml_sp_base_url!r} (https).\n"
            "    The password-less dev login would be reachable on what looks "
            "like a production host: anyone could sign in as any person_id.\n"
            "    Fix: set OP_DEV_BYPASS_AUTH=false in production."
        )

    for label, target in (
        ("OP_UPLOADS_DIR", settings.abs_uploads_dir),
        ("OP_DB_PATH", settings.abs_db_path.parent),
    ):
        reason = _writable(target)
        if reason:
            problems.append(
                f"{label} is not usable: {reason}.\n"
                "    Uploads and the SQLite database would be written into the "
                "container layer and lost on the next recreate — silently.\n"
                "    Fix: create the directory on the host and mount it "
                "(docker-compose.yml mounts ./data), with write access for the "
                "container user."
            )

    _raise(problems, "configuration")


def check_login_possible() -> None:
    """Is there any way to authenticate at all? Runs AFTER the migrations and
    AFTER the initial-admin bootstrap, because it asks the database."""
    if settings.dev_bypass_auth:
        return  # dev-login is open; nothing to strand the operator

    from app.database import SessionLocal
    from app.models import ROLE_ADMINISTRATOR, User

    db = SessionLocal()
    try:
        has_admin = (
            db.query(User)
            .filter(User.role == ROLE_ADMINISTRATOR, User.is_active == True)  # noqa: E712
            .first()
            is not None
        )
    finally:
        db.close()

    if not has_admin:
        _raise([
            "No way to log in: OP_DEV_BYPASS_AUTH is false and the database "
            "holds no active administrator.\n"
            "    OP_INITIAL_ADMIN bootstraps one at startup "
            "(app/main.py), but it is currently "
            f"{settings.initial_admin!r}.\n"
            "    Fix: set OP_INITIAL_ADMIN to the person_id that should own "
            "the instance, then restart."
        ], "authentication")


def _raise(problems: list[str], kind: str) -> None:
    if not problems:
        return
    body = "\n\n".join(f"  {i}. {p}" for i, p in enumerate(problems, 1))
    raise PreflightError(
        f"\n\nOnePage refused to start — {kind} preflight found "
        f"{len(problems)} problem(s):\n\n{body}\n\n"
        "Each of these boots 'successfully' and answers /health with ok, which "
        "is why the check exists.\n"
    )
