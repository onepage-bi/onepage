from __future__ import annotations

import functools
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import (
    can_update_dashboard_data,
    get_current_user, require_permission,
    get_dashboard_or_404, require_dashboard_access, require_dashboard_view,
)
from app import i18n
from app.config import settings
from app.database import get_db
from app.models import (
    AuditLog, Dashboard, DashboardFile, DashboardVersion,
    User, ROLE_ADMINISTRATOR,
    DASH_INTERACTIVE, DASH_PAGE, DASH_SNAPSHOT, DASH_STATIC,
)
from app.services import detector, docs_artifacts, perflog, rls, storage

router = APIRouter()

_MAX_UPLOAD = 50 * 1024 * 1024  # 50 MB

# A tag-shaped byte sequence near the start. Not a security boundary (the
# render iframe is sandboxed regardless) — this catches the real accident
# observed in prod: a 0-byte or wrong-format file silently published as the
# current version, blanking a working report.
_RE_HTML_TAG = re.compile(rb"<[A-Za-z!]")


def _validate_html_upload(html_bytes: bytes) -> None:
    if len(html_bytes) > _MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="File too large (max 50 MB)")
    stripped = html_bytes.strip()
    if not stripped:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is empty (0 bytes) — the report was not replaced. "
                   "Re-export the dashboard HTML and try again.")
    if not _RE_HTML_TAG.search(stripped[:65536]):
        raise HTTPException(
            status_code=400,
            detail="Uploaded file does not look like HTML (no tags found) — "
                   "the report was not replaced.")


class DashboardUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    is_published: bool | None = None
    is_public: bool | None = None
    # None is a real value here (= move to root); presence is checked via
    # model_fields_set in the handler.
    folder_id: int | None = None


def _make_slug(title: str, db: Session) -> str:
    base = re.sub(r'[^\w\s-]', '', title.lower())
    base = re.sub(r'[\s_-]+', '-', base).strip('-')[:80] or "dashboard"
    slug = base
    n = 1
    while db.query(Dashboard).filter(Dashboard.slug == slug).first():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _log(db: Session, actor: User, action: str, resource_type: str, resource_id: str, detail: dict, ip: str = ""):
    entry = AuditLog(
        actor_id=actor.id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        ip_address=ip,
    )
    entry.set_detail(detail)
    db.add(entry)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

# Collection season opening: one clip on the first visit to the report list.
# Lives here, not in `base.html`, so that neither a report, a presentation nor
# a share link pays a single byte for it or even reads the active season.
# The clip file and the credit are data, not code: changing the season means
# changing this table, not touching the template.
# The table itself is deployment data and lives in an optional module that is
# not part of the public tree; without it there is no intro.
try:
    from app.services.season_intros import SEASON_INTROS as _SEASON_INTROS
except ImportError:
    _SEASON_INTROS: dict[int, dict] = {}


@functools.lru_cache(maxsize=8)
def _intro_clip_present(name: str) -> bool:
    """The clip is third-party media and is absent from the public tree.

    Checked once per process: no file, no intro — never a broken player.
    """
    return (Path(__file__).resolve().parents[2] / "static" / "video" / name).is_file()


