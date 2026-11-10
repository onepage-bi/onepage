"""Generator manifest validation + wasm32 dependency resolution.

manifest.json is validated on the SERVER at registration time (RFC
generators E.1) — a bad manifest must stop the admin who registers it, not
the analyst who later launches a run.
"""
from __future__ import annotations

import json
import re

# rls (ADR-18): the policy validator is owned by app/services/rls.py — one
# implementation, imported like any other cross-service dependency. A second
# copy here (there used to be a "fallback" one) drifted apart from it and
# accepted exactly the typos the strict validator exists to catch.
from app.i18n import _
from app.services.rls import validate_policy as validate_rls_policy
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants — browser-engine ceilings from the measured engine border
# (docs/RFC_GENERATORS_RESEARCH.md, Appendix C.6)
# ---------------------------------------------------------------------------

# max_files raised 30 -> 100 -> 500 by owner decisions (2026-07-20/21, the
# second when a real pilot folder reached 106 files): the binding constraints
# of the measured border are the BYTE numbers below — the per-file engine's
# cost scales with bytes, not count, so many small files are fine. The count
# number's only real price is the folder walk (~63 ms/file on SMB ≈ 30 s at
# 500). The byte numbers stay as measured.
#
# These are RECOMMENDATIONS, not ceilings (owner decision 2026-08-20). They are
# still the defaults a manifest inherits when it omits data_scale, and the
# browser UI still shows a folder that exceeds them — but nothing rejects a
# manifest for declaring more, and nothing stops the analyst from running past
# them. Past these numbers the Pyodide worker may exhaust the tab's memory, so
# the warning stays; the decision moved to the person clicking Run.
REC_MAX_FILES = 500
REC_MAX_TOTAL_BYTES = 500 * 1024 * 1024
REC_MAX_FILE_BYTES = 35 * 1024 * 1024

ENGINES = ("browser", "agent", "auto")
PARAM_TYPES = ("string", "number", "boolean", "choice", "date")

WASM_BUNDLED = "bundled"
WASM_PYPI_WHEEL = "pypi-wheel"
WASM_PYPI_PURE = "pypi-pure"
WASM_UNAVAILABLE = "unavailable"
WASM_UNKNOWN = "unknown"

_TOP_LEVEL_KEYS = {
    "name", "version", "entrypoint", "params", "deps", "outputs",
    "data_scale", "engine", "write_back", "aux_inputs", "required_files",
    "sources", "inputs", "checks", "lake", "subfolders", "rls",
}

# Data-quality checks the browser engine evaluates per matched file (plan
# "Data Quality", P0.1). `known_values` carries no values on purpose: the
# engine only reports the distinct set and the SERVER compares it to a
# baseline it seeded itself — baselines never live in the manifest, so a
# generator re-registration cannot silently reset what "known" means.
# `ref_lookup` carries no values either: the engine is handed the CURRENT
# normalised keys of the platform reference table named by `table` and reports
# what did not map (`missing`), so a manifest can never freeze a key set.
CHECK_TYPES = ("not_null", "unique", "range", "regex", "enum", "known_values",
               "ref_lookup")
_CHECK_KEYS = {"match", "sheet", "column", "type", "min", "max", "pattern",
               "values", "on_fail", "label", "table"}
_MAX_CHECKS = 64
_MAX_ENUM_VALUES = 500
_MAX_VALUE_LEN = 64
_MAX_REGEX_LEN = 200
# Engine-side ceilings, exported so _lineage.py / observations.py quote the
# same numbers: distinct values kept per known_values check, and rows scanned
# per sheet before the result is flagged `sampled` (keeps a 35 MB workbook
# inside the 20 s per-run check budget).
MAX_KNOWN_VALUES = 500
MAX_CHECK_ROWS = 200_000

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_PARAM_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_DEP_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")
# Same charset genstorage.save_run_aux enforces on disk — kept in sync by hand
# (two small modules, not worth a shared import for one regex).
_AUX_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")
_MAX_AUX_INPUTS = 3
_MAX_REQUIRED_FILES = 8

# inputs: what the generator reads, declared by its author — file patterns,
# and inside them the sheets and columns the code depends on. This is the
# allow-list the engine applies to a folder (when present), the thing lineage
# draws, and the baseline the observed workbook structure is diffed against.
INPUT_ROLES = ("data", "reference")
_MAX_INPUTS = 32
_MAX_INPUT_SHEETS = 16
_MAX_INPUT_COLUMNS = 256
_MAX_COLUMN_LEN = 64
_INPUT_KEYS = {"match", "role", "required", "sheets", "source", "table"}
_INPUT_SHEET_KEYS = {"name", "columns"}
# A column entry may be a bare string, or {name, description, type} — the only
# extension of the column form (P3.6, ADR-16). Keeping the object shape a strict
# subset of the annotation row lets the same length ceiling serve both.
_INPUT_COLUMN_KEYS = {"name", "description", "type"}
_MAX_COLUMN_DESCRIPTION_LEN = 500

# Where an input comes from: the analyst's folder (every input before ADR-16),
# or the platform's reference tables ("Reference tables"). A platform input is not a
# file the analyst picks — the engine fetches `<table>.xlsx` from the server and
# hands it to the generator like any other workbook, so `match` must name that
# exact file and the schema the generator reads is declared here, typed, and
# frozen with the version.
INPUT_SOURCES = ("folder", "platform")
PLATFORM_SHEET = "data"
COLUMN_TYPES = ("text", "number", "date")
# Mirrors of app/services/reference.py validate_schema — the manifest declares
# the schema the platform will freeze for the table, so a manifest that could
# never satisfy it must fail HERE, at registration, not later with a 400 from
# the reference API the admin never sees. (app.services.reference imports
# SQLAlchemy; this module is deliberately dependency-free, so the two numbers
# are stated twice and pinned by tests/smoke_generators.py.)
_MIN_PLATFORM_COLUMNS = 2       # a key plus at least one mapped value
_MAX_PLATFORM_COLUMNS = 32
PLATFORM_KEY_TYPE = "text"      # a numeric/date key re-opens leading zeros

# sources (agent-engine GreenPlum executor). Frozen into the immutable version
# like every other field; the agent reads it to run the frozen SQL under the
# analyst's personal credential. The browser engine can never reach a database,
# so a manifest with `sources` and engine="browser" is rejected (browser_blockers).
SOURCE_TYPES = ("greenplum",)
SOURCE_FORMATS = ("csv_gz",)
_MAX_QUERIES = 16
_MAX_PARALLELISM = 4                              # plan ceiling: 2 cores -> <=2 gzip at once
_MAX_STATEMENT_TIMEOUT_MS = 24 * 60 * 60 * 1000   # 24h — a typo guard, not a policy
_SOURCES_KEYS = {"type", "connection", "parallelism",
                 "statement_timeout_ms", "freeze_cycle", "queries"}
_SOURCE_QUERY_KEYS = {"sql_file", "output_name", "format", "limit_declared"}
# A bundle-relative *.sql path: forward slashes only, no traversal, ends .sql.
_SQL_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]*\.sql$")

# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------

