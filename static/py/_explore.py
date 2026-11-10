"""OnePage — /explore query engine, browser side (Leg 3 of the lake plan).

Runs inside the SAME Pyodide worker as the generator engine (gen-worker.js,
mode 'explore'), but on a path that shares nothing with a report run: no
bundle, no generator module, no manifest. Its whole job is

    Excel partition on the user's H:\\  →  pandas frame  →  DuckDB view

so that `/explore` can ask ONE question across every partition of a catalogue
table without a single cell ever reaching the server.

Design constraints that shaped this file
----------------------------------------
* **The server never sees data.** Nothing here posts anything. The catalogue
  snapshot comes IN (table/column names, partition keys); rows never go out.
* **Memory like the run path.** `index_file()` parses one sheet, hands the
  frame to DuckDB and unlinks the /inbox copy immediately — the workbook bytes
  live for exactly one call. Only the registered frames stay resident, and
  `stats()["bytes"]` reports approximately what they cost, so the page can
  show it and stop before the 2.75 GB wasm wall.
* **SQL is hostile input.** `/explore` hands the user an editable SQL box and
  an LLM writes into it. `guard_sql()` is the whole trust boundary: comments
  stripped first (so nothing hides behind `--`), statements counted after
  (so `;` cannot smuggle a second one), and only a single SELECT/WITH
  survives. DuckDB itself is opened with `enable_external_access=false`, which
  is the belt to the guard's braces — neither is trusted alone.
* **Column names are the catalogue's, not Excel's.** `_norm_col()` is a
  verbatim port of `app.services.catalog.norm_col`; the catalogue, Data
  Quality and this file must never disagree about "the same column", because
  the query builder writes SQL from catalogue column names.

Public surface (everything gen-worker.js calls):
    set_catalog(catalog)                    – expected partitions per table
    index_file(path, sheet, header_row, table, partition, file=None)
    tables() / stats() / reset()
    guard_sql(sql) -> str                   – raises SQLGuardError
    run_sql(sql, limit=1000) -> dict
    export_xlsx(sql) -> bytes
"""
from __future__ import annotations

import io
import os
import re
import time
import unicodedata

# ── Limits ───────────────────────────────────────────────────────────────
DEFAULT_LIMIT = 1000
MAX_LIMIT = 10_000
EXPORT_MAX_ROWS = 100_000
QUERY_TIMEOUT_S = 30.0
MEMORY_LIMIT = "512MB"
FETCH_CHUNK = 2048
MAX_SQL_CHARS = 20_000

# Reserved result columns every partition frame carries. Named with the double
# underscore prefix the catalogue's own normalisation can never produce (it
# strips and casefolds a real header; a header of literal "__partition" is
# renamed out of the way in _frame_columns).
PARTITION_COL = "__partition"
# Materialise declared semantic views over bound tables (see
# _materialize_semantics). Off switch for memory-starved machines.
MATERIALIZE_VIEWS = True
FILE_COL = "__file"


class ExploreError(Exception):
    """Anything the page should show the user in Russian, not a traceback."""


class SQLGuardError(ExploreError):
    pass


class ExploreTimeout(ExploreError):
    pass


# ── Module state ─────────────────────────────────────────────────────────
_CON = None
_FRAMES: dict = {}      # relation name -> pandas frame (keeps it alive)
_TABLES: dict = {}      # table name -> [{rel, partition, file, rows, bytes}]
_EXPECTED: dict = {}    # table name -> [partition key, ...] from the catalogue
_DISPLAY: dict = {}     # table name -> display_name from the catalogue
_SEQ = 0
_INTERRUPTED = False


# ── Column naming (port of app/services/catalog.py: norm_col) ────────────
_UNNAMED_RE = re.compile(r"^unnamed:\s*\d+(\.\d+)?$")
_DEDUPE_SUFFIX_RE = re.compile(r"\.\d+$")
_WS_RE = re.compile(r"\s+")


def _norm_col(raw) -> str | None:
    """One Excel header -> the catalogue's canonical key, or None if artefact.

    Must stay byte-identical in behaviour to `app.services.catalog.norm_col`
    (which itself builds on `schemadiff.norm`): NFC, strip, casefold,
    whitespace collapsed, pandas' `Unnamed: 3` dropped and its `.1` duplicate
    tail undone. If these two ever drift, the query builder writes column
    names that do not exist in the view — the failure is silent SQL, not an
    exception, which is why the port is spelled out rather than approximated.
    """
    if not isinstance(raw, str):
        return None
    key = _WS_RE.sub(" ", unicodedata.normalize("NFC", raw).strip().casefold())
    if not key:
        return None
    if _UNNAMED_RE.match(key):
        return None
    key = _DEDUPE_SUFFIX_RE.sub("", key).strip()
    return key or None


# Table names come from the catalogue's own snake_case/translit naming, but
# they arrive through the page and are interpolated into DDL, so they are
# validated rather than trusted: letters (ASCII or Cyrillic), digits and
# underscore only. Everything a quoted identifier could be abused with — a
# quote, a backslash, a semicolon, a space — is rejected outright.
_TABLE_RE = re.compile(r"^[0-9A-Za-z_\u0400-\u04FF]{1,64}$")
_RESERVED_PREFIX = "_ep_"


def _check_table(name) -> str:
    name = "" if name is None else str(name).strip()
    if not _TABLE_RE.match(name):
        raise ExploreError(f"Invalid table name: {name!r}")
    if name.lower().startswith(_RESERVED_PREFIX):
        raise ExploreError(f"Table name reserved by the engine: {name!r}")
    return name


def _qi(name: str) -> str:
    """Quote an identifier. `name` is already validated; doubling any quote is
    a second line of defence, not the first."""
    return '"' + str(name).replace('"', '""') + '"'


# ── DuckDB connection ────────────────────────────────────────────────────
def _conn():
    """The single read-only-by-construction DuckDB connection.

    `enable_external_access=false` is set at DATABASE CREATION, not by a later
    `SET`: it is the switch that removes httpfs, ATTACH, COPY and every
    filesystem reader in one move, and a switch that can be flipped after the
    fact is a switch an injected statement can flip back. `lock_configuration`
    then nails the rest shut. Both are belt to `guard_sql()`'s braces.
    """
    global _CON
    if _CON is not None:
        return _CON
    try:
        import duckdb
    except ImportError as e:      # pragma: no cover - environment problem
        raise ExploreError(
            "The DuckDB query engine failed to load — Explore is unavailable."
        ) from e
    config = {
        "enable_external_access": False,
        "memory_limit": MEMORY_LIMIT,
        "threads": 1,
    }
    try:
        _CON = duckdb.connect(":memory:", config=config)
    except Exception:
        # An older/leaner build that refuses one of the keys: fall back to
        # SET, then verify external access really is off before handing the
        # connection out — a connection that silently kept it on is worse
        # than no /explore at all.
        _CON = duckdb.connect(":memory:")
        for k, v in config.items():
            try:
                _CON.execute(f"SET {k}=?", [v])
            except Exception:
                pass
        try:
            got = _CON.execute(
                "SELECT current_setting('enable_external_access')").fetchone()[0]
        except Exception:
            got = True
        if got not in (False, "false", "False", 0, "0"):
            _CON.close()
            _CON = None
            raise ExploreError(
                "DuckDB could not be started in safe mode "
                "(enable_external_access); queries are disabled.")
    for stmt in (
        "SET python_scan_all_frames=false",   # only explicitly registered frames
        "SET lock_configuration=true",        # no SET at all from here on
    ):
        try:
            _CON.execute(stmt)
        except Exception:
            pass
    return _CON


def reset() -> None:
    """Drop every frame, view and the connection itself (folder re-picked, or
    the page navigating away). Frees the biggest thing this worker holds."""
    global _CON, _FRAMES, _TABLES, _EXPECTED, _DISPLAY, _SEQ, _SEM_DEPS, _SEM_DIRTY, _SEM_STATUS, _IO_CON
    for c in (_CON, _IO_CON):
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
    _CON = None
    _IO_CON = None
    _FRAMES = {}
    _TABLES = {}
    _EXPECTED = {}
    _DISPLAY = {}
    _SEQ = 0
    _SEM_DEPS = {}
    _SEM_STATUS = {}
    _SEM_DIRTY = True


# ── Catalogue snapshot ───────────────────────────────────────────────────
def set_catalog(catalog) -> dict:
    """Record what the SERVER believes each table's partitions are.

    This is the only reason a result can honestly say "5 of 7 partitions":
    without it, a query over the partitions that happen to be on this laptop
    looks exactly like a query over all of them. Values (`values` in the
    snapshot) are deliberately ignored here — they are the query builder's
    dropdowns, and nothing on this side needs them.
    """
    global _EXPECTED, _DISPLAY
    tables = catalog
    if isinstance(catalog, dict):
        tables = catalog.get("tables") or []
        set_semantics(catalog.get("semantics") or [])
    expected, display = {}, {}
    for t in (tables or []):
        if not isinstance(t, dict):
            continue
        try:
            name = _check_table(t.get("name"))
        except ExploreError:
            continue
        keys = []
        for p in (t.get("partitions") or []):
            key = p.get("key") if isinstance(p, dict) else p
            key = "" if key is None else str(key)
            if key not in keys:
                keys.append(key)
        expected[name] = keys
        display[name] = str(t.get("display_name") or name)
    _EXPECTED = expected
    _DISPLAY = display
    return {"tables": len(expected),
            "partitions": sum(len(v) for v in expected.values())}


