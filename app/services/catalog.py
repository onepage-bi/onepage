r"""Leg 1 — canonical table inference over observed headers.

This module is the ONE implementation of the algorithm validated by Leg 0.
`tools/lake_probe.py` is now a CLI + gate report on top of `infer()`; if the
probe and the service ever disagreed about what a table is, the gate would be
measuring something the product does not do.

What it does
------------
1. `load_corpus()` builds the (generator, data_source) observation chains the
   lineage page builds and resolves NULL `schema_json` through
   `lineage.collect_chain_files` — the engine writes NULL for "unchanged since
   the previous observation", so a cached file's header lives on an older row.
2. One signature per (FILE, SHEET) — never per sheet name. Three different 1C
   exports all call their sheet `TDSheet`; merging by sheet name is the bug
   this whole plan exists to fix.
3. `classify_unit()` splits `table` from `pivot`/`fragment` BEFORE clustering.
   Production workbooks are full of pivot corners and one-column leftovers;
   each used to become its own "table". The rejects are not thrown away — they
   are the "Header not recognized" corpus, stored in `catalog_nontables`.
4. `cluster_units()` — deterministic greedy agglomeration inside ONE
   `data_source_id` (0.6 containment + 0.25 jaccard + 0.15 order-LCS, hard
   split on header row / type conflicts, join at 0.85, second pass merging
   centroids at 0.92). Deterministic ordering is what makes `table_id` stable.
5. `name_clusters()` — name from the common token block of the file names with
   date/number tokens generalised, per-file partition key from the tokens that
   were dropped, stable `table_id = "t_" + sha1(ds|name|sha1(core))[:12]`.
6. `rebuild()` — compute-on-write into `catalog_tables` / `catalog_partitions`
   / `catalog_consumers` / `catalog_nontables`, under a time budget.

Status semantics (ADR-12, `docs/PERF_DECISIONS.md`)
---------------------------------------------------
G1 and G4 FAILED on production, so by the fallback declared before the run the
inference ships as a **SUGGESTER**: every row lands as `inferred` and only a
human turns it into `confirmed`. Nothing here ever writes a manifest — see
`suggest_inputs()`, which hands back a PATCH plus the list of files that patch
would drop, for a human to load as a new generator version.

Thresholds are the ones registered before the production run. The env
overrides exist to explore sensitivity; changing a default is an ADR.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import (
    CatalogConsumer, CatalogNonTable, CatalogPartition, CatalogTable,
    Dashboard, DataSource, Generator, GenerationRun, QualityResult,
    SourceObservation,
)
from app.services import lineage
from app.services.lineage_export import _translit as translit
from app.services.schemadiff import norm

# ───────────────────────────── registered thresholds ─────────────────────────
W_CONTAINMENT = 0.60
W_JACCARD = 0.25
W_ORDER_LCS = 0.15
JOIN_MIN = float(os.environ.get("OP_LAKE_SIM_MIN", 0.85))           # greedy join
MERGE_MIN = float(os.environ.get("OP_LAKE_MERGE_MIN", 0.92))        # centroid merge
TYPE_CONFLICT_MAX = float(os.environ.get("OP_LAKE_TYPE_CONFLICT", 0.20))
MODE_RATIO = 0.50      # centroid = columns present in >= 50 % of members
CORE_RATIO = 0.80      # core    = columns present in >= 80 % of members

DEFAULTS = {"OP_LAKE_SIM_MIN": 0.85, "OP_LAKE_MERGE_MIN": 0.92,
            "OP_LAKE_TYPE_CONFLICT": 0.20}

# A set smaller than this is too small for containment to mean anything:
# |A∩B| / min(|A|,|B|) is 1.0 for every 1-column sheet that happens to share
# its single header. Below the floor we fall back to jaccard.
MIN_CONTAINMENT_SET = 3

MAX_EVIDENCE = 20          # evidence rows kept per table
MAX_NONTABLE_HEADERS = 16  # headers kept per rejected sheet
MAX_COLUMNS_STORED = 512   # columns kept per table row

STATUS_INFERRED = "inferred"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_INFERRED, STATUS_CONFIRMED, STATUS_REJECTED)


# ───────────────────────────── signatures ────────────────────────────────────

_UNNAMED_RE = re.compile(r"^unnamed:\s*\d+(\.\d+)?$")
_DEDUPE_SUFFIX_RE = re.compile(r"\.\d+$")
_WS_RE = re.compile(r"\s+")


def norm_col(raw: str) -> str | None:
    """One column header -> its canonical key, or None if it is an artefact.

    `schemadiff.norm` is the SAME normalisation Data Quality uses (NFC, strip,
    casefold) — the catalogue and DQ must never disagree about "the same
    column". On top of it: whitespace collapsed, and pandas' two ways of
    naming what the workbook did not name dropped/undone — `Unnamed: 3` for a
    blank header, a `.1` tail for a duplicate one.
    """
    if not isinstance(raw, str):
        return None
    key = _WS_RE.sub(" ", norm(raw))
    if not key:
        return None
    if _UNNAMED_RE.match(key):
        return None
    key = _DEDUPE_SUFFIX_RE.sub("", key).strip()
    return key or None


class Sig:
    """S / order / hdr / types of one (file, sheet) — or of a centroid.

    `disp` keeps the human-readable header behind each normalised key: the
    catalogue shows what the workbook says, matches on what DQ says."""

    __slots__ = ("S", "order", "hdr", "types", "disp")

    def __init__(self, S, order, hdr, types, disp=None):
        self.S = frozenset(S)
        self.order = tuple(order)
        self.hdr = hdr
        self.types = dict(types or {})
        self.disp = dict(disp or {})


# ───────────────────────────── unit classification ───────────────────────────
#
# Production is not a lake of tables. It is a lake of tables PLUS the pivot
# sheets, summary corners and one-column leftovers an analyst keeps in the same
# workbook. So a (file, sheet) is classified BEFORE clustering and only a
# `table` is clustered at all; the rest are the population the product greets
# with "Header not recognized" (`catalog_nontables`).

# The Russian entries below are DATA, not interface text: the labels Russian-
# locale Excel writes into pivot tables, matched to recognise such exports.
PIVOT_MARKERS = frozenset({
    "row labels", "column labels", "grand total", "(all)",
    "названия строк", "названия столбцов", "общий итог",
})
PIVOT_PREFIXES = ("sum of ", "count of ", "average of ", "max of ", "min of ",
                  "сумма по полю", "количество по полю")
MIN_TABLE_COLUMNS = 3
PIVOT_NUMERIC_SHARE = 0.50
_NUMERIC_HEADER_RE = re.compile(r"^[-+]?\d+(?:[.,]\d+)?$")


def classify_unit(sig: "Sig") -> tuple[str, str]:
    """-> ("table" | "pivot" | "fragment", why).

    fragment: fewer than 3 non-empty normalised columns — there is no table
              here to recognise, only a corner of one.
    pivot:    at least one pivot marker in the header, or half the header is
              bare numbers (Excel's serial dates and year columns, i.e. the
              crosstab's value axis pretending to be a schema).
    """
    cols = list(sig.order)
    if len(cols) < MIN_TABLE_COLUMNS:
        return "fragment", f"{len(cols)} normalised column(s) < {MIN_TABLE_COLUMNS}"
    hits = sorted(c for c in cols
                  if c in PIVOT_MARKERS or c.startswith(PIVOT_PREFIXES))
    if hits:
        return "pivot", "pivot marker: " + ", ".join(hits[:3])
    numeric = [c for c in cols if _NUMERIC_HEADER_RE.match(c)]
    if len(numeric) / len(cols) >= PIVOT_NUMERIC_SHARE:
        return "pivot", (f"{len(numeric)}/{len(cols)} headers are bare numbers "
                         f"(e.g. {', '.join(numeric[:3])})")
    return "table", ""


class Unit:
    """One (data_source, file name, sheet) — the thing that gets clustered.

    A file is observed once per run; the signature we keep is the one from the
    NEWEST observation that carried a schema, and everything else (which runs
    saw it, which generators, what size/mtime it had) accumulates across all
    of them."""

    __slots__ = ("ds_id", "name", "sheet", "sig", "runs", "generators",
                 "stamps", "file_ids", "_best_key", "rows", "profile_metrics",
                 "run_id", "file_id", "size", "mtime_ms", "matched_input")

    def __init__(self, ds_id, name, sheet):
        self.ds_id = ds_id
        self.name = name
        self.sheet = sheet
        self.sig = None
        self.rows = None
        self.runs = set()
        self.generators = set()
        self.stamps = set()          # (size, mtime_ms) actually observed
        self.file_ids = []           # newest first
        self.profile_metrics = {}
        self.run_id = None
        self.file_id = None
        self.size = None
        self.mtime_ms = None
        self.matched_input = None
        self._best_key = None

    @property
    def key(self):
        return (self.name.casefold(), self.sheet.casefold())

    def offer(self, sort_key, sig, rows, *, run_id=None, file_id=None,
              size=None, mtime_ms=None, matched_input=None):
        """Newest observation that carried a schema wins — for the signature
        AND for everything the catalogue reports as "the current state of this
        file" (size, mtime, the run and source_file the evidence points at)."""
        if self._best_key is None or sort_key > self._best_key:
            self._best_key = sort_key
            self.sig = sig
            self.rows = rows
            self.run_id = run_id
            self.file_id = file_id
            self.size = size
            self.mtime_ms = mtime_ms
            self.matched_input = matched_input


def sheet_sig(sheet: dict) -> tuple[Sig, int | None] | None:
    header = sheet.get("header")
    if not isinstance(header, list):
        return None
    order, seen, disp = [], set(), {}
    for h in header:
        key = norm_col(h)
        if key is None or key in seen:
            continue
        seen.add(key)
        order.append(key)
        disp[key] = h.strip() if isinstance(h, str) else key
    if not order:
        return None
    hdr = sheet.get("header_row")
    hdr = hdr if isinstance(hdr, int) else 0
    rows = sheet.get("rows") if isinstance(sheet.get("rows"), int) else None
    return Sig(order, order, hdr, {}, disp), rows


# ───────────────────────────── loading ───────────────────────────────────────

def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def load_corpus(db: Session, *, data_source_ids=None, run_ids=None,
                depth: int = 200):
    """-> (units by (ds_id, name, sheet), notes)

    `chains` is built the way `lineage._collect_observations` builds it —
    keyed by (generator name, data_source_id), newest observation first — and
    handed to the real `collect_chain_files`, so NULL schemas are resolved by
    the same walk-back the product uses. Re-implementing that walk here would
    let the catalogue and the lineage page disagree about what a cached file's
    header is.

    `data_source_ids` narrows the scope to the sources being rebuilt (the
    incremental path); `run_ids` narrows it in time (the probe's `--half`).
    """
    ds_filter = set(data_source_ids) if data_source_ids is not None else None

    gen_name = {g.id: g.name for g in db.query(Generator).all()}
    dash_gen = {d.id: d.generator_id for d in db.query(Dashboard).all()}
    runs = {r.id: r for r in db.query(GenerationRun).all()}

    q = db.query(SourceObservation)
    if ds_filter is not None:
        q = q.filter(SourceObservation.data_source_id.in_(sorted(ds_filter)))
    obs_rows = q.all()
    chains: dict[tuple[str, int], list] = {}
    for obs in obs_rows:
        if run_ids is not None and obs.run_id not in run_ids:
            continue
        run = runs.get(obs.run_id)
        if run is None:
            continue
        gname = gen_name.get(dash_gen.get(run.dashboard_id)) or f"dash:{run.dashboard_id}"
        chains.setdefault((gname, obs.data_source_id), []).append(obs)
    for chain in chains.values():
        chain.sort(key=lambda o: (o.created_at or datetime.min, o.id), reverse=True)

    levels_by_key = lineage.collect_chain_files(db, chains, depth=depth)

    units: dict[tuple, Unit] = {}
    unresolved: dict[tuple, dict] = {}
    for (gname, ds_id), levels in levels_by_key.items():
        for depth_i, level in enumerate(levels):
            obs = level["obs"]
            stamp = (obs.created_at or datetime.min, obs.id)
            for f in level["files"]:
                if f.get("kind") in ("unreadable", "excluded", "service"):
                    continue
                schema = f.get("schema")
                if not isinstance(schema, dict):
                    kind = f.get("kind")
                    if kind in ("parsed", "cached", "legacy"):
                        key = (ds_id, (f.get("name") or "").casefold())
                        prev = unresolved.get(key)
                        if prev is None:
                            unresolved[key] = {
                                "file": f.get("name") or "", "data_source_id": ds_id,
                                "kinds": {kind}, "generators": {gname},
                                "rows": 1, "deepest_level": depth_i}
                        else:
                            prev["kinds"].add(kind)
                            prev["generators"].add(gname)
                            prev["rows"] += 1
                            prev["deepest_level"] = max(prev["deepest_level"], depth_i)
                    continue
                for sh in (schema.get("sheets") or []):
                    if not isinstance(sh, dict):
                        continue
                    sheet_name = sh.get("name") or ""
                    made = sheet_sig(sh)
                    if made is None:
                        continue
                    sig, rows = made
                    key = (ds_id, (f["name"] or "").casefold(), sheet_name.casefold())
                    u = units.get(key)
                    if u is None:
                        u = units[key] = Unit(ds_id, f["name"] or "", sheet_name)
                    u.offer(stamp, sig, rows, run_id=obs.run_id,
                            file_id=f.get("source_file_id"), size=f.get("size"),
                            mtime_ms=f.get("mtime_ms"),
                            matched_input=f.get("matched_input"))
                    u.runs.add(obs.run_id)
                    u.generators.add(gname)
                    if f.get("size") is not None or f.get("mtime_ms") is not None:
                        u.stamps.add((f.get("size"), f.get("mtime_ms")))
                    if f.get("source_file_id"):
                        u.file_ids.append((stamp, f["source_file_id"]))

    # ── column profiles (quality_results, check_type='profile') ──────────────
    # Absent for every legacy generator today: this loop simply finds nothing
    # and every `types` dict stays empty. Nothing downstream may depend on a
    # profile existing.
    all_ids = [fid for u in units.values() for _st, fid in u.file_ids]
    prof: dict[int, dict[tuple, dict]] = {}
    for chunk in _chunks(all_ids, 800):
        for qr in (db.query(QualityResult)
                   .filter(QualityResult.source_file_id.in_(chunk),
                           QualityResult.check_type == "profile").all()):
            metric = qr.get_metric()
            col = norm_col(qr.column or "")
            if not col:
                continue
            prof.setdefault(qr.source_file_id, {})[((qr.sheet or "").casefold(), col)] = metric

    profiles_seen = 0
    for u in units.values():
        u.file_ids.sort(key=lambda x: x[0], reverse=True)
        types: dict[str, str] = {}
        metrics: dict[str, dict] = {}
        for _st, fid in reversed(u.file_ids):     # oldest first, newest wins
            for (sheet_key, col), metric in (prof.get(fid) or {}).items():
                if sheet_key != u.sheet.casefold():
                    continue
                metrics[col] = metric
                if metric.get("type"):
                    types[col] = metric["type"]
        if types:
            profiles_seen += 1
        u.sig.types = {c: t for c, t in types.items() if c in u.sig.S}
        u.profile_metrics = metrics

    # A file counts as "without a schema" only if NO observation anywhere gave
    # it one. A `legacy` row for a file that also has a parsed observation is
    # not a gap — it is the migration's own row for a file we know perfectly.
    resolved = {(ds, name) for (ds, name, _sheet) in units}
    missing = []
    also_resolved = 0
    for (ds, name), row in unresolved.items():
        if (ds, name) in resolved:
            also_resolved += 1
            continue
        kinds = sorted(row["kinds"])
        row["kinds"] = kinds
        row["generators"] = sorted(row["generators"])
        row["reason"] = (
            "legacy row only — a migrated observation, never backfilled and "
            "never a donor (its schema_json is not trusted), so this file has "
            "no sniffed header anywhere"
            if kinds == ["legacy"] else
            "no observation of this file carried a schema, and no older "
            "observation inside the walk-back window could donate one")
        missing.append(row)
    missing.sort(key=lambda r: (r["data_source_id"], r["kinds"], r["file"].casefold()))
    src_q = db.query(DataSource)
    if ds_filter is not None:
        src_q = src_q.filter(DataSource.id.in_(sorted(ds_filter)))
    sources = {ds.id: {"key_name": ds.key_name, "label": ds.label, "kind": ds.kind,
                       "declared_path": ds.declared_path}
               for ds in src_q.all()}
    for row in missing:
        src = sources.get(row["data_source_id"]) or {}
        row["data_source"] = src.get("label") or src.get("key_name") or "?"
    notes = {"units": len(units), "chains": len(chains),
             "sources": {str(k): v for k, v in sorted(sources.items())},
             "files_without_schema": len(missing),
             "files_without_schema_detail": missing,
             "files_resolved_from_another_observation": also_resolved,
             "backfill_cap": lineage._MAX_BACKFILL_ROWS,
             "units_with_profiles": profiles_seen}
    return units, notes


# ───────────────────────────── similarity ────────────────────────────────────

def lcs_len(a: tuple, b: tuple) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(cur[j], prev[j + 1]))
        prev = cur
    return prev[-1]


