"""Data Quality: findings computed on read, never stored.

Everything this module reports already exists in the database — what the
author DECLARED (`inputs`, `checks` of the current frozen manifest), what the
engine OBSERVED (`source_observations` / `source_files` / `quality_results`),
and what a human ACCEPTED (`value_baselines`, `quality_acks`, the per-source
thresholds on `data_sources`). `evaluate()` is the join, run when somebody
opens the page. Nothing here writes; the caller owns the session.

**Why compute-on-read.** A finding is a statement about the CURRENT state of
a source seen through the CURRENT manifest. Storing findings would mean
invalidating them on every manifest edit, every accepted baseline, every
threshold change — three write paths that would each have to remember to. The
history the page draws comes from the rows the runs already left behind, so
recomputing is the cheap option as well as the correct one: one bounded
window (`HISTORY_RUNS` runs per dashboard) of observations and their files.

**Stable ids.** `finding_id()` is a sha1 prefix over
(kind, source, generator, file/match, sheet, column) — no run, no timestamp —
so the same problem carries the same id across runs. That is what lets a
human acknowledge it (`quality_acks.finding_id`) and what lets `since_run`
mean something: the evaluator re-runs the schema comparison against older
levels of the chain and reports the OLDEST consecutive run that produced the
same id.

**Honest limits.**

  * Findings are computed per (generator, source) PAIR, because that is the
    unit everything else is keyed by — declared inputs, checks, aliases,
    baselines, `min_rows` thresholds. A folder read by two generators
    therefore yields two `file_stale` findings, one per report. That is
    noisier than a per-folder count and it is what makes each row
    actionable by the person who owns the report.
  * `since_run` walks the chain only for the `schema_*` kinds (they are the
    ones a pure re-diff can reproduce from stored data). Every other kind
    reports the run it was computed on. An ack bounded by `until_run_id`
    reads that field, so acking a check failure is bounded by the run it was
    last seen on, not by the run it first appeared on.
  * A legacy generator (no `inputs`) gets freshness, completeness and rows
    trends only: there is nothing declared to diff, no `checks` to run, and
    no `match` to key a baseline by. The plan says so out loud; so does the
    page.
  * The agent engine observes nothing, so a source only ever read by it has
    no levels here and produces no findings — silence, not an "ok". That is
    the SQL path only: a native folder run on the desktop agent (`engine:
    "local"`) reads files through the same R2 contract as the browser and
    posts the same observations, so it is observable exactly like `browser`.
  * `detail.affected` answers "who else reads this column" from the lineage
    graph and labels every answer with how it knows (`consumed_as_of_run:N`
    is evidence; `declared` is a claim; `sheet_level` is python-calamine's
    ceiling; `unobservable` is the agent engine — the GreenPlum SQL path,
    never the native folder run). It never guesses higher than the evidence
    it has.
"""
from __future__ import annotations

import urllib.parse

import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.i18n import _
from app.models import (
    DataSource, GenerationRun, Generator, GeneratorVersion, InputOverride, QualityAck,
    QualityResult, SourceFile, SourceObservation, ValueBaseline,
)
from app.services import datasources, genmanifest, globmatch, lineage, observations, schemadiff
from app.services.schemadiff import norm

# A source nobody has refreshed in this long is stale, unless its own row
# says otherwise. Same number the Data Sources page uses — one definition of
# "stale" per installation, not two.
STALE_DAYS_DEFAULT = datasources.STALE_DAYS

# "Half the files/rows are gone" is the default alarm. A source that needs a
# tighter or looser number carries it in `thresholds_json.drop_pct`.
DROP_PCT_DEFAULT = 50

# How deep the history window goes. Ten runs is a fortnight of a daily report
# and a quarter of a weekly one — enough for a trend line, bounded enough
# that the page's queries stay small.
HISTORY_RUNS = 10

SEVERITY = ("error", "warn", "info")
_SEV_RANK = {s: i for i, s in enumerate(SEVERITY)}

KINDS = (
    "schema_missing_pattern",
    "schema_missing_sheet",
    "schema_missing_column",
    "schema_extra_column",
    "schema_unexpected_file",
    "schema_unreadable",
    "file_stale",
    "file_count_drop",
    "required_file_gone",
    "rows_drop",
    "rows_zero",
    "check_fail",
    "new_values",
    "header_shift",
    "dead_declaration",
    # ADR-16 "Reference tables". `unmapped_values` is drift (values the report
    # brought that the platform reference table has no row for);
    # `rerun_needed` is freshness (the report was published against an older
    # version of a table than the one that is current now).
    "unmapped_values",
    "rerun_needed",
)

# How many still-missing values one `unmapped_values` finding carries, and
# how many keys the "download a template for the new values" link may ask
# for. Both are UI ceilings, not data ceilings: `detail.total` always states
# the true count.
MAX_UNMAPPED_SHOWN = 50
MAX_TEMPLATE_KEYS = 500
# Byte ceiling for the encoded `keys` parameter of the template link. nginx's
# default `large_client_header_buffers` gives a request line ~8 KB, so a link
# any longer than this comes back 414 instead of a workbook.
MAX_TEMPLATE_URL_BYTES = 6000

# A suggested rename must be this close to the missing column, as a share of
# the longer spelling. 0.25 accepts "SKU Description" vs "SKU_Discription"
# (2 edits over 15 chars) and refuses two genuinely different headers.
_SUGGEST_MAX_RATIO = 0.25

# Skip reasons the engine emits for a check it could not complete. Anything
# else is not a quality signal, it is a bug report.
_SKIPPED_REASONS = ("cardinality_exceeded", "unreadable")

_UNEXPECTED_FILE_REASON = "not declared in inputs"

# Reason/error texts that describe the ENGINE's own capacity, not the data:
# the observation batch cap forced a schema to be skipped or stripped, or the
# lineage probe itself did not load. A file that genuinely has no header
# still needs the analyst's attention (severity follows `role`); a schema the
# engine chose not to read this run is not that — commit 633eaaa's whole
# point was to stop the two from looking identical, so this finding must not
# re-merge them into one severity.
_CAPACITY_REASON_MARKERS = ("budget", "batch cap", "stripped to fit", "probe unavailable")


def _is_capacity_reason(reason: str) -> bool:
    r = reason.lower()
    return any(marker in r for marker in _CAPACITY_REASON_MARKERS)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _id_part(value) -> str:
    """"" for None, normalised (NFC + strip + casefold) otherwise — the same
    normalisation schemadiff compares columns by, so "Date " and "date" are
    one finding and not two."""
    if value is None:
        return ""
    return norm(str(value))


