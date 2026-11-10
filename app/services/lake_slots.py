"""Slot -> lake conversion (ADR-14b addendum): agent-built reports publish
their DATA as slot files (csv/xlsx/json), not as a source folder — Van Stock
is the first. To let the chat answer over them, publish converts each slot a
generator's semantics claims (`sources: [{"slot": "data.csv"}]`) into a lake
Parquet partition, server-side, in the publish request itself. From there the
ordinary ADR-14b path takes over: `lake_exec` indexes the partition under the
synthetic table name `slot__<semantic table>`, the sem views build on it, the
chat asks.

Decisions this file encodes (2026-08-24):
  * `parser_version` is "slot-1", NOT the engine's PARSER_VERSION: that
    constant versions the Excel->frame coercions, which a converted slot
    never saw. Values are written as VARCHAR and typed downstream by
    `_sem_cast`, so "slot-1" is bumped only when THIS conversion changes.
    `lake_exec` accepts both versions; the browser download path refuses
    slot manifests (its gate equals the worker's version) — slot reports
    answer on the server, honestly.
  * Conversion lives in the PUBLISH path (best-effort, never fails a
    publish) — the general mechanism for any agent report, not a Van Stock
    special case. It replaces the dashboard's whole manifest, so v1 supports
    generators whose semantics are slot-only; a folder generator's own lake
    step (browser export) is a different, later writer and wins by being
    last.
  * Cells never reach the MODEL: this file talks to disk, not to any LLM.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.i18n import _
from app.models import Dashboard, DashboardFile, GenerationRun, GeneratorVersion, User
from app.services import storage
from app.services.catalog import norm_col
from app.services.semantics import slot_table_name

PARSER_VERSION_SLOT = "slot-1"

_MAX_SLOT_ROWS = 2_000_000      # a slot table past this is not a chat answer's source
SLOT_PARTITION = "slot"         # the one partition every converted slot lives in


def slot_sources(sem: dict) -> list[tuple[str, dict, dict]]:
    """Every (table_name, table, source) whose source names a slot file."""
    out = []
    for tname, t in (sem.get("tables") or {}).items():
        if not isinstance(t, dict) or t.get("rows") is not None:
            continue
        for s in t.get("sources") or []:
            if isinstance(s, dict) and s.get("slot"):
                out.append((tname, t, s))
    return out


def _norm_headers(raw_cols: list) -> list[str | None]:
    """Slot headers -> catalogue keys, duplicates suffixed `__{k+1}` — the
    SAME recipe as `_explore._frame_columns`/`semantics.source_key`, so a
    declared `source` header lands on the converted column by construction."""
    seen: dict[str, int] = {}
    out: list[str | None] = []
    for c in raw_cols:
        key = norm_col(str(c)) if c is not None else None
        if not key or key.startswith("__"):
            out.append(None)
            continue
        k = seen.get(key, 0)
        seen[key] = k + 1
        out.append(key if k == 0 else f"{key}__{k + 1}")
    return out


def _cell(v):
    if v is None:
        return None
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return str(v)


def _read_slot(path: Path, original_name: str, src: dict):
    """One slot file -> (columns, rows-of-text). Formats: csv/tsv/txt, xlsx,
    json (array of objects, or {"columns": [...], "rows": [[...]]})."""
    import pandas as pd
    lname = original_name.lower()
    # `.csv.gz` keeps its double extension on disk, and DuckDB's reader
    # picks gzip up from the file name itself — Van Stock's 1.6M-row export
    # is exactly this shape.
    if lname.endswith((".csv.gz", ".tsv.gz", ".txt.gz")):
        ext = lname[:-3].rsplit(".", 1)[-1]
    else:
        ext = lname.rsplit(".", 1)[-1] if "." in lname else ""
    if ext in ("csv", "tsv", "txt"):
        import duckdb
        con = duckdb.connect(":memory:")
        try:
            df = con.execute(
                "SELECT * FROM read_csv(?, all_varchar=true, header=true, "
                "sample_size=-1)", [str(path)]).df()
        finally:
            con.close()
    elif ext in ("xlsx", "xlsm"):
        sheet = src.get("sheet")
        df = pd.read_excel(path, sheet_name=sheet if sheet not in (None, "", "*") else 0,
                           header=int(src.get("header_row") or 0), dtype=object)
    elif ext == "json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("columns"), list):
            df = pd.DataFrame(list(data.get("rows") or []), columns=data["columns"], dtype=object)
        elif isinstance(data, list):
            df = pd.DataFrame(data, dtype=object)
        else:
            raise ValueError("json slot must be an array of objects or {columns, rows}")
    else:
        raise ValueError(_("format .{ext} cannot be converted (csv/xlsx/json)", ext=ext or "?"))
    if len(df) > _MAX_SLOT_ROWS:
        raise ValueError(_("too many rows: {n}", n=len(df)))
    keys = _norm_headers(list(df.columns))
    cols, idxs = [], []
    for i, k in enumerate(keys):
        if k is not None:
            cols.append(k)
            idxs.append(i)
    if not cols:
        raise ValueError(_("the file has no column with a header"))
    rows = [[_cell(r[i]) for i in idxs] for r in df.itertuples(index=False, name=None)]
    return cols, rows


def _to_parquet(cols: list[str], rows: list[list], original_name: str) -> bytes:
    """VARCHAR-only Parquet with the engine's reserved provenance columns —
    exactly the shape `index_cached` reads back."""
    import duckdb
    import pandas as pd
    import tempfile
    import os
    df = pd.DataFrame(rows, columns=cols, dtype=object)
    df["__partition"] = SLOT_PARTITION
    df["__file"] = original_name
    con = duckdb.connect(":memory:")
    fd, tmp = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    try:
        con.register("_slot_out", df)
        cast = ", ".join(f'CAST("{c}" AS VARCHAR) AS "{c}"' for c in list(df.columns))
        con.execute(f"COPY (SELECT {cast} FROM _slot_out) TO '{tmp}' "
                    "(FORMAT PARQUET, COMPRESSION ZSTD)")
        return Path(tmp).read_bytes()
    finally:
        con.close()
        try:
            os.unlink(tmp)
        except OSError:
            pass


def convert_run(db: Session, dashboard: Dashboard, run: GenerationRun,
                version: int, user: User) -> dict | None:
    """Convert this publish's slot files into the dashboard's lake. Returns a
    summary dict, or None when the generator's semantics claim no slots.
    Raises nothing the caller must survive — every per-slot failure is an
    entry in `errors`, and only a total failure (no semantics, no files at
    all) returns early."""
    if not dashboard.generator_id:
        return None
    gv = db.query(GeneratorVersion.semantics_json).filter(
        GeneratorVersion.id == run.generator_version_id).first()
    if not gv or not gv[0]:
        return None
    try:
        sem = json.loads(gv[0])
    except (TypeError, ValueError):
        return None
    wanted = slot_sources(sem)
    if not wanted:
        return None

    slots = {f.original_name: f for f in db.query(DashboardFile).filter(
        DashboardFile.dashboard_id == dashboard.id,
        DashboardFile.version_number == version).all()}

    entries, errors, total = [], [], 0
    for tname, _t, src in wanted:
        fname = str(src.get("slot"))
        f = slots.get(fname)
        if f is None:
            # Forgiving, still deterministic: `"slot": "van_stock_daily"`
            # finds `van_stock_daily.csv.gz` as long as exactly ONE published
            # slot has that stem (name up to the first dot). Ambiguity stays
            # an error — guessing between two files is worse than asking.
            stem_hits = [g for name, g in slots.items()
                         if name.split(".", 1)[0] == fname.split(".", 1)[0]]
            if len(stem_hits) == 1:
                f = stem_hits[0]
        if f is None:
            have = ", ".join(sorted(slots)) or _("nothing")
            errors.append({"slot": fname,
                           "error": _("the slot is not published in this version (available: {have})", have=have)})
            continue
        # stored_path is relative to the uploads root (files.py convention).
        path = settings.abs_uploads_dir / f.stored_path
        try:
            # Read by the PUBLISHED file's name (format detection needs the
            # real extension), but stamp `__file`/manifest `name` with the
            # DECLARED slot value: the sem view filters partitions with
            # `WHERE __file IN (binding.files)`, and the binding is built
            # from the same declaration — consistent by construction,
            # whether the author wrote the name with or without extension.
            cols, rows = _read_slot(path, f.original_name, src)
            parquet = _to_parquet(cols, rows, fname)
        except Exception as e:
            errors.append({"slot": fname, "error": str(e)[:200]})
            continue
        key = hashlib.sha1(parquet).hexdigest()
        storage.save_lake_part(dashboard.id, key, parquet)
        try:
            st = path.stat()
            size, mtime_ms = st.st_size, int(st.st_mtime * 1000)
        except OSError:
            size, mtime_ms = f.size_bytes or 0, int(datetime.utcnow().timestamp() * 1000)
        entries.append({
            "name": fname, "size": size, "mtime_ms": mtime_ms,
            "sheet": src.get("sheet"), "header_row": int(src.get("header_row") or 0),
            "key": key, "table": slot_table_name(tname),
            "partition": SLOT_PARTITION, "bytes": len(parquet),
        })
        total += len(parquet)

    if not entries:
        return {"files": 0, "bytes": 0, "errors": errors}

    manifest = {
        "run_id": run.id,
        "parser_version": PARSER_VERSION_SLOT,
        "files": entries,
        "created_at": datetime.utcnow().isoformat(),
        "by": user.person_id,
    }
    storage.save_lake_manifest(dashboard.id, json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
    keep = {e["key"] for e in entries}
    for k in storage.list_lake_part_keys(dashboard.id) - keep:
        storage.delete_lake_part(dashboard.id, k)
    return {"files": len(entries), "bytes": total, "errors": errors}