def containment(a: frozenset, b: frozenset) -> float:
    """Raw containment. Every caller goes through `set_overlap` instead — this
    stays as the definition the gate report's numbers refer to."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def set_overlap(a: frozenset, b: frozenset) -> float:
    """Containment where containment means something, jaccard where it does not.

    Containment normalises by the SMALLER set, so a 1- or 2-column sheet is
    "fully contained" in anything that shares its headers — a 1.0 that says
    nothing about the two being the same table. Below `MIN_CONTAINMENT_SET`
    the honest number is jaccard, which punishes the size difference instead
    of hiding it."""
    if not a or not b:
        return 0.0
    if min(len(a), len(b)) >= MIN_CONTAINMENT_SET:
        return len(a & b) / min(len(a), len(b))
    return jaccard(a, b)


def order_lcs(a: Sig, b: Sig) -> float:
    if not a.order or not b.order:
        return 0.0
    return lcs_len(a.order, b.order) / min(len(a.order), len(b.order))


def type_conflict(a: Sig, b: Sig) -> tuple[int, int]:
    shared = [c for c in (a.S & b.S) if c in a.types and c in b.types]
    bad = [c for c in shared if a.types[c] != b.types[c]]
    return len(bad), len(shared)


def hard_split(a: Sig, b: Sig) -> str | None:
    """Why these two may never share a table, or None."""
    if a.hdr != b.hdr:
        return f"header_row {a.hdr} != {b.hdr}"
    bad, shared = type_conflict(a, b)
    if shared and bad / shared > TYPE_CONFLICT_MAX:
        return f"type conflicts {bad}/{shared}"
    return None


def sim(a: Sig, b: Sig) -> float:
    return (W_CONTAINMENT * set_overlap(a.S, b.S)
            + W_JACCARD * jaccard(a.S, b.S)
            + W_ORDER_LCS * order_lcs(a, b))


# ───────────────────────────── clustering ────────────────────────────────────

class Cluster:
    __slots__ = ("members", "centroid")

    def __init__(self, unit: Unit):
        self.members = [unit]
        self.centroid = None
        self.recompute()

    def recompute(self):
        n = len(self.members)
        cnt = Counter(c for m in self.members for c in m.sig.S)
        keep = {c for c, k in cnt.items() if k / n >= MODE_RATIO}
        if not keep:                                    # pathological; never empty
            keep = set(cnt)
        rep = max(self.members, key=lambda m: (len(m.sig.S), m.key))
        order = tuple(c for c in rep.sig.order if c in keep)
        hdr = Counter(m.sig.hdr for m in self.members).most_common()
        hdr = sorted(h for h, k in hdr if k == hdr[0][1])[0]
        types: dict[str, str] = {}
        disp: dict[str, str] = {}
        for c in keep:
            votes = Counter(m.sig.types[c] for m in self.members if c in m.sig.types)
            if votes:
                types[c] = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            shown = Counter(m.sig.disp[c] for m in self.members if c in m.sig.disp)
            if shown:
                disp[c] = sorted(shown.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        self.centroid = Sig(keep, order, hdr, types, disp)

    def core(self) -> list[str]:
        n = len(self.members)
        cnt = Counter(c for m in self.members for c in m.sig.S)
        return sorted(c for c, k in cnt.items() if k / n >= CORE_RATIO)

    def presence(self) -> dict[str, float]:
        n = len(self.members)
        cnt = Counter(c for m in self.members for c in m.sig.S)
        return {c: k / n for c, k in cnt.items()}

    def add(self, unit: Unit):
        self.members.append(unit)
        self.recompute()


def cluster_units(units: list[Unit]) -> list[Cluster]:
    """Deterministic greedy agglomeration inside ONE data_source.

    Deterministic on purpose: the order is `(file name, sheet)`, so two runs
    produce the same clusters and therefore the same `table_id`s. A randomised
    or insertion-ordered pass would make stability meaningless."""
    clusters: list[Cluster] = []
    for u in sorted(units, key=lambda x: x.key):
        best, best_s = None, -1.0
        for c in clusters:
            if hard_split(u.sig, c.centroid):
                continue
            s = sim(u.sig, c.centroid)
            if s > best_s:
                best, best_s = c, s
        if best is not None and best_s >= JOIN_MIN:
            best.add(u)
        else:
            clusters.append(Cluster(u))

    # Second pass: two clusters that grew apart from different seeds but
    # describe the same table.
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i], clusters[j]
                if hard_split(a.centroid, b.centroid):
                    continue
                if sim(a.centroid, b.centroid) >= MERGE_MIN:
                    for m in b.members:
                        a.members.append(m)
                    a.recompute()
                    clusters.pop(j)
                    merged = True
                    break
            if merged:
                break
    for c in clusters:
        c.members.sort(key=lambda m: m.key)
    clusters.sort(key=lambda c: c.members[0].key)
    return clusters


# ───────────────────────────── naming & partitions ───────────────────────────

_TOKEN_SPLIT_RE = re.compile(r"[_\s]+")
_RANGE_RE = re.compile(r"^(\d{1,2})[-.](\d{1,2})[-.](\d{2,4})$")
_MONTH_RE = re.compile(r"^(\d{2})\.(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")
_WEEK_RE = re.compile(r"^w\d+$", re.I)
_SOP_RE = re.compile(r"^sop\d+$", re.I)
_NUM_RE = re.compile(r"^\d+$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _year(raw: str) -> str:
    v = int(raw)
    if len(raw) == 4:
        return f"{v:04d}"
    return f"{2000 + v:04d}" if v < 70 else f"{1900 + v:04d}"


def generalise_token(tok: str) -> str | None:
    """A date/number token -> its partition label; a word token -> None.

    `03-06.26` is a RANGE of months in one year (the 1C exports name a quarter
    that way), `07.26` a single month, `w31`/`SOP7` a planning-cycle label that
    means nothing to a calendar and is kept verbatim."""
    m = _RANGE_RE.match(tok)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
        if 1 <= a <= 12 and 1 <= b <= 12:
            return f"{y}-{a:02d}" if a == b else f"{y}-{a:02d}_{y}-{b:02d}"
        return f"{y}-{min(a, b):02d}"
    m = _MONTH_RE.match(tok)
    if m:
        a, y = int(m.group(1)), _year(m.group(2))
        if 1 <= a <= 12:
            return f"{y}-{a:02d}"
        return y
    if _YEAR_RE.match(tok):
        return tok
    if _WEEK_RE.match(tok) or _SOP_RE.match(tok):
        return tok.lower()
    if _NUM_RE.match(tok):
        return tok
    return None


def slug(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").casefold()
    return _SLUG_RE.sub("_", translit(s)).strip("_")


def tokenize(file_name: str) -> tuple[list[str], list[str]]:
    """file name -> (word tokens, partition labels), both in file order."""
    stem = Path(file_name).stem
    words, labels = [], []
    for tok in _TOKEN_SPLIT_RE.split(stem):
        if not tok:
            continue
        label = generalise_token(tok)
        if label is not None:
            labels.append(label)
            continue
        w = slug(tok)
        if w:
            words.append(w)
    return words, labels


def common_block(seqs: list[list[str]]) -> tuple[list[str], bool]:
    """Longest common token PREFIX of all sequences; when that is empty, the
    longest common contiguous BLOCK.

    The prefix is what every real family gives (`DataSalesOrder_03-06.26`,
    `DataSalesOrder_07.26` -> `DataSalesOrder`). The block is the honest
    fallback for `real_forecast_SOP7.xlsx` next to
    `old1_real_forecast_SOP7.xlsx`, where a decorating prefix on ONE member
    would otherwise leave the whole cluster nameless. The tokens outside the
    block become that file's partition label — which is what `old1` means."""
    if not seqs:
        return [], False
    pref = list(seqs[0])
    for s in seqs[1:]:
        k = 0
        while k < len(pref) and k < len(s) and pref[k] == s[k]:
            k += 1
        pref = pref[:k]
        if not pref:
            break
    if pref:
        return pref, True
    best: list[str] = []
    first = seqs[0]
    for i in range(len(first)):
        for j in range(len(first), i, -1):
            cand = first[i:j]
            if len(cand) <= len(best):
                break
            if all(_contains(s, cand) for s in seqs[1:]):
                best = cand
                break
    return best, False