def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate(manifest: dict) -> list[str]:
    """Structural check of a generator manifest. Returns a list of
    human-readable errors ([] means valid). Unknown top-level keys are an
    error on purpose — the manifest is a contract, and a typo in it should
    be visible, not silently ignored."""
    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]

    errors: list[str] = []

    for key in sorted(set(manifest) - _TOP_LEVEL_KEYS):
        errors.append(f"unknown field: {key!r}")

    # name --------------------------------------------------------------
    name = manifest.get("name")
    if name is None:
        errors.append("name is required")
    elif not isinstance(name, str) or not _NAME_RE.match(name):
        errors.append("name must match ^[a-z0-9][a-z0-9_-]{0,63}$")

    # version (author label — the platform numbers version_number itself)
    version = manifest.get("version")
    if version is None:
        errors.append("version is required")
    elif not isinstance(version, str) or not (1 <= len(version) <= 64):
        errors.append("version must be a string of 1..64 characters")

    # entrypoint ----------------------------------------------------------
    entrypoint = manifest.get("entrypoint")
    if entrypoint is None:
        errors.append("entrypoint is required")
    elif not isinstance(entrypoint, dict):
        errors.append("entrypoint must be an object")
    else:
        module = entrypoint.get("module")
        if module is None:
            errors.append("entrypoint.module is required")
        elif not isinstance(module, str) or not _MODULE_RE.match(module):
            errors.append(
                "entrypoint.module must be a dotted Python module path"
            )

    # params ----------------------------------------------------------------
    params = manifest.get("params")
    if params is not None:
        if not isinstance(params, list):
            errors.append("params must be a list")
        else:
            seen_keys: set[str] = set()
            for i, p in enumerate(params):
                if not isinstance(p, dict):
                    errors.append(f"params[{i}] must be an object")
                    continue

                key = p.get("key")
                if key is None:
                    errors.append(f"params[{i}].key is required")
                elif not isinstance(key, str) or not _PARAM_KEY_RE.match(key):
                    errors.append(
                        f"params[{i}].key must match ^[a-z0-9_]{{1,64}}$"
                    )
                elif key in seen_keys:
                    errors.append(f"params[{i}].key is a duplicate: {key!r}")
                else:
                    seen_keys.add(key)

                ptype = p.get("type")
                if ptype is None:
                    errors.append(f"params[{i}].type is required")
                elif ptype not in PARAM_TYPES:
                    errors.append(
                        f"params[{i}].type must be one of {PARAM_TYPES}"
                    )
                elif ptype == "choice":
                    choices = p.get("choices")
                    if not isinstance(choices, list) or not choices:
                        errors.append(
                            f"params[{i}].choices is required and must be a "
                            f"non-empty list when type is 'choice'"
                        )

                if "label" in p and p["label"] is not None and not isinstance(p["label"], str):
                    errors.append(f"params[{i}].label must be a string")
                if "required" in p and not isinstance(p["required"], bool):
                    errors.append(f"params[{i}].required must be a boolean")

    # deps --------------------------------------------------------------------
    deps = manifest.get("deps")
    if deps is not None:
        if not isinstance(deps, list):
            errors.append("deps must be a list")
        else:
            for i, d in enumerate(deps):
                if not isinstance(d, dict):
                    errors.append(f"deps[{i}] must be an object")
                    continue

                dname = d.get("name")
                if dname is None:
                    errors.append(f"deps[{i}].name is required")
                elif not isinstance(dname, str) or not _DEP_NAME_RE.match(dname):
                    errors.append(
                        f"deps[{i}].name must be a valid package name"
                    )

                dversion = d.get("version")
                if dversion is None:
                    errors.append(f"deps[{i}].version is required")
                elif not isinstance(dversion, str):
                    errors.append(f"deps[{i}].version must be a string")

    # outputs -------------------------------------------------------------------
    outputs = manifest.get("outputs")
    if outputs is None:
        errors.append("outputs is required")
    elif not isinstance(outputs, dict):
        errors.append("outputs must be an object")
    else:
        html = outputs.get("html")
        if html is None:
            errors.append("outputs.html is required")
        elif not isinstance(html, bool):
            errors.append("outputs.html must be a boolean")

        slots = outputs.get("slots")
        if slots is not None:
            if not isinstance(slots, list):
                errors.append("outputs.slots must be a list")
            else:
                seen_idx: set[int] = set()
                for i, s in enumerate(slots):
                    if not isinstance(s, dict):
                        errors.append(f"outputs.slots[{i}] must be an object")
                        continue

                    idx = s.get("index")
                    if idx is None:
                        errors.append(f"outputs.slots[{i}].index is required")
                    elif not _is_int(idx) or idx < 0:
                        errors.append(
                            f"outputs.slots[{i}].index must be an integer >= 0"
                        )
                    elif idx in seen_idx:
                        errors.append(
                            f"outputs.slots[{i}].index is a duplicate: {idx}"
                        )
                    else:
                        seen_idx.add(idx)

                    sname = s.get("name")
                    if sname is None:
                        errors.append(f"outputs.slots[{i}].name is required")
                    elif not isinstance(sname, str):
                        errors.append(f"outputs.slots[{i}].name must be a string")

                    if "type" in s and s["type"] is not None and not isinstance(s["type"], str):
                        errors.append(f"outputs.slots[{i}].type must be a string")
                    if "write_back" in s and not isinstance(s["write_back"], bool):
                        errors.append(
                            f"outputs.slots[{i}].write_back must be a boolean"
                        )

    # aux_inputs ------------------------------------------------------------------
    # Platform-generic "auxiliary input file" mechanism (pilot: an analyst's
    # filled reasons workbook consumed by a special second run of the same
    # generator). Optional; frozen into the immutable version like everything
    # else here.
    aux_inputs = manifest.get("aux_inputs")
    if aux_inputs is not None:
        if not isinstance(aux_inputs, list):
            errors.append("aux_inputs must be a list")
        elif len(aux_inputs) > _MAX_AUX_INPUTS:
            errors.append(f"aux_inputs must have at most {_MAX_AUX_INPUTS} entries")
        else:
            # download_from must name a slot the generator itself writes back —
            # otherwise there is nothing for the platform to hand the analyst.
            writeback_slots: set[str] = set()
            if isinstance(outputs, dict) and isinstance(outputs.get("slots"), list):
                for s in outputs["slots"]:
                    if (isinstance(s, dict) and s.get("write_back") is True
                            and isinstance(s.get("name"), str)):
                        writeback_slots.add(s["name"])

            seen_aux_names: set[str] = set()
            for i, a in enumerate(aux_inputs):
                if not isinstance(a, dict):
                    errors.append(f"aux_inputs[{i}] must be an object")
                    continue

                aname = a.get("name")
                if aname is None:
                    errors.append(f"aux_inputs[{i}].name is required")
                elif (not isinstance(aname, str) or not _AUX_NAME_RE.match(aname)
                        or ".." in aname):
                    errors.append(
                        f"aux_inputs[{i}].name must match "
                        f"^[A-Za-z0-9][A-Za-z0-9._ -]{{0,127}}$ and must not contain '..'"
                    )
                elif aname in seen_aux_names:
                    errors.append(f"aux_inputs[{i}].name is a duplicate: {aname!r}")
                else:
                    seen_aux_names.add(aname)

                label = a.get("label")
                if label is None:
                    errors.append(f"aux_inputs[{i}].label is required")
                elif not isinstance(label, str) or not (1 <= len(label) <= 64):
                    errors.append(
                        f"aux_inputs[{i}].label must be a string of 1..64 characters"
                    )

                download_from = a.get("download_from")
                if download_from is not None:
                    if not isinstance(download_from, str):
                        errors.append(f"aux_inputs[{i}].download_from must be a string")
                    elif download_from not in writeback_slots:
                        errors.append(
                            f"aux_inputs[{i}].download_from must equal the name of an "
                            f"outputs.slots entry with write_back: true"
                        )

    # required_files ----------------------------------------------------------------
    # Names the browser engine must find in the selected input folders before a
    # run may start. Checked pre-run (C.6 border, same pass as the junk
    # filters), so a missing supporting workbook is a diagnosis card, not a
    # silent fallback inside the generator. Distinct from aux_inputs: these are
    # ordinary main-folder files, not a separate analyst upload with its own
    # FSA handle.
    required_files = manifest.get("required_files")
    if required_files is not None:
        if not isinstance(required_files, list):
            errors.append("required_files must be a list")
        elif len(required_files) > _MAX_REQUIRED_FILES:
            errors.append(
                f"required_files must have at most {_MAX_REQUIRED_FILES} entries"
            )
        else:
            aux_names: set[str] = set()
            if isinstance(aux_inputs, list):
                for a in aux_inputs:
                    if isinstance(a, dict) and isinstance(a.get("name"), str):
                        aux_names.add(a["name"])

            seen_required_names: set[str] = set()
            for i, name in enumerate(required_files):
                if not isinstance(name, str) or not _AUX_NAME_RE.match(name) or ".." in name:
                    errors.append(
                        f"required_files[{i}] must match "
                        f"^[A-Za-z0-9][A-Za-z0-9._ -]{{0,127}}$ and must not contain '..'"
                    )
                elif name in seen_required_names:
                    errors.append(f"required_files[{i}] is a duplicate: {name!r}")
                elif name in aux_names:
                    errors.append(
                        f"required_files[{i}] duplicates an aux_inputs name: {name!r}"
                    )
                else:
                    seen_required_names.add(name)

    # inputs --------------------------------------------------------------------------
    # Declared reads. Validated structurally here; the pattern grammar is the
    # engine's own (globmatch), so a pattern the server accepts is one the
    # engine will evaluate identically. A required_files name that is also
    # admitted by an inputs[].match of a different role is a contradiction the
    # author should resolve, not one the platform should guess at.
    inputs = manifest.get("inputs")
    if inputs is not None:
        errors.extend(_validate_inputs(inputs, required_files))

    # checks — validated against the declared inputs (a check on a column
    # nobody declared can never run), so they are checked right after inputs.
    checks = manifest.get("checks")
    if checks is not None:
        errors.extend(_validate_checks(checks, inputs))

    # sources ---------------------------------------------------------------------
    # Agent-engine GreenPlum executor: frozen SQL files + their csv.gz outputs.
    # Every field is frozen into the immutable version — the analyst never edits
    # the SQL locally (anti-drift). Unknown sub-keys are errors, same discipline
    # as the top level. Cross-engine sanity (browser can't reach a DB) is enforced
    # separately in browser_blockers, which knows the resolved engine.
    sources = manifest.get("sources")
    if sources is not None:
        if not isinstance(sources, dict):
            errors.append("sources must be an object")
        else:
            for key in sorted(set(sources) - _SOURCES_KEYS):
                errors.append(f"unknown sources field: {key!r}")

            stype = sources.get("type")
            if stype is None:
                errors.append("sources.type is required")
            elif stype not in SOURCE_TYPES:
                errors.append(f"sources.type must be one of {SOURCE_TYPES}")

            connection = sources.get("connection")
            if connection is None:
                errors.append("sources.connection is required")
            elif not isinstance(connection, str) or not _NAME_RE.match(connection):
                errors.append(
                    "sources.connection must match ^[a-z0-9][a-z0-9_-]{0,63}$"
                )

            parallelism = sources.get("parallelism")
            if parallelism is not None and (
                    not _is_int(parallelism) or not (1 <= parallelism <= _MAX_PARALLELISM)):
                errors.append(
                    f"sources.parallelism must be an integer in 1..{_MAX_PARALLELISM}"
                )

            timeout = sources.get("statement_timeout_ms")
            if timeout is not None and (
                    not _is_int(timeout) or not (1 <= timeout <= _MAX_STATEMENT_TIMEOUT_MS)):
                errors.append(
                    f"sources.statement_timeout_ms must be an integer in "
                    f"1..{_MAX_STATEMENT_TIMEOUT_MS}"
                )

            if "freeze_cycle" in sources and not isinstance(sources["freeze_cycle"], bool):
                errors.append("sources.freeze_cycle must be a boolean")

            queries = sources.get("queries")
            if queries is None:
                errors.append("sources.queries is required")
            elif not isinstance(queries, list) or not queries:
                errors.append("sources.queries must be a non-empty list")
            elif len(queries) > _MAX_QUERIES:
                errors.append(f"sources.queries must have at most {_MAX_QUERIES} entries")
            else:
                seen_sql: set[str] = set()
                seen_out: set[str] = set()
                for i, q in enumerate(queries):
                    if not isinstance(q, dict):
                        errors.append(f"sources.queries[{i}] must be an object")
                        continue
                    for key in sorted(set(q) - _SOURCE_QUERY_KEYS):
                        errors.append(f"sources.queries[{i}] has unknown field: {key!r}")

                    sql_file = q.get("sql_file")
                    if sql_file is None:
                        errors.append(f"sources.queries[{i}].sql_file is required")
                    elif (not isinstance(sql_file, str) or ".." in sql_file
                            or not _SQL_FILE_RE.match(sql_file)):
                        errors.append(
                            f"sources.queries[{i}].sql_file must be a bundle-relative "
                            f"'*.sql' path (forward slashes, no '..')"
                        )
                    elif sql_file in seen_sql:
                        errors.append(
                            f"sources.queries[{i}].sql_file is a duplicate: {sql_file!r}"
                        )
                    else:
                        seen_sql.add(sql_file)

                    output_name = q.get("output_name")
                    if output_name is None:
                        errors.append(f"sources.queries[{i}].output_name is required")
                    elif (not isinstance(output_name, str)
                            or not _AUX_NAME_RE.match(output_name) or ".." in output_name):
                        errors.append(
                            f"sources.queries[{i}].output_name must match "
                            f"^[A-Za-z0-9][A-Za-z0-9._ -]{{0,127}}$ and must not contain '..'"
                        )
                    elif output_name in seen_out:
                        errors.append(
                            f"sources.queries[{i}].output_name is a duplicate: {output_name!r}"
                        )
                    else:
                        seen_out.add(output_name)

                    fmt = q.get("format")
                    if fmt is None:
                        errors.append(f"sources.queries[{i}].format is required")
                    elif fmt not in SOURCE_FORMATS:
                        errors.append(
                            f"sources.queries[{i}].format must be one of {SOURCE_FORMATS}"
                        )

                    limit_declared = q.get("limit_declared")
                    if limit_declared is not None and (
                            not _is_int(limit_declared) or limit_declared < 1):
                        errors.append(
                            f"sources.queries[{i}].limit_declared must be an integer >= 1"
                        )

    # data_scale ------------------------------------------------------------------
    data_scale = manifest.get("data_scale")
    if data_scale is not None:
        if not isinstance(data_scale, dict):
            errors.append("data_scale must be an object")
        else:
            for k in ("max_files", "max_total_bytes", "max_file_bytes"):
                if k not in data_scale:
                    continue
                v = data_scale[k]
                if not _is_int(v) or v < 1:
                    errors.append(f"data_scale.{k} must be an integer >= 1")

    # engine / write_back -----------------------------------------------------------
    engine = manifest.get("engine")
    if engine is not None and engine not in ENGINES:
        errors.append(f"engine must be one of {ENGINES}")

    write_back = manifest.get("write_back")
    if write_back is not None and not isinstance(write_back, bool):
        errors.append("write_back must be a boolean")

    # lake (phase 9.3): the author's opt-out from publishing the report's
    # partition index to the server. Absent/true = publish (only generators
    # with semantics.json do anything at all); false = never.
    lake = manifest.get("lake")
    if lake is not None and not isinstance(lake, bool):
        errors.append("lake must be a boolean")
    # subfolders: how many levels below each chosen folder the browser engine
    # walks (0 = flat, the default). A file from a subfolder is presented to
    # the generator as `<sub>__<name>.xlsx` — flat for the generator's own
    # globbing, unique across subfolders, and the same name everywhere else
    # (lineage, cache keys, the lake manifest).
    sub = manifest.get("subfolders")
    if sub is not None and (isinstance(sub, bool) or not isinstance(sub, int) or not 0 <= sub <= 4):
        errors.append("subfolders must be an integer 0..4")

    # rls (ADR-18): row-level security policy for interactive slot files.
    # Structural validation is owned by app/services/rls.py (validate_policy).
    rls = manifest.get("rls")
    if rls is not None:
        errors.extend(f"rls: {e}" for e in validate_rls_policy(rls))

        # Cross-check with outputs.slots (only when outputs.slots is itself
        # declared as a list — outputs is required, but a malformed outputs
        # already carries its own errors above). Every index the policy
        # names must exist as a real output slot, and every output slot must
        # be covered (column, via, or passthrough) — an uncovered slot file
        # would otherwise 403 at serve time for every non-see_all viewer.
        if isinstance(outputs, dict) and isinstance(outputs.get("slots"), list):
            output_idx = {
                s["index"] for s in outputs["slots"]
                if isinstance(s, dict) and _is_int(s.get("index"))
            }
            rls_idx: set[int] = set()
            if isinstance(rls, dict):
                for s in rls.get("slots") or []:
                    if isinstance(s, dict) and _is_int(s.get("index")):
                        rls_idx.add(s["index"])
                for idx in rls.get("passthrough") or []:
                    if _is_int(idx):
                        rls_idx.add(idx)

            for idx in sorted(rls_idx - output_idx):
                errors.append(
                    f"rls: index {idx} is not declared in outputs.slots"
                )
            for idx in sorted(output_idx - rls_idx):
                errors.append(
                    f"rls: outputs.slots[{idx}] is not covered by the "
                    f"policy (slots or passthrough)"
                )

    return errors


