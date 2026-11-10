"""
Per-request performance timing — pure ASGI middleware.

Deliberately NOT a BaseHTTPMiddleware: the raw ASGI form adds no task/queue
overhead and never interferes with streaming bodies. Placed outermost (added
last in create_app) so the measured duration includes routing, handler, and
GZip compression — everything except uvicorn itself.
"""
from __future__ import annotations

import os
import random
import time
from time import perf_counter

from starlette.datastructures import MutableHeaders

from app import i18n
from app.config import settings
from app.services import perflog

_SKIP_PREFIXES = ("/static/",)
_SKIP_PATHS = ("/health", "/favicon.ico")

# LangMiddleware skips more than the others: /render/ answers the bytes of a
# report, never chrome, so negotiating a language for it is pure overhead. It
# cannot live in _SKIP_PREFIXES — PerfLogMiddleware shares that tuple and the
# fast lane IS timed.
_LANG_SKIP_PREFIXES = _SKIP_PREFIXES + ("/render/",)


class PerfLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.perf_enabled or not perflog.runtime_on():
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith(_SKIP_PREFIXES) or path in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return
        # One read of the published probability: the SAME value decides the
        # sample and gets stamped into the record, so the 1/sample weight on
        # /admin/perf is exactly unbiased. Adaptive off => OP_PERF_SAMPLE.
        # The decision is made now but APPLIED at emit time: error responses
        # (>= 400) are always emitted, or a rare 5xx on a busy server would
        # vanish from /admin/errors with probability 1-eff.
        eff = perflog.effective_sample()
        keep = eff >= 1.0 or random.random() < eff

        rec: dict = {"db_ms": 0.0, "db_n": 0}
        token = perflog.request_record.set(rec)
        t0 = perf_counter()
        meta = {"status": 0, "bytes": 0, "hdr_ms": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                meta["status"] = message["status"]
                # Time-to-headers = server "think time" (transfer excluded).
                meta["hdr_ms"] = (perf_counter() - t0) * 1000
                headers = MutableHeaders(raw=message["headers"])
                headers.append(
                    "Server-Timing",
                    f'app;dur={meta["hdr_ms"]:.1f}, '
                    f'db;dur={rec["db_ms"]:.1f};desc="{rec["db_n"]}q"',
                )
            elif message["type"] == "http.response.body":
                meta["bytes"] += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            perflog.request_record.reset(token)
            status = meta["status"]
            if keep or status >= 400:
                dur_ms = (perf_counter() - t0) * 1000
                route = scope.get("route")
                client = scope.get("client")
                record = {
                    "ts": round(time.time(), 3),
                    "src": "server",
                    "pid": os.getpid(),
                    "method": scope.get("method", ""),
                    "route": getattr(route, "path", None) or path,
                    "path": path,
                    "status": status,
                    "dur_ms": round(dur_ms, 3),
                    "hdr_ms": round(meta["hdr_ms"], 3) if meta["hdr_ms"] is not None else None,
                    "db_ms": round(rec["db_ms"], 3),
                    "db_n": rec["db_n"],
                    "bytes": meta["bytes"],
                    "ip": client[0] if client else None,
                }
                if eff < 1.0 and status < 400:
                    # The weight must be 1/P(emitted). Successes are emitted
                    # with probability eff — stamp it (absent field == 1.0:
                    # legacy files, beacon, GC). Errors are emitted
                    # unconditionally, so they carry NO stamp even when the
                    # gate happened to pass them — stamping eff there would
                    # count each error with expectation 2-eff, not 1.
                    record["sample"] = eff
                if rec.get("uid") is not None:
                    record["uid"] = rec["uid"]
                if rec.get("stages"):
                    record["stages"] = rec["stages"]
                if rec.get("sql"):
                    record["sql"] = rec["sql"]
                perflog.emit(record)


# ---------------------------------------------------------------------------
# Render fast lane — pure-ASGI shortcut for warm authenticated render hits.
#
# A full FastAPI request costs ~1-2 ms in routing + dependency injection +
# response plumbing alone. For the hottest request in the system (a warm
# /render/{id}/ cache hit) this lane does the minimum imaginable work in the
# event loop: parse cookie -> session+user JOIN (dedicated read-only conn) ->
# dashboard authz (one row + optional permission EXISTS, second dedicated
# conn) -> fingerprint stats -> serve the pre-encoded variant bytes.
#
# SAFETY MODEL: this lane NEVER answers an error. Anything but a clean,
# fully-authorized warm hit — no cookie, unknown session, missing dashboard,
# no access, share token in the query, cache miss, any exception — falls
# through to the full FastAPI route, which re-derives everything with the
# canonical logic. Authorization semantics are byte-for-byte those of
# files._authorize_render (admin | owner | is_public | permission+published),
# and both DB reads happen per request — revocation applies instantly, same
# as the slow path.
#
# Perf stages: fast=1, cache_hit=1, fast_us (lane entry -> response built,
# auth included) and serve_us (cache lookup -> response built — the number
# comparable to "handler-only" latency claims).
# ---------------------------------------------------------------------------

import re
import sqlite3
import threading
from time import monotonic
from types import SimpleNamespace

from app.auth.session import get_session_id_from_cookie, session_store
from app.models import ROLE_ADMINISTRATOR

_RENDER_PATH_RE = re.compile(r"^/render/([0-9a-f-]{36})/$")
_FAST_ROUTE = SimpleNamespace(path="/render/{dashboard_id}/")

# `conf_hash` is read here not to check a password but to REFUSE: the lane
# cannot, and must not be able to, handle the second lock (it answers before
# routing, before dependencies and without the session unlock record). A
# confidential report falls through to the ordinary route and loses the 1.3 µs
# path — such reports are a handful, and that is the right price for the lock
# being checked in exactly one place. The column rides the same SELECT, so an ordinary report pays nothing.
_AUTHZ_SQL = ("SELECT current_version, owner_id, is_public, is_published, conf_hash"
              " FROM dashboards WHERE id = ?")
_PERM_SQL = ("SELECT 1 FROM dashboard_permissions"
             " WHERE dashboard_id = ? AND user_id = ? LIMIT 1")

# Auth/authz TTL micro-caches: SQLite reads cost ~10-100 µs — the last thing
# between a warm hit and single-digit µs. 1 s of memoization means revocation
# (session delete, deactivation, permission removal) reaches the LANE at most
# 1 s late — every other route still checks the DB per request. Negative
# results are never cached (unknown session/dashboard always falls through).
_AUTH_TTL = 1.0
_CACHE_CAP = 4096


class RenderFastLane:
    def __init__(self, app):
        self.app = app
        self._db = None
        self._db_lock = threading.Lock()
        self._auth: dict = {}    # sid -> (deadline, AuthRow)
        self._authz: dict = {}   # dashboard_id -> (deadline, row tuple)
        self._perm: dict = {}    # (dashboard_id, user_id) -> (deadline, bool)
        self._sid: dict = {}     # raw cookie header -> sid (pure parse memo)

    @staticmethod
    def _cached(d: dict, key, now: float):
        hit = d.get(key)  # GIL-atomic; single-threaded in the loop anyway
        return hit[1] if hit is not None and hit[0] > now else None

    @staticmethod
    def _remember(d: dict, key, val, now: float) -> None:
        if len(d) > _CACHE_CAP:
            d.clear()  # cheap reset; refills within one TTL of traffic
        d[key] = (now + _AUTH_TTL, val)

    def _conn(self):
        if self._db is None:
            from app.config import settings as cfg
            conn = sqlite3.connect(cfg.abs_db_path, check_same_thread=False)
            conn.execute("PRAGMA busy_timeout=250")  # fast-lane reader must fail fast, never hold event loop
            conn.execute("PRAGMA query_only=ON")  # this conn never writes
            self._db = conn
        return self._db

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "GET"
                or scope.get("query_string")):
            await self.app(scope, receive, send)
            return
        m = _RENDER_PATH_RE.match(scope.get("path", ""))
        if m is None or not settings.cache_encoded:
            await self.app(scope, receive, send)
            return
        try:
            response = self._try_serve(scope, m.group(1))
        except Exception:
            response = None  # the full route decides — the lane never errors
        if response is None:
            await self.app(scope, receive, send)
            return
        scope["route"] = _FAST_ROUTE  # perf records group under the template
        await response(scope, receive, send)

    @staticmethod
    def _acct_db(seconds: float) -> None:
        # The dedicated raw connection bypasses the engine's SQL timing
        # listeners — account manually so db_ms/db_n stay honest (same
        # pattern as SqliteSessionStore._perf).
        rec = perflog.request_record.get()
        if rec is not None:
            rec["db_n"] = rec.get("db_n", 0) + 1
            rec["db_ms"] = rec.get("db_ms", 0.0) + seconds * 1000

    def _try_serve(self, scope, dashboard_id):
        t0 = perf_counter()
        cookie = accept_encoding = ""
        for name, value in scope.get("headers") or ():
            if name == b"cookie":
                cookie = value.decode("latin-1")
            elif name == b"accept-encoding":
                accept_encoding = value.decode("latin-1")
        if not cookie:
            return None
        sid = self._sid.get(cookie)  # browsers resend the identical header
        if sid is None:
            sid = get_session_id_from_cookie(cookie)
            if not sid:
                return None
            if len(self._sid) > _CACHE_CAP:
                self._sid.clear()
            self._sid[cookie] = sid

        now = monotonic()
        user = self._cached(self._auth, sid, now)
        if user is None:
            hit = session_store.get_with_user(sid)
            if hit is None:
                return None
            user = hit[1]
            self._remember(self._auth, sid, user, now)

        row = self._cached(self._authz, dashboard_id, now)
        if row is None:
            conn = self._conn()
            t_db = perf_counter()
            with self._db_lock:
                row = conn.execute(_AUTHZ_SQL, (dashboard_id,)).fetchone()
            self._acct_db(perf_counter() - t_db)
            if row is None:
                return None
            self._remember(self._authz, dashboard_id, row, now)
        current_version, owner_id, is_public, is_published, conf_hash = row
        if conf_hash:
            return None
        if not (user.role == ROLE_ADMINISTRATOR or owner_id == user.id or is_public):
            if not is_published:
                return None
            pkey = (dashboard_id, user.id)
            allowed = self._cached(self._perm, pkey, now)
            if allowed is None:
                conn = self._conn()
                t_db = perf_counter()
                with self._db_lock:
                    allowed = conn.execute(_PERM_SQL, pkey).fetchone() is not None
                self._acct_db(perf_counter() - t_db)
                if allowed:  # only positive grants are memoized
                    self._remember(self._perm, pkey, True, now)
            if not allowed:
                return None

        # Imports deferred: files.py imports this module's sibling deps at
        # startup; by first request everything is initialized.
        from app.api.files import render_cache, prepared_from_cell, fingerprint_cached
        from app.services import encoding

        t1 = perf_counter()
        key = (dashboard_id, current_version,
               fingerprint_cached(dashboard_id, current_version))
        cell = render_cache.get(key)
        if cell is None:
            return None
        response = prepared_from_cell(cell, encoding.negotiate(accept_encoding))
        if response is None:
            return None  # variant not encoded yet -> full route (threadpool)
        t2 = perf_counter()

        rec = perflog.request_record.get()
        if rec is not None:
            rec["uid"] = user.id
            stages = rec.setdefault("stages", {})
            stages["cache_hit"] = 1
            stages["fast"] = 1
            stages["fast_us"] = round((t2 - t0) * 1e6, 1)
            stages["serve_us"] = round((t2 - t1) * 1e6, 1)
        return response


