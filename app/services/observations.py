"""Ingest of what a run's `input_manifest` says it will read, and of the
structure/consumption batches the browser engine posts back while it reads it.

Two moments, two functions on the happy path:

  * `ingest_run_start` — the run just began: upsert a `DataSource` row per
    folder the engine enumerated, record ONE `SourceObservation` per folder
    (what the walk saw before any file was opened), and a `SourceFile` row
    per attributed file so later batches (`apply_batch`) have something to
    update. Also hands back `last_known`, so the engine can skip re-sniffing
    a file whose (name, size, mtime_ms) it already recorded last time.
  * `apply_batch` — the engine streamed a batch of per-file results (kind,
    observed schema, consumed columns) during the run.
  * `finalize` — the run is done: roll the observation summary up onto the
    `DataSource` row the catalogue page reads.

All three are plain functions over a caller-owned `Session` — this module
never commits; `app/api/generators.py` decides the commit boundary, same
discipline as `datasources.py` and `genmanifest.py`.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    DataSource, FILE_KINDS, GenerationRun, GeneratorVersion, QualityResult,
    RunStage, SourceFile, SourceObservation, ValueBaseline,
)
from app.services import genmanifest

# input_manifest size guard (POST …/runs). Numbers per the plan: a folder
# union tops out at 8 (main + 7 extra FSA handles), 5000 files comfortably
# covers the largest known folder (140 files) with headroom, and 1 MB of JSON
# for a file listing is already generous — a manifest anywhere near these caps
# is not a real run, it is a client bug or an attack.
MAX_MANIFEST_BYTES = 1_000_000
MAX_MANIFEST_FILES = 5000
MAX_MANIFEST_DIRS = 8

# Structure budget per file, enforced when a batch sets schema_json. Mirrors
# the manifest's own inputs[].sheets limits (genmanifest._MAX_INPUT_SHEETS /
# _MAX_INPUT_COLUMNS / _MAX_COLUMN_LEN) so an observed workbook can never cost
# more to store than the declaration that describes it.
MAX_SCHEMA_SHEETS = 32
MAX_SCHEMA_HEADER_CELLS = 256
MAX_SCHEMA_CHARS = 64

# Largest int an SQLite INTEGER column (and every consumer that round-trips
# through JSON/JS) is guaranteed to hold without precision loss or overflow.
_MAX_SAFE_INT = 2 ** 53

# Quality budget per file, enforced when a batch sets checks/profiles. Same
# spirit as the schema budget above: an observed file can never cost more to
# store than what the manifest declared it would check.
_MAX_CHECKS_PER_FILE = 64
_MAX_PROFILES_PER_FILE = 256
_MAX_EXAMPLES = 5
_MAX_METRIC_STR = 64
# ref_lookup `missing`: mirrors static/py/_lineage.py MAX_MISSING (app.* cannot
# be imported there, so the number is stated twice and pinned by both smokes).
_MAX_MISSING = 500
_MAX_REASON_LEN = 64
_STATUS_VALUES = {"pass", "fail", "skipped"}
_PROFILE_TYPES = {"number", "text", "date", "mixed", "empty"}

# Stage budget per RUN (P4.1), mirroring the engine-side caps in
# static/py/_lineage.py so the two sides cannot disagree about what a
# well-behaved run may send: 32 `onepage.stage()` calls, 256 column names and
# 64 column profiles per stage. The engine already drops everything above
# these; the server re-applies them because an engine is a client and a
# client's promises are not a validation.
MAX_STAGES_PER_RUN = 32
MAX_STAGE_COLUMNS = 256
MAX_STAGE_PROFILES = 64
# A stage's read_files is the file names the hooks saw between this call and
# the previous one. A run tops out at MAX_MANIFEST_FILES files overall, but no
# single step of a report meaningfully reads more than this many DISTINCT
# files — and the graph only uses the names to find the patterns they match.
MAX_STAGE_READ_FILES = 256
_MAX_STAGE_NAME = 64
_MAX_STAGE_FILENAME = 255


def check_manifest_size(input_manifest: dict) -> None:
    """Raise ValueError if `input_manifest` is too big to ingest. Called
    BEFORE any row is written, so a 413 leaves no partial run behind."""
    if not isinstance(input_manifest, dict):
        return
    dirs = input_manifest.get("dirs")
    if isinstance(dirs, list) and len(dirs) > MAX_MANIFEST_DIRS:
        raise ValueError(f"input_manifest lists more than {MAX_MANIFEST_DIRS} folders")
    files = input_manifest.get("files")
    if isinstance(files, list) and len(files) > MAX_MANIFEST_FILES:
        raise ValueError(f"input_manifest lists more than {MAX_MANIFEST_FILES} files")
    size = len(json.dumps(input_manifest, ensure_ascii=False).encode("utf-8"))
    if size > MAX_MANIFEST_BYTES:
        raise ValueError(
            f"input_manifest is {size} bytes, over the {MAX_MANIFEST_BYTES}-byte limit")


# The path and root come only from the desktop application (RFC_DESKTOP_MIRROR,
# "Window and folders", decision 3): the browser fundamentally does not know paths. Hence the fields
# are optional and parsing is silent: garbage in a field the platform has always
# lived without must not cost a run its observations (a 400 on the final batch
# would lose the whole structure of a successful run — same logic as for stages).
MAX_PATH_LEN = 512      # = length of the source_files.path / data_sources.root_path columns
MAX_ROOT_ID_LEN = 32    # = data_sources.root_id; folders.identity yields 16 hex


def clean_path(val) -> str | None:
    """A path string or None. Truncate to the column rather than refuse: the truncated
    tail of a long UNC path still shows which file it is, while a refusal
    would leave the person with no path at all. NUL is stripped — SQLite stores it,
    but it truncates the string in any C consumer (copying, export)."""
    if not isinstance(val, str):
        return None
    val = val.replace("\x00", "").strip()
    return val[:MAX_PATH_LEN] or None


def clean_root_id(val) -> str | None:
    if not isinstance(val, str):
        return None
    return val.replace("\x00", "").strip()[:MAX_ROOT_ID_LEN] or None


def roots_by_dir(roots, n_dirs: int) -> dict[int, tuple[str | None, str | None]]:
    """{dir_index: (root_path, root_id)} from the optional list
    `[{dir, root_path, root_id}]`. Anything unrecognised (not a list, not a dict,
    `dir` out of range, bool instead of a number) is silently skipped."""
    out: dict[int, tuple[str | None, str | None]] = {}
    if not isinstance(roots, list):
        return out
    for r in roots:
        if not isinstance(r, dict):
            continue
        idx = r.get("dir", 0)
        if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0 or idx >= n_dirs:
            continue
        rp, rid = clean_path(r.get("root_path")), clean_root_id(r.get("root_id"))
        if rp or rid:
            out[idx] = (rp, rid)
    return out


def _apply_root(ds: DataSource, root: tuple[str | None, str | None] | None) -> None:
    """The latest observation overwrites the previous one, but the ABSENCE of a field does not
    erase: a browser run of the same source sends no root, and without
    this caveat it would null out what the application knew."""
    if not root:
        return
    if root[0]:
        ds.root_path = root[0]
    if root[1]:
        ds.root_id = root[1]


def _mtime_to_dt(ms) -> datetime | None:
    """mtime_ms is a JS epoch-millisecond number from the analyst's machine.
    Copied (not imported) from datasources.py — same convention as the
    other private `_log` helpers in this codebase: small, stable, cheaper to
    duplicate than to couple two modules over."""
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _skipped_for(skipped: list, idx: int, n_dirs: int) -> str | None:
    """The engine concatenates the per-folder skip lists (gen-engine.js), so
    an entry can only be attributed to folder `idx` when it carries a `dir`
    tag matching it, or when there is a single folder to attribute it to.
    Mirrors lineage_migrate._skipped_for (kept separate on purpose — same
    convention as _mtime_to_dt above)."""
    mine = [s for s in skipped if isinstance(s, dict)
            and (s.get("dir") == idx or (s.get("dir") is None and n_dirs == 1 and idx == 0))]
    return json.dumps(mine, ensure_ascii=False) if mine else None


def ingest_run_start(db: Session, run: GenerationRun, input_manifest: dict | None) -> dict:
    """Upsert sources + write one SourceObservation/SourceFile set per folder
    the manifest names. `run` must already have an id (caller flushes first).

    An agent-engine run posts a constant manifest with no `dirs` — this is
    read the same as no folders at all: no observations, no error, just
    `{"observation_ids": {}, "last_known": {}}`.
    """
    if not isinstance(input_manifest, dict):
        return {"observation_ids": {}, "last_known": {}}
    raw_dirs = input_manifest.get("dirs")
    dirs = [str(d) for d in raw_dirs] if isinstance(raw_dirs, list) else []
    if not dirs:
        return {"observation_ids": {}, "last_known": {}}

    raw_files = input_manifest.get("files")
    files = raw_files if isinstance(raw_files, list) else []
    raw_skipped = input_manifest.get("skipped")
    skipped = raw_skipped if isinstance(raw_skipped, list) else []

    # Attribute every non-aux file to its folder up front — same rule as
    # datasources.build(): `dir` absent means index 0, `aux: true` files
    # (the analyst's own upload) belong to no folder at all.
    by_dir: dict[int, list[dict]] = {}
    for f in files:
        if not isinstance(f, dict) or f.get("aux") or not f.get("name"):
            continue
        idx = f.get("dir") or 0
        if not isinstance(idx, int) or idx < 0 or idx >= len(dirs):
            continue
        by_dir.setdefault(idx, []).append(f)

    now = datetime.utcnow()
    observation_ids: dict[int, int] = {}
    last_known: dict[str, list] = {}
    roots = roots_by_dir(input_manifest.get("roots"), len(dirs))

    for i, name in enumerate(dirs):
        ds = (db.query(DataSource)
              .filter(DataSource.kind == "folder", DataSource.key_name == name)
              .first())
        if ds is None:
            ds = DataSource(kind="folder", key_name=name, first_seen_at=now)
            db.add(ds)
            db.flush()  # need ds.id for the observation FK below
        _apply_root(ds, roots.get(i))

        # The PREVIOUS observation of this source (any run but this one) is
        # what lets the engine skip re-sniffing an unchanged file — it never
        # opens a file it already knows the (size, mtime_ms) of.
        prev = (db.query(SourceObservation)
                .filter(SourceObservation.data_source_id == ds.id,
                        SourceObservation.run_id != run.id)
                .order_by(SourceObservation.id.desc())
                .first())
        if prev is not None:
            for sf_name, sf_size, sf_mtime in (
                db.query(SourceFile.name, SourceFile.size, SourceFile.mtime_ms)
                .filter(SourceFile.observation_id == prev.id).all()
            ):
                if sf_size is not None and sf_mtime is not None:
                    last_known[sf_name] = [sf_size, sf_mtime]

        dfiles = by_dir.get(i, [])
        total_bytes = 0
        newest_mtime: datetime | None = None
        names_for_fp: list[str] = []
        for f in dfiles:
            names_for_fp.append(str(f["name"]).casefold())
            size = f.get("size")
            if isinstance(size, (int, float)) and size > 0:
                total_bytes += int(size)
            mt = _mtime_to_dt(f.get("mtime_ms"))
            if mt and (newest_mtime is None or mt > newest_mtime):
                newest_mtime = mt

        fingerprint = hashlib.sha256(
            "\n".join(sorted(names_for_fp)).encode("utf-8")
        ).hexdigest()

        obs = SourceObservation(
            run_id=run.id, data_source_id=ds.id, dir_index=i,
            file_count=len(dfiles), total_bytes=total_bytes,
            newest_mtime=newest_mtime, fingerprint=fingerprint,
            skipped_json=_skipped_for(skipped, i, len(dirs)), truncated=False,
        )
        db.add(obs)
        db.flush()  # need obs.id for the SourceFile rows below
        observation_ids[i] = obs.id

        for f in dfiles:
            size = f.get("size")
            mt = f.get("mtime_ms")
            matched = f.get("matched_input")
            db.add(SourceFile(
                observation_id=obs.id, name=str(f["name"]),
                size=int(size) if isinstance(size, (int, float)) else None,
                mtime_ms=int(mt) if isinstance(mt, (int, float)) else None,
                kind="listed",
                matched_input=matched if isinstance(matched, str) else None,
                path=clean_path(f.get("path")),
            ))

    return {"observation_ids": observation_ids, "last_known": last_known}


def _truncate_schema(schema: dict) -> tuple[dict, bool]:
    """Enforce the structure budget on an observed schema, truncating rather
    than rejecting — a batch mid-run is not the moment to fail the run over
    an oversized workbook, it is the moment to note it (`truncated=True`)."""
    sheets = schema.get("sheets")
    if not isinstance(sheets, list):
        return schema, False

    truncated = len(sheets) > MAX_SCHEMA_SHEETS
    out_sheets = []
    for sh in sheets[:MAX_SCHEMA_SHEETS]:
        if not isinstance(sh, dict):
            out_sheets.append(sh)
            continue
        header = sh.get("header")
        new_sh = dict(sh)
        if isinstance(header, list):
            if len(header) > MAX_SCHEMA_HEADER_CELLS:
                truncated = True
            new_header = []
            for cell in header[:MAX_SCHEMA_HEADER_CELLS]:
                if isinstance(cell, str) and len(cell) > MAX_SCHEMA_CHARS:
                    truncated = True
                    new_header.append(cell[:MAX_SCHEMA_CHARS])
                else:
                    new_header.append(cell)
            new_sh["header"] = new_header
        out_sheets.append(new_sh)

    out = dict(schema)
    out["sheets"] = out_sheets
    return out, truncated


def _parse_check_id(check_id: str) -> tuple[str, str, str, str] | None:
    """Split a check_id "{type}:{match}:{sheet}:{column}" from the RIGHT:
    `column` is the last segment, `sheet` the second-last, `match` is
    everything left in between. `match` is NOT assumed colon-free — a glob
    pattern is only forbidden "/" by genmanifest.globmatch.validate_pattern,
    never ":" — so it cannot be split from the LEFT after the fixed `type`
    prefix. `type`, at the front, is the one fixed-position field on that
    side. Returns None for anything with fewer than 4 colon-separated parts
    or an empty field — malformed check_ids are dropped by the caller, not
    raised on; a batch mid-run is not the moment to fail loudly. `sheet` and
    `column` are trimmed to MAX_SCHEMA_CHARS here, once, so every consumer
    (QualityResult's sheet/column columns and ValueBaseline's, which share
    the same String(64) width) agrees on the same value — the QualityResult
    row and the baseline key it later seeds can never disagree over a
    >64-char name."""
    parts = check_id.split(":")
    if len(parts) < 4:
        return None
    ctype = parts[0]
    column = parts[-1][:MAX_SCHEMA_CHARS]
    sheet = parts[-2][:MAX_SCHEMA_CHARS]
    match = ":".join(parts[1:-2])
    if not (ctype and match and sheet and column):
        return None
    return ctype, match, sheet, column


def _clean_examples(val) -> list[str]:
    if not isinstance(val, list):
        return []
    return [v[:_MAX_METRIC_STR] for v in val[:_MAX_EXAMPLES] if isinstance(v, str)]


def _clean_distinct(val) -> list[str] | None:
    if not isinstance(val, list):
        return None
    return [v[:_MAX_METRIC_STR] for v in val[:genmanifest.MAX_KNOWN_VALUES] if isinstance(v, str)]


def _clean_missing(val) -> list[str] | None:
    """`missing` of a ref_lookup result: the ORIGINAL text of the values that
    matched no key in the platform reference table. Same discipline and the
    same ceilings as `distinct` (the engine already caps at MAX_MISSING and
    cuts every value to MAX_CELL_CHARS; this re-applies both, because the
    batch arrives over the network). Never a baseline — nothing is seeded
    from it; it is what `quality._drift_block` re-checks against the table's
    current keys, so an import can close the finding without a rerun."""
    if not isinstance(val, list):
        return None
    return [v[:_MAX_METRIC_STR] for v in val[:_MAX_MISSING] if isinstance(v, str)]


def _clean_int(val) -> int | None:
    if isinstance(val, int) and not isinstance(val, bool) and 0 <= val <= _MAX_SAFE_INT:
        return val
    return None


def _clean_check_item(item: dict) -> dict | None:
    """Validate + trim one entry of a batch's `checks` list (the engine's
    run_checks() output, one per {sheet,column,type}). Returns None for
    anything malformed — the caller drops it silently rather than failing
    the whole batch over one bad item."""
    if not isinstance(item, dict):
        return None
    check_id = item.get("check_id")
    if not isinstance(check_id, str) or not check_id or len(check_id) > 256:
        return None
    parsed = _parse_check_id(check_id)
    if parsed is None:
        return None
    ctype, _match, sheet, column = parsed
    if ctype not in genmanifest.CHECK_TYPES:
        return None
    status = item.get("status")
    if status not in _STATUS_VALUES:
        return None
    reason = item.get("reason")
    reason = reason[:_MAX_REASON_LEN] if isinstance(reason, str) else None
    return {
        "check_id": check_id,
        "check_type": ctype,
        "sheet": sheet,
        "column": column,
        "status": status,
        "count": _clean_int(item.get("count")),
        "failed": _clean_int(item.get("failed")),
        "sampled": bool(item.get("sampled")) if isinstance(item.get("sampled"), bool) else False,
        "metric": {
            "examples": _clean_examples(item.get("examples")),
            "distinct": _clean_distinct(item.get("distinct")),
            "distinct_overflow": bool(item.get("distinct_overflow")),
            "missing": _clean_missing(item.get("missing")),
            "missing_overflow": bool(item.get("missing_overflow")),
            "reason": reason,
        },
    }


def _clean_profile_scalar(val):
    if isinstance(val, str):
        return val[:_MAX_METRIC_STR]
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return val
    return None


def _clean_profile_item(item: dict) -> dict | None:
    """Validate + trim one entry of a batch's `profiles` list (one per
    declared column). Returns None for anything malformed."""
    if not isinstance(item, dict):
        return None
    sheet, column = item.get("sheet"), item.get("column")
    if not isinstance(sheet, str) or not sheet.strip() or not isinstance(column, str) or not column.strip():
        return None
    sheet = sheet.strip()[:MAX_SCHEMA_CHARS]
    column = column.strip()[:MAX_SCHEMA_CHARS]
    ptype = item.get("type")
    if ptype not in _PROFILE_TYPES:
        ptype = None
    top: list[list] = []
    top_raw = item.get("top")
    if isinstance(top_raw, list):
        for pair in top_raw[:5]:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                v, n = pair
                n = _clean_int(n)
                if isinstance(v, str) and n is not None:
                    top.append([v[:_MAX_METRIC_STR], n])
    sampled = bool(item.get("sampled")) if isinstance(item.get("sampled"), bool) else False
    return {
        "check_id": f"profile:{sheet}:{column}",
        "sheet": sheet,
        "column": column,
        "sampled": sampled,
        "metric": {
            "type": ptype,
            "nulls": _clean_int(item.get("nulls")),
            "distinct": _clean_int(item.get("distinct")),
            "min": _clean_profile_scalar(item.get("min")),
            "max": _clean_profile_scalar(item.get("max")),
            "top": top,
            "sampled": sampled,
        },
    }


def _apply_quality(db: Session, source_file: SourceFile, observation: SourceObservation,
                    checks, profiles) -> None:
    """Ingest one file's `checks` + `profiles` results, REPLACING whatever
    quality_results this source_file already had. Delete-then-insert makes a
    replayed batch idempotent (same seq re-sent -> same rows, no doubling)
    and a later batch for the same file (different seq — e.g. the engine's
    own check pass supersedes an earlier partial one) a correct overwrite,
    the same discipline `apply_batch` already uses for schema_json/consumed.

    Items are capped (_MAX_CHECKS_PER_FILE / _MAX_PROFILES_PER_FILE) and
    individually validated by `_clean_check_item`/`_clean_profile_item`;
    malformed ones are silently dropped rather than failing the batch.
    Duplicate check_ids within one batch (a buggy engine re-emitting the
    same check) are collapsed to the LAST occurrence, matching the
    UNIQUE(source_file_id, check_id) constraint the table already carries."""
    db.query(QualityResult).filter(
        QualityResult.source_file_id == source_file.id).delete(synchronize_session=False)

    rows: dict[str, QualityResult] = {}
    for item in (checks or [])[:_MAX_CHECKS_PER_FILE]:
        cleaned = _clean_check_item(item)
        if cleaned is None:
            continue
        rows[cleaned["check_id"]] = QualityResult(
            source_file_id=source_file.id, observation_id=observation.id,
            check_id=cleaned["check_id"], check_type=cleaned["check_type"],
            sheet=cleaned["sheet"], column=cleaned["column"], status=cleaned["status"],
            count=cleaned["count"], failed=cleaned["failed"], sampled=cleaned["sampled"],
            metric_json=json.dumps(cleaned["metric"], ensure_ascii=False),
        )
    for item in (profiles or [])[:_MAX_PROFILES_PER_FILE]:
        cleaned = _clean_profile_item(item)
        if cleaned is None:
            continue
        rows[cleaned["check_id"]] = QualityResult(
            source_file_id=source_file.id, observation_id=observation.id,
            check_id=cleaned["check_id"], check_type="profile",
            sheet=cleaned["sheet"], column=cleaned["column"], status="pass",
            count=None, failed=None, sampled=cleaned["sampled"],
            metric_json=json.dumps(cleaned["metric"], ensure_ascii=False),
        )
    for row in rows.values():
        db.add(row)
    # This session runs with autoflush=False (repo-wide convention); flush
    # explicitly so `finalize`, called later in the same request with the
    # same session, sees these rows when it queries for known_values checks
    # to seed baselines from.
    db.flush()


def _clean_names(val, limit: int, max_len: int) -> list[str]:
    """A bounded list of trimmed non-empty strings — column names, stage
    input names, file names. Non-strings are skipped, not raised on."""
    if not isinstance(val, list):
        return []
    out: list[str] = []
    for v in val:
        if not isinstance(v, str):
            continue
        v = v.strip()
        if not v:
            continue
        out.append(v[:max_len])
        if len(out) >= limit:
            break
    return out


def _clean_ms(val) -> int | None:
    """A duration the engine measured with `perf_counter` — it arrives as a
    FLOAT (`static/py/_lineage.py` rounds to 0.1 ms). `_clean_int` would drop
    it and silently lose every stage timing, so numbers are accepted and
    rounded here; the column is an INT because a stage worth naming is worth
    milliseconds, and a sub-millisecond one honestly reads as 0."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    if val != val or val in (float("inf"), float("-inf")) or val < 0:  # NaN/inf/negative
        return None
    if val > _MAX_SAFE_INT:  # oversized -> SQLite INTEGER overflow
        return None
    return int(round(val))