def _validate_platform_input(item, i, match, role, table, required_files) -> list[str]:
    """The extra contract a `source: "platform"` input carries (ADR-16).

    The file the engine materialises for it is generated by the server, not
    picked by the analyst: a fixed layout (one sheet `data`, header in row 1,
    the key column first) under the exact name `<table>.xlsx`. So `match` may
    not be a glob here — it must BE that name, spelled exactly, or the engine's
    attribution (`matched_input`, compared with ===) silently misses the file
    the server just handed it. The column types are the schema the platform
    freezes for the table, which is why every column must carry one."""
    errors: list[str] = []
    where = f"inputs[{i}]"
    if not isinstance(table, str) or not _NAME_RE.match(table):
        errors.append(f"{where}.table must match ^[a-z0-9][a-z0-9_-]{{0,63}}$")
        table = None
    if table is not None:
        want = f"{table}.xlsx"
        if match != want:
            errors.append(
                f'{where}.match must equal {want!r} for a platform input '
                f'(no glob, case-sensitive)')
        elif isinstance(required_files, list) and any(
                isinstance(n, str) and n == want for n in required_files):
            errors.append(
                f"{where}: a platform input may not also be listed in required_files")
    if role != "reference":
        errors.append(f'{where}.role must be "reference" for a platform input')

    sheets = item.get("sheets")
    if not isinstance(sheets, list) or len(sheets) != 1:
        errors.append(
            f'{where}.sheets must declare exactly one sheet named "{PLATFORM_SHEET}" '
            f"for a platform input")
        return errors
    sh = sheets[0]
    if not isinstance(sh, dict) or sh.get("name") != PLATFORM_SHEET:
        errors.append(
            f'{where}.sheets[0].name must be "{PLATFORM_SHEET}" for a platform input')
        return errors
    cols = sh.get("columns")
    if not isinstance(cols, list) or len(cols) < _MIN_PLATFORM_COLUMNS:
        errors.append(
            f"{where}.sheets[0].columns must declare at least "
            f"{_MIN_PLATFORM_COLUMNS} columns for a platform input")
        return errors
    if len(cols) > _MAX_PLATFORM_COLUMNS:
        errors.append(
            f"{where}.sheets[0].columns must have at most {_MAX_PLATFORM_COLUMNS} "
            f"columns for a platform input")
    for k, col in enumerate(cols):
        if not isinstance(col, dict) or col.get("type") not in COLUMN_TYPES:
            errors.append(
                f"{where}.sheets[0].columns[{k}] must be an object with a type "
                f"({', '.join(COLUMN_TYPES)}) for a platform input")
    first = cols[0]
    if isinstance(first, dict) and first.get("type") in COLUMN_TYPES \
            and first.get("type") != PLATFORM_KEY_TYPE:
        errors.append(
            f"{where}.sheets[0].columns[0] is the key column and must have type "
            f'"{PLATFORM_KEY_TYPE}"')
    return errors