def finding_id(kind: str, source_key: str, generator: str | None,
               file_or_match: str | None, sheet: str | None,
               column: str | None, extra: str | None = None) -> str:
    """The stable identity of a finding: sha1 of the parts that say WHAT is
    wrong WHERE, joined by "|". No run id, no timestamp — a finding that
    comes back after a run that fixed it is the same finding again, which is
    exactly what an ack bounded by `until_run_id` needs to be able to say.

    `extra` discriminates findings that share the same place: two checks on
    ONE (file, sheet, column) are two different problems (`not_null` and
    `regex` can fail independently, and genmanifest's uniqueness is on
    (match, sheet, column, type)), so `check_fail` passes the check id and
    every other kind leaves it empty — which keeps their ids byte-identical
    to what a six-part call produces."""
    raw = "|".join(_id_part(p) for p in
                   (kind, source_key, generator, file_or_match, sheet, column))
    if extra is not None:
        raw += "|" + _id_part(extra)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def levenshtein(a: str, b: str) -> int:
    """Edit distance, two rows of memory. Pure and dependency-free on purpose:
    the only caller compares at most a few hundred short column names, and a
    new package in the server image would cost more than these ten lines."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def nearest(name: str, candidates: list[str]) -> str | None:
    """The candidate closest to `name` by normalised edit distance, or None
    when the closest one is still further than `_SUGGEST_MAX_RATIO`. Ties go
    to the alphabetically first candidate so the answer is deterministic."""
    best: tuple[float, str] | None = None
    a = norm(name)
    for cand in sorted(candidates):
        b = norm(cand)
        longest = max(len(a), len(b))
        if not longest:
            continue
        ratio = levenshtein(a, b) / longest
        if ratio > _SUGGEST_MAX_RATIO:
            continue
        if best is None or ratio < best[0]:
            best = (ratio, cand)
    return best[1] if best else None


def _loads(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _iso(dt) -> str | None:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _sheets_of(files: list[dict]) -> dict:
    """(file name, sheet name) -> {rows, header_row, match}. One level's
    resolved schemas flattened; the file list already had NULL schemas walked
    back by `lineage.collect_chain_files`, so a level whose files were all
    "unchanged" still answers here."""
    out: dict[tuple[str, str], dict] = {}
    for f in files:
        schema = f.get("schema")
        if not isinstance(schema, dict):
            continue
        for sh in (schema.get("sheets") or []):
            if not isinstance(sh, dict):
                continue
            name = str(sh.get("name") or "")
            rows = sh.get("rows")
            hdr = sh.get("header_row")
            rows = rows if isinstance(rows, int) and not isinstance(rows, bool) else None
            hdr = hdr if isinstance(hdr, int) and not isinstance(hdr, bool) else None
            # sniff's own contract (static/py/_lineage.py): "rows" is the sheet
            # HEIGHT (physical row count, header row included), not the number
            # of data rows — the caller derives that as rows - header_row - 1.
            # A header-only export (rows == header_row + 1) has 0 data rows.
            data_rows = max(0, rows - hdr - 1) if rows is not None and hdr is not None else rows
            out[(f["name"], name)] = {
                "rows": rows,
                "data_rows": data_rows,
                "header_row": hdr,
                "match": f.get("matched_input"),
            }
    return out


def _match_of(name: str, inputs: list[dict]) -> str | None:
    return globmatch.first_match([i["match"] for i in inputs], name) if inputs else None


def _input_by_match(inputs: list[dict]) -> dict[str, dict]:
    return {i["match"]: i for i in inputs if isinstance(i.get("match"), str)}


# ---------------------------------------------------------------------------
# Schema findings (re-runnable against any level of the chain)
# ---------------------------------------------------------------------------

def _schema_findings(inputs: list[dict], files: list[dict], aliases: dict,
                     skipped: list) -> list[dict]:
    """The `schema_*` findings of ONE level, as bare dicts (kind/severity/
    file/sheet/column/message/detail). Deterministic and pure — `evaluate`
    calls it on the newest level for the real findings and on the older ones
    only to see which ids still reproduce (that is `since_run`)."""
    if not inputs:
        return []                       # legacy generator: nothing declared
    by_match = _input_by_match(inputs)
    diff = schemadiff.diff(inputs, files, aliases=aliases)
    out: list[dict] = []

    for match in diff["unmatched_patterns"]:
        item = by_match.get(match) or {}
        required = bool(item.get("required"))
        out.append({
            "kind": "schema_missing_pattern",
            "severity": "error" if required else "warn",
            "file": match, "sheet": None, "column": None,
            "message": (_("No files found matching pattern «{pattern}»", pattern=match)
                        + (_(" (mandatory input)") if required else "")),
            "detail": {"required": required, "role": item.get("role", "data")},
        })

    for match, info in sorted(diff["per_pattern"].items()):
        role = (by_match.get(match) or {}).get("role", "data")
        for sheet, sh in sorted(info["sheets"].items()):
            if sh.get("capacity"):
                # The engine chose not to read this sheet this run (batch
                # cap / probe unavailable) — never evidence the sheet is
                # actually missing, so no finding here at all. The run-level
                # `schema_unreadable` info finding (from the file's `error`)
                # already tells the analyst the schema wasn't read.
                continue
            if not sh["observed"]:
                out.append({
                    "kind": "schema_missing_sheet", "severity": "warn",
                    "file": match, "sheet": sheet, "column": None,
                    "message": _("Sheet «{sheet}» not found in files «{pattern}»",
                                 sheet=sheet, pattern=match),
                    "detail": {"columns_declared": len(sh["missing_columns"])},
                })
                # Every declared column of an absent sheet is "missing" too;
                # reporting them would be one problem told 30 times.
                continue
            for col in sorted(sh["missing_columns"]):
                detail: dict = {"role": role}
                suggest = nearest(col, sh["extra_columns"])
                if suggest:
                    detail["suggest"] = suggest
                out.append({
                    "kind": "schema_missing_column",
                    "severity": "warn" if role == "data" else "info",
                    "file": match, "sheet": sheet, "column": col,
                    "message": (_("Column «{column}» is declared but not found on sheet «{sheet}»",
                                  column=col, sheet=sheet)
                                + (_("; looks like «{suggest}»", suggest=suggest)
                                   if suggest else "")),
                    "detail": detail,
                })
            for col in sorted(sh["extra_columns"]):
                out.append({
                    "kind": "schema_extra_column", "severity": "info",
                    "file": match, "sheet": sheet, "column": col,
                    "message": _("Column «{column}» is in the file but not declared", column=col),
                    "detail": {},
                })

    for entry in skipped or []:
        if not isinstance(entry, dict) or entry.get("reason") != _UNEXPECTED_FILE_REASON:
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        out.append({
            "kind": "schema_unexpected_file", "severity": "info",
            "file": name, "sheet": None, "column": None,
            "message": _("File «{name}» is in the folder but doesn't match any input",
                         name=name),
            "detail": {"reason": entry["reason"]},
        })

    out.extend(_schema_unreadable_findings(files, by_match))
    return out


def _schema_unreadable_findings(files: list[dict], by_match: dict[str, dict]) -> list[dict]:
    """`schema_unreadable`: one finding per (file, sheet) whose `schema_json`
    (commit 633eaaa) says WHY no header was found — either a per-sheet
    `reason`, or a top-level `error` on a file that sniff() could not open at
    all (no per-sheet detail exists there: `sheets` is empty). A file whose
    sheets DID come back with their own reasons is reported per sheet only —
    the top-level "no header in any sheet: ..." echo would just be the first
    sheet's reason a second time.

    Severity follows the pattern's declared `role`, same rule as
    `schema_missing_column`: a `data` input missing its header is a warning,
    anything else (reference/legacy/unmatched) is informational. The one
    override is a capacity reason (`_is_capacity_reason`) — the engine chose
    not to read this schema, which is never evidence the FILE is broken, so
    it stays `info` regardless of role and carries a hint instead of an
    alarm."""
    out: list[dict] = []
    for f in files:
        schema = f.get("schema")
        if not isinstance(schema, dict):
            continue
        name = f.get("name")
        if not isinstance(name, str) or not name:
            continue
        match = f.get("matched_input")
        role = (by_match.get(match) or {}).get("role", "data") if isinstance(match, str) else "data"
        sheets = schema.get("sheets")
        top_err = schema.get("error")
        if isinstance(top_err, str) and top_err and not sheets:
            out.append(_schema_unreadable_finding(name, None, top_err, role))
        for sh in (sheets or []):
            if not isinstance(sh, dict):
                continue
            reason = sh.get("reason")
            if not (isinstance(reason, str) and reason):
                continue
            sheet_name = str(sh.get("name") or "")
            out.append(_schema_unreadable_finding(name, sheet_name, reason, role))
    return out


def _schema_unreadable_finding(file: str, sheet: str | None, reason: str, role: str) -> dict:
    capacity = _is_capacity_reason(reason)
    severity = "info" if capacity else ("warn" if role == "data" else "info")
    where = f"«{file}»" + (f" / {sheet}" if sheet else "")
    message = _("Header not found in {where}: {reason}", where=where, reason=reason)
    if capacity:
        message += _(" — increase the limit / use fewer sheets")
    return {
        "kind": "schema_unreadable", "severity": severity,
        "file": file, "sheet": sheet, "column": None,
        "message": message,
        "detail": {"reason": reason, "role": role, "capacity": capacity},
    }


# ---------------------------------------------------------------------------
# Impact analysis: who else reads this column
# ---------------------------------------------------------------------------

class _Impact:
    """`detail.affected` for the `schema_*` findings, from ONE lineage graph.

    The graph already knows which generators declare which sheet/column and
    which of them the read hooks saw being consumed. This class indexes that
    once and answers per finding; the honesty is in `confidence`, which never
    claims more than the evidence: an observed consume is dated by the run
    that observed it, a declaration is only a declaration, python-calamine
    can only prove a SHEET was opened, and the agent engine proves nothing
    (its SQL path, that is — a native folder run is as observable as the
    browser; `engines` therefore holds the RESOLVED engine).
    """

    def __init__(self, graph: dict, reports_by_gen: dict, engines: dict,
                 run_by_gen: dict, consumed_by_gen: dict) -> None:
        nodes = {n["id"]: n for n in graph["nodes"]}
        gen_of_sheet: dict[str, str] = {}
        for e in graph["edges"]:
            # A sheet's read edge lands either straight on the generator
            # ("reads", the plain case) or on a stage the generator declared
            # via onepage.stage() (lineage.EDGE_READ_ORDER == "read_order",
            # P4.1) — a stage node is labelled with the STAGE name, so the
            # generator has to be read off its "generator" data field instead.
            if e["kind"] not in ("reads", lineage.EDGE_READ_ORDER) \
                    or not e["from"].startswith("sheet:"):
                continue
            target = nodes.get(e["to"])
            if target is None:
                continue
            if target["kind"] == "generator":
                gen_of_sheet[e["from"]] = target["label"]
            elif target["kind"] == "stage":
                gen = (target.get("data") or {}).get("generator")
                if gen:
                    gen_of_sheet[e["from"]] = gen

        self.by_column: dict[tuple[str, str], dict[str, bool]] = {}
        self.by_sheet: dict[str, dict[str, bool]] = {}
        self.by_pattern: dict[str, set[str]] = {}
        for node in graph["nodes"]:
            if node["kind"] == "pattern":
                gen = (node.get("data") or {}).get("generator")
                if gen:
                    self.by_pattern.setdefault(node["label"], set()).add(gen)
                continue
            if node["kind"] != "sheet":
                continue
            gen = gen_of_sheet.get(node["id"])
            if not gen:
                continue
            skey = norm(node["label"])
            sheet_acc = self.by_sheet.setdefault(skey, {})
            sheet_acc[gen] = sheet_acc.get(gen, False)
            for entry in (node["data"].get("columns") or []):
                acc = self.by_column.setdefault((skey, norm(entry["name"])), {})
                consumed = bool(entry.get("consumed"))
                acc[gen] = acc.get(gen, False) or consumed
                if consumed:
                    sheet_acc[gen] = True

        self.reports_by_gen = reports_by_gen
        self.engines = engines
        self.run_by_gen = run_by_gen
        self.consumed_by_gen = consumed_by_gen

    def _confidence(self, gen: str, sheet: str | None, column: str | None,
                    consumed: bool) -> str:
        if self.engines.get(gen) == "agent":
            # The only truly blind path: SQL in a subprocess, nobody opens
            # files, there are no `_lineage.py` hooks there. Browser and
            # the native folder run (`local`) do not end up here — see
            # genmanifest.resolved_engine.
            return "unobservable"
        if not consumed:
            return "declared"
        if sheet is not None and column is not None:
            # `sheet` can be the DECLARED name, and a declaration may be a
            # glob ("Sheet*"); the consume index is keyed by the OBSERVED
            # sheet name. Match the two the way schemadiff does, or a glob
            # would silently skip the downgrade below.
            accs = self._consumed_for(gen, sheet)
            col = norm(column)
            if accs and not any(col in a["columns"] for a in accs) \
                    and any(a["all"] for a in accs):
                # python-calamine can only say the SHEET was opened.
                return "sheet_level"
        run_id = self.run_by_gen.get(gen)
        return f"consumed_as_of_run:{run_id}" if run_id else "declared"

    def _consumed_for(self, gen: str, sheet: str) -> list[dict]:
        """Every consume record of `gen` whose observed sheet name `sheet`
        admits — the exact key first, then the glob reading of it."""
        key = norm(sheet)
        exact = self.consumed_by_gen.get((gen, key))
        if exact is not None:
            return [exact]
        return [acc for (g, observed), acc in self.consumed_by_gen.items()
                if g == gen and globmatch.matches(key, observed)]

    def affected(self, sheet: str | None, column: str | None,
                 match: str | None) -> list[dict]:
        if sheet is not None and column is not None:
            hits = self.by_column.get((norm(sheet), norm(column)), {})
        elif sheet is not None:
            hits = self.by_sheet.get(norm(sheet), {})
        elif match is not None:
            hits = {g: False for g in self.by_pattern.get(match, set())}
        else:
            hits = {}
        out = []
        for gen in sorted(hits):
            out.append({
                "generator": gen,
                "reports": self.reports_by_gen.get(gen, []),
                "confidence": self._confidence(gen, sheet, column, hits[gen]),
            })
        return out


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------

def evaluate(db: Session, dashboards: list, *, now: datetime | None = None,
             history: int = HISTORY_RUNS, impact: bool = True) -> dict:
    """{"sources": [...], "findings": [...], "pending_values": [...],
    "history": {...}, "stats": {...}} for the dashboards in scope.

    `dashboards` is the caller's reach (`_generator_dashboards`); nothing
    outside it is queried, so the page a user sees is exactly the data they
    could have run themselves.

    `impact=False` skips the impact analysis (`detail.affected` is then
    absent from the schema findings) and with it a whole second lineage
    graph — the counters do not depend on it, so `badge_count` opts out.
    """
    now = now or datetime.utcnow()
    dash_ids = [d.id for d in dashboards]
    if not dash_ids:
        return _empty()

    runs_by_dash = datasources._recent_runs(db, dash_ids, limit=history)
    gen_ids = {d.generator_id for d in dashboards if d.generator_id}
    gen_names = dict(
        db.query(Generator.id, Generator.name).filter(Generator.id.in_(gen_ids)).all()
    ) if gen_ids else {}
    gid_by_name = {name: gid for gid, name in gen_names.items()}
    manifests = datasources._current_manifests(db, gen_ids)
    manifest_by_gen = {gen_names[gid]: manifests.get(gid) or {}
                       for gid in gen_ids if gid in gen_names}

    gen_name_by_dash = {d.id: gen_names.get(d.generator_id) for d in dashboards}
    dashes_by_gen: dict[str, list] = {}
    for d in dashboards:
        name = gen_name_by_dash.get(d.id)
        if name:
            dashes_by_gen.setdefault(name, []).append(d)
    reports_by_gen = {
        gen: sorted(({"id": d.id, "title": d.title} for d in ds), key=lambda r: r["id"])
        for gen, ds in dashes_by_gen.items()
    }
    dash_ids_by_gen = {gen: sorted(d.id for d in ds) for gen, ds in dashes_by_gen.items()}

    chains = lineage._collect_observations(db, dashboards, runs_by_dash, gen_name_by_dash)
    levels_by_pair = lineage.collect_chain_files(db, chains, depth=history) if chains else {}
    levels_by_pair = {k: v for k, v in levels_by_pair.items() if v}
    if not levels_by_pair:
        return _empty()

    ds_ids = {ds_id for (_gen, ds_id) in levels_by_pair}
    ds_rows = {row.id: row for row in
               db.query(DataSource).filter(DataSource.id.in_(ds_ids)).all()}

    # Overrides carry the confirmed column renames; schemadiff counts an
    # aliased column as found, so the alert stops without the generator
    # being edited.
    aliases_by_pair: dict[tuple[str, int], dict] = {}
    if gen_ids and ds_ids:
        for ov in (db.query(InputOverride)
                   .filter(InputOverride.data_source_id.in_(ds_ids),
                           InputOverride.generator_id.in_(gen_ids)).all()):
            gname = gen_names.get(ov.generator_id)
            if gname:
                aliases_by_pair[(gname, ov.data_source_id)] = ov.get_aliases()

    # Every quality_results row of the window, in one query. Keyed by
    # source_file_id, which is what the carry-forward walks.
    sf_ids: list[int] = []
    for levels in levels_by_pair.values():
        for level in levels:
            sf_ids.extend(f["source_file_id"] for f in level["files"]
                          if f.get("source_file_id"))
    qr_by_sf: dict[int, list[QualityResult]] = {}
    if sf_ids:
        for qr in (db.query(QualityResult)
                   .filter(QualityResult.source_file_id.in_(sf_ids))
                   .order_by(QualityResult.id).all()):
            qr_by_sf.setdefault(qr.source_file_id, []).append(qr)

    baselines: dict[tuple, ValueBaseline] = {}
    if ds_ids and gen_ids:
        for b in (db.query(ValueBaseline)
                  .filter(ValueBaseline.data_source_id.in_(ds_ids),
                          ValueBaseline.generator_id.in_(gen_ids)).all()):
            baselines[(b.data_source_id, b.generator_id, b.match, b.sheet, b.column)] = b

    skipped_by_obs = _skipped_by_obs(db, levels_by_pair)

    # ADR-16: every reference table this page could possibly talk about — the
    # platform inputs of the manifests in scope plus whatever the `ref_lookup`
    # checks point at (normally the same set; a frozen manifest can disagree
    # with itself and both halves still deserve an answer). Two queries for
    # the whole page, then `_ref_filter` is pure.
    ref_slugs: set[str] = set()
    for man in manifest_by_gen.values():
        for pi in genmanifest.platform_inputs(man or {}):
            ref_slugs.add(pi["table"])
        for c in genmanifest.checks_for(man or {}):
            if c.get("table"):
                ref_slugs.add(c["table"])
    ref_ctx = _ref_context(db, ref_slugs)

    findings: list[dict] = []
    pending: list[dict] = []
    history_out: dict[str, dict] = {}
    consumed_by_gen: dict[tuple[str, str], dict] = {}
    run_by_gen: dict[str, int] = {}
    # generator -> the ctx of its FIRST source (pairs are walked in
    # (generator, source id) order, so "first" is the lowest source id): what
    # a per-REPORT finding hangs off, since `rerun_needed` is about the report
    # and not about any one folder it reads.
    primary_ctx_by_gen: dict[str, dict] = {}

    for (gen, ds_id) in sorted(levels_by_pair, key=lambda k: (k[0], k[1])):
        levels = levels_by_pair[(gen, ds_id)]
        ds = ds_rows.get(ds_id)
        if ds is None:
            continue
        source_key = f"{ds.kind}:{ds.key_name}"
        manifest = manifest_by_gen.get(gen) or {}
        inputs = genmanifest.inputs_for(manifest)
        checks = {c["id"]: c for c in genmanifest.checks_for(manifest)}
        aliases = aliases_by_pair.get((gen, ds_id), {})
        ctx = {
            "source": source_key, "source_id": ds_id, "generator": gen,
            "dashboard_ids": dash_ids_by_gen.get(gen, []),
        }
        primary_ctx_by_gen.setdefault(gen, ctx)
        latest = levels[0]
        run_id = latest["obs"].run_id
        if gen not in run_by_gen:
            run_by_gen[gen] = run_id
        _index_consumed(consumed_by_gen, gen, latest["files"])

        findings.extend(_schema_block(levels, inputs, aliases, skipped_by_obs, ctx))
        findings.extend(_freshness_block(levels, inputs, ds, now, ctx))
        findings.extend(_checks_block(levels, qr_by_sf, checks, ctx))
        drift, pend = _drift_block(levels, qr_by_sf, ds_id, gid_by_name.get(gen),
                                   baselines, checks, ref_ctx, ctx)
        findings.extend(drift)
        pending.extend(pend)
        findings.extend(_dead_declarations(levels, inputs, ctx))

        history_out[f"{source_key}|{gen}"] = _history_series(levels)

    findings.extend(_rerun_block(db, dashboards, gen_name_by_dash, manifest_by_gen,
                                 primary_ctx_by_gen, ref_ctx, now))

    # ---- impact analysis, one graph for the whole page ------------------
    if impact and any(f["kind"].startswith("schema_") for f in findings):
        graph = lineage.build_graph(db, dashboards, now=now)
        # The RESOLVED engine, not the declared one: "auto" by itself says
        # nothing, and `unobservable` below is set from it. A folder run
        # (engine "local" at start) arrives with a browser/auto manifest without
        # `sources` and resolves to "browser" — it DOES send observations, and marking
        # it "unobservable" would throw away real evidence.
        engines = {gen: genmanifest.resolved_engine(manifest_by_gen.get(gen) or {})
                   for gen in dashes_by_gen}
        impact = _Impact(graph, reports_by_gen, engines, run_by_gen, consumed_by_gen)
        for f in findings:
            if f["kind"].startswith("schema_"):
                f["detail"]["affected"] = impact.affected(
                    f["sheet"], f["column"], f["file"])

    _apply_acks(db, findings, now)

    findings.sort(key=lambda f: (_SEV_RANK.get(f["severity"], 9), f["source"],
                                 f["generator"] or "", f["kind"], f["file"] or "",
                                 f["sheet"] or "", f["column"] or "", f["id"]))
    pending.sort(key=lambda p: (p["source_id"], p["generator"], p["match"],
                                p["sheet"], p["column"]))

    sources = _sources(ds_rows, findings)
    stats = {
        "sources_ok": sum(1 for s in sources if s["status"] == "ok"),
        "sources_warn": sum(1 for s in sources if s["status"] == "warn"),
        "sources_error": sum(1 for s in sources if s["status"] == "error"),
        "open": sum(1 for f in findings if not f["acked"]),
        "acked": sum(1 for f in findings if f["acked"]),
        "pending_values": len(pending),
        "stale": len({f["source"] for f in findings if f["kind"] == "file_stale"}),
    }
    return {"sources": sources, "findings": findings, "pending_values": pending,
            "history": history_out, "stats": stats}


def _empty() -> dict:
    return {"sources": [], "findings": [], "pending_values": [], "history": {},
            "stats": {"sources_ok": 0, "sources_warn": 0, "sources_error": 0,
                      "open": 0, "acked": 0, "pending_values": 0, "stale": 0}}


def _mk(ctx: dict, *, kind: str, severity: str, file: str | None, sheet: str | None,
        column: str | None, message: str, since_run: int, first_seen_at,
        detail: dict | None = None, extra: str | None = None) -> dict:
    return {
        "id": finding_id(kind, ctx["source"], ctx["generator"], file, sheet, column,
                         extra),
        "severity": severity, "kind": kind,
        "source": ctx["source"], "source_id": ctx["source_id"],
        "generator": ctx["generator"], "dashboard_ids": ctx["dashboard_ids"],
        "file": file, "sheet": sheet, "column": column,
        "message": message, "since_run": since_run,
        "first_seen_at": _iso(first_seen_at),
        "detail": dict(detail or {}), "acked": False, "ack": None,
    }


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

def _skipped_by_obs(db: Session, levels_by_pair: dict) -> dict[int, list]:
    """observation id -> its `skipped_json` list, for the newest level of each
    pair only (that is the only level `schema_unexpected_file` reports on)."""
    ids = {levels[0]["obs"].id for levels in levels_by_pair.values() if levels}
    out: dict[int, list] = {}
    if not ids:
        return out
    for oid, raw in (db.query(SourceObservation.id, SourceObservation.skipped_json)
                     .filter(SourceObservation.id.in_(ids)).all()):
        val = _loads(raw)
        out[oid] = val if isinstance(val, list) else []
    return out


def _schema_block(levels: list, inputs: list, aliases: dict,
                  skipped_by_obs: dict, ctx: dict) -> list[dict]:
    """Declared vs observed on the newest level, with `since_run` resolved by
    re-running the same comparison against the older ones until the id stops
    reproducing. The walk stops at the first level that does NOT carry the
    finding: a problem that came, went and came back is dated from its
    return, which is the reading a human would give it."""
    if not inputs:
        return []
    latest = levels[0]
    raw = _schema_findings(inputs, latest["files"], aliases,
                           skipped_by_obs.get(latest["obs"].id, []))
    if not raw:
        return []

    older_ids: list[tuple[set[str], SourceObservation]] = []
    for level in levels[1:]:
        found = _schema_findings(inputs, level["files"], aliases, [])
        ids = {finding_id(f["kind"], ctx["source"], ctx["generator"],
                          f["file"], f["sheet"], f["column"]) for f in found}
        older_ids.append((ids, level["obs"]))

    out = []
    for f in raw:
        fid = finding_id(f["kind"], ctx["source"], ctx["generator"],
                         f["file"], f["sheet"], f["column"])
        since, seen_at = latest["obs"].run_id, latest["obs"].created_at
        for ids, obs in older_ids:
            if fid not in ids:
                break
            since, seen_at = obs.run_id, obs.created_at
        out.append(_mk(ctx, kind=f["kind"], severity=f["severity"], file=f["file"],
                       sheet=f["sheet"], column=f["column"], message=f["message"],
                       since_run=since, first_seen_at=seen_at, detail=f["detail"]))
    return out


def _freshness_block(levels: list, inputs: list, ds: DataSource,
                     now: datetime, ctx: dict) -> list[dict]:
    """Freshness, completeness and row trends — the part a LEGACY generator
    still gets, because none of it needs a declaration: an observation knows
    how many files it saw, how new the newest one was, and (through the
    resolved schema) how many rows each sheet held."""
    out: list[dict] = []
    latest = levels[0]
    obs = latest["obs"]
    prev = levels[1] if len(levels) > 1 else None
    thresholds = ds.get_thresholds()
    drop_pct = thresholds["drop_pct"] if thresholds["drop_pct"] is not None else DROP_PCT_DEFAULT
    stale_days = ds.stale_after_days or STALE_DAYS_DEFAULT
    run_id, seen_at = obs.run_id, obs.created_at
    by_match = _input_by_match(inputs)

    # ---- file_stale ------------------------------------------------------
    if obs.newest_mtime is not None and obs.newest_mtime < now - timedelta(days=stale_days):
        age = max(0, (now - obs.newest_mtime).days)
        out.append(_mk(ctx, kind="file_stale", severity="warn", file=None, sheet=None,
                       column=None,
                       message=_("Data hasn't been updated for {days} days (threshold {threshold})",
                                 days=age, threshold=stale_days),
                       since_run=run_id, first_seen_at=seen_at,
                       detail={"age_days": age, "stale_after_days": stale_days,
                               "newest_mtime": _iso(obs.newest_mtime)}))

    # ---- file_count_drop -------------------------------------------------
    reasons: dict = {"file_count": obs.file_count}
    dropped = False
    if prev is not None and prev["obs"].file_count:
        floor = prev["obs"].file_count * (1 - drop_pct / 100.0)
        if obs.file_count < floor:
            dropped = True
            reasons.update({"previous": prev["obs"].file_count, "drop_pct": drop_pct})
    if ds.min_files is not None and obs.file_count < ds.min_files:
        dropped = True
        reasons["min_files"] = ds.min_files
    if dropped:
        out.append(_mk(ctx, kind="file_count_drop", severity="warn", file=None,
                       sheet=None, column=None,
                       message=_("Files in the source: {count}", count=obs.file_count)
                               + (_(", was {previous}", previous=reasons["previous"])
                                  if "previous" in reasons else "")
                               + (_(", minimum {min}", min=ds.min_files)
                                  if "min_files" in reasons else ""),
                       since_run=run_id, first_seen_at=seen_at, detail=reasons))

    # ---- required_file_gone ----------------------------------------------
    if prev is not None and inputs:
        now_by_match = _count_by_match(latest["files"], inputs)
        prev_by_match = _count_by_match(prev["files"], inputs)
        for match, item in sorted(by_match.items()):
            if not item.get("required"):
                continue
            if prev_by_match.get(match, 0) > 0 and now_by_match.get(match, 0) == 0:
                out.append(_mk(ctx, kind="required_file_gone", severity="error",
                               file=match, sheet=None, column=None,
                               message=_("Mandatory files «{pattern}» disappeared: was {previous}, now 0",
                                         pattern=match, previous=prev_by_match[match]),
                               since_run=run_id, first_seen_at=seen_at,
                               detail={"previous": prev_by_match[match]}))

    # ---- rows_zero / rows_drop / header_shift ----------------------------
    cur_sheets = _sheets_of(latest["files"])
    prev_sheets = _sheets_of(prev["files"]) if prev is not None else {}
    for (fname, sheet), info in sorted(cur_sheets.items()):
        match = info["match"] or _match_of(fname, inputs)
        item = by_match.get(match) if match else None
        if inputs and item is None:
            continue                     # a file no declared input admits
        role = (item or {}).get("role", "data")
        declared_sheet = _declares_sheet(item, sheet) if item else True
        # Use data-row counts (sheet height minus header) for zero/drop/min
        # checks — the raw "rows" is the physical sheet height and would call
        # a header-only export (0 data rows) non-empty.
        rows = info["data_rows"]

        if rows == 0 and (not inputs or (declared_sheet and role in ("data", "reference"))):
            out.append(_mk(ctx, kind="rows_zero",
                           severity="error" if (inputs and role == "data") else "warn",
                           file=fname, sheet=sheet, column=None,
                           message=_("Sheet «{sheet}» of file «{file}» is empty (0 rows)",
                                     sheet=sheet, file=fname),
                           since_run=run_id, first_seen_at=seen_at, detail={"rows": 0}))
        elif isinstance(rows, int):
            min_rows = thresholds["min_rows"].get(f"{match or '*'}/{sheet}")
            prev_rows = (prev_sheets.get((fname, sheet)) or {}).get("data_rows")
            detail: dict = {"rows": rows}
            hit = False
            if isinstance(prev_rows, int) and prev_rows > 0 and \
                    rows <= prev_rows * (1 - drop_pct / 100.0):
                hit = True
                detail.update({"previous": prev_rows, "drop_pct": drop_pct})
            if min_rows is not None and rows < min_rows:
                hit = True
                detail["min_rows"] = min_rows
            if hit:
                out.append(_mk(ctx, kind="rows_drop", severity="warn", file=fname,
                               sheet=sheet, column=None,
                               message=_("Rows on sheet «{sheet}»: {rows}",
                                         sheet=sheet, rows=rows)
                                       + (_(", was {previous}", previous=detail["previous"])
                                          if "previous" in detail else "")
                                       + (_(", minimum {min}", min=min_rows)
                                          if min_rows is not None else ""),
                               since_run=run_id, first_seen_at=seen_at, detail=detail))

        prev_info = prev_sheets.get((fname, sheet))
        if prev_info is not None and info["header_row"] is not None \
                and prev_info["header_row"] is not None \
                and info["header_row"] != prev_info["header_row"]:
            out.append(_mk(ctx, kind="header_shift", severity="info", file=fname,
                           sheet=sheet, column=None,
                           message=_("The header row on sheet «{sheet}» has shifted: {previous} → {now}",
                                     sheet=sheet, previous=prev_info["header_row"],
                                     now=info["header_row"]),
                           since_run=run_id, first_seen_at=seen_at,
                           detail={"header_row": info["header_row"],
                                   "previous": prev_info["header_row"]}))
    return out


def _declares_sheet(item: dict, sheet: str) -> bool:
    """An input that declares no sheets declares them all — the engine reads
    the workbook either way, so a row count on any of its sheets is on a
    sheet the generator uses."""
    declared = item.get("sheets") or []
    if not declared:
        return True
    return any(globmatch.matches(str(sh.get("name") or "*"), sheet) for sh in declared)


def _count_by_match(files: list[dict], inputs: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in files:
        match = f.get("matched_input") or _match_of(f["name"], inputs)
        if match:
            out[match] = out.get(match, 0) + 1
    return out


def _resolved_results(levels: list, qr_by_sf: dict) -> dict[str, list]:
    """file name -> the newest quality_results rows that are still TRUE of it.

    The engine does not re-check a file it did not re-open: an `unchanged`
    file arrives with `checks: undefined`, so the newest level has NO rows
    for it. Carrying the previous level's results forward is then the same
    contract `collect_chain_files` already applies to schemas — the bytes are
    the same, so the result of checking them is too.

    The carry-forward is only legal when the file really is the same file.
    Missing results do NOT only mean "unchanged": the worker drops the checks
    when it runs out of budget (CHECK_BUDGET_MS) and the engine strips them
    from an oversized payload, and in both cases the file WAS re-read and may
    have changed. So a donor is accepted only when the current file proves it
    is byte-identical — same size and mtime_ms — or, when the engine reported
    no stat at all, when it called the file `cached`. Anything else reports
    nothing rather than reporting last run's verdict on this run's file."""
    current = {f["name"]: f for f in (levels[0]["files"] if levels else [])}
    out: dict[str, list] = {}
    for name, cur in current.items():
        for level in levels:
            f = next((x for x in level["files"] if x["name"] == name), None)
            if f is None:
                continue
            rows = qr_by_sf.get(f.get("source_file_id") or -1)
            if not rows:
                continue
            if f is cur or _same_bytes(cur, f):
                out[name] = rows
            break                       # first donor only: older ones are staler
    return out


def _same_bytes(cur: dict, donor: dict) -> bool:
    """Is `cur` provably the same file as `donor`? Same size AND same mtime_ms,
    or — when the engine recorded no stat — its own word that it was cached."""
    if cur.get("size") is not None and cur.get("mtime_ms") is not None:
        return (cur["size"], cur["mtime_ms"]) == (donor.get("size"), donor.get("mtime_ms"))
    return cur.get("kind") == "cached"


def _checks_block(levels: list, qr_by_sf: dict, checks: dict, ctx: dict) -> list[dict]:
    """`check_fail` per (file, check_id) — a warn, never a block: the plan
    reserves `on_fail: "block"` and this version refuses it at registration.
    A `skipped` result is an info: the engine could not answer, which is not
    the same as an answer of "bad"."""
    out: list[dict] = []
    obs = levels[0]["obs"]
    for fname, rows in sorted(_resolved_results(levels, qr_by_sf).items()):
        for qr in sorted(rows, key=lambda r: r.check_id):
            if qr.check_type == "profile":
                continue
            spec = checks.get(qr.check_id) or {}
            label = spec.get("label")
            metric = qr.get_metric()
            if qr.status == "fail":
                out.append(_mk(
                    ctx, kind="check_fail", severity="warn", file=fname,
                    sheet=qr.sheet, column=qr.column,
                    message=_("{check}: {failed} of {count} rows failed the «{column}» check in «{file}»",
                              check=label or qr.check_type, failed=qr.failed or 0,
                              count=qr.count or 0, column=qr.column, file=fname),
                    since_run=obs.run_id, first_seen_at=obs.created_at,
                    detail={"count": qr.count, "failed": qr.failed,
                            "examples": metric.get("examples") or [],
                            "sampled": bool(qr.sampled), "label": label,
                            "type": qr.check_type, "check_id": qr.check_id},
                    extra=qr.check_id))
            elif qr.status == "skipped" and metric.get("reason") in _SKIPPED_REASONS:
                out.append(_mk(
                    ctx, kind="check_fail", severity="info", file=fname,
                    sheet=qr.sheet, column=qr.column,
                    message=_("{check}: the «{column}» check in «{file}» could not be run ({reason})",
                              check=label or qr.check_type, column=qr.column,
                              file=fname, reason=metric["reason"]),
                    since_run=obs.run_id, first_seen_at=obs.created_at,
                    detail={"count": qr.count, "failed": qr.failed, "examples": [],
                            "sampled": bool(qr.sampled), "label": label,
                            "type": qr.check_type, "check_id": qr.check_id,
                            "reason": metric["reason"]},
                    extra=qr.check_id))
    return out


# ---------------------------------------------------------------------------
# ADR-16 reference tables: the "values nobody mapped" side
# ---------------------------------------------------------------------------

def _ref_context(db: Session, slugs) -> dict:
    """{"tables": {slug: entry}, "by_id": {id: entry}, "keys": {slug: set}}
    for the given reference-table slugs, in TWO queries however many slugs
    there are — one for the tables (left-joined to the responsible user, so
    a name never costs a third), one for the rows of each table's CURRENT
    version.

    The keys are what makes an import close a finding WITHOUT a rerun: the
    engine recorded which values missed on the run, but whether they still
    miss is a question about the table as it is NOW, and this is where that
    is answered."""
    slugs = sorted({s for s in slugs if isinstance(s, str) and s})
    ctx: dict = {"tables": {}, "by_id": {}, "keys": {}}
    if not slugs:
        return ctx
    from sqlalchemy import and_, or_

    from app.models import ReferenceTable, ReferenceTableVersion, User
    from app.services import reference

    wanted: list[tuple[int, int]] = []
    for t, u in (db.query(ReferenceTable, User)
                 .outerjoin(User, ReferenceTable.responsible_user_id == User.id)
                 .filter(ReferenceTable.slug.in_(slugs)).all()):
        entry = {
            "id": t.id, "slug": t.slug, "title": t.title,
            "current_version": t.current_version or 0,
            "row_count": 0,
            "responsible": ({"id": u.id, "display_name": u.display_name or u.person_id}
                            if u is not None else None),
        }
        ctx["tables"][t.slug] = entry
        ctx["by_id"][t.id] = entry
        ctx["keys"][t.slug] = set()
        if entry["current_version"]:
            wanted.append((t.id, entry["current_version"]))
    if wanted:
        conds = [and_(ReferenceTableVersion.table_id == tid,
                      ReferenceTableVersion.version_number == vnum)
                 for tid, vnum in wanted]
        for tid, rows_json, row_count in (
                db.query(ReferenceTableVersion.table_id,
                         ReferenceTableVersion.rows_json,
                         ReferenceTableVersion.row_count)
                .filter(or_(*conds)).all()):
            entry = ctx["by_id"].get(tid)
            if entry is None:
                continue
            entry["row_count"] = row_count or 0
            rows = _loads(rows_json)
            if not isinstance(rows, list):
                rows = []
            ctx["keys"][entry["slug"]] = set(reference.keys_norm([], rows))
    return ctx


def _template_url(slug: str, values: list[str]) -> tuple[str, int]:
    """The «download a template with the new keys» link, and how many keys it
    actually carries. Keys travel as one newline-separated, URL-encoded
    parameter (a value may legitimately hold a comma).

    TWO ceilings, and the byte one is the binding one: MAX_TEMPLATE_KEYS is
    what the endpoint accepts, but 500 keys of Cyrillic at MAX_KEY_CHARS
    percent-encode to ~190 KB — far past nginx's 8 KB request line, so the
    link would 414 for exactly the tables that need it most, and the URL is
    stored in every finding's `detail` besides. So keys are added only while
    the encoded query stays inside MAX_TEMPLATE_URL_BYTES; the caller reports
    the honest `total` next to it, and the analyst who needs the rest exports
    the table itself.

    Returns (url, keys_in_url) so the UI can say the template is partial
    instead of quietly handing back fewer rows than the finding counts."""
    budget = MAX_TEMPLATE_URL_BYTES
    taken: list[str] = []
    used = 0
    for v in values[:MAX_TEMPLATE_KEYS]:
        # +3 for the encoded "\n" separator that joins it to the previous key.
        cost = len(urllib.parse.quote(v, safe="")) + (3 if taken else 0)
        if taken and used + cost > budget:
            break
        taken.append(v)
        used += cost
    q = urllib.parse.urlencode({"unmapped": "1", "keys": "\n".join(taken)})
    return f"/api/reference/{urllib.parse.quote(slug, safe='')}/template?{q}", len(taken)


def _ref_filter(groups: dict, ctx: dict) -> dict:
    """{slug: {"values": iterable of ORIGINAL strings, "overflow": bool}} ->
    {slug: summary}, dropping every value the table's CURRENT version now has
    a row for. `total` is the honest count of what is still unmapped;
    `values` is the capped list the UI shows and `more` the remainder.

    A slug with no table row at all is kept (`exists: False`) — «the manifest
    points at a reference table nobody has created» is a finding of its own,
    not silence."""
    from app.services import reference

    out: dict[str, dict] = {}
    for slug, acc in groups.items():
        entry = ctx["tables"].get(slug)
        keys = ctx["keys"].get(slug) or set()
        still = sorted({v for v in (acc.get("values") or [])
                        if reference.norm_key(v) not in keys})
        shown = still[:MAX_UNMAPPED_SHOWN]
        item = {
            "table": slug,
            "title": (entry or {}).get("title") or slug,
            "exists": entry is not None,
            "current_version": (entry or {}).get("current_version") or 0,
            "responsible": (entry or {}).get("responsible"),
            "overflow": bool(acc.get("overflow")),
            "values": shown,
            "total": len(still),
            "more": max(0, len(still) - len(shown)),
        }
        if entry is not None:
            item["reference_url"] = "/reference/" + urllib.parse.quote(slug, safe="")
            url, in_url = _template_url(slug, still)
            item["template_url"] = url
            item["template_keys"] = in_url
            item["template_partial"] = in_url < len(still)
        out[slug] = item
    return out


def _ref_missing(db: Session, groups: dict) -> dict:
    """`_ref_filter` with its own context — the entry point for a caller that
    has no context to share (publish's `run_summary`). `evaluate()` builds the
    context ONCE for the whole page and calls `_ref_filter` directly, so the
    two paths run the same filtering code without the same query cost."""
    if not groups:
        return {}
    return _ref_filter(groups, _ref_context(db, groups.keys()))


def _unmapped_findings(match: str, sheet: str, column: str, slug: str, acc: dict,
                       ref_ctx: dict, obs, ctx: dict) -> list[dict]:
    """The `unmapped_values` finding(s) for ONE (file pattern, sheet, column,
    reference table). Three outcomes, and they are deliberately different
    severities:

      * the table does not exist yet -> INFO. The manifest declares it, the
        run measured against nothing; nobody can fix values in a table that
        was never created, so this is a "create it" note, not an alarm.
      * the engine hit its `missing` ceiling -> INFO, and the list says so.
        An incomplete list must not read as a complete one.
      * values still missing from the CURRENT version -> WARN. Someone has
        to map them, and `detail.responsible` says who.

    `finding_id` gets the slug as `extra`, so two tables checked against one
    column stay two findings."""
    item = _ref_filter({slug: acc}, ref_ctx)[slug]
    detail = dict(item)
    detail["files"] = sorted(acc.get("files") or ())
    if not item["exists"]:
        return [_mk(ctx, kind="unmapped_values", severity="info", file=match,
                    sheet=sheet, column=column,
                    message=_("Reference table «{slug}» has not been created yet — there's nothing to check «{column}» against",
                              slug=slug, column=column),
                    since_run=obs.run_id, first_seen_at=obs.created_at,
                    detail=detail, extra=slug)]
    if not item["current_version"]:
        # ADR-16 (auto-created tables): the structure exists, the rows do not.
        # Reporting 500 "misses" here would be technically true and
        # useless — every key misses an empty table. ONE line that says what
        # to do, with the empty template already linked in `detail`.
        return [_mk(ctx, kind="unmapped_values", severity="info", file=match,
                    sheet=sheet, column=column,
                    message=_("Reference table «{title}» is empty — upload an Excel file, there's nothing to check «{column}» against yet",
                              title=item["title"], column=column),
                    since_run=obs.run_id, first_seen_at=obs.created_at,
                    detail=detail, extra=slug)]
    if item["overflow"]:
        return [_mk(ctx, kind="unmapped_values", severity="info", file=match,
                    sheet=sheet, column=column,
                    message=_("Reference table «{title}»: too many mismatches in «{column}» — the list is incomplete",
                              title=item["title"], column=column),
                    since_run=obs.run_id, first_seen_at=obs.created_at,
                    detail=detail, extra=slug)]
    if not item["total"]:
        return []
    shown = ", ".join(item["values"][:3])
    return [_mk(ctx, kind="unmapped_values", severity="warn", file=match,
                sheet=sheet, column=column,
                message=(_("Not in reference table «{title}» ({count}): {values}",
                           title=item["title"], count=item["total"], values=shown)
                         + ("…" if item["total"] > 3 else "")),
                since_run=obs.run_id, first_seen_at=obs.created_at,
                detail=detail, extra=slug)]


def _drift_block(levels: list, qr_by_sf: dict, ds_id: int, generator_id: int | None,
                 baselines: dict, checks: dict, ref_ctx: dict,
                 ctx: dict) -> tuple[list[dict], list[dict]]:
    """Categorical drift: the `known_values` distinct set against the accepted
    baseline. One `match` can span several files, and they all seed and
    compare against the SAME baseline row (that is the table's UNIQUE key),
    so the distinct sets are unioned before the comparison — exactly what
    `observations.finalize` does when it seeds."""
    out: list[dict] = []
    pending: list[dict] = []
    obs = levels[0]["obs"]
    groups: dict[tuple[str, str, str], dict] = {}
    # ADR-16: the same union, one level down — `missing` of a `ref_lookup`
    # keyed by the reference TABLE as well, because two tables may be checked
    # against one column and they are two different problems.
    ref_groups: dict[tuple[str, str, str, str], dict] = {}
    for fname, rows in _resolved_results(levels, qr_by_sf).items():
        for qr in rows:
            if qr.check_type not in ("known_values", "ref_lookup"):
                continue
            parsed = observations._parse_check_id(qr.check_id)
            if parsed is None:
                continue
            _ctype, match, sheet, column = parsed
            metric = qr.get_metric()
            if qr.check_type == "ref_lookup":
                slug = (checks.get(qr.check_id) or {}).get("table")
                if not slug:
                    continue          # frozen manifest lost the table: nothing to say
                racc = ref_groups.setdefault((match, sheet, column, slug),
                                             {"values": set(), "overflow": False,
                                              "files": set()})
                racc["files"].add(fname)
                if metric.get("missing_overflow"):
                    racc["overflow"] = True
                for v in (metric.get("missing") or []):
                    racc["values"].add(v)
                continue
            acc = groups.setdefault((match, sheet, column),
                                    {"distinct": set(), "overflow": False, "files": set()})
            acc["files"].add(fname)
            if metric.get("distinct_overflow"):
                acc["overflow"] = True
            for v in (metric.get("distinct") or []):
                acc["distinct"].add(v)

    for (match, sheet, column, slug), racc in sorted(ref_groups.items()):
        out.extend(_unmapped_findings(match, sheet, column, slug, racc, ref_ctx,
                                      obs, ctx))

    for (match, sheet, column), acc in sorted(groups.items()):
        if acc["overflow"]:
            out.append(_mk(ctx, kind="new_values", severity="info", file=match,
                           sheet=sheet, column=column,
                           message=_("Too many distinct values in «{column}» — reconciliation with the reference table was not performed",
                                     column=column),
                           since_run=obs.run_id, first_seen_at=obs.created_at,
                           detail={"distinct_overflow": True,
                                   "limit": genmanifest.MAX_KNOWN_VALUES}))
            continue
        baseline = baselines.get((ds_id, generator_id, match, sheet, column)) \
            if generator_id is not None else None
        if baseline is None:
            out.append(_mk(ctx, kind="new_values", severity="info", file=match,
                           sheet=sheet, column=column,
                           message=_("The reference set of values for «{column}» has not been set yet — there's nothing to check against", column=column),
                           since_run=obs.run_id, first_seen_at=obs.created_at,
                           detail={"baseline": None,
                                   "distinct": len(acc["distinct"])}))
            continue
        new = sorted(acc["distinct"] - set(baseline.get_values()))
        if not new:
            continue
        out.append(_mk(ctx, kind="new_values", severity="warn", file=match,
                       sheet=sheet, column=column,
                       message=_("New values in «{column}» ({count}): {values}",
                                 column=column, count=len(new),
                                 values=", ".join(new[:3]))
                               + ("…" if len(new) > 3 else ""),
                       since_run=obs.run_id, first_seen_at=obs.created_at,
                       detail={"new": new, "baseline_version": baseline.version,
                               "baseline_size": len(baseline.get_values()),
                               "files": sorted(acc["files"])}))
        pending.append({
            "source_id": ds_id, "generator": ctx["generator"],
            "generator_id": generator_id, "match": match, "sheet": sheet,
            "column": column, "new": new, "baseline_version": baseline.version,
        })
    return out, pending


def _rerun_block(db: Session, dashboards: list, gen_name_by_dash: dict,
                 manifest_by_gen: dict, primary_ctx_by_gen: dict, ref_ctx: dict,
                 now: datetime) -> list[dict]:
    """`rerun_needed` (ADR-16): a report whose last finished run resolved a
    reference table at a version that is no longer current, or ran WITHOUT a
    table that now has data.

    Per report, not per source: the thing to redo is the run. The severity
    split is the point — a stale VERSION is a warn (the published numbers are
    knowably behind an import somebody made), a run deliberately taken
    "without the reference table" is only an info (a legitimate mode ADR-16
    requires to keep working; the note exists so nobody forgets it is on).

    ONE query: `run_reference_uses` joined to its run, ordered by run id, so
    the last row per (report, table) IS the latest finished run's answer —
    no per-dashboard loop over the database.

    SUCCESSFUL runs only. `publish_run` is what marks a run success, so a
    RUN_SUCCESS row is exactly "this version of the report was built against
    that dictionary version"; an errored/canceled/lost run published nothing.
    Counting those would let a failed re-run silently CLOSE the warning about
    the version the report actually shows."""
    from app.models import RUN_SUCCESS, RunReferenceUse

    scope: dict[str, tuple[str, object, set]] = {}
    for d in dashboards:
        gen = gen_name_by_dash.get(d.id)
        if not gen or gen not in primary_ctx_by_gen:
            continue
        slugs = {pi["table"] for pi in
                 genmanifest.platform_inputs(manifest_by_gen.get(gen) or {})}
        if slugs:
            scope[d.id] = (gen, d, slugs)
    if not scope:
        return []

    latest: dict[tuple[str, int], tuple] = {}
    for dash_id, table_id, vnum, applied, run_id, started in (
            db.query(GenerationRun.dashboard_id, RunReferenceUse.table_id,
                     RunReferenceUse.version_number, RunReferenceUse.applied,
                     RunReferenceUse.run_id, GenerationRun.started_at)
            .join(GenerationRun, RunReferenceUse.run_id == GenerationRun.id)
            .filter(GenerationRun.dashboard_id.in_(scope.keys()),
                    GenerationRun.status == RUN_SUCCESS)
            .order_by(RunReferenceUse.run_id).all()):
        latest[(dash_id, table_id)] = (run_id, vnum, applied, started)

    out: list[dict] = []
    for (dash_id, table_id), (run_id, vnum, applied, started) in sorted(latest.items()):
        gen, d, slugs = scope[dash_id]
        entry = ref_ctx["by_id"].get(table_id)
        if entry is None or entry["slug"] not in slugs:
            continue                     # table dropped from the manifest since
        cur = entry["current_version"] or 0
        ctx = dict(primary_ctx_by_gen[gen])
        ctx["dashboard_ids"] = [dash_id]
        detail = {
            "dashboard_id": dash_id, "title": d.title, "slug": entry["slug"],
            "table_title": entry["title"], "used_version": vnum,
            "current_version": cur, "applied": bool(applied), "run_id": run_id,
            "responsible": entry["responsible"],
            "reference_url": "/reference/" + urllib.parse.quote(entry["slug"], safe=""),
            "dashboard_url": "/d/" + str(dash_id),
        }
        if applied and vnum is not None and cur and vnum < cur:
            out.append(_mk(
                ctx, kind="rerun_needed", severity="warn", file=None, sheet=None,
                column=None,
                message=_("«{dashboard}» is built on reference table «{title}» v{used}, current is v{current}: a recalculation is needed",
                          dashboard=d.title, title=entry["title"], used=vnum,
                          current=cur),
                since_run=run_id, first_seen_at=started, detail=detail,
                extra=f"{dash_id}:{entry['slug']}"))
        elif not applied and cur > 0:
            out.append(_mk(
                ctx, kind="rerun_needed", severity="info", file=None, sheet=None,
                column=None,
                message=_("«{dashboard}»: the last run was without reference table «{title}» (current version v{current})",
                          dashboard=d.title, title=entry["title"], current=cur),
                since_run=run_id, first_seen_at=started, detail=detail,
                extra=f"{dash_id}:{entry['slug']}"))
    return out


def _index_consumed(acc: dict, gen: str, files: list[dict]) -> None:
    """(generator, normalised sheet) -> {"all": bool, "columns": {norm}} from
    the newest level. Feeds the `sheet_level` verdict in impact analysis."""
    for f in files:
        consumed = f.get("consumed")
        if not isinstance(consumed, dict):
            continue
        for name, entry in (consumed.get("sheets") or {}).items():
            if not isinstance(entry, dict):
                continue
            slot = acc.setdefault((gen, norm(str(name))), {"all": False, "columns": set()})
            slot["all"] = slot["all"] or bool(entry.get("all"))
            for c in (entry.get("columns") or []):
                if isinstance(c, str):
                    slot["columns"].add(norm(c))


def _dead_declarations(levels: list, inputs: list, ctx: dict) -> list[dict]:
    """A declared column that IS in the file and that no run ever read.

    Only emitted when the chain contains at least one level where the file
    was actually parsed AND the read hooks recorded something (`kind ==
    "parsed"` and `consumed_json` present) — otherwise "never consumed" only
    means "nothing could have observed it", which is the tri-state's null and
    not a finding. `detail.runs_checked` says how much evidence there is.
    """
    if not inputs:
        return []
    by_match = _input_by_match(inputs)
    runs_checked = 0
    seen: dict[tuple[str, str], set[str]] = {}      # (match, norm sheet) -> consumed cols
    sheets_evidenced: set[tuple[str, str]] = set()
    for level in levels:
        evidence = False
        for f in level["files"]:
            if f.get("kind") != "parsed" or not isinstance(f.get("consumed"), dict):
                continue
            match = f.get("matched_input") or _match_of(f["name"], inputs)
            if match not in by_match:
                continue
            evidence = True
            for name, entry in (f["consumed"].get("sheets") or {}).items():
                if not isinstance(entry, dict):
                    continue
                key = (match, norm(str(name)))
                sheets_evidenced.add(key)
                acc = seen.setdefault(key, set())
                if entry.get("all"):
                    acc.add("*")
                for c in (entry.get("columns") or []):
                    if isinstance(c, str):
                        acc.add(norm(c))
        if evidence:
            runs_checked += 1
    if not runs_checked:
        return []

    headers: dict[tuple[str, str], set[str]] = {}
    for f in levels[0]["files"]:
        schema = f.get("schema")
        if not isinstance(schema, dict):
            continue
        match = f.get("matched_input") or _match_of(f["name"], inputs)
        if match not in by_match:
            continue
        for sh in (schema.get("sheets") or []):
            if not isinstance(sh, dict):
                continue
            key = (match, norm(str(sh.get("name") or "")))
            acc = headers.setdefault(key, set())
            for h in (sh.get("header") or []):
                if isinstance(h, str) and h.strip():
                    acc.add(norm(h))

    out: list[dict] = []
    obs = levels[0]["obs"]
    for match, item in sorted(by_match.items()):
        for sh in (item.get("sheets") or []):
            sheet_glob = str(sh.get("name") or "*")
            for col in sorted({str(c) for c in (sh.get("columns") or []) if isinstance(c, str)}):
                key_cols = norm(col)
                # Only sheets that carry the column AND have consumption
                # evidence can say anything about it.
                relevant = [k for k in sheets_evidenced
                            if k[0] == match and globmatch.matches(sheet_glob, k[1])
                            and key_cols in headers.get(k, set())]
                if not relevant:
                    continue
                if any(key_cols in seen.get(k, set()) or "*" in seen.get(k, set())
                       for k in relevant):
                    continue
                out.append(_mk(ctx, kind="dead_declaration", severity="info",
                               file=match, sheet=sheet_glob, column=col,
                               message=_("Column «{column}» is declared and present in the file, but the generator never read it",
                                         column=col),
                               since_run=obs.run_id, first_seen_at=obs.created_at,
                               detail={"runs_checked": runs_checked}))
    return out


def _history_series(levels: list) -> dict:
    """{"runs": [...], "file_count": [...], "rows": {"<file>/<sheet>": [...]}}
    — newest run first, every list the same length as `runs`, `null` where a
    level had no number for that key. The page draws sparklines from this and
    nothing else, so alignment is the contract."""
    runs = [level["obs"].run_id for level in levels]
    file_count = [level["obs"].file_count for level in levels]
    per_level = [_sheets_of(level["files"]) for level in levels]
    keys = sorted({k for sheets in per_level for k in sheets})
    rows = {f"{fname}/{sheet}": [(sheets.get((fname, sheet)) or {}).get("rows")
                                 for sheets in per_level]
            for (fname, sheet) in keys}
    return {"runs": runs, "file_count": file_count, "rows": rows}


def _apply_acks(db: Session, findings: list[dict], now: datetime) -> None:
    """An ack is a human saying "I know". It never hides the finding — it
    stops it counting as open. Bounded two ways, and BOTH bounds must still
    hold: `until_at` in the future, and `until_run_id` at or after the run
    the finding has been reproducing since (so an ack "until the next run"
    expires the moment the problem is seen on a newer run).

    Honest limit for the UI on top of this: `since_run` of a `schema_*`
    finding is the OLDEST run of the current unbroken reproduction, and it
    stays pinned there while the problem persists. So `until_run_id` set to
    the run visible at ack time never falls behind it, and an ack "until the
    next run" on a schema finding is in practice permanent until the schema
    is fixed. Offer schema acks with `until_at` (a date) — or spell the
    behaviour out — rather than a "next run" button that never expires."""
    if not findings:
        return
    ids = {f["id"] for f in findings}
    rows = (db.query(QualityAck).filter(QualityAck.finding_id.in_(ids))
            .order_by(QualityAck.id.desc()).all())
    by_id: dict[str, list[QualityAck]] = {}
    for ack in rows:
        by_id.setdefault(ack.finding_id, []).append(ack)
    for f in findings:
        for ack in by_id.get(f["id"], []):
            # An ack is stored against ONE source (`data_source_id`, the
            # authorisation check at write time). finding_id alone is not
            # unique across sources — reusing the same evaluator inputs on a
            # different source can hash to the same 16 hex chars — so an ack
            # whose source does not match this finding's source must never
            # apply here. Without this a user with reach to source B could
            # POST an ack naming source B but a finding_id observed on
            # source A and silently suppress A's finding for everyone.
            if ack.data_source_id is not None and ack.data_source_id != f["source_id"]:
                continue
            if ack.until_at is not None and ack.until_at <= now:
                continue
            if ack.until_run_id is not None and ack.until_run_id < f["since_run"]:
                continue
            f["acked"] = True
            f["ack"] = {"id": ack.id, "note": ack.note, "until_at": _iso(ack.until_at),
                        "until_run_id": ack.until_run_id, "created_by": ack.created_by}
            break


def _sources(ds_rows: dict, findings: list[dict]) -> list[dict]:
    """One row per source in the window; `status` is the worst severity among
    its OPEN findings, so an acked error does not keep a source red.

    `status` is ok|warn|error and nothing else: an `info` finding (an extra
    column, a shifted header, a dead declaration) is worth listing and worth
    counting in `open`, but it is not a reason to colour a source — a page
    where every source is amber because a workbook grew a column is a page
    nobody reads."""
    by_source: dict[str, list[dict]] = {}
    for f in findings:
        by_source.setdefault(f["source"], []).append(f)
    out = []
    for ds in ds_rows.values():
        key = f"{ds.kind}:{ds.key_name}"
        mine = by_source.get(key, [])
        open_f = [f for f in mine if not f["acked"]]
        status = "ok"
        for sev in ("error", "warn"):
            if any(f["severity"] == sev for f in open_f):
                status = sev
                break
        row = {
            "key": key, "name": ds.key_name, "source_id": ds.id, "status": status,
            "findings": len(mine), "open": len(open_f), "version": ds.version,
            # Raw (possibly-null) column, so the editor can tell "inherits the
            # installation default" from "explicitly set to N" and round-trip
            # a blank field as null instead of pinning the default in place.
            "stale_after_days": ds.stale_after_days,
            "stale_after_days_default": STALE_DAYS_DEFAULT,
            "min_files": ds.min_files, "thresholds": ds.get_thresholds(),
        }
        # owner_id is the Impact-Analysis addendum's column; tolerated as
        # absent so this module boots against a DB that predates it.
        if hasattr(ds, "owner_id"):
            row["owner_id"] = ds.owner_id
        out.append(row)
    out.sort(key=lambda s: s["key"])
    return out


# ---------------------------------------------------------------------------
# Publish + badge
# ---------------------------------------------------------------------------

def run_summary(db: Session, run) -> dict:
    """What ONE run's checks found, for the publish warnings.

    Deliberately NOT `evaluate()` on one dashboard: publish happens while the
    run's own results are the freshest thing in the database and the question
    is narrow ("did this run's files fail anything, did they bring values
    nobody has accepted"). No chain walk, no schema diff — publish already
    reports the schema diff through `genmanifest.publish_warnings`.
    """
    out: dict = {"failed_checks": [], "new_values": [], "unmapped": [],
                 "quality_url": "/data-quality"}
    obs_rows = (db.query(SourceObservation)
                .filter(SourceObservation.run_id == run.id).all())
    gen_row = (db.query(GeneratorVersion.generator_id, GeneratorVersion.manifest_json)
               .filter(GeneratorVersion.id == run.generator_version_id).first())
    generator_id = gen_row[0] if gen_row else None
    manifest = _loads(gen_row[1]) if gen_row else None
    manifest = manifest if isinstance(manifest, dict) else {}
    checks = {c["id"]: c for c in genmanifest.checks_for(manifest)}
    gen_name = None
    if generator_id is not None:
        row = db.query(Generator.name).filter(Generator.id == generator_id).first()
        gen_name = row[0] if row else None
    if gen_name:
        out["quality_url"] = "/data-quality?generator=" + urllib.parse.quote(str(gen_name), safe="")
    if not obs_rows:
        return out

    obs_by_id = {o.id: o for o in obs_rows}
    sf_names = dict(db.query(SourceFile.id, SourceFile.name)
                    .filter(SourceFile.observation_id.in_(obs_by_id)).all())
    rows = (db.query(QualityResult)
            .filter(QualityResult.observation_id.in_(obs_by_id))
            .order_by(QualityResult.id).all()) if sf_names else []

    for qr in rows:
        if qr.check_type == "profile" or qr.status != "fail":
            continue
        spec = checks.get(qr.check_id) or {}
        out["failed_checks"].append({
            "file": sf_names.get(qr.source_file_id), "sheet": qr.sheet,
            "column": qr.column, "type": qr.check_type,
            "label": spec.get("label"), "failed": qr.failed, "count": qr.count,
        })
    out["failed_checks"].sort(key=lambda r: (r["file"] or "", r["sheet"] or "",
                                             r["column"] or "", r["type"]))

    if generator_id is not None:
        groups: dict[tuple[int, str, str, str], set[str]] = {}
        files_of: dict[tuple[int, str, str, str], set[str]] = {}
        for qr in rows:
            if qr.check_type != "known_values":
                continue
            obs = obs_by_id.get(qr.observation_id)
            parsed = observations._parse_check_id(qr.check_id)
            if obs is None or parsed is None:
                continue
            _ctype, match, sheet, column = parsed
            distinct = qr.get_metric().get("distinct")
            if distinct is None:
                continue
            key = (obs.data_source_id, match, sheet, column)
            groups.setdefault(key, set()).update(distinct)
            files_of.setdefault(key, set()).add(sf_names.get(qr.source_file_id) or "")
        # One query for every baseline of this generator over the run's data
        # sources; publish is a write path and must not loop over queries.
        by_key: dict[tuple, ValueBaseline] = {}
        if groups:
            for b in (db.query(ValueBaseline)
                      .filter(ValueBaseline.generator_id == generator_id,
                              ValueBaseline.data_source_id.in_(
                                  {k[0] for k in groups})).all()):
                by_key[(b.data_source_id, b.match, b.sheet, b.column)] = b
        for key, values in sorted(groups.items()):
            (ds_id, match, sheet, column) = key
            baseline = by_key.get(key)
            if baseline is None:
                continue                 # seeded by this very run: not news
            new = sorted(values - set(baseline.get_values()))
            if not new:
                continue
            out["new_values"].append({
                "file": match, "sheet": sheet, "column": column,
                "values": new[:5], "more": max(0, len(new) - 5),
            })

    # ADR-16 `unmapped`: values THIS run brought that the reference table has
    # no row for, re-checked against the table as it is NOW (an import made
    # between the run and the publish has already closed them). Same filter
    # the Data Quality feed uses — `_ref_missing` is the one implementation.
    ref_groups: dict[str, dict] = {}
    for qr in rows:
        if qr.check_type != "ref_lookup":
            continue
        slug = (checks.get(qr.check_id) or {}).get("table")
        if not slug:
            continue
        metric = qr.get_metric()
        acc = ref_groups.setdefault(slug, {"values": set(), "overflow": False})
        if metric.get("missing_overflow"):
            acc["overflow"] = True
        acc["values"].update(metric.get("missing") or [])
    for slug, item in sorted(_ref_missing(db, ref_groups).items()):
        if not item["total"] and not item["overflow"]:
            continue
        resp = item.get("responsible") or None
        out["unmapped"].append({
            "slug": slug, "title": item["title"],
            # `exists`/`current_version` are what tell "no such reference table"
            # from "reference table is empty" from "there are misses" in the publish
            # note (genmanifest.publish_warnings) — the count alone cannot.
            "exists": item["exists"],
            "current_version": item["current_version"],
            "values": item["values"][:5],
            "more": max(0, item["total"] - min(5, len(item["values"]))),
            "total": item["total"],
            "responsible": resp["display_name"] if resp else None,
        })
    return out


def badge_count(db: Session, dashboards: list, *, now: datetime | None = None) -> int:
    """Open findings in this user's scope — the number on the bell. Same
    evaluator, same scope rules; the API layer is what caches it. Impact
    analysis is skipped: it costs a second lineage graph and contributes
    nothing to the count."""
    return evaluate(db, dashboards, now=now, impact=False)["stats"]["open"]


def prune(db: Session, *, keep_per_source: int = 30, keep_weekly_days: int = 180,
          now: datetime | None = None) -> dict:
    """Retention for `source_files` / `quality_results` (P4.3). Observations
    themselves (`source_observations`), baselines, acks, annotations and
    run_stages are never touched here — this only thins the per-FILE detail
    that backs `evaluate()`'s schema diff and check history, not the
    per-RUN measurements (file_count/rows/fingerprint) the trend lines read.

    Per data source, newest-first: the `keep_per_source` most recent
    observations keep their files/results untouched — except `evaluate()`'s
    live window is `HISTORY_RUNS` runs per DASHBOARD (`datasources._recent_runs`
    partitions `limit` by dashboard_id), so a source read by N dashboards can
    have up to `HISTORY_RUNS * N` observations inside that window. Pruning
    fewer than that would delete `source_files` that `evaluate()` still reads,
    corrupting `since_run` and the rows trend — so the effective floor per
    source is `max(keep_per_source, HISTORY_RUNS * distinct dashboards reading
    it)`, computed below from `SourceObservation` joined to `GenerationRun`.
    Older than that, within `keep_weekly_days` days, one observation per ISO
    week (the newest one in that week) is also kept in full — a fortnightly
    report still shows a monotonic trend, just at weekly resolution. Anything
    older than `keep_weekly_days`, and every other observation in a week that
    already has its keeper, loses its `source_files` (and, via cascade-by-hand,
    the `quality_results` that hang off those files).

    **Carry-forward guard.** `evaluate()` / `lineage.collect_chain_files()`
    resolve a `SourceFile.schema_json` of NULL ("unchanged since last look")
    by walking back through OLDER observations of the same source for the
    nearest row with the same `name` that still carries a schema. If that
    donor row lived in an observation this function would otherwise prune,
    the walk would come up empty for every kept observation newer than it.
    So before deleting anything, for every (source, file name) this scans
    ALL observations of that source (not just the pruned ones) and protects
    the single newest `SourceFile` row that has a non-NULL `schema_json` —
    the exact row a future walk-back would land on — regardless of which
    window it falls in.
    """
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=keep_weekly_days)
    stats = {"sources": 0, "observations_pruned": 0, "files_deleted": 0, "results_deleted": 0}

    source_ids = [row[0] for row in
                  db.query(SourceObservation.data_source_id).distinct().all()]

    # Distinct dashboards that have ever posted an observation for each
    # source — the read window's real width, per the docstring above.
    dash_sets: dict[int, set] = {}
    for source_id, dash_id in (
            db.query(SourceObservation.data_source_id, GenerationRun.dashboard_id)
            .join(GenerationRun, GenerationRun.id == SourceObservation.run_id)
            .distinct().all()):
        dash_sets.setdefault(source_id, set()).add(dash_id)
    dash_counts = {sid: len(dset) for sid, dset in dash_sets.items()}

    for source_id in source_ids:
        obs_rows = (db.query(SourceObservation.id, SourceObservation.created_at)
                    .filter(SourceObservation.data_source_id == source_id)
                    .order_by(SourceObservation.created_at.desc(), SourceObservation.id.desc())
                    .all())
        if not obs_rows:
            continue
        stats["sources"] += 1

        keep_effective = max(keep_per_source, HISTORY_RUNS * dash_counts.get(source_id, 1))
        keep_full: set[int] = {oid for oid, _created in obs_rows[:keep_effective]}
        seen_weeks: set[tuple] = set()
        for oid, created in obs_rows[keep_per_source:]:
            created = created or now
            if created < cutoff:
                continue  # past the weekly-thinning window entirely: prune it
            wk = created.isocalendar()[:2]  # (iso_year, iso_week) — one keeper/week
            if wk not in seen_weeks:
                seen_weeks.add(wk)
                keep_full.add(oid)

        prune_ids = [oid for oid, _created in obs_rows if oid not in keep_full]
        if not prune_ids:
            continue

        # Schema-donor guard, see docstring: newest (source, name) row that
        # still has a schema, across every observation of this source.
        obs_rank = {oid: rank for rank, (oid, _created) in enumerate(obs_rows)}  # 0 = newest
        all_obs_ids = [oid for oid, _created in obs_rows]
        donor_rank: dict[str, int] = {}
        donor_file_id: dict[str, int] = {}
        for i in range(0, len(all_obs_ids), 500):
            chunk = all_obs_ids[i:i + 500]
            for fid, oid, name in (
                    db.query(SourceFile.id, SourceFile.observation_id, SourceFile.name)
                    .filter(SourceFile.observation_id.in_(chunk),
                            SourceFile.schema_json.isnot(None)).all()):
                rank = obs_rank.get(oid)
                if rank is None:
                    continue
                if name not in donor_rank or rank < donor_rank[name]:
                    donor_rank[name] = rank
                    donor_file_id[name] = fid
        protected_file_ids = set(donor_file_id.values())

        delete_file_ids: list[int] = []
        for i in range(0, len(prune_ids), 500):
            chunk = prune_ids[i:i + 500]
            for (fid,) in (db.query(SourceFile.id)
                           .filter(SourceFile.observation_id.in_(chunk)).all()):
                if fid not in protected_file_ids:
                    delete_file_ids.append(fid)
        if not delete_file_ids:
            continue

        for i in range(0, len(delete_file_ids), 500):
            chunk = delete_file_ids[i:i + 500]
            stats["results_deleted"] += (
                db.query(QualityResult)
                .filter(QualityResult.source_file_id.in_(chunk))
                .delete(synchronize_session=False))
            stats["files_deleted"] += (
                db.query(SourceFile)
                .filter(SourceFile.id.in_(chunk))
                .delete(synchronize_session=False))
        stats["observations_pruned"] += len(prune_ids)
        # Commit per source, not once for the whole run: a single transaction
        # spanning every source could hold SQLite's write lock past
        # busy_timeout while a concurrent observation ingest waits on it.
        db.commit()

    return stats
