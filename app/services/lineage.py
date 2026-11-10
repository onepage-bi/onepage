"""The six-layer lineage graph: folder → pattern → sheet → generator → output → report.

The Data Sources page answered "who reads this folder". That is one edge of a
much longer chain, and every link of it already exists somewhere in the
database — this module is the join, not a new measurement:

  layer 0  `data_sources` + the LATEST observation of each — via
           `datasources.build()`, which is called, never re-implemented: the
           union-not-sum byte rule and the data_stale/unread split are its
           decisions and must not be made twice.
  layer 1  `genmanifest.inputs_for()` of the generator's CURRENT frozen
           manifest — what the author DECLARED it reads. A generator with no
           `inputs` gets ONE synthetic pattern node `*` marked `legacy: true`,
           because the engine's legacy behaviour ("every Excel file in the
           folder") is a real pattern that the graph must draw, not an absence.
  layer 2  the declared sheets/columns crossed with `source_files.schema_json`
           (observed headers) and `consumed_json` (what the read hooks saw).
           Column state is a TRI-state — declared/observed are booleans, but
           `consumed` is null whenever nothing could have observed it (cached
           file, agent engine, hookless reader), which is different from
           "declared and never touched".
  layer 2.5 the STAGES of the generator's latest run (P4.1) — the
           `onepage.stage()` snapshots its own code took of its intermediate
           frames. When a run reported any, the read edges no longer end at
           the generator: they end at the stage that read those files, the
           stages feed each other in call (or declared) order, and the last
           one feeds the generator. A generator that never calls `stage()` —
           and every agent-engine one, which has no hooks — keeps the direct
           edge and this band is simply absent.
  layer 3  the generator, layer 4 its outputs (html, declared slots, and the
           write-backs its last run reported), layer 5 the reports.

`writes_back` is the one edge that points BACKWARDS (output → source): a
generator that writes an .xlsx into the analyst's folder makes that folder
both an input and an output, and hiding the cycle would hide exactly the
thing an owner needs to see before deleting a file. Every other edge goes
strictly up a layer.

Bounded by construction: the observation window comes from `datasources`
(at most a few runs per dashboard), and the node count is capped — sheet
nodes are dropped first (they are the only unbounded axis: one workbook can
declare 32 sheets) and `meta.truncated` says so.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    Annotation, Generator, GeneratorVersion, RunStage, SourceFile, SourceObservation,
)
from app.services import datasources, genmanifest, globmatch
from app.services.schemadiff import norm

# One page render must never turn into an unbounded DOM. 5000 is far above any
# real installation (the pilot's largest generator draws ~60 nodes) and low
# enough that the browser-side layout stays interactive.
MAX_NODES = 5000

# Files whose schema_json is NULL mean "unchanged since the previous
# observation" (see SourceFile's docstring), so the header comes from an
# older observation of the same source. That walk back is capped: a folder
# nobody has re-sniffed in months is not worth an unbounded query on a page
# that is opened by hand.
_MAX_BACKFILL_ROWS = 5000

LAYER_SOURCE = 0
LAYER_PATTERN = 1
LAYER_SHEET = 2
LAYER_GENERATOR = 3
LAYER_OUTPUT = 4
LAYER_REPORT = 5
# Stages (P4.1) are a band BETWEEN the sheets and the generator they feed, and
# they arrived after the layer ordinals were already pinned by a shipped
# fixture (tests/fixtures/lineage_sample.json), a shipped UI and a shipped
# export. Renumbering five kinds under them would break all three to buy a
# round number, so the new band takes the half step instead. Nothing computes
# with `layer` — dagre ranks from the edges; it is read as an ordering, and
# 2.5 orders correctly.
LAYER_STAGE = 2.5

# Stage edge kinds: a file (its pattern or sheet node) feeds the stage that
# read it, stages feed each other, and the last stage feeds the generator.
EDGE_READ_ORDER = "read_order"   # file -> stage, drawn dashed: "read here, in this order"
EDGE_FEEDS = "feeds"             # stage -> stage


def _iso(dt) -> str | None:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _loads(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


class _Graph:
    """Node/edge accumulator that refuses duplicates.

    Ids are content-derived (`gen:{name}`, `pat:{gen}:{match}`, …) so the same
    node is reached from several directions by construction — two sources
    matching one pattern, two reports on one generator. First writer wins;
    later arrivals are the same node, not a conflict."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._edge_keys: set[tuple[str, str, str]] = set()

    def node(self, node_id: str, layer: int, kind: str, label: str, data: dict) -> str:
        if node_id not in self.nodes:
            self.nodes[node_id] = {"id": node_id, "layer": layer, "kind": kind,
                                   "label": label, "data": data}
        return node_id

    def edge(self, src: str, dst: str, kind: str) -> None:
        key = (src, dst, kind)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        self.edges.append({"from": src, "to": dst, "kind": kind})