def _clean_stage_profile(item) -> dict | None:
    """One entry of a stage's `profiles` — the same shape as a FILE profile
    (`_clean_profile_item`) minus the `sheet`, which an in-memory DataFrame
    does not have, plus `count` (rows examined), which it does. Kept as a
    separate function rather than a parameterised one: the two shapes are
    pinned by two different producers, and a shared cleaner would have to be
    edited in lockstep with both."""
    if not isinstance(item, dict):
        return None
    column = item.get("column")
    if not isinstance(column, str) or not column.strip():
        return None
    ptype = item.get("type")
    if ptype not in _PROFILE_TYPES:
        ptype = None
    top: list[list] = []
    top_raw = item.get("top")
    if isinstance(top_raw, list):
        for pair in top_raw[:5]:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                v, n = pair
                n = _clean_int(n)
                if isinstance(v, str) and n is not None:
                    top.append([v[:_MAX_METRIC_STR], n])
    return {
        "column": column.strip()[:MAX_SCHEMA_CHARS],
        "type": ptype,
        "nulls": _clean_int(item.get("nulls")),
        "count": _clean_int(item.get("count")),
        "distinct": _clean_int(item.get("distinct")),
        "min": _clean_profile_scalar(item.get("min")),
        "max": _clean_profile_scalar(item.get("max")),
        "top": top,
        "sampled": item.get("sampled") is True,
    }