# ── Indexing ─────────────────────────────────────────────────────────────
def _read_frame(path: str, sheet, header_row):
    import pandas as pd
    if sheet is None or sheet == "":
        sheet = 0
    try:
        hdr = int(header_row)
    except (TypeError, ValueError):
        hdr = 0
    if hdr < 0:
        hdr = 0
    last = None
    # calamine first: it is the engine the run path and the lineage probe read
    # with, so /explore sees the same used range, the same header offset and
    # the same values a generator would. openpyxl is the fallback for a build
    # without python-calamine (and for .xlsm oddities).
    for engine in ("calamine", "openpyxl", None):
        try:
            kwargs = {"sheet_name": sheet, "header": hdr}
            if engine:
                kwargs["engine"] = engine
            return pd.read_excel(path, **kwargs)
        except Exception as e:
            last = e
    raise ExploreError(f"Could not read the sheet: {_short(last)}")


def _frame_columns(df, table: str) -> list:
    """Rename the sheet's headers to catalogue keys, in place, collision-safe.

    Three things can go wrong and all three are silent if unhandled: a header
    the catalogue drops as an artefact (blank / `Unnamed: 7`), two headers that
    normalise to the same key, and a header that literally spells one of our
    reserved columns. Each gets a distinct, stable, still-queryable name
    instead of being dropped — data the user can see in Excel must be
    reachable from SQL.
    """
    used = {PARTITION_COL, FILE_COL}
    out = []
    for i, raw in enumerate(list(df.columns)):
        key = _norm_col(raw if isinstance(raw, str) else str(raw))
        if not key:
            key = f"col_{i + 1}"
        if key in used:
            n = 2
            while f"{key}__{n}" in used:
                n += 1
            key = f"{key}__{n}"
        used.add(key)
        out.append(key)
    df.columns = out
    return out


def _coerce_time_cols(df) -> None:
    """Excel time-of-day cells surface as `datetime.time` objects; DuckDB
    maps such a column to TIME. Another partition where the same column read
    as full datetimes becomes TIMESTAMP — and the partition-union view then
    dies on DuckDB's unimplemented TIME -> TIMESTAMP cast. Times become
    "HH:MM:SS" strings instead: stable across partitions, lexically sortable,
    and castable back inside any query that needs arithmetic."""
    import datetime as _dt
    from pandas.api.types import infer_dtype
    for col in list(df.columns):
        s = df[col]
        if s.dtype != object:
            continue
        # infer_dtype is C-level: only columns that CAN hold time objects pay
        # for a per-cell pass (an all-string column never does).
        if infer_dtype(s, skipna=True) not in ("time", "mixed"):
            continue
        mask = s.map(lambda v: isinstance(v, _dt.time))
        if mask.any():
            df[col] = s.map(
                lambda v: v.isoformat() if isinstance(v, _dt.time) else v)


def _coerce_mixed_cols(df) -> None:
    """An object column holding numbers AND strings in ONE sheet (1C writes
    ' ' where a number is missing) makes DuckDB guess INT32 from the first
    rows and die on the first blank. Blanks become NULL; if what remains is
    all numeric the column becomes float, otherwise everything becomes text
    — either way DuckDB sees one type."""
    import pandas as pd
    from pandas.api.types import infer_dtype
    for col in list(df.columns):
        s = df[col]
        if s.dtype != object:
            continue
        kind = infer_dtype(s, skipna=True)
        if kind == "string":
            # Vectorised: blanks -> NULL, nothing else to do for pure text.
            blank = s.str.strip().eq("")
            if blank.any():
                df[col] = s.where(~blank, None)
            continue
        if kind in ("integer", "floating", "mixed-integer-float", "decimal"):
            df[col] = pd.to_numeric(s, errors="coerce")
            continue
        if kind not in ("mixed", "mixed-integer"):
            continue   # dates, times, booleans, empty: nothing to reconcile
        # Only genuinely mixed columns reach the per-cell pass.
        kinds = set()
        for v in s:
            if v is None or (isinstance(v, float) and v != v):
                continue
            if isinstance(v, str):
                kinds.add("blank" if not v.strip() else "str")
            elif isinstance(v, bool):
                kinds.add("str")
            elif isinstance(v, (int, float)):
                kinds.add("num")
            else:
                kinds.add("other")
            if {"str", "num"} <= kinds:
                break
        if "blank" in kinds:
            s = s.map(lambda v: None if isinstance(v, str) and not v.strip() else v)
        if "num" in kinds and "str" not in kinds and "other" not in kinds:
            df[col] = pd.to_numeric(s, errors="coerce")
        elif "num" in kinds or ("other" in kinds and ("str" in kinds or "blank" in kinds)):
            # Numbers or dates mixed with text ('-' where a date is missing):
            # one string column; the semantic layer parses it back by type.
            df[col] = s.map(_cell_to_text)
        elif "blank" in kinds:
            df[col] = s


def _cell_to_text(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat(sep=" ") if hasattr(v, "hour") and hasattr(v, "date") else v.isoformat()
        except TypeError:
            return v.isoformat()
    return str(v)


def index_file(path: str, sheet=None, header_row=0, table=None,
               partition=None, file=None, cache_key=None) -> dict:
    """Parse ONE partition file into the table's DuckDB view.

    The /inbox copy is unlinked before returning, unconditionally — including
    on the error paths — because the caller (gen-worker.js) has already
    dropped its ArrayBuffer and this file is the only remaining copy of a
    workbook that can be hundreds of megabytes.
    """
    global _SEQ
    table = _check_table(table)
    part = "" if partition is None else str(partition)
    fname = str(file or os.path.basename(path or ""))
    t0 = time.monotonic()
    try:
        df = _read_frame(path, sheet, header_row)
    finally:
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except Exception:
            pass
    t1 = time.monotonic()
    cols = _frame_columns(df, table)
    _coerce_time_cols(df)
    _coerce_mixed_cols(df)
    t2 = time.monotonic()
    out = _register_frame(df, table, part, fname, sheet, cols)
    t3 = time.monotonic()
    if cache_key:
        out["cached"] = _cache_put(cache_key, df)
    out["ms"].update({"parse": int((t1 - t0) * 1000), "coerce": int((t2 - t1) * 1000),
                      "cache_write": int((time.monotonic() - t3) * 1000)})
    return out


def _register_frame(df, table: str, part: str, fname: str, sheet, cols) -> dict:
    global _SEQ
    t0 = time.monotonic()
    df[PARTITION_COL] = part
    df[FILE_COL] = fname
    approx = _frame_bytes(df)

    con = _conn()
    _SEQ += 1
    rel = f"{_RESERVED_PREFIX}{_SEQ}"
    # A NATIVE DuckDB table, not a registered pandas frame: a registered
    # frame is re-scanned through Python on every query (object columns
    # converted cell by cell each time — the "first queries hang" symptom),
    # and every DESCRIBE over it re-samples the frame. Copy once, columnar,
    # and let the frame go.
    tmp = f"{rel}_src"
    con.register(tmp, df)
    try:
        con.execute(f"CREATE TABLE main.{_qi(rel)} AS SELECT * FROM {_qi(tmp)}")
    finally:
        try:
            con.unregister(tmp)
        except Exception:
            pass
    types = {r[0]: str(r[1]).upper() for r in con.execute(f"DESCRIBE main.{_qi(rel)}").fetchall()}
    entry = {"rel": rel, "partition": part, "file": fname,
             "rows": int(len(df)), "bytes": approx, "sheet": sheet, "types": types}
    parts = _TABLES.setdefault(table, [])
    # Re-indexing the same file (the user hit "Index" twice, or the
    # folder handed back a rewritten workbook) REPLACES its partition rather
    # than adding a second copy — otherwise every re-index silently doubles
    # every count in every answer.
    for old in list(parts):
        if old["file"] == fname and old["partition"] == part:
            _drop_rel(con, old["rel"])
            parts.remove(old)
    parts.append(entry)
    t1 = time.monotonic()
    _rebuild_view(con, table)
    return {"table": table, "partition": part, "file": fname,
            "rows": entry["rows"], "columns": cols, "bytes": approx,
            "partitions": len(parts),
            "ms": {"register": int((t1 - t0) * 1000), "view": int((time.monotonic() - t1) * 1000)}}


# ── Partition cache (Parquet in the browser's OPFS, mounted at /cache) ──
# A re-index used to re-read every workbook from the network share and
# re-parse it — 20 minutes for a folder nothing changed in. Parsed partitions
# are kept as Parquet under a key of (name, size, mtime, sheet, header row,
# parser version); on the next index the page asks which keys are present and
# never even opens those files. Written and read through a SEPARATE DuckDB
# connection that may touch files — the query connection stays locked.
CACHE_DIR = "/cache/explore"
CACHE_MAX_BYTES = 2 * 1024 ** 3
PARSER_VERSION = "4"   # bump when index-time coercions change: old parquet is wrong
_IO_CON = None


def cache_key(name, size, mtime_ms, sheet=None, header_row=0) -> str:
    import hashlib
    raw = f"{PARSER_VERSION}|{name}|{int(size or 0)}|{int(mtime_ms or 0)}|{sheet or ''}|{int(header_row or 0)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.parquet")


def _io_conn():
    global _IO_CON
    if _IO_CON is None:
        import duckdb
        _IO_CON = duckdb.connect(":memory:", config={"threads": 1, "memory_limit": MEMORY_LIMIT})
    return _IO_CON


def cache_probe(keys) -> dict:
    """{key: True|False} — which partitions can be served without the file.

    Carries `parser_version` alongside the keys: a manifest of partitions
    exported by another browser is only importable when the parser that wrote
    them coerces types the way this one does (see PARSER_VERSION)."""
    out = {"parser_version": PARSER_VERSION}
    for k in (keys or []):
        k = str(k)
        # The portable shape, not this engine's sha1 width: a key minted by
        # another engine's recipe still names a file this one can serve.
        out[k] = bool(re.fullmatch(r"[a-f0-9]{16,64}", k)) and os.path.exists(_cache_path(k))
    return out


