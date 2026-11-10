"""OIDC sign-in — the third door, next to the password and SAML.

Authorization Code flow with PKCE. No new dependency: `httpx` is already here,
and everything else is stdlib. That is the whole reason this exists alongside
SAML — `python3-saml` needs a compiler and system libraries, and a project
nobody can `pip install` is a project nobody tries.

WHY THE ID TOKEN'S SIGNATURE IS NOT VERIFIED
--------------------------------------------
The token arrives on a direct TLS connection to the provider's own token
endpoint, authenticated with our client secret. OpenID Connect Core §3.1.3.7
item 6 says the signature check MAY be skipped in exactly this case, because
TLS already proves who sent it and nobody else could have. Verifying it
properly would mean JWKS fetching, key rotation and an RSA implementation —
a lot of surface to protect a channel that is already protected. What we do
check are the claims that bind the token to THIS login: issuer, audience and
expiry. `nonce` is sent and checked for the same reason.

WHAT THE STATE COOKIE IS FOR
----------------------------
`state` and the PKCE verifier live in one short-lived HttpOnly cookie. The
callback compares the `state` in the URL with the one in the cookie: without
that, anyone could feed a victim a callback URL carrying their own code and
land the victim in the attacker's account. The cookie is deleted as soon as
the callback reads it, successfully or not.

This module holds no FastAPI routes on purpose — the same split as
`app/auth/saml.py`. Routes live in `app/api/auth.py`, and both doors end in
the one place a session may be created, `identity.find_or_create`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass

import httpx

from app.config import settings

STATE_COOKIE = "op_oidc"
STATE_TTL_S = 600          # a login the human abandoned should not stay open
_CLOCK_SKEW_S = 120        # the provider's clock is not ours

_discovery_cache: dict[str, tuple[float, dict]] = {}


class OidcError(RuntimeError):
    """Anything that stops a login. The message reaches the person, so it has
    to say what happened without naming a secret."""


@dataclass(frozen=True)
class Endpoints:
    authorization: str
    token: str
    issuer: str


def enabled() -> bool:
    return settings.oidc_enabled


def _discovery_url() -> str:
    return settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"


def discover(*, timeout: float = 10.0) -> Endpoints:
    """Read the provider's own description of itself, cached.

    Cached because it changes once in years, and a call to someone else's
    server on every sign-in would put their outage on our login page.
    """
    key = settings.oidc_issuer.rstrip("/")
    hit = _discovery_cache.get(key)
    if hit and (time.time() - hit[0]) < settings.oidc_discovery_ttl_s:
        doc = hit[1]
    else:
        try:
            r = httpx.get(_discovery_url(), timeout=timeout)
            r.raise_for_status()
            doc = r.json()
        except Exception as exc:  # noqa: BLE001 — any failure is the same story
            raise OidcError(f"OIDC provider did not answer discovery: {exc}") from exc
        _discovery_cache[key] = (time.time(), doc)

    for field in ("authorization_endpoint", "token_endpoint"):
        if not doc.get(field):
            raise OidcError(f"OIDC discovery has no {field}")
    # The issuer the provider claims wins over the one in our config: it is
    # what will be inside the token, and comparing a token against our own
    # typo would reject every login with an unhelpful message.
    return Endpoints(authorization=doc["authorization_endpoint"],
                     token=doc["token_endpoint"],
                     issuer=doc.get("issuer", key))


def reset_cache() -> None:
    """Drop the discovery cache — tests and a provider that just moved."""
    _discovery_cache.clear()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_state() -> tuple[str, str, str]:
    """(state, nonce, pkce_verifier) — three independent random values."""
    return (secrets.token_urlsafe(24), secrets.token_urlsafe(16),
            secrets.token_urlsafe(48))


def pack_state(state: str, nonce: str, verifier: str) -> str:
    """What goes into the cookie. Plain, because it is not a secret worth
    hiding from its own owner: it is useless to anyone who cannot also read
    the callback, and it lives ten minutes."""
    return "|".join((state, nonce, verifier, str(int(time.time()))))


def unpack_state(raw: str) -> tuple[str, str, str]:
    """Reverse of pack_state. Raises OidcError on anything unexpected —
    including an expired cookie, which is the ordinary case of a login left
    open in a tab since yesterday."""
    parts = (raw or "").split("|")
    if len(parts) != 4:
        raise OidcError("the sign-in did not start here — begin again")
    state, nonce, verifier, started = parts
    try:
        age = time.time() - int(started)
    except ValueError as exc:
        raise OidcError("the sign-in did not start here — begin again") from exc
    if age > STATE_TTL_S:
        raise OidcError("the sign-in took too long — begin again")
    return state, nonce, verifier


def challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def authorize_url(ep: Endpoints, redirect_uri: str, state: str, nonce: str,
                  verifier: str) -> str:
    from urllib.parse import urlencode
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge(verifier),
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in ep.authorization else "?"
    return ep.authorization + sep + urlencode(params)


def exchange_code(ep: Endpoints, code: str, redirect_uri: str, verifier: str,
                  *, timeout: float = 15.0) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": settings.oidc_client_id,
        "code_verifier": verifier,
    }
    # A public client has no secret; sending an empty one makes some providers
    # reject the request outright.
    secret = settings.oidc_client_secret.strip()
    if secret:
        data["client_secret"] = secret
    try:
        r = httpx.post(ep.token, data=data, timeout=timeout,
                       headers={"Accept": "application/json"})
    except Exception as exc:  # noqa: BLE001
        raise OidcError(f"could not reach the OIDC provider: {exc}") from exc
    if r.status_code != 200:
        # The provider's body can quote the client secret back at us in an
        # error about it, so only the documented fields are repeated.
        try:
            body = r.json()
            detail = str(body.get("error") or "")[:120]
        except Exception:  # noqa: BLE001
            detail = ""
        raise OidcError(f"OIDC provider refused the code{': ' + detail if detail else ''}")
    try:
        return r.json()
    except Exception as exc:  # noqa: BLE001
        raise OidcError("OIDC provider answered with something that is not JSON") from exc


def read_id_token(token: str) -> dict:
    """Claims out of the JWT payload, WITHOUT signature verification.

    Allowed here and only here: the token came straight from the token
    endpoint over TLS with client authentication (OIDC Core §3.1.3.7 item 6).
    This function must never be used on a token that arrived any other way —
    an unverified JWT from the browser is whatever its bearer typed.
    """
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise OidcError("OIDC provider returned a malformed id_token")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception as exc:  # noqa: BLE001
        raise OidcError("OIDC provider returned an unreadable id_token") from exc


def verify_claims(claims: dict, ep: Endpoints, nonce: str) -> None:
    """The three bindings that make this token ours, and this login's.

    Not a substitute for a signature — see the module docstring for why there
    is none. These catch a token that is genuine but belongs to a different
    issuer, a different application, yesterday, or a different sign-in.
    """
    iss = str(claims.get("iss") or "")
    if iss.rstrip("/") != ep.issuer.rstrip("/"):
        raise OidcError("the id_token came from a different issuer")

    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if settings.oidc_client_id not in [str(a) for a in auds if a is not None]:
        raise OidcError("the id_token was issued for a different application")

    exp = claims.get("exp")
    try:
        if exp is None or float(exp) + _CLOCK_SKEW_S < time.time():
            raise OidcError("the id_token has expired — sign in again")
    except (TypeError, ValueError) as exc:
        raise OidcError("the id_token has no usable expiry") from exc

    # `nonce` ties the token to the browser that started THIS sign-in. A
    # provider that does not echo it leaves replay open, so a missing nonce is
    # a refusal, not a shrug.
    if str(claims.get("nonce") or "") != nonce:
        raise OidcError("the id_token does not match this sign-in")


def person_id_from(claims: dict) -> str:
    """The account name, by the claim the operator named.

    Falls back to `sub` — always present by spec — rather than failing the
    login: a provider that does not send `preferred_username` would otherwise
    look broken, and `sub` at least identifies the person stably.
    """
    name = str(claims.get(settings.oidc_claim_person_id) or "").strip()
    if name:
        return name
    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise OidcError("the id_token carries no account name")
    return sub