# ---------------------------------------------------------------------------
# Cross-origin guard for state-changing requests
# ---------------------------------------------------------------------------
"""
The session cookie is SameSite=None in production — it has to be, or the
sandboxed render iframe never receives it (see _login_user). That removes the
browser's own CSRF protection, and nothing replaced it: no token, no Origin
check, no CORS middleware. Any user-uploaded dashboard is same-site JS running
under the viewer's session, so the attacker here is not a distant website — it
is the HTML a colleague uploaded, executing while an administrator reads it.

Placement matters as much as the logic. This is registered INSIDE RenderFastLane
(added before it in create_app), so a warm /render hit short-circuits in the lane
and never enters this code at all: zero cost on the hot path. Everything else
pays one frozenset lookup on the method, and only unsafe methods scan headers.

Why a missing Origin is allowed: CSRF needs a browser attaching ambient
credentials, and browsers send Origin on every request except same-origin
GET/HEAD (Fetch spec) — including cross-site form posts. A request with no
Origin and no Referer therefore did not come from a browser acting for someone
else, so rejecting it would break curl, health probes and the smoke tests
without closing any vector. The opaque-origin iframe sends `Origin: null`, which
is not in the allow-list and so is refused like any other foreign origin.
"""

_SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))
# ADFS posts the SAML assertion from the IdP's origin — the one legitimate
# cross-origin state change. Its authenticity rests on the signed assertion,
# not on where the POST came from.
_ORIGIN_EXEMPT = frozenset(("/saml/acs",))


