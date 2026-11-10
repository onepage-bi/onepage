"""Which report reads what — assembled from data the platform already keeps.

Nothing here collects anything new. The facts already exist, in two shapes:

  * `source_observations` / `source_files` — what the engine actually saw on a
    given run, ONE row per (run, data_source) it walked: file count, byte
    total, newest mtime, a content fingerprint, and the file list underneath
    it. `data_sources` is the identity these attach to — a folder promoted
    from a basename into a row the moment an observation (or a human claim)
    needs somewhere to live. `input_overrides` carries the operational
    exclusion rules, one row per (generator, source) pair.
  * the FROZEN `manifest.json` of the current generator version — what the
    author DECLARED: `sources` (GreenPlum), `required_files`, `aux_inputs`.
    GreenPlum has no observations (nothing is walked, nothing is measured),
    so it is read from here regardless of how the folder side is built.

A DB that predates this migration, or a dashboard whose only history is an
`agent`-engine run, has zero rows in `source_observations` for that dashboard.
Such a dashboard falls back to parsing `generation_runs.input_manifest` JSON
directly (`_build_from_manifest_json`, the pre-migration code path) so it does
not simply vanish from the page the day this shipped.

**On folder identity.** The File System Access API never exposes a path, only
`handle.name`. So an UNCLAIMED folder source is a BASENAME, and two reports are
shown as sharing a folder when that basename matches — on one analyst's machine
`Stock_RDC` may be a network share and on another a local copy. Such a source is
a HEURISTIC and is labelled as one in the UI.

A source stops being a guess when someone claims it in the `data_sources`
registry and states its path. The browser still cannot discover or verify that
path, but it does not need to: a stated path is an identity, which a basename
never was. GreenPlum sources are certain from the start (the connection name is
a server-side alias).

**On freshness.** `newest_mtime` is the newest file's modification time as the
analyst's own machine reported it. It says when the DATA last changed, which is
a different and more useful question than when the report last ran — a report
that runs nightly over a folder nobody has updated in three weeks is stale even
though every run succeeded. `data_stale` asks that question; `unread` asks the
complementary one ("has anyone even looked lately") — a source can be fresh
data nobody has re-read, or a stale folder someone re-observed yesterday and
found unchanged. Both dates are carried so the UI can show the gap. The clock
is the analyst's, not the server's, so treat a few hours of skew as noise.

**On the union, not the sum.** A folder read by three reports is ONE folder.
The old code summed per-reader byte counts and triple-counted it; this code
takes the LATEST observation of a source — across every dashboard in scope,
not per-dashboard — as the folder's current state, because a single run's
walk already lists the whole folder, not just the files that run happened to
touch. `reports`/`last_read`/`failing` still range over every observation in
the window, because "who reads this" is a different question from "what does
it currently hold".

Cost: this runs on a rare, human-opened page. One bounded query (at most
`_RUNS_PER_DASH` runs per dashboard, via a window function) plus a bounded
observation/file query plus dict work.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    DataSource, Generator, GeneratorVersion, GenerationRun,
    InputOverride, SourceFile, SourceObservation,
)
from app.services import genmanifest

# How far back to look for a run that recorded folder names / observations.
# Runs used to store `dirs` only when extra folders were in play, so the
# newest run of an older report can legitimately have none; three is enough
# to cover that gap without dragging whole file lists out of the database.
_RUNS_PER_DASH = 3

KIND_FOLDER = "folder"
KIND_GREENPLUM = "greenplum"
KIND_TABLE = "table"  # ADR-16 reference table, registered via ensure_data_source()

_MAX_FILES_SHOWN = 40
# Names kept per source so the rule editor can show, live, which files a
# pattern would drop. Capped because a folder may legitimately hold 500 and the
# editor only needs enough to make the effect obvious; the UI says when it is
# looking at a sample rather than the whole folder.
_MAX_NAMES_PER_SOURCE = 200

# A source nobody has touched in this long is called out. Not a threshold with
# any measurement behind it — a reporting cadence of a week is the common case
# here, so two weeks means at least one cycle was missed.
STALE_DAYS = 14


def _loads(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _recent_runs(db: Session, dash_ids: list[str], *,
                 limit: int = _RUNS_PER_DASH) -> dict[str, list[GenerationRun]]:
    """Up to `limit` (default _RUNS_PER_DASH) newest runs per dashboard, in one query.

    `limit` exists for the Data Quality history (HISTORY_RUNS), which needs a
    deeper window than the Data Sources page; every other caller keeps the
    default.

    A plain "newest run per dashboard" join would be cheaper, but the newest run
    is exactly the one that may have died before recording anything — and then
    the report would look sourceless. The window function keeps the result set
    bounded by dashboard count either way, so history length never matters.
    """
    if not dash_ids:
        return {}
    rn = func.row_number().over(
        partition_by=GenerationRun.dashboard_id,
        order_by=GenerationRun.started_at.desc(),
    ).label("rn")
    inner = select(GenerationRun.id, rn).where(
        GenerationRun.dashboard_id.in_(dash_ids)
    ).subquery()
    ids = select(inner.c.id).where(inner.c.rn <= limit)
    rows = (db.query(GenerationRun)
            .filter(GenerationRun.id.in_(ids))
            .order_by(GenerationRun.dashboard_id, GenerationRun.started_at.desc())
            .all())
    out: dict[str, list[GenerationRun]] = {}
    for r in rows:
        out.setdefault(r.dashboard_id, []).append(r)
    return out


def _current_manifests(db: Session, generator_ids: set[int]) -> dict[int, dict]:
    """generator_id -> frozen manifest of its CURRENT version."""
    if not generator_ids:
        return {}
    rows = (db.query(GeneratorVersion.generator_id, GeneratorVersion.manifest_json)
            .filter(GeneratorVersion.generator_id.in_(generator_ids),
                    GeneratorVersion.is_current == True)  # noqa: E712
            .all())
    return {gid: _loads(raw) for gid, raw in rows}


def _reference_tables(db: Session, slugs: set[str],
                      dash_ids: list[str] | None = None) -> dict[str, dict]:
    """slug -> {exists, title, current_version, row_count, responsible} for the
    ADR-16 reference tables named by the manifests in scope.

    Two queries and only when a manifest declares a platform input at all:
    one for the tables (left-joined to the responsible user, so a name is not
    a third query), one for the row counts of their CURRENT versions. A slug
    with no table row is deliberately absent from the result rather than
    present-and-empty — the caller renders "not created yet", which is a
    different statement from "created and empty"."""
    slugs = {s for s in slugs if isinstance(s, str) and s}
    if not slugs:
        return {}
    from sqlalchemy import and_, or_

    from app.models import ReferenceTable, ReferenceTableVersion, User

    out: dict[str, dict] = {}
    by_id: dict[int, dict] = {}
    wanted: list[tuple[int, int]] = []
    for t, u in (db.query(ReferenceTable, User)
                 .outerjoin(User, ReferenceTable.responsible_user_id == User.id)
                 .filter(ReferenceTable.slug.in_(slugs)).all()):
        entry = {
            "exists": True, "title": t.title, "slug": t.slug,
            "current_version": t.current_version or 0, "row_count": 0,
            "responsible": (u.display_name or u.person_id) if u is not None else None,
        }
        out[t.slug] = entry
        by_id[t.id] = entry
        if entry["current_version"]:
            wanted.append((t.id, entry["current_version"]))
    if wanted:
        conds = [and_(ReferenceTableVersion.table_id == tid,
                      ReferenceTableVersion.version_number == vnum)
                 for tid, vnum in wanted]
        for tid, count in (db.query(ReferenceTableVersion.table_id,
                                    ReferenceTableVersion.row_count)
                           .filter(or_(*conds)).all()):
            if tid in by_id:
                by_id[tid]["row_count"] = count or 0

    # How many reports in scope were last built against an older version than
    # the one that is current — the same statement Data Quality makes as
    # `rerun_needed`, counted here so the source row can say it without the
    # page having to run the evaluator. One query, ordered by run id so the
    # last row per (report, table) is that report's latest answer.
    if by_id and dash_ids:
        from app.models import RUN_SUCCESS, GenerationRun, RunReferenceUse
        latest: dict[tuple[str, int], tuple] = {}
        for dash_id, tid, vnum, applied in (
                db.query(GenerationRun.dashboard_id, RunReferenceUse.table_id,
                         RunReferenceUse.version_number, RunReferenceUse.applied)
                .join(GenerationRun, RunReferenceUse.run_id == GenerationRun.id)
                .filter(GenerationRun.dashboard_id.in_(dash_ids),
                        # SUCCESSFUL only — same rule as quality._rerun_block:
                        # a run that errored published nothing, so it is not
                        # the answer to "what is this report built on".
                        GenerationRun.status == RUN_SUCCESS)
                .order_by(RunReferenceUse.run_id).all()):
            latest[(dash_id, tid)] = (vnum, applied)
        for (_dash_id, tid), (vnum, applied) in latest.items():
            entry = by_id.get(tid)
            if entry is None or not entry["current_version"]:
                continue
            if not applied or vnum is None or vnum < entry["current_version"]:
                entry["stale_reports"] = entry.get("stale_reports", 0) + 1
    for entry in out.values():
        entry.setdefault("stale_reports", 0)
    return out


def _mtime_to_dt(ms) -> datetime | None:
    """mtime_ms is a JS epoch-millisecond number from the analyst's machine."""
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _dashboards_with_observations(db: Session, runs_by_dash: dict[str, list]) -> set[str]:
    """Dashboards with an observation on a run INSIDE the recent window —
    the fork between the new table-backed path and the JSON fallback.

    Forking on "ever had an observation, anywhere in history" was wrong: a
    dashboard that had one browser-engine run months ago followed by three
    agent-engine runs (which never emit observations) would land in the
    table-backed path, find nothing in its window, and render an empty card
    — while the JSON path would have listed its folders just fine. Forking
    on "a run inside the window has one" makes the two paths cover exactly
    the runs each is about to look at, so a dashboard whose engines are
    mixed never loses its most-recent-JSON folders to a stale table hit.
    """
    run_id_to_dash = {r.id: d_id for d_id, runs in runs_by_dash.items() for r in runs}
    if not run_id_to_dash:
        return set()
    rows = (db.query(SourceObservation.run_id)
            .filter(SourceObservation.run_id.in_(run_id_to_dash.keys()))
            .distinct()
            .all())
    return {run_id_to_dash[r[0]] for r in rows}


