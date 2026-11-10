from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings
from app.models import User, ROLE_ADMINISTRATOR

BASE_DIR = Path(__file__).resolve().parent.parent

# Process start, captured at import time (once per worker) — reported on
# /health so an operator can tell a fresh rollout from a container that has
# been quietly running for a week.
_STARTED_AT = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Lifespan: DB init, admin bootstrap, session GC
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse to boot into a configuration that cannot work. FIRST, before the
    # upload dir is created or any table is touched — the failure modes it
    # catches all boot "successfully" and answer /health with ok.
    from app.services.preflight import check_config, check_login_possible
    check_config()

    # Ensure upload dir exists
    settings.abs_uploads_dir.mkdir(parents=True, exist_ok=True)

    # Schema migrations: Alembic, under a file lock, before anything reads or
    # writes a row. See app/services/migrate.py — it also decides how a
    # database that predates Alembic is adopted. The wall of idempotent
    # ALTER TABLE / CREATE INDEX that used to sit here is now revision 0002.
    from app.services import migrate as _migrate
    _migrate.run()

    # Translation catalogues: read once, here, so no request ever touches the
    # filesystem for a string. ru is not a file — its keys ARE the source text.
    from app import i18n as _i18n
    _i18n.load()

    # Backfill dashboards.render_size for rows that predate the column
    # (one-time scan; afterwards the value is maintained at upload/mutation/
    # render time). Not a migration: it reads the uploads directory, not just
    # the database.
    from app.database import SessionLocal as _SL
    from app.models import Dashboard as _Dash
    from app.services import storage as _storage
    _db = _SL()
    try:
        _stale = _db.query(_Dash).filter(_Dash.render_size == None).all()  # noqa: E711
        for _d in _stale:
            _d.render_size = _storage.estimate_render_size(_d.id, _d.current_version)
        if _stale:
            _db.commit()
    finally:
        _db.close()

    # Data migration (exclude_rules -> input_overrides, observation backfill)
    # lives in services/lineage_migrate.run() — Phase A2.
    try:
        from app.services import lineage_migrate as _lm
        _lm.run()
    except ImportError:
        pass

    # Only now: SQLite enforces foreign keys for every connection opened from
    # here on (app/database.py). AFTER the schema migration — Alembic's batch
    # ALTER rebuilds a table by create-copy-drop-rename and enforcement would
    # refuse the drop — and after the data migrations above, which read and
    # rewrite rows written in the years the database had no enforcement at
    # all. Rows already in the file are not re-checked: tools/fk_orphans.py
    # does that, on a copy, before the deploy (docs/DEPLOY.md §2d).
    from app.database import enable_foreign_keys
    enable_foreign_keys()

    # Quests (ADR-17): apply the admin overlay (if any) at boot so every
    # worker starts with the same effective config, not just whichever one
    # happens to serve the first request after `_TTL_S` expires.
    try:
        from app.services import quests_config as _quests_config
        _quests_config.ensure_fresh(force=True)
    except Exception:
        pass

    # Bootstrap initial admin
    if settings.initial_admin:
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            # Through identity: an env value that is a UPN (`name@example.com`)
            # must find the numeric account it merged into, not create a
            # second administrator on every restart (prod 2026-08-28).
            from sqlalchemy.exc import IntegrityError

            from app.services import identity
            if identity.find_or_create(db, settings.initial_admin, create=False) is None:
                try:
                    identity.find_or_create(db, settings.initial_admin,
                                            role=ROLE_ADMINISTRATOR)
                    db.commit()
                    print(f"[auth] Bootstrapped initial admin: {settings.initial_admin}")
                except IntegrityError:
                    # A worker race, not an error. With OP_WORKERS > 1 several
                    # processes start at once, all of them see "no administrator"
                    # and all insert the row; the UNIQUE on person_id lets exactly
                    # one through. The loser used to crash at startup with
                    # IntegrityError — i.e. the server came up only partially, and
                    # which part depended on who got there first. Caught on CI
                    # (macos-14, 2026-11-03) by the "every racing worker boots" test;
                    # the race never reproduced on my machine.
                    #
                    # The desired outcome is already reached — the administrator
                    # exists, a sibling created it. Roll back our attempt and move on.
                    db.rollback()
                    print("[auth] Initial admin already created by another worker")
        finally:
            db.close()

    # Now that the bootstrap has had its chance: is there any way in at all?
    check_login_possible()

    # Background session GC + WAL hygiene
    from app.auth.session import session_store

    def _gc():
        import time
        from datetime import date
        from sqlalchemy import text
        from app.database import engine
        _last_prune_day = None
        while True:
            time.sleep(600)
            try:
                session_store.purge_expired()
                # Periodic TRUNCATE checkpoint: PASSIVE checkpoints can lag
                # under a steady write trickle and the -wal file grows without
                # bound. Runs off-loop; cheap no-op when the WAL is small.
                with engine.connect() as conn:
                    conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            except Exception:
                pass  # housekeeping must never kill the thread

            # Data Quality retention (P4.3): source_files/quality_results
            # thinning, once/day off the same tick — see quality.prune().
            today = date.today()
            if _last_prune_day != today:
                _last_prune_day = today
                try:
                    from app.database import SessionLocal
                    from app.services import quality
                    _db = SessionLocal()
                    try:
                        stats = quality.prune(_db)
                        print(f"[quality-prune] {stats}")
                    finally:
                        _db.close()
                except Exception as exc:
                    print(f"[quality-prune] FAILED: {type(exc).__name__}: {exc}")

                # Canonical-table inference (Leg 1), same daily tick. Every
                # source, under a time budget, with its own except: a folder
                # that grew pathological delays the catalogue, it does not
                # stall housekeeping. Off entirely when OP_CATALOG_ENABLED is
                # false.
                try:
                    from app.services import catalog
                    catalog.rebuild_safely(budget_s=30.0)
                except Exception as exc:
                    print(f"[catalog-rebuild] FAILED: {type(exc).__name__}: {exc}")

    t = threading.Thread(target=_gc, daemon=True)
    t.start()

    # Background perf-log writer (JSONL, see app/services/perflog.py)
    from app.services import perflog
    perflog.start()

    # Background render-cache warm-up (users never pay a cold miss)
    from app.services import warm
    warm.warm_startup()

    # GC-pause telemetry (audit F11): measure gen1/gen2 loop-thread stalls
    from app.services import gcwatch
    gcwatch.start()

    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(title="OnePage", lifespan=lifespan, docs_url=None, redoc_url=None)

    # Compress responses (HTML render, JSON APIs, slot data). Starlette's GZip
    # responder skips bodies that already carry a Content-Encoding (e.g. the
    # pre-compressed *.br slot files served by /render/{id}/files/{slot}), so it
    # never double-encodes them.
    from fastapi.middleware.gzip import GZipMiddleware
    # Level 6: ~3-5x less CPU than the default 9 for ~2-5% size cost. Heavy
    # bodies (/render) bypass this middleware entirely — they negotiate their
    # own Content-Encoding via app/services/encoding.py in the threadpool.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

    # Warm authenticated /render/{id}/ hits served straight from the ASGI
    # layer — no FastAPI routing/DI on the hottest path. Anything but a clean
    # warm hit falls through to the full route. Sits inside PerfLog (so it is
    # timed + logged) and outside GZip (its responses are pre-encoded).
    from app.middleware import LangMiddleware, OriginGuard, PerfLogMiddleware, RenderFastLane

    # Reject state-changing requests carrying a foreign Origin — the CSRF
    # defence the SameSite=None session cookie cannot provide. Registered
    # BEFORE RenderFastLane on purpose: that makes it the INNER of the two, so
    # a warm /render hit is answered by the lane and never reaches this code.
    if settings.origin_guard:
        app.add_middleware(OriginGuard)

    app.add_middleware(RenderFastLane)

    # Interface language from cookie + Accept-Language, headers only.
    # add_middleware prepends, so this executes OUTSIDE RenderFastLane (added
    # earlier) and INSIDE PerfLog (added later): PerfLog -> Lang -> lane ->
    # OriginGuard. Wrapping the lane means a warm /render hit WOULD pay for it,
    # so LangMiddleware skips "/render/" itself (_LANG_SKIP_PREFIXES); being
    # inside PerfLog is what puts the ContextVar in place for everything it
    # times.
    app.add_middleware(LangMiddleware)

    # Request timing → perf log. Added last = outermost, so the measured
    # duration covers routing + handler + gzip.
    app.add_middleware(PerfLogMiddleware)

    # Jinja2
    templates_dir = BASE_DIR / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        auto_reload=settings.templates_auto_reload,
    )

    class _TemplateSettings:
        """`settings` for templates. Reading `quests_enabled` first re-applies
        the admin overlay (<= 1 s TTL, one stat), so a switch made at
        /admin/quests by one worker reaches the sidebar of the others."""

        def __getattr__(self, name):
            if name == "quests_enabled":
                try:
                    from app.services import quests_config as _qc
                    _qc.ensure_fresh()
                except Exception:
                    pass
            return getattr(settings, name)

    env.globals["settings"] = _TemplateSettings()

    # i18n: `{{ _('…') }}` in templates, `{{ _n(n, '…|…') }}` for plurals.
    # newstyle=False on purpose — the newstyle wrapper runs `%`-formatting over
    # EVERY translated string, which both ignores our `{name}` placeholders and
    # blows up on any text containing a literal `%`. With it off, `_` calls our
    # gettext directly and the kwargs reach `str.format_map`.
    from app import i18n
    env.add_extension("jinja2.ext.i18n")
    env.install_gettext_callables(
        i18n.gettext,
        i18n.jinja_ngettext,   # `{% trans %}`'s (singular, plural, n) shape
        newstyle=False,
    )
    env.globals["_"] = i18n.gettext
    env.globals["_n"] = i18n.ngettext
    env.globals["_h"] = i18n.gettext_html
    env.globals["current_lang"] = i18n.current_lang
    env.globals["LANGS"] = i18n.LANGS
    env.globals["LANG_LABELS"] = i18n.LANG_LABELS
    env.globals["LANG_SHORT"] = i18n.LANG_SHORT

    # Static files
    static_dir = BASE_DIR / "static"

    # Cache-bust token for ?v= asset links: max mtime across static files, so it
    # changes exactly when an asset changes (stable across plain restarts).
    asset_v = 0
    for f in static_dir.rglob("*"):
        if f.is_file():
            asset_v = max(asset_v, int(f.stat().st_mtime))
    env.globals["asset_v"] = asset_v
    app.state.jinja = env

    class CachedStaticFiles(StaticFiles):
        """Versioned (?v=) asset links and fonts are immutable for a week;
        everything else gets a short window + ETag revalidation."""
        async def get_response(self, path: str, scope):
            resp = await super().get_response(path, scope)
            if resp.status_code in (200, 304):
                if scope.get("query_string") or path.startswith("fonts/"):
                    resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
                else:
                    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
            return resp

    app.mount("/static", CachedStaticFiles(directory=str(static_dir)), name="static")

    # Routers
    from app.api.auth import router as auth_router
    from app.api.me import router as me_router
    from app.api.dashboards import router as dash_router
    from app.api.files import router as files_router
    from app.api.permissions import router as perms_router
    from app.api.admin import router as admin_router
    from app.api.share import router as share_router
    from app.api.perf import router as perf_router
    from app.api.folders import router as folders_router
    from app.api.generators import router as generators_router
    from app.api.quality import router as quality_router
    from app.api.catalog import router as catalog_router
    from app.api.explore import router as explore_router
    from app.api.glossary import router as glossary_router
    from app.api.favorites import router as favorites_router
    from app.api.reference import router as reference_router
    from app.api.presentations import router as presentations_router
    from app.api.navigators import router as navigators_router
    from app.api.employees import router as employees_router
    from app.api.scopes import router as scopes_router
    from app.api.lake import router as lake_router
    from app.api.learn import router as learn_router
    from app.api.quests import router as quests_router, admin_router as quests_admin_router
    from app.api.sync import router as sync_router

    app.include_router(auth_router)
    app.include_router(me_router)
    app.include_router(dash_router)
    app.include_router(files_router)
    app.include_router(perms_router)
    app.include_router(admin_router)
    app.include_router(share_router)
    app.include_router(perf_router)
    app.include_router(folders_router)
    app.include_router(generators_router)
    app.include_router(quality_router)
    app.include_router(catalog_router)
    app.include_router(explore_router)
    app.include_router(glossary_router)
    app.include_router(favorites_router)
    app.include_router(reference_router)
    app.include_router(presentations_router)
    app.include_router(navigators_router)
    app.include_router(employees_router)
    app.include_router(scopes_router)
    app.include_router(lake_router)
    app.include_router(learn_router)
    app.include_router(quests_router)
    app.include_router(quests_admin_router)
    app.include_router(sync_router)

    # Health check
    @app.get("/health")
    async def health(deep: int = 0):
        """Unauthenticated health probe.

        Default (`/health`) is a liveness stub: the process is up and the event
        loop answers. Cheap enough for the bench runner to poll in a tight loop,
        and excluded from perf logging (middleware._SKIP_PATHS).

        `/health?deep=1` also proves the instance can actually SERVE: it reads
        from SQLite and writes into the uploads directory. A process with a
        corrupt database or an unmounted volume passes the stub and fails this —
        which is the whole point, since that is exactly the state a first deploy
        lands in.

        Residual caveat under `network_mode: host` (docs/DEPLOY.md): the
        container shares the host network namespace, so a probe of
        localhost:8743 can reach ANY host process on that port. The deep mode
        does not fully close that hole, but it does turn "something is bound to
        8743" into "an OnePage instance holding our database answered".
        """
        if not deep:
            return {
                "status": "ok",
                "build": settings.build_sha,
                "started_at": _STARTED_AT,
            }

        checks: dict[str, str] = {}
        try:
            from sqlalchemy import text as _text
            from app.database import engine as _eng
            with _eng.connect() as _c:
                _c.execute(_text("SELECT 1 FROM users LIMIT 1")).fetchall()
            checks["db"] = "ok"
        except Exception as exc:
            checks["db"] = f"fail: {type(exc).__name__}"
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(dir=settings.abs_uploads_dir,
                                             prefix=".op-health-"):
                pass
            checks["uploads"] = "ok"
        except Exception as exc:
            checks["uploads"] = f"fail: {type(exc).__name__}"

        healthy = all(v == "ok" for v in checks.values())
        # Warm-up runs on a background daemon thread and never gates readiness;
        # "ok" means serveable, not "cache is hot".
        return JSONResponse(
            {
                "status": "ok" if healthy else "degraded",
                "build": settings.build_sha,
                "started_at": _STARTED_AT,
                "checks": checks,
            },
            status_code=200 if healthy else 503,
        )

    # Global 401 redirect to login
    @app.exception_handler(401)
    async def unauthorized_handler(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        if request.url.path.startswith("/render/"):
            return HTMLResponse(
                "<body style='font:13px sans-serif;padding:20px;background:#0d1117;color:#8b949e'>"
                "Session expired. <a href='/login' style='color:#ffb000'>Log in</a> and return to the dashboard.</body>",
                status_code=401,
            )
        from fastapi.responses import RedirectResponse
        return RedirectResponse(f"/login?next={request.url.path}", status_code=302)

    return app


app = create_app()
