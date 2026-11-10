"""Declared vs observed: the seed of Data Quality.

`genmanifest.inputs_for()` says what a generator author DECLARED it reads —
a file pattern, which sheets, which columns. `source_files.schema_json` says
what the engine actually OBSERVED there (sheet names + header row, via
`static/py/_lineage.py::sniff`, calamine's sheet-level ceiling). `diff()`
compares the two and is a pure function on purpose: no DB session, no I/O,
so it can run at publish time (this run's files) or later over any pair of
{inputs, files} a caller assembles — the viewer's schema-diff partial and the
`/data-sources` drawer both want the same computation, not two.

Column comparison ignores everything a human would call "the same header
typed differently": NFC normalization (composed vs decomposed accents),
surrounding whitespace, and case.

Beyond that, a caller may pass `aliases` — per-source column renames an
analyst confirmed by hand ("SKU Description" is really "SKU_Discription" in
this branch's files). An aliased declared column counts as observed when its
observed spelling is present; it is reported separately as `aliased` so the
lineage view can mark it rather than pretend the header matched.
"""
from __future__ import annotations

import unicodedata

from app.services import globmatch


def norm(col: str) -> str:
    return unicodedata.normalize("NFC", col).strip().casefold()


# Reason/error texts that describe the ENGINE's own capacity, not the data:
# the observation batch cap forced a schema to be skipped or stripped, or the
# lineage probe itself did not load (gen-worker.js sends `{sheets: [],
# error: "..."}` for these instead of a NULL schema). Such a schema was never
# actually read this run, so it must not be scored as "sheet observed, empty"
# — that would report every declared column as missing. Shared with
# `quality._is_capacity_reason`, which applies the same check to per-sheet
# `reason` strings.
CAPACITY_REASON_MARKERS = ("budget", "batch cap", "stripped to fit", "probe unavailable")


def is_capacity_reason(reason: str) -> bool:
    r = reason.lower()
    return any(marker in r for marker in CAPACITY_REASON_MARKERS)


def _schema_is_capacity_limited(schema: dict) -> bool:
    """True when this file's schema is the engine's own stand-in for "did
    not read this run" (empty sheets + a capacity error), not a genuine
    empty/unreadable workbook."""
    err = schema.get("error")
    return (isinstance(err, str) and bool(err) and is_capacity_reason(err)
            and not schema.get("sheets"))


def _files_for_pattern(match: str, files: list[dict]) -> list[dict]:
    # matched_input is set by the engine's allow-list enumeration and is the
    # authoritative attribution; fall back to a fresh glob test only for
    # callers that assembled `files` without it (e.g. a hand-built test case).
    tagged = [f for f in files if f.get("matched_input") == match]
    if tagged:
        return tagged
    return [f for f in files if isinstance(f.get("name"), str) and globmatch.matches(match, f["name"])]


def _observed_columns(files: list[dict], sheet_pattern: str) -> tuple[dict[str, str], bool, bool]:
    """norm(header) -> header as written (first spelling wins), whether any
    matching sheet was seen at all, and whether every file that COULD have
    matched was capacity-limited this run (engine chose not to read it, not
    "workbook has no such sheet"). Comparison is on the normalised key; what
    a person reads is the spelling that is actually in the workbook."""
    observed: dict[str, str] = {}
    seen_sheet = False
    saw_any_file = False
    saw_real_file = False
    for f in files:
        schema = f.get("schema")
        if not isinstance(schema, dict):
            continue
        saw_any_file = True
        if _schema_is_capacity_limited(schema):
            continue
        saw_real_file = True
        for sh in (schema.get("sheets") or []):
            if not isinstance(sh, dict):
                continue
            name = sh.get("name") or ""
            if not globmatch.matches(sheet_pattern, name):
                continue
            seen_sheet = True
            for h in (sh.get("header") or []):
                if isinstance(h, str) and h.strip():
                    observed.setdefault(norm(h), h.strip())
    # capacity-limited only when we had files to look at but every one of
    # them was a capacity stand-in — an empty `files` list is genuinely
    # "no files matched", not "engine skipped reading them".
    capacity_only = saw_any_file and not saw_real_file
    return observed, seen_sheet, capacity_only


def _consumed_but_undeclared(inputs: list[dict], files: list[dict]) -> list[dict]:
    by_match = {it.get("match"): it for it in inputs if isinstance(it.get("match"), str)}
    out: list[dict] = []
    for f in files:
        consumed = f.get("consumed")
        if not isinstance(consumed, dict):
            continue
        decl_item = by_match.get(f.get("matched_input"))
        decl_sheets = (decl_item.get("sheets") or []) if decl_item else []
        for sheet_name, sh_consumed in (consumed.get("sheets") or {}).items():
            if not isinstance(sh_consumed, dict):
                continue
            cols_consumed = [c for c in (sh_consumed.get("columns") or []) if isinstance(c, str)]
            if not cols_consumed:
                continue
            declared_cols: set[str] = set()
            declared_sheet = False
            for sh_decl in decl_sheets:
                if not isinstance(sh_decl, dict):
                    continue
                if globmatch.matches(str(sh_decl.get("name") or "*"), sheet_name):
                    declared_sheet = True
                    declared_cols |= {norm(c) for c in (sh_decl.get("columns") or []) if isinstance(c, str)}
            if not declared_sheet:
                # No declaration at all for this sheet (or no pattern matched
                # the file) — every column it read is undeclared.
                undeclared = cols_consumed
            else:
                undeclared = [c for c in cols_consumed if norm(c) not in declared_cols]
            if undeclared:
                out.append({"file": f.get("name"), "sheet": sheet_name, "columns": undeclared})
    return out


