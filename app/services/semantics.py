"""Generator semantics — `semantics.json` at the bundle root (ADR-13).

What a question-answering layer needs to know about a report, as DATA that
ships with the generator version and is frozen alongside the manifest:
canonical tables (source sheet/headers -> snake_case columns), derived views,
KPI definitions as DuckDB SQL over those tables, a glossary, and the rules the
generator applies before any number exists. The platform never *computes* with
this on the server — it validates it at registration (shape, identifiers,
read-only SQL, and a dry run over empty tables so a typo in a column name is a
400 for the author, not a runtime error for an operator) and hands it to the
browser engine, where the same SQL runs over the files the user opened.

The file is optional: a bundle without it registers exactly as before.
"""
from __future__ import annotations

import json
import re

SEMANTICS_NAME = "semantics.json"
SEMANTICS_MAX_BYTES = 512 * 1024

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TYPES = {"text", "number", "datetime", "date", "bool"}
_ROLES = {"key", "dim", "measure", "time", "attr"}
_TOP_LEVEL = {
    "schema_version", "version", "generator", "generator_version", "calculation_version",
    "status", "scope", "glossary", "rules", "tables", "views", "kpis",
    "aggregations", "periods", "output_tables", "uncertainties",
    "known_findings", "questions",
}
_LIMITS = {"tables": 64, "columns": 256, "views": 64, "kpis": 128, "glossary": 400,
           "inline_rows": 2000, "sql_chars": 20000, "question_forms": 16}
DRY_RUN_TIMEOUT_S = 5.0

# ── document version (independent of `schema_version`) ──────────────────
# `schema_version` above is the fixed shape marker (must be 1, always has
# been). `version` is the NEW, separate knob this file's format itself may
# grow over time — optional, defaults to 1, and a bundle declaring a version
# newer than this build understands is a 400 (upgrade the platform, don't
# silently half-read a newer file) rather than an author-facing shape error.
SUPPORTED_VERSION = 1
_KPI_OWNER_MAX = 120
_APPROVED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ── platform reference tables as a semantics source (ADR-16 + ADR-13) ─────
# `sources: [{"reference": "sl_client_map"}]` — the table's rows are the
# CURRENT version of a platform reference table ("Reference tables"), not a sheet
# in the user's folder and not a frozen `rows` block. The author declares the
# shape; the platform supplies the rows at snapshot time, so a correction made
# on the reference page is in the next answer without a re-registration.
#
# `_SLUG_RE` MIRRORS `app.services.reference.SLUG_RE` — pinned by
# tests/smoke_reference_semantics.py. It is copied rather than imported so
# this module stays importable without the SQLAlchemy models behind it.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# A reference column is text|number|date (reference.COLUMN_TYPES); `datetime`
# and `bool` have no counterpart on a reference table, so a semantics column
# that claims one could never match its source.
_REFERENCE_TYPES = {"text", "number", "date"}
# The wire ceiling for one reference-backed table. `reference.MAX_ROWS` is
# 20 000 and the store also caps a version at 2 MB of compact JSON, so a
# single table costs at most ~1.5 MB in the snapshot (rows travel as lists,
# not objects — see `_explore._materialize_semantics`). MAX_REFERENCE_BYTES
# is the budget for ALL reference tables in ONE snapshot: past it the rest
# come back `unbound` with an honest message instead of a 6 MB page.
MAX_REFERENCE_ROWS = 20_000
MAX_REFERENCE_BYTES = 4 * 1024 * 1024


class SemanticsError(ValueError):
    """Human-readable semantics problem — the API maps it to HTTP 400."""


# ── the read-only guard is the WORKER's guard ────────────────────────────
# `static/py/_explore.py` carries the hardened `guard_sql` (comments stripped
# first, one statement, SELECT/WITH, keywords scanned with identifiers masked
# and callables with identifiers readable — the `"read_csv"(...)` trap). The
# module is stdlib-only at import time, so the server loads that very file
# instead of keeping a second, weaker regex in step with it by hand: one
# guard, one test suite, and what passes registration is exactly what the
# browser will accept.
_GUARD = None