def _contains(seq: list[str], block: list[str]) -> bool:
    n = len(block)
    return any(seq[i:i + n] == block for i in range(len(seq) - n + 1))


def _block_at(seq: list[str], block: list[str]) -> int:
    n = len(block)
    for i in range(len(seq) - n + 1):
        if seq[i:i + n] == block:
            return i
    return -1


def _narrow_date_key(unit: Unit) -> str | None:
    """Fallback (2): a `type=date` profile column whose min..max sits inside
    one month (or one year). No profiles -> no key, never an exception."""
    metrics = unit.profile_metrics or {}
    best = None
    for col in sorted(metrics):
        m = metrics[col]
        if m.get("type") != "date":
            continue
        lo, hi = _as_date(m.get("min")), _as_date(m.get("max"))
        if not lo or not hi:
            continue
        if lo[:7] == hi[:7]:
            cand = lo[:7]
        elif lo[:4] == hi[:4]:
            cand = lo[:4]
        else:
            continue
        if best is None or len(cand) > len(best):
            best = cand
    return best


_DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})")


def _as_date(v) -> str | None:
    if not isinstance(v, str):
        return None
    m = _DATE_PREFIX_RE.match(v.strip())
    return m.group(0) if m else None


_NUMBERED_TOKEN_RE = re.compile(r"^(?=.*[a-z])[a-z]*[a-z_]\d+$")