def build(db: Session, dashboards: list, *, now: datetime | None = None) -> dict:
    """Return {"sources": [...], "reports": [...], "stats": {...}}.

    Both lists are plain dicts, ready for the template and for the JSON the map
    is drawn from. Sources sort by how many reports they feed, so the shared
    ones — the thing the page exists to reveal — reach the top on their own.
    """
    now = now or datetime.utcnow()
    dash_ids = [d.id for d in dashboards]
    dash_by_id = {d.id: d for d in dashboards}
    runs_by_dash = _recent_runs(db, dash_ids)
    gen_ids = {d.generator_id for d in dashboards if d.generator_id}
    manifests = _current_manifests(db, gen_ids)
    gen_names = dict(
        db.query(Generator.id, Generator.name).filter(Generator.id.in_(gen_ids)).all()
    ) if gen_ids else {}

    sources: dict[str, dict] = {}
    reports: list[dict] = []

    # ADR-16: the reference tables the manifests in scope declare as platform
    # inputs, resolved ONCE for the whole build (two queries, and only when a
    # manifest actually declares one) — `_touch_reference` below is then a
    # pure dict lookup however many reports read the same table.
    ref_tables = _reference_tables(
        db, {pi["table"] for m in manifests.values()
             for pi in genmanifest.platform_inputs(m or {})}, dash_ids)

    def _touch(kind: str, name: str) -> dict:
        key = f"{kind}:{name}"
        s = sources.get(key)
        if s is None:
            s = sources[key] = {
                "key": key, "kind": kind, "name": name,
                "certain": kind in (KIND_GREENPLUM, KIND_TABLE),
                "reports": [], "generators": set(),
                "files": 0, "bytes": 0, "file_names": [],
                # Full paths of the files read and the source root — only
                # when the desktop application sent them; for browser
                # runs they are empty. A set, not a list: the same file is seen by
                # several observations.
                "file_paths": [], "root_path": None, "root_id": None,
                "newest_data": None,     # max file mtime seen in this source
                "last_read": None,       # max run start / observation among readers
                "failing": 0,            # readers whose last run failed
                "queries": set(),
                "fingerprint": None,     # set by the observation path only
            }
        return s

    def _touch_greenplum(d, manifest: dict, gen_name: str, last_run, failed: bool,
                          report_sources: list[str]) -> None:
        src = manifest.get("sources")
        if not (isinstance(src, dict) and src.get("connection")):
            return
        s = _touch(KIND_GREENPLUM, str(src["connection"]))
        s["reports"].append(d.id)
        s["generators"].add(gen_name)
        report_sources.append(s["key"])
        if last_run and (s["last_read"] is None or last_run.started_at > s["last_read"]):
            s["last_read"] = last_run.started_at
        if failed:
            s["failing"] += 1
        for q in (src.get("queries") or []):
            if isinstance(q, dict) and q.get("sql_file"):
                s["queries"].add(str(q["sql_file"]))

    def _touch_reference(d, manifest: dict, gen_name: str, last_run, failed: bool,
                         report_sources: list[str]) -> None:
        """A platform reference table is a SOURCE this report reads, exactly
        like a folder or a GreenPlum connection — that is the whole point of
        giving it a `data_sources` row (ADR-16): Data Sources, lineage and the
        Data Quality acks all key on `data_source_id`, and a mapping nobody
        maintains has to be visible in the same place a stale folder is.

        `certain=True` without qualification: a slug is an identity the
        platform issued, not a basename two machines can share. It costs no
        query — the manifests are already loaded and `ref_tables` was resolved
        once above."""
        for pi in genmanifest.platform_inputs(manifest):
            slug = pi["table"]
            info = ref_tables.get(slug)
            s = _touch(KIND_TABLE, slug)
            s["reports"].append(d.id)
            s["generators"].add(gen_name)
            report_sources.append(s["key"])
            s["table"] = info or {"exists": False, "current_version": 0,
                                  "row_count": 0, "responsible": None,
                                  "title": None, "stale_reports": 0}
            if last_run and (s["last_read"] is None or last_run.started_at > s["last_read"]):
                s["last_read"] = last_run.started_at
            if failed:
                s["failing"] += 1

    observed_ids = _dashboards_with_observations(db, runs_by_dash)
    observed = [d for d in dashboards if d.id in observed_ids]
    fallback = [d for d in dashboards if d.id not in observed_ids]

    _build_from_observations(db, observed, runs_by_dash, manifests, gen_names,
                             dash_by_id, sources, reports, _touch, _touch_greenplum,
                             _touch_reference)
    _build_from_manifest_json(fallback, runs_by_dash, manifests, gen_names,
                              sources, reports, _touch, _touch_greenplum,
                              _touch_reference)

    def _age_days(dt):
        return None if dt is None else max(0, (now - dt).days)

    # Registry overlay. Rows are created lazily — only when somebody edits a
    # source, or when an observation is ingested for it — so opening this page
    # never writes. A source with a declared path stops being a guess:
    # identity comes from what a human stated, not from a basename that two
    # machines can share.
    registry: dict[str, DataSource] = {}
    if sources:
        for row in db.query(DataSource).all():
            registry[f"{row.kind}:{row.key_name}"] = row

    # Override rules, keyed by the registry row id (only a claimed/observed
    # source can have any — the FK requires an existing data_sources row).
    source_ids = {row.id for row in registry.values()}
    overrides_by_source: dict[int, list[dict]] = {}
    if source_ids:
        rows = (db.query(InputOverride, Generator.name)
                .join(Generator, InputOverride.generator_id == Generator.id)
                .filter(InputOverride.data_source_id.in_(source_ids))
                .all())
        for ov, gname in rows:
            overrides_by_source.setdefault(ov.data_source_id, []).append(
                {"generator": gname, "rules": ov.get_excludes()})

    # Declared inputs per generator name, for the /data-sources drawer's
    # per-reader schema_diff render — same genmanifest.inputs_for() the
    # viewer's Data dock uses, keyed by name since s["generators"] already
    # is (a generator can be dropped from `manifests` mid-loop if its
    # current version changed between the two queries above; inputs_for
    # tolerates the resulting {} the same way it tolerates a legacy
    # manifest with no `inputs` key).
    inputs_by_gen_name = {
        gen_names[gid]: genmanifest.inputs_for(manifests.get(gid) or {})
        for gid in gen_ids if gid in gen_names
    }
    # Author-written column descriptions (P3.6, manifest's optional
    # {name, description} column form) per generator, same keying as
    # inputs_by_gen_name — the drawer/lineage chip fall back to these when
    # no annotation overrides them.
    column_descriptions_by_gen_name = {
        gen_names[gid]: genmanifest.column_descriptions(manifests.get(gid) or {})
        for gid in gen_ids if gid in gen_names
    }

    # Observed-vs-declared diff per (source, reader generator), for the
    # drawer. The walk-back through older observations (a warm run leaves
    # schema_json NULL on unchanged files) lives in lineage.py; a second
    # implementation here would be the drift this feature exists to catch,
    # so the import is local only to break the module cycle (lineage calls
    # build()).
    diffs_by_key: dict[str, dict[str, dict]] = {}
    if observed:
        from app.services import lineage, schemadiff
        gen_name_by_dash = {d.id: gen_names.get(d.generator_id) for d in observed}
        chains = lineage._collect_observations(db, observed, runs_by_dash, gen_name_by_dash)
        files_by_pair = lineage._collect_files(db, chains) if chains else {}
        key_by_ds_id = {row.id: key for key, row in registry.items()}
        for (gname, ds_id), flist in files_by_pair.items():
            key = key_by_ds_id.get(ds_id)
            if key is None:
                continue
            decl = inputs_by_gen_name.get(gname) or []
            if not decl:
                continue                      # legacy generator: nothing to diff against
            diffs_by_key.setdefault(key, {})[gname] = schemadiff.diff(decl, flist)

    for s in sources.values():
        row = registry.get(s["key"])
        s["generator_diffs"] = diffs_by_key.get(s["key"], {})
        s["source_id"] = row.id if row else None
        s["version"] = row.version if row else None
        s["declared_path"] = row.declared_path if row else None
        s["root_path"] = row.root_path if row else None
        s["root_id"] = row.root_id if row else None
        s["file_paths"] = sorted(set(s["file_paths"]))[:_MAX_NAMES_PER_SOURCE]
        s["label"] = (row.label if row and row.label else None)
        if s["kind"] == KIND_TABLE:
            s.setdefault("table", {"exists": False, "current_version": 0,
                                   "row_count": 0, "responsible": None,
                                   "title": None, "stale_reports": 0})
            # The table's own title wins over a data_sources label: the
            # reference page is where it is edited, and two names for one
            # thing is exactly what a slug exists to prevent.
            if s["table"].get("title"):
                s["label"] = s["table"]["title"]
        s["notes"] = row.notes if row else None
        # GreenPlum was always certain (a server-side alias). A folder becomes
        # certain the moment someone states its path.
        # root_id is a fingerprint of the path the application REPORTED, not a folder name,
        # so for such a source "matched by name" is wrong.
        if s["kind"] == KIND_FOLDER and (s["declared_path"] or s["root_id"]):
            s["certain"] = True
        s["reports"] = sorted(set(s["reports"]))
        s["report_count"] = len(s["reports"])
        # {generator_name: inputs_for(...)} for only the generators that
        # actually read THIS source — not the full inputs_by_gen_name map,
        # so the drawer's per-generator loop never has to filter.
        s["generator_inputs"] = {
            gname: inputs_by_gen_name.get(gname, [])
            for gname in s["generators"]
        }
        s["generator_column_descriptions"] = {
            gname: column_descriptions_by_gen_name.get(gname, {})
            for gname in s["generators"]
        }
        s["generators"] = sorted(s["generators"])
        s["queries"] = sorted(s["queries"])
        s["file_names"] = sorted(set(s["file_names"]))[:_MAX_NAMES_PER_SOURCE]
        s["names_capped"] = s["files"] > len(s["file_names"])
        s["data_age_days"] = _age_days(s["newest_data"])
        s["read_age_days"] = _age_days(s["last_read"])
        s["data_stale"] = (s["data_age_days"] is not None and s["data_age_days"] >= STALE_DAYS)
        s["unread"] = (s["read_age_days"] is None or s["read_age_days"] >= STALE_DAYS)
        s["stale"] = s["data_stale"]   # alias kept for the current template this round
        s["shared"] = s["report_count"] > 1
        overrides = overrides_by_source.get(s["source_id"], []) if s["source_id"] else []
        overrides.sort(key=lambda o: o["generator"])
        s["overrides"] = overrides
        excl: set[str] = set()
        for o in overrides:
            excl.update(o["rules"])
        s["excludes"] = sorted(excl)

    source_list = sorted(sources.values(),
                         key=lambda s: (-s["report_count"], -s["bytes"], s["name"].lower()))
    reports.sort(key=lambda r: (r["title"] or "").lower())

    total_bytes = sum(s["bytes"] for s in source_list)
    total_files = sum(s["files"] for s in source_list)

    return {
        "sources": source_list,
        "reports": reports,
        "stats": {
            "sources": len(source_list),
            "reports": len(reports),
            "shared": sum(1 for s in source_list if s["shared"]),
            "folders": sum(1 for s in source_list if s["kind"] == KIND_FOLDER),
            "greenplum": sum(1 for s in source_list if s["kind"] == KIND_GREENPLUM),
            "tables": sum(1 for s in source_list if s["kind"] == KIND_TABLE),
            "unsourced": sum(1 for r in reports if not r["sources"]),
            "stale": sum(1 for s in source_list if s["stale"]),
            "data_stale": sum(1 for s in source_list if s["data_stale"]),
            "unread": sum(1 for s in source_list if s["unread"]),
            "failing": sum(1 for r in reports if r["last_run_status"] == "error"),
            "bytes": total_bytes,
            "files": total_files,
            "unidentified": sum(1 for s in source_list
                                if s["kind"] == KIND_FOLDER and not s["declared_path"]
                                and not s["root_id"]),
            "excluded_rules": sum(len(s["excludes"]) for s in source_list),
        },
    }


