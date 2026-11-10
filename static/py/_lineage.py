"""Engine-side lineage probe. ONE file, two runtimes.

Loaded into the Pyodide worker (gen-worker.js fetches it and runs it before
the generator module is imported) and imported directly by CPython tests —
so nothing here may depend on the browser. Pure Python over python-calamine
and, when present, pandas.

Four entry points, called by the worker:

  sniff(path, ...)   -> dict   observed workbook structure, BEFORE add_file
  run_checks(...)    -> dict   manifest `checks` + column profiles, one stream
                               per sheet, BEFORE add_file
  install_hooks()    -> None   wrap the readers so actual reads are recorded
  drain()            -> dict   what the generator opened since the last drain,
                               keyed by absolute path
  drain_stages()     -> list   the onepage.stage() snapshots taken so far
  note_file(name)    -> None   worker: this file is going into add_file now

install_hooks() also publishes the generator-facing runtime module `onepage`
(sys.modules + builtins), whose only member today is stage(name, df, inputs).

Column granularity is a pandas-only bonus, and deliberately biased toward
OVER-reporting: calamine hands out rows, so its path can only ever say
"the whole sheet"; a pandas frame reports columns only for `usecols=[...]` and
for subscripts written by the generator itself. Selections we cannot see —
`df.loc[:, [...]]`, `.iloc`, `.filter()`, a `usecols="A:B"` range, a merge/melt
that consumes every column — leave the sheet at `"all": True`. A missing
dependency would be the damaging error; a too-wide one is only noise.
"""
from __future__ import annotations

import datetime as _dt
import inspect
import os
import re
import sys
import time
import unicodedata
from collections import Counter

# Budgets. The worker and the server enforce the same numbers; a workbook that
# exceeds them is recorded as far as the budget allows and flagged truncated.
MAX_SHEETS = 32
MAX_COLUMNS = 256
MAX_CELL_CHARS = 64
# Mirrors of genmanifest.MAX_CHECK_ROWS / MAX_KNOWN_VALUES (app.* cannot be
# imported here); tests/smoke_lineage_py.py pins the numbers together.
MAX_CHECK_ROWS = 200_000
MAX_KNOWN_VALUES = 500
# ref_lookup: distinct unmapped keys reported per check before the result is
# flagged missing_overflow. Unlike known_values (which CLEARS its set on
# overflow, because a partial "known" set would poison the server baseline),
# the first MAX_MISSING are kept — an incomplete list of values nobody mapped
# is still exactly what the responsible person needs to act on.
MAX_MISSING = 500
# Header-row heuristic: rows scanned before giving up. The window is measured
# from the first NON-EMPTY row of the sheet, not from absolute row 1 — see
# _sheet_rows for why that distinction is the difference between a header and
# a row of empty strings. Kept at 20: the production repro showed the real
# files have their header within a few rows of the used range once the blank
# prefix is skipped, so widening the window was not what they needed.
MAX_HEADER_SCAN = 20
# Longest reason/error text kept on a schema (server and lineage UI show it).
MAX_REASON_CHARS = 200

_consumed: dict[str, dict] = {}

# Saved unwrapped callables/classes, keyed by a short name. Kept so a second
# install_hooks() can tell "already ours" from "not touched yet", and so sniff()
# never goes through our own proxy (sniffing is not consumption).
_orig: dict = {}
_hooks_installed = False

# Non-zero while we are inside a pandas wrapper: pandas' own calamine engine
# does `from python_calamine import CalamineWorkbook`, i.e. it picks up our
# proxy, and would stamp the whole sheet as consumed right after read_excel
# already recorded the exact usecols.
_suppress_calamine = 0

# Stack of markers, one per read_excel call in flight; the ExcelFile.parse
# wrapper pushes into the top one when it has already recorded the read.
_inner_parse: list = []

# Set by _install_pandas(). Module-level (not a local of that function) so a
# tracked frame can be pickled — df.to_pickle() into the OPFS /cache aggregate
# is a normal thing for a generator to do, and a local class has no import path.
_TrackedFrame = None


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------

def _text(value, max_chars: int = MAX_CELL_CHARS) -> str:
    """Cell/column label -> trimmed, budget-capped str."""
    if value is None:
        return ""
    if isinstance(value, str):
        s = value
    elif isinstance(value, bool):
        s = "TRUE" if value else "FALSE"
    elif isinstance(value, float) and value.is_integer():
        # calamine returns every number as a float; "SKU 2" reads better than "SKU 2.0"
        s = str(int(value))
    else:
        s = str(value)
    s = s.strip()
    return s[:max_chars]


def _entry(path: str, sheet: str) -> dict:
    sheets = _consumed.setdefault(path, {"sheets": {}})["sheets"]
    e = sheets.get(sheet)
    if e is None:
        if len(sheets) >= MAX_SHEETS:
            return {"columns": [], "all": True}  # over budget: recorded nowhere
        # "all": True is the honest default — a sheet handed to the generator
        # whole (calamine) or a frame not yet subscripted is a full-sheet read
        # until we see the first column key.
        e = sheets[sheet] = {"columns": [], "all": True}
    return e


def _record_sheet(path, sheet) -> None:
    """Whole-sheet read (calamine can only ever tell us this much)."""
    if not path:
        return
    _entry(str(path), _text(sheet))


def _record_columns(path, sheet, columns) -> None:
    if not path or not columns:
        return
    e = _entry(str(path), _text(sheet))
    for c in columns:
        if not isinstance(c, str):
            continue  # positional usecols / numeric labels carry no name to show
        name = _text(c)
        if name and name not in e["columns"] and len(e["columns"]) < MAX_COLUMNS:
            e["columns"].append(name)
    if e["columns"]:
        e["all"] = False


def _touch(path) -> None:
    """File was opened, nothing read from it yet."""
    if path:
        _consumed.setdefault(str(path), {"sheets": {}})


# --------------------------------------------------------------------------
# sniff
# --------------------------------------------------------------------------

def _workbook_cls():
    """The REAL CalamineWorkbook, never our proxy."""
    if "calamine" in _orig:
        return _orig["calamine"]
    import python_calamine
    return python_calamine.CalamineWorkbook


def _is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


# ── Text tables (CSV/TSV) ───────────────────────────────────────────────
# A delimited text file is read through the SAME sniff/run_checks code as a
# workbook: `_TextWorkbook` quacks like CalamineWorkbook (one sheet, named
# "csv" or "tsv", `iter_rows()` streaming csv.reader rows, `height` unknown).
# Only the first 64 KB are read to pick the delimiter; the rest streams.
# Encoding: UTF-8 with an optional BOM (what the DWH exports write); bytes
# that do not decode are replaced, never fatal — a check must not die on one
# bad character in row 2,000,000.

_TEXT_EXT = (".csv", ".tsv")
_TEXT_SNIFF_BYTES = 65536
_TEXT_DELIMS = ";,\t|"


def _is_text_table(path) -> bool:
    return str(path).lower().endswith(_TEXT_EXT)


