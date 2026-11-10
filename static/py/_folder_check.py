"""OnePage — "Check folder" (H2), browser side.

Deterministic, model-free data-quality checks over whatever `_explore.py`
has already indexed in THIS worker session — no generator run, no server
round trip, no LLM. Loaded next to `_explore.py` by `gen-worker.js`'s
`explore:check` handler and imported as a plain sibling module (same trick
as every other worker file: `import _explore` resolves because both land in
Pyodide's working directory).

Deliberately generic: nothing here knows the MEANING of any report's
columns — every check is a structural property of the indexed tables
themselves (`_explore._TABLES`, populated by `index_file`/`index_cached`):

  * **duplicate_key** — a column that behaves as a unique key WITHIN every
    single partition (distinct/total >= 0.98 per file) but whose values
    repeat ACROSS partitions once unioned — the honest generic signal for
    "these files overlap" without knowing what an "id" column is called.
  * **blank_rate** — a column whose blank rate exceeds 20% in some
    partition(s) while other partitions of the same table have it filled
    (<5% blank) — a column that silently broke in one file.
  * **type_conflict** — a column recorded with a numeric DuckDB type in some
    partitions and a non-numeric one in others (`_explore._TABLES[*][i]
    ["types"]`, the same per-partition type map `_rebuild_view` widens to
    VARCHAR when it disagrees) — the union already lost precision for it.
  * **small_partition** — a partition whose row count is under 30% of the
    table's median partition size — a likely truncated or partially loaded
    file.
  * **check_* (not_null/unique/range/regex/enum/known_values)** — whatever
    `checks` the page passes in, already resolved to an indexed catalogue
    `table` + `column` (the same five check semantics as
    `app.services.genmanifest.CHECK_TYPES` / Data Quality v1's
    `static/py/_lineage.py:run_checks`, evaluated here with SQL over the
    ALREADY-INDEXED union view instead of a row-by-row pass over one file).

Every finding is `{kind, severity, table, column, count, sample, message}`:
`sample` is at most 5 values, each cut to 64 chars — the same budget Data
Quality v1 uses for examples that may cross the network boundary, kept here
even though nothing here ever leaves the browser, because a finding is
rendered straight into the page's DOM.
"""
from __future__ import annotations

import _explore

_CHECK_TYPES = ("not_null", "unique", "range", "regex", "enum", "known_values")
_MAX_SAMPLE = 5
_MAX_SAMPLE_LEN = 64
_MAX_KEY_COLUMNS = 40   # cap the per-column per-partition uniqueness scan


def _cut(v, maxlen=_MAX_SAMPLE_LEN) -> str:
    s = "" if v is None else str(v)
    return s if len(s) <= maxlen else s[:maxlen - 1] + "…"


def _sample(values, cap=_MAX_SAMPLE) -> list:
    out = []
    for v in values:
        if v is None:
            continue
        out.append(_cut(v))
        if len(out) >= cap:
            break
    return out


def _finding(kind: str, severity: str, table: str, column, count: int,
            sample: list, message: str) -> dict:
    return {"kind": kind, "severity": severity, "table": table, "column": column,
            "count": int(count), "sample": sample, "message": message}


def _median(nums: list) -> float:
    s = sorted(nums)
    n = len(s)
    if not n:
        return 0.0
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _common_cols(parts: list, union: bool = False) -> set:
    """Column names present in every partition (union=False, the safe set
    to compare row-for-row) or in at least one (union=True, needed for the
    blank-rate scan where a column can legitimately be absent from an older
    partition — that absence is a schema question, not a blank-rate one)."""
    out = None
    for p in parts:
        cols = set(p.get("types") or {}) - {_explore.PARTITION_COL, _explore.FILE_COL}
        if out is None:
            out = cols
        elif union:
            out |= cols
        else:
            out &= cols
    return out or set()


# ── Structural checks (schema-agnostic, run over every indexed table) ──────

def _check_duplicate_keys(con, table: str, parts: list, findings: list) -> None:
    if len(parts) < 2:
        return
    common = sorted(_common_cols(parts))[:_MAX_KEY_COLUMNS]
    key_col = None
    for col in common:
        qcol = _explore._qi(col)
        ok = True
        for p in parts:
            row = con.execute(
                f"SELECT count(*), count(DISTINCT {qcol}) FROM main.{_explore._qi(p['rel'])}"
            ).fetchone()
            total, distinct = (row or (0, 0))
            if total and distinct < total * 0.98:
                ok = False
                break
        if ok:
            key_col = col
            break
    if key_col is None:
        return
    qcol, qtable = _explore._qi(key_col), _explore._qi(table)
    dup_groups = con.execute(
        f"SELECT count(*) FROM (SELECT {qcol} FROM main.{qtable} "
        f"WHERE {qcol} IS NOT NULL GROUP BY {qcol} HAVING count(*) > 1)"
    ).fetchone()[0]
    if not dup_groups:
        return
    sample_rows = con.execute(
        f"SELECT {qcol} FROM main.{qtable} WHERE {qcol} IS NOT NULL "
        f"GROUP BY {qcol} HAVING count(*) > 1 LIMIT {_MAX_SAMPLE}"
    ).fetchall()
    findings.append(_finding(
        "duplicate_key", "warn", table, key_col, dup_groups,
        _sample(r[0] for r in sample_rows),
        f"Column «{key_col}» is unique within each file, but "
        f"{dup_groups} of its values repeat across the partitions of table «{table}» — "
        "the files may overlap in period, or the folder may contain duplicates."))


