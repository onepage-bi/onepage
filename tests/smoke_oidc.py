"""Smoke for OIDC login (`app/auth/oidc.py`, routes in `app/api/auth.py`).

The main thing pinned here is NOT "login works" but that it **refuses**.
A door into the system is not checked on the happy path: the happy path breaks
loudly and at once, while a hole in token verification stays silent until the day
someone uses it. So there are a dozen refusals here per happy path, and each one
maps to one line of `verify_claims`/`unpack_state`.

The id_token signature is deliberately not verified (OIDC Core §3.1.3.7, item 6: the token
arrived over a direct TLS connection with the token endpoint under client authentication).
So everything else that ties the token to THIS login (issuer, audience, expiry
and nonce) must be checked, and each of them has a test.

The provider is replaced at the level of our own functions (`discover`,
`exchange_code`): the test is about our logic, not httpx.
"""
import base64
import json
import os
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlparse

TMP = tempfile.mkdtemp(prefix="op-oidc-smoke-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
})
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.auth import oidc  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

FAILURES = []

ISSUER = "https://idp.example.com/realms/demo"
CLIENT = "onepage"


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def jwt(payload: dict) -> str:
    """JWT with a stub signature: nobody verifies it anyway, and the test must not
    pretend that it does."""
    def seg(d):
        raw = json.dumps(d, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{seg({'alg': 'RS256'})}.{seg(payload)}.c2ln"


def claims(**over) -> dict:
    base = {"iss": ISSUER, "aud": CLIENT, "exp": time.time() + 300,
            "sub": "uuid-1", "preferred_username": "anna.petrova"}
    base.update(over)
    return base


EP = oidc.Endpoints(authorization=f"{ISSUER}/protocol/openid-connect/auth",
                    token=f"{ISSUER}/protocol/openid-connect/token",
                    issuer=ISSUER)


def enable():
    settings.oidc_issuer = ISSUER
    settings.oidc_client_id = CLIENT
    settings.oidc_client_secret = "shhh"
    oidc.reset_cache()


def disable():
    settings.oidc_issuer = ""
    settings.oidc_client_id = ""
    oidc.reset_cache()


def start(c):
    """Start a login and return (authorization params, state, nonce)."""
    r = c.get("/oidc/login", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    cookie = c.cookies.get(oidc.STATE_COOKIE)
    state, nonce, _verifier = oidc.unpack_state(cookie)
    return q, state, nonce


def main():
    # ── disabled: there must be no doors ─────────────────────────────────
    disable()
    with TestClient(app) as c:
        check("disabled: /oidc/login -> 404",
              c.get("/oidc/login", follow_redirects=False).status_code == 404)
        check("disabled: /oidc/callback -> 404",
              c.get("/oidc/callback?code=x&state=y", follow_redirects=False).status_code == 404)
        check("disabled: no button on the login page",
              "/oidc/login" not in c.get("/login").text)

    enable()
    oidc.discover = lambda **kw: EP  # noqa: E731 — provider stub

    with TestClient(app) as c:
        check("enabled: the login page has the button",
              "/oidc/login" in c.get("/login").text)

        # ── login start ──────────────────────────────────────────────────
        q, state, nonce = start(c)
        check("start: redirects to the provider's authorization endpoint",
              c.get("/oidc/login", follow_redirects=False).headers["location"].startswith(EP.authorization))
        check("start: code flow", q.get("response_type") == ["code"])
        check("start: PKCE is S256, not plain",
              q.get("code_challenge_method") == ["S256"] and q.get("code_challenge"))
        check("start: client_id is ours", q.get("client_id") == [CLIENT])
        check("start: redirect_uri from the site setting, not from Host",
              q.get("redirect_uri") == [settings.saml_sp_base_url.rstrip("/") + "/oidc/callback"])
        check("start: state and nonce are random and distinct",
              state and nonce and state != nonce)
        check("start: the client secret does not reach the URL",
              "shhh" not in str(q))

        # ── PKCE: the challenge really derives from the verifier ─────────
        # Start the login AGAIN and take the URL and the cookie from the same
        # request: every /oidc/login issues a new pair, and comparing the challenge
        # of one login with the verifier of another is pointless.
        q, _state, _nonce = start(c)
        _s, _n, verifier = oidc.unpack_state(c.cookies.get(oidc.STATE_COOKIE))
        check("PKCE: challenge = S256(verifier)",
              oidc.challenge(verifier) == q["code_challenge"][0])

        # ── refusals ─────────────────────────────────────────────────────
        oidc.exchange_code = lambda *a, **kw: {"id_token": jwt(claims(nonce=nonce))}  # noqa: E731

        q, state, nonce = start(c)
        r = c.get(f"/oidc/callback?code=c&state={state}X", follow_redirects=False)
        check("refused: foreign state", r.status_code == 400, r.status_code)

        q, state, nonce = start(c)
        c.cookies.delete(oidc.STATE_COOKIE)
        r = c.get(f"/oidc/callback?code=c&state={state}", follow_redirects=False)
        check("refused: login not started here (no cookie)", r.status_code == 400, r.status_code)

        q, state, nonce = start(c)
        r = c.get(f"/oidc/callback?state={state}", follow_redirects=False)
        check("refused: provider sent no code", r.status_code == 400, r.status_code)

        q, state, nonce = start(c)
        r = c.get(f"/oidc/callback?error=access_denied&state={state}", follow_redirects=False)
        check("refused: provider rejected the login", r.status_code == 400, r.status_code)

        bad = [
            ("foreign issuer", dict(iss="https://evil.example.com")),
            ("foreign audience", dict(aud="another-app")),
            ("expired token", dict(exp=time.time() - 10_000)),
            ("no expiry", dict(exp=None)),
            ("foreign nonce", dict(nonce="not-ours")),
        ]
        for label, over in bad:
            q, state, nonce = start(c)
            payload = claims(nonce=nonce)
            payload.update(over)
            oidc.exchange_code = lambda *a, **kw: {"id_token": jwt(payload)}  # noqa: E731
            r = c.get(f"/oidc/callback?code=c&state={state}", follow_redirects=False)
            check(f"refused: {label}", r.status_code == 400, r.status_code)

        q, state, nonce = start(c)
        oidc.exchange_code = lambda *a, **kw: {"id_token": "not-a-jwt"}  # noqa: E731
        r = c.get(f"/oidc/callback?code=c&state={state}", follow_redirects=False)
        check("refused: id_token is not a JWT", r.status_code == 400, r.status_code)

        q, state, nonce = start(c)
        oidc.exchange_code = lambda *a, **kw: (_ for _ in ()).throw(  # noqa: E731
            oidc.OidcError("provider refused"))
        r = c.get(f"/oidc/callback?code=c&state={state}", follow_redirects=False)
        check("refused: provider did not exchange the code", r.status_code == 400, r.status_code)

        # ── happy path ───────────────────────────────────────────────────
        q, state, nonce = start(c)
        oidc.exchange_code = lambda *a, **kw: {"id_token": jwt(claims(nonce=nonce))}  # noqa: E731
        r = c.get(f"/oidc/callback?code=c&state={state}", follow_redirects=False)
        check("login: redirect to home", r.status_code == 302 and r.headers["location"] == "/",
              f"{r.status_code} {r.headers.get('location')}")
        from app.auth.session import COOKIE_NAME
        check("login: session issued", bool(c.cookies.get(COOKIE_NAME)))
        check("login: state cookie removed", not c.cookies.get(oidc.STATE_COOKIE))
        check("login: the page opens", c.get("/").status_code == 200)

        # ── account and audit ────────────────────────────────────────────
        from app.database import SessionLocal
        from app.models import AuditLog, User
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.person_id == "anna.petrova").first()
            check("login: account created from the claim", u is not None)
            ok = db.query(AuditLog).filter(AuditLog.action == "oidc_login").count()
            bad_n = db.query(AuditLog).filter(AuditLog.action == "oidc_login_failed").count()
            check("audit: success recorded", ok >= 1, ok)
            check("audit: every refusal recorded", bad_n >= 10, bad_n)
            rows = db.query(AuditLog).filter(AuditLog.action.like("oidc_%")).all()
            leaked = [r.id for r in rows if "shhh" in (r.detail or "")]
            check("audit: the client secret does not reach the log", not leaked, leaked)
        finally:
            db.close()

        # ── the claim is configurable, with a fallback ───────────────────
        settings.oidc_claim_person_id = "email"
        check("claim: the one named by the setting is used",
              oidc.person_id_from({"email": "x@example.com", "sub": "s"}) == "x@example.com")
        check("claim: if the named one is missing, sub is used, not a refusal",
              oidc.person_id_from({"sub": "s"}) == "s")
        settings.oidc_claim_person_id = "preferred_username"

    # ── expired state cookie ─────────────────────────────────────────
    old = oidc.pack_state("s", "n", "v").rsplit("|", 1)[0] + "|" + str(int(time.time()) - 10_000)
    try:
        oidc.unpack_state(old)
        check("refused: expired login", False, "no exception was raised")
    except oidc.OidcError:
        check("refused: expired login", True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("oidc: all checks passed")


if __name__ == "__main__":
    main()
