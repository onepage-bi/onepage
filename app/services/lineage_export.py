"""Lineage export — three views of the graph `lineage.build_graph` returns:
JSON (the graph as-is, optionally scoped), Markdown (a readable digest with
column profiles), and Mermaid (an erDiagram a colleague can paste anywhere).

Nothing here recomputes lineage; it slices and formats what the page already
draws. `subgraph()` is the one piece of logic, and it is not new logic —
it is `reach()` from static/js/lineage.js ported line for line: an
undirected BFS over every edge of the FULL graph (all layers, `writes_back`
included in both directions, exactly as the UI highlights on hover/click)
from one focus node. A colleague who exports "focused on this folder" gets
the same closure the page was showing them, not an approximation of it.
"""
from __future__ import annotations

import hashlib
import re

from sqlalchemy.orm import Session

from app.models import Generator, GeneratorVersion, QualityResult
from app.services import datasources, genmanifest, globmatch, lineage
from app.services.schemadiff import norm

# A profile-enrichment pass over an export is a convenience, not the Data
# Quality page — capped so a workspace with thousands of observed files
# cannot turn "download the lineage" into an unbounded query.
_MAX_PROFILE_FILES = 2000


# -- Scoping -----------------------------------------------------------
def subgraph(graph: dict, focus_id: str | None) -> dict:
    """`graph` restricted to the closure of `focus_id` — every node reachable
    by walking edges in EITHER direction, starting from that one node. With
    no focus, or a focus id the graph does not contain, the graph is returned
    unchanged (an unknown focus is not an error here; the route decides what
    "not found" means)."""
    if not focus_id:
        return graph
    nodes_by_id = {n["id"]: n for n in graph.get("nodes") or []}
    if focus_id not in nodes_by_id:
        return {"nodes": [], "edges": [], "meta": dict(graph.get("meta") or {}), "focus": focus_id}

    out_to: dict[str, list[str]] = {}
    in_from: dict[str, list[str]] = {}
    for e in graph.get("edges") or []:
        out_to.setdefault(e["from"], []).append(e["to"])
        in_from.setdefault(e["to"], []).append(e["from"])

    live = {focus_id}
    stack = [focus_id]
    while stack:
        x = stack.pop()
        for y in in_from.get(x, []):
            if y not in live:
                live.add(y)
                stack.append(y)
    stack = [focus_id]
    while stack:
        x = stack.pop()
        for y in out_to.get(x, []):
            if y not in live:
                live.add(y)
                stack.append(y)

    return {
        "nodes": [n for n in graph["nodes"] if n["id"] in live],
        "edges": [e for e in graph["edges"] if e["from"] in live and e["to"] in live],
        "meta": dict(graph.get("meta") or {}),
        "focus": focus_id,
    }


def _adjacency(graph: dict) -> dict[str, list[str]]:
    out_to: dict[str, list[str]] = {}
    for e in graph.get("edges") or []:
        out_to.setdefault(e["from"], []).append(e["to"])
    return out_to


def _generator_after(nodes: dict, out_to: dict, node_id: str) -> dict | None:
    """The generator a sheet (or pattern) feeds, walking THROUGH stage nodes.

    Before P4.1 this was one hop — a sheet pointed at its generator. With
    stages the sheet points at the step that read it and the generator is at
    the end of that chain, so every caller that used to ask "which generator
    is downstream of this node" asks here instead. Only stage nodes are
    walked through (the chain is short and bounded at 32) and `seen` keeps a
    cyclic `inputs` declaration from spinning."""
    stack = list(out_to.get(node_id, []))
    seen: set[str] = set()
    while stack:
        x = stack.pop(0)
        if x in seen:
            continue
        seen.add(x)
        node = nodes.get(x)
        if node is None:
            continue
        if node["kind"] == "generator":
            return node
        if node["kind"] == "stage":
            stack.extend(out_to.get(x, []))
    return None


def _stages_of(nodes: dict, graph: dict, gen_id: str) -> list[dict]:
    """This generator's stage nodes, in call order."""
    out = [n for n in (graph.get("nodes") or [])
           if n["kind"] == "stage" and (n.get("data") or {}).get("generator")
           == nodes[gen_id]["label"]]
    return sorted(out, key=lambda n: (n.get("data") or {}).get("order") or 0)


