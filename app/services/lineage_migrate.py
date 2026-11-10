"""One-shot data migration into the Data Lineage v2 tables, run at startup.

Two moves, both idempotent, both bounded — this runs on every boot and must
cost nothing once it has run:

  1. `data_sources.exclude_rules` (folder-level, shipped to every generator in
     the instance) becomes `input_overrides` rows on the (generator, source)
     pairs that actually read the folder. A rule is knowledge about what a
     GENERATOR may read; the v1 storage was the wrong layer. A source whose
     folder appears in no run history has no generator to attach to, so its
     rules STAY on the row and a warning names it — a lost exclusion silently
     re-admits junk into a report.

  2. Observation backfill from `generation_runs.input_manifest`, so the
     rewritten Data Sources page has history on day one instead of showing an
     empty catalogue until every report has run again. Backfilled rows are
     marked `kind="legacy"`: they carry name/size/mtime because the manifest
     had them, and never a schema, because nothing sniffed those files.

The whole thing is one transaction and swallows its own failure: a database
that cannot be migrated must still boot and serve reports.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    Dashboard, DataSource, GenerationRun, InputOverride, SourceFile,
    SourceObservation,
)

# Rule lookup scans run history for a folder NAME, so it needs more depth than
# the page's 3-run window: a folder may have been read by a generator whose
# dashboard has run a dozen times since. 20 keeps the scan bounded per
# dashboard while covering months of a weekly report.
_RULE_RUNS_PER_DASH = 20
# Backfill depth. Ten observations is enough to draw a trend line on the page;
# more would multiply file rows by history length for no readable gain.
_BACKFILL_RUNS_PER_DASH = 10
# A run manifest is capped at 5000 files by the ingest guard, but older rows
# predate that cap. Counts and bytes are computed over ALL files; only the
# per-file rows stop here, and the observation says so via `truncated`.
_MAX_FILES_PER_OBS = 5000

KIND_FOLDER = "folder"


def _loads(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _dirs_of(manifest: dict) -> list[str]:
    dirs = manifest.get("dirs")
    if not isinstance(dirs, list):
        return []
    return [d for d in dirs if isinstance(d, str) and d]


def _mtime_to_dt(ms) -> datetime | None:
    """mtime_ms is a JS epoch-millisecond number from the analyst's machine."""
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _fingerprint(names: list[str]) -> str:
    """sha256 over the sorted, case-folded name set — the same identity the
    engine computes for a live observation, so a backfilled row and a fresh
    one are comparable."""
    blob = "\n".join(sorted(n.casefold() for n in names))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _recent_run_ids(db, per_dash: int) -> list[int]:
    """Newest `per_dash` run ids per dashboard, in one query (window function,
    same shape as datasources._recent_runs) — history length never widens the
    result set."""
    rn = func.row_number().over(
        partition_by=GenerationRun.dashboard_id,
        order_by=GenerationRun.started_at.desc(),
    ).label("rn")
    inner = select(GenerationRun.id, rn).subquery()
    return [r[0] for r in db.execute(
        select(inner.c.id).where(inner.c.rn <= per_dash)).all()]


# ---------------------------------------------------------------------------
# 1. exclude_rules -> input_overrides
# ---------------------------------------------------------------------------

def _migrate_rules(db) -> tuple[int, int, list[str]]:
    sources = [s for s in db.query(DataSource)
               .filter(DataSource.exclude_rules.isnot(None)).all()
               if s.get_excludes()]
    if not sources:
        return 0, 0, []

    gen_of_dash = dict(db.query(Dashboard.id, Dashboard.generator_id)
                       .filter(Dashboard.generator_id.isnot(None)).all())
    if not gen_of_dash:
        run_ids: list[int] = []
    else:
        run_ids = _recent_run_ids(db, _RULE_RUNS_PER_DASH)

    created, cleared, warnings = 0, 0, []
    for src in sources:
        rules = src.get_excludes()
        gen_ids: set[int] = set()
        if run_ids:
            # LIKE prefilter first: json.loads over every historical manifest
            # would parse megabytes to answer "does this name appear at all".
            rows = (db.query(GenerationRun.dashboard_id, GenerationRun.input_manifest)
                    .filter(GenerationRun.id.in_(run_ids),
                            GenerationRun.input_manifest.contains(src.key_name,
                                                                  autoescape=True))
                    .all())
            for dash_id, raw in rows:
                gid = gen_of_dash.get(dash_id)
                if gid and src.key_name in _dirs_of(_loads(raw)):
                    gen_ids.add(gid)

        if not gen_ids:
            warnings.append(
                f"{src.key_name}: no generator reads it, {len(rules)} rule(s) left in place")
            continue
        bad = [r for r in rules if "/" in r or "\\" in r]
        for gid in sorted(gen_ids):
            exists = (db.query(InputOverride.id)
                      .filter(InputOverride.generator_id == gid,
                              InputOverride.data_source_id == src.id).first())
            if exists:
                continue  # never overwrite what a human has already set
            ov = InputOverride(generator_id=gid, data_source_id=src.id)
            ov.set_excludes(rules)
            db.add(ov)
            created += 1
        # Rules move verbatim, separators included: they can never match while
        # the walk is flat, but dropping them would hide the author's intent.
        if bad:
            warnings.append(f"{src.key_name}: rule(s) with a path separator "
                            f"never match a flat walk: {', '.join(bad)}")
        src.exclude_rules = None
        cleared += 1

    return created, cleared, warnings