def trim_numbered_tail(block: list[str]) -> list[str]:
    """Drop trailing word-plus-number tokens from a name block.

    `ExRDC postDFM'05.xlsb` tokenises to `exrdc` + `postdfm_05`: the number is
    welded to a word, so `generalise_token` cannot see it and the whole thing
    used to end up in the NAME — giving `exrdc_postdfm_05` and
    `exrdc_postdfm_07` as two differently-named tables of the same shape, while
    the six-file `exrdc_mapping` cluster carried the very same tokens as
    PARTITION keys. Same corpus, two answers. The number belongs to the
    partition in both cases; the trimmed token falls through to the per-file
    key below on its own, because that key is "every word outside the block".

    Never trims the last token: a name must survive."""
    out = list(block)
    while len(out) > 1 and _NUMBERED_TOKEN_RE.match(out[-1]):
        out.pop()
    return out


def name_clusters(clusters: list[Cluster], ds_id: int) -> list[dict]:
    """Name every cluster of one source, resolve collisions, derive per-file
    partition keys, and mint the stable `table_id`."""
    out = []
    for c in clusters:
        toks = [tokenize(m.name) for m in c.members]
        words = [w for w, _l in toks]
        block, is_prefix = common_block(words)
        block = trim_numbered_tail(block)
        name = "_".join(block) or slug(c.members[0].sheet) or "table"
        parts = []
        for m, (w, labels) in zip(c.members, toks):
            at = _block_at(w, block) if block else -1
            leftover_idx = [i for i in range(len(w)) if not (at >= 0 and at <= i < at + len(block))]
            key_parts = []
            # Emit in file order: leftover words and generalised labels keep
            # the sequence they had in the name (`old1` + `sop7`).
            for i in range(len(w)):
                if i in leftover_idx:
                    key_parts.append(w[i])
            key_parts.extend(labels)
            key = "_".join(key_parts) or _narrow_date_key(m)
            parts.append(key)
        out.append({"cluster": c, "name": name, "stem": name,
                    "prefix": is_prefix, "parts": parts})

    # Collisions, in two rounds. First the informative one: two tables whose
    # files are named alike but whose SHEET differs get the sheet in the name
    # (`datasalesorder_tdsheet` vs `datasalesorder_sheet1`). Whatever still
    # collides gets `_2`, `_3`, … — a name is an identifier here, and ten
    # tables called `svt_sheet1` are not addressable by anyone.
    seen = Counter(o["name"] for o in out)
    for o in out:
        if seen[o["name"]] > 1:
            o["name"] = f"{o['name']}_{slug(o['cluster'].members[0].sheet)}"
    taken: set[str] = set()
    for o in out:                       # `out` is in deterministic cluster order
        base = o["name"]
        name, k = base, 1
        while name in taken:
            k += 1
            name = f"{base}_{k}"
        taken.add(name)
        o["name"] = name
    for o in out:
        core = o["cluster"].core()
        inner = hashlib.sha1(",".join(sorted(core)).encode("utf-8")).hexdigest()
        o["core"] = core
        o["table_id"] = "t_" + hashlib.sha1(
            f"{ds_id}|{o['name']}|{inner}".encode("utf-8")).hexdigest()[:12]
    return out