def _severity(inputs: list[dict], unmatched: list[str], per_pattern: dict) -> str:
    required = {it["match"] for it in inputs if it.get("required") and isinstance(it.get("match"), str)}
    if required & set(unmatched):
        return "error"

    warn = False
    has_info = False
    for item in inputs:
        match = item.get("match")
        info = per_pattern.get(match)
        if not info:
            continue
        role = item.get("role", "data")
        for sh in info["sheets"].values():
            if sh["missing_columns"] and role == "data":
                warn = True
            if sh["extra_columns"]:
                has_info = True
    if warn:
        return "warn"
    return "info" if has_info else "ok"


def diff(inputs: list[dict], files: list[dict], aliases: dict | None = None) -> dict:
    """`inputs` is `genmanifest.inputs_for(manifest)` output. `files` is a
    list of {name, matched_input, schema, consumed} — the shape `source_files`
    rows collapse to once their JSON columns are parsed. `aliases` is
    `input_overrides.column_aliases`: {"<match>/<sheet>": {declared: observed}},
    keyed by the declared sheet glob as written in the manifest. A declared
    column whose alias target is observed (normalised comparison) is neither
    missing nor extra; it appears in per_pattern[match].sheets[sheet]["aliased"]
    under its declared spelling. Still pure — aliases are data, not a lookup."""
    aliases = aliases if isinstance(aliases, dict) else {}
    unmatched_patterns: list[str] = []
    per_pattern: dict[str, dict] = {}

    for item in inputs:
        match = item.get("match")
        if not isinstance(match, str):
            continue
        # ADR-16: a `source: "platform"` input is a workbook the SERVER
        # materialises for the run (`<slug>.xlsx`, from a reference table). It
        # is never in the analyst's folder and therefore never in a folder
        # observation, so diffing it against the observed files would report
        # "No files found matching pattern" on every single run of a perfectly
        # healthy generator. Its schema is frozen on the reference table and
        # validated at import, which is where that contract is enforced —
        # not here.
        if item.get("source") == "platform":
            continue
        matched_files = _files_for_pattern(match, files)
        if not matched_files:
            unmatched_patterns.append(match)
            continue

        sheets_out: dict[str, dict] = {}
        for sh_decl in (item.get("sheets") or []):
            if not isinstance(sh_decl, dict):
                continue
            decl = {}
            for c in (sh_decl.get("columns") or []):
                if isinstance(c, str) and c.strip():
                    decl.setdefault(norm(c), c.strip())
            if not decl:
                continue
            sheet_pattern = str(sh_decl.get("name") or "*")
            observed_cols, observed_any, capacity_only = _observed_columns(matched_files, sheet_pattern)
            # norm(declared) -> norm(observed target) for this match/sheet.
            alias_map: dict[str, str] = {}
            raw_aliases = aliases.get(f"{match}/{sheet_pattern}")
            if isinstance(raw_aliases, dict):
                for d, o in raw_aliases.items():
                    if isinstance(d, str) and isinstance(o, str) and d.strip() and o.strip():
                        alias_map[norm(d)] = norm(o)
            aliased = [decl[k] for k in decl
                       if k not in observed_cols and alias_map.get(k) in observed_cols]
            alias_targets = {alias_map[norm(a)] for a in aliased}
            # A capacity-limited sheet was never actually read this run — it
            # is "not observed" the same way a NULL schema is, not "observed
            # and empty". Reporting every declared column missing here would
            # be the engine's own skip decision mistaken for a broken file.
            sheets_out[sheet_pattern] = {
                # Declared spelling for what is missing (that is the name the
                # author wrote), workbook spelling for what is extra.
                "missing_columns": [decl[k] for k in decl
                                    if k not in observed_cols and decl[k] not in aliased]
                                   if not capacity_only else [],
                "extra_columns": sorted(v for k, v in observed_cols.items()
                                        if k not in decl and k not in alias_targets)
                                 if observed_any else [],
                "aliased": aliased,
                "observed": observed_any,
                "capacity": capacity_only,
            }

        per_pattern[match] = {"files": len(matched_files), "sheets": sheets_out}

    consumed_but_undeclared = _consumed_but_undeclared(inputs, files)
    severity = _severity(inputs, unmatched_patterns, per_pattern)

    return {
        "unmatched_patterns": unmatched_patterns,
        "per_pattern": per_pattern,
        "consumed_but_undeclared": consumed_but_undeclared,
        "severity": severity,
    }