# ---------------------------------------------------------------------------
# 2. Observation backfill
# ---------------------------------------------------------------------------

def _backfill(db) -> tuple[int, int]:
    if db.query(SourceObservation.id).first() is not None:
        return 0, 0  # already have real observations; never touch them
    if db.query(GenerationRun.id).first() is None:
        return 0, 0

    run_ids = _recent_run_ids(db, _BACKFILL_RUNS_PER_DASH)
    if not run_ids:
        return 0, 0
    runs = (db.query(GenerationRun)
            .filter(GenerationRun.id.in_(run_ids),
                    GenerationRun.input_manifest.isnot(None))
            .order_by(GenerationRun.started_at.asc(), GenerationRun.id.asc())
            .all())

    by_key: dict[str, DataSource] = {
        s.key_name: s for s in db.query(DataSource).filter(DataSource.kind == KIND_FOLDER).all()
    }
    n_obs = n_files = 0

    for run in runs:
        manifest = _loads(run.input_manifest)
        dirs = _dirs_of(manifest)
        if not dirs:
            continue
        files = manifest.get("files")
        files = files if isinstance(files, list) else []
        skipped = manifest.get("skipped")
        skipped = skipped if isinstance(skipped, list) else []
        started = run.started_at or datetime.utcnow()

        seen: set[str] = set()
        for idx, key in enumerate(dirs):
            # A manifest listing one basename twice (two machines, same folder
            # name) gets one observation: the row is UNIQUE per (run, source).
            if key in seen:
                continue
            seen.add(key)

            src = by_key.get(key)
            if src is None:
                src = DataSource(kind=KIND_FOLDER, key_name=key, first_seen_at=started)
                db.add(src)
                db.flush()
                by_key[key] = src
            elif src.first_seen_at is None or src.first_seen_at > started:
                src.first_seen_at = started

            mine = [f for f in files
                    if isinstance(f, dict) and not f.get("aux")
                    and (f.get("dir") or 0) == idx and isinstance(f.get("name"), str)]
            newest = None
            total = 0
            for f in mine:
                total += f.get("size") if isinstance(f.get("size"), int) else 0
                dt = _mtime_to_dt(f.get("mtime_ms"))
                if dt and (newest is None or dt > newest):
                    newest = dt

            obs = SourceObservation(
                run_id=run.id, data_source_id=src.id, dir_index=idx,
                file_count=len(mine), total_bytes=total, newest_mtime=newest,
                fingerprint=_fingerprint([f["name"] for f in mine]),
                skipped_json=_skipped_for(skipped, idx, len(dirs)),
                truncated=len(mine) > _MAX_FILES_PER_OBS,
                created_at=started,
            )
            db.add(obs)
            db.flush()
            n_obs += 1

            for f in mine[:_MAX_FILES_PER_OBS]:
                db.add(SourceFile(
                    observation_id=obs.id, name=f["name"],
                    size=f.get("size") if isinstance(f.get("size"), int) else None,
                    mtime_ms=f.get("mtime_ms") if isinstance(f.get("mtime_ms"), int) else None,
                    kind="legacy",
                ))
                n_files += 1

            # Newest run wins: `runs` is ordered oldest-first, so the last
            # write is the summary of the most recent observation.
            src.last_seen_at = started
            src.fingerprint = obs.fingerprint
            src.last_file_count = obs.file_count

    return n_obs, n_files


def _skipped_for(skipped: list, idx: int, n_dirs: int) -> str | None:
    """The engine concatenates the per-folder skip lists WITHOUT a dir tag
    (gen-engine.js:693), so an entry can only be attributed when it carries one
    or when there is a single folder to attribute it to."""
    mine = [s for s in skipped if isinstance(s, dict)
            and (s.get("dir") == idx or (s.get("dir") is None and n_dirs == 1 and idx == 0))]
    return json.dumps(mine, ensure_ascii=False) if mine else None


# ---------------------------------------------------------------------------

def run() -> None:
    db = SessionLocal()
    try:
        created, cleared, warnings = _migrate_rules(db)
        n_obs, n_files = _backfill(db)
        if created or cleared or n_obs:
            db.commit()
            print(f"[lineage-migrate] overrides created: {created}; "
                  f"observations backfilled: {n_obs} ({n_files} files)")
        else:
            db.rollback()  # nothing to do — the common case on every later boot
        # Warnings repeat on every boot on purpose: rules that reached no
        # generator are a standing condition, not a one-time event.
        for w in warnings:
            print(f"[lineage-migrate] warning: {w}")
    except Exception as exc:  # a failed migration must not prevent startup
        db.rollback()
        print(f"[lineage-migrate] FAILED, left untouched: {type(exc).__name__}: {exc}")
    finally:
        db.close()