def _clean_stage_item(item) -> dict | None:
    """Validate + trim one entry of a batch's `stages` list. Returns None for
    anything without a usable name — a stage IS its name, and an unnamed one
    could not be referenced by another stage's `inputs` anyway.

    `order` is returned as given (an int in range) or None when absent/bad;
    `_apply_stages` is the one that knows which orders are already claimed by
    other items in the same batch, so it — not this function — picks the
    fallback slot for a None here."""
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    order = item.get("order")
    if not (isinstance(order, int) and not isinstance(order, bool)
            and 0 <= order < MAX_STAGES_PER_RUN):
        order = None
    raw_inputs = item.get("inputs")
    # None and [] are different (see RunStage.get_inputs): only a real list
    # becomes a stored list; anything else is "not declared".
    inputs_clean = (_clean_names(raw_inputs, MAX_STAGES_PER_RUN, _MAX_STAGE_NAME)
                    if isinstance(raw_inputs, list) else None)
    raw_profiles = item.get("profiles")
    profiles: list[dict] = []
    if isinstance(raw_profiles, list):
        for x in raw_profiles[:MAX_STAGE_PROFILES]:
            cleaned_profile = _clean_stage_profile(x)
            if cleaned_profile is not None:
                profiles.append(cleaned_profile)
    return {
        "order": order,
        "name": name.strip()[:_MAX_STAGE_NAME],
        "rows": _clean_int(item.get("rows")),
        "columns": _clean_names(item.get("columns"), MAX_STAGE_COLUMNS, MAX_SCHEMA_CHARS),
        "profiles": profiles,
        "inputs": inputs_clean,
        "read_files": _clean_names(item.get("read_files"), MAX_STAGE_READ_FILES,
                                   _MAX_STAGE_FILENAME),
        "ms": _clean_ms(item.get("ms")),
    }