def _source_node_id(s: dict) -> str:
    """`src:{ds_id}` for a registry-backed source.

    A source seen only through the pre-migration JSON fallback has no
    `data_sources` row yet (rows are created on ingest or on a human claim,
    never on page render), and it still has to appear in the graph. Its id
    falls back to the key it IS — kind + basename — which is stable for as
    long as the row is missing and becomes `src:{id}` the moment one exists.
    """
    if s.get("source_id"):
        return f"src:{s['source_id']}"
    return f"src:{s['kind']}:{s['name']}"


def _collect_observations(db: Session, dashboards: list, runs_by_dash: dict,
                          gen_name_by_dash: dict) -> dict:
    """(generator_name, data_source_id) -> observations, newest first.

    Keyed by GENERATOR, not by dashboard: two reports built by the same
    generator read a folder the same way, and the graph draws the generator
    once. The newest observation of the pair is the one whose file list is
    current; the older ones exist only to resolve a NULL schema_json.
    """
    run_ids: list[int] = []
    gen_by_run: dict[int, str] = {}
    for d in dashboards:
        gen = gen_name_by_dash.get(d.id)
        if not gen:
            continue
        for r in runs_by_dash.get(d.id, []):
            run_ids.append(r.id)
            gen_by_run[r.id] = gen
    if not run_ids:
        return {}
    rows = (db.query(SourceObservation)
            .filter(SourceObservation.run_id.in_(run_ids))
            .all())
    chains: dict[tuple[str, int], list[SourceObservation]] = {}
    for obs in rows:
        gen = gen_by_run.get(obs.run_id)
        if not gen:
            continue
        chains.setdefault((gen, obs.data_source_id), []).append(obs)
    for chain in chains.values():
        chain.sort(key=lambda o: o.created_at or datetime.min, reverse=True)
    return chains


def collect_chain_files(db: Session, chains: dict, depth: int = 10) -> dict:
    """(generator, data_source_id) -> [{obs, files}] for EVERY observation of
    the chain (newest first, at most `depth` of them), NULL schemas resolved.

    `_collect_files` only needed the newest level; the Data Quality evaluator
    needs the whole history (rows per run, files that came and went), and it
    needs every level to carry a usable schema — a level whose file says
    "unchanged" (schema_json NULL, kind != "legacy") inherits the schema from
    the nearest OLDER observation of the same file name. That is the engine's
    contract in reverse: it skips the sniff when name/size/mtime already
    match, so the previous header IS the current header.

    One query fetches the source_files of all observation ids in the chains,
    the walk is in memory from the oldest level up: `carry[name]` is the
    latest known schema for a name and is cleared when the name is absent
    from a level, so a file that disappears and comes back does not borrow a
    schema from before the gap (its reappearance is a fresh sniff anyway).

    Honest limit: the newest level of every chain is fetched uncapped (that is
    what the graph and the pair view render, and it used to be uncapped); the
    older levels share one query capped at _MAX_BACKFILL_ROWS, ordered by
    observation id DESC, so when the cap bites it is the deepest history —
    across all chains on the page — that loses its carry-forward, never the
    files of the current run. A legacy row (kind == "legacy") is neither
    backfilled nor used as a donor: its schema_json, when present, came from
    a different sniffing path and must not be mistaken for a fresh header.

    Each file dict: name, kind, matched_input, schema, consumed, size,
    mtime_ms, source_file_id. Keys only get added, never dropped.
    """
    obs_key: dict[int, tuple[tuple[str, int], int]] = {}
    out: dict[tuple[str, int], list[dict]] = {}
    for key, chain in chains.items():
        levels = []
        for rank, obs in enumerate(chain[:depth]):
            obs_key[obs.id] = (key, rank)
            levels.append({"obs": obs, "files": []})
        out[key] = levels
    if not obs_key:
        return out

    newest_ids = [oid for oid, (_k, rank) in obs_key.items() if rank == 0]
    older_ids = [oid for oid, (_k, rank) in obs_key.items() if rank > 0]
    rows = (db.query(SourceFile)
            .filter(SourceFile.observation_id.in_(newest_ids))
            .order_by(SourceFile.observation_id.desc(), SourceFile.id)
            .all())
    if older_ids:
        rows += (db.query(SourceFile)
                 .filter(SourceFile.observation_id.in_(older_ids))
                 .order_by(SourceFile.observation_id.desc(), SourceFile.id)
                 .limit(_MAX_BACKFILL_ROWS)
                 .all())
    for sf in rows:
        key, rank = obs_key[sf.observation_id]
        out[key][rank]["files"].append({
            "name": sf.name, "kind": sf.kind,
            "matched_input": sf.matched_input,
            "schema": _loads(sf.schema_json),
            "consumed": _loads(sf.consumed_json),
            "size": sf.size, "mtime_ms": sf.mtime_ms,
            "source_file_id": sf.id,
        })

    for levels in out.values():
        carry: dict[str, dict] = {}
        for level in reversed(levels):          # oldest first
            seen: dict[str, dict] = {}
            for f in level["files"]:
                if f["schema"] is None and f["kind"] != "legacy":
                    prev = carry.get(f["name"])
                    if prev is not None:
                        f["schema"] = prev
                if f["schema"] is not None and f["kind"] != "legacy":
                    seen[f["name"]] = f["schema"]
            carry = seen
    return out