def _validate_inputs(inputs, required_files) -> list[str]:
    from app.services import globmatch

    errors: list[str] = []
    if not isinstance(inputs, list):
        return ["inputs must be a list"]
    if len(inputs) > _MAX_INPUTS:
        return [f"inputs must have at most {_MAX_INPUTS} entries"]

    seen_match: set[str] = set()
    by_match: dict[str, str] = {}
    n_platform = 0
    n_folder_data = 0
    for i, item in enumerate(inputs):
        if not isinstance(item, dict):
            errors.append(f"inputs[{i}] must be an object")
            continue
        for key in sorted(set(item) - _INPUT_KEYS):
            errors.append(f"inputs[{i}] has unknown field: {key!r}")

        match = item.get("match")
        err = globmatch.validate_pattern(match)
        if err:
            errors.append(f"inputs[{i}].match: {err}")
        elif match.lower() in seen_match:
            errors.append(f"inputs[{i}].match is a duplicate: {match!r}")
        else:
            seen_match.add(match.lower())

        role = item.get("role", "data")
        if role not in INPUT_ROLES:
            errors.append(f"inputs[{i}].role must be one of {', '.join(INPUT_ROLES)}")
        elif err is None:
            by_match[match] = role

        if "required" in item and not isinstance(item["required"], bool):
            errors.append(f"inputs[{i}].required must be a boolean")

        source = item.get("source", "folder")
        if source not in INPUT_SOURCES:
            errors.append(f"inputs[{i}].source must be one of {', '.join(INPUT_SOURCES)}")
            source = None
        table = item.get("table")
        is_platform = source == "platform"
        if is_platform:
            n_platform += 1
            errors.extend(_validate_platform_input(item, i, match, role, table,
                                                   required_files))
        elif table is not None:
            errors.append(f'inputs[{i}].table is only valid for source "platform"')
        elif source == "folder" and role == "data" and err is None:
            n_folder_data += 1

        sheets = item.get("sheets")
        if sheets is None:
            continue
        if not isinstance(sheets, list):
            errors.append(f"inputs[{i}].sheets must be a list")
            continue
        if len(sheets) > _MAX_INPUT_SHEETS:
            errors.append(f"inputs[{i}].sheets must have at most {_MAX_INPUT_SHEETS} entries")
            continue
        for j, sh in enumerate(sheets):
            where = f"inputs[{i}].sheets[{j}]"
            if not isinstance(sh, dict):
                errors.append(f"{where} must be an object")
                continue
            for key in sorted(set(sh) - _INPUT_SHEET_KEYS):
                errors.append(f"{where} has unknown field: {key!r}")
            name = sh.get("name", "*")
            # Sheet names are free text in Excel ("Sheet1", "Q3 / plan"), so
            # only emptiness and length are policed, not the charset.
            if not isinstance(name, str) or not name.strip() or len(name) > 64:
                errors.append(f"{where}.name must be a non-empty string of at most 64 chars")
            cols = sh.get("columns")
            if cols is None:
                continue
            if not isinstance(cols, list):
                errors.append(f"{where}.columns must be a list")
                continue
            if len(cols) > _MAX_INPUT_COLUMNS:
                errors.append(f"{where}.columns must have at most {_MAX_INPUT_COLUMNS} entries")
                continue
            seen_cols: set[str] = set()
            for k, col in enumerate(cols):
                # A column is either a plain string or {name, description} —
                # both forms carry the same name constraint, so validate on
                # the extracted name and only additionally check description
                # when the object form is used.
                if isinstance(col, dict):
                    for key in sorted(set(col) - _INPUT_COLUMN_KEYS):
                        errors.append(f"{where}.columns[{k}] has unknown field: {key!r}")
                    name_val = col.get("name")
                    ctype_col = col.get("type")
                    if ctype_col is not None and ctype_col not in COLUMN_TYPES:
                        errors.append(
                            f"{where}.columns[{k}].type must be one of "
                            f"{', '.join(COLUMN_TYPES)}")
                    desc = col.get("description")
                    if desc is not None and (
                        not isinstance(desc, str) or len(desc) > _MAX_COLUMN_DESCRIPTION_LEN
                    ):
                        errors.append(
                            f"{where}.columns[{k}].description must be a string of at most "
                            f"{_MAX_COLUMN_DESCRIPTION_LEN} chars")
                elif isinstance(col, str):
                    name_val = col
                else:
                    name_val = None

                if not isinstance(name_val, str) or not name_val.strip() or len(name_val) > _MAX_COLUMN_LEN:
                    errors.append(
                        f"{where}.columns[{k}] must be a non-empty string (or an object with a "
                        f"non-empty name) of at most {_MAX_COLUMN_LEN} chars")
                elif name_val.strip().casefold() in seen_cols:
                    errors.append(f"{where}.columns[{k}] is a duplicate: {name_val!r}")
                else:
                    seen_cols.add(name_val.strip().casefold())

    # A platform input is an inputs[] entry, so declaring one flips the manifest
    # out of legacy mode (is_legacy_inputs) and turns `inputs` into the engine's
    # allow-list (gen-engine.js:1145) — a generator that had been reading every
    # Excel file in the folder would suddenly read only the reference workbook.
    # Refuse the manifest instead of silently starving the run.
    if n_platform and not n_folder_data:
        errors.append(_("a platform input requires declared inputs with role=data"))

    # A required_files name that a data glob also admits (`SL_NWD.xlsx` ×
    # `SL_*.xlsx`) is NOT a contradiction: the exact name wins. `inputs_for`
    # puts required names first, and every reader attributes a file to the
    # FIRST entry that admits it, so the service workbook is a reference and
    # the glob keeps every weekly file. (Prod 2026-08-27: refusing this
    # forced `SL_??.xlsx`, which silently dropped `SL_01_02.xlsx`.)
    return errors


