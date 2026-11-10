"""Provenance page (GET /d/{id}/provenance, GET /api/dashboards/{id}/provenance).

One read-only assembly of «where did this report's numbers come from»: the
report version, the run that produced it, the immutable generator version it
ran, the deps that version froze, the semantics it shipped, the reference
tables it resolved, the RLS policy guarding it, the lake manifest built from
it, and the source folders the run observed. Every field here is a count, a
hash, a timestamp, or an identifier — never a data VALUE — but `require_
dashboard_access` alone is a wider gate than the platform gives the same
facts everywhere else: /generator/runs, /generator/manifest and /data-sources
all scope to `can_update_dashboard_data` (a viewer with mere read access to a
published dashboard is not shown who runs its generator, on what deps, from
which source folders), and the lake index (ADR-18) is closed to anyone but a
`see_all` subject on an RLS report. So this module re-derives both gates
itself and hides the run/generator/deps/source-observations block behind
`can_update_dashboard_data`, and the lake-manifest block behind that AND the
same RLS full-access check `app.api.lake._assert_rls_full_access` applies —
everything else (the version record, reference-table uses, and the bare fact
of whether RLS is on) stays visible to anyone `require_dashboard_access`
already let in.

Everything is read with plain synchronous queries (no relationships walked,
no N+1) because the caller wraps the whole call in `run_in_threadpool` — this
module must stay import-light and side-effect-free.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from app.auth.deps import can_update_dashboard_data
from app.models import (
    Dashboard, DashboardVersion, DataSource, GenerationRun, Generator,
    GeneratorVersion, ReferenceTable, RunReferenceUse, SourceFile, SourceObservation,
    User,
)
from app.services import rls, storage

_MAX_PATHS_PER_SOURCE = 200


def _rls_full_access(db: Session, dashboard: Dashboard, user: User) -> bool:
    """Same rule as app.api.lake._assert_rls_full_access: a non-RLS report
    has no gate here; an RLS report only opens the lake block to a subject
    with see_all (admin, owner, or a slot explicitly marked see_all)."""
    if not dashboard.rls_enabled:
        return True
    return rls.subject(db, dashboard, user).see_all


def _user_name(db: Session, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    row = db.query(User.person_id).filter(User.id == user_id).first()
    return row[0] if row else None


def build(db: Session, dashboard: Dashboard, version_number: int | None,
          user: User) -> dict | None:
    """Returns the provenance payload for `version_number` (default: the
    dashboard's current version), or None when a version was explicitly
    asked for and no such `dashboard_versions` row exists — the caller turns
    that into a 404.

    `user` is the already-authorized viewer (require_dashboard_access ran
    upstream) — it is used only to compute the narrower can_update_data /
    RLS-see_all gates on the sections that carry more than "a report of this
    version exists"."""
    can_update = can_update_dashboard_data(dashboard=dashboard, user=user, db=db)
    vnum = version_number if version_number is not None else dashboard.current_version

    dv = (db.query(DashboardVersion)
          .filter(DashboardVersion.dashboard_id == dashboard.id,
                  DashboardVersion.version_number == vnum)
          .order_by(DashboardVersion.id.desc())
          .first())
    if dv is None and version_number is not None:
        return None

    version_info = {
        "number": vnum,
        "is_current": vnum == dashboard.current_version,
        "created_at": (dv.created_at if dv else dashboard.updated_at),
        "note": dv.note if dv else None,
        "created_by": _user_name(db, dv.created_by) if dv else None,
    }

    run = None
    if dashboard.generator_id:
        run = (db.query(GenerationRun)
               .filter(GenerationRun.dashboard_id == dashboard.id,
                       GenerationRun.output_version_number == vnum)
               .order_by(GenerationRun.started_at.desc())
               .first())

    run_info = None
    generator_info = None
    deps = []
    semantics_info = None
    reference_uses: list[dict] = []
    observations: list[dict] = []

    if run is not None:
        # Reference-table uses are shown to every viewer require_dashboard_
        # access let in (they name a slug + version, never who ran anything
        # or where from), so this join runs regardless of can_update.
        ref_rows = (db.query(RunReferenceUse, ReferenceTable.slug)
                    .join(ReferenceTable, ReferenceTable.id == RunReferenceUse.table_id)
                    .filter(RunReferenceUse.run_id == run.id)
                    .order_by(ReferenceTable.slug)
                    .all())
        reference_uses = [
            {"slug": slug, "version_number": use.version_number, "applied": use.applied}
            for use, slug in ref_rows
        ]

    if run is not None and can_update:
        # Everything below names who ran the generator, on what deps, and
        # from which source folders — scoped exactly like /generator/runs
        # and /data-sources (see app.api.generators._assert_can_update_data):
        # a viewer with mere read access to a published dashboard does not
        # get to see this, only someone who could also run the generator.
        run_info = {
            "id": run.id,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "engine": run.engine,
            "status": run.status,
            "started_by": _user_name(db, run.started_by),
        }

        gv = (db.query(GeneratorVersion)
              .filter(GeneratorVersion.id == run.generator_version_id)
              .first())
        if gv is not None:
            gen = db.query(Generator).filter(Generator.id == gv.generator_id).first()
            generator_info = {
                "name": gen.name if gen else None,
                "version_number": gv.version_number,
                "checksum_sha256": gv.checksum_sha256,
                "created_at": gv.created_at,
                "created_by": _user_name(db, gv.created_by),
            }
            manifest = gv.get_manifest()
            deps = [
                {"name": d.get("name"), "version": d.get("version")}
                for d in (manifest.get("deps") or [])
                if isinstance(d, dict) and d.get("name")
            ]
            if gv.semantics_json:
                sha256 = hashlib.sha256(gv.semantics_json.encode("utf-8")).hexdigest()
                try:
                    sem = json.loads(gv.semantics_json)
                except (ValueError, TypeError):
                    sem = {}
                kpis = sem.get("kpis") if isinstance(sem, dict) else None
                semantics_info = {
                    "sha256": sha256,
                    "kpi_count": len(kpis) if isinstance(kpis, dict) else 0,
                }

        obs_rows = (db.query(SourceObservation, DataSource)
                    .join(DataSource, DataSource.id == SourceObservation.data_source_id)
                    .filter(SourceObservation.run_id == run.id)
                    .order_by(SourceObservation.dir_index)
                    .all())
        # Full file paths — owner's decision 12.10 ("so that a person sees
        # WHICH EXACT file was read"). They sit inside the same can_update block,
        # so they introduce no new right. One query for all the run's folders, not
        # one query per folder; a ceiling — the page must not grow with the folder.
        paths_by_obs: dict[int, list[str]] = {}
        if obs_rows:
            for oid, pth in (db.query(SourceFile.observation_id, SourceFile.path)
                             .filter(SourceFile.observation_id.in_([o.id for o, _ in obs_rows]),
                                     SourceFile.path.isnot(None))
                             .order_by(SourceFile.id).all()):
                lst = paths_by_obs.setdefault(oid, [])
                if len(lst) < _MAX_PATHS_PER_SOURCE:
                    lst.append(pth)
        observations = [
            {"source": ds.key_name, "fingerprint": obs.fingerprint,
             "file_count": obs.file_count,
             "root_path": ds.root_path, "root_id": ds.root_id,
             "file_paths": paths_by_obs.get(obs.id, [])}
            for obs, ds in obs_rows
        ]

    # The bare fact of whether RLS guards this report is visible to anyone
    # who can open it; the slot count and policy hash describe the policy
    # itself and are scoped like the rest of the can_update-gated block above.
    rls_info = {"enabled": dashboard.rls_enabled, "slot_count": None, "policy_hash": None}
    if dashboard.rls_enabled and can_update:
        policy = dashboard.get_rls() or {}
        rls_info["slot_count"] = len(policy.get("slots") or [])
        rls_info["policy_hash"] = rls.policy_hash(dashboard)

    # ADR-18: the lake index is the report's rows verbatim in parquet, with
    # no RLS filtering — closed to anyone but a see_all subject, same as
    # app.api.lake._assert_rls_full_access. Also requires can_update (the
    # run-id inside it is otherwise-gated run metadata).
    lake_info = None
    raw_manifest = (storage.read_lake_manifest(dashboard.id)
                     if can_update and _rls_full_access(db, dashboard, user) else None)
    if raw_manifest:
        try:
            lm = json.loads(raw_manifest)
        except (ValueError, TypeError):
            lm = None
        if isinstance(lm, dict):
            lake_info = {
                "run_id": lm.get("run_id"),
                "created_at": lm.get("created_at"),
                "partitions": len(lm.get("files") or []),
            }

    return {
        "dashboard_id": dashboard.id,
        "title": dashboard.title,
        "version": version_info,
        "run": run_info,
        # True only when a run actually exists but this viewer lacks
        # can_update_data — lets the template say "hidden" rather than the
        # misleading "no run" it would otherwise show for run_info=None.
        "run_restricted": run is not None and not can_update,
        "generator_version": generator_info,
        "deps": deps,
        "semantics": semantics_info,
        "reference_uses": reference_uses,
        "rls": rls_info,
        "rls_restricted": bool(dashboard.rls_enabled) and not can_update,
        "lake_manifest": lake_info,
        "source_observations": observations,
    }