def _cache_reserve(key: str) -> str:
    """Everything that lands in CACHE_DIR — a freshly parsed partition or one
    imported from another user's export — goes through the same directory
    creation and the same LRU eviction. Returns the final path."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    _cache_evict(CACHE_MAX_BYTES)
    return _cache_path(key)


def cache_export(keys) -> dict:
    """{key: bytes} for the Parquet partitions this cache actually holds.

    The bytes are the file as it sits on disk — the point is that ANOTHER
    user's browser can import them and skip reading the source workbooks
    entirely. Missing keys are omitted, not reported: the caller asked what is
    here, and the answer is a subset."""
    out = {}
    for k in (keys or []):
        k = str(k)
        if not re.fullmatch(r"[a-f0-9]{16,64}", k):
            continue
        path = _cache_path(k)
        try:
            with open(path, "rb") as fh:
                out[k] = fh.read()
        except Exception:
            continue
    return out


def _as_bytes(data):
    """The bytes may arrive as python bytes (native run), a memoryview or a
    JS typed array proxied in from the worker — take whichever shape came."""
    if data is None:
        return b""
    for conv in (bytes, lambda d: bytes(d.to_bytes()), lambda d: bytes(d.to_py()),
                 lambda d: bytes(bytearray(d))):
        try:
            return conv(data)
        except Exception:
            continue
    return None


def cache_import(key, data) -> dict:
    """Write one exported partition into this cache. Nothing is trusted.

    The bytes arrive from a server manifest another user produced, so they are
    checked BEFORE they can poison a cache the engine reads without opening any
    workbook: a Parquet magic number, then a real DuckDB `DESCRIBE` over the
    written temp file. Only then does `os.replace` publish it under the key —
    a torn or rejected import leaves nothing behind."""
    key = str(key)
    if not re.fullmatch(r"[a-f0-9]{16,64}", key):
        return {"ok": False, "error": "Bad partition key"}
    buf = _as_bytes(data)
    if buf is None:
        return {"ok": False, "error": "The partition cannot be read as bytes"}
    if buf[:4] != b"PAR1":
        return {"ok": False, "error": "This is not Parquet — partition rejected"}
    tmp = ""
    try:
        path = _cache_reserve(key)
        tmp = path + f".tmp{os.getpid()}"
        with open(tmp, "wb") as fh:
            fh.write(buf)
        _io_conn().execute(f"DESCRIBE SELECT * FROM read_parquet('{tmp}')").fetchall()
        os.replace(tmp, path)
        return {"ok": True, "bytes": len(buf)}
    except Exception as e:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return {"ok": False, "error": f"Partition rejected: {e}"}


def _cache_put(key: str, df) -> bool:
    """Best-effort: a cache that cannot be written only costs the next index
    its speed, never this one its result."""
    if not re.fullmatch(r"[0-9a-f]{40}", str(key)):
        return False
    try:
        _cache_reserve(key)
        con = _io_conn()
        con.register("_cache_out", df)
        try:
            path = _cache_path(key)
            con.execute(f"COPY _cache_out TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        finally:
            con.unregister("_cache_out")
        return True
    except Exception:
        return False


def index_cached(key: str, table=None, partition=None, file=None, sheet=None) -> dict:
    """Register a partition from its cached Parquet — no workbook read."""
    table = _check_table(table)
    part = "" if partition is None else str(partition)
    fname = str(file or "")
    if not re.fullmatch(r"[a-f0-9]{16,64}", str(key)) or not os.path.exists(_cache_path(key)):
        raise ExploreError(f"No partition in the cache for {fname}")
    t0 = time.monotonic()
    con = _io_conn()
    df = con.execute(f"SELECT * EXCLUDE ({_qi(PARTITION_COL)}, {_qi(FILE_COL)}) "
                     f"FROM read_parquet('{_cache_path(key)}')").df()
    try:
        os.utime(_cache_path(key), None)   # LRU: touched = recently used
    except Exception:
        pass
    t1 = time.monotonic()
    out = _register_frame(df, table, part, fname, sheet, list(df.columns))
    out["cached"] = True
    out["ms"]["cache_read"] = int((t1 - t0) * 1000)
    return out


def _cache_evict(limit_bytes: int) -> None:
    try:
        files = [(os.path.getmtime(p), os.path.getsize(p), p) for p in
                 (os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)) if p.endswith(".parquet")]
    except Exception:
        return
    total = sum(s for _, s, _ in files)
    for _, size, p in sorted(files):
        if total <= limit_bytes:
            break
        try:
            os.unlink(p)
            total -= size
        except Exception:
            pass


def cache_stats() -> dict:
    try:
        files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR) if f.endswith(".parquet")]
    except Exception:
        return {"files": 0, "bytes": 0}
    return {"files": len(files), "bytes": sum(os.path.getsize(p) for p in files)}


def cache_clear() -> dict:
    n = 0
    try:
        for f in os.listdir(CACHE_DIR):
            if f.endswith(".parquet"):
                os.unlink(os.path.join(CACHE_DIR, f))
                n += 1
    except Exception:
        pass
    return {"removed": n}


def _drop_rel(con, rel: str) -> None:
    try:
        con.execute(f"DROP TABLE IF EXISTS main.{_qi(rel)}")
    except Exception:
        pass
    try:
        con.unregister(rel)
    except Exception:
        pass
    _FRAMES.pop(rel, None)


def _rebuild_view(con, table: str) -> None:
    global _SEM_DIRTY
    _SEM_DIRTY = True   # every branch changes what the sem views stand on
    parts = _TABLES.get(table) or []
    if not parts:
        try:
            con.execute(f"DROP VIEW IF EXISTS main.{_qi(table)}")
        except Exception:
            pass
        return
    # UNION ALL BY NAME, not UNION ALL: partitions of one catalogue table are
    # allowed to differ in the non-core columns (that is what a table
    # *version* is), and positional UNION would either fail outright or, worse,
    # line up two different columns because they happen to share an index.
    # Partitions of one table disagree on a column's TYPE all the time in 1C
    # exports (a numeric column in one month, ' ' strings in the next): left
    # to DuckDB, the union casts the text to the number and dies on the first
    # blank. Reconcile explicitly — numbers widen to DOUBLE, anything mixed
    # with text becomes VARCHAR (the semantic layer re-parses text itself).
    per_rel = {}
    seen: dict = {}
    for p in parts:
        # Types were captured once at registration — no DESCRIBE per
        # partition per rebuild (that was quadratic in the folder size).
        cols = p.get("types") or {r[0]: str(r[1]).upper() for r in
                                  con.execute(f"DESCRIBE SELECT * FROM main.{_qi(p['rel'])}").fetchall()}
        per_rel[p["rel"]] = cols
        for c, t in cols.items():
            seen.setdefault(c, set()).add(t)
    target = {}
    for c, ts in seen.items():
        if len(ts) > 1:
            target[c] = "DOUBLE" if all(_is_numeric_type(t) for t in ts) else "VARCHAR"
    selects = []
    for p in parts:
        reps = [f"CAST({_qi(c)} AS {target[c]}) AS {_qi(c)}"
                for c in per_rel[p["rel"]] if c in target]
        selects.append(f"SELECT * REPLACE ({', '.join(reps)}) FROM main.{_qi(p['rel'])}" if reps
                       else f"SELECT * FROM main.{_qi(p['rel'])}")
    body = " UNION ALL BY NAME ".join(selects)
    # `main.` is load-bearing: once the semantic layer sets search_path to
    # 'sem,main', an unqualified CREATE would land the catalogue view in
    # `sem` and shadow a semantic table of the same name.
    con.execute(f"CREATE OR REPLACE VIEW main.{_qi(table)} AS {body}")


_NUMERIC_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT",
                  "USMALLINT", "UINTEGER", "UBIGINT", "FLOAT", "DOUBLE", "DECIMAL")


def _is_numeric_type(t: str) -> bool:
    return t.startswith(_NUMERIC_TYPES)


def _frame_bytes(df) -> int:
    """Approximate, on purpose: `deep=True` walks every object cell in
    Python (tens of seconds per big sheet in wasm) for a number only the
    header's "N MB" reads."""
    try:
        shallow = int(df.memory_usage(deep=False).sum())
        obj_cols = sum(1 for d in df.dtypes if d == object)
        return shallow + obj_cols * len(df) * 40
    except Exception:
        try:
            return int(len(df) * max(1, len(df.columns)) * 8)
        except Exception:
            return 0


def tables() -> list:
    """What is actually queryable right now, with the catalogue's expectation
    beside it — the page's "N tables · M partitions" header and the staleness
    badge both read from here."""
    out = []
    for name in sorted(_TABLES):
        parts = _TABLES[name]
        have = [p["partition"] for p in parts]
        expected = _EXPECTED.get(name, [])
        out.append({
            "name": name,
            "display_name": _DISPLAY.get(name, name),
            "partitions": [{"key": p["partition"], "file": p["file"],
                            "rows": p["rows"]} for p in parts],
            "rows": sum(p["rows"] for p in parts),
            "bytes": sum(p["bytes"] for p in parts),
            "expected": len(expected) if expected else len(parts),
            "missing": [k for k in expected if k not in have],
        })
    return out


def stats() -> dict:
    parts = sum(len(v) for v in _TABLES.values())
    return {
        "tables": len(_TABLES),
        "partitions": parts,
        "rows": sum(p["rows"] for v in _TABLES.values() for p in v),
        "bytes": sum(p["bytes"] for v in _TABLES.values() for p in v),
    }