# ───────────────────────────── analysis ──────────────────────────────────────

def analyse(units: dict, notes: dict) -> dict:
    """The full inference result for one corpus: tables (with per-file
    evidence and the containment numbers the Leg 0 gates read), the sheets
    classified out, and which generator reads which source."""
    by_ds: dict[int, list[Unit]] = {}
    non_tables: list[dict] = []
    for u in units.values():
        kind, why = classify_unit(u.sig)
        if kind != "table":
            non_tables.append({
                "kind": kind, "reason": why, "data_source_id": u.ds_id,
                "file": u.name, "sheet": u.sheet,
                "columns": len(u.sig.S), "header_row": u.sig.hdr,
                "rows": u.rows,
                "headers": [u.sig.disp.get(c, c) for c in u.sig.order][:MAX_NONTABLE_HEADERS],
                "generators": sorted(u.generators), "runs": len(u.runs),
            })
            continue
        by_ds.setdefault(u.ds_id, []).append(u)
    non_tables.sort(key=lambda r: (r["data_source_id"], r["kind"], r["file"].casefold(),
                                   r["sheet"].casefold()))

    tables: list[dict] = []
    for ds_id in sorted(by_ds):
        clusters = cluster_units(by_ds[ds_id])
        named = name_clusters(clusters, ds_id)
        centroids = [(o["name"], o["cluster"].centroid) for o in named]
        for o in named:
            c = o["cluster"]
            files = []
            intra_vals, inter_vals = [], []
            for m, part in zip(c.members, o["parts"]):
                intra = set_overlap(m.sig.S, c.centroid.S)
                inter, inter_with, inter_sim = 0.0, None, 0.0
                for other_name, other in centroids:
                    if other is c.centroid:
                        continue
                    # A centroid too small to be contained in anything
                    # meaningfully is not evidence that this file is ambiguous.
                    if len(other.S) < MIN_CONTAINMENT_SET:
                        continue
                    v = set_overlap(m.sig.S, other.S)
                    if v > inter:
                        inter, inter_with = v, other_name
                    # Informational only, never gated: the FULL similarity the
                    # clusterer itself would use.
                    inter_sim = max(inter_sim, sim(m.sig, other))
                intra_vals.append(intra)
                inter_vals.append(inter)
                files.append({
                    "file": m.name, "sheet": m.sheet, "partition": part,
                    "columns": len(m.sig.S), "header_row": m.sig.hdr,
                    "rows": m.rows, "runs": len(m.runs),
                    "generators": sorted(m.generators),
                    "stamps": sorted((s or -1, t or -1) for s, t in m.stamps),
                    "size": m.size, "mtime_ms": m.mtime_ms,
                    "run_id": m.run_id, "source_file_id": m.file_id,
                    "matched_input": m.matched_input,
                    "intra_containment": round(intra, 4),
                    "inter_containment_max": round(inter, 4),
                    "inter_containment_with": inter_with,
                    "inter_sim_max": round(inter_sim, 4),
                })
            n = len(c.members)
            presence = c.presence()
            part_share = sum(1 for p in o["parts"] if p) / n
            gap = max(0.0, min(intra_vals) - max(inter_vals))
            density = sum(intra_vals) / n
            runs = {r for m in c.members for r in m.runs}
            confidence = (0.4 * density + 0.3 * gap + 0.2 * part_share
                          + 0.1 * min(1.0, len(runs) / 5.0))
            gens = sorted({g for m in c.members for g in m.generators})
            dup_keys = _duplicate_keys(files)
            tables.append({
                "table_id": o["table_id"],
                "data_source_id": ds_id,
                "name": o["name"],
                "name_stem": o["stem"],
                "named_by": "prefix" if o["prefix"] else "common_block",
                # NEVER a member file name: `c.members[0]` is just the
                # alphabetically-first partition (Unit sort key, `Cluster`
                # above), so showing its raw file name presents one
                # partition's identity as the table's — the exact "never
                # present an inferred fact as fact" failure ADR-12 exists to
                # avoid. `o["name"]` is the inference-owned identifier that
                # already feeds `table_id` and the suggest-inputs patch;
                # prettify it (underscores -> spaces, title case) into a
                # first-cut human label instead. It stays fully editable
                # (`display_name` PATCH) and is still clearly a suggestion,
                # not a filename dressed up as one.
                "display_name": " ".join(w.capitalize() for w in o["name"].split("_")) or o["name"],
                "sheets": sorted({m.sheet for m in c.members}),
                "files": files,
                "n_files": n,
                "n_columns": len(c.centroid.S),
                "core": o["core"],
                "core_size": len(o["core"]),
                "header_row": c.centroid.hdr,
                "columns": [{"name": col,
                             "display": c.centroid.disp.get(col, col),
                             "present_ratio": round(presence[col], 4),
                             "type": c.centroid.types.get(col)}
                            for col in sorted(presence, key=lambda x: (-presence[x], x))],
                "partitions": sorted({p for p in o["parts"] if p}),
                "partition_share": round(part_share, 4),
                "duplicate_partition_keys": dup_keys,
                "consumers": gens,
                "n_runs": len(runs),
                "intra_containment_min": round(min(intra_vals), 4),
                "inter_containment_max": round(max(inter_vals), 4),
                "gap": round(gap, 4),
                "confidence": round(confidence, 4),
                "evidence": [{"file": f["file"], "sheet": f["sheet"],
                              "source_file_id": f["source_file_id"],
                              "run_id": f["run_id"],
                              "sim": round(sim(m.sig, c.centroid), 4)}
                             for f, m in list(zip(files, c.members))[:MAX_EVIDENCE]],
            })

    reads: dict[str, set] = {}
    for u in units.values():
        for g in u.generators:
            reads.setdefault(g, set()).add(u.ds_id)
    return {"tables": tables, "notes": notes,
            "non_tables": summarise_non_tables(non_tables),
            "reads": {g: sorted(ds) for g, ds in sorted(reads.items())}}