def _collect_files(db: Session, chains: dict) -> dict:
    """(generator, data_source_id) -> [{name, schema, consumed, ...}] of the
    LATEST observation, with NULL schemas resolved backwards through the chain.
    Thin wrapper over `collect_chain_files` — one code path for the walk."""
    return {key: (levels[0]["files"] if levels else [])
            for key, levels in collect_chain_files(db, chains).items()}


def _collect_stages(db: Session, dashes_by_gen: dict, runs_by_dash: dict) -> dict:
    """generator name -> [RunStage] of the generator's LATEST run that has
    any, ordered by `order`.

    Latest RUN, not a union across runs: stages describe one execution of the
    code, and merging two runs' step lists would invent a pipeline that never
    happened. A generator whose newest run emitted no stages (an older engine
    build, a generator that never calls `stage()`, the agent engine — which
    has no hooks at all) falls back to its newest run that did, so the graph
    keeps showing the last known pipeline instead of blanking it; nothing
    older than the observation window `datasources._recent_runs` already
    bounds is reachable either way.
    """
    run_ids: list[int] = []
    gen_by_run: dict[int, tuple[str, datetime]] = {}
    for gen_name, gen_dashes in dashes_by_gen.items():
        for d in gen_dashes:
            for r in runs_by_dash.get(d.id, []):
                run_ids.append(r.id)
                gen_by_run[r.id] = (gen_name, r.started_at or datetime.min)
    if not run_ids:
        return {}

    by_run: dict[int, list[RunStage]] = {}
    for st in (db.query(RunStage).filter(RunStage.run_id.in_(run_ids)).all()):
        by_run.setdefault(st.run_id, []).append(st)

    best: dict[str, tuple[datetime, int, list[RunStage]]] = {}
    for run_id, stages in by_run.items():
        gen_name, started = gen_by_run[run_id]
        current = best.get(gen_name)
        # (started_at, run_id) breaks the tie between two runs stamped in the
        # same second the same way the rest of this module does: the later id.
        if current is None or (started, run_id) > (current[0], current[1]):
            best[gen_name] = (started, run_id, sorted(stages, key=lambda x: x.order))
    return {gen_name: v[2] for gen_name, v in best.items()}


def _add_stage_nodes(g: _Graph, gen_name: str, gid, gen_id: str,
                     stages: list) -> tuple[list[str], dict[str, list[str]]]:
    """The stage chain of one generator: nodes, stage->stage edges, and the
    edges from the last stage(s) into the generator. Returns
    (stage node ids in call order, {casefolded file name: [stage id, ...]}).

    Upstream resolution follows the contract's two cases: an explicit
    `inputs` list names other stages (unknown names are dropped — the author
    may have renamed a step between runs), and a NULL `inputs` means "the
    previous call", which is what a linear script produces without the author
    writing anything at all.

    A stage nobody consumes is a terminal one and feeds the generator. If
    every stage has a consumer — only possible when `inputs` describes a cycle
    — the LAST one by call order feeds it anyway, so the generator is never
    left disconnected from the files that reached it.
    """
    ids: list[str] = []
    id_by_order: dict[int, str] = {}
    id_by_name: dict[str, str] = {}
    for st in stages:
        sid = f"stage:{gid}:{st.order}"
        if sid in g.nodes:
            continue
        g.node(sid, LAYER_STAGE, "stage", st.name, {
            "generator": gen_name, "order": st.order, "rows": st.rows,
            "columns": st.get_columns(), "inputs": st.get_inputs(),
            "read_files": st.get_read_files(), "ms": st.ms,
        })
        ids.append(sid)
        id_by_order[st.order] = sid
        id_by_name.setdefault(st.name, sid)

    consumed: set[str] = set()
    for i, st in enumerate(stages):
        sid = id_by_order.get(st.order)
        if sid is None:
            continue
        declared = st.get_inputs()
        if declared is None:
            prev = stages[i - 1] if i > 0 else None
            upstream = [id_by_order[prev.order]] if prev is not None and prev.order in id_by_order else []
        else:
            upstream = [id_by_name[n] for n in declared if n in id_by_name]
        for up in upstream:
            if up == sid:
                continue
            g.edge(up, sid, EDGE_FEEDS)
            consumed.add(up)

    terminal = [sid for sid in ids if sid not in consumed]
    if not terminal and ids:
        terminal = [ids[-1]]
    for sid in terminal:
        g.edge(sid, gen_id, "reads")

    by_file: dict[str, list[str]] = {}
    for st in stages:
        sid = id_by_order.get(st.order)
        if sid is None:
            continue
        for name in st.get_read_files():
            bucket = by_file.setdefault(name.casefold(), [])
            if sid not in bucket:
                bucket.append(sid)
    return ids, by_file