# ── Semantic layer (ADR-13) ──────────────────────────────────────────────
# The generator's semantics.json, bound server-side to catalogue tables,
# becomes views in a `sem` schema: one canonical view per semantic table
# (renamed + typed columns over the indexed catalogue views, unioned by
# name), then the declared views in order. KPI SQL from the file runs over
# them; `intent_sql` turns a validated intent into deterministic SQL the
# page shows before anything executes. Nothing here is a second SQL engine —
# the same guard, connection and limits apply.
_SEMANTICS: list = []   # [{generator, semantics, bindings}] from the snapshot
# (generator_id, table, slug, version) -> the pandas frame built ONCE from a
# platform dictionary's rows. The snapshot hands the rows over as a list of
# lists; keeping BOTH that list and the frame alive doubled a 20 000 x 32
# dictionary in a tab that never releases it, and `_mat_key` then had to
# json-dump the list on every materialisation. The frame is the better copy,
# so the rows are moved into it and dropped from the semantics — see
# `_reference_frame`.
_SEM_REF_FRAMES: dict = {}
_SEM_STATUS: dict = {}  # last materialisation report, for the page
_SEM_DEPS: dict = {}    # semantic view name -> {catalogue table names}
_SEM_READY: dict = {}   # (generator_id, kpi key) -> bool
_SEM_DIRTY: bool = True
_SEM_TYPES = {"text": "VARCHAR", "number": "DOUBLE", "datetime": "TIMESTAMP",
              "date": "DATE", "bool": "BOOLEAN"}
_INTENT_OPS = {"=": "=", "!=": "<>", "<>": "<>", ">": ">", ">=": ">=", "<": "<",
               "<=": "<=", "in": "IN", "like": "ILIKE"}
_VALUE_AGGS = {"sum": "SUM", "avg": "AVG", "mean": "AVG", "average": "AVG",
               "max": "MAX", "min": "MIN", "count": "SUM"}


def set_semantics(entries) -> None:
    global _SEMANTICS, _SEM_DIRTY, _SEM_STATUS, _SEM_REF_FRAMES
    # A new snapshot carries new dictionary rows: nothing built from the old
    # ones may be reused, and their frames must not outlive them.
    _SEM_REF_FRAMES = {}
    out = []
    for e in (entries or []):
        if isinstance(e, dict) and isinstance(e.get("semantics"), dict):
            out.append(e)
    _SEMANTICS = out
    _SEM_STATUS = {}
    _SEM_DIRTY = True


def _sem_cast(key: str, typ: str) -> str:
    """Declared type -> expression over the catalogue column. Text goes
    through as-is; numbers and dates are parsed the way the generators do
    (1C exports mix real dates, '08.07.2026 15:36:35' text and Excel
    serials in ONE column across files), so a declared `datetime` is a
    TIMESTAMP whatever the cell looked like."""
    col = _qi(key)
    s = f"CAST({col} AS VARCHAR)"
    if typ == "number":
        return (f"TRY_CAST(replace(replace(replace(replace(trim({s}), chr(160), ''), ' ', ''), "
                f"',', '.'), '−', '-') AS DOUBLE)")
    if typ in ("datetime", "date"):
        ts = (f"COALESCE(TRY_CAST({s} AS TIMESTAMP), try_strptime(trim({s}), '%d.%m.%Y %H:%M:%S'), "
              f"try_strptime(trim({s}), '%d.%m.%Y %H:%M'), try_strptime(trim({s}), '%d.%m.%Y'), "
              f"CASE WHEN TRY_CAST({s} AS DOUBLE) BETWEEN 20000 AND 80000 THEN TIMESTAMP '1899-12-30' "
              f"+ to_seconds(CAST(TRY_CAST({s} AS DOUBLE) * 86400 AS BIGINT)) END)")
        return f"CAST({ts} AS DATE)" if typ == "date" else ts
    if typ == "bool":
        return f"TRY_CAST({s} AS BOOLEAN)"
    return s


def _reference_frame(entry, tname: str, t: dict, cols: dict):
    """The pandas frame for ONE reference-backed table, built at most once per
    (generator, table, slug, version) and reused by every later
    materialisation.

    The rows are DROPPED from the semantics as soon as the frame holds them:
    a re-index re-materialises the whole layer, and carrying the wire shape
    (a list of lists) alongside the frame meant two copies of a dictionary
    that can be 7.6 MB — plus `_mat_key` serialising that list on every view.
    A new snapshot resets the cache (`set_semantics`), so the only way to
    reach a stale frame would be a version change without new rows, which the
    server cannot produce."""
    import pandas as pd
    ref = t.get("reference") or {}
    key = (entry.get("generator_id"), str(tname), str(ref.get("slug")), ref.get("version"))
    cached = _SEM_REF_FRAMES.get(key)
    if cached is not None:
        return cached
    ref_cols = list(ref.get("columns") or cols.keys())
    rows = [dict(zip(ref_cols, r)) for r in (ref.get("rows") or [])]
    df = pd.DataFrame(rows, columns=list(cols)).astype(object)
    _SEM_REF_FRAMES[key] = df
    ref.pop("rows", None)
    return df