def _build_from_observations(db: Session, observed: list, runs_by_dash: dict,
                              manifests: dict, gen_names: dict, dash_by_id: dict,
                              sources: dict, reports: list, _touch, _touch_greenplum,
                              _touch_reference) -> None:
    """The table-backed path: `source_observations` / `source_files`.

    Two passes over the same observation set, because they answer different
    questions. Pass 1 is per-SOURCE: across every observed dashboard in the
    window, what does the source currently hold (the latest observation
    alone — see module docstring on the union) and who has read it at all
    (every observation, not just the latest). Pass 2 is per-DASHBOARD: what
    does THIS report's own most-recent-with-data run show, to build its
    report card the same shape the old JSON path produced.
    """
    if not observed:
        return

    run_ids: list[int] = []
    run_meta: dict[int, tuple[str, str, datetime]] = {}   # run_id -> (dash_id, status, started_at)
    for d in observed:
        for r in runs_by_dash.get(d.id, []):
            run_ids.append(r.id)
            run_meta[r.id] = (d.id, r.status, r.started_at)
    if not run_ids:
        return

    obs_rows: list[tuple[SourceObservation, DataSource]] = (
        db.query(SourceObservation, DataSource)
        .join(DataSource, SourceObservation.data_source_id == DataSource.id)
        .filter(SourceObservation.run_id.in_(run_ids))
        .all()
    )
    # obs_rows is non-empty by construction: `observed` only contains
    # dashboards `_dashboards_with_observations` found a window-run
    # observation for, so at least one row here belongs to each of them.

    # ---- Pass 1: per source, across all observed dashboards -------------
    by_ds: dict[int, list[SourceObservation]] = {}
    ds_row_by_id: dict[int, DataSource] = {}
    for obs, ds in obs_rows:
        by_ds.setdefault(ds.id, []).append(obs)
        ds_row_by_id[ds.id] = ds

    for ds_id, obs_list in by_ds.items():
        ds = ds_row_by_id[ds_id]
        s = _touch(ds.kind, ds.key_name)
        latest = max(obs_list, key=lambda o: o.created_at)
        s["files"] = latest.file_count
        s["bytes"] = latest.total_bytes
        s["newest_data"] = latest.newest_mtime
        s["fingerprint"] = latest.fingerprint
        names = (db.query(SourceFile.name, SourceFile.path)
                 .filter(SourceFile.observation_id == latest.id)
                 .limit(_MAX_NAMES_PER_SOURCE)
                 .all())
        s["file_names"] = [n for (n, _p) in names]
        s["file_paths"] = [p for (_n, p) in names if p]

        readers: set[str] = set()
        for obs in obs_list:
            dash_id, _status, _started = run_meta[obs.run_id]
            readers.add(dash_id)
            if s["last_read"] is None or obs.created_at > s["last_read"]:
                s["last_read"] = obs.created_at
        for dash_id in readers:
            s["reports"].append(dash_id)
            d = dash_by_id.get(dash_id)
            if d:
                s["generators"].add(gen_names.get(d.generator_id) or "—")
            last_run = (runs_by_dash.get(dash_id) or [None])[0]
            if last_run and last_run.status == "error":
                s["failing"] += 1

    # ---- Pass 2: per dashboard, its own report card ----------------------
    obs_by_run: dict[int, list[tuple[SourceObservation, DataSource]]] = {}
    for obs, ds in obs_rows:
        obs_by_run.setdefault(obs.run_id, []).append((obs, ds))

    for d in observed:
        manifest = manifests.get(d.generator_id) or {}
        runs = runs_by_dash.get(d.id, [])
        gen_name = gen_names.get(d.generator_id) or "—"
        last_run = runs[0] if runs else None
        failed = bool(last_run and last_run.status == "error")

        chosen_run = None
        for r in runs:                       # runs is newest-first already
            if obs_by_run.get(r.id):
                chosen_run = r
                break

        report_sources: list[str] = []
        dirs: list[str] = []
        file_names: list[str] = []
        file_paths: list[str] = []
        roots: list[dict] = []
        bytes_total = 0
        report_newest = None
        skipped_n = 0

        if chosen_run is not None:
            pairs = sorted(obs_by_run[chosen_run.id], key=lambda p: p[0].dir_index)
            for obs, ds in pairs:
                s = _touch(ds.kind, ds.key_name)
                report_sources.append(s["key"])
                dirs.append(ds.key_name)
                bytes_total += obs.total_bytes
                if obs.newest_mtime and (report_newest is None or obs.newest_mtime > report_newest):
                    report_newest = obs.newest_mtime
                skipped = _loads_list(obs.skipped_json)
                skipped_n += len(skipped)
                names = (db.query(SourceFile.name, SourceFile.path)
                         .filter(SourceFile.observation_id == obs.id)
                         .limit(_MAX_FILES_SHOWN)
                         .all())
                file_names.extend(n for (n, _p) in names)
                file_paths.extend(p for (_n, p) in names if p)
                if ds.root_path:
                    roots.append({"name": ds.key_name, "root_path": ds.root_path})

        _touch_greenplum(d, manifest, gen_name, last_run, failed, report_sources)
        _touch_reference(d, manifest, gen_name, last_run, failed, report_sources)

        reports.append({
            "id": d.id,
            "title": d.title,
            "generator": gen_name,
            "engine": manifest.get("engine") or "auto",
            "sources": report_sources,
            "dirs": dirs,
            "dirs_at": chosen_run.started_at if chosen_run else None,
            "files": file_names[:_MAX_FILES_SHOWN],
            "files_total": len(file_names),
            "file_paths": file_paths[:_MAX_FILES_SHOWN],
            "roots": roots,
            "bytes": bytes_total,
            "newest_data": report_newest,
            "skipped": skipped_n,
            "required_files": [str(n) for n in (manifest.get("required_files") or [])],
            "aux_names": [str(a.get("name")) for a in (manifest.get("aux_inputs") or [])
                          if isinstance(a, dict) and a.get("name")],
            "last_run_at": last_run.started_at if last_run else None,
            "last_run_status": last_run.status if last_run else None,
        })