def _observed_sheets(files: list[dict]) -> dict:
    """Observed sheet name (normalized) -> {label, columns{norm: label}, rows,
    reason_counts{reason text: n_files}}.

    `reason_counts` tallies the per-sheet `reason` the engine's `sniff()`
    attaches when a sheet's header could not be read (commit 633eaaa) — one
    file may report a reason while another file of the same pattern reads
    fine, so the count is kept per reason text rather than collapsed to a
    single flag. Callers decide "header missing" by columns being empty AND
    a reason present (see the sheet-node builders below)."""
    out: dict[str, dict] = {}
    for f in files:
        schema = f.get("schema")
        if not isinstance(schema, dict):
            continue
        for sh in (schema.get("sheets") or []):
            if not isinstance(sh, dict):
                continue
            label = str(sh.get("name") or "")
            key = norm(label)
            entry = out.setdefault(key, {"label": label, "columns": {}, "rows": None,
                                          "reason_counts": {}})
            for h in (sh.get("header") or []):
                if isinstance(h, str) and h.strip():
                    entry["columns"].setdefault(norm(h), h.strip())
            rows = sh.get("rows")
            if isinstance(rows, int) and (entry["rows"] is None or rows > entry["rows"]):
                entry["rows"] = rows
            reason = sh.get("reason")
            if isinstance(reason, str) and reason:
                rc = entry["reason_counts"]
                rc[reason] = rc.get(reason, 0) + 1
    return out