def summarise_non_tables(rows: list[dict]) -> dict:
    """The sheets that were never offered to the clusterer, and why.

    Not a leftovers bin — it is the population the product has to have an
    answer for. Every row here is a sheet a user will open expecting a table
    and get nothing, so the names are kept, not a count."""
    per_ds: dict[int, dict] = {}
    for r in rows:
        e = per_ds.setdefault(r["data_source_id"],
                              {"data_source_id": r["data_source_id"],
                               "pivot": 0, "fragment": 0, "files": set(),
                               "examples": []})
        e[r["kind"]] += 1
        e["files"].add(r["file"])
        if len(e["examples"]) < 5:
            e["examples"].append({"file": r["file"], "sheet": r["sheet"],
                                  "kind": r["kind"], "reason": r["reason"],
                                  "headers": r["headers"]})
    return {
        "total": len(rows),
        "pivot": sum(1 for r in rows if r["kind"] == "pivot"),
        "fragment": sum(1 for r in rows if r["kind"] == "fragment"),
        "distinct_files": len({(r["data_source_id"], r["file"]) for r in rows}),
        "per_data_source": [
            {**{k: v for k, v in e.items() if k != "files"},
             "distinct_files": len(e["files"])}
            for _ds, e in sorted(per_ds.items())],
        "units": rows,
    }


def _duplicate_keys(files: list[dict]) -> list[dict]:
    """Same partition key, different (size, mtime) — a re-issue, not a copy.
    Listed, never silently merged."""
    by_key: dict[str, list[dict]] = {}
    for f in files:
        if f["partition"]:
            by_key.setdefault(f["partition"], []).append(f)
    out = []
    for key, group in sorted(by_key.items()):
        if len(group) < 2:
            continue
        stamps = {tuple(s) for f in group for s in f["stamps"]}
        if len(stamps) > 1:
            out.append({"key": key, "files": [f["file"] for f in group],
                        "stamps": sorted(stamps)})
    return out


def infer(db: Session, data_source_ids=None, run_ids=None, *,
          depth: int = 200) -> dict:
    """The whole inference, read-only: corpus -> clusters -> named tables.

    This is the function `tools/lake_probe.py` runs its gates over and the one
    `rebuild()` persists. One implementation, one answer."""
    units, notes = load_corpus(db, data_source_ids=data_source_ids,
                               run_ids=run_ids, depth=depth)
    return analyse(units, notes)


# ───────────────────────────── persistence ───────────────────────────────────

def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def observed_source_ids(db: Session) -> list[int]:
    """Sources that any run has actually observed — the only ones inference
    can say anything about. A source row with no observation is not skipped
    work, it is an empty question."""
    return sorted({row[0] for row in
                   db.query(SourceObservation.data_source_id).distinct().all()
                   if row[0] is not None})


def rebuild(db: Session, data_source_id: int | None = None, *,
            budget_s: float = 20.0, depth: int = 200,
            now: datetime | None = None) -> dict:
    """Compute-on-write: re-infer per data source and upsert the catalogue.

    Contract, in the order the invariants matter:

    * **A `confirmed` or `rejected` row is never deleted.** Nothing here
      deletes a `catalog_tables` row at all: an inferred table that vanishes
      from the inference keeps its `last_seen_at` UNTOUCHED (that is what
      makes staleness visible) and is reported in `stats["stale"]`.
    * **Human edits survive.** `status`, `owner_id`, `confirmed_by/at` are
      only ever written by a human, so inference does not touch them. `name`
      is inference-owned (it feeds `table_id`; renaming it would break id
      stability), and `display_name` becomes human-owned the moment somebody
      edits the row — which is exactly what `version > 1` means, since the API
      bumps the optimistic-lock version on every human edit and rebuild never
      does.
    * **Partitions/consumers are replaced for the tables this run inferred**,
      not for every table of the source: a stale table keeps its last known
      partitions, matching its untouched `last_seen_at`. `catalog_nontables`
      IS replaced wholesale per source — it is a property of the source, not
      of a table.
    * **The budget is a wall, not a suggestion.** Sources are processed in id
      order; the FIRST one always runs (a budget of zero must not turn the job
      into a silent no-op) and every source that does not fit is listed in
      `stats["skipped"]`. `_gc`'s daily tick and the post-run debounce both
      call this, so a folder that grew pathological delays the catalogue, it
      does not stall housekeeping.

    Returns a stats dict; callers log it. Exceptions are the caller's problem
    (both call sites wrap this) — a partial commit is not, because each source
    is committed on its own.
    """
    started = time.monotonic()
    now = now or datetime.utcnow()
    if data_source_id is not None:
        ds_ids = [data_source_id]
    else:
        ds_ids = observed_source_ids(db)

    stats = {"sources": 0, "tables": 0, "created": 0, "updated": 0,
             "partitions": 0, "consumers": 0, "nontables": 0,
             "stale": [], "skipped": [], "seconds": 0.0}

    for i, ds_id in enumerate(ds_ids):
        if i and (time.monotonic() - started) >= budget_s:
            stats["skipped"] = ds_ids[i:]
            break
        res = infer(db, data_source_ids=[ds_id], depth=depth)
        _persist_source(db, ds_id, res, now, stats)
        db.commit()
        stats["sources"] += 1

    stats["seconds"] = round(time.monotonic() - started, 3)
    return stats


def _persist_source(db: Session, ds_id: int, res: dict, now: datetime,
                    stats: dict) -> None:
    tables = [t for t in res["tables"] if t["data_source_id"] == ds_id]
    existing = {row.table_id: row for row in
                db.query(CatalogTable).filter(CatalogTable.data_source_id == ds_id).all()}
    seen_ids = set()
    # table_id is derived from the cluster's CORE columns (see name_clusters /
    # the id-building step above): ordinary membership drift (a file family
    # gains or drops a column across the 80% core threshold) mints a brand
    # new id for the SAME `name`, orphaning any confirmed/edited row under
    # the old id. Track newly-minted ids by name so the stale pass below can
    # link the old row to its successor via `superseded_by` instead of
    # leaving it to rot forever.
    created_by_name: dict[str, str] = {}

    for t in tables:
        seen_ids.add(t["table_id"])
        row = existing.get(t["table_id"])
        if row is None:
            row = CatalogTable(
                table_id=t["table_id"], data_source_id=ds_id,
                display_name=t["display_name"], status=STATUS_INFERRED,
                first_seen_at=now, version=1)
            db.add(row)
            existing[t["table_id"]] = row
            stats["created"] += 1
            created_by_name[t["name"]] = t["table_id"]
        else:
            stats["updated"] += 1
            # `version > 1` == a human has edited this row; their display_name
            # is a decision, not a stale guess.
            if row.version <= 1:
                row.display_name = t["display_name"]
        row.name = t["name"]
        row.name_stem = t["name_stem"]
        row.sheets_json = _dumps(t["sheets"])
        row.core_cols_json = _dumps(t["core"])
        row.columns_json = _dumps([
            {"norm": c["name"], "display": c["display"],
             "present_ratio": c["present_ratio"], "type": c["type"]}
            for c in t["columns"][:MAX_COLUMNS_STORED]])
        row.header_row = t["header_row"]
        row.confidence = t["confidence"]
        row.evidence_json = _dumps(t["evidence"])
        row.n_files = t["n_files"]
        row.last_seen_at = now
        row.updated_at = now

        db.query(CatalogPartition).filter(
            CatalogPartition.table_id == t["table_id"]).delete(synchronize_session="fetch")
        for f in t["files"]:
            db.add(CatalogPartition(
                table_id=t["table_id"],
                # "" and not NULL: SQLite treats NULLs as distinct inside a
                # UNIQUE index, so a NULL key would let the same file be
                # inserted twice on the next rebuild.
                key=f["partition"] or "",
                file_name=f["file"], sheet=(f.get("sheet") or "")[:64],
                size=f["size"], mtime_ms=f["mtime_ms"],
                rows=f["rows"], last_run_id=f["run_id"], last_seen_at=now))
            stats["partitions"] += 1

        db.query(CatalogConsumer).filter(
            CatalogConsumer.table_id == t["table_id"]).delete(synchronize_session="fetch")
        for gen_id, matched, run_id in _consumers(db, t):
            db.add(CatalogConsumer(table_id=t["table_id"], generator_id=gen_id,
                                   matched_input=matched, last_run_id=run_id))
            stats["consumers"] += 1
        stats["tables"] += 1

    for table_id, row in sorted(existing.items()):
        if table_id in seen_ids:
            continue
        # Untouched last_seen_at IS the staleness signal; no deletes here.
        successor = created_by_name.get(row.name)
        if successor and row.superseded_by != successor:
            row.superseded_by = successor
        stats["stale"].append({"table_id": table_id, "name": row.name,
                               "status": row.status,
                               "superseded_by": row.superseded_by,
                               "last_seen_at": row.last_seen_at})

    db.query(CatalogNonTable).filter(
        CatalogNonTable.data_source_id == ds_id).delete(synchronize_session="fetch")
    for r in (res.get("non_tables") or {}).get("units") or []:
        if r["data_source_id"] != ds_id:
            continue
        db.add(CatalogNonTable(
            data_source_id=ds_id, file_name=r["file"], sheet=r["sheet"],
            kind=r["kind"], reason=r["reason"],
            headers_json=_dumps(r["headers"][:MAX_NONTABLE_HEADERS]),
            last_seen_at=now))
        stats["nontables"] += 1