def inputs_for(manifest: dict) -> list[dict]:
    """The generator's declared inputs in ONE canonical shape, whatever the
    manifest's vintage. `required_files` names become reference inputs that
    are required; explicit `inputs` entries get their defaults filled. A
    frozen manifest from before `inputs` existed yields only the sugar, and a
    manifest with neither yields [] — which the engine reads as "everything
    Excel in the folder", the legacy behaviour.

    Order: `required_files` names FIRST, then explicit entries. Readers (the
    engine's enumerate, quality's `_match_of`) attribute a file to the first
    entry admitting it, so an exact required name wins over a data glob that
    also covers it — `SL_NWD.xlsx` stays a reference while `SL_*.xlsx` keeps
    every weekly file. A required name spelled verbatim as an explicit
    `inputs[].match` is not duplicated: the explicit entry stands, moved to
    the front."""
    out: list[dict] = []
    for item in (manifest.get("inputs") or []):
        if not isinstance(item, dict) or not isinstance(item.get("match"), str):
            continue
        sheets = []
        for sh in (item.get("sheets") or []):
            if not isinstance(sh, dict):
                continue
            # A column may be authored as a bare string or {name, description}
            # (P3.6) — this function always hands back list[str], the shape
            # every existing reader (engine, schemadiff, lineage) expects;
            # descriptions are exposed separately by column_descriptions().
            names: list[str] = []
            for c in (sh.get("columns") or []):
                if isinstance(c, str):
                    names.append(c.strip())
                elif isinstance(c, dict) and isinstance(c.get("name"), str):
                    names.append(c["name"].strip())
            sheets.append({
                "name": str(sh.get("name") or "*"),
                "columns": names,
            })
        source = item.get("source") if item.get("source") in INPUT_SOURCES else "folder"
        table = item.get("table") if source == "platform" else None
        out.append({
            "match": item["match"],
            "role": item.get("role") if item.get("role") in INPUT_ROLES else "data",
            "required": bool(item.get("required", False)),
            "sheets": sheets,
            "source": source,
            "table": table if isinstance(table, str) else None,
            "from_required_files": False,
        })
    front: list[dict] = []
    for name in (manifest.get("required_files") or []):
        if not isinstance(name, str):
            continue
        exact = next((e for e in out if e["match"].casefold() == name.casefold()), None)
        if exact is not None:
            if exact not in front:
                front.append(exact)
            continue
        front.append({
            "match": name, "role": "reference", "required": True,
            "sheets": [], "source": "folder", "table": None,
            "from_required_files": True,
        })
    return front + [e for e in out if e not in front]


def is_legacy_inputs(inputs: list[dict]) -> bool:
    """True when the manifest declares NO inputs of its own — entries folded in
    from `required_files` (`from_required_files`) do not count: for such a
    manifest the engine still reads every Excel file in the folder (regression
    2026-08-21, ops-rdc-health), so it is the population "Suggest inputs"
    targets and the one lineage draws with the synthetic `*` pattern."""
    return not any(not (i or {}).get("from_required_files") for i in (inputs or []))


def column_descriptions(manifest: dict) -> dict[str, dict[str, str]]:
    """The manifest-authored column descriptions, keyed `"<match>/<sheet>"` ->
    {column: description} — the counterpart `inputs_for` drops when it
    normalizes columns down to list[str]. Only the object column form
    (`{name, description}`) carries a description; a bare string contributes
    nothing. Malformed entries are skipped, same discipline as `checks_for`:
    `validate()` gates registration, this also runs over frozen manifests."""
    out: dict[str, dict[str, str]] = {}
    for item in (manifest.get("inputs") or []):
        if not isinstance(item, dict) or not isinstance(item.get("match"), str):
            continue
        match = item["match"]
        for sh in (item.get("sheets") or []):
            if not isinstance(sh, dict):
                continue
            sheet = str(sh.get("name") or "*")
            cols: dict[str, str] = {}
            for c in (sh.get("columns") or []):
                if not isinstance(c, dict):
                    continue
                name, desc = c.get("name"), c.get("description")
                if isinstance(name, str) and name.strip() and isinstance(desc, str) and desc.strip():
                    cols[name.strip()] = desc
            if cols:
                out[f"{match}/{sheet}"] = cols
    return out


def column_types(manifest: dict) -> dict[tuple[str, str, str], str]:
    """The manifest-authored column types, keyed `(match, sheet, column)` ->
    one of COLUMN_TYPES. The counterpart `inputs_for` drops when it normalizes
    columns down to list[str], same as `column_descriptions`. Only the object
    column form carries a type; a bare string contributes nothing, and an
    unknown type is skipped (validate() gates registration, this also runs
    over frozen manifests)."""
    out: dict[tuple[str, str, str], str] = {}
    for item in (manifest.get("inputs") or []):
        if not isinstance(item, dict) or not isinstance(item.get("match"), str):
            continue
        match = item["match"]
        for sh in (item.get("sheets") or []):
            if not isinstance(sh, dict):
                continue
            sheet = str(sh.get("name") or "*")
            for c in (sh.get("columns") or []):
                if not isinstance(c, dict):
                    continue
                name, ctype = c.get("name"), c.get("type")
                if isinstance(name, str) and name.strip() and ctype in COLUMN_TYPES:
                    out[(match, sheet, name.strip())] = ctype
    return out