def _materialize_semantics() -> dict:
    """(Re)build the `sem` schema from the current index. Runs lazily whenever
    the index changed since the last call. Declared VIEWS over bound tables
    are MATERIALISED (CREATE TABLE AS): a view re-reads and re-casts every
    partition on every question, and on an office laptop under wasm that was
    45 s+ per answer for 34 weekly files; the table costs about one such
    scan once, here, and every answer after it is milliseconds. Falls back
    to the view if the copy cannot be made (memory)."""
    global _SEM_STATUS, _SEM_DEPS, _SEM_DIRTY, _SEM_READY, _SEQ
    if not _SEM_DIRTY:
        return _SEM_STATUS
    con = _conn()
    status = {"generators": []}
    _SEM_DEPS = {}
    _SEM_READY = {}
    try:
        # The frames the PREVIOUS build registered for inline/reference tables
        # go first. Dropping only the views left the pandas objects alive in
        # `_FRAMES` and registered in DuckDB, so every re-index (or every new
        # version of a dictionary) added another full copy of the rows to a
        # browser tab that never releases them.
        for rel in [r for r in _FRAMES if r.startswith(_RESERVED_PREFIX + "sem")]:
            _drop_rel(con, rel)
        con.execute("CREATE SCHEMA IF NOT EXISTS sem")
        for (vname,) in con.execute(
                "SELECT view_name FROM duckdb_views() WHERE schema_name = 'sem'").fetchall():
            con.execute(f"DROP VIEW IF EXISTS sem.{_qi(vname)}")
        for (tname,) in con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'sem'").fetchall():
            con.execute(f"DROP TABLE IF EXISTS sem.{_qi(tname)}")
        con.execute("SET search_path = 'sem,main'")
    except Exception as e:
        _SEM_STATUS = {"generators": [], "error": f"Semantic layer unavailable: {_short(e)}"}
        _SEM_DIRTY = False
        return _SEM_STATUS
    created: set = set()
    for entry in _SEMANTICS:
        sem = entry["semantics"]
        bindings = entry.get("bindings") or {}
        g = {"generator": entry.get("generator"), "generator_id": entry.get("generator_id"),
             "tables": {}, "views": {}, "errors": []}
        for tname, t in (sem.get("tables") or {}).items():
            if tname in created or tname in _TABLES:
                g["tables"][tname] = "conflict"
                g["errors"].append(f"{tname}: name in use by another table")
                continue
            cols = t.get("columns") or {}
            rows = t.get("rows")
            # A reference-backed table (ADR-16): the server attached the
            # current version of a platform dictionary. Rows arrive as LISTS
            # in `reference.columns` order — the compact wire shape — and are
            # materialised through exactly the inline path below, so what the
            # registration dry run proved over typed empty tables is what
            # runs here. No rows (table not created, no access, over budget)
            # is not an error for the report: this ONE table goes `unbound`
            # with the server's own wording and every other answer stands.
            ref = t.get("reference") if isinstance(t.get("reference"), dict) else None
            ref_df = None
            if ref is not None:
                if ref.get("status") != "ok":
                    g["tables"][tname] = "unbound"
                    g["errors"].append(f"{tname}: {ref.get('message') or 'reference table unavailable'}")
                    continue
                ref_df = _reference_frame(entry, tname, t, cols)
                rows = []          # a frame stands in for them from here on
            try:
                if rows is not None:
                    import pandas as pd
                    _SEQ += 1
                    rel = f"{_RESERVED_PREFIX}sem{_SEQ}"
                    df = ref_df if ref_df is not None else \
                        pd.DataFrame(list(rows), columns=list(cols)).astype(object)
                    _FRAMES[rel] = df
                    con.register(rel, df)
                    # Same shape as a bound table — declared types, the two
                    # engine columns — so what passed the server's dry run
                    # over typed tables behaves identically here.
                    parts = [f"{_sem_cast(canon, (spec or {}).get('type') or 'text')} AS {_qi(canon)}"
                             for canon, spec in cols.items()]
                    parts += [f"NULL::VARCHAR AS {_qi(PARTITION_COL)}", f"NULL::VARCHAR AS {_qi(FILE_COL)}"]
                    con.execute(f"CREATE VIEW sem.{_qi(tname)} AS SELECT {', '.join(parts)} FROM {_qi(rel)}")
                    g["tables"][tname] = "reference" if ref is not None else "inline"
                    if ref is not None:
                        # Which version answered — the page shows it verbatim
                        # ("reference sl_client_map v7"), so an analyst can
                        # tell "I fixed the reference table" from "the chat
                        # has not seen the fix yet" without opening anything.
                        g.setdefault("references", []).append(
                            {"table": tname, "slug": ref.get("slug"),
                             "version": ref.get("version"), "title": ref.get("title"),
                             "rows": int(len(df))})
                    deps: set = set()
                else:
                    selects, deps = [], set()
                    for m in (bindings.get(tname) or {}).get("matches") or []:
                        ct = m.get("table")
                        if ct not in _TABLES:
                            continue
                        # The server bound columns against the CATALOGUE's
                        # column list; the partitions on this machine are
                        # the truth. A key the catalogue never learned (a
                        # duplicate header's `__2`, a header that drifted)
                        # is re-resolved here from the declared source
                        # header, and what still cannot be found is reported
                        # instead of silently becoming NULL everywhere.
                        try:
                            actual = {r[0] for r in con.execute(
                                f"DESCRIBE SELECT * FROM main.{_qi(ct)}").fetchall()}
                        except Exception:
                            actual = set()
                        parts = []
                        for canon, spec in cols.items():
                            key = (m.get("columns") or {}).get(canon)
                            typ = (spec or {}).get("type") or "text"
                            src = (spec or {}).get("source")
                            if actual and (not key or key not in actual) and src:
                                occ = int((spec or {}).get("occurrence") or 0)
                                alt = _source_key_local(src, occ)
                                if alt in actual:
                                    key = alt
                                else:
                                    # The k-th column whose name derives from this
                                    # header, whatever suffix the reader gave the
                                    # duplicate (`__2` is ours; `_1` is pandas').
                                    base = _norm_col(str(src)) or ""
                                    cands = sorted(c for c in actual
                                                   if base and (c == base or c.startswith(base + "_")))
                                    key = cands[occ] if base and occ < len(cands) else None
                            if key is None and src:
                                g.setdefault("unbound_columns", {}).setdefault(tname, []).append(canon)
                            if key:
                                parts.append(f"{_sem_cast(key, typ)} AS {_qi(canon)}")
                            else:
                                parts.append(f"NULL::{_SEM_TYPES.get(typ, 'VARCHAR')} AS {_qi(canon)}")
                        parts += [f"{_qi(PARTITION_COL)}", f"{_qi(FILE_COL)}"]
                        sel = f"SELECT {', '.join(parts)} FROM main.{_qi(ct)}"
                        # A catalogue table is a cluster of files; only the
                        # ones the source pattern named may feed this table.
                        files = [str(f) for f in (m.get("files") or []) if f]
                        if files:
                            sel += f" WHERE {_qi(FILE_COL)} IN ({', '.join(_lit(f) for f in files)})"
                        selects.append(sel)
                        deps.add(ct)
                    if not selects:
                        g["tables"][tname] = "unbound"
                        continue
                    con.execute(f"CREATE VIEW sem.{_qi(tname)} AS "
                                + " UNION ALL BY NAME ".join(selects))
                    g["tables"][tname] = "bound"
            except Exception as e:
                g["tables"][tname] = "error"
                g["errors"].append(f"{tname}: {_short(e)}")
                continue
            created.add(tname)
            _SEM_DEPS[tname] = deps
        for vname, v in (sem.get("views") or {}).items():
            sql = v.get("sql") if isinstance(v, dict) else v
            if vname in created or vname in _TABLES:
                g["views"][vname] = "conflict"
                continue
            try:
                # The frozen text is the resolved text — no templating here.
                con.execute(f"CREATE VIEW sem.{_qi(vname)} AS {str(sql)}")
                con.execute(f"SELECT * FROM sem.{_qi(vname)} LIMIT 0")
            except Exception as e:
                g["views"][vname] = "error"
                g["errors"].append(f"{vname}: {_short(e)}")
                continue
            low = str(sql).lower()
            deps = set()
            for name in created:
                if re.search(r"(?<![a-z0-9_])" + re.escape(name) + r"(?![a-z0-9_])", low):
                    deps |= _SEM_DEPS.get(name, set())
            _SEM_DEPS[vname] = deps
            g["views"][vname] = "ok"
            created.add(vname)
            if deps and MATERIALIZE_VIEWS:
                try:
                    tmp_name = f"{vname}__mat"
                    con.execute(f"DROP TABLE IF EXISTS sem.{_qi(tmp_name)}")
                    # The materialised rows are a pure function of (engine
                    # version, this view's frozen SQL and its tables, the
                    # exact partitions underneath) — so they are cached in
                    # Parquet like a partition is. On an office laptop the
                    # build took 88 s; the reload takes about a second.
                    mkey = _mat_key(entry, sem, vname, deps)
                    mdf = _mat_load(mkey)
                    if mdf is not None:
                        _SEQ += 1
                        rel = f"{_RESERVED_PREFIX}mat{_SEQ}"
                        _FRAMES[rel] = mdf
                        con.register(rel, mdf)
                        con.execute(f"CREATE TABLE sem.{_qi(tmp_name)} AS SELECT * FROM {_qi(rel)}")
                        g.setdefault("materialized_from_cache", []).append(vname)
                    else:
                        con.execute(f"CREATE TABLE sem.{_qi(tmp_name)} AS SELECT * FROM sem.{_qi(vname)}")
                    con.execute(f"DROP VIEW sem.{_qi(vname)}")
                    con.execute(f"ALTER TABLE sem.{_qi(tmp_name)} RENAME TO {_qi(vname)}")
                    g["views"][vname] = "materialized"
                    if mdf is None and mkey:
                        if not _mat_store(mkey, con, vname):
                            g.setdefault("warnings", []).append(f"{vname}: materialisation not saved to the cache")
                except Exception:
                    # Keep the view: slower, never wrong. Make sure no half-made
                    # copy is left behind under the temporary name.
                    try:
                        con.execute(f"DROP TABLE IF EXISTS sem.{_qi(vname + '__mat')}")
                    except Exception:
                        pass
        # Ready = every DECLARED name the KPI's SQL touches came up; resolving
        # against what survived would make a KPI over an unbound table "ready".
        declared = set(sem.get("tables") or {}) | set(sem.get("views") or {})
        g["kpis"] = []
        for k, kp in (sem.get("kpis") or {}).items():
            names = _sem_names_in(kp.get("cell_sql") or "", declared)
            ready = bool(names) and all(
                g["tables"].get(n) in ("bound", "inline", "reference")
                or g["views"].get(n) in ("ok", "materialized")
                for n in names)
            _SEM_READY[(entry.get("generator_id"), k)] = ready
            g["kpis"].append(
                {"key": k, "name": kp.get("name"), "short": kp.get("short"),
                 "dims": kp.get("dims") or [], "measure": kp.get("measure"),
                 "target": kp.get("target"), "definition": kp.get("definition"),
                 "unit": kp.get("unit"),
                 "owner": kp.get("owner"), "approved_at": kp.get("approved_at"),
                 # The chat's «is this a NEW question or a follow-up?» guard
                 # and the page's generator pick read the KPI's own phrases;
                 # without them (prod 2026-08-28) "service level" was not a
                 # metric word and a fresh question re-ran the open answer.
                 "question_forms": list(kp.get("question_forms") or []),
                 "dim_synonyms": dict(kp.get("dim_synonyms") or {}),
                 "ready": ready})
        status["generators"].append(g)
    _SEM_STATUS = status
    _SEM_DIRTY = False
    return status


def _strip_reference_rows(sem) -> None:
    """Drop the wire copy of every platform dictionary's rows from `sem`.

    By the time anything computes a cache key the rows are already inside a
    pandas frame (`_reference_frame`), so this is normally a no-op — but it
    makes the invariant true by construction instead of by call order: NO
    cache key ever hashes dictionary rows. `reference.rows` is data, not
    shape; the shape that matters is (slug, version), which `_mat_key` stamps
    separately. A full 20 000 x 32 dictionary is 7.6 MB of JSON, and hashing
    it cost ~20 ms per view, every rebuild, for a value that cannot change
    without the version changing too."""
    if not isinstance(sem, dict):
        return
    for t in (sem.get("tables") or {}).values():
        if isinstance(t, dict) and isinstance(t.get("reference"), dict):
            t["reference"].pop("rows", None)


def _mat_key(entry, sem, vname, deps) -> str | None:
    """Cache key of a materialised view: engine parser version, the
    generator's identity/version, the view's frozen SQL and the declared
    tables it reads, and the identity of every partition underneath
    (file, partition, rows, bytes — what `_TABLES` knows)."""
    try:
        import hashlib
        import json as _json
        _strip_reference_rows(sem)
        parts = []
        for ct in sorted(deps):
            for p in _TABLES.get(ct) or []:
                parts.append((ct, p.get("file"), p.get("partition"), p.get("rows"), p.get("bytes")))
        raw = _json.dumps({
            "pv": PARSER_VERSION, "gen": entry.get("generator_id"), "ver": entry.get("version"),
            "view": vname, "sql": (sem.get("views") or {}).get(vname),
            "tables": {k: (sem.get("tables") or {}).get(k) for k in sorted(deps)},
            # A reference-backed table contributes no catalogue partition, so
            # `parts` cannot notice that "Reference tables" moved. (slug, version)
            # IS the identity of its rows — stamp that, and never the rows:
            # a full 20 000 x 32 dictionary is 7.6 MB of JSON, which turned a
            # cache-key computation into 20 ms per view for nothing.
            "refs": sorted(
                (str(n), str((tt.get("reference") or {}).get("slug")),
                 (tt.get("reference") or {}).get("version"))
                for n, tt in ((sem.get("tables") or {}).items())
                if isinstance(tt, dict) and isinstance(tt.get("reference"), dict)),
            "parts": parts}, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
    except Exception:
        return None


def _mat_load(key):
    if not key:
        return None
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    try:
        return _io_conn().execute(f"SELECT * FROM read_parquet('{path}')").df()
    except Exception:
        return None


def _mat_store(key, con, vname) -> bool:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        df = con.execute(f"SELECT * FROM sem.{_qi(vname)}").df()
        _cache_reserve(key)
        io = _io_conn()
        io.register("_mat_out", df)
        try:
            io.execute(f"COPY _mat_out TO '{_cache_path(key)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        finally:
            io.unregister("_mat_out")
        return True
    except Exception:
        return False


def _source_key_local(source: str, occurrence: int = 0) -> str:
    """The engine's own name for a sheet header: `_norm_col` + the `__{k+1}`
    suffix `_frame_columns` gives the k-th duplicate — the same rule
    `semantics.source_key` applies on the server, evaluated here against
    the partitions that actually exist."""
    key = _norm_col(str(source)) or ""
    return key if occurrence == 0 else f"{key}__{occurrence + 1}"


def _sem_names_in(sql: str, names) -> list:
    low = sql.lower()
    return [n for n in names
            if re.search(r"(?<![a-z0-9_])" + re.escape(n) + r"(?![a-z0-9_])", low)]


def semantics_status() -> dict:
    try:
        return _materialize_semantics()
    except ExploreError as e:
        return {"generators": [], "error": str(e)}


def _lit(v) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return repr(v)
    if isinstance(v, float):
        import math
        return repr(v) if math.isfinite(v) else "NULL"
    return "'" + str(v).replace("'", "''") + "'"


_TIME_DIM_NAMES = {"year", "month", "week", "day"}

_RU2LAT = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch",
    "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"})


