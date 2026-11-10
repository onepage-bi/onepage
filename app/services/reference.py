"""ADR-16 "Reference tables" — pure schema/row/diff logic plus
a handful of small DB helpers. No FastAPI here: the API layer (phase 2) wraps
these with routes, auth and multipart parsing; the engine (phase 3/4) copies
`norm_key` verbatim into the generator's browser runtime.

Why a service module and not just model methods: `norm_key` in particular has
to be byte-for-byte portable to Python-in-Pyodide (no third-party imports),
and keeping the whole validate/coerce/diff pipeline free of FastAPI/SQLAlchemy
imports means it can be pinned with plain unit-style smokes and reused by
`tools/real_smoke.py`-style scripts without booting the app.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.i18n import _

# ---------------------------------------------------------------------------
# norm_key — MIRRORED in static/py/_lineage.py — change both.
#
# One normalisation rule for the whole platform (ADR-16 decision): a key is
# STORED as the analyst typed it but COMPARED normalised, so "Toyota" and
# "Toyota " (trailing space from a copy-paste) are the same row, but the
# original spelling still shows on screen. It has to produce IDENTICAL output
# in CPython (server) and Pyodide (generator sandbox) or a value that matches
# on the server would silently miss in the browser check, or vice versa — so
# this function uses only `re`/`unicodedata`, nothing that could differ
# between the two runtimes (no locale, no third-party normalisation).
# ---------------------------------------------------------------------------
_DASH_RE = re.compile(r"[‐-―−]")
_WS_RE = re.compile(r"\s+")


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
    s = _DASH_RE.sub("-", s)
    s = _WS_RE.sub(" ", s)
    s = s.strip()
    return s.casefold()


# ---------------------------------------------------------------------------
# Limits & schema
# ---------------------------------------------------------------------------
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_ROWS = 20_000
MAX_ROWS_BYTES = 2 * 1024 * 1024
MAX_KEY_CHARS = 64
MAX_COLUMNS = 32
COLUMN_TYPES = ("text", "number", "date")


def validate_schema(columns: list[dict]) -> list[dict]:
    """Names unique case-insensitively, <=64 chars, types in COLUMN_TYPES,
    at least 2 columns, first column is the key and must be type "text"
    (a numeric or date key would re-open the leading-zero / locale problems
    norm_key exists to avoid). Returns a canonicalised [{name, type}] list;
    raises ValueError with a message meant to reach a human (API turns it
    into a 400)."""
    if not isinstance(columns, list) or len(columns) < 2:
        raise ValueError(_("The schema must contain at least 2 columns"))
    if len(columns) > MAX_COLUMNS:
        raise ValueError(_("No more than {max} columns", max=MAX_COLUMNS))
    out = []
    seen_lower = set()
    for i, c in enumerate(columns):
        if not isinstance(c, dict):
            raise ValueError(_("Column {n}: must be an object {{name, type}}", n=i + 1))
        name = c.get("name")
        ctype = c.get("type")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(_("Column {n}: empty name", n=i + 1))
        name = name.strip()
        if len(name) > 64:
            raise ValueError(_("Column «{name}»: name is longer than 64 characters", name=name))
        if ctype not in COLUMN_TYPES:
            raise ValueError(_("Column «{name}»: type must be one of {types}",
                             name=name, types=COLUMN_TYPES))
        lower = name.casefold()
        if lower in seen_lower:
            raise ValueError(_("Column «{name}»: name is repeated (case-insensitive)", name=name))
        seen_lower.add(lower)
        out.append({"name": name, "type": ctype})
    if out[0]["type"] != "text":
        raise ValueError(_("The first column (key) «{name}» must be of type text",
                         name=out[0]["name"]))
    return out


def _canon_text(value) -> str:
    """A scalar -> its canonical display string. An integral float (Excel's
    way of storing what looks like an integer) renders without ".0"; every
    other value goes through str()."""
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:
            return ""
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


_NUMERIC_STR_RE = re.compile(r"^-?[0-9][0-9\s ]*([.,][0-9]+)?$")


def _coerce_number(value):
    """int/float pass through as float; a numeric-looking string is parsed
    ("12" -> 12.0, "1,5" -> 1.5 — comma as decimal separator, "1 234" ->
    1234.0 — space/nbsp as thousands separator); anything else is a row
    error, by design (no silent text-to-number guessing beyond this)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return None if v != v else v
    if isinstance(value, str):
        s = value.strip()
        if not s or not _NUMERIC_STR_RE.match(s):
            return None
        s = re.sub(r"[\s ]", "", s)
        if "," in s:
            if "." in s:
                s = s.replace(",", "")
            else:
                s = s.replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _coerce_date(value):
    """Only real date/datetime objects (what openpyxl hands back for a cell
    formatted as a date) are accepted -> ISO 'YYYY-MM-DD'. A string is
    rejected by design (ADR-16 risk #10): a text cell that merely looks like
    a date is ambiguous (DD.MM vs MM.DD) in a way the platform must not
    silently resolve."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None


def coerce_row(columns: list[dict], raw_row: list) -> tuple[list, list[dict]]:
    """Per-column typed coercion for import. Returns (row, errors); row has
    one entry per column (str for text, float for number, ISO str for date),
    errors is [{"column": name, "reason": str}, ...] — a row with any error
    is not committable (dry-run shows it, commit refuses while errors exist,
    per the API contract in the plan)."""
    errors: list[dict] = []
    row: list = [None] * len(columns)
    n = min(len(columns), len(raw_row))
    for i in range(n):
        col = columns[i]
        raw = raw_row[i]
        if i == 0:
            key = _canon_text(raw).strip()
            if not key:
                errors.append({"column": col["name"], "reason": _("key is empty")})
                row[i] = ""
                continue
            if len(key) > MAX_KEY_CHARS:
                errors.append({"column": col["name"],
                               "reason": _("key is longer than {max} characters",
                                           max=MAX_KEY_CHARS)})
                row[i] = key
                continue
            row[i] = key
            continue
        ctype = col["type"]
        if raw is None or raw == "":
            row[i] = None
            continue
        if ctype == "text":
            row[i] = _canon_text(raw)
        elif ctype == "number":
            v = _coerce_number(raw)
            if v is None:
                errors.append({"column": col["name"],
                               "reason": _("not a number: {value}", value=repr(raw))})
            row[i] = v
        elif ctype == "date":
            v = _coerce_date(raw)
            if v is None:
                errors.append({"column": col["name"],
                               "reason": _("not a date: {value}", value=repr(raw))})
            row[i] = v
        else:  # pragma: no cover — validate_schema already rejects this
            errors.append({"column": col["name"], "reason": _("unknown type")})
    for i in range(n, len(columns)):
        if i == 0:
            errors.append({"column": columns[0]["name"], "reason": _("key is empty")})
            row[i] = ""
        else:
            row[i] = None
    return row, errors


def fit_rows(columns: list[dict], rows: list[list]) -> list[list]:
    """Every row exactly `len(columns)` wide: short rows (written before a
    manifest ADDED a column — see `_ensure_reference_tables`) are padded with
    None, long ones are cut.

    ADR-16 froze the schema; the addendum softened that to "the key and the
    types of existing columns are frozen, appending a column is allowed",
    which means a version written yesterday can be narrower than the schema
    it is read under today. Fitting on READ rather than rewriting history
    keeps versions immutable and keeps export/import/diff consistent: every
    consumer sees the current shape whichever version it is looking at."""
    width = len(columns)
    out: list[list] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        if len(row) == width:
            out.append(row)
        elif len(row) < width:
            out.append(row + [None] * (width - len(row)))
        else:
            out.append(row[:width])
    return out


def add_columns(db: Session, table, columns: list[dict]) -> list[str]:
    """Append columns to a table's frozen schema, additively. Returns the
    names actually added ([] when the schema already covers them).

    The key column and every existing column keep their name, position and
    type — this is the ONLY schema change the platform makes on its own, and
    it is the one that cannot invalidate a single stored row. Callers commit;
    the rows are fitted lazily by `fit_rows` on every read, so no version is
    rewritten and no history is lost."""
    have = {c["name"].casefold() for c in table.get_columns()}
    fresh = [c for c in columns if c["name"].casefold() not in have]
    if not fresh:
        return []
    merged = table.get_columns() + [{"name": c["name"], "type": c["type"]} for c in fresh]
    validate_schema(merged)
    table.set_columns(merged)
    table.updated_at = datetime.utcnow()
    return [c["name"] for c in fresh]


def schema_conflicts(table, columns: list[dict]) -> list[str]:
    """What a declaration asks for that the table's frozen schema cannot give
    without breaking stored rows: a column whose TYPE changed, and a KEY that
    is not the table's key. A column the declaration drops is NOT a conflict —
    a generator may stop reading a column the dictionary still keeps — and a
    column it adds is handled by `add_columns`."""
    have = {c["name"].casefold(): c for c in table.get_columns()}
    out: list[str] = []
    for c in columns:
        cur = have.get(c["name"].casefold())
        if cur is not None and cur["type"] != c["type"]:
            out.append(_("column «{name}»: type {have} in the reference table, {want} in the declaration",
                         name=cur["name"], have=repr(cur["type"]), want=repr(c["type"])))
    if columns and columns[0]["name"].casefold() != str(table.key_column or "").casefold():
        out.append(_("key: «{have}» in the reference table, «{want}» in the declaration",
                     have=table.key_column, want=columns[0]["name"]))
    return out


def diff_rows(columns: list[dict], old_rows: list[list], new_rows: list[list],
              *, delete_missing: bool = False) -> dict:
    """Match old vs new by norm_key(row[0]). Stable order: existing rows keep
    their current position (updated in place where a new value matched),
    genuinely new keys are appended sorted by normalised key. A key repeated
    within `new_rows` is a data problem, not a diff outcome — it is recorded
    in "duplicates" and neither copy participates in the merge. With
    `delete_missing=False` (the default) a key absent from `new_rows` is left
    exactly as it was — this is an upsert, not a replace; "removed" only
    counts rows actually dropped from the result, i.e. only ever nonzero when
    `delete_missing=True`."""
    key_of = lambda row: norm_key(row[0]) if row else ""  # noqa: E731

    old_by_key: dict[str, list] = {}
    old_order: list[str] = []
    for row in old_rows:
        k = key_of(row)
        if k not in old_by_key:
            old_order.append(k)
        old_by_key[k] = row

    errors: list[dict] = []
    duplicate_set: set[str] = set()
    new_by_key: dict[str, list] = {}
    seen_new: set[str] = set()
    for i, row in enumerate(new_rows):
        if not isinstance(row, list) or len(row) != len(columns):
            errors.append({"row": i, "column": None,
                           "reason": _("the number of values doesn't match the schema")})
            continue
        k = key_of(row)
        if not k:
            errors.append({"row": i, "column": columns[0]["name"], "reason": _("key is empty")})
            continue
        if k in seen_new:
            duplicate_set.add(k)
            continue
        seen_new.add(k)
        new_by_key[k] = row
    # A duplicated key participates in NEITHER the update nor the add — an
    # ambiguous import must not silently pick "whichever copy came first".
    for k in duplicate_set:
        new_by_key.pop(k, None)
    duplicates = sorted(duplicate_set)

    added = changed = unchanged = removed = 0
    result_rows: list[list] = []
    for k in old_order:
        old_row = old_by_key[k]
        if k in new_by_key:
            new_row = new_by_key[k]
            if new_row == old_row:
                unchanged += 1
                result_rows.append(old_row)
            else:
                changed += 1
                result_rows.append(new_row)
        else:
            if delete_missing:
                removed += 1
            else:
                result_rows.append(old_row)

    added_keys = sorted(k for k in new_by_key if k not in old_by_key)
    for k in added_keys:
        added += 1
        result_rows.append(new_by_key[k])

    return {
        "rows": result_rows,
        "added": added,
        "changed": changed,
        "removed": removed,
        "unchanged": unchanged,
        "errors": errors,
        "duplicates": duplicates,
    }


def rows_sha256(rows: list[list]) -> str:
    """sha256 over the canonical JSON encoding of `rows` — used to tell an
    unchanged re-upload (identical hash) from a real change without a
    row-by-row compare."""
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def keys_norm(columns: list[dict], rows: list[list]) -> list[str]:
    """Sorted unique normalised keys — what the engine's ref_lookup check
    (phase 3/4) fetches to compare a report column against, and the shape
    `GET .../versions/{n}/keys` (phase 2) returns."""
    out = {norm_key(row[0]) for row in rows if row}
    out.discard("")
    return sorted(out)


# ---------------------------------------------------------------------------
# DB helpers — thin, caller owns the commit (same convention as the rest of
# the codebase: services mutate a passed-in Session, routes commit).
# ---------------------------------------------------------------------------
def get_table(db: Session, slug: str):
    from app.models import ReferenceTable
    return db.execute(
        select(ReferenceTable).where(ReferenceTable.slug == slug)
    ).scalar_one_or_none()


def current_rows(db: Session, table) -> list[list]:
    """The rows of `table.current_version`, or [] for a table that has never
    had a successful import (current_version == 0 — registered from a
    manifest but nobody has uploaded data yet)."""
    from app.models import ReferenceTableVersion
    if not table.current_version:
        return []
    v = db.execute(
        select(ReferenceTableVersion).where(
            ReferenceTableVersion.table_id == table.id,
            ReferenceTableVersion.version_number == table.current_version,
        )
    ).scalar_one_or_none()
    if v is None or not v.rows_json:
        return []
    try:
        rows = json.loads(v.rows_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    # A version written before a manifest appended a column is narrower than
    # the schema it is read under now — fit it, don't rewrite it.
    return fit_rows(table.get_columns(), rows)


def create_version(db: Session, table, rows: list[list], user_id: int | None,
                    summary: dict, note: str | None = None):
    """Writes the next ReferenceTableVersion, bumps `table.current_version`
    and `table.updated_at`. Enforces MAX_ROWS / MAX_ROWS_BYTES here (not in
    the API layer) so every caller — the import endpoint AND any future
    script-driven seeding — gets the same ceiling; a version that would blow
    either limit is refused before anything is written (ValueError), never
    truncated silently. Caller commits."""
    from app.models import ReferenceTableVersion

    if len(rows) > MAX_ROWS:
        raise ValueError(_("A reference table cannot contain more than {max} rows", max=MAX_ROWS))
    # Stored rows are always exactly as wide as the schema. The import path
    # already refuses a mismatched row (`diff_rows`), but a caller working
    # through the service directly — a seeding script, a restore of a version
    # written before a column was appended — must not be able to leave a row
    # that every reader then has to repair.
    rows = fit_rows(table.get_columns(), rows)
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_ROWS_BYTES:
        raise ValueError(_("Data exceeds the {max} MB limit",
                           max=MAX_ROWS_BYTES // (1024 * 1024)))

    next_version = table.current_version + 1
    version = ReferenceTableVersion(
        table_id=table.id,
        version_number=next_version,
        rows_json=encoded,
        row_count=len(rows),
        sha256=rows_sha256(rows),
        summary_json=json.dumps(summary, ensure_ascii=False),
        note=note,
        created_by=user_id,
    )
    db.add(version)
    table.current_version = next_version
    table.updated_at = datetime.utcnow()
    return version


def ensure_data_source(db: Session, table):
    """The `data_sources` row a reference table is represented by for Data
    Sources / lineage / Data Quality acks (ADR-16) — those all key on
    `data_source_id`, and a table gains one the same way a folder or
    GreenPlum connection does, via `datasources.KIND_TABLE`. Idempotent:
    a second call for the same table returns the existing row untouched
    (label is kept in sync with the table's current title, the way an
    editable field should be)."""
    from app.models import DataSource
    from app.services.datasources import KIND_TABLE

    row = db.execute(
        select(DataSource).where(
            DataSource.kind == KIND_TABLE, DataSource.key_name == table.slug
        )
    ).scalar_one_or_none()
    if row is None:
        row = DataSource(kind=KIND_TABLE, key_name=table.slug, label=table.title)
        db.add(row)
        db.flush()
    elif row.label != table.title:
        row.label = table.title
    return row