def _season_intro() -> dict | None:
    """The story block for the report list — or `None` if the season has none.

    An overlay read error is swallowed on purpose: not showing the story is
    harmless, breaking the home page over a broken json is not.
    """
    if not settings.quests_enabled:
        return None
    try:
        from app.services import quests
        intro = _SEASON_INTROS.get(quests.active_season()["key"])
        if intro is None or not _intro_clip_present(intro["video"]):
            return None
        intro = dict(intro)
        credit = intro.pop("credit_html_by_lang", None) or {}
        intro["credit_html"] = credit.get(i18n.current_lang()) or credit.get("en", "")
        return intro
    except Exception:
        return None


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from sqlalchemy import and_, func, or_, select
    from sqlalchemy.orm import joinedload, load_only

    from app.models import (
        DashboardFavorite, DashboardPermission, Folder, GenerationRun,
        Presentation, PresentationItem, SourceObservation,
    )

    with perflog.span("query"):
        # Single round trip: owner eager-loaded via JOIN, access rules folded
        # into one WHERE (owned OR public OR granted+published) instead of the
        # old permission-list materialization + double UNION.
        q = db.query(Dashboard).options(
            joinedload(Dashboard.owner).load_only(User.person_id)
        # Navigators live in their own section: in the report list they would be
        # reports that cannot be updated and have nothing to look at under
        # data. Filtered by type, not by a separate table — see DASH_PAGE.
        ).filter(Dashboard.dash_type != DASH_PAGE)
        if user.role != ROLE_ADMINISTRATOR:
            perm_sq = (
                select(DashboardPermission.dashboard_id)
                .where(DashboardPermission.user_id == user.id)
                .scalar_subquery()
            )
            q = q.filter(or_(
                Dashboard.owner_id == user.id,
                Dashboard.is_public == True,
                and_(Dashboard.id.in_(perm_sq), Dashboard.is_published == True),
            ))
        dashboards = q.order_by(Dashboard.updated_at.desc()).all()
        folders = db.query(Folder).order_by(Folder.position, Folder.name).all()

        # "My reports": (a) this user's own favourites — {dashboard_id: position}.
        fav_map = {
            did: pos for did, pos in
            db.query(DashboardFavorite.dashboard_id, DashboardFavorite.position)
              .filter(DashboardFavorite.user_id == user.id).all()
        }
        # (b) data_at per dashboard = newest source mtime across its runs, only
        # for generator-backed dashboards on THIS page — skipped entirely when
        # none of the listed dashboards has a generator (the common case for a
        # page of hand-uploaded reports costs nothing extra).
        gen_dash_ids = [d.id for d in dashboards if d.generator_id]
        data_at_map: dict[str, object] = {}
        if gen_dash_ids:
            data_at_map = {
                did: newest for did, newest in
                db.query(GenerationRun.dashboard_id, func.max(SourceObservation.newest_mtime))
                  .join(SourceObservation, SourceObservation.run_id == GenerationRun.id)
                  .filter(GenerationRun.dashboard_id.in_(gen_dash_ids))
                  .group_by(GenerationRun.dashboard_id)
                  .all()
                if newest is not None
            }

        # (c) the user's own presentations, for the "Presentations" section —
        # ONE query: an outer join to the slides with a grouped COUNT, never a
        # COUNT per deck. `stale_count` is deliberately NOT computed here (v1):
        # it would need the per-slide freshness walk `/check` does, and that is
        # a query per report on a page whose whole budget is six.
        pres_rows = (
            db.query(Presentation.id, Presentation.title, Presentation.updated_at,
                     func.count(PresentationItem.id))
              .outerjoin(PresentationItem,
                         PresentationItem.presentation_id == Presentation.id)
              .filter(Presentation.owner_id == user.id)
              .group_by(Presentation.id, Presentation.title, Presentation.updated_at)
              .order_by(Presentation.updated_at.desc())
              .all()
        )

    # One pass: filter-chip counts + the grid payload (grid cards are built
    # client-side from this JSON instead of a second server-rendered DOM tree)
    # + folder buckets for the sectioned table.
    counts = {DASH_INTERACTIVE: 0, DASH_SNAPSHOT: 0, DASH_STATIC: 0, "published": 0, "draft": 0}
    items = []
    buckets: dict[int | None, list[Dashboard]] = {None: []}
    for f in folders:
        buckets[f.id] = []
    for d in dashboards:
        counts[d.dash_type] = counts.get(d.dash_type, 0) + 1
        counts["published" if d.is_published else "draft"] += 1
        buckets.setdefault(d.folder_id, buckets[None]).append(d)
        data_at = data_at_map.get(d.id)
        items.append({
            "id": d.id,
            "title": d.title,
            "desc": d.description or "",
            "type": d.dash_type,
            "pub": d.is_published,
            "owner": d.owner.person_id if d.owner else "",
            "ver": d.current_version,
            "upd": d.updated_at.strftime("%Y-%m-%d"),
            "folder": d.folder_id,
            "size": d.render_size or 0,
            "fav": fav_map.get(d.id),
            "pub_at": d.updated_at.strftime("%Y-%m-%d"),
            "data_at": data_at.strftime("%Y-%m-%d") if data_at else None,
            # "Strictly confidential": the live preview of such a report is
            # /render/{id}/, which now answers 403 until the password is entered.
            # Without the flag the tile would show an empty frame and pile up 403s in
            # /admin/errors. The field rides on the already loaded row, the home page
            # gains no queries (the six-query budget is intact).
            "conf": bool(d.conf_hash),
        })

    # Root ("Unsorted") first, then folders in manual order. Empty folders are
    # kept only for users who can manage them (visitors never see them).
    can_manage_folders = user.role in (ROLE_ADMINISTRATOR, "superuser")
    sections = [(None, buckets[None])]
    for f in folders:
        if buckets[f.id] or can_manage_folders:
            sections.append((f, buckets[f.id]))

    dash_payload = {
        "items": items,
        "folders": [{"id": f.id, "name": f.name} for f, lst in sections[1:]],
        "preview_max": settings.preview_max_bytes,
        # "Presentations" — the page renders the section only when non-empty;
        # /presentations is where an empty workspace starts its first deck.
        "presentations": [{
            "id": pid,
            "title": title,
            "items_count": int(cnt or 0),
            "updated_at": upd.strftime("%Y-%m-%d") if upd else None,
        } for pid, title, upd, cnt in pres_rows],
    }

    env: Environment = request.app.state.jinja
    with perflog.span("render"):
        html = await run_in_threadpool(env.get_template("pages/index.html").render,
            user=user, dashboards=dashboards, counts=counts, sections=sections,
            can_manage_folders=can_manage_folders, season_intro=_season_intro(),
            dash_payload=dash_payload, request=request,
        )
    return HTMLResponse(html)


@router.get("/themes", response_class=HTMLResponse)
async def themes_page(
    request: Request,
    user: User = Depends(require_permission("upload_dashboard")),
    db: Session = Depends(get_db),
):
    # Recolouring edits the dashboard's source theme, so only reports the user
    # can manage are listed: their own (admins see all).
    if user.role == ROLE_ADMINISTRATOR:
        dashboards = db.query(Dashboard).order_by(Dashboard.updated_at.desc()).all()
    else:
        dashboards = db.query(Dashboard).filter(
            Dashboard.owner_id == user.id
        ).order_by(Dashboard.updated_at.desc()).all()
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(env.get_template("pages/themes.html").render,
        user=user, dashboards=dashboards, request=request,
    ))


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(
    request: Request,
    user: User = Depends(require_permission("upload_dashboard")),
):
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(env.get_template("pages/upload.html").render, user=user, request=request))