def _norm_val(v) -> str:
    return str(v).strip().casefold().translate(_RU2LAT)


def cell_for_probe(kp) -> str:
    return str(kp.get("cell_sql") or "").strip().rstrip(";")


def _distinct_vals(cell: str, dim: str) -> list:
    try:
        rows = _conn().execute(
            "WITH c AS (\n" + cell + "\n) SELECT DISTINCT " + _qi(dim) + " FROM c LIMIT 500").fetchall()
        return [r[0] for r in rows if r[0] is not None]
    except Exception:
        return []


def _probe_value(cell: str, dim: str, word: str):
    """Does `word` exist as a VALUE of `dim`? Exact case-insensitive hit over
    the cell itself — works at any cardinality (a 500-value DISTINCT scan
    would miss a SKU name)."""
    try:
        row = _conn().execute(
            f"WITH c AS ({cell}) SELECT {_qi(dim)} FROM c "
            f"WHERE lower(CAST({_qi(dim)} AS VARCHAR)) = lower({_lit(word)}) LIMIT 1"
        ).fetchone()
    except Exception:
        return None
    return None if row is None else row[0]


def _fix_filter_value(cell: str, dim: str, val: str, allowed: list) -> dict:
    """Does `val` exist in `dim`? Exact → keep. A unique case/translit match
    ("grando" → Grando) → the corrected value. Absent in `dim` but uniquely
    present in ONE other non-time dim → move the filter there. Otherwise:
    missing, with the dim's known values for an honest message."""
    vals = _distinct_vals(cell, dim)
    strs = [str(v) for v in vals]
    if val in strs:
        return {"value": val}
    want = _norm_val(val)
    near = [v for v in strs if _norm_val(v) == want]
    if len(near) == 1:
        return {"value": near[0]}
    candidates = []
    for other in allowed:
        if other == dim or other in _TIME_DIM_NAMES:
            continue
        ovals = [str(v) for v in _distinct_vals(cell, other)]
        hit = [v for v in ovals if v == val] or [v for v in ovals if _norm_val(v) == want]
        if hit:
            candidates.append((other, hit[0]))
    if len(candidates) == 1:
        return {"dim": candidates[0][0], "value": candidates[0][1]}
    return {"missing": True, "known": strs}