def _consumers(db: Session, table: dict) -> list[tuple[int, str | None, int | None]]:
    """(generator_id, matched_input, last_run_id) for one inferred table.

    The inference carries generator NAMES (that is what the observation chain
    is keyed by); the row needs an id, so the names are resolved once here.
    A generator that has since been deleted simply drops out."""
    names = {g for f in table["files"] for g in f["generators"]}
    if not names:
        return []
    by_name = {g.name: g.id for g in
               db.query(Generator).filter(Generator.name.in_(sorted(names))).all()}
    out = []
    for name in sorted(names):
        gid = by_name.get(name)
        if gid is None:
            continue
        matched, run_id = None, None
        for f in table["files"]:
            if name not in f["generators"]:
                continue
            if f["matched_input"] and matched is None:
                matched = f["matched_input"]
            if f["run_id"] is not None and (run_id is None or f["run_id"] > run_id):
                run_id = f["run_id"]
        out.append((gid, matched, run_id))
    return out


# ───────────────────────────── the job ───────────────────────────────────────

_DEBOUNCE_S = 60.0
_PENDING: dict[int, threading.Timer] = {}
_PENDING_LOCK = threading.Lock()


def enabled() -> bool:
    from app.config import settings
    return bool(getattr(settings, "catalog_enabled", True))


def rebuild_safely(data_source_id: int | None = None, *,
                   budget_s: float = 20.0) -> dict | None:
    """`rebuild` on its own session, with its own `except`. Housekeeping must
    never take the thread (or the request) down with it."""
    if not enabled():
        return None
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        stats = rebuild(db, data_source_id, budget_s=budget_s)
        print(f"[catalog-rebuild] {stats}")
        return stats
    except Exception as exc:                                # noqa: BLE001
        print(f"[catalog-rebuild] FAILED: {type(exc).__name__}: {exc}")
        try:
            db.rollback()
        except Exception:
            pass
        return None
    finally:
        db.close()


def schedule(data_source_id: int, *, delay_s: float | None = None) -> None:
    """Debounced background rebuild of ONE source, the `warm.schedule` shape.

    60 s, because the trigger is a finished generator run and a user who
    re-runs a report three times in a row should cost one inference, not
    three. The timer thread opens its own session — the request's session is
    long closed by the time it fires."""
    if not enabled():
        return
    delay = _DEBOUNCE_S if delay_s is None else delay_s

    def _run():
        with _PENDING_LOCK:
            _PENDING.pop(data_source_id, None)
        rebuild_safely(data_source_id)

    with _PENDING_LOCK:
        prev = _PENDING.pop(data_source_id, None)
        if prev is not None:
            prev.cancel()
        timer = threading.Timer(delay, _run)
        timer.daemon = True
        _PENDING[data_source_id] = timer
        timer.start()


def schedule_for_run(run_id: int, *, delay_s: float | None = None) -> None:
    """Debounce a rebuild for every source the finished run observed.

    Called right after `observations.finalize` + commit. It costs one query
    and no inference on the request path — deliberately, since this runs
    inside the engine's `final` observation POST."""
    if not enabled():
        return
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        ids = sorted({row[0] for row in
                      db.query(SourceObservation.data_source_id)
                      .filter(SourceObservation.run_id == run_id).all()
                      if row[0] is not None})
    except Exception:
        return
    finally:
        db.close()
    for ds_id in ids:
        schedule(ds_id, delay_s=delay_s)


# ───────────────────────────── the suggester ─────────────────────────────────
#
# ADR-12: the catalogue NEVER writes a manifest. This builds a patch a human
# loads as a new generator version, and it ships the list of files that patch
# would stop reading, because a suggestion whose cost is invisible is a trap.

_GLOB_NUM_TAIL_RE = re.compile(r"^(.*[^\W\d_])\d+$", re.UNICODE)
_GLOB_SPLIT_RE = re.compile(r"([_\s]+)")


def _glob_token(tok: str) -> str:
    """A file-name token -> its glob form.

    A pure date/number token IS the partition and becomes `*`; a word welded
    to a number (`SOP7`, `w31`, `a1`) keeps the word and wildcards the number,
    which is what makes `real_forecast_SOP*.xlsx` and not `*.xlsx`."""
    if (_RANGE_RE.match(tok) or _MONTH_RE.match(tok) or _YEAR_RE.match(tok)
            or _NUM_RE.match(tok)):
        return "*"
    m = _GLOB_NUM_TAIL_RE.match(tok)
    if m:
        return m.group(1) + "*"
    return tok


def file_glob(name: str) -> str:
    """One file name -> the glob that admits its family."""
    p = Path(name)
    parts = _GLOB_SPLIT_RE.split(p.stem)
    out: list[str] = []
    for i, chunk in enumerate(parts):
        if i % 2:                       # the separator itself
            out.append(chunk)
            continue
        out.append(_glob_token(chunk))
    # Collapse `*_*` runs: two adjacent wildcards say nothing the first did not.
    collapsed: list[str] = []
    for i, chunk in enumerate(out):
        if (chunk == "*" and len(collapsed) >= 2 and collapsed[-2] == "*"
                and not collapsed[-1].strip("_ ")):
            collapsed.pop()
            continue
        collapsed.append(chunk)
    return "".join(collapsed) + p.suffix


def best_glob(names: list[str]) -> str | None:
    """The glob for a table: the candidate that admits the most of its files,
    then the shortest, then the lexicographically first.

    Deliberately NOT "the longest common prefix": `real_forecast_SOP7.xlsx`
    next to `old1_real_forecast_SOP7.xlsx` has none, and a `*`-prefixed
    pattern would quietly admit every neighbour file in the folder. A pattern
    that drops the decorated file and SAYS so is the honest suggestion."""
    from app.services import globmatch
    cands = []
    for n in sorted(set(names)):
        g = file_glob(n)
        if globmatch.validate_pattern(g) is not None:
            continue
        # A pure date/number stem (`07.26.xlsx`) globs its whole stem away,
        # leaving `*.xlsx` — that passes validate_pattern (which only bans
        # wildcards-only over the FULL pattern including the suffix) but
        # admits every neighbour file of that extension, exactly what this
        # function's docstring says never to emit.
        suffix = Path(n).suffix
        stem_glob = g[:-len(suffix)] if suffix and g.endswith(suffix) else g
        if stem_glob.replace("*", "").replace("?", "") == "":
            continue
        hits = sum(1 for m in names if globmatch.matches(g, m))
        cands.append((-hits, len(g), g))
    if not cands:
        return None
    return sorted(cands)[0][2]