class _TextSheet:
    height = None          # unknown without a full pass — the stream decides
    start = (0, 0)

    def __init__(self, path, delimiter):
        self._path, self._delim = path, delimiter

    def iter_rows(self):
        import csv
        f = open(self._path, "r", encoding="utf-8-sig", errors="replace", newline="")
        try:
            for row in csv.reader(f, delimiter=self._delim):
                yield row
        finally:
            f.close()


class _TextWorkbook:
    def __init__(self, path):
        import csv
        self._path = str(path)
        with open(self._path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            head = f.read(_TEXT_SNIFF_BYTES)
        try:
            self._delim = csv.Sniffer().sniff(head, delimiters=_TEXT_DELIMS).delimiter
        except csv.Error:
            first = head.split("\n", 1)[0]
            self._delim = max(_TEXT_DELIMS, key=first.count) if first else ","
        self.sheet_names = ["tsv" if self._delim == "\t" else "csv"]

    def get_sheet_by_name(self, name):
        if name not in self.sheet_names:
            raise KeyError(name)
        return _TextSheet(self._path, self._delim)


def _open_table(path):
    """CalamineWorkbook for a workbook, _TextWorkbook for .csv/.tsv."""
    if _is_text_table(path):
        return _TextWorkbook(path)
    return _workbook_cls().from_path(str(path))


def _sheet_rows(sheet):
    """`sheet.iter_rows()` advanced to the sheet's USED RANGE.

    calamine reports two different origins for the same sheet and the mismatch
    is silent: `height`, `to_python()` and pandas' calamine engine all start at
    the first non-empty row, while `iter_rows()` starts at absolute row 0 and
    replays every blank row above the table. A 1C export with a 25-row title
    block therefore spent the whole MAX_HEADER_SCAN window on padding and came
    back with a header of empty strings, header_row 0, and error None — a
    schema that looks successful and matches no column. `sheet.start` is the
    (row, col) of the used range; skipping that many rows lines the iterator
    up with `height`, so `header_row` stays comparable with `rows` (data rows
    are still rows - header_row - 1) and with what pandas actually read.

    Defensive: `start` is only trusted when it is a 2-tuple of ints, so an
    older/stubbed calamine simply keeps the previous behaviour.
    """
    rows = sheet.iter_rows()
    start = getattr(sheet, "start", None)
    off = 0
    if (isinstance(start, (tuple, list)) and len(start) == 2
            and isinstance(start[0], int) and not isinstance(start[0], bool)):
        off = max(0, start[0])
    for _ in range(off):
        if next(rows, None) is None:
            break
    return rows


def _scan_header(rows, max_scan: int = MAX_HEADER_SCAN):
    """Consume at most `max_scan` rows from the iterator and pick the header.

    Returns (header_row, header_cells, buffered, reason). `reason` is None when
    a header row was actually found and a short string when it was NOT — the
    caller must not pass a fallback row off as a header, because "row 0 of a
    blank prefix" and "a real header" used to be indistinguishable downstream.
    `buffered` holds every
    consumed row AFTER the header, so a caller streaming the data rows loses
    nothing: chain(buffered, rows) is the sheet from the header onward. ONE
    pass — the iterator is never rewound and never read past max_scan here.

    The header is the first row where at least two cells are non-empty and
    they are >= 50 % of the row (test (a)). The scan STOPS there uncondi-
    tionally — a full-width first row IS the table, whatever it contains:
    duplicate labels ("Amount", "Amount"), a year-column header (["Region",
    2024, 2025]), pure numbers. It is returned as the header regardless, so
    the text-heavy data row underneath it is never promoted to header
    instead. Only title/blank lines above the table (rows failing (a)) are
    skipped. (a) is the guard against a title: calamine pads every row to
    the sheet width, so "Report for August" over a 300-column table is one
    unique string at 1/300 — without the floor it would win as row 0.
    Fallback when NO row in the scan window passes (a) at all: row 0 is still
    returned as the cells (callers that stream data need them), but `reason`
    says so and sniff records an empty header instead of a fake one. Honest
    limits: a header that fills
    under half of the sheet's width (a stray note far to the right) falls
    back to row 0; a 2-cell title over a 3-column table (ratio 2/3) wins as
    row 0; a second table lower on the sheet is not recognised.
    """
    seen: list = []
    for row in rows:
        cells = list(row)
        seen.append(cells)
        if len(seen) > max_scan:
            break
        nonempty = [c for c in cells if not _is_empty(c)]
        ratio = len(nonempty) / len(cells) if cells else 0.0
        if len(nonempty) >= 2 and ratio >= 0.5:
            # The first full-width row IS the table, whatever it contains —
            # duplicate labels, a year-column header ([Region, 2024, 2025]),
            # pure numbers. This used to be gated on the row also being
            # string-heavy and unique; when it wasn't, the code fell through
            # to `return 0, seen[0], ...` and promoted the TITLE line above
            # it to header instead — the opposite of the docstring's own
            # contract, and the reason a title-over-a-year-column-header
            # sheet reported column_missing on every check.
            idx = len(seen) - 1
            return idx, cells, [], None
    if not seen:
        return 0, [], [], "sheet is empty"
    return 0, seen[0], seen[1:], f"no header in first {max_scan} rows"


def sniff(path: str, max_sheets: int = MAX_SHEETS, max_columns: int = MAX_COLUMNS,
          max_chars: int = MAX_CELL_CHARS) -> dict:
    """Return {"sheets": [{"name", "header": [...], "header_row": int|None,
    "rows": int|None, "truncated": bool, "reason": str?}], "truncated": bool,
    "error": str|None}.

    NEVER returns an indistinguishable empty. A sheet whose header could not be
    read carries "header": [], "header_row": None and a short "reason" (empty
    sheet, unreadable sheet, no header in the scan window); a workbook that
    could not be opened carries "sheets": [] and a non-empty "error"; a
    workbook where no sheet yielded a header carries a top-level "error" too.
    The old shape had exactly one way to say all of those — a header of empty
    strings, or nothing at all — which is how seven production files ended up
    with no schema anywhere in their chain and nothing to explain it.
    Reads at most MAX_HEADER_SCAN rows per sheet via python-calamine
    `iter_rows()` to locate the header (see _scan_header); never materialises
    a sheet (`to_python()` on a 200k-row sheet costs seconds and hundreds of MB).

    "header_row" is 0-based; "rows" still counts every physical row (it is the
    sheet height, not the data-row count), so the server derives data rows as
    rows - header_row - 1 when it wants them.

    Top-level "truncated" means the sheet LIST was cut at max_sheets; the
    per-sheet flag means that sheet's header row was cut at max_columns.

    .xls / .xlsb go down the same path — calamine handles all three formats.
    """
    try:
        wb = _open_table(path)
        names = list(wb.sheet_names)
    except BaseException as exc:  # password, corrupt, unsupported, missing
        _reraise_control(exc)
        return {"sheets": [], "truncated": False, "error": _error_text(exc)}

    sheets = []
    for name in names[:max_sheets]:
        header: list[str] = []
        header_row = 0
        rows = None
        cut = False
        reason = None
        try:
            sheet = wb.get_sheet_by_name(name)
            h = getattr(sheet, "height", None)
            rows = int(h) if isinstance(h, int) else None
            # A sheet with no cell range — an empty tab (the default "Sheet1"
            # nobody filled) or a chartsheet — makes calamine's iter_rows panic
            # on a Rust `Option::unwrap` (src/types/sheet.rs:253). height == 0
            # is the cheap pre-check that keeps that panic out of the log; the
            # BaseException below is the net, because a PanicException is NOT
            # an Exception and would otherwise abort the whole sniff.
            if rows == 0:
                reason = "sheet is empty"
            else:
                header_row, cells, _, reason = _scan_header(_sheet_rows(sheet))
                if reason is None:
                    if len(cells) > max_columns:
                        cells = cells[:max_columns]
                        cut = True
                    header = [_text(c, max_chars) for c in cells]
        except BaseException as exc:
            # One unreadable sheet (chartsheet, damaged part, a used range too
            # big for the heap) must not lose the rest of the workbook — it
            # stays in the list with an empty header AND the reason why, so
            # "we could not read it" never reads as "it has no columns".
            _reraise_control(exc)
            reason = _error_text(exc)
        entry = {"name": _text(name, max_chars), "header": header,
                 "header_row": None if reason else header_row,
                 "rows": rows, "truncated": cut}
        if reason:
            entry["reason"] = _text(reason, MAX_REASON_CHARS)
        sheets.append(entry)

    if not sheets:
        error = "workbook has no sheets"
    elif all("reason" in sh for sh in sheets):
        # Every sheet failed for its own reason: say so at the top too, so a
        # caller that only looks at "error" still learns the schema is empty
        # on purpose rather than missing.
        error = _text("no header in any sheet: " + str(sheets[0].get("reason")),
                      MAX_REASON_CHARS)
    else:
        error = None
    return {"sheets": sheets, "truncated": len(names) > max_sheets, "error": error}


def _reraise_control(exc: BaseException) -> None:
    """Let the interpreter's own signals through a BaseException catch.

    MemoryError used to be on this list and is deliberately NOT any more.
    calamine's get_sheet_by_name materialises a dense height x width cell grid,
    so a single stray cell far outside the table turns a 5 KB workbook into a
    500 000 x 200 range and costs gigabytes (measured: 4.8 GB RSS in CPython,
    an OOM on the Pyodide heap). Re-raising that aborted the WHOLE sniff, the
    worker caught it in JS and sent `schema: null`, and the server reads null
    as "unchanged" — so the file kept no schema and no explanation anywhere in
    its chain. It is a per-sheet condition and is now recorded as one.
    """
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        raise exc


def _error_text(exc: BaseException) -> str:
    msg = str(exc).strip() or exc.__class__.__name__
    return _text(f"{exc.__class__.__name__}: {msg}", 200)



# --------------------------------------------------------------------------
# checks + profiles
# --------------------------------------------------------------------------

_GLOB_ESCAPE = set(".+^${}()|[]\\")
_glob_cache: dict = {}


def _glob_match(pattern: str, name: str) -> bool:
    """Port of app.services.globmatch.to_regex (** -> .*, * -> [^/]*, ? -> [^/],
    every other metacharacter literal, IGNORECASE, anchored). app.* is not
    importable in the worker; tests/smoke_lineage_py.py runs the globmatch
    table through this copy so the two cannot drift silently."""
    rx = _glob_cache.get(pattern)
    if rx is None:
        out: list[str] = []
        i, n = 0, len(pattern)
        while i < n:
            c = pattern[i]
            if c == "*":
                if i + 1 < n and pattern[i + 1] == "*":
                    out.append(".*")
                    i += 1
                else:
                    out.append("[^/]*")
            elif c == "?":
                out.append("[^/]")
            elif c in _GLOB_ESCAPE:
                out.append("\\" + c)
            else:
                out.append(c)
            i += 1
        rx = _glob_cache[pattern] = re.compile("^" + "".join(out) + "$", re.IGNORECASE)
    return rx.match(name) is not None


def _norm(s) -> str:
    """Same as schemadiff.norm: NFC + strip + casefold."""
    return unicodedata.normalize("NFC", str(s)).strip().casefold()


def _key(v) -> str:
    """Equality key for unique/enum: _norm of the cell's text form. Goes
    through _text (untruncated) so 1.0 and "1" collide the way they do in the
    header — calamine hands every number out as a float."""
    return _norm(v if isinstance(v, str) else _text(v, 1 << 30))


# ===========================================================================
# MIRRORED from app/services/reference.py — change both
# ---------------------------------------------------------------------------
# The ONE normalisation rule for reference-table keys on the whole platform
# (ADR-16). The server normalises the keys it stores and the ones it hands the
# engine; the engine normalises the cell values it compares against them. Two
# implementations of one rule is the price of the runtime split (app.* cannot
# be imported inside Pyodide) — tests/smoke_reference_service.py and
# tests/smoke_lineage_py.py pin the same cases on both sides, and
# smoke_lineage_py diffs the two functions value-by-value.
#
# Deliberately NOT normalised: leading zeros ("007" stays "007" — a code is not
# a number), and nothing is truncated here (the length ceiling lives at import
# time, where a too-long key is an error the author can see).
_KEY_DASH_RE = re.compile(r"[\u2010-\u2015\u2212]")
_KEY_WS_RE = re.compile(r"\s+")


def norm_key(s) -> str:
    """None/NaN -> ""; a float that is integral (7.0, as Excel hands back an
    integer cell) renders as "7", never "7.0"; nbsp and every other Unicode
    whitespace collapse to a single space; the Unicode dash family and the
    minus sign unify to ASCII "-"; result is stripped and casefolded.
    Leading zeros are left untouched ("007" stays "007") — they are part of
    the value, not whitespace or punctuation."""
    if s is None:
        return ""
    if isinstance(s, float):
        if s != s:  # NaN != NaN, true without importing math
            return ""
        s = str(int(s)) if s.is_integer() else str(s)
    else:
        s = str(s)
    s = unicodedata.normalize("NFC", s)
    s = _KEY_DASH_RE.sub("-", s)
    s = _KEY_WS_RE.sub(" ", s)
    s = s.strip()
    return s.casefold()
# ===========================================================================


def _number(v):
    """int/float (bool excluded) or a str that parses as float -> float; else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _is_date(v) -> bool:
    return isinstance(v, (_dt.date, _dt.datetime))


class _CheckState:
    __slots__ = ("spec", "col", "count", "failed", "examples", "seen", "status",
                 "reason", "rx", "enum", "overflow", "keys", "missing", "_mc")

    def __init__(self, spec, col, max_chars):
        self.spec = spec
        self.col = col
        self.count = 0
        self.failed = 0
        self.examples: list[str] = []
        self.seen: set = set()
        self.status = "pass"
        self.reason = None
        self.rx = None
        self.enum = None
        self.overflow = False
        self.keys = None
        self.missing: dict = {}      # norm key -> first original text seen
        t = spec.get("type")
        if t == "regex":
            try:
                self.rx = re.compile(str(spec.get("pattern") or ""))
            except re.error:
                self.status, self.reason = "skipped", "bad_pattern"
        elif t == "enum":
            self.enum = {_norm(x) for x in (spec.get("values") or [])}
        elif t == "ref_lookup":
            # `keys` is injected into the spec by the engine from the platform
            # reference table's CURRENT version — a manifest never carries it.
            # Absent (the table does not exist yet, or the fetch failed) is not
            # a failure: with nothing to compare against, every value would
            # read as unmapped, which is the opposite of the truth.
            ks = spec.get("keys")
            if isinstance(ks, (list, tuple, set)):
                self.keys = {k for k in ks if isinstance(k, str)}
            else:
                self.status, self.reason = "skipped", "no_keys"
        self._mc = max_chars

    def fail(self, v):
        self.failed += 1
        if len(self.examples) < 5:
            self.examples.append(_text(v, self._mc))


class _Profile:
    __slots__ = ("col", "nulls", "count", "kinds", "top", "overflow",
                 "nmin", "nmax", "dmin", "dmax", "tmin", "tmax")

    def __init__(self, col):
        self.col = col
        self.nulls = 0
        self.count = 0
        self.kinds: set = set()
        self.top: Counter = Counter()  # value -> n; its key count is `distinct`
        self.overflow = False
        self.nmin = self.nmax = None
        self.dmin = self.dmax = None
        self.tmin = self.tmax = None  # (norm key, original str)


def _sheet_specs(specs, declared, sheet_name):
    """(specs that match this sheet, declared columns for it) by glob."""
    mine = [sp for sp in specs if _glob_match(sp.get("sheet") or "*", sheet_name)]
    cols: list[str] = []
    for glob, names in (declared or {}).items():
        if _glob_match(glob or "*", sheet_name):
            for c in names or []:
                if isinstance(c, str) and c not in cols:
                    cols.append(c)
    return mine, cols


def run_checks(path: str, specs: list, declared: dict, max_rows: int = MAX_CHECK_ROWS,
               max_distinct: int = MAX_KNOWN_VALUES, max_chars: int = MAX_CELL_CHARS) -> dict:
    """Run the manifest's checks for ONE file and profile its declared columns.

    `specs` is genmanifest.checks_for(...) filtered to this file's match;
    `declared` is {sheet_glob: [column, ...]} from inputs[].sheets for the same
    match. Returns {"results": [...], "profiles": [...], "ms": float,
    "error": str|None} — see the plan for the per-entry shape. Everything in
    it is JSON-serialisable and every cell value that leaves this function is
    a str cut to max_chars: <= 5 fail examples per check, <= max_distinct
    distinct values for known_values, <= 5 top values per profile. Those are
    the only cell contents the server ever sees.

    One streaming iter_rows() pass per physical sheet: header located by the
    same heuristic as sniff (_scan_header), needed columns mapped by _norm,
    then at most max_rows data rows; a longer sheet marks every result and
    profile of that sheet sampled=True. A sheet glob that admits several
    physical sheets yields one result per sheet under the same check_id.
    skipped reasons: "sheet_missing", "column_missing", "unreadable",
    "cardinality_exceeded" (unique beyond max_distinct*4 distinct values),
    "bad_pattern", "no_keys" (a ref_lookup spec the engine could not attach
    the reference table's keys to). A workbook that will not open returns
    every spec skipped "unreadable" plus "error"; a single bad sheet loses
    only its own specs.

    `ref_lookup` is the one check whose spec carries data IN: `keys`, the
    reference table's current normalised keys. It reports `missing` — the
    original text of up to MAX_MISSING distinct unmapped values, sorted by
    their normalised form, with `missing_overflow` when there were more. The
    keys themselves are never echoed back.
    Honest limits: a "range" check treats a numeric-looking string as a
    number (float(s)), nothing cleverer — "1 234,5" fails; known_values and
    unique compare the cell's text form, so 1.0 and "1" are one value.
    """
    t0 = time.perf_counter()
    specs = [sp for sp in (specs or []) if isinstance(sp, dict)]
    results: list[dict] = []
    profiles: list[dict] = []

    def done(error=None):
        return {"results": results, "profiles": profiles,
                "ms": round((time.perf_counter() - t0) * 1000.0, 1), "error": error}

    def skipped(sp, sheet, reason):
        return {"check_id": sp.get("id"), "sheet": sheet, "column": sp.get("column"),
                "status": "skipped", "count": 0, "failed": 0, "examples": [],
                "distinct": None, "distinct_overflow": False,
                "missing": None, "missing_overflow": False, "sampled": False,
                "reason": reason}

    try:
        wb = _open_table(path)
        names = list(wb.sheet_names)
    except BaseException as exc:
        _reraise_control(exc)
        for sp in specs:
            results.append(skipped(sp, sp.get("sheet"), "unreadable"))
        return done(_error_text(exc))

    matched: set = set()
    for name in names:
        sname = _text(name, max_chars)
        mine, cols = _sheet_specs(specs, declared, sname)
        if not mine and not cols:
            continue
        matched.update(id(sp) for sp in mine)
        try:
            _run_sheet(wb, name, sname, mine, cols, results, profiles,
                       max_rows, max_distinct, max_chars)
        except BaseException as exc:
            _reraise_control(exc)
            # Drop whatever this sheet produced before it broke — a half-run
            # check would read as a verdict — and record the sheet as unreadable.
            results[:] = [r for r in results if r["sheet"] != sname]
            profiles[:] = [p for p in profiles if p["sheet"] != sname]
            for sp in mine:
                results.append(skipped(sp, sname, "unreadable"))
    for sp in specs:
        if id(sp) not in matched:
            results.append(skipped(sp, sp.get("sheet"), "sheet_missing"))
    return done()


def _run_sheet(wb, name, sname, mine, cols, results, profiles, max_rows, max_distinct, max_chars):
    sheet = wb.get_sheet_by_name(name)
    h = getattr(sheet, "height", None)
    # Same used-range alignment as sniff (_sheet_rows): without it a sheet with
    # a title block taller than the scan window resolved NO columns and every
    # check against it came back column_missing.
    rows_iter = iter(()) if isinstance(h, int) and h == 0 else _sheet_rows(sheet)
    header_row, header, buffered, _hdr_reason = _scan_header(rows_iter)
    if _hdr_reason is not None:
        # Never index columns off a row that is not a header — and put that row
        # back at the front of the stream, because with no header it is data.
        if header:
            buffered = [header] + list(buffered)
        header = []
    index: dict[str, int] = {}
    for i, c in enumerate(header):
        k = _norm(_text(c, max_chars))
        if k and k not in index:
            index[k] = i

    cap = max_distinct * 4
    checks: list[_CheckState] = []
    for sp in mine:
        col = index.get(_norm(sp.get("column") or ""))
        st = _CheckState(sp, col, max_chars)
        if col is None:
            st.status, st.reason = "skipped", "column_missing"
        checks.append(st)
    profs: dict[int, _Profile] = {}
    for c in cols + [sp.get("column") for sp in mine]:
        col = index.get(_norm(c or ""))
        if col is not None and col not in profs:
            profs[col] = _Profile(c)
    live = [st for st in checks if st.col is not None and st.status != "skipped"]

    sampled = False
    n = 0
    if live or profs:
        def stream():
            yield from buffered
            yield from rows_iter
        for row in stream():
            if n >= max_rows:
                sampled = True
                break
            n += 1
            cells = row if isinstance(row, list) else list(row)
            w = len(cells)
            for st in live:
                if st.status == "skipped":
                    continue
                v = cells[st.col] if st.col < w else None
                _check_cell(st, v, cap, max_distinct, max_chars)
            for col, pr in profs.items():
                _profile_cell(pr, cells[col] if col < w else None, cap, max_chars)

    for st in checks:
        sp = st.spec
        r = {"check_id": sp.get("id"), "sheet": sname, "column": sp.get("column"),
             "status": st.status, "count": st.count, "failed": st.failed,
             "examples": st.examples, "distinct": None, "distinct_overflow": False,
             "missing": None, "missing_overflow": False,
             "sampled": sampled and st.status != "skipped", "reason": st.reason}
        if st.status != "skipped":
            if sp.get("type") == "known_values":
                if st.overflow:
                    r["distinct_overflow"] = True
                else:
                    r["distinct"] = sorted(st.seen)
            elif sp.get("type") == "ref_lookup":
                # Sorted by the NORMALISED key so the order is stable across
                # runs whatever casing the source file used that day; the
                # ORIGINAL text is what travels, because that is what the
                # person filling the reference table has to recognise. The
                # `keys` the engine injected never travel back.
                r["missing"] = [st.missing[k] for k in sorted(st.missing)]
                r["missing_overflow"] = st.overflow
                if st.failed:
                    r["status"] = "fail"
            elif st.failed:
                r["status"] = "fail"
        results.append(r)

    for col, pr in profs.items():
        profiles.append(_profile_out(pr, sname, max_distinct, max_chars, sampled))


def _check_cell(st: _CheckState, v, cap, max_distinct, max_chars):
    if _is_empty(v):
        if st.spec.get("type") == "not_null":
            # not_null tests every cell (empty ones are exactly what it looks
            # for), so count must include them too — otherwise failed/count
            # is not a valid fraction (see _lineage.py history for the bug
            # this guards against: "90 of 10 rows" from disjoint sets).
            st.count += 1
            st.fail("")
        return
    st.count += 1
    t = st.spec.get("type")
    if t == "not_null":
        return
    if t == "unique":
        k = _key(v)
        if k in st.seen:
            st.fail(v)
        elif len(st.seen) >= cap:
            st.status, st.reason = "skipped", "cardinality_exceeded"
            st.seen.clear()
        else:
            st.seen.add(k)
    elif t == "range":
        x = _number(v)
        lo, hi = st.spec.get("min"), st.spec.get("max")
        if x is None or (lo is not None and x < lo) or (hi is not None and x > hi):
            st.fail(v)
    elif t == "regex":
        if st.rx.fullmatch(v if isinstance(v, str) else _text(v, 1 << 30)) is None:
            st.fail(v)
    elif t == "enum":
        if _key(v) not in st.enum:
            st.fail(v)
    elif t == "known_values":
        if not st.overflow:
            st.seen.add(_text(v, max_chars))
            if len(st.seen) > max_distinct:
                st.overflow = True
                st.seen.clear()
    elif t == "ref_lookup":
        k = norm_key(v)
        if not k or k in st.keys:
            return
        st.fail(v)          # failed += 1, plus <=5 examples like every other check
        if k not in st.missing:
            if len(st.missing) >= MAX_MISSING:
                # Keep the first MAX_MISSING — see MAX_MISSING. `failed` keeps
                # counting past the cap, so the count stays the truth even when
                # the list does not.
                st.overflow = True
            else:
                st.missing[k] = _text(v, max_chars)


def _profile_cell(pr: _Profile, v, cap, max_chars):
    if _is_empty(v):
        pr.nulls += 1
        return
    pr.count += 1
    if isinstance(v, bool):
        pr.kinds.add("mixed")
    elif isinstance(v, (int, float)):
        pr.kinds.add("number")
        if pr.nmin is None or v < pr.nmin:
            pr.nmin = v
        if pr.nmax is None or v > pr.nmax:
            pr.nmax = v
    elif _is_date(v):
        pr.kinds.add("date")
        # datetime vs date do not compare; key on the ISO text, which sorts.
        s = v.isoformat()
        if pr.dmin is None or s < pr.dmin:
            pr.dmin = s
        if pr.dmax is None or s > pr.dmax:
            pr.dmax = s
    elif isinstance(v, str):
        pr.kinds.add("text")
        k = _norm(v)
        if pr.tmin is None or k < pr.tmin[0]:
            pr.tmin = (k, v)
        if pr.tmax is None or k > pr.tmax[0]:
            pr.tmax = (k, v)
    else:
        pr.kinds.add("mixed")
    s = _text(v, max_chars)
    if not pr.overflow:
        if s in pr.top or len(pr.top) < cap:
            pr.top[s] += 1
        else:
            pr.overflow = True
    elif s in pr.top:
        pr.top[s] += 1


def _profile_out(pr: _Profile, sname, max_distinct, max_chars, sampled) -> dict:
    if not pr.count:
        kind = "empty"
    elif len(pr.kinds) == 1:
        kind = next(iter(pr.kinds))
    else:
        kind = "mixed"
    mn = mx = None
    if kind == "number":
        mn, mx = pr.nmin, pr.nmax
    elif kind == "date":
        mn, mx = pr.dmin, pr.dmax
    elif kind == "text":
        mn, mx = _text(pr.tmin[1], max_chars), _text(pr.tmax[1], max_chars)
    return {"sheet": sname, "column": pr.col, "type": kind, "nulls": pr.nulls,
            "count": pr.count, "distinct": None if pr.overflow else len(pr.top),
            "min": mn, "max": mx,
            "top": [[v, n] for v, n in pr.top.most_common(5)],
            "sampled": bool(sampled or pr.overflow)}


# --------------------------------------------------------------------------
# stages (P4.1)
# --------------------------------------------------------------------------
# The generator narrates its own pipeline: `onepage.stage("merged", df)` after
# a meaningful step. Nothing is declared in the manifest — the order is the
# call order — and nothing here may ever break a run: every entry point is
# try/except-wrapped and a bad argument is dropped in silence.

MAX_STAGES = 32
MAX_STAGE_COLUMNS = 256
MAX_STAGE_PROFILES = 64
MAX_STAGE_ROWS = 200_000
# read_files is a hint, not an inventory: a stage that follows 500 add_file
# calls lists the first MAX_STAGE_FILES of them and no more.
MAX_STAGE_FILES = 64
# _stage_inputs' own cap — deliberately not MAX_STAGES (that one bounds how
# many stage() CALLS a run may record; an `inputs=[...]` list on ONE call is
# an unrelated quantity that happens to share no contract with it).
MAX_STAGE_INPUTS = 32
# Cumulative wall-clock across every stage() call THIS RUN, mirroring the
# worker's CHECK_BUDGET_MS discipline for run_checks(): profiling ≤ 64 columns
# × ≤ 200_000 rows per call is "cheap" once, not 32 times over (measured
# 4.3 s/call on a wide low-cardinality frame -> 138 s at the ceiling with no
# budget at all). Once spent, later calls in the same run keep recording
# name/order/rows/columns/inputs/read_files (all O(columns), never the cost)
# but skip the profiling pass — still narration, just without a profile.
MAX_STAGE_BUDGET_MS = 20_000

_stages: list[dict] = []
_stage_ms_spent = 0.0
# File names handed to session.add_file() since the last accepted stage() call;
# the worker calls note_file() right before add_file, so a stage recorded from
# inside add_file lists that file, and a stage recorded in finish() lists
# everything read since the previous stage. It is a reading ORDER, not proof of
# a data dependency — the graph draws these edges dashed for that reason.
_stage_files: list[str] = []


def note_file(name) -> None:
    """Worker hook: `name` is about to be handed to session.add_file()."""
    try:
        s = _text(name)
        if s and len(_stage_files) < MAX_STAGE_FILES and s not in _stage_files:
            _stage_files.append(s)
    except BaseException as exc:   # pragma: no cover - defensive
        _reraise_control(exc)


def drain_stages() -> list:
    """Pop and return the accepted stage snapshots, in call order."""
    out = list(_stages)
    _stages.clear()
    return out


def _is_frame(df) -> bool:
    pd = sys.modules.get("pandas")
    if pd is None:
        return False
    try:
        return isinstance(df, pd.DataFrame)
    except BaseException as exc:
        _reraise_control(exc)
        return False


def _is_na(v) -> bool:
    """None / NaN / NaT / pd.NA -> True. Deliberately duck-typed: pandas is not
    imported here, and `v != v` is the one test every NA flavour answers."""
    if v is None:
        return True
    try:
        return bool(v != v)
    except BaseException as exc:
        _reraise_control(exc)
        return True   # pd.NA: comparison is NA and bool(NA) raises


def _stage_cell(v):
    """One frame cell -> something _profile_cell() understands: NA -> None,
    numpy scalar -> its Python counterpart, everything else untouched."""
    if _is_na(v):
        return None
    if isinstance(v, (str, bool, int, float, _dt.date, _dt.datetime)):
        return v
    item = getattr(v, "item", None)
    if callable(item):
        try:
            return item()
        except BaseException as exc:
            _reraise_control(exc)
    return v


def _stage_inputs(inputs):
    if not isinstance(inputs, (list, tuple)):
        return None
    out: list[str] = []
    for x in inputs:
        s = _text(x)
        if s and s not in out and len(out) < MAX_STAGE_INPUTS:
            out.append(s)
    return out or None


def stage(name, df, inputs=None) -> None:
    """Snapshot an intermediate DataFrame. See the module docstring / the
    authoring guide: shape + column names + a profile of the first
    MAX_STAGE_PROFILES columns over at most MAX_STAGE_ROWS rows. Never raises,
    never returns anything, and ignores anything that is not a DataFrame.

    Once MAX_STAGE_BUDGET_MS has been spent profiling THIS RUN's earlier
    stages, later calls still record name/order/rows/columns/inputs/
    read_files (all cheap, bounded by column count) but skip the profiling
    pass — an empty `profiles` list rather than minutes of hidden cost."""
    global _stage_ms_spent
    t0 = time.perf_counter()
    try:
        if len(_stages) >= MAX_STAGES or not _is_frame(df):
            return
        label = _text(name)
        if not label:
            return
        cols_all = list(df.columns)
        columns = [_text(c) for c in cols_all[:MAX_STAGE_COLUMNS]]
        rows = int(len(df.index))
        cap = MAX_KNOWN_VALUES * 4
        take = min(rows, MAX_STAGE_ROWS)
        sampled = rows > MAX_STAGE_ROWS
        profiles: list[dict] = []
        if _stage_ms_spent < MAX_STAGE_BUDGET_MS:
            for i in range(min(len(cols_all), MAX_STAGE_PROFILES)):
                pr = _Profile(columns[i])
                try:
                    values = df.iloc[:take, i].tolist() if take else []
                except BaseException:
                    continue   # an exotic dtype (extension array, object of objects)
                for v in values:
                    _profile_cell(pr, _stage_cell(v), cap, MAX_CELL_CHARS)
                profiles.append(_profile_out(pr, None, MAX_KNOWN_VALUES,
                                             MAX_CELL_CHARS, sampled))
        ms = (time.perf_counter() - t0) * 1000.0
        _stage_ms_spent += ms
        _stages.append({
            "name": label,
            "order": len(_stages),
            "rows": rows,
            "columns": columns,
            "profiles": profiles,
            "inputs": _stage_inputs(inputs),
            "read_files": list(_stage_files),
            "ms": round(ms, 1),
        })
        _stage_files.clear()
    except BaseException:
        # A stage is narration. It never costs the run — not even a warning,
        # because the worker has no channel for one from inside the generator.
        # Unlike sniff()/run_checks(), this promise covers EVERY exception,
        # MemoryError included: stage() is the one call site that allocates a
        # fresh MAX_STAGE_ROWS-element Python list per column, so an
        # allocation failure here is plausible, not academic, and the
        # authoring guide states flatly that stage() never raises.
        pass


def _install_onepage() -> None:
    """Expose the `onepage` module the generator calls: importable
    (`import onepage`) AND present as a builtin, so a generator that never
    imports it still works. Idempotent; ours is recognised by _op_lineage."""
    import builtins
    import types

    mod = sys.modules.get("onepage")
    if mod is None or not getattr(mod, "_op_lineage", False):
        mod = types.ModuleType("onepage")
        mod.__doc__ = "OnePage generator runtime API (see docs/GENERATOR_AUTHORING.md)."
        mod._op_lineage = True
        mod.stage = stage
        mod.MAX_STAGES = MAX_STAGES
        # The platform's key normalisation, so a generator that reads a
        # reference workbook maps with exactly the rule the ref_lookup check
        # (and the server) used — no generator-local trim/lower drift.
        mod.norm_key = norm_key
        sys.modules["onepage"] = mod
    if getattr(builtins, "onepage", None) is not mod:
        builtins.onepage = mod


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------

def install_hooks() -> None:
    """Wrap python_calamine.CalamineWorkbook (module-level proxy — the class
    is Rust-backed and must not be subclassed) and pandas.read_excel /
    pandas.ExcelFile when pandas is importable. Must run BEFORE the generator
    module is imported, or its `from python_calamine import CalamineWorkbook`
    binds the unwrapped class.

    Idempotent: a second call is a no-op.
    """
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    _install_calamine()
    _install_pandas()
    _install_onepage()


def drain() -> dict:
    """Pop and return {path: {"sheets": {name: {"columns": [...], "all": bool}}}}."""
    out = dict(_consumed)
    _consumed.clear()
    return out


def _install_calamine() -> None:
    try:
        import python_calamine
    except Exception:
        return
    real = getattr(python_calamine, "CalamineWorkbook", None)
    if real is None or getattr(real, "_op_lineage_proxy", False):
        return
    _orig["calamine"] = real

    class CalamineWorkbookProxy:
        """Stand-in for the Rust class: same constructors, remembers the path.

        Generators do `from python_calamine import CalamineWorkbook`, so this
        class IS what they hold — `isinstance(wb, CalamineWorkbook)` against
        the name they imported keeps working. Subclassing the Rust class is not
        possible, hence the name swap in the module namespace.
        """

        _op_lineage_proxy = True

        def __init__(self, wb, path):
            self._op_wb = wb
            self._op_path = path

        @classmethod
        def from_path(cls, path, *a, **kw):
            return cls(real.from_path(path, *a, **kw), str(path))

        def __getattr__(self, name):
            return getattr(self._op_wb, name)

        def __dir__(self):
            return sorted(set(dir(self._op_wb)) | set(object.__dir__(self)))

        def get_sheet_by_name(self, name, *a, **kw):
            if not _suppress_calamine:
                # calamine hands out ROWS, never columns: sheet-level is the
                # finest truth available on this path (see plan, limitation 1).
                _record_sheet(self._op_path, name)
            return self._op_wb.get_sheet_by_name(name, *a, **kw)

        def get_sheet_by_index(self, index, *a, **kw):
            if not _suppress_calamine:
                try:
                    name = self._op_wb.sheet_names[index]
                except Exception:
                    name = "#%s" % (index,)
                _record_sheet(self._op_path, name)
            return self._op_wb.get_sheet_by_index(index, *a, **kw)

    def _filelike_ctor(attr):
        def ctor(cls, obj, *a, **kw):
            path = getattr(obj, "name", None) or "<buffer>"
            return cls(getattr(real, attr)(obj, *a, **kw), str(path))
        return classmethod(ctor)

    # from_buffer/from_object exist depending on the calamine build (0.6.2 ships
    # from_object); wrap whichever are there so no constructor loses the path.
    for attr in ("from_filelike", "from_buffer", "from_object"):
        if hasattr(real, attr):
            setattr(CalamineWorkbookProxy, attr, _filelike_ctor(attr))

    CalamineWorkbookProxy.__name__ = "CalamineWorkbook"
    CalamineWorkbookProxy.__qualname__ = "CalamineWorkbook"
    python_calamine.CalamineWorkbook = CalamineWorkbookProxy

    # load_workbook() is the one-liner python-calamine's README leads with, and
    # it constructs the Rust class directly — without this it would be a hole
    # straight past the proxy.
    real_load = getattr(python_calamine, "load_workbook", None)
    if real_load is not None and not getattr(real_load, "_op_lineage_wrapped", False):
        _orig["load_workbook"] = real_load

        def load_workbook(obj, *a, **kw):
            path = obj if isinstance(obj, (str, bytes, os.PathLike)) else \
                (getattr(obj, "name", None) or "<buffer>")
            return CalamineWorkbookProxy(real_load(obj, *a, **kw), _fsdecode(path))

        load_workbook._op_lineage_wrapped = True
        python_calamine.load_workbook = load_workbook


def _fsdecode(p):
    if isinstance(p, str):
        return p
    try:
        return os.fsdecode(p)
    except Exception:
        return None


def _io_path(io):
    """Best-effort absolute-ish path behind a pandas `io` argument, or None.

    Never raises: it runs outside the wrapped call, so an exception here would
    surface as a failure of a read that used to work.
    """
    try:
        if isinstance(io, str):
            return io
        # pandas.ExcelFile is itself os.PathLike, but its __fspath__ returns the
        # RAW io — a BufferedReader for `ExcelFile(open(p,"rb"))`, which makes
        # os.fsdecode raise. Unwrap the container BEFORE the PathLike branch.
        for attr in ("_io", "io"):  # pandas 3.0 keeps it in _io
            if hasattr(io, attr):
                inner = getattr(io, attr)
                if inner is io:
                    break
                return _io_path(inner)
        if isinstance(io, (bytes, os.PathLike)):
            return _fsdecode(io)
        name = getattr(io, "name", None)
        return name if isinstance(name, str) else None
    except Exception:
        return None


def _sheet_label(sheet_name):
    if isinstance(sheet_name, str):
        return sheet_name
    if isinstance(sheet_name, int):
        # No name without opening the book; the caller resolves it when it can
        # (ExcelFile.parse has sheet_names in hand), otherwise this marker ships.
        return "#%d" % sheet_name
    return None


def _bind(orig, args, kwargs, names):
    """Best-effort read of named params out of a positional/keyword call."""
    out = {}
    try:
        b = inspect.signature(orig).bind(*args, **kwargs)
        b.apply_defaults()
        for n in names:
            out[n] = b.arguments.get(n)
    except (TypeError, ValueError):
        for n in names:
            out[n] = kwargs.get(n)
    return out


def _external_caller() -> bool:
    """True when the frame method was called from outside pandas itself.

    Depth 2 = the caller of __getitem__ (1 is __getitem__'s own frame). Reading
    one frame's globals costs ~1 µs, and only on a subscript of a tracked frame.
    """
    try:
        mod = sys._getframe(2).f_globals.get("__name__") or ""
    except Exception:
        return True  # cannot tell -> record (over-reporting is the safe side)
    return mod != "pandas" and not mod.startswith("pandas.")


def _rebuild_frame(state):
    """Unpickle a tracked frame as a plain DataFrame (see _TrackedFrame.__reduce__)."""
    import pandas as pd
    obj = pd.DataFrame.__new__(pd.DataFrame)
    obj.__setstate__(state)
    for attr in ("_lin_key", "_lin_cols"):  # a plain frame must carry no lineage
        obj.__dict__.pop(attr, None)
    return obj


def _install_pandas() -> None:
    try:
        import pandas as pd
    except Exception:
        return  # pandas is optional in a generator bundle; calamine hooks stand alone
    if getattr(pd.read_excel, "_op_lineage_wrapped", False):
        return

    global _TrackedFrame

    class _TrackedFrame(pd.DataFrame):  # noqa: F811 — published to the module below
        """DataFrame that remembers where it was read from.

        `_metadata` makes pandas carry `_lin_key` across the operations that
        call __finalize__ (slicing, copy, arithmetic), so `df.dropna()['Qty']`
        still attributes to the right sheet. `_lin_cols` is the column set as it
        was AT READ TIME — see __getitem__.
        """

        _metadata = ["_lin_key", "_lin_cols"]

        @property
        def _constructor(self):
            return _TrackedFrame

        def __getitem__(self, key):
            k = getattr(self, "_lin_key", None)
            # Only a subscript written by the GENERATOR is evidence of column
            # use. pandas subscripts frames constantly inside its own algorithms
            # (merge on a temporary "_cross_<uuid>" key, assign(), melt, ...) and
            # counting those both invents columns that were never in the file and
            # — worse — flips the sheet to all:False while every OTHER column of
            # the frame still flows into the result. Two guards, both cheap:
            # the caller's module, and the read-time column set.
            if k is not None and _external_caller():
                cols = getattr(self, "_lin_cols", None)
                if isinstance(key, str):
                    keys = [key]
                elif isinstance(key, (list, tuple)) and all(isinstance(x, str) for x in key):
                    keys = list(key)
                else:
                    keys = []
                if cols is not None:
                    keys = [x for x in keys if x in cols]
                _record_columns(k[0], k[1], keys)
            return super().__getitem__(key)

        def __finalize__(self, other, method=None, **kwargs):
            out = super().__finalize__(other, method=method, **kwargs)
            if getattr(out, "_lin_key", None) is None:
                k = getattr(other, "_lin_key", None)
                if k is None:  # concat/merge pass a helper object, not a frame
                    for o in getattr(other, "objs", ()) or ():
                        k = getattr(o, "_lin_key", None)
                        if k is not None:
                            break
                if k is not None:
                    object.__setattr__(out, "_lin_key", k)
            return out

        def __reduce__(self):
            # A lineage key is process-local, so a pickled frame comes back a
            # PLAIN DataFrame: df.to_pickle() into the OPFS /cache aggregate
            # must stay loadable by a session that has not installed the hooks.
            return (_rebuild_frame, (self.__getstate__(),))

    _TrackedFrame.__qualname__ = "_TrackedFrame"  # pickle looks the class up by it

    def _track(frame, path, sheet):
        if path is None or not isinstance(frame, pd.DataFrame):
            return frame
        try:
            if type(frame) is pd.DataFrame:
                frame.__class__ = _TrackedFrame  # in place: no data copy
            object.__setattr__(frame, "_lin_key", (str(path), _text(sheet)))
            # Frozen here, never refreshed: a name that is not a column of what
            # we read cannot be a column of the source file.
            object.__setattr__(frame, "_lin_cols",
                               frozenset(c for c in frame.columns if isinstance(c, str)))
        except Exception:
            pass
        return frame

    def _record_result(result, path, sheet_name, usecols, resolve=None):
        if path is None:
            return result
        # A string usecols ("A:B", "SKU,Qty") is an Excel range/expression, not a
        # list of labels: we do not expand it, so the sheet stays all:True. Same
        # for a callable. Over-reporting is the safe direction.
        cols = usecols if isinstance(usecols, (list, tuple)) and \
            all(isinstance(c, str) for c in usecols) else None
        if isinstance(result, dict):
            # sheet_name=None -> the dict keys ARE the real sheet names, but for
            # sheet_name=[0, "Ref"] pandas keys the dict by the elements AS GIVEN,
            # so an int element arrives as an int and must be resolved first.
            for name, frame in result.items():
                if resolve is not None:
                    name = resolve(name)
                if cols:
                    _record_columns(path, name, cols)
                else:
                    _record_sheet(path, name)
                _track(frame, path, name)
        else:
            label = _sheet_label(sheet_name)
            if label is None:
                label = "#0"
            if cols:
                _record_columns(path, label, cols)
            else:
                _record_sheet(path, label)
            _track(result, path, label)
        return result

    orig_read_excel = pd.read_excel
    _orig["read_excel"] = orig_read_excel

    def read_excel(io, *args, **kwargs):
        global _suppress_calamine
        path = _io_path(io)
        p = _bind(orig_read_excel, (io,) + args, kwargs, ("sheet_name", "usecols"))
        _suppress_calamine += 1
        _inner_parse.append([])
        try:
            result = orig_read_excel(io, *args, **kwargs)
        finally:
            _suppress_calamine -= 1
            inner = _inner_parse.pop()
        if inner:
            # read_excel is a thin wrapper over ExcelFile.parse (pandas 3.0
            # source), and that wrapper already recorded — with the sheet NAME
            # resolved from an int sheet_name, which we cannot do out here.
            # Recording again would mint a phantom "#0" entry and misdirect the
            # frame's _lin_key to it.
            return result
        try:
            return _record_result(result, path, p.get("sheet_name", 0), p.get("usecols"))
        except Exception:
            return result

    read_excel._op_lineage_wrapped = True
    read_excel.__doc__ = orig_read_excel.__doc__
    read_excel.__name__ = "read_excel"
    pd.read_excel = read_excel

    orig_parse = pd.ExcelFile.parse
    _orig["ExcelFile.parse"] = orig_parse

    def parse(self, *args, **kwargs):
        global _suppress_calamine
        path = _io_path(self)
        p = _bind(orig_parse, (self,) + args, kwargs, ("sheet_name", "usecols"))
        sheet_name = p.get("sheet_name", 0)
        resolve = None
        if isinstance(sheet_name, (int, list, tuple)) and not isinstance(sheet_name, bool):
            try:  # ExcelFile is already open, so the names cost nothing here
                names = list(orig_sheet_names.fget(self))
            except Exception:
                names = None
            if names is not None:
                def resolve(key, _names=names):
                    if isinstance(key, int) and not isinstance(key, bool):
                        try:
                            return _names[key]
                        except IndexError:
                            return key
                    return key
                if isinstance(sheet_name, int):
                    sheet_name = resolve(sheet_name)
        _suppress_calamine += 1
        try:
            result = orig_parse(self, *args, **kwargs)
        finally:
            _suppress_calamine -= 1
        try:
            out = _record_result(result, path, sheet_name, p.get("usecols"), resolve)
        except Exception:
            return result
        if _inner_parse and path is not None:
            _inner_parse[-1].append(1)  # tells the read_excel wrapper to stand down
        return out

    parse._op_lineage_wrapped = True
    pd.ExcelFile.parse = parse

    orig_sheet_names = pd.ExcelFile.sheet_names
    if isinstance(orig_sheet_names, property):
        _orig["ExcelFile.sheet_names"] = orig_sheet_names

        def sheet_names(self):
            # Enumerating is not consuming: register the file with no sheets so
            # "opened but nothing read" is distinguishable from "never opened".
            _touch(_io_path(self))
            return orig_sheet_names.fget(self)

        pd.ExcelFile.sheet_names = property(sheet_names)

    _tracked_frame_cls[0] = _TrackedFrame


# Exposed for the test: the subclass is created inside _install_pandas so the
# module imports cleanly without pandas.
_tracked_frame_cls: list = [None]