def _apply_stages(db: Session, run: GenerationRun, stages) -> int:
    """Ingest a batch's `stages`, REPLACING every run_stage this run had.

    Replace-all (not per-item upsert) because the engine accumulates stages
    for the whole run and re-sends the WHOLE list with the final batch: the
    last list is the truth, and a replay of it must not double rows or leave
    behind a stage the generator stopped emitting. Same delete-then-insert
    discipline as `_apply_quality`, one level up (per run, not per file),
    matching UNIQUE(run_id, order).

    Malformed items are dropped silently and the batch still succeeds — a
    stage is a narrative aid, never a reason to fail somebody's run.
    """
    db.query(RunStage).filter(RunStage.run_id == run.id).delete(synchronize_session=False)
    if not isinstance(stages, list):
        return 0

    # Two passes: first collect every cleaned item, keeping its explicit
    # order (or None). Then hand out orders to the None ones from whatever
    # slots 0..MAX_STAGES_PER_RUN-1 the explicit orders left free, in the
    # items' own sequence — so a batch that forgot `order` on some items
    # still lands every named stage instead of one silently overwriting
    # another that happened to fall back to the same position.
    cleaned_items = []
    claimed: set[int] = set()
    for item in stages[:MAX_STAGES_PER_RUN]:
        cleaned = _clean_stage_item(item)
        if cleaned is None:
            continue
        cleaned_items.append(cleaned)
        if cleaned["order"] is not None:
            claimed.add(cleaned["order"])

    free_orders = (o for o in range(MAX_STAGES_PER_RUN) if o not in claimed)
    rows: dict[int, RunStage] = {}
    for cleaned in cleaned_items:
        order = cleaned["order"]
        if order is None:
            order = next(free_orders, None)
            if order is None:  # no free slot left (shouldn't happen: <=32 items)
                continue
        # Two items claiming the SAME explicit order: the LAST wins, as
        # everywhere else in this module — the unique key admits exactly one.
        rows[order] = RunStage(
            run_id=run.id, order=order, name=cleaned["name"],
            rows=cleaned["rows"],
            columns_json=json.dumps(cleaned["columns"], ensure_ascii=False),
            profiles_json=json.dumps(cleaned["profiles"], ensure_ascii=False),
            inputs_json=(json.dumps(cleaned["inputs"], ensure_ascii=False)
                         if cleaned["inputs"] is not None else None),
            read_files_json=json.dumps(cleaned["read_files"], ensure_ascii=False),
            ms=cleaned["ms"],
        )
    for row in rows.values():
        db.add(row)
    db.flush()
    return len(rows)