# -- Column profiles -----------------------------------------------------
def collect_profiles(db: Session, dashboards: list) -> dict:
    """(generator_name, pattern_match, sheet_name_RAW, column_norm) -> the
    profile's metric dict (type/nulls/distinct/top/…), from the newest
    observation's `quality_results` rows where `check_type == "profile"`.

    The sheet component is the PHYSICAL sheet name the engine profiled, kept
    raw (not normalized) — sheet nodes in the graph are keyed by the
    DECLARED name, which is a glob (`*` by default, docs/GENERATOR_AUTHORING
    _RU.md), not necessarily the literal sheet name. Callers match a node's
    label against these raw names with `globmatch.matches`, the same
    comparison `lineage._add_sheet_nodes` uses to fold observed sheets into
    a declared one.

    An export does not need `quality.evaluate`'s whole pipeline (that
    resolves "still true of this run" across every check, over a run
    history) — one number per column is enough, so this walks the same
    newest-files-per-(generator, source) map `lineage.py` already builds
    and reads their profile rows directly. Capped by `_MAX_PROFILE_FILES`;
    when several files match the same pattern, the FIRST one carrying a
    profile for a given column wins — profiles are not merged across files.
    """
    dash_ids = [d.id for d in dashboards]
    runs_by_dash = datasources._recent_runs(db, dash_ids)
    gen_ids = {d.generator_id for d in dashboards if d.generator_id}
    gen_names = dict(
        db.query(Generator.id, Generator.name).filter(Generator.id.in_(gen_ids)).all()
    ) if gen_ids else {}
    versions: dict[int, dict] = {}
    if gen_ids:
        for gid, raw in (db.query(GeneratorVersion.generator_id, GeneratorVersion.manifest_json)
                         .filter(GeneratorVersion.generator_id.in_(gen_ids),
                                 GeneratorVersion.is_current == True)  # noqa: E712
                         .all()):
            versions[gid] = lineage._loads(raw) or {}
    gen_name_by_dash = {d.id: gen_names.get(d.generator_id) for d in dashboards}

    chains = lineage._collect_observations(db, dashboards, runs_by_dash, gen_name_by_dash)
    files_by_pair = lineage._collect_files(db, chains)

    dashes_by_gen: dict[str, list] = {}
    for d in dashboards:
        name = gen_name_by_dash.get(d.id)
        if name:
            dashes_by_gen.setdefault(name, []).append(d)

    sf_ids: list[int] = []
    sf_context: dict[int, tuple[str, str]] = {}   # source_file_id -> (generator, match)
    for gen_name, gen_dashes in dashes_by_gen.items():
        if len(sf_ids) >= _MAX_PROFILE_FILES:
            break
        gid = gen_dashes[0].generator_id
        manifest = versions.get(gid) or {}
        patterns = [item["match"] for item in genmanifest.inputs_for(manifest)] or ["*"]
        for (fgen, _ds_id), files in files_by_pair.items():
            if fgen != gen_name:
                continue
            for f in files:
                sfid = f.get("source_file_id")
                if sfid is None:
                    continue
                match = globmatch.first_match(patterns, f["name"])
                if match is None:
                    continue
                sf_context.setdefault(sfid, (gen_name, match))
                sf_ids.append(sfid)
                if len(sf_ids) >= _MAX_PROFILE_FILES:
                    break
            if len(sf_ids) >= _MAX_PROFILE_FILES:
                break

    if not sf_ids:
        return {}

    out: dict[tuple[str, str, str, str], dict] = {}
    rows = (db.query(QualityResult)
            .filter(QualityResult.source_file_id.in_(sf_ids),
                    QualityResult.check_type == "profile")
            .all())
    for qr in rows:
        ctx = sf_context.get(qr.source_file_id)
        if ctx is None or not qr.sheet or not qr.column:
            continue
        gen_name, match = ctx
        key = (gen_name, match, qr.sheet, norm(qr.column))
        out.setdefault(key, qr.get_metric())
    return out