def _source_files_for(db: Session, ds_ids: list[int], depth: int = 200) -> dict[int, list[str]]:
    """Every file name ever observed in each source, through the same
    walk-back the inference uses — the denominator for matched/unmatched."""
    if not ds_ids:
        return {}
    gen_name = {g.id: g.name for g in db.query(Generator).all()}
    dash_gen = {d.id: d.generator_id for d in db.query(Dashboard).all()}
    runs = {r.id: r for r in db.query(GenerationRun).all()}
    chains: dict[tuple[str, int], list] = {}
    for obs in (db.query(SourceObservation)
                .filter(SourceObservation.data_source_id.in_(sorted(ds_ids))).all()):
        run = runs.get(obs.run_id)
        if run is None:
            continue
        gname = gen_name.get(dash_gen.get(run.dashboard_id)) or f"dash:{run.dashboard_id}"
        chains.setdefault((gname, obs.data_source_id), []).append(obs)
    for chain in chains.values():
        chain.sort(key=lambda o: (o.created_at or datetime.min, o.id), reverse=True)
    levels_by_key = lineage.collect_chain_files(db, chains, depth=depth)
    out: dict[int, set[str]] = {ds: set() for ds in ds_ids}
    for (_g, ds_id), levels in levels_by_key.items():
        for level in levels:
            for f in level["files"]:
                # "excluded" is a deliberate input_overrides rule — not a
                # cost to report. "service" (supporting workbooks like an
                # SL_NWD reasons catalog) is NOT excluded here: it is exactly
                # the kind of file a spliced patch can silently stop reading
                # (gen-engine.js's enumerator does not exempt it), so it must
                # stay in the matched/unmatched denominator this function
                # builds. See load_corpus (line ~302) for the SEPARATE,
                # correct use of the same skip-list when sampling content for
                # table inference, where service files are the right thing
                # to leave out.
                if f.get("kind") == "excluded":
                    continue
                if f.get("name"):
                    out.setdefault(ds_id, set()).add(f["name"])
    return {ds: sorted(names) for ds, names in out.items()}


def generator_source_ids(db: Session, generator_id: int) -> list[int]:
    """The data sources this generator has actually observed."""
    dash_ids = [d.id for d in db.query(Dashboard)
                .filter(Dashboard.generator_id == generator_id).all()]
    if not dash_ids:
        return []
    run_ids = [r.id for r in db.query(GenerationRun)
               .filter(GenerationRun.dashboard_id.in_(dash_ids)).all()]
    if not run_ids:
        return []
    return sorted({row[0] for row in
                   db.query(SourceObservation.data_source_id)
                   .filter(SourceObservation.run_id.in_(run_ids)).distinct().all()
                   if row[0] is not None})


def suggest_inputs(db: Session, generator_id: int, *,
                   allowed_source_ids: set[int] | None = None,
                   max_inputs: int = 32, max_sheets: int = 16,
                   max_columns: int = 256, max_column_len: int = 64) -> dict:
    """A manifest `inputs` PATCH for a legacy generator — and its cost.

    Pure read. The caller splices `result["inputs"]` into the manifest and
    hands it to a human, who loads it as a NEW generator version; nothing here
    writes a manifest, and rollback is the previous version.

    `unmatched` is the point of the whole function: `inputs` is an ALLOW-list,
    so publishing this patch stops the generator from reading anything the
    patterns miss. Those names are computed over ALL observations of the
    source, not over the tables' own files, so a file that only ever appeared
    once still shows up as something the patch would drop.

    `allowed_source_ids` narrows the generator's observed sources to the ones
    the CALLER may actually see (`None` = unrestricted, i.e. admin) — a
    generator can back dashboards owned by different people over different
    folders, and one runnable dashboard must not leak every other owner's
    source contents. Same reach boundary as `_reachable_source_ids` /
    `_sources_observed_by` elsewhere in this module's callers.

    Caps mirror `genmanifest`'s so a spliced patch validates rather than
    exploding at registration.
    """
    ds_ids = generator_source_ids(db, generator_id)
    if allowed_source_ids is not None:
        ds_ids = [d for d in ds_ids if d in allowed_source_ids]
    tables = []
    if ds_ids:
        tables = (db.query(CatalogTable)
                  .filter(CatalogTable.data_source_id.in_(ds_ids),
                          CatalogTable.status.in_((STATUS_CONFIRMED, STATUS_INFERRED)))
                  .all())
    # Confirmed first: if the cap bites, a human-approved table survives.
    tables.sort(key=lambda t: (t.status != STATUS_CONFIRMED, -(t.confidence or 0.0),
                               t.name or ""))

    files_by_ds = _source_files_for(db, ds_ids)
    inputs: list[dict] = []
    reported: list[dict] = []
    used: set[str] = set()
    for t in tables:
        names = [p.file_name for p in
                 db.query(CatalogPartition)
                 .filter(CatalogPartition.table_id == t.table_id).all()]
        match = best_glob(names) if names else None
        if match is None or match.lower() in used:
            continue
        if len(inputs) >= max_inputs:
            reported.append({"table_id": t.table_id, "name": t.name,
                             "status": t.status, "match": match,
                             "skipped": "over the manifest's inputs cap"})
            continue
        used.add(match.lower())
        cols = []
        for c in t.get_columns():
            if c["norm"] not in set(t.get_core_cols()):
                continue
            disp = (c.get("display") or c["norm"]).strip()
            if not disp or len(disp) > max_column_len:
                continue
            cols.append(disp)
        cols = cols[:max_columns]
        sheets = [{"name": s, "columns": list(cols)}
                  for s in t.get_sheets()[:max_sheets]]
        entry = {"match": match, "role": "data", "sheets": sheets}
        inputs.append(entry)
        reported.append({"table_id": t.table_id, "name": t.name,
                         "status": t.status, "match": match,
                         "files": sorted(names)})

    patterns = [e["match"] for e in inputs]
    from app.services import globmatch
    sources = []
    matched_all: list[str] = []
    unmatched_all: list[str] = []
    for ds_id in ds_ids:
        names = files_by_ds.get(ds_id) or []
        matched = [n for n in names if globmatch.first_match(patterns, n)]
        unmatched = [n for n in names if not globmatch.first_match(patterns, n)]
        src = db.query(DataSource).filter(DataSource.id == ds_id).first()
        sources.append({
            "data_source_id": ds_id,
            "key_name": getattr(src, "key_name", None),
            "label": getattr(src, "label", None),
            "matched": matched, "unmatched": unmatched,
        })
        matched_all += matched
        unmatched_all += unmatched

    return {"generator_id": generator_id, "inputs": inputs,
            "tables": reported, "sources": sources,
            "matched": sorted(set(matched_all)),
            "unmatched": sorted(set(unmatched_all))}