def platform_inputs(manifest: dict) -> list[dict]:
    """The manifest's `source: "platform"` inputs, normalised to what the
    platform needs to answer with: `{match, table, key_column, columns:
    [{name, type}]}` — the frozen schema of the reference table this generator
    consumes, in author order (the FIRST column is the key, by the fixed
    layout the server writes). Malformed entries are skipped, same discipline
    as `checks_for`; validate() is what rejects them at registration."""
    out: list[dict] = []
    for item in (manifest.get("inputs") or []):
        if not isinstance(item, dict) or item.get("source") != "platform":
            continue
        table, match = item.get("table"), item.get("match")
        if not (isinstance(table, str) and table and isinstance(match, str) and match):
            continue
        cols: list[dict] = []
        for sh in (item.get("sheets") or []):
            if not isinstance(sh, dict) or str(sh.get("name") or "") != PLATFORM_SHEET:
                continue
            for c in (sh.get("columns") or []):
                if not isinstance(c, dict):
                    continue
                name, ctype = c.get("name"), c.get("type")
                if isinstance(name, str) and name.strip() and ctype in COLUMN_TYPES:
                    cols.append({"name": name.strip(), "type": ctype})
        if not cols:
            continue
        out.append({
            "match": match,
            "table": table,
            "key_column": cols[0]["name"],
            "columns": cols,
        })
    return out


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_checks(checks, inputs) -> list[str]:
    """Checks are only as meaningful as the inputs they point at: `match` must
    be one of inputs[].match — spelled exactly, because the engine attributes
    files by `matched_input = inputs[].match` and filters checks with `===`;
    a casefold-tolerant validator would admit a check that never runs — and
    `column` must be declared on a sheet that overlaps the check's sheet.
    Both are resolved here, at registration, so the engine can trust a check
    without re-deriving it.

    `on_fail` accepts only "warn" in this version — "block" is reserved in
    the contract (a failing check that stops publish) but nothing enforces it
    yet, and accepting the word silently would promise what we do not do.

    Structural errors in `inputs` are reported by _validate_inputs; here an
    unusable `inputs` just means every check fails the match lookup."""
    errors: list[str] = []
    if not isinstance(checks, list):
        return ["checks must be a list"]
    if len(checks) > _MAX_CHECKS:
        return [f"checks must have at most {_MAX_CHECKS} entries"]
    if checks and not (isinstance(inputs, list) and inputs):
        return ["checks require inputs"]

    from app.services import globmatch

    # match (exact spelling) -> {sheet glob -> {casefold(column)}}
    declared: dict[str, dict[str, set[str]]] = {}
    for item in inputs:
        if not isinstance(item, dict) or not isinstance(item.get("match"), str):
            continue
        sheets: dict[str, set[str]] = {}
        for sh in (item.get("sheets") or []):
            if not isinstance(sh, dict):
                continue
            name = sh.get("name", "*")
            if not isinstance(name, str):
                continue
            cols: set[str] = set()
            for c in (sh.get("columns") or []):
                if isinstance(c, str):
                    cols.add(c.strip().casefold())
                elif isinstance(c, dict) and isinstance(c.get("name"), str):
                    cols.add(c["name"].strip().casefold())
            sheets.setdefault(name, set()).update(cols)
        declared[item["match"]] = sheets
    # Tables a `ref_lookup` may point at: only the platform inputs THIS
    # manifest declares. A check against a table the generator never reads is
    # a typo, not a feature — and the engine would have no keys to hand it.
    platform_tables = {p["table"] for p in platform_inputs({"inputs": inputs})}

    seen: set[tuple] = set()
    for i, chk in enumerate(checks):
        where = f"checks[{i}]"
        if not isinstance(chk, dict):
            errors.append(f"{where} must be an object")
            continue
        for key in sorted(set(chk) - _CHECK_KEYS):
            errors.append(f"{where} has unknown field: {key!r}")

        ctype = chk.get("type")
        if ctype not in CHECK_TYPES:
            errors.append(f"{where}.type must be one of {', '.join(CHECK_TYPES)}")
            ctype = None

        match = chk.get("match")
        match_ok = isinstance(match, str) and match in declared
        if not match_ok:
            errors.append(f"{where}.match must equal one of inputs[].match")

        sheet = chk.get("sheet", "*")
        if not isinstance(sheet, str) or not sheet.strip() or len(sheet) > 64:
            errors.append(f"{where}.sheet must be a non-empty string of at most 64 chars")
            sheet = None
        elif ":" in sheet:
            # The check id is "{type}:{match}:{sheet}:{column}", split from
            # the RIGHT (observations._parse_check_id) because `match` is a
            # glob that may legitimately contain ":". `sheet` and `column`
            # may not, or the split silently misparses and corrupts the
            # stored finding's sheet/column (and the baseline seeded from it).
            errors.append(f"{where}.sheet must not contain ':'")
            sheet = None

        column = chk.get("column")
        if not isinstance(column, str) or not column.strip() or len(column) > _MAX_COLUMN_LEN:
            errors.append(
                f"{where}.column must be a non-empty string of at most {_MAX_COLUMN_LEN} chars")
            column = None
        elif ":" in column:
            errors.append(f"{where}.column must not contain ':'")
            column = None
        elif match_ok and sheet is not None:
            # The check's sheet is itself a glob; it must overlap at least
            # one declared sheet glob that lists the column, in either
            # direction: "Sheet*" against a declaration for "*" counts (the
            # declaration admits every sheet the check can touch), and the
            # default "*" against a declared "Data" counts too (the check
            # covers the declared sheet). Overlap is the honest test — both
            # globs are resolved against real sheet names only at run time.
            found = False
            for decl_sheet, cols in declared[match].items():
                if (globmatch.matches(decl_sheet, sheet) or globmatch.matches(sheet, decl_sheet)
                        or decl_sheet == sheet) and column.strip().casefold() in cols:
                    found = True
                    break
            if not found:
                errors.append(
                    f"{where}.column {column!r} is not declared in inputs for match "
                    f"{match!r}, sheet {sheet!r}")

        if ctype == "range":
            lo, hi = chk.get("min"), chk.get("max")
            if lo is None and hi is None:
                errors.append(f"{where}: range needs min and/or max")
            if lo is not None and not _is_number(lo):
                errors.append(f"{where}.min must be a number")
            if hi is not None and not _is_number(hi):
                errors.append(f"{where}.max must be a number")
            if _is_number(lo) and _is_number(hi) and lo > hi:
                errors.append(f"{where}: min must be <= max")
        elif "min" in chk or "max" in chk:
            errors.append(f"{where}: min/max are only valid for type range")

        if ctype == "regex":
            pat = chk.get("pattern")
            if not isinstance(pat, str) or not pat or len(pat) > _MAX_REGEX_LEN:
                errors.append(
                    f"{where}.pattern must be a non-empty string of at most {_MAX_REGEX_LEN} chars")
            else:
                try:
                    re.compile(pat)
                except re.error as exc:
                    errors.append(f"{where}.pattern does not compile: {exc}")
        elif "pattern" in chk:
            errors.append(f"{where}: pattern is only valid for type regex")

        if ctype == "enum":
            values = chk.get("values")
            if not isinstance(values, list) or not values:
                errors.append(f"{where}.values must be a non-empty list")
            elif len(values) > _MAX_ENUM_VALUES:
                errors.append(f"{where}.values must have at most {_MAX_ENUM_VALUES} entries")
            elif not all(isinstance(v, str) and len(v) <= _MAX_VALUE_LEN for v in values):
                errors.append(
                    f"{where}.values must be strings of at most {_MAX_VALUE_LEN} chars")
        elif "values" in chk:
            # known_values included: its values come from the server baseline.
            errors.append(f"{where}: values are only valid for type enum")

        if ctype == "ref_lookup":
            table = chk.get("table")
            if not isinstance(table, str) or not _NAME_RE.match(table):
                errors.append(f"{where}.table must match ^[a-z0-9][a-z0-9_-]{{0,63}}$")
            elif table not in platform_tables:
                errors.append(
                    f"{where}.table {table!r} has no platform input in this manifest "
                    f'(declare inputs[] with source "platform" and this table)')
        elif "table" in chk:
            errors.append(f"{where}: table is only valid for type ref_lookup")

        on_fail = chk.get("on_fail", "warn")
        if on_fail == "block":
            errors.append(_('{where}.on_fail "block" is not supported in this version',
                            where=where))
        elif on_fail != "warn":
            errors.append(f'{where}.on_fail must be "warn"')

        label = chk.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > 64):
            errors.append(f"{where}.label must be a string of at most 64 chars")

        if ctype and match_ok and sheet is not None and column is not None:
            key = (match, sheet.strip().casefold(), column.strip().casefold(), ctype)
            if key in seen:
                errors.append(f"{where} is a duplicate check: {ctype} on {match}/{sheet}/{column}")
            seen.add(key)
    return errors