def _group_profiles(profiles: dict) -> dict[tuple[str, str, str], list[tuple[str, dict]]]:
    """`profiles` regrouped to (generator, pattern_match, column_norm) ->
    [(raw_sheet_name, metric), …], so a lookup by a declared (possibly glob)
    sheet label only has to walk the raw names for that column, not the
    whole profile map."""
    grouped: dict[tuple[str, str, str], list[tuple[str, dict]]] = {}
    for (gen_name, match, sheet_raw, col_norm), metric in profiles.items():
        grouped.setdefault((gen_name, match, col_norm), []).append((sheet_raw, metric))
    return grouped


def _lookup_profile(grouped: dict, gen_label: str, pat_label: str,
                    sheet_label: str, column_name: str) -> dict | None:
    """The profile for one rendered column — matched the same way
    `lineage._add_sheet_nodes` folds an observed sheet into a declared node:
    `globmatch.matches(declared_name_or_glob, physical_sheet_name)`, not a
    literal string comparison. `sheet_label` is a node label, which is
    either the declared name (a glob, `*` by default) or, when nothing was
    declared, the physical name itself — either way this one check covers
    it, case-insensitively."""
    for sheet_raw, metric in grouped.get((gen_label, pat_label, norm(column_name)), []):
        if globmatch.matches(sheet_label, sheet_raw):
            return metric
    return None


def _md_cell(s: str) -> str:
    """Escape a value for a Markdown table cell — `|` would otherwise split
    it into extra columns, and a literal newline would break the row."""
    return str(s).replace("|", "\\|").replace("\n", " ")


def _format_profile(metric: dict | None) -> str:
    if not metric:
        return ""
    bits = []
    if metric.get("type"):
        bits.append(str(metric["type"]))
    if metric.get("nulls") is not None:
        bits.append(f"{metric['nulls']} nulls")
    if metric.get("distinct") is not None:
        bits.append(f"{metric['distinct']} distinct")
    top = metric.get("top") or []
    if top:
        bits.append("top: " + ", ".join(f"{v} ({n})" for v, n in top))
    return " · ".join(bits)


def _column_flag(col: dict) -> str:
    flag = ("declared+observed" if col.get("declared") and col.get("observed")
            else "declared only" if col.get("declared")
            else "observed only")
    if col.get("consumed") is True:
        flag += " · consumed ✓"
    return flag