@router.get("/docs", response_class=HTMLResponse)
async def docs_page(
    request: Request,
    user: User = Depends(get_current_user),
):
    env: Environment = request.app.state.jinja
    # The copyable artifacts are files, not catalogue keys — see
    # app/services/docs_artifacts.py for why.
    artifacts = docs_artifacts.load(i18n.current_lang())
    return HTMLResponse(await run_in_threadpool(
        env.get_template("pages/docs.html").render,
        user=user, request=request, artifacts=artifacts))


@router.get("/anonymizer", response_class=HTMLResponse)
async def anonymizer_page(
    request: Request,
    user: User = Depends(get_current_user),
):
    # Client-side only tool: masking happens entirely in the browser.
    # The server just serves the static page — no upload endpoint, no data touches the server.
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(env.get_template("pages/anonymizer.html").render, user=user, request=request))


@router.get("/d/{dashboard_id}", response_class=HTMLResponse)
async def viewer_page(
    request: Request,
    dashboard: Dashboard = Depends(require_dashboard_view),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The report page.

    The dependency here is `require_dashboard_view`, not `require_dashboard_access`,
    for exactly one reason: for a "strictly confidential" report this page
    must show the password form, not a 403. Everything else — serving, slots,
    the lake, the source — goes through `require_dashboard_access`, which does
    not hand out a locked report. So the page is the only window into the locked
    room, and there is nothing in it but the lock.
    """
    from app.services import confidential as conf
    if conf.enabled(dashboard):
        from app.auth.session import get_session_id_from_cookie
        sid = get_session_id_from_cookie(request.headers.get("cookie", ""))
        if not conf.is_unlocked(db, sid, dashboard):
            env_lock: Environment = request.app.state.jinja
            return HTMLResponse(await run_in_threadpool(
                env_lock.get_template("pages/confidential.html").render,
                user=user, dashboard=dashboard, request=request))

    from app.models import (
        DashboardPermission, Generator, GeneratorVersion, GenerationRun,
        SourceObservation, DataSource,
    )
    from app.services import genmanifest, lineage, schemadiff
    is_owner = (user.role == ROLE_ADMINISTRATOR or dashboard.owner_id == user.id)

    can_update_data = is_owner
    if not can_update_data:
        perm = db.query(DashboardPermission).filter(
            DashboardPermission.dashboard_id == dashboard.id,
            DashboardPermission.user_id == user.id,
        ).first()
        can_update_data = bool(perm and perm.can_update_data)
    # Owner decision 2026-09-04: «can update data» opens the whole right dock
    # (settings, access, data, versions, share, annotate, colors, text) — the
    # colleague is trusted to maintain the report. Only deleting it stays with
    # the owner/administrator (`is_owner` gates the Danger zone).
    can_manage = can_update_data

    # "Chat" beside the report (phase 4). Both halves are already computed —
    # a generated report is the only thing the chat has semantics for, and the
    # people who may discuss it are the people who may re-run its data. ZERO
    # new queries: this is the hot `/d/{id}` path.
    # The pane embeds /explore, so it goes away with OP_EXPLORE_ENABLED.
    can_discuss = (settings.explore_enabled and bool(dashboard.generator_id)
                   and can_update_data)

    # ADR-18. `rls_json` is already on the loaded dashboard row, so an ordinary
    # report gets zero new queries here — the /d/{id} hot path rule is kept
    # literally. Only rls.subject() queries user_scopes, and only for a report
    # with a policy.
    rls_enabled = dashboard.rls_enabled
    rls_see_all = True
    if rls_enabled:
        rls_see_all = rls.subject(db, dashboard, user).see_all
    # A viewer with their own territory gets neither share, nor slot upload,
    # nor the source — the template hides exactly what the backend would 403 anyway
    # (the rest of the dock stays working if the "update data" right is there).
    rls_scoped = rls_enabled and not rls_see_all
    if rls_scoped:
        # The chat reads the lake, and the lake is not filtered (ADR-18) — it is closed.
        can_discuss = False
    # "Strictly confidential": the chat and server queries are closed to everyone
    # (owner decision 2026-10-20) — they read the same rows by another path.
    confidential_mode = conf.enabled(dashboard)
    if confidential_mode:
        can_discuss = False

    slots = storage.list_slot_files(dashboard.id, dashboard.current_version)
    slots_json = json.dumps(slots)

    # Generator provenance + last run, for the Data-dock's "Generator" section
    # (docs/RFC_GENERATORS_RESEARCH.md E.5). One joined query for the current
    # version + its author (mirrors /api/generators' listing query), plus one
    # query for the latest run — no N+1.
    generator_ctx = None
    if dashboard.generator_id:
        row = (db.query(Generator, GeneratorVersion, User.person_id)
               .join(GeneratorVersion,
                     (GeneratorVersion.generator_id == Generator.id)
                     & (GeneratorVersion.is_current == True))  # noqa: E712
               .outerjoin(User, User.id == GeneratorVersion.created_by)
               .filter(Generator.id == dashboard.generator_id)
               .first())
        if row is not None:
            gen, ver, author_person_id = row
            manifest = ver.get_manifest()
            # A few recent runs, not just the newest: the latest attempt may have
            # died before it recorded anything, and the question "which folders
            # does this report read?" should still have an answer. Same index,
            # same single query — LIMIT 5 instead of 1.
            recent = (db.query(GenerationRun)
                      .filter(GenerationRun.dashboard_id == dashboard.id)
                      .order_by(GenerationRun.started_at.desc())
                      .limit(5).all())
            last_run = recent[0] if recent else None

            # Which of the recent runs actually recorded observations (Data
            # Lineage v2): the most recent one with source_observations rows,
            # not just the newest attempt — a run that died before ingest, or
            # the agent engine on its SQL path (no observations by design —
            # a native folder run `local` does post them), should not blank
            # out an answer a colleague already has from an earlier run.
            obs_run = None
            for r in recent:
                if db.query(SourceObservation.id).filter(SourceObservation.run_id == r.id).first():
                    obs_run = r
                    break

            # Declared in the frozen manifest, so this is true before any run —
            # required_files sugar is folded in, one shape for the diff below.
            inputs_decl = genmanifest.inputs_for(manifest)
            input_dirs, dirs_at, observed_diff = [], None, None

            if obs_run is not None:
                # Folder identity from data_sources.key_name, not the run's raw
                # JSON — the registry row is what /data-sources lets a person
                # correct (declared path, label), so this now agrees with it.
                obs_rows = (db.query(SourceObservation.id, SourceObservation.data_source_id,
                                      DataSource.key_name)
                            .join(DataSource, DataSource.id == SourceObservation.data_source_id)
                            .filter(SourceObservation.run_id == obs_run.id)
                            .all())
                input_dirs = sorted({key_name for _, _, key_name in obs_rows})[:8]
                dirs_at = obs_run.started_at
                if inputs_decl and obs_rows:
                    # A cold read only records schema_json for files that
                    # changed since the previous observation of the SAME
                    # source (gen-engine's `last_known` skip) — a warm run
                    # leaves it NULL on every unchanged file, which is not
                    # "no schema", it is "same schema as last time". Reuse
                    # lineage.py's chain walk-back instead of re-deriving it:
                    # two call sites computing "unchanged since" independently
                    # is exactly the kind of drift Data Lineage v2 exists to
                    # avoid.
                    ds_ids = {ds_id for _, ds_id, _ in obs_rows}
                    run_ids = [r.id for r in recent]
                    run_rank = {r.id: i for i, r in enumerate(recent)}
                    chain_key = gen.name
                    chains: dict[tuple, list] = {}
                    for obs in (db.query(SourceObservation)
                                .filter(SourceObservation.run_id.in_(run_ids),
                                        SourceObservation.data_source_id.in_(ds_ids))
                                .all()):
                        chains.setdefault((chain_key, obs.data_source_id), []).append(obs)
                    for chain in chains.values():
                        chain.sort(key=lambda o: run_rank.get(o.run_id, len(recent)))
                    files_by_source = lineage._collect_files(db, chains)
                    files_for_diff = [f for flist in files_by_source.values() for f in flist]
                    observed_diff = schemadiff.diff(inputs_decl, files_for_diff)
            else:
                # Old JSON fallback (pre-Data-Lineage-v2 rows, or a build that
                # has not ingested an observation for this dashboard yet):
                # folder names are still readable straight off input_manifest.
                for r in recent:
                    if not r.input_manifest:
                        continue
                    try:
                        im = json.loads(r.input_manifest)
                    except (ValueError, TypeError):
                        continue
                    d = im.get("dirs") if isinstance(im, dict) else None
                    if isinstance(d, list) and d:
                        input_dirs = [str(x) for x in d[:8]]
                        dirs_at = r.started_at
                        break

            aux_names = [str(a.get("name")) for a in (manifest.get("aux_inputs") or [])
                         if isinstance(a, dict) and a.get("name")][:3]

            generator_ctx = {
                "name": gen.name,
                "version_number": ver.version_number,
                "version_label": manifest.get("version"),
                "engine": manifest.get("engine"),
                "created_at": ver.created_at,
                "author": author_person_id,
                "last_run": last_run,
                "input_dirs": input_dirs,
                "input_dirs_at": dirs_at,
                "inputs_decl": inputs_decl,
                "observed_diff": observed_diff,
                "observed_at": dirs_at.strftime("%Y-%m-%d") if (obs_run is not None and dirs_at) else None,
                "aux_names": aux_names,
            }

    # Show mode: `/d/{id}?pres=<pid>&i=<k>` draws the presenter bar above the
    # viewer bar. The report itself is untouched — a presentation is routing,
    # not a second rendering surface. Nothing here runs (and no query is made)
    # for a plain `/d/{id}`, so the viewer's query budget is unchanged.
    pres_ctx = None
    pres_id = request.query_params.get("pres")
    if pres_id:
        from app.api import presentations as pres_api
        try:
            slide_i = int(request.query_params.get("i") or 0)
        except (TypeError, ValueError):
            slide_i = -1
        # None (not this user's deck, unknown id, `i` out of range) means the
        # params are simply ignored — a bad link opens the report, not an error.
        pres_ctx = pres_api.show_context(db, user, pres_id, slide_i)

    # Version history: the "Versions" panel looped over `versions`, which the
    # page never received — i.e. it promised a rollback that did not exist.
    # One query, and only for those allowed to change the report.
    versions = []
    if can_manage:
        rows = (db.query(DashboardVersion, User.display_name, User.person_id)
                .outerjoin(User, User.id == DashboardVersion.created_by)
                .filter(DashboardVersion.dashboard_id == dashboard.id,
                        DashboardVersion.version_number != dashboard.current_version)
                .order_by(DashboardVersion.version_number.desc())
                .limit(20).all())
        versions = [{"number": v.version_number, "note": v.note,
                     "created_by": name or oid or "—", "created_at": v.created_at}
                    for v, name, oid in rows]

    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(env.get_template("pages/viewer.html").render,
        user=user, dashboard=dashboard, request=request, versions=versions,
        can_manage=can_manage, can_update_data=can_update_data, is_owner=is_owner,
        can_discuss=can_discuss,
        rls_enabled=rls_enabled, rls_see_all=rls_see_all, rls_scoped=rls_scoped,
        confidential=confidential_mode,
        slots=slots, slots_json=slots_json,
        generator_ctx=generator_ctx, pres_ctx=pres_ctx,
    ))


# ---------------------------------------------------------------------------
# Provenance — "where did these numbers come from" for one report version. Read-only,
# counts/hashes/timestamps only (never a data value). require_dashboard_
# access gates the route (can this user open the report at all); the builder
# itself re-derives the narrower can_update_data / RLS see_all gates the rest
# of the platform uses for this same run/generator/lake data, so a viewer
# with plain read access sees the version record and reference-table uses
# only — see app/services/provenance.py for the full rationale.
# ---------------------------------------------------------------------------

def _parse_version_qs(request: Request) -> int | None:
    raw = request.query_params.get("v")
    if raw is None:
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="v must be an integer version number")
    # Reject implausible magnitudes before they reach a SQLite integer bind
    # (SQLite integers are 64-bit signed; Python int has no such bound, so a
    # huge literal like `?v=999...9` would otherwise raise an unhandled
    # OverflowError deep in the driver instead of a clean 404).
    if not 0 < v < 2**31:
        raise HTTPException(status_code=404, detail="Version not found")
    return v


@router.get("/api/dashboards/{dashboard_id}/provenance")
async def api_dashboard_provenance(
    request: Request,
    dashboard: Dashboard = Depends(require_dashboard_access),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services import provenance

    vnum = _parse_version_qs(request)
    data = await run_in_threadpool(provenance.build, db, dashboard, vnum, user)
    if data is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return data


@router.get("/d/{dashboard_id}/provenance", response_class=HTMLResponse)
async def provenance_page(
    request: Request,
    dashboard: Dashboard = Depends(require_dashboard_access),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services import provenance

    vnum = _parse_version_qs(request)
    data = await run_in_threadpool(provenance.build, db, dashboard, vnum, user)
    if data is None:
        raise HTTPException(status_code=404, detail="Version not found")

    env: Environment = request.app.state.jinja
    html_out = await run_in_threadpool(
        env.get_template("pages/provenance.html").render,
        user=user, request=request, dashboard=dashboard, data=data,
    )
    return HTMLResponse(html_out)


# ---------------------------------------------------------------------------
# API — create
# ---------------------------------------------------------------------------

@router.post("/api/dashboards")
async def create_dashboard(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    html_file: UploadFile = File(...),
    data_file_0: UploadFile = File(None),
    data_file_1: UploadFile = File(None),
    data_file_2: UploadFile = File(None),
    user: User = Depends(require_permission("upload_dashboard")),
    db: Session = Depends(get_db),
):
    html_bytes = await html_file.read()
    _validate_html_upload(html_bytes)

    html_str = html_bytes.decode("utf-8", errors="replace")
    dash_type = detector.detect_type(html_str)
    slot_count = detector.count_file_slots(html_str) if dash_type == DASH_INTERACTIVE else 0

    dash_id = str(uuid.uuid4())
    slug = _make_slug(title, db)
    version = 1

    # Save HTML
    storage.init_dashboard(dash_id)
    html_path = storage.save_html(dash_id, version, html_bytes)

    # Create DB record
    dash = Dashboard(
        id=dash_id, slug=slug, title=title,
        description=description or None,
        dash_type=dash_type, owner_id=user.id,
        slot_count=slot_count, current_version=version,
    )
    db.add(dash)

    ver = DashboardVersion(
        dashboard_id=dash_id, version_number=version,
        html_path=html_path, created_by=user.id,
    )
    db.add(ver)

    # Save optional data files
    data_files = [f for f in [data_file_0, data_file_1, data_file_2] if f and f.filename]
    for slot_idx, df in enumerate(data_files):
        if slot_idx >= slot_count and dash_type == DASH_INTERACTIVE:
            break
        content = await df.read()
        if not content:
            continue
        stored = storage.save_slot_file(dash_id, version, slot_idx, df.filename, content)
        db.add(DashboardFile(
            dashboard_id=dash_id, version_number=version,
            slot_index=slot_idx, original_name=df.filename,
            stored_path=stored, size_bytes=len(content),
        ))

    # Snapshot JSON: promote the first data file to snapshot.json (inlined at
    # render) ONLY when the page ships an empty bootstrap-snapshot placeholder.
    # If the page already bakes its snapshot inline, the data file stays a plain
    # slot delivered to the iframe via postMessage — not re-inlined (this is how
    # heavy out-of-HTML datasets avoid bloating the rendered page).
    if dash_type == DASH_SNAPSHOT and data_files and detector.snapshot_inline_is_empty(html_str):
        slot_content = storage.read_slot_file(dash_id, version, 0)
        if slot_content:
            storage.save_snapshot_json(dash_id, version, slot_content[0])

    dash.render_size = storage.estimate_render_size(dash_id, version)
    _log(db, user, "upload", "dashboard", dash_id, {"title": title, "type": dash_type})
    db.commit()

    return RedirectResponse(f"/d/{dash_id}", status_code=303)


# ---------------------------------------------------------------------------
# API — update metadata
# ---------------------------------------------------------------------------

@router.patch("/api/dashboards/{dashboard_id}")
async def update_dashboard(
    dashboard_id: str,
    body: DashboardUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dash = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dash:
        raise HTTPException(status_code=404)
    if not can_update_dashboard_data(dash, user, db):  # settings follow the update right
        raise HTTPException(status_code=403)

    content_changed = False
    if body.title is not None:
        dash.title = body.title
        content_changed = True
    if body.description is not None:
        dash.description = body.description
        content_changed = True
    if body.is_published is not None:
        dash.is_published = body.is_published
        content_changed = True
    if body.is_public is not None:
        dash.is_public = body.is_public
        content_changed = True

    if "folder_id" in body.model_fields_set:
        if body.folder_id is not None:
            from app.models import Folder
            if not db.query(Folder.id).filter(Folder.id == body.folder_id).first():
                raise HTTPException(status_code=400, detail="Folder not found")
        dash.folder_id = body.folder_id
        _log(db, user, "move_to_folder", "dashboard", dashboard_id, {"folder_id": body.folder_id})

    # A folder move alone must not bump updated_at (it would reshuffle the
    # recency-sorted list for everyone). The column has onupdate=func.now(),
    # which fires whenever the column is absent from the UPDATE — assigning the
    # unchanged value is a no-op for SQLAlchemy history, so the column must be
    # force-marked dirty to pin its current value.
    if content_changed:
        dash.updated_at = datetime.utcnow()
        _log(db, user, "update", "dashboard", dashboard_id, {"title": body.title, "is_published": body.is_published})
    else:
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(dash, "updated_at")
    db.commit()
    return {"ok": True}


def _next_dash_type(dash: Dashboard, html_str: str) -> str:
    """The type of the version about to be written.

    A navigator is a navigator by the owner's declaration, not by its markup:
    `detect_type` has no signature for DASH_PAGE (a navigator is plain HTML with
    links, indistinguishable from static), so re-detecting on a new version or a
    restore turned every navigator into `static` — it fell out of its section,
    lost the link bridge, and Power BI opened as a white tab again (found on the
    24af8e1 copy, 2026-11-05). The type was forced at upload
    (`navigators.create_navigator`); it stays forced for life.
    """
    if dash.dash_type == DASH_PAGE:
        return DASH_PAGE
    return detector.detect_type(html_str)


# ---------------------------------------------------------------------------
# API — replace HTML (new version, same URL)
# ---------------------------------------------------------------------------

@router.post("/api/dashboards/{dashboard_id}/version")
@router.put("/api/dashboards/{dashboard_id}/html")
async def replace_html(
    dashboard_id: str,
    html_file: UploadFile = File(...),
    confirm_type_change: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dash = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dash:
        raise HTTPException(status_code=404)
    # Owner decision 2026-09-04: «can update data» also covers a new version
    # of the report itself — the colleague trusted to refresh the numbers may
    # upload the refreshed HTML. Settings/access/delete stay owner-only.
    if not can_update_dashboard_data(dash, user, db):
        raise HTTPException(status_code=403)

    html_bytes = await html_file.read()
    _validate_html_upload(html_bytes)

    html_str = html_bytes.decode("utf-8", errors="replace")
    dash_type = _next_dash_type(dash, html_str)
    slot_count = detector.count_file_slots(html_str) if dash_type == DASH_INTERACTIVE else 0

    # A replace that flips the dashboard type (static → interactive, …) is
    # almost always the wrong file — e.g. an app shell uploaded over a full
    # static export, which then renders blank because its data slot is empty
    # (the exact prod incident this guards). Ask once; the client retries
    # with confirm_type_change=1.
    # ADR-18: a report with an RLS policy keeps its data in slot files, and
    # they are filtered on serving. Static/snapshot keep the rows right in the HTML —
    # such a replacement would silently hand everything to everyone while the
    # "Access by territory" badge promises the opposite. A refusal, not a confirm: first
    # remove the policy (PUT /rls {"rls": null}), a separate and visible action.
    if dash.rls_enabled and rls.eligibility_error(
            _Typed(dash, dash_type), html_str) is not None:
        raise HTTPException(status_code=409, detail={
            "code": "rls_type_change",
            "from": dash.dash_type,
            "to": dash_type,
            "message": ("This report has an RLS policy: a non-interactive version "
                        "would serve its rows unfiltered. Remove the policy first."),
        })

    if dash_type != dash.dash_type and not confirm_type_change:
        raise HTTPException(status_code=409, detail={
            "code": "type_change",
            "from": dash.dash_type,
            "to": dash_type,
            "message": (f"Report type would change: {dash.dash_type} → {dash_type}. "
                        "This usually means the wrong file was uploaded."),
        })

    new_version = dash.current_version + 1

    # Save new HTML version
    html_path = storage.save_html(dashboard_id, new_version, html_bytes)

    # Copy slot files from previous version so data is not lost
    storage.copy_slot_files(dashboard_id, dash.current_version, new_version)

    # Create version record
    db.add(DashboardVersion(
        dashboard_id=dashboard_id, version_number=new_version,
        html_path=html_path, created_by=user.id,
    ))

    dash.current_version = new_version
    dash.dash_type = dash_type
    dash.slot_count = slot_count
    dash.render_size = storage.estimate_render_size(dashboard_id, new_version)
    dash.updated_at = datetime.utcnow()

    detail = {"version": new_version, "type": dash_type}
    if confirm_type_change:
        detail["type_change_confirmed"] = True
    _log(db, user, "replace_html", "dashboard", dashboard_id, detail)
    db.commit()
    from app.services import warm
    warm.schedule(dashboard_id)
    return {"ok": True, "version": new_version}


@router.post("/api/dashboards/{dashboard_id}/versions/{number}/restore")
async def restore_version(request: Request, dashboard_id: str, number: int,
                          user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    """Bring back an earlier version of the report — forward, not backward.

    The old version does not "become current": its content is laid down as a NEW
    version on top. History is not rewritten, the restore itself lands in the
    history and can be restored the same way — the same discipline as generators
    and reference tables (versions are immutable, the pointer moves).

    The data stays FRESH: slot files are copied from the current version, not
    from the restored one. A restore rolls back the calculation and the look, not
    last week's numbers — otherwise one click would quietly give people old figures.
    """
    dash = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dash:
        raise HTTPException(status_code=404)
    if not can_update_dashboard_data(dash, user, db):
        raise HTTPException(status_code=403)
    # The second lock applies here too: this endpoint does not go through
    # require_dashboard_access, and replacing the current version without unlocking is not allowed.
    from app.services import confidential
    confidential.assert_unlocked_request(db, request, dash)
    if number == dash.current_version:
        raise HTTPException(status_code=409, detail="This version is already current")

    row = (db.query(DashboardVersion)
           .filter(DashboardVersion.dashboard_id == dashboard_id,
                   DashboardVersion.version_number == number).first())
    if not row:
        raise HTTPException(status_code=404, detail="No such version")
    try:
        html_bytes = storage.read_html(dashboard_id, number)
    except OSError:
        # The DB row exists, the file does not: nothing to fix, but staying silent is not an option.
        raise HTTPException(status_code=410, detail="The file of that version is gone")

    html_str = html_bytes.decode("utf-8", errors="replace")
    dash_type = _next_dash_type(dash, html_str)
    slot_count = detector.count_file_slots(html_str) if dash_type == DASH_INTERACTIVE else 0

    # The same guard as for HTML replacement: a report with an RLS policy keeps rows
    # in slot files that are filtered on serving, while static would hand everything to everyone.
    if dash.rls_enabled and rls.eligibility_error(
            _Typed(dash, dash_type), html_str) is not None:
        raise HTTPException(status_code=409, detail={
            "code": "rls_type_change",
            "message": ("This report has an RLS policy: the restored version "
                        "would serve its rows unfiltered. Remove the policy first."),
        })

    new_version = dash.current_version + 1
    html_path = storage.save_html(dashboard_id, new_version, html_bytes)
    storage.copy_slot_files(dashboard_id, dash.current_version, new_version)
    db.add(DashboardVersion(
        dashboard_id=dashboard_id, version_number=new_version,
        html_path=html_path, created_by=user.id,
        note="restore v%d" % number))
    dash.current_version = new_version
    dash.dash_type = dash_type
    dash.slot_count = slot_count
    dash.render_size = storage.estimate_render_size(dashboard_id, new_version)
    dash.updated_at = datetime.utcnow()
    _log(db, user, "dashboard_restore", "dashboard", dashboard_id,
         {"from": number, "version": new_version})
    db.commit()
    from app.services import warm
    warm.schedule(dashboard_id)
    return {"ok": True, "version": new_version, "restored_from": number}


# ---------------------------------------------------------------------------
# API — delete
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# API — RLS policy (ADR-18). Owner/administrator only: the policy decides who
# sees which rows, so it is a right of the report, not of «can update data».
# ---------------------------------------------------------------------------

class RlsBody(BaseModel):
    rls: dict | None = None


def _rls_dash_or_403(dashboard_id: str, user: User, db: Session) -> Dashboard:
    dash = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dash:
        raise HTTPException(status_code=404)
    if user.role != ROLE_ADMINISTRATOR and dash.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only owner or administrator")
    return dash


class _Typed:
    """A Dashboard wrapper with a DIFFERENT dash_type — RLS eligibility check for
    a not-yet-saved version (replace_html / new version)."""
    def __init__(self, dash: Dashboard, dash_type: str):
        self.id, self.current_version, self.dash_type = dash.id, dash.current_version, dash_type


@router.put("/api/dashboards/{dashboard_id}/rls")
async def put_dashboard_rls(
    dashboard_id: str,
    body: RlsBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dash = _rls_dash_or_403(dashboard_id, user, db)
    policy = body.rls
    if policy is not None:
        # Only an interactive report is filtered: its data is in slot files. Static
        # and snapshot keep data inside the HTML — there is nothing to filter, and silently
        # "enabling RLS" there would promise protection that does not exist.
        reason = rls.eligibility_error(dash)
        if reason is not None:
            raise HTTPException(status_code=409, detail=reason)
        errors = rls.validate_policy(policy)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        dash.rls_json = json.dumps(policy, ensure_ascii=False)
    else:
        dash.rls_json = None
    # The filtered-files cache key itself carries the policy fingerprint
    # (rls.policy_hash), so a policy change is invalidated by key rotation, also
    # with OP_WORKERS > 1. clear() remains as an immediate release of memory
    # in this process, not as the invalidation mechanism.
    rls.cache.clear()
    slots = len(policy.get("slots") or []) if isinstance(policy, dict) else 0
    _log(db, user, "rls_policy_set", "dashboard", dash.id,
         {"enabled": policy is not None, "slots": slots},
         request.client.host if request.client else "")
    db.commit()
    return {"ok": True, "enabled": dash.rls_enabled}


@router.get("/api/dashboards/{dashboard_id}/rls")
async def get_dashboard_rls(
    dashboard_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dash = _rls_dash_or_403(dashboard_id, user, db)
    subj = rls.subject(db, dash, user)
    return {"rls": dash.get_rls(), "see_all": subj.see_all,
            "key_count": len(subj.keys)}


@router.delete("/api/dashboards/{dashboard_id}")
async def delete_dashboard(
    dashboard_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dash = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dash:
        raise HTTPException(status_code=404)
    if user.role != ROLE_ADMINISTRATOR and dash.owner_id != user.id:
        raise HTTPException(status_code=403)

    storage.delete_dashboard_files(dashboard_id)
    # The ORM cascade covers versions/files/permissions/share_tokens; the rest
    # of the report's tail — its generation runs and everything hanging off
    # them (observations → files → quality results, stages, reference uses) —
    # has no relationship and used to be left dangling. Under foreign_keys=ON
    # that dangling tail refuses the DELETE, so it is cleared explicitly here;
    # a slide or an AI-answer record that merely POINTS at the report keeps
    # its own life and loses only the link. See app/services/integrity.py.
    from app.services import integrity
    cascaded = integrity.detach(db, "dashboards", "id", dashboard_id)
    _log(db, user, "delete", "dashboard", dashboard_id,
         {"title": dash.title, "cascaded": cascaded})
    db.expire(dash)
    db.delete(dash)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# "Strictly confidential" — the second lock on a report.
# What closes together with the password and why — app/services/confidential.py.
# ---------------------------------------------------------------------------

class ConfBody(BaseModel):
    password: str | None = None


class UnlockBody(BaseModel):
    password: str = ""


@router.put("/api/dashboards/{dashboard_id}/confidential")
async def put_dashboard_confidential(
    dashboard_id: str,
    body: ConfBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Turn the mode on, change the password (`password`) or turn it off (`null`).

    The right is OWNER or administrator, not "update data". Removing the lock from
    a report is a decision of the same order as deleting it: it sits in the danger
    zone, not next to the update-data button.
    """
    from app.services import confidential as conf
    dash = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dash:
        raise HTTPException(status_code=404)
    if user.role != ROLE_ADMINISTRATOR and dash.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only owner or administrator")

    was = conf.enabled(dash)
    if body.password is None:
        conf.clear(db, dash)
    else:
        conf.set_password(db, dash, body.password)
    # The report's issued share links are not deleted: they stopped working on their
    # own (`_require_share` and `_token_or_404` refuse based on the report's state).
    # Deleting them here would silently lose the links when the mode is turned off.
    _log(db, user, "dashboard_confidential_set", "dashboard", dash.id,
         {"enabled": conf.enabled(dash), "was": was,
          "changed_password": body.password is not None},
         request.client.host if request.client else "")
    db.commit()
    return {"ok": True, "enabled": conf.enabled(dash)}


@router.post("/api/dashboards/{dashboard_id}/unlock")
async def unlock_dashboard(
    dashboard_id: str,
    body: UnlockBody,
    request: Request,
    dashboard: Dashboard = Depends(require_dashboard_view),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Enter the password and unlock for a while in THIS session.

    Dependency `require_dashboard_view`: the password is asked of someone who may
    see the report anyway. For an outsider the endpoint answers 403 before the
    password is checked — otherwise it would be an oracle for "does this report
    exist" and a place where anyone could guess the password.
    """
    from app.services import confidential as conf
    from app.auth.session import get_session_id_from_cookie
    sid = get_session_id_from_cookie(request.headers.get("cookie", "")) or ""
    ip = request.client.host if request.client else ""
    if not conf.enabled(dashboard):
        return {"ok": True, "unlocked": True}
    ok = conf.unlock(db, sid, user, dashboard, body.password)
    # Log both sides: a successful entry into a confidential report is a fact that
    # must be visible, and a run of failures is the only trace of guessing.
    _log(db, user, "dashboard_unlock" if ok else "dashboard_unlock_failed",
         "dashboard", dashboard.id, {"ok": ok}, ip)
    db.commit()
    if not ok:
        raise HTTPException(status_code=403, detail=conf.LOCKED)
    return {"ok": True, "unlocked": True}