def checks_for(manifest: dict) -> list[dict]:
    """The manifest's checks in ONE canonical shape for the engine and the
    server: every key present (None when absent), defaults filled, and a
    stable `id` = "type:match:sheet:column" — the key `quality_results` is
    upserted by and findings are identified by across runs. The id is built
    from the author's spelling, not casefolded: a manifest that renames a
    column is a new check, which is the honest reading. Sheet and column are
    stripped (validation keys on the stripped forms) so the id and the
    per-sheet filter in the worker compare against sniffed sheet names.

    Malformed entries are skipped rather than raised — validate() gated them
    at registration; this function also runs over frozen manifests."""
    out: list[dict] = []
    for chk in (manifest.get("checks") or []):
        if not isinstance(chk, dict):
            continue
        match, column, ctype = chk.get("match"), chk.get("column"), chk.get("type")
        if not (isinstance(match, str) and isinstance(column, str) and ctype in CHECK_TYPES):
            continue
        sheet = chk.get("sheet").strip() if isinstance(chk.get("sheet"), str) else "*"
        sheet = sheet or "*"
        column = column.strip()
        out.append({
            "id": f"{ctype}:{match}:{sheet}:{column}",
            "type": ctype,
            "match": match,
            "sheet": sheet,
            "column": column,
            "min": chk.get("min"),
            "max": chk.get("max"),
            "pattern": chk.get("pattern"),
            "values": list(chk["values"]) if isinstance(chk.get("values"), list) else None,
            "on_fail": chk.get("on_fail") or "warn",
            "label": chk.get("label"),
            # ref_lookup only: the platform reference table whose CURRENT keys
            # the engine injects into this spec before run_checks sees it.
            "table": chk["table"] if ctype == "ref_lookup" and isinstance(chk.get("table"), str) else None,
        })
    return out


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize(manifest: dict) -> dict:
    """Return a COPY of manifest with defaults filled in: engine="auto",
    write_back=False, params=[], deps=[], outputs.slots=[], aux_inputs=[],
    required_files=[], inputs=[], checks=[], and any missing data_scale field backfilled with the
    REC_* browser-engine recommendation."""
    result = dict(manifest)

    result.setdefault("engine", "auto")
    result.setdefault("write_back", False)
    result["params"] = list(manifest.get("params") or [])
    result["deps"] = list(manifest.get("deps") or [])
    result["aux_inputs"] = list(manifest.get("aux_inputs") or [])
    result["required_files"] = list(manifest.get("required_files") or [])
    # Stored as written (defaults live in inputs_for), so the frozen manifest
    # keeps saying exactly what the author said.
    result["inputs"] = list(manifest.get("inputs") or [])
    result["checks"] = list(manifest.get("checks") or [])

    outputs = dict(manifest.get("outputs") or {})
    outputs["slots"] = list(outputs.get("slots") or [])
    result["outputs"] = outputs

    data_scale = dict(manifest.get("data_scale") or {})
    data_scale.setdefault("max_files", REC_MAX_FILES)
    data_scale.setdefault("max_total_bytes", REC_MAX_TOTAL_BYTES)
    data_scale.setdefault("max_file_bytes", REC_MAX_FILE_BYTES)
    result["data_scale"] = data_scale

    # sources: pass through with the executor defaults made explicit, so the
    # frozen manifest fully describes the run the agent will perform. Only when
    # present and well-formed (validate() gates registration before this runs).
    src = manifest.get("sources")
    if isinstance(src, dict):
        src = dict(src)
        src.setdefault("parallelism", 3)
        src.setdefault("statement_timeout_ms", 300000)
        src.setdefault("freeze_cycle", False)
        src["queries"] = list(src.get("queries") or [])
        result["sources"] = src

    # rls (ADR-18): passed through unchanged — it has no defaults to fill,
    # only validate()'s structural gate. Absent stays absent (not-RLS is the
    # overwhelming default; Dashboard.rls_enabled reads json is-not-None).
    if "rls" in manifest:
        result["rls"] = manifest["rls"]

    return result


# ---------------------------------------------------------------------------
# wasm32 dependency resolution
# ---------------------------------------------------------------------------

_lock_cache: dict[str, str] | None = None


def _pep503_normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_lock() -> dict[str, str]:
    """Lazily read the vendored Pyodide package lock. Read once per process;
    a missing file degrades to an empty lock (everything resolves via PyPI)
    with a single warning, not a crash."""
    global _lock_cache
    if _lock_cache is not None:
        return _lock_cache

    path = Path(__file__).resolve().parent / "data" / "pyodide_lock_314.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        _lock_cache = dict(raw.get("packages") or {})
    except OSError:
        print(f"genmanifest: no Pyodide lock file at {path} — treating as empty")
        _lock_cache = {}
    except (ValueError, AttributeError) as e:
        print(f"genmanifest: Pyodide lock file at {path} is invalid ({e}) — treating as empty")
        _lock_cache = {}
    return _lock_cache


def _resolve_from_pypi(name: str, version: str) -> tuple[str, str | None]:
    """Returns (wasm32 status, wheel filename or None). The filename is frozen
    into the version manifest so the browser engine can try the SAME wheel
    from our own origin (static/pyodide/wheels/, populated by
    deploy/fetch-pyodide.py) before falling back to pypi.org — corporate
    proxies/CSP may block the external fetch."""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return (WASM_UNAVAILABLE if e.code == 404 else WASM_UNKNOWN), None
    except (urllib.error.URLError, OSError, ValueError):
        return WASM_UNKNOWN, None

    filenames = [u.get("filename", "") for u in (data.get("urls") or []) if isinstance(u, dict)]
    for fn in filenames:
        if "emscripten" in fn and "wasm32" in fn:
            return WASM_PYPI_WHEEL, fn
    # "-none-any.whl" (not "-py3-none-any.whl"): pure wheels commonly carry a
    # py2.py3 tag (e.g. openpyxl-3.1.5-py2.py3-none-any.whl) — micropip
    # installs any abi-none / platform-any wheel.
    for fn in filenames:
        if fn.endswith("-none-any.whl"):
            return WASM_PYPI_PURE, fn
    return WASM_UNAVAILABLE, None


def resolve_deps(deps: list[dict]) -> list[dict]:
    """Return a copy of deps with a computed "wasm32" status on each entry.

    bundled: the PEP 503-normalized name is in the vendored Pyodide lock
    (app/services/data/pyodide_lock_314.json). If the pinned lock version
    differs from the requested one, the status stays "bundled" (micropip
    installs whatever version ships with the distribution) but a
    "wasm32_note" is added.

    Otherwise resolved against PyPI: a filename containing both "emscripten"
    and "wasm32" -> pypi-wheel; else a "-none-any.whl" (pure) -> pypi-pure; else
    (sdist only) -> unavailable (micropip cannot build an sdist); a 404 ->
    unavailable; any other network error or timeout -> unknown (so an
    offline registration of an agent-only generator isn't blocked).
    """
    lock = _load_lock()
    resolved: list[dict] = []
    for dep in deps:
        item = dict(dep)
        name = dep.get("name", "")
        version = dep.get("version", "")
        norm = _pep503_normalize(name)
        if norm in lock:
            item["wasm32"] = WASM_BUNDLED
            lock_version = lock[norm]
            if lock_version != version:
                item["wasm32_note"] = f"pyodide dist ships {lock_version}"
        else:
            status, wheel = _resolve_from_pypi(name, version)
            item["wasm32"] = status
            if wheel:
                item["wheel"] = wheel
        resolved.append(item)
    return resolved


# ---------------------------------------------------------------------------
# Browser-engine capability enforcement
# ---------------------------------------------------------------------------

def resolved_engine(manifest: dict) -> str:
    """Which engine will ACTUALLY execute the run: "browser" or "agent".

    `engine` in the manifest declares an intent, and "auto" is not an answer: the boundary
    is decided by the presence of `sources` (GreenPlum is read only by the agent with its
    legacy CLI). Exactly this rule lives in `gen-engine.js::engineMode`, and
    here it is repeated for the server.

    A native folder run (`engine: "local"` at run start) arrives with a
    "browser"/"auto" manifest WITHOUT `sources` and answers "browser" here —
    deliberately: for everything that asks "do we observe this engine", local and
    browser are the same (both send files, structure and reads). The agent
    is unobservable not because it is the agent, but because the SQL path opens no
    files and has no hooks.
    """
    declared = (manifest or {}).get("engine") or "auto"
    if declared in ("browser", "agent"):
        return declared
    return "agent" if (manifest or {}).get("sources") else "browser"