def _normalize_origin(value: str) -> str:
    """scheme://host[:port], lowercased, no trailing slash or path."""
    value = value.strip().rstrip("/")
    if not value:
        return ""
    parts = value.split("://", 1)
    if len(parts) != 2:
        return ""
    scheme, rest = parts
    return f"{scheme.lower()}://{rest.split('/', 1)[0].lower()}"


def allowed_origins() -> frozenset[str]:
    """The app's own public origin, plus any extra names it answers to."""
    out = set()
    base = _normalize_origin(settings.saml_sp_base_url)
    if base:
        out.add(base)
    for extra in settings.trusted_origins.split(","):
        norm = _normalize_origin(extra)
        if norm:
            out.add(norm)
    return frozenset(out)


class OriginGuard:
    def __init__(self, app):
        self.app = app
        # Resolved once: settings do not change under a running process, and a
        # per-request rebuild would be pure waste on every mutation.
        self.allowed = allowed_origins()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        if scope["path"] in _ORIGIN_EXEMPT:
            await self.app(scope, receive, send)
            return

        origin = referer = ctype = clen = None
        for key, value in scope["headers"]:
            if key == b"origin":
                origin = value
            elif key == b"referer":
                referer = value
            elif key == b"content-type":
                ctype = value
            elif key == b"content-length":
                clen = value

        reason = ""
        if origin is not None:
            if _normalize_origin(origin.decode("latin-1")) not in self.allowed:
                reason = "cross-origin request rejected"
        elif referer is not None:
            if _normalize_origin(referer.decode("latin-1")) not in self.allowed:
                reason = "cross-site referer rejected"

        # FastAPI parses a body with NO Content-Type as JSON (routing.py:
        # `if not content_type_value: json_body = await request.json()`), and a
        # fetch() carrying an untyped Blob sends exactly that — a CORS-simple
        # request that skips the preflight a real JSON post would need. Refuse
        # the shape outright; every legitimate client labels its body.
        if not reason and ctype is None and clen not in (None, b"0"):
            reason = "request body without a Content-Type"

        if reason:
            body = b'{"detail":"' + reason.encode() + b'"}'
            await send({
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


class LangMiddleware:
    """Interface language for the request, from headers alone — pure ASGI, no
    I/O, no SQL.

    The cookie and Accept-Language are all this layer can see; the authoritative
    source (`users.lang`) rides the session JOIN that auth already runs and
    overrides this from app/auth/deps.py. That ordering is what keeps the
    anonymous pages (/login, /access-denied, /share/…) translated too.

    Ordering: `add_middleware` PREPENDS, so `create_app`'s registration order
    (OriginGuard, RenderFastLane, Lang, PerfLog) executes outermost-first as
    PerfLog -> Lang -> RenderFastLane -> OriginGuard. This layer therefore
    wraps the fast lane: a /render hit reaches THIS class before the lane can
    short-circuit it, so being "outside RenderFastLane" buys the lane nothing.
    `_LANG_SKIP_PREFIXES` is what actually keeps it free — /render answers the
    bytes of a report, never chrome, so it needs no language at all. Static
    assets skip for the same reason.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith(_LANG_SKIP_PREFIXES) or path in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        cookie = accept = None
        for key, value in scope["headers"]:
            if key == b"cookie":
                cookie = value
            elif key == b"accept-language":
                accept = value

        lang_cookie = None
        if cookie:
            for part in cookie.decode("latin-1").split(";"):
                name, _, val = part.partition("=")
                if name.strip() == i18n.COOKIE_NAME:
                    lang_cookie = val.strip()
                    break

        token = i18n.set_lang(i18n.negotiate(
            lang_cookie, accept.decode("latin-1") if accept else None, None))
        try:
            await self.app(scope, receive, send)
        finally:
            i18n.reset(token)