def _majority_reason(reason_counts: dict[str, int]) -> str | None:
    """Most-reported reason text, ties broken by text so the pick is
    deterministic across runs with the same counts."""
    if not reason_counts:
        return None
    return max(reason_counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _consumed_sheets(files: list[dict]) -> dict:
    """Observed sheet name (normalized) -> {columns: set, all: bool}. Absent key
    means NOTHING could have observed a read of that sheet — the tri-state's
    null, not a "was not read"."""
    out: dict[str, dict] = {}
    for f in files:
        consumed = f.get("consumed")
        if not isinstance(consumed, dict):
            continue
        for name, entry in (consumed.get("sheets") or {}).items():
            if not isinstance(entry, dict):
                continue
            acc = out.setdefault(norm(str(name)), {"columns": set(), "all": False})
            acc["all"] = acc["all"] or bool(entry.get("all"))
            for c in (entry.get("columns") or []):
                if isinstance(c, str):
                    acc["columns"].add(norm(c))
    return out


def _columns_for_sheet(declared_cols: list[str], observed: dict, consumed: dict | None,
                       descriptions: dict[str, str] | None = None) -> list[dict]:
    """Merge one sheet's declared and observed headers into tri-state entries.

    `consumed` is None when no read of this sheet could have been recorded;
    a column that was never OBSERVED is null too, because "we did not see the
    column" makes "was it read" unanswerable rather than false. When the sheet
    WAS observed and hooks did record its reads, an absent column is False —
    for the declared and the undeclared alike, since the evidence is the same
    one either way. (The sample fixture shows null on its undeclared column;
    that is an illustration of the shape, not a second rule.)

    `descriptions` (P3.6), keyed by the column's OWN spelling (not norm()'d —
    the caller already merged annotation-over-manifest by exact name), fills
    an optional "description" per entry so the column chip's tooltip has
    something to say beyond declared/observed/consumed. Only undeclared
    (observed-only) columns are looked up by their observed label; a
    declared column is looked up by its declared spelling.
    """
    out: list[dict] = []
    seen: set[str] = set()
    descriptions = descriptions or {}

    def _consumed_state(key: str, is_observed: bool):
        if consumed is None or not is_observed:
            return None
        return True if (consumed["all"] or key in consumed["columns"]) else False

    for col in declared_cols:
        key = norm(col)
        if key in seen:
            continue
        seen.add(key)
        is_observed = key in observed
        entry = {"name": col, "declared": True, "observed": is_observed,
                "consumed": _consumed_state(key, is_observed)}
        desc = descriptions.get(col)
        if desc:
            entry["description"] = desc
        out.append(entry)
    for key, label in observed.items():
        if key in seen:
            continue
        seen.add(key)
        entry = {"name": label, "declared": False, "observed": True,
                "consumed": _consumed_state(key, True)}
        desc = descriptions.get(label)
        if desc:
            entry["description"] = desc
        out.append(entry)
    return out


def build_graph(db: Session, dashboards: list, *, now: datetime | None = None) -> dict:
    """{"nodes": [...], "edges": [...], "meta": {...}} — the shape pinned by
    tests/fixtures/lineage_sample.json. `dashboards` is the caller's scope
    (what this user may run); nothing outside it reaches the graph."""
    now = now or datetime.utcnow()
    g = _Graph()

    agg = datasources.build(db, dashboards, now=now)
    dash_ids = [d.id for d in dashboards]
    runs_by_dash = datasources._recent_runs(db, dash_ids)
    runs_considered = sum(len(v) for v in runs_by_dash.values())

    gen_ids = {d.generator_id for d in dashboards if d.generator_id}
    gen_names = dict(
        db.query(Generator.id, Generator.name).filter(Generator.id.in_(gen_ids)).all()
    ) if gen_ids else {}
    versions = {}
    if gen_ids:
        for gid, vnum, raw in (db.query(GeneratorVersion.generator_id,
                                        GeneratorVersion.version_number,
                                        GeneratorVersion.manifest_json)
                               .filter(GeneratorVersion.generator_id.in_(gen_ids),
                                       GeneratorVersion.is_current == True)  # noqa: E712
                               .all()):
            versions[gid] = (vnum, _loads(raw) or {})

    gen_name_by_dash = {d.id: gen_names.get(d.generator_id) for d in dashboards}
    dashes_by_gen: dict[str, list] = {}
    for d in dashboards:
        name = gen_name_by_dash.get(d.id)
        if name:
            dashes_by_gen.setdefault(name, []).append(d)

    chains = _collect_observations(db, dashboards, runs_by_dash, gen_name_by_dash)
    files_by_pair = _collect_files(db, chains)

    # ---- layer 0: sources ------------------------------------------------
    src_ids: dict[str, str] = {}        # datasources key -> node id
    src_by_ds_id: dict[int, str] = {}   # data_sources.id -> node id
    src_by_name: dict[str, str] = {}    # folder key_name -> node id (write-back targets)
    sources_by_gen: dict[str, list[dict]] = {}
    for s in agg["sources"]:
        node_id = _source_node_id(s)
        src_ids[s["key"]] = node_id
        if s.get("source_id"):
            src_by_ds_id[s["source_id"]] = node_id
        if s["kind"] == datasources.KIND_GREENPLUM:
            data = {"source_id": s["source_id"], "queries": s["queries"],
                    "shared": s["shared"], "key_name": s["name"]}
        elif s["kind"] == datasources.KIND_TABLE:
            # ADR-16 reference table. Not in `src_by_name`: that map exists to
            # resolve write-back TARGETS, and nothing writes back into a
            # platform table from a generator run.
            tbl = s.get("table") or {}
            data = {"source_id": s["source_id"], "shared": s["shared"],
                    "key_name": s["name"], "certain": True,
                    "exists": bool(tbl.get("exists")),
                    "current_version": tbl.get("current_version") or 0,
                    "row_count": tbl.get("row_count") or 0,
                    "responsible": tbl.get("responsible")}
        else:
            src_by_name.setdefault(s["name"], node_id)
            data = {
                "source_id": s["source_id"], "declared_path": s["declared_path"],
                "files": s["files"], "bytes": s["bytes"],
                "data_age_days": s["data_age_days"], "read_age_days": s["read_age_days"],
                "fingerprint": s["fingerprint"], "shared": s["shared"],
                "data_stale": s["data_stale"], "unread": s["unread"],
                "key_name": s["name"], "certain": s["certain"],
                # Sent by the application (see datasources): shown in
                # the node drawer; the right is the same as for the graph itself.
                "root_path": s["root_path"], "root_id": s["root_id"],
                "file_paths": s["file_paths"],
            }
        g.node(node_id, LAYER_SOURCE, s["kind"], s["label"] or s["name"], data)
        for gen in s["generators"]:
            sources_by_gen.setdefault(gen, []).append(s)

    # ---- layers 1-4: one generator at a time -----------------------------
    reports_by_gen: dict[str, list[str]] = {}
    sheet_node_ids: set[str] = set()
    # sheet-bearing pattern id -> the [(node id, edge kind)] its sheets read
    # into, so the truncation path below can restore the same connection
    # without the sheets.
    patterns_with_sheets: dict[str, list[tuple[str, str]]] = {}
    stages_by_gen = _collect_stages(db, dashes_by_gen, runs_by_dash)

    for gen_name, gen_dashes in dashes_by_gen.items():
        gid = gen_dashes[0].generator_id
        vnum, manifest = versions.get(gid, (None, {}))
        inputs = genmanifest.inputs_for(manifest)
        gen_sources = sources_by_gen.get(gen_name, [])
        folder_sources = [s for s in gen_sources if s["kind"] == datasources.KIND_FOLDER]
        # ADR-16: slug -> node id, for the `source: "platform"` inputs below.
        table_src_by_slug = {s["name"]: src_ids[s["key"]] for s in gen_sources
                             if s["kind"] == datasources.KIND_TABLE}
        observed_any = any(key[0] == gen_name and chain for key, chain in chains.items())

        # Descriptions (P3.6): the manifest author's own column descriptions
        # ("<match>/<sheet>" -> {column: description}), overlaid by any
        # data-owner Annotation rows on the sources THIS generator actually
        # reads — the annotation is the more current voice, so it wins on a
        # key collision. Scoped per generator (not the whole graph) because
        # column names are only unambiguous within one generator's own
        # declared inputs.
        manifest_col_desc = genmanifest.column_descriptions(manifest)
        ann_by_key: dict[tuple[str, str, str | None], str] = {}
        ds_ids = {s.get("source_id") for s in folder_sources if s.get("source_id")}
        if ds_ids:
            for a in db.query(Annotation).filter(Annotation.data_source_id.in_(ds_ids)).all():
                ann_by_key[(a.match, a.sheet, a.column or None)] = a.description

        # Entries folded in from `required_files` do not make a manifest
        # declared: the engine still reads every Excel file for it.
        legacy = genmanifest.is_legacy_inputs(inputs)
        if legacy and folder_sources:
            # The engine reads every Excel file when `inputs` is empty. Drawing
            # nothing there would say "this generator reads no files", which is
            # the opposite of the truth.
            inputs = [i for i in inputs if i.get("from_required_files")] + [
                {"match": "*", "role": "data", "required": False,
                 "sheets": [], "from_required_files": False, "legacy": True}]

        gen_id = f"gen:{gen_name}"
        gen_data = {"generator_id": gid, "version": vnum,
                    "engine": manifest.get("engine") or "auto",
                    "reports": len(gen_dashes), "observed": observed_any}
        if legacy:
            gen_data["legacy_inputs"] = True
        # Stages, when the last run reported any: the generator node stops
        # being the single sink of every file and becomes the end of a chain.
        gen_stages = stages_by_gen.get(gen_name) or []
        stage_ids, stages_by_file = ([], {})
        if gen_stages:
            stage_ids, stages_by_file = _add_stage_nodes(g, gen_name, gid, gen_id, gen_stages)
            gen_data["stages"] = len(stage_ids)
        g.node(gen_id, LAYER_GENERATOR, "generator", gen_name, gen_data)

        # GreenPlum has nothing to walk and nothing to sniff: the connection
        # feeds the generator directly, with no pattern or sheet in between.
        for s in gen_sources:
            if s["kind"] == datasources.KIND_GREENPLUM:
                g.edge(src_ids[s["key"]], gen_id, "reads")

        # Attribute every observed file to the FIRST pattern that admits it —
        # the engine's own loop order, so the graph credits the same rule the
        # run did.
        matched_total: dict[str, int] = {item["match"]: 0 for item in inputs}
        matched_per_source: dict[tuple[str, str], int] = {}
        files_per_pattern: dict[str, list[dict]] = {item["match"]: [] for item in inputs}
        for s in folder_sources:
            ds_id = s.get("source_id")
            pair_files = files_by_pair.get((gen_name, ds_id), []) if ds_id else []
            for f in pair_files:
                match = globmatch.first_match([item["match"] for item in inputs], f["name"])
                if match is None:
                    continue
                matched_total[match] += 1
                matched_per_source[(s["key"], match)] = matched_per_source.get((s["key"], match), 0) + 1
                files_per_pattern[match].append(f)

        for item in inputs:
            match = item["match"]
            pat_id = f"pat:{gen_name}:{match}"
            pat_data = {"generator": gen_name, "role": item.get("role", "data"),
                        "required": bool(item.get("required")),
                        "matched_files": matched_total[match],
                        "unmatched": matched_total[match] == 0}
            if item.get("source") == "platform":
                # The workbook is written by the platform at run time, so it
                # is never among the files a folder observation saw: counting
                # it as "unmatched" would paint a healthy input red.
                pat_data["platform"] = True
                pat_data["table"] = item.get("table")
                pat_data["unmatched"] = False
            if item.get("from_required_files"):
                pat_data["from_required_files"] = True
            if item.get("legacy"):
                pat_data["legacy"] = True
            schema_errors: dict[str, int] = {}
            for f in files_per_pattern[match]:
                schema = f.get("schema")
                if isinstance(schema, dict):
                    err = schema.get("error")
                    if isinstance(err, str) and err:
                        schema_errors[err] = schema_errors.get(err, 0) + 1
            if schema_errors:
                pat_data["schema_errors"] = schema_errors
            g.node(pat_id, LAYER_PATTERN, "pattern", match, pat_data)

            # A platform input is fed by exactly ONE source — its reference
            # table — and by no folder: drawing every folder into it would
            # claim the workbook could arrive from the analyst's disk, which
            # is the confusion `source: "platform"` exists to end.
            if item.get("source") == "platform":
                tnode = table_src_by_slug.get(item.get("table"))
                if tnode:
                    g.edge(tnode, pat_id, "matches")
            linked = False
            for s in folder_sources:
                if item.get("source") == "platform":
                    break
                ds_id = s.get("source_id")
                has_files = bool(files_by_pair.get((gen_name, ds_id))) if ds_id else False
                # A source with no file list for this generator (never observed,
                # or observed only outside the window) is linked to every
                # pattern: "we do not know which files it holds" must not read
                # as "it holds none".
                if matched_per_source.get((s["key"], match)) or not has_files:
                    g.edge(src_ids[s["key"]], pat_id, "matches")
                    linked = True
            if not linked and item.get("source") != "platform":
                # Declared but absent everywhere — still shown, hanging off the
                # folders it was supposed to find files in.
                for s in folder_sources:
                    g.edge(src_ids[s["key"]], pat_id, "matches")

            # Which stage(s) read the files this pattern matched. Attribution
            # is per PATTERN, not per file: the graph has no file node (140
            # weekly workbooks are one `Stock_*.xlsx`), so a stage that read
            # any of them is drawn as reading the pattern. Files no stage
            # claims keep the old edge straight into the generator — a
            # generator that stages only part of its work still shows the
            # rest.
            sinks = [(gen_id, "reads")]
            if stages_by_file:
                claimed = set()
                for f in files_per_pattern[match]:
                    claimed.update(stages_by_file.get(str(f["name"]).casefold(), ()))
                ordered = [sid for sid in stage_ids if sid in claimed]
                if ordered:
                    sinks = [(sid, EDGE_READ_ORDER) for sid in ordered]

            sheets_added = _add_sheet_nodes(g, gen_name, item, pat_id, gen_id,
                                            files_per_pattern[match], pat_data,
                                            manifest_col_desc, ann_by_key, sinks)
            for sid in sheets_added:
                sheet_node_ids.add(sid)
            if sheets_added:
                patterns_with_sheets[pat_id] = sinks
            else:
                for target, kind in sinks:
                    g.edge(pat_id, target, kind)

        _add_outputs(db, g, gen_name, gen_id, manifest, gen_dashes, runs_by_dash,
                     src_by_name, reports_by_gen)

    # ---- layer 5: reports ------------------------------------------------
    for rep in agg["reports"]:
        rep_id = f"rep:{rep['id']}"
        g.node(rep_id, LAYER_REPORT, "report", rep["title"], {
            "dashboard_id": rep["id"],
            "status": rep["last_run_status"],
            "last_run_at": _iso(rep["last_run_at"]),
            "engine": rep["engine"],
        })
        for out_id in reports_by_gen.get(rep["generator"], []):
            g.edge(out_id, rep_id, "publishes")

    truncated = False
    if len(g.nodes) > MAX_NODES:
        # Sheets are the only axis a single manifest can inflate (32 sheets x
        # 256 columns each). Dropping them keeps the folder→generator→report
        # spine intact, which is the part nobody can reconstruct by hand.
        truncated = True
        for sid in sheet_node_ids:
            g.nodes.pop(sid, None)
        g.edges = [e for e in g.edges
                   if e["from"] not in sheet_node_ids and e["to"] not in sheet_node_ids]
        for pat_id, sinks in patterns_with_sheets.items():
            for target, kind in sinks:
                g.edges.append({"from": pat_id, "to": target, "kind": kind})

    meta = {"generated_at": now.isoformat(), "runs_considered": runs_considered,
            "stale_days": datasources.STALE_DAYS}
    if truncated:
        meta["truncated"] = True
    return {"nodes": list(g.nodes.values()), "edges": g.edges, "meta": meta}


def _add_sheet_nodes(g: _Graph, gen_name: str, item: dict, pat_id: str, gen_id: str,
                     files: list[dict], pat_data: dict,
                     manifest_col_desc: dict[str, dict[str, str]] | None = None,
                     ann_by_key: dict[tuple[str, str, str | None], str] | None = None,
                     sinks: list[tuple[str, str]] | None = None) -> list[str]:
    """Sheet nodes for one pattern; returns their ids (empty = none to draw).

    A DECLARED sheet always gets a node, observed or not — an absent one is the
    finding. An UNDECLARED observed sheet gets a node only when the pattern
    declares no sheets at all (there is nothing else to draw); when the author
    did declare sheets, the extras are a diff line on the pattern instead, or a
    workbook with 30 incidental tabs would bury the two that matter.

    `manifest_col_desc`/`ann_by_key` (P3.6) are the generator's own
    description sources (build_graph computes them once per generator, not
    per sheet); a sheet's node data gets a "description" when a sheet-level
    annotation exists, and each column entry gets one when either source has
    something to say — annotation overrides the manifest on a collision.
    """
    observed = _observed_sheets(files)
    consumed = _consumed_sheets(files)
    # `sinks` (P4.1) is where a sheet's read edge lands: the generator, as it
    # always did, or the stage(s) that consumed this pattern's files.
    sinks = sinks or [(gen_id, "reads")]
    declared = [sh for sh in (item.get("sheets") or []) if isinstance(sh, dict)]
    out: list[str] = []
    used_observed: set[str] = set()
    manifest_col_desc = manifest_col_desc or {}
    ann_by_key = ann_by_key or {}

    for sh in declared:
        sheet_name = str(sh.get("name") or "*")
        sheet_id = f"sheet:{gen_name}:{item['match']}:{sheet_name}"
        if sheet_id in g.nodes:
            continue
        cols: dict[str, str] = {}
        rows = None
        matched_consumed: dict | None = None
        reason_counts: dict[str, int] = {}
        for key, obs in observed.items():
            if not globmatch.matches(sheet_name, obs["label"]):
                continue
            used_observed.add(key)
            cols.update(obs["columns"])
            if isinstance(obs["rows"], int) and (rows is None or obs["rows"] > rows):
                rows = obs["rows"]
            for r, n in (obs.get("reason_counts") or {}).items():
                reason_counts[r] = reason_counts.get(r, 0) + n
            c = consumed.get(key)
            if c is not None:
                if matched_consumed is None:
                    matched_consumed = {"columns": set(), "all": False}
                matched_consumed["columns"] |= c["columns"]
                matched_consumed["all"] = matched_consumed["all"] or c["all"]
        declared_cols = [str(c) for c in (sh.get("columns") or []) if isinstance(c, str)]
        col_desc = dict(manifest_col_desc.get(f"{item['match']}/{sheet_name}", {}))
        for (m, s, c), desc in ann_by_key.items():
            if m == item["match"] and s == sheet_name and c:
                col_desc[c] = desc
        entries = _columns_for_sheet(declared_cols, cols, matched_consumed, col_desc)
        diff: dict[str, list[str]] = {}
        missing = [e["name"] for e in entries if e["declared"] and not e["observed"]]
        extra = [e["name"] for e in entries if not e["declared"]]
        if missing:
            diff["missing_columns"] = missing
        if extra:
            diff["extra_columns"] = extra
        node_data = {"columns": entries, "observed_rows": rows, "diff": diff}
        if not cols and reason_counts:
            node_data["header_missing"] = True
            node_data["reason"] = _majority_reason(reason_counts)
        sheet_desc = ann_by_key.get((item["match"], sheet_name, None))
        if sheet_desc:
            node_data["description"] = sheet_desc
        g.node(sheet_id, LAYER_SHEET, "sheet", sheet_name, node_data)
        g.edge(pat_id, sheet_id, "contains")
        for target, kind in sinks:
            g.edge(sheet_id, target, kind)
        out.append(sheet_id)

    if not declared:
        for key, obs in observed.items():
            sheet_id = f"sheet:{gen_name}:{item['match']}:{obs['label']}"
            if sheet_id in g.nodes:
                continue
            # A pattern with no declared sheets is the one case where the
            # drawer synthesises its annotation target as sheet='*' (there is
            # no declared sheet name to key against) — so a note saved there
            # must be looked up by '*' too, not only by the observed label,
            # or it saves and round-trips through GET but never renders.
            col_desc = {}
            for (m, s, c), desc in ann_by_key.items():
                if m == item["match"] and c and s in (obs["label"], "*"):
                    col_desc[c] = desc
            entries = _columns_for_sheet([], obs["columns"], consumed.get(key), col_desc)
            node_data = {"columns": entries, "observed_rows": obs["rows"],
                        "diff": {"extra_columns": [e["name"] for e in entries]} if entries else {}}
            if not obs["columns"] and obs.get("reason_counts"):
                node_data["header_missing"] = True
                node_data["reason"] = _majority_reason(obs["reason_counts"])
            sheet_desc = (ann_by_key.get((item["match"], obs["label"], None))
                        or ann_by_key.get((item["match"], "*", None)))
            if sheet_desc:
                node_data["description"] = sheet_desc
            g.node(sheet_id, LAYER_SHEET, "sheet", obs["label"], node_data)
            g.edge(pat_id, sheet_id, "contains")
            for target, kind in sinks:
                g.edge(sheet_id, target, kind)
            out.append(sheet_id)
    else:
        extra_sheets = [obs["label"] for key, obs in observed.items() if key not in used_observed]
        if extra_sheets:
            pat_data.setdefault("diff", {})["extra_sheets"] = sorted(extra_sheets)
    return out


def _add_outputs(db: Session, g: _Graph, gen_name: str, gen_id: str, manifest: dict,
                 gen_dashes: list, runs_by_dash: dict, src_by_name: dict,
                 reports_by_gen: dict) -> None:
    """Layer 4. `html` is unconditional (every generator publishes one), slots
    come from the manifest, and write-backs come from the newest run that
    reported any — the only server-side record of a file the generator put
    back into somebody's folder."""
    publishing: list[str] = []

    html_id = g.node(f"out:{gen_name}:html", LAYER_OUTPUT, "output",
                     "dashboard.html", {"slot": None})
    g.edge(gen_id, html_id, "writes")
    publishing.append(html_id)

    for slot in ((manifest.get("outputs") or {}).get("slots") or []):
        if not isinstance(slot, dict) or not isinstance(slot.get("index"), int):
            continue
        slot_id = g.node(f"out:{gen_name}:{slot['index']}", LAYER_OUTPUT, "output",
                         str(slot.get("name") or f"slot_{slot['index']}"),
                         {"slot": slot["index"], "type": slot.get("type"),
                          "write_back": bool(slot.get("write_back"))})
        g.edge(gen_id, slot_id, "writes")
        publishing.append(slot_id)

    # Newest run across this generator's dashboards that reported outputs.
    best_run = None
    for d in gen_dashes:
        for r in runs_by_dash.get(d.id, []):
            if not r.outputs_json:
                continue
            if best_run is None or (r.started_at or datetime.min) > (best_run.started_at or datetime.min):
                best_run = r
    if best_run is not None:
        payload = _loads(best_run.outputs_json) or {}
        for wb in (payload.get("write_back") or []):
            if not isinstance(wb, dict) or not isinstance(wb.get("name"), str):
                continue
            targets = [str(t) for t in (wb.get("targets") or [])]
            failed = [str(t) for t in (wb.get("failed") or [])]
            wb_id = g.node(f"out:{gen_name}:wb:{wb['name']}", LAYER_OUTPUT, "write_back",
                           wb["name"], {"targets": targets, "failed": failed,
                                        "last_written": _iso(best_run.finished_at or best_run.started_at)})
            g.edge(gen_id, wb_id, "writes")
            # The backwards edge: this folder is both input and output.
            for t in targets:
                if t in src_by_name:
                    g.edge(wb_id, src_by_name[t], "writes_back")

    reports_by_gen[gen_name] = publishing