def _worker_guard():
    global _GUARD
    if _GUARD is None:
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[2] / "static" / "py" / "_explore.py"
        spec = importlib.util.spec_from_file_location("_op_explore_guard", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _GUARD = mod
    return _GUARD


# ── structural validation ────────────────────────────────────────────────
def _check_sql(sql, where: str, errors: list[str]) -> None:
    if not isinstance(sql, str) or not sql.strip():
        errors.append(f"{where}: sql must be a non-empty string")
        return
    if len(sql) > _LIMITS["sql_chars"]:
        errors.append(f"{where}: sql exceeds {_LIMITS['sql_chars']} characters")
        return
    # validate() runs on the normalized text, so any `{name}` still here has
    # no matching rule — inside a string literal an empty-table dry run would
    # never notice it.
    left = _PLACEHOLDER_RE.search(sql)
    if left:
        errors.append(f"{where}: unresolved placeholder {left.group(0)} (no such key in rules)")
    try:
        _worker_guard().guard_sql(sql)
    except Exception as e:
        errors.append(f"{where}: {e}")


def _check_columns(tname: str, columns, errors: list[str]) -> None:
    if not isinstance(columns, dict) or not columns:
        errors.append(f"tables.{tname}.columns must be a non-empty object")
        return
    if len(columns) > _LIMITS["columns"]:
        errors.append(f"tables.{tname}: more than {_LIMITS['columns']} columns")
    seen_sources: dict[tuple[str, int], str] = {}
    for cname, c in columns.items():
        where = f"tables.{tname}.columns.{cname}"
        if not _ID_RE.match(str(cname)):
            errors.append(f"{where}: column name must match ^[a-z][a-z0-9_]{{0,63}}$")
        if cname.startswith("__"):
            errors.append(f"{where}: '__' prefix is reserved for engine columns")
        if not isinstance(c, dict):
            errors.append(f"{where}: must be an object")
            continue
        if c.get("type") not in _TYPES:
            errors.append(f"{where}.type must be one of {sorted(_TYPES)}")
        if c.get("role") is not None and c.get("role") not in _ROLES:
            errors.append(f"{where}.role must be one of {sorted(_ROLES)}")
        src = c.get("source")
        if src is not None:
            if not isinstance(src, str) or not src.strip() or len(src) > 200:
                errors.append(f"{where}.source must be a non-empty string (≤200)")
            else:
                occ = c.get("occurrence", 0)
                if not isinstance(occ, int) or isinstance(occ, bool) or occ < 0 or occ > 32:
                    errors.append(f"{where}.occurrence must be an integer 0..32")
                    occ = 0
                # Dedupe on the key the engine will resolve to (norm_col +
                # occurrence suffix): headers that differ only in NFC form or
                # whitespace are ONE catalogue column, not two.
                key = (source_key(src, occ) or src.strip().casefold(), occ)
                if key in seen_sources:
                    errors.append(
                        f"{where}: source header {src!r} is already mapped by "
                        f"{seen_sources[key]!r} — duplicate headers need distinct `occurrence` values")
                seen_sources[key] = cname
        for k in ("description",):
            if c.get(k) is not None and (not isinstance(c[k], str) or len(c[k]) > 500):
                errors.append(f"{where}.{k} must be a string (≤500)")


_PATTERN_BAD_CHARS = set('<>()[]{}|:"')
_PATTERN_TAIL_RE = re.compile(r"(\.xls[xmb]?|\.xls\*|\*|\?)$", re.IGNORECASE)


def _check_file_pattern(pattern) -> str | None:
    """A source `file` is a glob over workbook names, never prose. The first
    blind extraction wrote `"*.xlsx (usually SL_<week>.xlsx)"` — it validated,
    matched nothing, and the whole semantics silently fell out of /explore."""
    from app.services.globmatch import validate_pattern
    err = validate_pattern(pattern)
    if err:
        return err
    if any(ch in _PATTERN_BAD_CHARS for ch in pattern):
        return "looks like prose, not a file glob (no <>()[]{}|:\" in a workbook name)"
    if not _PATTERN_TAIL_RE.search(pattern):
        return "must end with a workbook extension (.xlsx/.xlsm/.xls) or a wildcard"
    return None


def reference_slug_of(t) -> str | None:
    """The reference table slug a semantic table is backed by, or None."""
    if not isinstance(t, dict):
        return None
    for s in (t.get("sources") or []):
        if isinstance(s, dict) and isinstance(s.get("reference"), str) and s["reference"]:
            return s["reference"]
    return None


def _check_reference_table(tname: str, t, errors: list[str]) -> None:
    """The extra rules a reference-backed table lives by. The declaration has
    to be complete enough that the platform can fill it in without guessing:
    ONE source, no inline `rows`, no per-column `source` headers (a column
    name IS the reference column name), a single `business_key` naming the
    column that must line up with the table's `key_column`, and types that a
    reference table can actually hold."""
    where = f"tables.{tname}"
    sources = t.get("sources") if isinstance(t.get("sources"), list) else []
    refs = [s.get("reference") for s in sources
            if isinstance(s, dict) and s.get("reference") is not None]
    if not refs:
        return
    if len(sources) > 1:
        errors.append(f"{where}.sources: a reference-backed table has exactly one source")
    if t.get("rows") is not None:
        errors.append(f"{where}: a reference-backed table must not carry inline rows "
                      f"(the platform supplies them from the current version)")
    bk = t.get("business_key")
    if not isinstance(bk, str) or not bk.strip():
        errors.append(f"{where}.business_key must name the key column "
                      f"(a reference table has exactly one key)")
    cols = t.get("columns") if isinstance(t.get("columns"), dict) else {}
    for cname, c in cols.items():
        if not isinstance(c, dict):
            continue
        if c.get("source") is not None:
            errors.append(f"{where}.columns.{cname}.source is not allowed on a reference-backed "
                          f"table: the column name is the reference column name")
        if c.get("type") in _TYPES and c.get("type") not in _REFERENCE_TYPES:
            errors.append(f"{where}.columns.{cname}.type must be one of "
                          f"{sorted(_REFERENCE_TYPES)} on a reference-backed table")


def _check_table(tname: str, t, errors: list[str]) -> None:
    where = f"tables.{tname}"
    if not _ID_RE.match(str(tname)) or str(tname).startswith("__"):
        errors.append(f"{where}: table name must match ^[a-z][a-z0-9_]{{0,63}}$")
    if not isinstance(t, dict):
        errors.append(f"{where}: must be an object")
        return
    sources = t.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append(f"{where}.sources must be a non-empty list")
    else:
        for i, s in enumerate(sources):
            if not isinstance(s, dict):
                errors.append(f"{where}.sources[{i}] must be an object")
                continue
            if not any(k in s for k in ("file", "file_pattern", "bundle_config",
                                        "object", "slot", "reference")):
                errors.append(f"{where}.sources[{i}] needs file/file_pattern, "
                              f"bundle_config, object, slot or reference")
            # A reference source (ADR-16): the rows are a platform reference
            # table's CURRENT version. Exclusive of every other source kind —
            # a table is either the user's files or the platform's dictionary,
            # never a union of the two.
            ref = s.get("reference")
            if ref is not None:
                if not isinstance(ref, str) or not _SLUG_RE.match(ref):
                    errors.append(f"{where}.sources[{i}].reference must be a reference table slug "
                                  f"(^[a-z0-9][a-z0-9_-]{{0,63}}$)")
                if any(k in s for k in ("file", "file_pattern", "bundle_config", "object", "slot")):
                    errors.append(f"{where}.sources[{i}]: reference is exclusive of "
                                  f"file/object/slot sources")
            # A slot source (ADR-14b addendum): the table's rows are a slot
            # file this generator publishes (`slot_N` original name). The
            # publish path converts it into a lake partition; the browser
            # folder mode simply never binds it.
            slot = s.get("slot")
            if slot is not None:
                if not isinstance(slot, str) or not slot.strip() or len(slot) > 200 \
                        or re.search(r"[/\\]|\.\.", slot):
                    errors.append(f"{where}.sources[{i}].slot must be a bare published file name")
                if any(k in s for k in ("file", "file_pattern", "bundle_config", "object")):
                    errors.append(f"{where}.sources[{i}]: slot is exclusive of file/object sources")
            # Only workbook sources carry a glob; inline tables describe where
            # their rows came from (`object`, `bundle_config`) — that is
            # provenance, not a pattern to match files against.
            pat = s.get("file", s.get("file_pattern"))
            if pat is not None and not any(k in s for k in ("object", "bundle_config", "reference")) \
                    and t.get("rows") is None:
                bad = _check_file_pattern(pat)
                if bad:
                    errors.append(f"{where}.sources[{i}].file {pat!r}: {bad}")
    _check_columns(tname, t.get("columns"), errors)
    _check_reference_table(tname, t, errors)
    bk = t.get("business_key")
    if bk is not None:
        keys = [bk] if isinstance(bk, str) else bk
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            errors.append(f"{where}.business_key must be a string or a list of strings")
        else:
            cols = t.get("columns") if isinstance(t.get("columns"), dict) else {}
            for k in keys:
                if k not in cols:
                    errors.append(f"{where}.business_key {k!r} is not a declared column")
    rows = t.get("rows")
    if rows is not None:
        if not isinstance(rows, list) or len(rows) > _LIMITS["inline_rows"]:
            errors.append(f"{where}.rows must be a list of ≤{_LIMITS['inline_rows']} objects")
        else:
            cols = t.get("columns") if isinstance(t.get("columns"), dict) else {}
            for i, r in enumerate(rows[:50]):
                if not isinstance(r, dict) or any(k not in cols for k in r):
                    errors.append(f"{where}.rows[{i}] has keys outside the declared columns")
                    break


def _valid_iso_date(s: str) -> bool:
    import datetime
    try:
        datetime.date.fromisoformat(s)
        return True
    except ValueError:
        return False


def validate(sem, manifest_name: str | None = None) -> list[str]:
    """Shape check. Returns human-readable errors ([] = valid). Unknown
    top-level keys are errors on purpose, like the manifest."""
    if not isinstance(sem, dict):
        return ["semantics must be a JSON object"]
    errors: list[str] = []
    for key in sorted(set(sem) - _TOP_LEVEL):
        errors.append(f"unknown field: {key!r}")
    if sem.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    version = sem.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("version must be a positive integer")
    elif version > SUPPORTED_VERSION:
        errors.append(
            f"unsupported semantics version {version} (this build supports up to "
            f"{SUPPORTED_VERSION}); upgrade the platform or lower the file's version")
        return errors  # a newer shape may not match anything else checked below
    gen = sem.get("generator")
    if not isinstance(gen, str) or not gen:
        errors.append("generator is required")
    elif manifest_name is not None and gen != manifest_name:
        errors.append(f"generator {gen!r} does not match manifest name {manifest_name!r}")

    tables = sem.get("tables")
    if not isinstance(tables, dict) or not tables:
        errors.append("tables must be a non-empty object")
        tables = {}
    elif len(tables) > _LIMITS["tables"]:
        errors.append(f"more than {_LIMITS['tables']} tables")
    for tname, t in tables.items():
        _check_table(str(tname), t, errors)

    # SQL is checked in its RESOLVED form whether the caller passes the raw
    # file or the normalized one — the guard must see the text that runs.
    rules = sem.get("rules") if isinstance(sem.get("rules"), dict) else {}
    views = sem.get("views") or {}
    if not isinstance(views, dict):
        errors.append("views must be an object")
        views = {}
    elif len(views) > _LIMITS["views"]:
        errors.append(f"more than {_LIMITS['views']} views")
    for vname, v in views.items():
        if not _ID_RE.match(str(vname)) or vname in tables:
            errors.append(f"views.{vname}: name must match ^[a-z][a-z0-9_]{{0,63}}$ and not collide with a table")
        sql = v.get("sql") if isinstance(v, dict) else v
        _check_sql(_resolve(sql, rules), f"views.{vname}", errors)

    kpis = sem.get("kpis")
    if not isinstance(kpis, dict) or not kpis:
        errors.append("kpis must be a non-empty object")
        kpis = {}
    elif len(kpis) > _LIMITS["kpis"]:
        errors.append(f"more than {_LIMITS['kpis']} kpis")
    for key, k in kpis.items():
        where = f"kpis.{key}"
        if not _ID_RE.match(str(key)):
            errors.append(f"{where}: key must match ^[a-z][a-z0-9_]{{0,63}}$")
        if not isinstance(k, dict):
            errors.append(f"{where}: must be an object")
            continue
        for req in ("name", "definition", "aggregation"):
            if not isinstance(k.get(req), str) or not k[req].strip():
                errors.append(f"{where}.{req} is required")
        qf = k.get("question_forms")
        if qf is not None and (not isinstance(qf, list) or len(qf) > _LIMITS["question_forms"]
                               or not all(isinstance(x, str) for x in qf)):
            errors.append(f"{where}.question_forms must be a list of ≤{_LIMITS['question_forms']} strings")
        if k.get("target") is not None and not isinstance(k["target"], (int, float)):
            errors.append(f"{where}.target must be a number")
        owner = k.get("owner")
        if owner is not None and (not isinstance(owner, str) or not (0 < len(owner) <= _KPI_OWNER_MAX)):
            errors.append(f"{where}.owner must be a non-empty string of ≤{_KPI_OWNER_MAX} characters")
        approved_at = k.get("approved_at")
        if approved_at is not None and (not isinstance(approved_at, str)
                                         or not _APPROVED_AT_RE.match(approved_at)
                                         or not _valid_iso_date(approved_at)):
            errors.append(f"{where}.approved_at must be an ISO date (YYYY-MM-DD)")
        _check_sql(_resolve(k.get("cell_sql"), rules), f"{where}.cell_sql", errors)

    glossary = sem.get("glossary") or {}
    if not isinstance(glossary, dict) or len(glossary) > _LIMITS["glossary"]:
        errors.append(f"glossary must be an object of ≤{_LIMITS['glossary']} terms")
    else:
        for term, desc in glossary.items():
            if not isinstance(desc, str) or not (0 < len(desc) <= 500) or not (0 < len(str(term)) <= 120):
                errors.append(f"glossary[{term!r}] must be a non-empty string (≤500)")
    rules = sem.get("rules") or {}
    if not isinstance(rules, dict):
        errors.append("rules must be an object")
    return errors


_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def _resolve(sql, rules: dict):
    """`{key}` -> the rule's value, BEFORE any guard sees the SQL. Substituting
    after validation would let a rule value smuggle a second statement past
    every check (the text the guard approved is not the text that runs).
    Unknown keys stay literal and fail the dry run loudly."""
    if not isinstance(sql, str):
        return sql

    def rep(m):
        val = rules.get(m.group(1))
        if isinstance(val, bool) or not isinstance(val, (str, int, float)):
            return m.group(0)
        return str(val)
    return _PLACEHOLDER_RE.sub(rep, sql)


def normalize(sem: dict) -> dict:
    """One shape for readers: views are objects with `sql`; sources say `file`;
    columns carry an explicit `occurrence`; every `{placeholder}` in SQL is
    already resolved from `rules` — the frozen text IS the text that runs."""
    out = json.loads(json.dumps(sem))
    out.setdefault("version", 1)
    rules = out.get("rules") if isinstance(out.get("rules"), dict) else {}
    out["views"] = {
        name: ({**v, "sql": _resolve(v.get("sql"), rules)} if isinstance(v, dict)
               else {"sql": _resolve(v, rules)})
        for name, v in (out.get("views") or {}).items()
    }
    for k in (out.get("kpis") or {}).values():
        if isinstance(k, dict):
            k["cell_sql"] = _resolve(k.get("cell_sql"), rules)
    for t in out.get("tables", {}).values():
        for s in t.get("sources", []):
            if "file_pattern" in s and "file" not in s:
                s["file"] = s.pop("file_pattern")
        for c in t.get("columns", {}).values():
            if "source" in c:
                c.setdefault("occurrence", 0)
    return out


# ── dry run over empty tables ────────────────────────────────────────────
_DUCK_TYPES = {"text": "VARCHAR", "number": "DOUBLE", "datetime": "TIMESTAMP",
               "date": "DATE", "bool": "BOOLEAN"}


def _qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def dry_run(sem: dict, *, inplace: bool = False) -> list[str]:
    """Execute every view and KPI over EMPTY canonical tables (inline rows
    included) in a locked-down DuckDB. Catches unknown columns, broken SQL
    and a missing n/d|value in the result shape. Returns errors; raises
    SemanticsError only if DuckDB itself is unavailable — the caller decides
    whether that is fatal. With `inplace=True` the (already normalized) dict
    is enriched: each KPI gets `dims` and `measure` derived from its result
    columns."""
    try:
        import duckdb
    except ImportError as e:  # pragma: no cover - environment problem
        raise SemanticsError("duckdb is not installed; cannot dry-run semantics") from e
    if not inplace:
        sem = normalize(sem)
    errors: list[str] = []
    # Same posture as the worker's connection: no external access, a memory
    # ceiling and one thread — plus an interrupt timer per statement, so a
    # cross join over inline rows cannot pin a threadpool worker forever.
    con = duckdb.connect(database=":memory:", config={
        "enable_external_access": False, "memory_limit": "512MB", "threads": 1})

    def run(stmt: str):
        import threading
        timer = threading.Timer(DRY_RUN_TIMEOUT_S, con.interrupt)
        timer.daemon = True
        timer.start()
        try:
            return con.execute(stmt)
        finally:
            timer.cancel()

    def short(e) -> str:
        msg = str(e).splitlines()[0][:200] if str(e).strip() else "error"
        return f"timed out after {DRY_RUN_TIMEOUT_S:g}s" if "interrupt" in msg.lower() else msg

    try:
        for tname, t in sem["tables"].items():
            # The exact shape the worker materialises: declared columns with
            # their types, plus the two engine columns — nothing else, so a
            # cell_sql that leans on anything more fails HERE.
            cols = [f"{_qi(c)} {_DUCK_TYPES.get(spec.get('type'), 'VARCHAR')}"
                    for c, spec in t["columns"].items()]
            cols += ['"__partition" VARCHAR', '"__file" VARCHAR']
            con.execute(f"CREATE TABLE {_qi(tname)} ({', '.join(cols)})")
            for r in t.get("rows") or []:
                keys = [k for k in r if k in t["columns"]]
                if not keys:
                    continue
                con.execute(
                    f"INSERT INTO {_qi(tname)} ({', '.join(_qi(k) for k in keys)}) "
                    f"VALUES ({', '.join('?' for _ in keys)})", [r[k] for k in keys])
        for vname, v in sem["views"].items():
            try:
                run(f"CREATE VIEW {_qi(vname)} AS {v['sql']}")
                run(f"SELECT * FROM {_qi(vname)} LIMIT 0")
            except Exception as e:
                errors.append(f"views.{vname}: {short(e)}")
        for key, k in sem["kpis"].items():
            try:
                cur = run(k["cell_sql"])
                cols = [d[0] for d in (cur.description or [])]
                names = {c.lower() for c in cols}
            except Exception as e:
                errors.append(f"kpis.{key}.cell_sql: {short(e)}")
                continue
            if not ({"n", "d"} <= names or "value" in names):
                errors.append(f"kpis.{key}.cell_sql must return columns n and d, or value (got {sorted(names)})")
            # Every other result column is a dimension the intent layer may
            # group or filter by — recorded here so nobody has to parse SQL
            # again downstream. An explicit `dims` wins; otherwise columns
            # that are not the measures and do not look like extra measures.
            declared = k.get("dims")
            if isinstance(declared, list) and declared:
                unknown = [x for x in declared if x not in cols]
                if unknown:
                    errors.append(f"kpis.{key}.dims names columns the SQL does not return: {unknown}")
            else:
                k["dims"] = [c for c in cols if c.lower() not in _MEASURE_COLS
                             and not c.startswith("__")
                             and not c.lower().startswith("value_")
                             and not c.lower().endswith(("_lines", "_count", "_rows"))]
            k["measure"] = "ratio" if {"n", "d"} <= names else "value"
    finally:
        con.close()
    return errors


_MEASURE_COLS = {"n", "d", "value"}


# ── the schema a free-form question is answered over (tier 2) ────────────
# `/explore`'s second tier lets the model write ONE SELECT over this report's
# own semantic views when no KPI fits the question. To do that it needs the
# view's real column list — and a view is frozen as SQL, not as columns, so
# `SELECT o.*, …` only resolves once something creates it. `schema_outline`
# therefore builds the same empty typed tables `dry_run` does and asks DuckDB
# what each view returns. Empty tables: no row of data is read, produced or
# returned anywhere on this path, which is what lets the prompt built from
# this outline stay values-free by construction.
MAX_OUTLINE_COLUMNS = 80
_DESC_CHARS = 200


def _column_notes(sem: dict) -> dict[str, dict]:
    """Everything the bundle says about a column NAME, from wherever it was
    declared (a canonical table, or a view's `derived_columns`). Views inherit
    their inputs' columns verbatim, so a name declared once describes every
    view it flows into."""
    notes: dict[str, dict] = {}
    for t in (sem.get("tables") or {}).values():
        for cname, c in ((t or {}).get("columns") or {}).items():
            if isinstance(c, dict):
                notes.setdefault(str(cname), c)
    for v in (sem.get("views") or {}).values():
        if not isinstance(v, dict):
            continue
        for cname, c in (v.get("derived_columns") or {}).items():
            if isinstance(c, dict):
                notes[str(cname)] = c            # a derived column wins: it IS this view's
    return notes


def column_descriptions(sem: dict) -> dict[str, str]:
    """{column name: the author's Russian description} for every column the
    bundle declares anywhere — the vocabulary the intent prompt shows next
    to a KPI's dims so a model can map "warehouse" to `shipping_display` without
    guessing from the identifier. Prose only: nothing here is a cell value."""
    return {name: str(spec.get("description") or "").strip()
            for name, spec in _column_notes(sem).items()
            if isinstance(spec, dict) and str(spec.get("description") or "").strip()}


def _outline_column(name: str, typ: str, notes: dict) -> dict:
    spec = notes.get(name) or {}
    return {"name": name, "type": str(spec.get("type") or typ or "text"),
            "description": str(spec.get("description") or "")[:_DESC_CHARS],
            "role": spec.get("role")}


def schema_outline(sem: dict) -> dict:
    """`{"tables": {...}, "views": {...}}` — every declared table and view
    with its description and its real columns (name, type, Russian
    description). Never raises: a missing/failing duckdb degrades to the
    declared columns for tables and `derived_columns` for views, which is
    less complete but never wrong."""
    notes = _column_notes(sem)
    out: dict[str, dict] = {"tables": {}, "views": {}}
    for tname, t in (sem.get("tables") or {}).items():
        cols = [_outline_column(str(c), (spec or {}).get("type") or "text", notes)
                for c, spec in ((t or {}).get("columns") or {}).items()]
        out["tables"][str(tname)] = {
            "description": str((t or {}).get("description") or "")[:_DESC_CHARS],
            "columns": cols[:MAX_OUTLINE_COLUMNS]}
    for vname, v in (sem.get("views") or {}).items():
        spec = v if isinstance(v, dict) else {}
        out["views"][str(vname)] = {
            "description": str(spec.get("description") or "")[:_DESC_CHARS],
            "grain": str(spec.get("grain") or "")[:_DESC_CHARS],
            # Placeholder until the dry run below fills in the real list.
            "columns": [_outline_column(str(c), (s or {}).get("type") or "text", notes)
                        for c, s in (spec.get("derived_columns") or {}).items()][:MAX_OUTLINE_COLUMNS]}
    try:
        real = _view_columns(sem)
    except Exception:
        return out
    for vname, cols in real.items():
        if vname in out["views"]:
            out["views"][vname]["columns"] = [
                _outline_column(n, t, notes) for n, t in cols][:MAX_OUTLINE_COLUMNS]
    return out


def _view_columns(sem: dict) -> dict[str, list[tuple[str, str]]]:
    """{view: [(column, duckdb type), ...]} from a `LIMIT 0` over the empty
    typed tables — the same locked-down connection AND the same per-statement
    interrupt timer `dry_run` uses. Registration already proved every frozen
    view completes over empty tables, but this runs on a user request, so it
    must not be the one place a pathological view can pin a threadpool
    worker."""
    import duckdb
    import threading
    con = duckdb.connect(database=":memory:", config={
        "enable_external_access": False, "memory_limit": "512MB", "threads": 1})

    def run(stmt: str):
        timer = threading.Timer(DRY_RUN_TIMEOUT_S, con.interrupt)
        timer.daemon = True
        timer.start()
        try:
            return con.execute(stmt)
        finally:
            timer.cancel()

    out: dict[str, list[tuple[str, str]]] = {}
    try:
        for tname, t in (sem.get("tables") or {}).items():
            cols = [f"{_qi(c)} {_DUCK_TYPES.get((spec or {}).get('type'), 'VARCHAR')}"
                    for c, spec in ((t or {}).get("columns") or {}).items()]
            cols += ['"__partition" VARCHAR', '"__file" VARCHAR']
            con.execute(f"CREATE TABLE {_qi(tname)} ({', '.join(cols)})")
        for vname, v in (sem.get("views") or {}).items():
            sql = v.get("sql") if isinstance(v, dict) else v
            try:
                run(f"CREATE VIEW {_qi(vname)} AS {sql}")
                cur = run(f"SELECT * FROM {_qi(vname)} LIMIT 0")
                out[str(vname)] = [(d[0], str(d[1])) for d in (cur.description or [])]
            except Exception:
                continue
    finally:
        con.close()
    return out


# ── binding to catalog tables ────────────────────────────────────────────
def source_key(source: str, occurrence: int = 0) -> str | None:
    """The catalogue column key a source header lands on in the browser
    engine: `catalog.norm_col(header)`, and for the k-th duplicate of one
    header the `__{k+1}` suffix `_explore._frame_columns` appends. Engine
    pseudo-sources (`__source_file`) map to the engine's own columns."""
    if source.startswith("__"):
        return {"__source_file": "__file", "__file": "__file",
                "__partition": "__partition"}.get(source)
    from app.services.catalog import norm_col
    key = norm_col(source)
    if not key:
        return None
    return key if occurrence == 0 else f"{key}__{occurrence + 1}"


def _source_matches(src: dict, file_name: str, sheet: str | None) -> bool:
    import fnmatch
    pattern = src.get("file")
    if not pattern:
        return False
    if not fnmatch.fnmatchcase(file_name.casefold(), pattern.casefold()):
        return False
    want = src.get("sheet")
    if want in (None, "", "*"):
        return True
    return (sheet or "").casefold() == str(want).casefold()


def slot_table_name(sem_table: str) -> str:
    """The ENGINE table a converted slot registers as (ADR-14b addendum).
    Deliberately `slot__` (double underscore): `catalog.norm_col` collapses
    whitespace, so normalisation can never mint a catalogue table whose name
    collides with this prefix. Used by bind() and by the publish conversion
    (`lake_slots`) — one definition, no string drift."""
    return "slot__" + sem_table


def reference_sources(sem: dict) -> dict[str, dict]:
    """`{sem_table: {"slug", "key", "columns": {name: type}}}` for every table
    backed by a platform reference table. What both the registration-time
    schema check and the snapshot binder work from — one reading of the
    declaration, no re-walking of `sources` at either call site."""
    out: dict[str, dict] = {}
    for tname, t in ((sem or {}).get("tables") or {}).items():
        slug = reference_slug_of(t)
        if not slug:
            continue
        cols = t.get("columns") if isinstance(t.get("columns"), dict) else {}
        out[str(tname)] = {
            "slug": slug,
            "key": t.get("business_key") if isinstance(t.get("business_key"), str) else None,
            "columns": {str(c): (spec or {}).get("type") or "text"
                        for c, spec in cols.items() if isinstance(spec, dict)},
        }
    return out


def reference_schema_errors(sem: dict, schemas: dict[str, dict]) -> list[str]:
    """Semantics vs the reference tables that ALREADY exist, as a human diff.

    `schemas` is `{slug: {"key_column": str, "columns": {name: type}}}` and
    carries an entry ONLY for a table that exists — a slug missing from it is
    not an error: like a manifest's platform input, a report may be registered
    before anyone creates the dictionary it will read, and the binding simply
    appears the day the table is created. Where the table DOES exist the shape
    has to line up, because nothing later in the pipeline can repair it: an
    unknown column would answer NULL for every row, and a key that is not the
    table's key would join nothing.

    Comparison is case-insensitive on names (the reference API stores the
    author's spelling but compares folded, `validate_schema`), exact on types.
    A semantics that declares a SUBSET of the reference columns is fine, and
    so is one that declares a column the table does not have YET: the ADR-16
    addendum makes that an additive schema change (`reference.add_columns`),
    not a conflict. What cannot be repaired without touching stored rows —
    a changed TYPE, a moved KEY — is what this function reports."""
    errors: list[str] = []
    for tname, spec in reference_sources(sem).items():
        have = schemas.get(spec["slug"])
        if not have:
            continue
        by_lower = {str(n).casefold(): (str(n), ty)
                    for n, ty in (have.get("columns") or {}).items()}
        for cname, ctype in spec["columns"].items():
            found = by_lower.get(cname.casefold())
            if found is None:
                continue          # a new column: added, not refused
            if found[1] != ctype:
                errors.append(
                    f"tables.{tname}.columns.{cname}: type {ctype!r} does not match the type "
                    f"of column «{found[0]}» of reference table {spec['slug']} ({found[1]!r})")
        key_column = str(have.get("key_column") or "")
        if spec["key"] and key_column and spec["key"].casefold() != key_column.casefold():
            errors.append(
                f"tables.{tname}.business_key {spec['key']!r} is not the key of reference table "
                f"{spec['slug']} (the key is «{key_column}»)")
    return errors


def bind(sem: dict, catalog_tables: list[dict]) -> dict:
    """Which catalogue tables back each semantic table, and how their columns
    line up. `catalog_tables` is the /explore snapshot shape (`name`,
    `table_id`, `columns[{name}]`, `partitions[{file, sheet}]`). A semantic
    table binds to EVERY catalogue table whose partitions match one of its
    sources — the engine unions them by name, which is exactly how the
    generators merge a master workbook with the weekly exports. Tables with
    inline `rows` bind to nothing; the engine builds them from the rows.
    Returns {sem_table: {"matches": [{table, table_id, columns{canon: key|None}}]}}."""
    sem = normalize(sem)
    out: dict[str, dict] = {}
    for tname, t in sem["tables"].items():
        # A reference-backed table (ADR-16) never matches a catalogue table:
        # its rows come from the platform, exactly like an inline `rows`
        # block — the snapshot binder attaches them, the engine reads them.
        ref_slug = reference_slug_of(t)
        if ref_slug:
            out[tname] = {"inline": True, "reference": ref_slug, "matches": []}
            continue
        if t.get("rows") is not None:
            out[tname] = {"inline": True, "matches": []}
            continue
        matches = []
        for ct in catalog_tables:
            # A catalogue table is a CLUSTER of (file, sheet) units; only the
            # partitions a source pattern names may feed the semantic table,
            # so the match carries its file list and the engine filters on it.
            files = sorted({p.get("file") for p in (ct.get("partitions") or [])
                            if any(_source_matches(src, p.get("file") or "", p.get("sheet"))
                                   for src in t.get("sources", []))})
            if not files:
                continue
            have = {c.get("name") for c in (ct.get("columns") or [])}
            cols = {}
            for cname, c in t["columns"].items():
                if "source" not in c:
                    cols[cname] = None
                    continue
                key = source_key(c["source"], int(c.get("occurrence") or 0))
                cols[cname] = key if (key and (key in have or key.startswith("__"))) else None
            matches.append({"table": ct.get("name"), "table_id": ct.get("table_id"),
                            "columns": cols, "files": files})
        # Slot sources bind unconditionally to the synthetic table the publish
        # conversion registers (`slot__<name>`) — there is no catalogue row to
        # match. Columns come straight from the declaration; the engine
        # re-resolves them against the actual partition (DESCRIBE) anyway, so
        # a header the slot does not carry degrades into an honest
        # `unbound_columns` note, never a silent NULL.
        slot_files = sorted({str(s.get("slot")) for s in t.get("sources", [])
                             if isinstance(s, dict) and s.get("slot")})
        if slot_files:
            cols = {}
            for cname, c in t["columns"].items():
                # No `source` on a slot column = the file's header IS the
                # canonical name (a DB export writes canonical headers) —
                # default to it instead of None, or every column resolves
                # to NULL. A declared `source` still wins.
                if "source" not in c:
                    cols[cname] = cname
                    continue
                cols[cname] = source_key(c["source"], int(c.get("occurrence") or 0))
            matches.append({"table": slot_table_name(tname), "table_id": None,
                            "columns": cols, "files": slot_files, "slot": True})
        out[tname] = {"inline": False, "matches": matches}
    return out


def validate_full(sem, manifest_name: str | None = None) -> list[str]:
    if not isinstance(sem, dict):
        return ["semantics must be a JSON object"]
    sem_n = normalize(sem)
    errors = validate(sem_n, manifest_name)
    if errors:
        return errors
    return dry_run(sem_n, inplace=True)


def parse_bytes(data: bytes, manifest_name: str | None = None) -> dict:
    """bytes -> normalized semantics dict, or SemanticsError listing every
    problem at once (the author fixes the file in one pass)."""
    try:
        sem = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise SemanticsError(f"semantics.json is not valid JSON: {e}")
    # Normalize FIRST: placeholders are resolved before the guard and the
    # dry run ever look at the SQL, and the resolved text is what freezes.
    sem_n = normalize(sem) if isinstance(sem, dict) else sem
    errors = validate(sem_n, manifest_name)
    if not errors:
        errors = dry_run(sem_n, inplace=True)
    if errors:
        raise SemanticsError("semantics.json: " + "; ".join(errors[:20])
                             + (f" (+{len(errors) - 20} more)" if len(errors) > 20 else ""))
    return sem_n