def _check_blank_rates(con, table: str, parts: list, findings: list) -> None:
    if len(parts) < 2:
        return
    for col in sorted(_common_cols(parts, union=True)):
        qcol = _explore._qi(col)
        rates = []
        for p in parts:
            if col not in (p.get("types") or {}):
                continue
            row = con.execute(
                f"SELECT count(*), count(*) FILTER (WHERE {qcol} IS NULL "
                f"OR trim(CAST({qcol} AS VARCHAR)) = '') FROM main.{_explore._qi(p['rel'])}"
            ).fetchone()
            total, blanks = (row or (0, 0))
            if total:
                rates.append((p["partition"] or p["file"], blanks / total))
        if len(rates) < 2:
            continue
        filled = [r for _, r in rates if r < 0.05]
        bad = [(part, r) for part, r in rates if r > 0.20]
        if bad and filled:
            findings.append(_finding(
                "blank_rate", "warn", table, col, len(bad),
                _sample(f"{part}: {round(r * 100)}%" for part, r in bad),
                f"In column «{col}» of table «{table}» the share of empty values is above 20% "
                f"in {len(bad)} partition(s), although the same column is filled in the other partitions."))


def _check_type_conflicts(table: str, parts: list, findings: list) -> None:
    if len(parts) < 2:
        return
    by_col: dict = {}
    for p in parts:
        for col, typ in (p.get("types") or {}).items():
            if col in (_explore.PARTITION_COL, _explore.FILE_COL):
                continue
            by_col.setdefault(col, {})[p["partition"] or p["file"]] = str(typ).upper()
    for col in sorted(by_col):
        per_part = by_col[col]
        types_seen = set(per_part.values())
        if len(types_seen) < 2:
            continue
        numeric = {t for t in types_seen if _explore._is_numeric_type(t)}
        if numeric and (types_seen - numeric):
            findings.append(_finding(
                "type_conflict", "warn", table, col, len(types_seen),
                _sample(f"{part}: {typ}" for part, typ in per_part.items()),
                f"Column «{col}» of table «{table}» has different types in different partitions "
                f"({', '.join(sorted(types_seen))}) — when combined it will become text (VARCHAR)."))


def _check_small_partitions(table: str, parts: list, findings: list) -> None:
    if len(parts) < 2:
        return
    med = _median([p["rows"] for p in parts])
    if med <= 0:
        return
    small = [p for p in parts if p["rows"] < med * 0.3]
    if not small:
        return
    findings.append(_finding(
        "small_partition", "info", table, None, len(small),
        _sample(f"{p['partition'] or p['file']}: {p['rows']} rows" for p in small),
        f"In table «{table}» {len(small)} partition(s) hold less than 30% of the median "
        f"row count ({int(med)}) — a file may not have finished loading, or the period is incomplete."))


# ── Checks the page passes in (genmanifest.CHECK_TYPES shape) ──────────────

def _eval_supplied_checks(con, checks, findings: list) -> None:
    """`checks`: a list of dicts already resolved to an indexed catalogue
    table + column — `{"type", "table", "column", "min"?, "max"?,
    "pattern"?, "values"?, "label"?}`, the same five semantics
    `genmanifest.CHECK_TYPES` validates, minus `match`/`sheet` (those name a
    SOURCE FILE glob, meaningless once files are already unioned into one
    indexed table). A check whose table/column is not currently indexed, or
    that errors evaluating (bad regex, wrong column type), is skipped —
    same "malformed entries are skipped" discipline as
    `genmanifest.checks_for`; this function also runs over caller input this
    module never validated at registration."""
    for chk in (checks or []):
        if not isinstance(chk, dict):
            continue
        ctype = chk.get("type")
        table, column = chk.get("table"), chk.get("column")
        if ctype not in _CHECK_TYPES or not isinstance(table, str) or not isinstance(column, str):
            continue
        parts = _explore._TABLES.get(table)
        if not parts:
            continue
        cols = set()
        for p in parts:
            cols |= set(p.get("types") or {})
        if column not in cols:
            continue
        try:
            qtable = _explore._qi(_explore._check_table(table))
        except _explore.ExploreError:
            continue
        qcol = _explore._qi(column)
        label = str(chk.get("label") or column)[:64]
        try:
            _eval_one_supplied_check(con, ctype, qtable, qcol, table, column, label, chk, findings)
        except Exception:
            continue