# -- Markdown --------------------------------------------------------------
def to_markdown(graph: dict, profiles: dict | None = None) -> str:
    """A digest ordered folder → pattern → sheet → columns → generator →
    outputs → reports, one `##` heading per source. Compact tables and
    bullet lists only — this is read in a PR description or a chat message,
    not rendered anywhere HTML would be trusted."""
    profiles_by_col = _group_profiles(profiles or {})
    nodes = {n["id"]: n for n in graph.get("nodes") or []}
    out_to = _adjacency(graph)
    lines: list[str] = ["# Lineage export", ""]

    meta = graph.get("meta") or {}
    if meta.get("generated_at"):
        lines.append(f"_Generated {meta['generated_at']}_  ")
    focus = nodes.get(graph.get("focus"))
    if focus:
        lines.append(f"_Scoped to **{focus['label']}** ({focus['kind']})_  ")
    if meta.get("truncated"):
        lines.append("_Some sheets were dropped — the graph exceeded the node budget._  ")
    lines.append("")
    lines.append("Markdown and Mermaid include column profiles (top values).")
    lines.append("")

    sources = sorted((n for n in graph.get("nodes") or [] if n["layer"] == 0),
                     key=lambda n: n["label"])
    if not sources:
        lines.append("_No sources in scope._")
        return "\n".join(lines).rstrip() + "\n"

    for src in sources:
        d = src.get("data") or {}
        lines.append(f"## {src['label']}")
        if src["kind"] == "folder":
            bits = []
            if d.get("declared_path"):
                bits.append(f"`{d['declared_path']}`")
            else:
                bits.append("no path claimed — identity is the folder name only")
            if d.get("files") is not None:
                bits.append(f"{d['files']} files")
            if d.get("shared"):
                bits.append("shared")
            if d.get("data_stale"):
                bits.append("stale")
            if d.get("unread"):
                bits.append("unread")
            lines.append("- " + " · ".join(bits))
        else:  # greenplum
            for q in (d.get("queries") or []):
                lines.append(f"- query: `{q}`")
        lines.append("")

        gens_seen: dict[str, dict] = {}
        patterns = sorted((nodes[t] for t in out_to.get(src["id"], []) if nodes[t]["kind"] == "pattern"),
                          key=lambda n: n["label"])
        for pat in patterns:
            pd = pat.get("data") or {}
            flags = []
            if pd.get("role") == "reference":
                flags.append("reference")
            if pd.get("required"):
                flags.append("required")
            if pd.get("legacy"):
                flags.append("legacy — no inputs declared")
            if pd.get("unmatched"):
                flags.append("no match on the last run")
            head = f"### {pat['label']}"
            if flags:
                head += f" _({', '.join(flags)})_"
            lines.append(head)

            sheets = sorted((nodes[t] for t in out_to.get(pat["id"], []) if nodes[t]["kind"] == "sheet"),
                            key=lambda n: n["label"])
            for sh in sheets:
                shd = sh.get("data") or {}
                gen_here = _generator_after(nodes, out_to, sh["id"])
                if gen_here:
                    gens_seen[gen_here["id"]] = gen_here
                sub = f"**{sh['label']}**"
                if shd.get("observed_rows"):
                    sub += f" — {shd['observed_rows']:,} rows"
                lines.append(sub)
                cols = shd.get("columns") or []
                if cols:
                    lines.append("")
                    lines.append("| column | flags | profile |")
                    lines.append("|---|---|---|")
                    gen_label = gen_here["label"] if gen_here else None
                    for c in cols:
                        prof_txt = ""
                        if gen_label:
                            metric = _lookup_profile(profiles_by_col, gen_label, pat["label"],
                                                     sh["label"], c["name"])
                            prof_txt = _format_profile(metric)
                        lines.append(f"| {_md_cell(c['name'])} | {_column_flag(c)} | {_md_cell(prof_txt)} |")
                    lines.append("")

            if not sheets:
                gen_here = _generator_after(nodes, out_to, pat["id"])
                if gen_here:
                    gens_seen[gen_here["id"]] = gen_here

        if not patterns:
            for t in out_to.get(src["id"], []):
                if nodes[t]["kind"] == "generator":
                    gens_seen[t] = nodes[t]

        for gen in sorted(gens_seen.values(), key=lambda n: n["label"]):
            gd = gen.get("data") or {}
            flags = []
            if gd.get("version") is not None:
                flags.append(f"v{gd['version']}")
            if gd.get("engine"):
                flags.append(gd["engine"])
            if gd.get("legacy_inputs"):
                flags.append("LEGACY")
            head = f"#### → {gen['label']}"
            if flags:
                head += f" _({', '.join(flags)})_"
            lines.append(head)
            for st in _stages_of(nodes, graph, gen["id"]):
                sd = st.get("data") or {}
                bits = []
                if sd.get("rows") is not None:
                    bits.append(f"{sd['rows']:,} rows")
                cols = sd.get("columns") or []
                if cols:
                    bits.append(f"{len(cols)} columns: " + ", ".join(cols[:8])
                                + (" …" if len(cols) > 8 else ""))
                if sd.get("inputs"):
                    bits.append("from " + ", ".join(sd["inputs"]))
                if sd.get("read_files"):
                    bits.append("reads " + ", ".join(sd["read_files"][:5])
                                + (" …" if len(sd["read_files"]) > 5 else ""))
                lines.append(f"- stage {(sd.get('order') or 0) + 1}. **{_md_cell(st['label'])}**"
                             + (f" — {_md_cell(' · '.join(bits))}" if bits else ""))
            outs = sorted((nodes[t] for t in out_to.get(gen["id"], [])
                          if nodes[t]["kind"] in ("output", "write_back")),
                         key=lambda n: n["label"])
            for o in outs:
                od = o.get("data") or {}
                if o["kind"] == "write_back":
                    kind_bit = "write-back" + (f" — failed: {', '.join(od['failed'])}" if od.get("failed") else "")
                else:
                    kind_bit = "html" if od.get("slot") is None else f"slot {od['slot']}"
                lines.append(f"- output: {o['label']} ({kind_bit})")
                for r in (nodes[t] for t in out_to.get(o["id"], []) if nodes[t]["kind"] == "report"):
                    rd = r.get("data") or {}
                    lines.append(f"  - report: **{r['label']}** — {rd.get('status') or '?'}, "
                                 f"{rd.get('last_run_at') or 'never run'}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# -- Mermaid -----------------------------------------------------------
_SLUG_RE = re.compile(r"[^A-Za-z0-9_]+")

# GOST-7.79-ish transliteration (System B, simplified): enough to turn a
# Cyrillic sheet/generator/column name into a readable Latin id instead of
# stripping it to nothing. Lowercase keys only — the lookup below folds each
# character to lowercase itself, so only the Cyrillic characters it actually
# translates change case; ASCII characters pass through unchanged, keeping
# existing "Sheet1"-style ids stable.
_CYRILLIC_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _translit(s: str) -> str:
    return "".join(_CYRILLIC_TRANSLIT.get(ch.lower(), ch) for ch in (s or ""))


def _slug(label: str, used: set[str], fallback: str = "n") -> str:
    """Mermaid entity/attribute ids: ASCII, no spaces. The real name is
    never lost — callers put it back as a quoted comment/`_label` attribute.
    Cyrillic is transliterated (GOST-like) rather than stripped, so
    a Cyrillic "Filial" (branch) becomes "filial" instead of collapsing to the bare fallback; a
    label with no translatable/ASCII content at all (emoji-only, say) still
    falls back to `fallback` (typically the node's kind).

    Collisions get a SHA1 suffix of the ORIGINAL label, not an incrementing
    counter: a counter's value depends on iteration order, which shifts when
    the export is scoped to a subgraph (`?focus=`) — the same node could get
    a different id in a focused export than in the full one. The hash
    suffix is a function of the label alone, so it is stable regardless of
    which other nodes happen to be exported alongside it."""
    base = _SLUG_RE.sub("_", _translit(label)).strip("_") or fallback or "n"
    if base[0].isdigit():
        base = "n_" + base
    if base not in used:
        used.add(base)
        return base
    h = hashlib.sha1((label or "").encode("utf-8")).hexdigest()[:6]
    cand, i = f"{base}_{h}", 2
    while cand in used:
        cand = f"{base}_{h}_{i}"
        i += 1
    used.add(cand)
    return cand


def _mm_esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def to_mermaid(graph: dict, profiles: dict | None = None) -> str:
    """An `erDiagram`: one entity per sheet (columns as attributes, typed from
    the profile when one is known, else `string`), plus bare generator/report
    entities wired by `reads` and `publishes`. Identifiers are slugs —
    Mermaid's ER grammar rejects spaces and non-Latin scripts in entity and
    attribute names — with the real label kept as each entity's first
    attribute and a quoted comment on every column."""
    profiles_by_col = _group_profiles(profiles or {})
    nodes = {n["id"]: n for n in graph.get("nodes") or []}
    out_to = _adjacency(graph)

    used_entities: set[str] = set()
    entity_id: dict[str, str] = {}

    def eid(node_id: str) -> str:
        if node_id not in entity_id:
            entity_id[node_id] = _slug(nodes[node_id]["label"], used_entities,
                                       fallback=nodes[node_id]["kind"])
        return entity_id[node_id]

    lines = ["erDiagram"]

    sheets = sorted((n for n in graph.get("nodes") or [] if n["kind"] == "sheet"),
                    key=lambda n: n["id"])
    for sh in sheets:
        se = eid(sh["id"])
        pat = next((nodes[e["from"]] for e in (graph.get("edges") or [])
                   if e["to"] == sh["id"] and nodes.get(e["from"], {}).get("kind") == "pattern"), None)
        gen_here = _generator_after(nodes, out_to, sh["id"])
        lines.append(f"    {se} {{")
        lines.append(f'        string _label "{_mm_esc(sh["label"])}"')
        used_attrs = {"_label"}
        for c in (sh.get("data") or {}).get("columns") or []:
            attr = _slug(c["name"], used_attrs, fallback="col")
            ptype = "string"
            if gen_here and pat:
                metric = _lookup_profile(profiles_by_col, gen_here["label"], pat["label"],
                                         sh["label"], c["name"])
                if metric and metric.get("type"):
                    ptype = _SLUG_RE.sub("_", str(metric["type"])).strip("_") or "string"
            comment = _column_flag(c) + (f" — {c['name']}" if attr != c["name"] else "")
            lines.append(f'        {ptype} {attr} "{_mm_esc(comment)}"')
        lines.append("    }")

    stages = sorted((n for n in graph.get("nodes") or [] if n["kind"] == "stage"),
                    key=lambda n: ((n.get("data") or {}).get("generator") or "",
                                   (n.get("data") or {}).get("order") or 0))
    for st in stages:
        se = eid(st["id"])
        sd = st.get("data") or {}
        lines.append(f"    {se} {{")
        lines.append(f'        string _label "{_mm_esc(st["label"])}"')
        if sd.get("rows") is not None:
            lines.append(f'        int _rows "{sd["rows"]} rows"')
        used_attrs = {"_label", "_rows"}
        for name in (sd.get("columns") or []):
            attr = _slug(name, used_attrs, fallback="col")
            comment = "stage column" + (f" — {name}" if attr != name else "")
            lines.append(f'        string {attr} "{_mm_esc(comment)}"')
        lines.append("    }")

    gens = sorted((n for n in graph.get("nodes") or [] if n["kind"] == "generator"), key=lambda n: n["id"])
    for gen in gens:
        ge = eid(gen["id"])
        gd = gen.get("data") or {}
        lines.append(f"    {ge} {{")
        lines.append(f'        string _label "{_mm_esc(gen["label"])}"')
        if gd.get("version") is not None:
            lines.append(f'        string version "v{gd["version"]}"')
        if gd.get("engine"):
            lines.append(f'        string engine "{_mm_esc(gd["engine"])}"')
        if gd.get("legacy_inputs"):
            lines.append('        string legacy "LEGACY"')
        lines.append("    }")

    reports = sorted((n for n in graph.get("nodes") or [] if n["kind"] == "report"), key=lambda n: n["id"])
    for rep in reports:
        re_ = eid(rep["id"])
        rd = rep.get("data") or {}
        lines.append(f"    {re_} {{")
        lines.append(f'        string _label "{_mm_esc(rep["label"])}"')
        if rd.get("status"):
            lines.append(f'        string status "{_mm_esc(rd["status"])}"')
        lines.append("    }")

    rel_seen: set[tuple[str, str, str]] = set()

    def rel(a_id: str, b_id: str, verb: str) -> None:
        if a_id not in entity_id and a_id not in nodes:
            return
        a, b = eid(a_id), eid(b_id)
        key = (a, b, verb)
        if key in rel_seen:
            return
        rel_seen.add(key)
        lines.append(f"    {a} ||--o{{ {b} : {verb}")

    for sh in sheets:
        for t in out_to.get(sh["id"], []):
            # "reads" either way: the sheet is read BY the generator, or by
            # the stage of it that got there first.
            if nodes[t]["kind"] in ("generator", "stage"):
                rel(sh["id"], t, "reads")

    for st in stages:
        for t in out_to.get(st["id"], []):
            kind = nodes[t]["kind"]
            if kind == "stage":
                rel(st["id"], t, "feeds")
            elif kind == "generator":
                rel(st["id"], t, "builds")

    for gen in gens:
        published: set[str] = set()
        stack = list(out_to.get(gen["id"], []))
        seen_walk: set[str] = set()
        while stack:
            x = stack.pop()
            if x in seen_walk:
                continue
            seen_walk.add(x)
            if nodes.get(x, {}).get("kind") == "report":
                published.add(x)
                continue
            stack.extend(out_to.get(x, []))
        for rep_id in sorted(published):
            rel(gen["id"], rep_id, "publishes")

    return "\n".join(lines) + "\n"