def apply_batch(db: Session, run: GenerationRun, body) -> dict:
    """Apply one observations batch (`ObservationBody`) to this run's
    SourceFile rows. Raises ValueError for anything the caller should turn
    into a 400 — an unknown dir/file, or a kind outside FILE_KINDS.

    `stages` (P4.1) is optional and independent of `files`: a batch may carry
    stages with an empty file list (the final flush usually does), and a
    batch that omits the key entirely leaves the run's existing stages alone
    — only a present list replaces them."""
    obs_by_dir = {
        obs.dir_index: obs
        for obs in db.query(SourceObservation).filter(SourceObservation.run_id == run.id).all()
    }

    # Source roots (optional, only from the application): by folder number
    # we find the observation, and through it the source.
    roots = roots_by_dir(getattr(body, "roots", None),
                         max(obs_by_dir, default=-1) + 1)
    for idx, root in roots.items():
        obs = obs_by_dir.get(idx)
        ds = db.get(DataSource, obs.data_source_id) if obs is not None else None
        if ds is not None:
            _apply_root(ds, root)

    files_updated = 0
    truncated_obs_ids: set[int] = set()
    for f in body.files:
        obs = obs_by_dir.get(f.dir)
        if obs is None:
            raise ValueError(f"no observation for dir {f.dir} on this run")
        sf = (db.query(SourceFile)
              .filter(SourceFile.observation_id == obs.id, SourceFile.name == f.name)
              .first())
        if sf is None:
            raise ValueError(f"{f.name!r} was not listed for dir {f.dir} at run start")
        if f.kind not in FILE_KINDS:
            raise ValueError(f"unknown file kind {f.kind!r}")

        sf.kind = f.kind
        # Only a sent path changes the column: a batch without `path` (the browser,
        # or the application repeating a file for the sake of schema) does not erase a known one.
        new_path = clean_path(getattr(f, "path", None))
        if new_path is not None:
            sf.path = new_path
        if f.schema_ is not None:
            schema, was_truncated = _truncate_schema(f.schema_)
            sf.schema_json = json.dumps(schema, ensure_ascii=False)
            if was_truncated:
                truncated_obs_ids.add(obs.id)
        if f.consumed is not None:
            sf.consumed_json = json.dumps(f.consumed, ensure_ascii=False)
        if f.checks is not None or f.profiles is not None:
            _apply_quality(db, sf, obs, f.checks, f.profiles)
        files_updated += 1

    if truncated_obs_ids:
        (db.query(SourceObservation)
         .filter(SourceObservation.id.in_(truncated_obs_ids))
         .update({"truncated": True}, synchronize_session=False))

    stages_updated = None
    if getattr(body, "stages", None) is not None:
        stages_updated = _apply_stages(db, run, body.stages)

    return {"files_updated": files_updated, "stages_updated": stages_updated}