def _eval_one_supplied_check(con, ctype, qtable, qcol, table, column, label, chk, findings) -> None:
    if ctype == "not_null":
        cnt = con.execute(
            f"SELECT count(*) FROM main.{qtable} WHERE {qcol} IS NULL "
            f"OR trim(CAST({qcol} AS VARCHAR)) = ''").fetchone()[0]
        if cnt:
            findings.append(_finding("check_not_null", "warn", table, column, cnt, [],
                f"«{label}»: {cnt} empty values in column «{column}» (not_null check)."))
        return
    if ctype == "unique":
        cnt = con.execute(
            f"SELECT count(*) FROM (SELECT {qcol} FROM main.{qtable} "
            f"WHERE {qcol} IS NOT NULL GROUP BY {qcol} HAVING count(*) > 1)").fetchone()[0]
        if cnt:
            rows = con.execute(
                f"SELECT {qcol} FROM main.{qtable} WHERE {qcol} IS NOT NULL "
                f"GROUP BY {qcol} HAVING count(*) > 1 LIMIT {_MAX_SAMPLE}").fetchall()
            findings.append(_finding("check_unique", "warn", table, column, cnt,
                _sample(r[0] for r in rows),
                f"«{label}»: {cnt} duplicate values in column «{column}» (unique check)."))
        return
    if ctype == "range":
        lo, hi = chk.get("min"), chk.get("max")
        conds, params = [], []
        if lo is not None:
            conds.append(f"TRY_CAST({qcol} AS DOUBLE) < ?")
            params.append(lo)
        if hi is not None:
            conds.append(f"TRY_CAST({qcol} AS DOUBLE) > ?")
            params.append(hi)
        if not conds:
            return
        where = " OR ".join(conds)
        cnt = con.execute(f"SELECT count(*) FROM main.{qtable} WHERE {where}", params).fetchone()[0]
        if cnt:
            rows = con.execute(f"SELECT {qcol} FROM main.{qtable} WHERE {where} LIMIT {_MAX_SAMPLE}",
                               params).fetchall()
            findings.append(_finding("check_range", "warn", table, column, cnt,
                _sample(r[0] for r in rows),
                f"«{label}»: {cnt} values out of range in column «{column}» (range check)."))
        return
    if ctype == "regex":
        pat = chk.get("pattern")
        if not isinstance(pat, str) or not pat:
            return
        where = (f"{qcol} IS NOT NULL AND NOT regexp_matches(CAST({qcol} AS VARCHAR), ?)")
        cnt = con.execute(f"SELECT count(*) FROM main.{qtable} WHERE {where}", [pat]).fetchone()[0]
        if cnt:
            rows = con.execute(f"SELECT {qcol} FROM main.{qtable} WHERE {where} LIMIT {_MAX_SAMPLE}",
                               [pat]).fetchall()
            findings.append(_finding("check_regex", "warn", table, column, cnt,
                _sample(r[0] for r in rows),
                f"«{label}»: {cnt} values do not match the pattern in column «{column}» (regex check)."))
        return
    if ctype in ("enum", "known_values"):
        values = chk.get("values")
        if not isinstance(values, list) or not values:
            return
        values = [str(v) for v in values[:500]]
        placeholders = ", ".join("?" for _ in values)
        where = f"{qcol} IS NOT NULL AND CAST({qcol} AS VARCHAR) NOT IN ({placeholders})"
        cnt = con.execute(f"SELECT count(*) FROM main.{qtable} WHERE {where}", values).fetchone()[0]
        if cnt:
            rows = con.execute(
                f"SELECT DISTINCT {qcol} FROM main.{qtable} WHERE {where} LIMIT {_MAX_SAMPLE}",
                values).fetchall()
            findings.append(_finding(f"check_{ctype}", "warn", table, column, cnt,
                _sample(r[0] for r in rows),
                f"«{label}»: {cnt} values outside the allowed list in column «{column}» ({ctype} check)."))
        return


# ── Entry point ──────────────────────────────────────────────────────────

def run(checks=None) -> list:
    """Every finding, over every table `_explore` currently has indexed,
    plus whatever `checks` the page supplied. Called fresh each time —
    findings are never cached, the index they read from can change under
    the button (re-index, new folder)."""
    con = _explore._conn()
    findings: list = []
    for table in sorted(_explore._TABLES):
        parts = _explore._TABLES.get(table) or []
        _check_duplicate_keys(con, table, parts, findings)
        _check_blank_rates(con, table, parts, findings)
        _check_type_conflicts(table, parts, findings)
        _check_small_partitions(table, parts, findings)
    _eval_supplied_checks(con, checks, findings)
    return findings