def browser_blockers(manifest: dict) -> list[str]:
    """422-level errors for a NORMALIZED manifest whose deps already carry a
    computed wasm32 status. Only applies when engine == "browser" ("auto" is
    resolved at the runtime border, not here); returns [] otherwise.

    Blocks only what the browser CANNOT do. Scale is not a blocker — see the
    REC_MAX_* note at the top of this module."""
    if manifest.get("engine") != "browser":
        return []

    blockers: list[str] = []

    # A database source is physically impossible in the browser (no TCP/libpq):
    # it needs the agent engine. engine="auto"+sources is fine (resolved to the
    # agent at the runtime border), so this only fires for explicit "browser".
    if manifest.get("sources") is not None:
        blockers.append(
            "sources require the agent engine — the browser cannot reach a database"
        )

    for dep in manifest.get("deps") or []:
        status = dep.get("wasm32")
        if status in (WASM_UNAVAILABLE, WASM_UNKNOWN):
            blockers.append(
                f"dependency {dep.get('name')!r} is not usable in the browser "
                f"engine (wasm32={status})"
            )

    # data_scale is NOT checked here any more: the REC_MAX_* numbers are
    # advice, not a ceiling, so a manifest may declare any scale it likes. The
    # browser UI warns the analyst when the chosen folder exceeds the declared
    # numbers and lets them run anyway. What stays a blocker is what the
    # browser physically cannot do (a database source, a non-wasm32 dep).

    return blockers


# ---------------------------------------------------------------------------
# Publish-time diagnostics
# ---------------------------------------------------------------------------

def _mb(n: int) -> str:
    """Decimal MB, matching how the preview cutoff is configured and documented
    (`preview_max_bytes = 1_500_000` is 1.5 MB, not 1.5 MiB)."""
    return f"{n / 1e6:.1f}"


def publish_warnings(manifest: dict, published: dict[int, tuple[str, int]], *,
                     render_size: int | None = None,
                     preview_max: int = 0,
                     upload_max: int = 0,
                     first_publish: bool = False,
                     observed_diff: dict | None = None,
                     quality: dict | None = None) -> list[str]:
    """Non-blocking diagnostics for a run that is about to publish.

    NOTHING here rejects a publish. Every condition below is legal today and
    some are deliberate — the pilot report is already over the preview cutoff
    and ships that way on purpose. They exist because the platform used to
    accept them in SILENCE: an oversized report simply lost its live grid
    preview, a slot published under an index the frozen manifest never declared
    reached the dashboard as data it does not expect, and a renamed output
    changed the `Content-Encoding` the browser applies (`*.gz` is decompressed
    transparently) without anyone being told.

    The whole check is a handful of dict lookups on data `publish_run` already
    holds, and it runs once per publish — a rare, human-initiated action. It
    reaches the only person who can act on it, at the only moment they are
    looking.

    Text is Russian on purpose: unlike `validate()`, whose errors an admin reads
    at registration, these reach the report author in the run panel, which is
    Russian on the agent path.

    `observed_diff` (`app/services/schemadiff.py::diff`, this run's declared vs
    observed inputs) adds two more non-blocking lines when present: a pattern
    that matched no file, and a declared column that never showed up in the
    observed header row. Same rule as everything else here — informational,
    never a reason to reject.

    `quality` (`app/services/quality.py::run_summary`, this run's own checks)
    adds failing-check, new-value and unmapped-reference lines, each capped at
    5 with a "… and N more" tail so one noisy column can't bury the rest of the
    panel.
    """
    out: list[str] = []

    if preview_max > 0 and render_size and render_size > preview_max:
        out.append(_(
            "The report is {size} MB, while the live preview threshold is {limit} MB — the card on the «Dashboards» page will show a placeholder instead of a preview. Keep the data in a slot, not in the HTML.",
            size=_mb(render_size), limit=_mb(preview_max)))

    declared: dict[int, str | None] = {}
    for slot in ((manifest.get("outputs") or {}).get("slots") or []):
        if isinstance(slot, dict) and _is_int(slot.get("index")):
            declared[slot["index"]] = slot.get("name")

    # No declared slots -> the manifest has no opinion, so neither do we.
    if declared:
        for idx in sorted(published):
            name = published[idx][0]
            if idx not in declared:
                out.append(_(
                    "slot_{index} was published, but it's not in the version manifest's outputs.slots — the dashboard isn't expecting it.", index=idx))
            elif declared[idx] and name and name != declared[idx]:
                out.append(_(
                    "slot_{index}: the published file is {got}, but the manifest declares {want}. The extension sets the Content-Encoding, so a mismatch changes what the page receives.",
                    index=idx, got=repr(name), want=repr(declared[idx])))
        if first_publish:
            for idx in sorted(set(declared) - set(published)):
                out.append(_(
                    "slot_{index} ({name}) is declared in the manifest but not published — on the first version the dashboard will be left without this data and hang on loading.", index=idx, name=declared[idx]))

    # 80 % of the hard cap: the pilot's largest extract sits at ~49 % today, and
    # lifting its LIMIT was measured to triple it — the 413 must not be the way
    # the author finds that out.
    if upload_max > 0:
        for idx in sorted(published):
            name, size = published[idx]
            if size > upload_max * 0.8:
                out.append(_(
                    "slot_{index} ({name}) takes up {pct}% of the publish ceiling — further data growth will hit a 413. Trim the export in the generator.",
                    index=idx, name=name, pct=round(100 * size / upload_max)))

    if observed_diff:
        for match in observed_diff.get("unmatched_patterns") or []:
            out.append(_(
                "Pattern {pattern} from inputs didn't match any file in this run — no source was found for it.", pattern=repr(match)))
        for match, info in (observed_diff.get("per_pattern") or {}).items():
            for sheet_name, sheet_diff in (info.get("sheets") or {}).items():
                missing = sheet_diff.get("missing_columns") or []
                if missing:
                    out.append(_(
                        "{pattern}, sheet {sheet}: declared columns not found in the header — {columns}.",
                        pattern=repr(match), sheet=repr(sheet_name),
                        columns=", ".join(missing)))

    if quality:
        # A batch may omit count/failed (older engine, stripped payload) — never
        # print the literal "None" to the author.
        def _num(v):
            return "?" if v is None else v

        failed_checks = quality.get("failed_checks") or []
        lines = []
        for fc in failed_checks[:5]:
            label = f" («{fc['label']}»)" if fc.get("label") else ""
            lines.append(_(
                "Check {type}{label} on column {column} of sheet {sheet} in {file}: {failed} of {count} rows failed",
                type=fc["type"], label=label, column=fc["column"],
                sheet=fc["sheet"], file=fc["file"],
                failed=_num(fc.get("failed")), count=_num(fc.get("count"))))
        extra = len(failed_checks) - 5
        if extra > 0:
            lines.append(_("… and {extra} more", extra=extra))
        out.extend(lines)

        new_values = quality.get("new_values") or []
        lines = []
        for nv in new_values[:5]:
            values = ", ".join(nv.get("values") or [])
            more = nv.get("more") or 0
            tail = _(" and {more} more", more=more) if more else ""
            lines.append(_(
                "New values appeared in column {column} ({file}/{sheet}): {values}{tail} — confirm them on the Data Quality page",
                column=nv["column"], file=nv["file"], sheet=nv["sheet"],
                values=values, tail=tail))
        extra = len(new_values) - 5
        if extra > 0:
            lines.append(_("… and {extra} more", extra=extra))
        out.extend(lines)

        # ADR-16: values this run's files carry that the reference table does
        # not map. Today those fall through to «show the raw code» in silence;
        # this line is the moment someone who can act on it is looking, and it
        # names the person who answers for the dictionary — the whole point of
        # `responsible` being a field and not a convention.
        unmapped = quality.get("unmapped") or []
        lines = []
        for um in unmapped[:5]:
            values = ", ".join(um.get("values") or [])
            more = um.get("more") or 0
            tail = _(", {more} more", more=more) if more else ""
            who = um.get("responsible") or _("not assigned")
            name = um.get("title") or um.get("table") or um.get("slug")
            # A table created from the manifest but never imported into: every
            # key "misses" it, so the count is noise. Say the one useful thing.
            if um.get("exists") and not um.get("current_version"):
                lines.append(_(
                    "Reference table «{name}» is empty — upload an Excel file (owner: {who})", name=name, who=who))
                continue
            lines.append(_(
                "Reference table «{name}»: {count} new values ({values}{tail}) — owner {who}",
                name=name, count=um.get("total"), values=values, tail=tail, who=who))
        extra = len(unmapped) - 5
        if extra > 0:
            lines.append(_("… and {extra} more", extra=extra))
        out.extend(lines)

    return out