def known_values_for_run(db: Session, run_id: int) -> list[QualityResult]:
    """QualityResult rows of check_type == "known_values" produced by any
    observation of this run — the pool `finalize` seeds baselines from, and
    what publish's run_summary (a later phase) reads to report new values
    without re-deriving the same join."""
    obs_ids = [row[0] for row in
               db.query(SourceObservation.id).filter(SourceObservation.run_id == run_id).all()]
    if not obs_ids:
        return []
    return (db.query(QualityResult)
            .filter(QualityResult.observation_id.in_(obs_ids),
                    QualityResult.check_type == "known_values")
            .all())


def finalize(db: Session, run: GenerationRun) -> None:
    """Run is done: roll this run's observations up onto the DataSource rows
    the catalogue page reads (last_seen_at/fingerprint/last_file_count) —
    the same summary-on-write discipline as `data_sources.updated_at`.

    Also seeds ValueBaseline rows for every `known_values` check this run
    produced that has no baseline yet: the first run of a report is not an
    alert, it is the reference. `values_json` is the sorted distinct list;
    `seeded_run_id` marks it as machine-created (vs. a human `accepted_at`).
    An EXISTING baseline — seeded or accepted — is never touched here; only
    an explicit accept (a later phase's endpoint) can move it, and it stays
    at version 1 across any number of runs until that happens."""
    now = datetime.utcnow()
    observations = db.query(SourceObservation).filter(SourceObservation.run_id == run.id).all()
    if not observations:
        return
    ds_ids = [obs.data_source_id for obs in observations]
    sources = {
        ds.id: ds
        for ds in db.query(DataSource).filter(DataSource.id.in_(ds_ids)).all()
    }
    for obs in observations:
        ds = sources.get(obs.data_source_id)
        if ds is None:
            continue
        ds.last_seen_at = now
        ds.fingerprint = obs.fingerprint
        ds.last_file_count = obs.file_count

    kv_rows = known_values_for_run(db, run.id)
    if kv_rows:
        gen_row = (db.query(GeneratorVersion.generator_id)
                   .filter(GeneratorVersion.id == run.generator_version_id).first())
        generator_id = gen_row[0] if gen_row else None
        if generator_id is not None:
            obs_by_id = {obs.id: obs for obs in observations}
            # One `match` glob (e.g. "Branches_*.xlsx") can span several
            # SourceFile rows in the same run — each file gets its own
            # QualityResult with its own local `distinct`, but they all seed
            # the SAME ValueBaseline row (UNIQUE on data_source_id/
            # generator_id/match/sheet/column). Group first and union the
            # distinct values across files before touching the table, so
            # every key is looked up/inserted exactly once — querying
            # `exists` per-row would miss a sibling row's INSERT still
            # sitting unflushed in this autoflush=False session and crash
            # on the table's UNIQUE constraint on the second file.
            groups: dict[tuple[int, str, str, str], set[str]] = {}
            # A sheet over MAX_CHECK_ROWS (static/py/_lineage.py) sets
            # sampled=True and still returns the distinct set it collected
            # from the first N rows — a PARTIAL read, not a scan of the whole
            # column. Seeding "the accepted reference set" from that would
            # silently drop every value that only appears after the sample
            # window, and later runs would report those long-existing values
            # as new_values. So a key with ANY sampled contribution this run
            # is excluded from seeding entirely (not just the sampled row) —
            # it stays unseeded and tries again on a future, unsampled run.
            tainted: set[tuple[int, str, str, str]] = set()
            for qr in kv_rows:
                obs = obs_by_id.get(qr.observation_id)
                if obs is None:
                    continue
                parsed = _parse_check_id(qr.check_id)
                if parsed is None:
                    continue
                _ctype, match, sheet, column = parsed
                distinct = qr.get_metric().get("distinct")
                if distinct is None:
                    continue
                key = (obs.data_source_id, match, sheet, column)
                if qr.sampled:
                    tainted.add(key)
                    continue
                groups.setdefault(key, set()).update(distinct)
            for key in tainted:
                groups.pop(key, None)

            for (data_source_id, match, sheet, column), values in groups.items():
                exists = (db.query(ValueBaseline)
                          .filter(ValueBaseline.data_source_id == data_source_id,
                                  ValueBaseline.generator_id == generator_id,
                                  ValueBaseline.match == match,
                                  ValueBaseline.sheet == sheet,
                                  ValueBaseline.column == column)
                          .first())
                if exists is not None:
                    continue
                db.add(ValueBaseline(
                    data_source_id=data_source_id, generator_id=generator_id,
                    match=match, sheet=sheet, column=column,
                    values_json=json.dumps(sorted(values), ensure_ascii=False),
                    seeded_run_id=run.id, version=1,
                ))