def _loads_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return obj if isinstance(obj, list) else []


def _build_from_manifest_json(dashboards: list, runs_by_dash: dict, manifests: dict,
                               gen_names: dict, sources: dict, reports: list,
                               _touch, _touch_greenplum, _touch_reference) -> None:
    """Pre-migration path: parse `generation_runs.input_manifest` JSON the way
    the page always used to, for a dashboard with zero `source_observations`
    rows (never ran since the migration, or only ever ran on the agent
    engine). Kept byte-for-byte in spirit with the original `build()` so a
    pre-migration report keeps showing exactly what it showed before."""
    for d in dashboards:
        manifest = manifests.get(d.generator_id) or {}
        runs = runs_by_dash.get(d.id, [])
        gen_name = gen_names.get(d.generator_id) or "—"

        # The newest run that recorded folder names, and the newest that
        # recorded files. Usually the same run; kept separate so an odd one
        # cannot blank both.
        dirs: list[str] = []
        dirs_at = None
        files_im: dict = {}
        files_at = None
        for r in runs:
            im = _loads(r.input_manifest)
            if not dirs:
                cand = im.get("dirs")
                if isinstance(cand, list) and cand:
                    dirs = [str(x) for x in cand]
                    dirs_at = r.started_at
            if not files_im:
                fl = im.get("files")
                if isinstance(fl, list) and fl:
                    files_im = im
                    files_at = r.started_at
            if dirs and files_im:
                break

        last_run = runs[0] if runs else None
        failed = bool(last_run and last_run.status == "error")

        # Attribute every non-aux file to the folder it came from. `dir` is
        # absent for the main folder, which is index 0 by construction.
        per_dir: dict[int, dict] = {}
        file_names: list[str] = []
        for f in (files_im.get("files") or []):
            if not isinstance(f, dict) or not f.get("name"):
                continue
            file_names.append(str(f["name"]))
            if f.get("aux"):
                continue                      # the analyst's upload, not a source
            idx = f.get("dir") or 0
            if not isinstance(idx, int) or idx < 0 or idx >= len(dirs):
                continue
            acc = per_dir.setdefault(idx, {"files": 0, "bytes": 0, "newest": None,
                                           "names": []})
            acc["files"] += 1
            if len(acc["names"]) < _MAX_NAMES_PER_SOURCE:
                acc["names"].append(str(f["name"]))
            size = f.get("size")
            if isinstance(size, (int, float)) and size > 0:
                acc["bytes"] += int(size)
            mt = _mtime_to_dt(f.get("mtime_ms"))
            if mt and (acc["newest"] is None or mt > acc["newest"]):
                acc["newest"] = mt

        skipped = files_im.get("skipped")
        skipped_n = len(skipped) if isinstance(skipped, list) else 0

        report_sources: list[str] = []
        report_newest = None

        for i, name in enumerate(dirs):
            s = _touch(KIND_FOLDER, name)
            # A source touched by the observation pass already carries its
            # LATEST observation as `fingerprint` (see module docstring on
            # the union, not the sum). If the same folder is ALSO read by a
            # fallback (no-observations) dashboard, its volume must not be
            # added on top — that would double-count the very folder the
            # union rule exists to count once. Only a source the observation
            # pass never touched gets its volume from this JSON path.
            already_observed = s["fingerprint"] is not None
            s["reports"].append(d.id)
            s["generators"].add(gen_name)
            report_sources.append(s["key"])
            acc = per_dir.get(i)
            if acc and not already_observed:
                s["files"] += acc["files"]
                s["bytes"] += acc["bytes"]
                for nm in acc["names"]:
                    if len(s["file_names"]) < _MAX_NAMES_PER_SOURCE:
                        s["file_names"].append(nm)
                if acc["newest"] and (s["newest_data"] is None or acc["newest"] > s["newest_data"]):
                    s["newest_data"] = acc["newest"]
            if acc and acc["newest"] and (report_newest is None or acc["newest"] > report_newest):
                report_newest = acc["newest"]
            if files_at and (s["last_read"] is None or files_at > s["last_read"]):
                s["last_read"] = files_at
            if failed:
                s["failing"] += 1

        _touch_greenplum(d, manifest, gen_name, last_run, failed, report_sources)
        _touch_reference(d, manifest, gen_name, last_run, failed, report_sources)

        reports.append({
            "id": d.id,
            "title": d.title,
            "generator": gen_name,
            "engine": manifest.get("engine") or "auto",
            "sources": report_sources,
            "dirs": dirs,
            "dirs_at": dirs_at,
            "files": file_names[:_MAX_FILES_SHOWN],
            "files_total": len(file_names),
            "file_paths": [], "roots": [],
            "bytes": int(files_im.get("total_bytes") or 0),
            "newest_data": report_newest,
            "skipped": skipped_n,
            "required_files": [str(n) for n in (manifest.get("required_files") or [])],
            "aux_names": [str(a.get("name")) for a in (manifest.get("aux_inputs") or [])
                          if isinstance(a, dict) and a.get("name")],
            "last_run_at": last_run.started_at if last_run else None,
            "last_run_status": last_run.status if last_run else None,
        })