def intent_sql(intent) -> dict:
    """A validated intent -> the SQL the page will SHOW, then run only on the
    user's click. Every name is checked against the KPI's own dims, every
    value is a literal: the model never writes SQL, it fills a form."""
    try:
        _materialize_semantics()
    except ExploreError as e:
        return {"sql": None, "error": str(e)}
    if not isinstance(intent, dict):
        return {"sql": None, "error": "Intent must be an object."}
    key = str(intent.get("kpi") or "")
    gid = intent.get("generator_id")
    if gid is None:
        return {"sql": None, "error": "Intent missing generator_id."}
    found = None
    for entry in _SEMANTICS:
        kp = (entry["semantics"].get("kpis") or {}).get(key)
        if kp and entry.get("generator_id") == gid:
            found = (entry, kp)
            break
    if not found:
        return {"sql": None, "error": f"Unknown metric: {key!r}."}
    entry, kp = found
    if not _SEM_READY.get((gid, key)):
        return {"sql": None, "error": f"Metric «{kp.get('short') or key}» not ready: its tables not indexed in this folder."}
    allowed = list(kp.get("dims") or [])
    dims = [str(d) for d in (intent.get("dims") or [])]
    bad = [d for d in dims if d not in allowed]
    if bad:
        return {"sql": None, "error": f"Metric «{kp.get('short') or key}» cannot slice by {bad}; available: {allowed}."}
    where = []
    notes = []
    # A dim the semantics DECLARES but this folder's files do not carry
    # (an `optional` column, absent header) is NULL in every row: grouping
    # by it answers nothing, and silence here reads as a broken product
    # ("which SKUs were under-delivered" over files with no SKU column — prod
    # 2026-08-23). Say it, and answer the rest of the question.
    unbound: set = set()
    for gg in (_SEM_STATUS.get("generators") or []):
        if gg.get("generator_id") == gid:
            for cols_ in (gg.get("unbound_columns") or {}).values():
                unbound.update(str(c) for c in cols_)
    gone = [d for d in dims if d in unbound]
    if gone:
        dims = [d for d in dims if d not in unbound]
        names = ", ".join(f"«{d}»" for d in gone)
        notes.append(f"the connected files have no column {names} — "
                     f"cannot slice by it, showing the total without it")
    for f in (intent.get("filters") or []):
        if not isinstance(f, dict):
            continue
        dim, op = str(f.get("dim") or ""), str(f.get("op") or "=").lower()
        if dim not in allowed:
            # A period or cut this KPI does not carry ("in week 23" for a
            # monthly share): answer without it and SAY so — an error here
            # turned a clear question into a dead end (stand 2026-09-06).
            notes.append(f"the metric has no cut by “{dim}”, so that filter was not applied "
                         f"and the result is computed without it; cuts of the metric: {', '.join(allowed) or 'none'}")
            continue
        if dim in unbound:
            notes.append(f"filter on «{dim}» skipped — the connected files have no such column")
            continue
        if op not in _INTENT_OPS:
            return {"sql": None, "error": f"Invalid filter operator: {op!r}."}
        # The model fills the form without ever seeing a value, so a filter
        # value can be misspelled ("grando") or hung on the WRONG dim
        # (`shipping_display = 'Grando'` when Grando is a shipping_group —
        # prod 2026-08-23, an empty answer with no explanation). The DATA is
        # right here: check, fix what is unambiguous, say what was done.
        if op == "=" and f.get("value") is not None and dim not in _TIME_DIM_NAMES:
            fixed = _fix_filter_value(cell_for_probe(kp), dim, str(f.get("value")), allowed)
            if fixed.get("dim") and fixed["dim"] != dim:
                notes.append(f"«{f.get('value')}» is a {fixed['dim']}, not a {dim}; filter moved")
                dim = fixed["dim"]
            if fixed.get("value") is not None and str(fixed["value"]) != str(f.get("value")):
                notes.append(f"«{f.get('value')}» read as «{fixed['value']}»")
            if fixed.get("value") is not None:
                f = dict(f, value=fixed["value"])
            elif fixed.get("missing"):
                near = fixed.get("known") or []
                notes.append(f"value «{f.get('value')}» is not in {dim}"
                             + (f" (available: {', '.join(near[:5])}…)" if near else ""))
        if op == "in":
            vals = f.get("values") or f.get("value") or []
            if not isinstance(vals, list) or not vals:
                return {"sql": None, "error": f"IN filter on {dim!r} with no values."}
            where.append(f"{_qi(dim)} IN ({', '.join(_lit(v) for v in vals[:50])})")
        else:
            val = f.get("value")
            if val is None:
                return {"sql": None, "error": f"Filter on {dim!r} with no value."}
            if op == "like":
                val = str(val)
                if "%" not in val:
                    val = f"%{val}%"
            where.append(f"{_qi(dim)} {_INTENT_OPS[op]} {_lit(val)}")
    # Residue probe (2026-08-24, "in MBU" lost silently): words of the question
    # nothing consumed arrive as `probe_words`, and the DATA decides what they
    # are. One dim carries the value -> that IS the filter the user meant
    # (plus a note); two dims carry it -> a `clarify` entry the page turns into
    # chips INSTEAD of a number; found nowhere -> silence (latin tile labels
    # land here, and a false clarification is worse than none).
    clarify = []
    kpay = {"key": key, "name": kp.get("name"), "definition": kp.get("definition"),
            "target": kp.get("target"), "unit": kp.get("unit"), "measure": kp.get("measure")}
    probes = [str(w).strip() for w in (intent.get("probe_words") or []) if str(w).strip()][:4]
    if probes:
        probe_dims = [d for d in allowed if d not in _TIME_DIM_NAMES and d not in unbound]
        pcell = cell_for_probe(kp)
        for w in probes:
            hits = []
            for d in probe_dims:
                v = _probe_value(pcell, d, w)
                if v is not None:
                    hits.append((d, v))
            if len(hits) == 1:
                d, v = hits[0]
                where.append(f"{_qi(d)} = {_lit(v)}")
                notes.append(f"«{w}» understood as {d} = «{v}»")
            elif len(hits) > 1:
                clarify.append({"word": w,
                                "options": [{"dim": d, "value": str(v)} for d, v in hits[:4]]})
            else:
                # Found nowhere. Silence here read as an answer ("were there
                # under-deliveries for F12" -> the unfiltered top, 2026-08-27):
                # say the word was looked for and not found, then answer
                # the rest of the question.
                notes.append(f"«{w}» not found in any dimension of the metric — "
                             f"showing the result without that filter")
    if clarify:
        return {"sql": None, "error": None, "notes": notes, "clarify": clarify, "kpi": kpay}

    cell = str(kp.get("cell_sql") or "").strip().rstrip(";")
    ratio = kp.get("measure") == "ratio"
    if ratio:
        measures = ('SUM("n") AS "n", SUM("d") AS "d", '
                    'ROUND(SUM("n") / NULLIF(SUM("d"), 0), 4) AS "value"')
    else:
        # The KPI's declared aggregation decides how cells roll up: an
        # average rolled up with SUM is a wrong number behind a plausible SQL.
        agg_word = str(kp.get("aggregation") or "sum").strip().lower().split()[0]
        agg = _VALUE_AGGS.get(agg_word, "SUM")
        measures = f'{agg}("value") AS "value"'
    select = ", ".join(_qi(d) for d in dims)
    sql = f"WITH c AS (\n{cell}\n)\nSELECT {select + ', ' if select else ''}{measures}\nFROM c"
    if where:
        sql += "\nWHERE " + " AND ".join(where)
    if dims:
        sql += "\nGROUP BY " + ", ".join(_qi(d) for d in dims)
    order = intent.get("order")
    if isinstance(order, dict) and order.get("by"):
        by = str(order["by"])
        if by not in dims + (["value", "n", "d"] if ratio else ["value"]):
            return {"sql": None, "error": f"Cannot sort by {by!r}."}
        sql += f"\nORDER BY {_qi(by)} {'DESC' if str(order.get('dir', '')).lower() == 'desc' else 'ASC'}"
    elif dims:
        sql += "\nORDER BY " + ", ".join(_qi(d) for d in dims)
    try:
        limit = int(intent.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    sql += f"\nLIMIT {max(1, min(limit, MAX_LIMIT))}"
    return {"sql": sql, "error": None, "notes": notes, "kpi": kpay}


# ── SQL guard ────────────────────────────────────────────────────────────
_DOLLAR_RE = re.compile(r"\$[A-Za-z_0-9]*\$")

# Everything that is not "read the frames we registered". DuckDB with
# enable_external_access=false already refuses most of it; this list refuses it
# BEFORE the parser sees it, so a future DuckDB that renames a setting, or a
# build where the config did not take, still cannot be talked into writing.
#
# Split in two, because the two halves are trusted differently:
#
# * _FORBIDDEN_KEYWORDS are parser keywords, not names DuckDB's catalog can
#   resolve — there is no quoted spelling that turns `"delete"` into a DELETE
#   statement (a single already-enforced SELECT/WITH statement has no syntax
#   position where DROP/INSERT/DELETE/... could appear as DML). A column
#   genuinely named "delete" or "set" must stay queryable, so this half is
#   scanned with identifiers MASKED (only a bare, unquoted keyword trips it).
# * _FORBIDDEN_CALLABLE are real DuckDB function/table-function names.
#   DuckDB resolves a QUOTED function name exactly like a bare one
#   (`SELECT "abs"(-1)` runs `abs`), so masking identifiers before this half
#   runs would let every one of them through spelled as `"read_csv"(...)`.
#   This half is therefore scanned with identifiers READABLE.
_FORBIDDEN_KEYWORDS = (
    # transactions / DDL / DML
    "attach", "detach", "insert", "update", "delete", "drop", "create",
    "alter", "truncate", "merge", "upsert",
    # NOT "replace": `replace()` is an everyday string function, and the only
    # dangerous spellings of the word — CREATE OR REPLACE, INSERT OR REPLACE —
    # are already refused by "create"/"insert".
    # engine control (statement keywords, not callable names)
    "pragma", "set", "reset", "install", "load", "copy", "export",
    "import", "checkpoint", "vacuum", "analyze", "begin", "commit",
    "rollback", "transaction", "prepare", "execute", "deallocate",
    "grant", "revoke", "use", "force",
)
_FORBIDDEN_CALLABLE = (
    # engine control reachable as a function/table-function call
    "call",
    # filesystem / environment readers that are functions, not statements
    "read_csv", "read_csv_auto", "read_parquet", "read_json",
    "read_json_auto", "read_json_objects", "read_text", "read_blob",
    "parquet_scan", "csv_scan", "sniff_csv", "glob", "getenv",
    "load_extension", "install_extension", "duckdb_extensions",
    # introspection / configuration disclosure (PRAGMA via function call)
    "current_setting", "duckdb_settings", "duckdb_functions",
    "pragma_database_list", "pragma_table_info", "pragma_version",
    "pragma_show", "pragma_collations",
)
_FORBIDDEN = _FORBIDDEN_KEYWORDS + _FORBIDDEN_CALLABLE  # union, for messages/tests


def _forbidden_re(words) -> re.Pattern:
    return re.compile(
        r"(?<![A-Za-z0-9_])(" + "|".join(sorted(words, key=len, reverse=True))
        + r")(?![A-Za-z0-9_])", re.IGNORECASE)


_FORBIDDEN_KEYWORDS_RE = _forbidden_re(_FORBIDDEN_KEYWORDS)
_FORBIDDEN_CALLABLE_RE = _forbidden_re(_FORBIDDEN_CALLABLE)


def _scan(sql: str, strip_comments: bool, mask_literals: bool,
          mask_idents: bool = True) -> str:
    """One character-level pass over `sql`, honouring quoting.

    `mask_idents=False` keeps DOUBLE-quoted text (SQL identifiers) readable
    while still blanking single- and dollar-quoted STRING literals. That is
    what `_referenced()` needs: every statement the query builder and the
    recipes emit quotes its table name (`FROM "datasalesorder"`), so masking
    identifiers too would leave the partition badge with nothing to find,
    while un-masking string literals would let `WHERE note = 'datasalesorder'`
    invent a table. The guard itself always masks both.

    Both jobs this scanner does are jobs a regex gets wrong in the same way:
    `WHERE note = '-- not a comment'` and `WHERE code = 'DROP'` are perfectly
    legal SELECTs, and a naive strip/search mangles or rejects them. Quoting
    handled: single-quoted strings (with `''` escapes), double-quoted
    identifiers (with `""`), and `$tag$ ... $tag$` dollar quotes.
    """
    out = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            q = ch
            body = []
            i += 1
            closed = False
            while i < n:
                c = sql[i]
                if c == q:
                    if i + 1 < n and sql[i + 1] == q:
                        body.append(c)
                        body.append(c)
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                body.append(c)
                i += 1
            if not closed:
                raise SQLGuardError("Unclosed quote in SQL.")
            text = "".join(body)
            if mask_literals and (q == "'" or mask_idents):
                # Keep the length (error offsets stay meaningful) but make the
                # contents unsearchable: a literal must never trip a keyword.
                text = " " * len(text)
            out.append(q + text + q)
            continue
        if ch == "$":
            m = _DOLLAR_RE.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                if end == -1:
                    raise SQLGuardError("Unclosed string $...$ in SQL.")
                body = sql[i + len(tag):end]
                if mask_literals:
                    body = " " * len(body)
                out.append(tag + body + tag)
                i = end + len(tag)
                continue
        if strip_comments and ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            out.append(" ")
            i = n if j == -1 else j
            continue
        if strip_comments and ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                raise SQLGuardError("Unclosed comment /* in SQL.")
            out.append(" ")
            i = j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _statements(sql: str) -> list:
    """Split on `;` OUTSIDE any quoting, dropping empties."""
    parts, buf = [], []
    masked = _scan(sql, strip_comments=False, mask_literals=True)
    # `masked` has exactly the same length as `sql`, so its semicolons index
    # straight back into the original text.
    for i, ch in enumerate(sql):
        if masked[i] == ";":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def guard_sql(sql) -> str:
    """The trust boundary. Returns the single sanitised statement to execute.

    Order is the whole point: comments are stripped FIRST, so
    `SELECT 1 --\\n; DROP TABLE t` cannot present itself as one statement,
    and statements are counted SECOND, so `;` cannot smuggle a payload past a
    prefix check. Only then is the survivor required to start with SELECT/WITH
    and to contain no forbidden word outside a literal. The sanitised text —
    not the caller's original — is what runs.
    """
    if sql is None:
        raise SQLGuardError("Empty query.")
    text = str(sql)
    if len(text) > MAX_SQL_CHARS:
        raise SQLGuardError(
            f"Query longer than {MAX_SQL_CHARS} characters — shorten it.")
    clean = _scan(text, strip_comments=True, mask_literals=False)
    stmts = _statements(clean)
    if not stmts:
        raise SQLGuardError("Empty query.")
    if len(stmts) > 1:
        raise SQLGuardError(
            "Only one query allowed — remove «;» and everything after.")
    stmt = stmts[0].strip()
    # Two scans, deliberately different on identifier-masking (see
    # _FORBIDDEN_KEYWORDS / _FORBIDDEN_CALLABLE above for why):
    #   masked_idents   — double-quoted text blanked too, so a bare/unquoted
    #                      DDL/DML keyword is caught but `"delete"` used as an
    #                      ordinary column reference is not.
    #   readable_idents — double-quoted text left readable, so a forbidden
    #                      function spelled as a quoted identifier
    #                      (`"read_csv"('/etc/passwd')`) cannot hide from the
    #                      scan the way it would if identifiers were masked.
    masked_idents = _scan(stmt, strip_comments=False, mask_literals=True, mask_idents=True)
    readable_idents = _scan(stmt, strip_comments=False, mask_literals=True, mask_idents=False)
    if not re.match(r"^\s*\(*\s*(select|with)(?![A-Za-z0-9_])", masked_idents, re.IGNORECASE):
        # What usually lands here is not malice but a copied FRAGMENT of a
        # query: a line with QUALIFY, a tail with FROM, the middle of a cheat
        # sheet. So the message says not only "not allowed" but what to do.
        raise SQLGuardError(
            "Only SELECT and WITH queries are allowed. This looks like a fragment — copy the whole statement, from SELECT or WITH through to the «;».")
    hit = _FORBIDDEN_KEYWORDS_RE.search(masked_idents) or _FORBIDDEN_CALLABLE_RE.search(readable_idents)
    if hit:
        raise SQLGuardError(
            f"Forbidden construct in SQL: {hit.group(1).upper()}. "
            "Explore can only read.")
    return stmt


# ── Execution ────────────────────────────────────────────────────────────
def _short(e) -> str:
    # DuckDB puts the message on the FIRST line and a caret on the last;
    # the last line alone reads as "      ^" to a human.
    txt = [ln for ln in str(e or "").strip().splitlines() if ln.strip()]
    return (txt[0] if txt else "error")[:300]


def _arm_interrupt(con, timeout_s):
    """Best-effort watchdog: cancel the query at `timeout_s`.

    Pyodide's default build has no pthreads, so `threading.Timer` cannot
    start there and this returns a no-op — which is why `run_sql` ALSO
    enforces the deadline between fetch chunks. Neither mechanism alone
    covers both cases: the timer stops a query stuck inside DuckDB (the
    CPython/test case), the fetch deadline stops one that streams forever
    (the browser case).
    """
    global _INTERRUPTED
    _INTERRUPTED = False
    if not timeout_s or timeout_s <= 0:
        return lambda: None
    try:
        import threading

        def fire():
            global _INTERRUPTED
            _INTERRUPTED = True
            try:
                con.interrupt()
            except Exception:
                pass

        timer = threading.Timer(float(timeout_s), fire)
        timer.daemon = True
        timer.start()
        return timer.cancel
    except Exception:
        return lambda: None


def _cell(v):
    """One value, as something `toJs` can hand to JavaScript unchanged."""
    if v is None:
        return None
    if isinstance(v, bool) or isinstance(v, (int, float, str)):
        # NaN is a float but means "missing" to every reader of this grid.
        if isinstance(v, float) and v != v:
            return None
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    item = getattr(v, "item", None)      # numpy scalar
    if callable(item):
        try:
            return _cell(item())
        except Exception:
            pass
    try:
        import decimal
        if isinstance(v, decimal.Decimal):
            return float(v)
    except Exception:
        pass
    return str(v)


def _referenced(stmt: str) -> list:
    """Which indexed tables this statement names.

    Deliberately crude — a masked word-boundary match over the known table
    names. It drives a BADGE ("5 of 7 partitions"), never a permission
    decision, so a false positive costs an extra line in the badge and a false
    negative costs a missing one. Anything cleverer would need a SQL parser
    for no gain in safety.
    """
    masked = _scan(stmt, strip_comments=True, mask_literals=True, mask_idents=False)
    low = masked.lower()
    hits = []
    for name in _TABLES:
        if re.search(r"(?<![A-Za-z0-9_\u0400-\u04FF])" + re.escape(name.lower())
                     + r"(?![A-Za-z0-9_\u0400-\u04FF])", low):
            hits.append(name)
    # A semantic view stands on catalogue tables; a query that names only the
    # view still deserves the same \u00AB\u043F\u043E K \u0438\u0437 N \u043F\u0430\u0440\u0442\u0438\u0446\u0438\u0439\u00BB badge.
    for name, deps in _SEM_DEPS.items():
        if re.search(r"(?<![A-Za-z0-9_\u0400-\u04FF])" + re.escape(name.lower())
                     + r"(?![A-Za-z0-9_\u0400-\u04FF])", low):
            for t in deps:
                if t in _TABLES and t not in hits:
                    hits.append(t)
    return hits


def _context(stmt: str) -> dict:
    """Partition provenance for the result badge."""
    used, missing, expected = [], [], 0
    for name in _referenced(stmt):
        parts = _TABLES.get(name) or []
        exp = _EXPECTED.get(name) or [p["partition"] for p in parts]
        expected += len(exp)
        have = [p["partition"] for p in parts]
        for p in parts:
            used.append({"table": name, "key": p["partition"], "file": p["file"]})
        for k in exp:
            if k not in have:
                missing.append({"table": name, "key": k})
    return {"partitions_used": used,
            "partitions_expected": expected or len(used),
            "missing_partitions": missing}


def _empty_result(error=None) -> dict:
    return {"columns": [], "rows": [], "total": 0, "ms": 0,
            "partitions_used": [], "partitions_expected": 0,
            "missing_partitions": [], "truncated": False,
            "incomplete": False, "error": error}


def _execute(stmt: str, limit: int, timeout_s: float):
    """Run `stmt`, return (columns, rows[<=limit], total|None, truncated).

    `limit + 1` is fetched on purpose: it answers "is there more?" without
    paying for a second execution when there isn't. Only when there IS more
    does a `count(*)` wrapper run, and it runs against the same deadline.
    """
    con = _conn()
    cancel = _arm_interrupt(con, timeout_s)
    deadline = time.monotonic() + float(timeout_s or 0)
    try:
        cur = con.execute(stmt)
        columns = [d[0] for d in (cur.description or [])]
        rows = []
        while len(rows) <= limit:
            chunk = cur.fetchmany(min(FETCH_CHUNK, limit + 1 - len(rows)))
            if not chunk:
                break
            rows.extend(chunk)
            if timeout_s and time.monotonic() > deadline:
                raise ExploreTimeout(
                    f"Query stopped: over {int(timeout_s)} s. "
                    "Add filter or restrict partitions.")
        truncated = len(rows) > limit
        if truncated:
            rows = rows[:limit]
    finally:
        cancel()
    total = len(rows)
    if truncated:
        total = None
        cancel2 = _arm_interrupt(con, max(1.0, deadline - time.monotonic()))
        try:
            got = con.execute(
                f"SELECT count(*) FROM ({stmt}) AS _op_total").fetchone()
            total = int(got[0]) if got else None
        except Exception:
            total = None            # honest unknown beats a wrong number
        finally:
            cancel2()
    return columns, rows, total, truncated


_ERROR_HINTS = (
    # (substring of the engine's message, human message shown FIRST)
    ("Unimplemented type for cast",
     "One column has mixed data types across files (e.g. time and datetime). "
     "Re-index folder; if it persists, contact admin."),
    ("Failed to cast",
     "Failed to unify column values across files. "
     "Try selecting columns explicitly or add partition filter."),
    ("does not exist",
     "Table or column not found. Check name "
     "(case-sensitive, quoted if needed)."),
    ("not found in FROM clause",
     "Column not found in selected table. Check name in Column list "
     "above builder."),
    ("Parser Error",
     "SQL syntax error. Check query near the location below."),
    ("Out of Memory",
     "Query ran out of browser memory. Narrow results: partition filter, "
     "fewer columns, smaller LIMIT."),
    ("Binder Error",
     "Query references missing name in data. Check column and table names."),
)


def _humanize_error(msg: str) -> str:
    """Mature tools never hand the user the engine's stack-speak. The human
    explanation with a next step comes first; the engine's own words stay in
    brackets — they are what an administrator will actually search for."""
    for needle, human in _ERROR_HINTS:
        if needle in msg:
            return f"{human} [{msg}]"
    return msg


def run_sql(sql, limit: int = DEFAULT_LIMIT, timeout_s: float = QUERY_TIMEOUT_S) -> dict:
    """Guard, run, and answer WITH ITS OWN CONTEXT.

    `incomplete` is a field of the result, never a log line: an answer built
    from 5 of 7 partitions is a different answer, and the person reading the
    grid is the person who has to know.
    """
    t0 = time.monotonic()
    try:
        stmt = guard_sql(sql)
    except ExploreError as e:
        return _empty_result(str(e))
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = DEFAULT_LIMIT
    lim = max(1, min(lim if lim > 0 else DEFAULT_LIMIT, MAX_LIMIT))
    ctx = _context(stmt)
    try:
        columns, raw, total, truncated = _execute(stmt, lim, timeout_s)
    except ExploreError as e:
        out = _empty_result(str(e))
        out.update(ctx)
        out["ms"] = int((time.monotonic() - t0) * 1000)
        out["incomplete"] = True
        return out
    except Exception as e:
        msg = _humanize_error(_short(e))
        if _INTERRUPTED:
            msg = (f"Query stopped: over {int(timeout_s)} s. "
                   "Add filter or restrict partitions.")
        out = _empty_result(msg)
        out.update(ctx)
        out["ms"] = int((time.monotonic() - t0) * 1000)
        out["incomplete"] = True
        return out
    out = {
        "columns": columns,
        "rows": [[_cell(v) for v in r] for r in raw],
        "total": total,
        "ms": int((time.monotonic() - t0) * 1000),
        "truncated": truncated,
        "error": None,
    }
    out.update(ctx)
    out["incomplete"] = bool(ctx["missing_partitions"]) or truncated
    return out


def export_xlsx(sql, limit: int = EXPORT_MAX_ROWS,
                timeout_s: float = QUERY_TIMEOUT_S) -> bytes:
    """The same query, as an .xlsx the browser can save.

    write_only mode on purpose: the export cap is 100k rows and openpyxl's
    normal model would hold every one of them as a Cell object next to the
    frames DuckDB is already keeping resident.
    """
    stmt = guard_sql(sql)
    try:
        lim = max(1, min(int(limit), EXPORT_MAX_ROWS))
    except (TypeError, ValueError):
        lim = EXPORT_MAX_ROWS
    columns, rows, _total, truncated = _execute(stmt, lim, timeout_s)
    try:
        from openpyxl import Workbook
    except ImportError as e:      # pragma: no cover - environment problem
        raise ExploreError("Export unavailable: openpyxl failed to load.") from e
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Result")
    ws.append([str(c) for c in columns])
    for r in rows:
        ws.append([_xl(v) for v in r])
    if truncated:
        # Never hand over a silently trimmed file: the sheet says so itself.
        ws.append([])
        ws.append([f"Showing first {lim} rows — result truncated."])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xl(v):
    """Excel keeps real dates and numbers; everything exotic becomes text."""
    if v is None:
        return None
    if isinstance(v, bool) or isinstance(v, (int, float, str)):
        if isinstance(v, float) and v != v:
            return None
        return v
    try:
        import datetime as _dt
        if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
            return v
    except Exception:
        pass
    item = getattr(v, "item", None)
    if callable(item):
        try:
            return _xl(item())
        except Exception:
            pass
    try:
        import decimal
        if isinstance(v, decimal.Decimal):
            return float(v)
    except Exception:
        pass
    return str(v)
