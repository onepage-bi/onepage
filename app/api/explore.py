"""`/explore` API (Leg 3): the catalog snapshot the browser worker indexes
against, the NL->SQL bridge, and saved questions.

Everything that touches actual cell VALUES lives entirely in the browser —
this module never receives a row of data and never will (`explore:sql` runs
inside the Pyodide worker, over files the user's own FSA folder handle can
read, and the result never reaches this server). What this module hands the
browser is metadata: the catalog snapshot (table/column/partition shape,
already public information within the user's reach) plus, capped hard, a
handful of EXAMPLE values already sitting on the server from Data Quality's
own profiling/baselines (`app/services/quality.py` docstring: "real cell
contents cross the network boundary" for THAT feature, already privacy
reviewed) — never the underlying rows.

Scope mirrors `app/api/catalog.py` exactly: `_reachable_source_ids` is the
same reach computation (admin sees everything, everyone else the sources
their runnable dashboards have actually observed), reused rather than
re-derived so "what tables can this user see" means the same thing on
`/catalog` and `/explore`.

ADR-12 (`docs/PERF_DECISIONS.md`): inference is a SUGGESTER. `/explore`
deliberately serves BOTH `confirmed` and `inferred` tables (never
`rejected`) — the plan's first-run flow lets a user confirm a table from
`/explore` itself rather than forcing a trip to `/catalog` first; `inferred`
rows carry their status in the snapshot so the page can flag them.
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from jinja2 import Environment
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.i18n import _
from app.auth.deps import get_current_user
from app.database import get_db
from app.models import (
    CatalogPartition, CatalogTable, DataSource, ExploreAnswer, ExploreFormSuggestion,
    ExploreQuestion, GlossaryTerm, QualityResult, SourceFile, SourceObservation, User,
    ValueBaseline, has_permission,
)
from app.services import storage
from app.services import explore_answers, nl2sql, reference as refsvc, semantics as semantics_svc
from app.services.catalog import norm_col
from app.api.catalog import _reachable_source_ids
from app.api.generators import _log, _generator_dashboards
from app.auth.deps import can_update_dashboard_data
from app.models import Generator, GeneratorVersion


def _require_explore_enabled() -> None:
    from app.config import settings
    if not settings.explore_enabled:
        raise HTTPException(status_code=404, detail="Explore is disabled")


router = APIRouter(dependencies=[Depends(_require_explore_enabled)])

# `CatalogTable.table_id` is `String(16)` (app/models.py) — bound every
# request-supplied table_id list item the same way so an oversized value
# 422s immediately instead of being echoed back whole in an error message.
TableId = Annotated[str, StringConstraints(max_length=16)]

MAX_VALUES_PER_COLUMN = 500
MAX_VALUE_CHARS = 64
_CATALOG_STATUSES = ("confirmed", "inferred")


# ─────────────────────────────── catalog snapshot ─────────────────────────

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _table_json(row: CatalogTable, sources: dict[int, DataSource],
                parts_by_table: dict[str, list[CatalogPartition]]) -> dict:
    src = sources.get(row.data_source_id)
    parts = parts_by_table.get(row.table_id, [])
    return {
        "table_id": row.table_id,
        "name": row.name,
        "display_name": row.display_name,
        "status": row.status,
        "source": {"id": row.data_source_id, "key_name": getattr(src, "key_name", None)},
        "columns": [{"name": c.get("norm"), "display": c.get("display"), "type": c.get("type")}
                   for c in row.get_columns() if c.get("norm")],
        "partitions": [{"key": p.key, "file": p.file_name, "sheet": p.sheet, "rows": p.rows,
                        "mtime_ms": p.mtime_ms, "size": p.size} for p in parts],
        "header_row": row.header_row,
    }


def _gather_values(db: Session, rows: list[CatalogTable],
                   cap: int = MAX_VALUES_PER_COLUMN) -> dict[str, list[str]]:
    """`{"<table_id>.<column>": [values...]}`, each list capped at `cap`
    (default `MAX_VALUES_PER_COLUMN`; nl2sql passes a much smaller cap since
    it only ever uses the first `settings.llm_example_values` of each list —
    gathering, deduping and holding 500 values per column just to slice
    `[:3]` afterwards is pure waste on that path), each value trimmed to
    `MAX_VALUE_CHARS`. Two sources, unioned:

      * `value_baselines` — the accepted set of values for a `known_values`
        check on this table's data source + sheet.
      * `quality_results` profile rows (`check_type='profile'`) for the
        source files this table's partitions actually resolved to — the
        "top ≤5 values" a normal quality profile already computed.

    Both sources already live on the server for Data Quality; this function
    only reads and re-caps them, it computes nothing new about cell
    contents. Batched per data_source rather than per column so a snapshot
    with N tables costs O(sources) queries, not O(columns).
    """
    if not rows:
        return {}
    by_ds: dict[int, list[CatalogTable]] = {}
    for r in rows:
        by_ds.setdefault(r.data_source_id, []).append(r)

    out: dict[str, list[str]] = {}
    for ds_id, ds_rows in by_ds.items():
        # baselines: (sheet.casefold(), norm(column)) -> set[str]
        baseline_map: dict[tuple[str, str], list[str]] = {}
        for vb in db.query(ValueBaseline).filter(ValueBaseline.data_source_id == ds_id).all():
            key_col = norm_col(vb.column)
            if not key_col:
                continue
            key = (vb.sheet.casefold(), key_col)
            baseline_map.setdefault(key, []).extend(vb.get_values())

        # partitions -> the file names this data source's tables resolved to
        table_ids = [r.table_id for r in ds_rows]
        parts = (db.query(CatalogPartition)
                 .filter(CatalogPartition.table_id.in_(table_ids)).all())
        file_names = {p.file_name for p in parts}
        parts_by_table: dict[str, list[CatalogPartition]] = {}
        for p in parts:
            parts_by_table.setdefault(p.table_id, []).append(p)

        # file_name.casefold() -> [source_file_id, ...] within this source
        sf_ids_by_name: dict[str, list[int]] = {}
        if file_names:
            sf_rows = (db.query(SourceFile.id, SourceFile.name)
                      .join(SourceObservation, SourceObservation.id == SourceFile.observation_id)
                      .filter(SourceObservation.data_source_id == ds_id,
                              SourceFile.name.in_(file_names)).all())
            for fid, fname in sf_rows:
                sf_ids_by_name.setdefault(fname.casefold(), []).append(fid)

        all_sf_ids = sorted({fid for ids in sf_ids_by_name.values() for fid in ids})
        # profile map: source_file_id -> {(sheet.casefold(), norm(column)): [[v,n],...]}
        profile_map: dict[int, dict[tuple[str, str], list]] = {}
        for chunk_start in range(0, len(all_sf_ids), 800):
            chunk = all_sf_ids[chunk_start:chunk_start + 800]
            for qr in (db.query(QualityResult)
                      .filter(QualityResult.source_file_id.in_(chunk),
                              QualityResult.check_type == "profile").all()):
                key_col = norm_col(qr.column or "")
                if not key_col:
                    continue
                metric = qr.get_metric()
                top = metric.get("top") or []
                profile_map.setdefault(qr.source_file_id, {})[(qr.sheet or "").casefold(), key_col] = top

        for r in ds_rows:
            for col in r.get_columns():
                col_norm = col.get("norm")
                if not col_norm:
                    continue
                seen: list[str] = []
                seen_set: set[str] = set()

                def _add(v):
                    if not isinstance(v, str):
                        return
                    v = v.strip()[:MAX_VALUE_CHARS]
                    if v and v not in seen_set:
                        seen_set.add(v)
                        seen.append(v)

                for sheet in r.get_sheets():
                    for v in baseline_map.get((sheet.casefold(), col_norm), []):
                        _add(v)
                    if len(seen) >= cap:
                        break
                if len(seen) < cap:
                    for p in parts_by_table.get(r.table_id, []):
                        for fid in sf_ids_by_name.get(p.file_name.casefold(), []):
                            for v, _cnt in profile_map.get(fid, {}).get((p.sheet.casefold(), col_norm), []):
                                _add(v)
                        if len(seen) >= cap:
                            break
                if seen:
                    out[f"{r.table_id}.{col_norm}"] = seen[:cap]
    return out


def _catalog_snapshot(db: Session, user: User, table_ids: list[str] | None = None,
                      values_cap: int = MAX_VALUES_PER_COLUMN) -> dict:
    allowed = _reachable_source_ids(db, user)
    # Slot-backed generators (ADR-14b addendum) have semantics with no
    # catalogue behind them at all — the early returns must still carry the
    # semantics snapshot, or a slot-only report loses its definitions.
    if allowed is not None and not allowed:
        return {"tables": [], "values": {}, "semantics": _semantics_snapshot(db, user, [])}

    q = db.query(CatalogTable).filter(CatalogTable.status.in_(_CATALOG_STATUSES))
    if allowed is not None:
        q = q.filter(CatalogTable.data_source_id.in_(sorted(allowed)))
    if table_ids is not None:
        q = q.filter(CatalogTable.table_id.in_(table_ids))
    rows = q.all()
    if not rows:
        return {"tables": [], "values": {}, "semantics": _semantics_snapshot(db, user, [])}

    sources = {ds.id: ds for ds in
              db.query(DataSource).filter(DataSource.id.in_({r.data_source_id for r in rows})).all()}
    parts = (db.query(CatalogPartition)
            .filter(CatalogPartition.table_id.in_([r.table_id for r in rows])).all())
    parts_by_table: dict[str, list[CatalogPartition]] = {}
    for p in parts:
        parts_by_table.setdefault(p.table_id, []).append(p)

    tables = [_table_json(r, sources, parts_by_table) for r in rows]
    values = _gather_values(db, rows, cap=values_cap)
    return {"tables": tables, "values": values,
            "semantics": _semantics_snapshot(db, user, tables)}


def _reference_state(db: Session, user: User, slug: str, budget: list[int],
                     memo: dict) -> dict:
    """Everything about ONE dictionary that a snapshot needs, resolved AT MOST
    ONCE per request however many generators read it.

    Two reports reading `sl_client_map` used to cost two `get_table`, two
    `current_rows` (a 2 MB JSON decode each) and — worse — two charges against
    the SAME shared byte budget, so a dictionary that fits could push itself
    out on its second reader. The state is memoised under `memo` by slug: the
    table, the verdict, the full rows, and whether this slug has already paid.

    Returns `{"status", "table", "rows", "message"}` — `rows` only when
    `status == "ok"`."""
    from app.api import reference as refapi

    cache = memo.setdefault("ref_state", {})
    if slug in cache:
        return cache[slug]

    table = refsvc.get_table(db, slug)
    if table is None:
        state = {"status": "missing", "table": None,
                 "message": _("reference table {slug} has not been created", slug=slug)}
    elif not refapi._can_read(db, user, table, memo):
        # Exactly the right to read the dictionary itself: reach to a report
        # that consumes it IN A RUN. A semantics declaration is not reach —
        # see `reference._run_consumers`.
        state = {"status": "forbidden", "table": None,
                 "message": _("no access to reference table {slug}", slug=slug)}
    else:
        rows = refsvc.current_rows(db, table)
        # The budget is charged for the DICTIONARY, once — the per-table
        # projections below are copies of these same bytes.
        cost = sum(len(str(v)) + 3 for row in rows for v in row)
        if cost > budget[0]:
            state = {"status": "too_big", "table": table,
                     "message": _("reference table {slug}: too many rows for a single response",
                                slug=slug)}
        else:
            budget[0] -= cost
            state = {"status": "ok", "table": table, "rows": rows,
                     "message": _("reference table {slug} v{version}",
                                slug=slug, version=table.current_version)}
    cache[slug] = state
    return state


def _bind_reference_rows(db: Session, user: User, sem: dict, budget: list[int],
                         memo: dict) -> int:
    """Fill every reference-backed semantic table (ADR-16) with the rows of
    the reference table's CURRENT version, in place, on the already-decoded
    (and therefore private) copy of the frozen semantics. Returns how many
    tables actually got rows.

    This is the whole "semantics reads reference tables" path: nothing is frozen
    into the bundle, so an edit made on `/reference/{slug}` is in the next
    answer without re-registering the generator. Rows travel as LISTS in the
    declared column order (never objects — that would inflate a 20 000-row
    dictionary from ~1.5 MB to ~2.3 MB on the wire) under
    `tables[t]["reference"]`, which is also where the status the page shows
    lives: "reference table sl_client_map v7", or the honest reason there are no
    rows. A table that stays without rows degrades to `unbound` in the
    worker — the rest of the report keeps answering.

    `memo` is the per-request dict shared with `reference._consumers`: one
    manifest sweep and one read per dictionary for the whole snapshot (see
    `_reference_state`). `budget` is a one-element list of the bytes left for
    reference rows in this snapshot (`semantics.MAX_REFERENCE_BYTES`).
    """
    bound = 0
    for tname, spec in semantics_svc.reference_sources(sem).items():
        t = sem["tables"].get(tname)
        if not isinstance(t, dict):
            continue
        slug = spec["slug"]
        state = _reference_state(db, user, slug, budget, memo)
        table = state["table"]
        if state["status"] != "ok":
            block = {"slug": slug, "status": state["status"], "message": state["message"]}
            if table is not None:
                block["version"] = table.current_version
            t["reference"] = block
            continue
        index = {str(c.get("name")).casefold(): i
                 for i, c in enumerate(table.get_columns() or [])}
        # The declared column order IS the row order on the wire; a column the
        # table does not carry cannot happen (registration adds it) but is
        # still filled with None rather than shifting the whole row.
        picks = [index.get(c.casefold()) for c in spec["columns"]]
        rows = [[(r[i] if i is not None and i < len(r) else None) for i in picks]
                for r in state["rows"]]
        t["reference"] = {
            "slug": slug, "status": "ok", "title": table.title,
            "version": table.current_version,
            "columns": list(spec["columns"].keys()),
            "rows": rows, "row_count": len(rows),
            "message": state["message"],
        }
        bound += 1
    return bound


def _semantics_snapshot(db: Session, user: User, tables: list[dict]) -> list[dict]:
    """Semantics (ADR-13) of every generator this user may RUN, bound to the
    catalogue tables in `tables`. Reach is the generator's, not the folder's:
    a report's definitions travel with the right to run the report. Only
    generators with at least one bound table are returned — definitions with
    nothing to stand on would just be noise in the prompt."""
    gen_ids = {d.generator_id for d in _generator_dashboards(db, user) if d.generator_id}
    if not gen_ids:
        return []
    rows = (db.query(Generator.id, Generator.name, GeneratorVersion.version_number,
                     GeneratorVersion.semantics_json)
            .join(GeneratorVersion, GeneratorVersion.generator_id == Generator.id)
            .filter(Generator.id.in_(sorted(gen_ids)),
                    GeneratorVersion.is_current == True,  # noqa: E712
                    GeneratorVersion.semantics_json.isnot(None))
            .all())
    out = []
    memo: dict = {}          # one manifest sweep for every _can_read below
    budget = [semantics_svc.MAX_REFERENCE_BYTES]
    for gid, gname, vnum, sem_json in rows:
        try:
            sem = json.loads(sem_json)
        except ValueError:
            continue
        bindings = semantics_svc.bind(sem, tables)
        # A report whose tables are ALL dictionaries (or whose file tables
        # have no folder indexed yet) has no catalogue match at all — but its
        # dictionary answers perfectly well, so a bound reference is reason
        # enough to ship the definitions. KPIs that need the file tables stay
        # NOT READY by the worker's own rule.
        bound_refs = _bind_reference_rows(db, user, sem, budget, memo)
        if not any(b["matches"] for b in bindings.values()) and not bound_refs:
            continue
        out.append({"generator_id": gid, "generator": gname, "version_number": vnum,
                    "semantics": sem, "bindings": bindings})
    return out


# ─────────────────────────────── page ─────────────────────────────────────

def _reports_snapshot(db: Session, user: User) -> list[dict]:
    """The REPORT cards of screen A: one per generator-backed dashboard this
    user may run. Reach is `_generator_dashboards` — the same helper the
    semantics snapshot already uses, so "a report I may run" means exactly one
    thing on this page. One extra query joins the generator's name and its
    current version number; nothing here touches a row of data."""
    dashboards = _generator_dashboards(db, user)
    # "Highly confidential": the chat is closed to everyone (owner's decision 2026-10-20),
    # so such a report never reaches the cards at all — otherwise a person would see
    # a card that answers 403 to the very first question.
    dashboards = [d for d in dashboards if not d.confidential]
    # ADR-18: an RLS report does not get into the chat cards if the viewer does not see it
    # in full — the chat goes to the lake, and the lake is not filtered. The user_scopes query
    # is made only for reports with a policy (usually there are none at all).
    if any(d.rls_enabled for d in dashboards):
        from app.services import rls
        dashboards = [d for d in dashboards
                      if not d.rls_enabled or rls.subject(db, d, user).see_all]
    gen_ids = {d.generator_id for d in dashboards if d.generator_id}
    if not gen_ids:
        return []
    meta: dict[int, tuple[str, int | None]] = {}
    rows = (db.query(Generator.id, Generator.name, GeneratorVersion.version_number)
            .outerjoin(GeneratorVersion,
                       (GeneratorVersion.generator_id == Generator.id)
                       & (GeneratorVersion.is_current == True))  # noqa: E712
            .filter(Generator.id.in_(sorted(gen_ids)))
            .all())
    for gid, gname, vnum in rows:
        meta[gid] = (gname, vnum)
    out = []
    for d in dashboards:
        if not d.generator_id:
            continue
        gname, vnum = meta.get(d.generator_id, ("", None))
        out.append({"dashboard_id": d.id, "title": d.title,
                    "generator_id": d.generator_id,
                    "generator_name": gname, "version": vnum,
                    # One stat per card, /explore only: the page probes the
                    # lake manifest just for reports that have one, so a
                    # report without an index costs no 404 in the console.
                    "lake": storage.lake_manifest_path(str(d.id)).exists(),
                    # For the card's live preview — same cutoff the home grid uses.
                    "size": int(getattr(d, "render_size", 0) or 0)})
    return out


@router.get("/explore", response_class=HTMLResponse)
async def explore_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    report: str = Query("", max_length=64),
    mode: str = Query("", max_length=16),
    embed: str = Query("", max_length=4),
):
    """The /explore page itself. The catalog snapshot is baked into the HTML
    (same idiom as /catalog) so the page can populate the table picker and the
    worker's `explore:init` without a second round trip. `reports_json` is the
    same idea for the report strip: the dashboards this user may run, with the
    generator that backs each.

    `report` / `mode` / `embed` are the viewer's "Chat" pane (phase 4): the same
    page, opened in a same-origin iframe beside an open report. They are read
    ONLY by the page (`#ex-root` data-attributes -> explore-chat.js), never by
    a query here: access is exactly the access `/explore` already grants, and
    `/api/explore/intent` re-checks generator reach per call as before. `embed`
    drops the sidebar/topbar chrome — nothing else about the page changes."""
    snapshot, reports = await run_in_threadpool(
        lambda: (_catalog_snapshot(db, user, None), _reports_snapshot(db, user)))
    env: Environment = request.app.state.jinja
    html_out = await run_in_threadpool(
        env.get_template("pages/explore.html").render,
        user=user, request=request,
        catalog_json=json.dumps(snapshot, ensure_ascii=False),
        reports_json=json.dumps(reports, ensure_ascii=False),
        report_param=report, mode_param=mode, embed=(embed == "1"),
    )
    return HTMLResponse(html_out)


@router.get("/api/explore/catalog")
async def api_explore_catalog(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await run_in_threadpool(_catalog_snapshot, db, user, None)


# ─────────────────────────────── nl2sql ────────────────────────────────────

class Nl2SqlBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    table_ids: list[TableId] = Field(min_length=1, max_length=32)


@router.post("/api/explore/nl2sql")
async def api_explore_nl2sql(
    body: Nl2SqlBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.config import settings

    # nl2sql only ever uses the first `settings.llm_example_values` (<=3) of
    # each column's values, so gathering the full 500-per-column cap here
    # would be pure waste — cap at what build_prompt can possibly use.
    snapshot = await run_in_threadpool(
        _catalog_snapshot, db, user, body.table_ids,
        max(0, min(settings.llm_example_values, nl2sql.MAX_EXAMPLES_PER_COLUMN)))
    found_ids = {t["table_id"] for t in snapshot["tables"]}
    missing = [t for t in body.table_ids if t not in found_ids][:32]
    if missing:
        raise HTTPException(status_code=422,
                            detail=_("Unavailable or unknown tables: {p1}", p1=', '.join(missing)))

    prompt = nl2sql.build_prompt(snapshot["tables"], snapshot["values"], body.question,
                                 max_examples=settings.llm_example_values)

    def _audit(outcome: str, extra: dict | None = None):
        payload = {"question": body.question, "table_ids": body.table_ids,
                  "model": nl2sql.model_for(), "provider": nl2sql.provider(), "outcome": outcome}
        if extra:
            payload.update(extra)
        _log(db, user, "explore_nl2sql", "explore", ",".join(body.table_ids), payload,
            ip=_client_ip(request))
        db.commit()

    _llm_gate(db, user)
    try:
        raw = await run_in_threadpool(nl2sql.call_llm, prompt)
    except nl2sql.Nl2SqlError as e:
        _audit("error", {"status_code": e.status_code})
        raise HTTPException(status_code=e.status_code, detail=e.message)

    sql = nl2sql.strip_fences(raw)
    if not nl2sql.is_select_only(sql):
        _audit("rejected_non_select")
        raise HTTPException(status_code=422,
                            detail=_("Model returned not a SELECT query; try rephrasing the question"))

    _audit("ok")
    return {"sql": sql, "explanation": None, "model": nl2sql.model_for()}


# ── follow-ups (phase 3): the previous answer, and nothing else ────────────
# A thread lives in the user's browser and stays there. What may cross to the
# server is ONE intent — the answer the user is currently looking at — so a
# question like "and by week?" can be read at all. Everything about it is
# bounded here rather than trusted: five keys, short lists, and a KPI that
# must exist in the generator's own frozen semantics (checked in
# `_intent_plan`, where the semantics are in hand).
_PREV_KEYS = {"kpi", "dims", "filters", "order", "limit"}
_PREV_MAX_DIMS = 8
_PREV_MAX_FILTERS = 12


def _validated_prev(prev: dict | None) -> dict | None:
    """Shape check for `prev_intent`, independent of any semantics. Raises
    ValueError (Pydantic turns it into a 422) rather than silently dropping:
    a page sending a shape we do not understand is a bug worth surfacing."""
    if prev is None:
        return None
    if not isinstance(prev, dict):
        raise ValueError(_("prev_intent must be an object"))
    extra = set(prev) - _PREV_KEYS
    if extra:
        raise ValueError(_("prev_intent: invalid keys {keys}", keys=sorted(extra)))
    kpi = prev.get("kpi")
    if not isinstance(kpi, str) or not kpi or len(kpi) > 64:
        raise ValueError(_("prev_intent.kpi is required"))
    dims = prev.get("dims") or []
    if not isinstance(dims, list) or len(dims) > _PREV_MAX_DIMS:
        raise ValueError(_("prev_intent.dims is too long"))
    filters = prev.get("filters") or []
    if not isinstance(filters, list) or len(filters) > _PREV_MAX_FILTERS:
        raise ValueError(_("prev_intent.filters is too long"))
    for f in filters:
        if not isinstance(f, dict) or not isinstance(f.get("dim"), str):
            raise ValueError(_("prev_intent.filters: invalid filter shape"))
        if f.get("op") not in nl2sql.INTENT_OPS:
            raise ValueError(_("prev_intent.filters: invalid op"))
        if "value" not in f and "values" not in f:
            raise ValueError(_("prev_intent.filters: missing value"))
    out = {"kpi": kpi, "dims": [str(d)[:64] for d in dims], "filters": filters}
    if isinstance(prev.get("order"), dict):
        out["order"] = prev["order"]
    if isinstance(prev.get("limit"), int):
        out["limit"] = max(1, min(prev["limit"], 1000))
    return out


class IntentBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    generator_id: int
    ready_kpis: list[str] | None = Field(default=None, max_length=128)
    # One-shot form path: the metric is already settled (the form matcher
    # answered, or the operator clicked the KPI) and the model is asked ONLY
    # for the cut. See `_intent_plan`.
    fixed_kpi: str | None = Field(default=None, max_length=64)
    # The answer the page is currently SHOWING, for a follow-up question.
    prev_intent: dict | None = None

    @field_validator("prev_intent")
    @classmethod
    def _check_prev(cls, v):
        return _validated_prev(v)


def _prev_for(sem: dict, body) -> dict | None:
    """`body.prev_intent` once the semantics are in hand: the KPI must be one
    this generator actually defines. A key it does not is a 422 — the page is
    describing an answer this report could not have produced, and quietly
    ignoring it would make the follow-up silently mean something else."""
    prev = getattr(body, "prev_intent", None)
    if not prev:
        return None
    if prev["kpi"] not in (sem.get("kpis") or {}):
        raise HTTPException(status_code=422,
                            detail=_("prev_intent: metric {p1} not found in report semantics", p1=repr(prev['kpi'][:40])))
    return prev


def _platform_glossary(db: Session, generator_id: int) -> dict[str, str]:
    """Company-wide + this generator's own platform-glossary terms (H2),
    ordered company-wide first so they read as the more authoritative
    definition when `build_intent_prompt` merges them ahead of the
    semantics glossary; the merge itself caps at `MAX_INTENT_GLOSSARY`."""
    rows = (db.query(GlossaryTerm)
           .filter(or_(GlossaryTerm.generator_id.is_(None), GlossaryTerm.generator_id == generator_id))
           .order_by(GlossaryTerm.generator_id.is_(None).desc(), GlossaryTerm.term)
           .limit(nl2sql.MAX_INTENT_GLOSSARY)
           .all())
    return {r.term: r.definition for r in rows}


def _intent_semantics(db: Session, user: User, generator_id: int) -> dict:
    """The frozen semantics of a generator this user may RUN, or 404. Reach
    is the generator's, not the folder's — the same rule the /explore
    snapshot uses, and the only gate on every intent route."""
    gen_ids = {d.generator_id for d in _generator_dashboards(db, user) if d.generator_id}
    if generator_id not in gen_ids:
        raise HTTPException(status_code=404, detail=_("Generator unavailable"))
    ver = (db.query(GeneratorVersion)
           .filter(GeneratorVersion.generator_id == generator_id,
                   GeneratorVersion.is_current == True)  # noqa: E712
           .first())
    if ver is None or not ver.semantics_json:
        raise HTTPException(status_code=404, detail=_("This generator version has no semantics"))
    return json.loads(ver.semantics_json)


def _intent_audit(db: Session, user: User, request: Request, body_question: str,
                  generator_id: int, action: str = "explore_intent"):
    """One audit closure shared by `/intent`, `/intent/prepare` and
    `/intent/validate`: whoever answered the question — the form matcher,
    our own server call, or the user's browser — the log line looks the
    same, so «who asked what, and did a model see it» is one query."""

    def _audit(outcome: str, extra: dict | None = None):
        payload = {"question": body_question, "generator_id": generator_id,
                   "model": nl2sql.model_for(), "provider": nl2sql.provider(),
                   "outcome": outcome}
        if extra:
            payload.update(extra)
        _log(db, user, action, "explore", str(generator_id), payload, ip=_client_ip(request))
        db.commit()
    return _audit


def _intent_plan(db: Session, sem: dict, body: IntentBody) -> dict:
    """Everything that happens BEFORE a model call, in one place so the
    server path (`/intent`) and the browser path (`/intent/prepare`) can
    never drift apart: the shortlist, the form-first answer when the
    question quotes a KPI's own `question_forms`, and otherwise the prompt +
    schema. Nothing here touches the network and nothing here can put a cell
    value in the prompt — `build_intent_prompt` only ever sees definitions.

    `body.fixed_kpi` short-circuits the whole choosing half: the metric is
    already known (a confident `match_forms` hit the page is following up
    on), so there is no form matching to redo and no menu to rank. The
    shortlist collapses to that one KPI, the schema's `kpi` enum to that one
    key, and the prompt says the metric is chosen and asks only for the cut.
    That is what turns "Ask" into one shot: the deterministic half
    picks the number, the model picks the slice, nobody clicks."""
    from app.config import settings
    kpis = sem.get("kpis") or {}
    # "Why …?" first, and unconditionally: the intent form cannot express
    # a reason, so a why-question must never reach the normal prompt (nor a
    # `fixed_kpi` follow-up, which only exists to refine a CUT).
    if nl2sql.classify_question(body.question) == "why":
        return _why_plan(db, sem, body)
    fixed = body.fixed_kpi if (body.fixed_kpi and body.fixed_kpi in kpis) else None
    if fixed and body.ready_kpis and fixed not in body.ready_kpis:
        fixed = None
    if fixed:
        shortlist = [fixed]
        extra_glossary = _platform_glossary(db, body.generator_id)
        prev = _prev_for(sem, body)
        prompt = nl2sql.build_intent_prompt(sem, shortlist, body.question,
                                            extra_glossary=extra_glossary, fixed_kpi=fixed,
                                            prev_line=nl2sql.prev_intent_line(sem, prev) if prev else None)
        fmt = (nl2sql.build_intent_schema(sem, shortlist, fixed_kpi=fixed)
               if settings.llm_structured else None)
        return {"source": "model", "shortlist": shortlist, "prompt": prompt,
                "schema": fmt, "fixed_kpi": fixed, "prev_intent": prev}
    shortlist = nl2sql.rank_kpis(sem, body.question, body.ready_kpis)
    hit = nl2sql.match_forms(sem, body.question, body.ready_kpis)
    if hit:
        return {"source": "form", "shortlist": shortlist, "hit": hit,
                "intent": nl2sql.form_intent(sem, hit["kpi"], body.question)}
    # ── the follow-up fall-through (phase 3) ──────────────────────────────
    # No form matched and the page is showing an answer already. If the
    # question carries no metric word of its own, it is a refinement of THAT
    # metric — pin it exactly as a form match would, so the model is left with
    # the cut. If it does carry one, the model stays free to pick a different
    # KPI and only gets the previous answer named as context.
    prev = _prev_for(sem, body)
    prev_line = nl2sql.prev_intent_line(sem, prev) if prev else ""
    extra_glossary = _platform_glossary(db, body.generator_id)
    if prev and not nl2sql.has_metric_words(sem, body.question, body.ready_kpis):
        follow = prev["kpi"]
        if not body.ready_kpis or follow in body.ready_kpis:
            shortlist = [follow]
            prompt = nl2sql.build_intent_prompt(sem, shortlist, body.question,
                                                extra_glossary=extra_glossary, fixed_kpi=follow,
                                                prev_line=prev_line)
            fmt = (nl2sql.build_intent_schema(sem, shortlist, fixed_kpi=follow)
                   if settings.llm_structured else None)
            return {"source": "model", "shortlist": shortlist, "prompt": prompt,
                    "schema": fmt, "fixed_kpi": follow, "prev_intent": prev}
    prompt = nl2sql.build_intent_prompt(sem, shortlist, body.question,
                                        extra_glossary=extra_glossary, prev_line=prev_line)
    fmt = nl2sql.build_intent_schema(sem, shortlist) if settings.llm_structured else None
    return {"source": "model", "shortlist": shortlist, "prompt": prompt, "schema": fmt,
            "fixed_kpi": None, "prev_intent": prev}


def _why_plan(db: Session, sem: dict, body: IntentBody) -> dict:
    """The pre-model half of a "why" question. The why-words are stripped
    before anything looks at the text, so both the KPI matcher and (if it
    comes to that) the prompt see "did SL drop in January 2026?" — a question about
    a metric and a period, which is the only part either of them can read.

    A confident form match settles the metric with no model at all. Otherwise
    the model is asked for the ONE thing it can contribute — which metric —
    constrained to the shortlist; `nl2sql.why_intent_from_raw` throws away
    everything else it wrote. Either way the answer is `source: "why"`: no
    dims, no order, no limit, and only the period the question named."""
    from app.config import settings
    stripped = nl2sql.strip_why(body.question)
    shortlist = nl2sql.rank_kpis(sem, stripped, body.ready_kpis)
    hit = nl2sql.match_forms(sem, stripped, body.ready_kpis)
    if hit:
        return {"source": "why", "shortlist": shortlist, "hit": hit, "question": stripped,
                "intent": nl2sql.why_intent(sem, hit["kpi"], stripped)}
    prompt = nl2sql.build_why_prompt(sem, shortlist, stripped,
                                     extra_glossary=_platform_glossary(db, body.generator_id))
    fmt = nl2sql.build_intent_schema(sem, shortlist) if settings.llm_structured else None
    return {"source": "why", "shortlist": shortlist, "prompt": prompt, "schema": fmt,
            "question": stripped, "hit": None, "intent": None, "fixed_kpi": None}


def _why_answer(intent: dict, shortlist: list[str], model: str | None = None) -> dict:
    """The wire shape of a why-answer. `why` carries the two things the page
    needs to build the card without re-deriving them: the period to compare
    against (via `nl2sql.shift_period`) and the metric being explained."""
    return {"intent": intent, "explanation": intent.get("explanation"),
            "model": model, "source": "why", "shortlist": shortlist,
            "why": {"period": intent.get("filters") or [], "kpi": intent.get("kpi")}}


def _with_residue(resp: dict, question: str, sem: dict) -> dict:
    """Attach the question's unconsumed value-shaped words (2026-08-24,
    "in MBU" lost silently). The page hands them to the ENGINE as
    `probe_words`; they never reach a model from here."""
    if isinstance(resp.get("intent"), dict):
        resp["residue"] = nl2sql.residue_words(question, sem, resp["intent"])
    return resp


def _with_answer_id(resp: dict, rec) -> dict:
    """Attach the id of the row `explore_answers.record()` just wrote — the
    only handle 👍/👎 has (`POST /api/explore/answers/{id}/vote`). `None` when
    nothing was recorded (a generator whose current version carries no frozen
    semantics, a flush that failed): the page then keeps the vote local
    instead of posting it, which is why no route may 500 over this."""
    resp["answer_id"] = rec.id if rec is not None else None
    return resp


def _form_answer(plan: dict) -> dict:
    intent, hit = plan["intent"], plan["hit"]
    return {"intent": intent, "explanation": intent.get("explanation"),
            "model": None, "source": "form", "shortlist": plan["shortlist"],
            "match": {"score": hit["score"], "runner_up": hit["runner_up"], "form": hit["form"]}}


@router.post("/api/explore/intent")
async def api_explore_intent(
    body: IntentBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Question -> validated intent over one generator's semantics (ADR-13).
    The model picks a KPI, dims and filters from the frozen definitions; the
    browser engine turns that into SQL and shows it. No SQL here, no cell
    values in the prompt, and the reach is the generator's: the user must be
    able to run a dashboard of this generator.

    This is the SERVER path: this process calls the provider itself
    (`nl2sql.call_llm`). Where the server has no egress, the page uses
    `/intent/prepare` + `/intent/validate` instead and the two share
    `_intent_plan` so the prompt is identical either way."""
    sem = _intent_semantics(db, user, body.generator_id)
    plan = _intent_plan(db, sem, body)
    _audit = _intent_audit(db, user, request, body.question, body.generator_id)

    if plan["source"] == "why":
        if plan["intent"] is not None:
            answer = _why_answer(plan["intent"], plan["shortlist"])
            rec = explore_answers.record(db, user.id, body.generator_id, body.question,
                                         "intent", "server", intent=answer["intent"])
            _audit("ok", {"kpi": answer["intent"]["kpi"], "source": "why",
                          "score": plan["hit"]["score"]})
            return _with_answer_id(answer, rec)
        _llm_gate(db, user)
        try:
            raw = await run_in_threadpool(nl2sql.call_llm, plan["prompt"], plan["schema"])
            intent = nl2sql.why_intent_from_raw(raw, sem, plan["question"])
        except nl2sql.Nl2SqlError as e:
            _audit("error", {"status_code": e.status_code, "source": "why"})
            raise HTTPException(status_code=e.status_code, detail=e.message)
        rec = explore_answers.record(db, user.id, body.generator_id, body.question,
                                     "intent", "server", intent=intent)
        _audit("ok", {"kpi": intent["kpi"], "source": "why"})
        return _with_answer_id(_why_answer(intent, plan["shortlist"], model=nl2sql.model_for()), rec)

    # Form-first: when the question is a near-quote of one KPI's own
    # `question_forms`, answer from the definitions alone. No model call at
    # all — the fastest and the only deterministic branch of this route.
    if plan["source"] == "form":
        answer = _form_answer(plan)
        rec = explore_answers.record(db, user.id, body.generator_id, body.question,
                                     "form", "server", intent=answer["intent"])
        _audit("ok", {"kpi": answer["intent"]["kpi"], "source": "form", "score": plan["hit"]["score"]})
        return _with_answer_id(_with_residue(answer, body.question, sem), rec)

    _llm_gate(db, user)
    try:
        raw = await run_in_threadpool(nl2sql.call_llm, plan["prompt"], plan["schema"])
        intent = nl2sql.ground_intent(
            body.question, nl2sql.parse_intent(raw, sem, fixed_kpi=plan.get("fixed_kpi")),
            prev_intent=plan.get("prev_intent"))
    except nl2sql.Nl2SqlError as e:
        _audit("error", {"status_code": e.status_code})
        raise HTTPException(status_code=e.status_code, detail=e.message)
    rec = explore_answers.record(db, user.id, body.generator_id, body.question,
                                 "intent", "server", intent=intent)
    _audit("ok", {"kpi": intent["kpi"], "source": "model",
                  "fixed_kpi": plan.get("fixed_kpi"),
                  "followup": bool(plan.get("prev_intent"))})
    return _with_answer_id(_with_residue(
        {"intent": intent, "explanation": intent.get("explanation"),
         "model": nl2sql.model_for(), "source": "model", "shortlist": plan["shortlist"],
         "fixed_kpi": plan.get("fixed_kpi")}, body.question, sem), rec)


# ───────────────────── browser-side model call (OP_LLM_BROWSER) ────────────
# Where THIS server has no egress but the users' browsers do (measured on
# prod: the container times out on api.deepseek.com, the corporate browser
# gets a CORS-clean 401 from it), the model call moves into the page and the
# server keeps everything else: reach, semantics, the prompt, and the
# validation of whatever text the page brings back. Three routes:
#
#   GET  /api/explore/llm-config    what the page may do and with which key
#   POST /api/explore/intent/prepare   the same pre-model work as /intent
#   POST /api/explore/intent/validate  parse_intent + ground_intent on the answer
#
# The page is never trusted: `raw` arriving at /validate is model text like
# any other, checked against the frozen semantics by the same validator.

# Sessions that already had the server key handed to them, so the audit log
# gets ONE handout line per session instead of one per page load. Bounded
# and process-local: a restart just re-audits, which is the safe direction.
_KEY_HANDOUTS: set[str] = set()
_MAX_KEY_HANDOUTS = 4096


def _llm_allowed(db: Session, user: User) -> bool:
    from app.services import llm_access
    return llm_access.allowed(db, user)


def _llm_gate(db: Session, user: User) -> None:
    """Owner decision 2026-08-23: the PAID provider (DeepSeek) serves only
    the per-user allowlist (`llm_access`). Deterministic answers — forms,
    why-cards, metric intents — stay open to everyone; this gate sits only
    where a model call happens or its prompt is handed to the page."""
    if not _llm_allowed(db, user):
        raise HTTPException(
            status_code=403,
            detail=_("DeepSeek model is available to a limited group – contact administrator"),
        )


@router.get("/api/explore/llm-config")
async def api_explore_llm_config(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """What the page needs to decide whether it can call the model itself.
    Deliberately independent of the SERVER's own provider credentials: a
    deployment whose server has no egress sets OP_LLM_BROWSER=true and
    OP_LLM_BROWSER_KEY and never sets OP_LLM_API_KEY, and this still answers
    a usable endpoint + model.

    `key` is non-null only when an operator configured a server-held key AND
    this user may run at least one generator (the same population that may
    ask a question at all). Everyone else gets null and pastes their own key
    (BYOK) — which stays in their browser's localStorage and never reaches
    this server."""
    from app.config import settings

    def _run():
        name = nl2sql.provider()
        # `server` = can THIS process reach a model on its own? The page needs
        # the answer before it decides what a form match means: with a model
        # somewhere (here or in the browser) it asks for the cut and runs in
        # one shot; with no model anywhere it runs the Grand Total and opens
        # the form. Deliberately a boolean — the server's own key is never
        # part of this payload.
        server = bool((settings.llm_api_key or "").strip()) if name == "deepseek" \
            else bool((settings.llm_endpoint or "").strip())
        cfg = {"provider": name, "endpoint": nl2sql.endpoint_for(name),
               "model": nl2sql.model_for(name), "browser": bool(settings.llm_browser),
               "server": server, "key": None}
        if name == "deepseek" and not _llm_allowed(db, user):
            # Not on the allowlist: the page sees «no model anywhere» and
            # stays fully deterministic (forms, metric chips, why-cards).
            cfg["server"] = False
            cfg["browser"] = False
            return cfg
        if not settings.llm_browser or not settings.llm_browser_key:
            return cfg
        may_run = any(d.generator_id for d in _generator_dashboards(db, user))
        if not may_run:
            return cfg
        cfg["key"] = settings.llm_browser_key
        sid = request.cookies.get("op_session") or f"uid:{user.id}"
        token = f"{user.id}:{sid}"
        if token not in _KEY_HANDOUTS:
            if len(_KEY_HANDOUTS) >= _MAX_KEY_HANDOUTS:
                _KEY_HANDOUTS.clear()
            _KEY_HANDOUTS.add(token)
            # The key itself is never in the payload — only the fact that a
            # named user received one, which is what an auditor needs.
            _log(db, user, "explore_llm_key", "explore", name,
                 {"provider": name, "model": cfg["model"], "endpoint": cfg["endpoint"]},
                 ip=_client_ip(request))
            db.commit()
        return cfg
    return await run_in_threadpool(_run)


@router.post("/api/explore/intent/prepare")
async def api_explore_intent_prepare(
    body: IntentBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The pre-model half of `/intent`, for a page that will make the model
    call itself. Same reach check, same semantics, same form-first shortcut
    (a confident match is answered here and the page never calls anybody),
    same prompt and schema — no model call, and never a cell value."""
    def _run():
        sem = _intent_semantics(db, user, body.generator_id)
        plan = _intent_plan(db, sem, body)
        _audit = _intent_audit(db, user, request, body.question, body.generator_id)
        if plan["source"] == "why":
            if plan["intent"] is not None:
                answer = _why_answer(plan["intent"], plan["shortlist"])
                _audit("ok", {"kpi": answer["intent"]["kpi"], "source": "why",
                              "score": plan["hit"]["score"]})
                return answer
            _llm_gate(db, user)
            _audit("prepared", {"source": "why"})
            return {"source": "why", "prompt": plan["prompt"], "schema": plan["schema"],
                    "shortlist": plan["shortlist"], "fixed_kpi": None}
        if plan["source"] == "form":
            answer = _form_answer(plan)
            _audit("ok", {"kpi": answer["intent"]["kpi"], "source": "form", "score": plan["hit"]["score"]})
            return _with_residue(answer, body.question, sem)
        _llm_gate(db, user)
        _audit("prepared", {"source": "model", "fixed_kpi": plan.get("fixed_kpi")})
        return {"source": "model", "prompt": plan["prompt"], "schema": plan["schema"],
                "shortlist": plan["shortlist"], "fixed_kpi": plan.get("fixed_kpi")}
    return await run_in_threadpool(_run)


class IntentValidateBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    generator_id: int
    raw: str = Field(min_length=1, max_length=20000)
    fixed_kpi: str | None = Field(default=None, max_length=64)
    # The browser path's half of the follow-up: `prepare` built the prompt with
    # it, so `validate` must ground with it too or the inherited period would
    # exist only on the server path.
    prev_intent: dict | None = None

    @field_validator("prev_intent")
    @classmethod
    def _check_prev(cls, v):
        return _validated_prev(v)


@router.post("/api/explore/intent/validate")
async def api_explore_intent_validate(
    body: IntentValidateBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The post-model half: model text the PAGE received -> validated intent.
    `raw` is untrusted exactly as an Ollama answer is untrusted; the same
    `parse_intent` + `ground_intent` decide what survives, so a browser
    cannot smuggle in a KPI, a dim or an op the semantics do not have."""
    def _run():
        sem = _intent_semantics(db, user, body.generator_id)
        _audit = _intent_audit(db, user, request, body.question, body.generator_id)
        # The class of the question is re-derived here, not taken from the
        # page: a why-question validates into a why-shaped intent (no dims,
        # no order, no limit) whatever the model wrote back.
        if nl2sql.classify_question(body.question) == "why":
            try:
                intent = nl2sql.why_intent_from_raw(
                    body.raw, sem, nl2sql.strip_why(body.question))
            except nl2sql.Nl2SqlError as e:
                _audit("error", {"status_code": e.status_code, "source": "why", "via": "browser"})
                raise HTTPException(status_code=e.status_code, detail=e.message)
            rec = explore_answers.record(db, user.id, body.generator_id, body.question,
                                         "intent", "browser", intent=intent)
            _audit("ok", {"kpi": intent["kpi"], "source": "why", "via": "browser"})
            return _with_answer_id(_why_answer(intent, [], model=nl2sql.model_for()), rec)
        # A `fixed_kpi` the browser sends is not trusted either: `parse_intent`
        # only honours it when the frozen semantics actually carry that key.
        prev = _prev_for(sem, body)
        try:
            intent = nl2sql.ground_intent(
                body.question, nl2sql.parse_intent(body.raw, sem, fixed_kpi=body.fixed_kpi),
                prev_intent=prev)
        except nl2sql.Nl2SqlError as e:
            _audit("error", {"status_code": e.status_code, "source": "browser"})
            raise HTTPException(status_code=e.status_code, detail=e.message)
        rec = explore_answers.record(db, user.id, body.generator_id, body.question,
                                     "intent", "browser", intent=intent)
        _audit("ok", {"kpi": intent["kpi"], "source": "model", "via": "browser",
                      "fixed_kpi": body.fixed_kpi,
                      "dims": intent.get("dims"), "filters": intent.get("filters")})
        return _with_answer_id(_with_residue(
            {"intent": intent, "explanation": intent.get("explanation"),
             "model": nl2sql.model_for(), "source": "model"}, body.question, sem), rec)
    return await run_in_threadpool(_run)


# ───────────────── checker: the model re-checks the intent ────────────────
# One advisory model call between "intent assembled" and "number shown"
# (2026-08-24, owner decision): does the structured query actually answer the
# question, and is the output format right? Fail-open by design — the page
# proceeds on any error/timeout; the verdict can only ADD a clarifying
# question, never block an answer by itself. The prompt carries the question
# and the intent's own names/values (question-origin), never a cell.

# ── Client-side failure log (2026-08-28) ───────────────────────────────
# Everything that can fail on "Ask" after the server has done its part
# happens in the BROWSER: the provider call (BYOK through the corporate
# proxy), the model's SQL running in DuckDB-WASM, the page's own JS. None of
# it left a trace on the server — so "nothing works for me" was
# undiagnosable from here. The page now posts ONE row per failure into the
# same `audit_log` the intent routes write to (`explore_client_error`), so
# «who asked what, and where did it break» is one query. No cell values:
# the question, the stage, the reason text the page showed, the HTTP status,
# the provider host and (tier 2 only) the model-written SQL the engine
# refused — text the page had already put on screen.

class ClientErrorBody(BaseModel):
    stage: Annotated[str, StringConstraints(min_length=1, max_length=32, pattern=r"^[a-z0-9_]+$")]
    reason: str = Field(default="", max_length=500)
    question: str = Field(default="", max_length=500)
    generator_id: int | None = None
    status: int | None = Field(default=None, ge=0, le=999)
    host: str = Field(default="", max_length=80)
    sql: str = Field(default="", max_length=400)
    tier: int | None = Field(default=None, ge=1, le=3)


@router.post("/api/explore/client-error")
async def api_explore_client_error(
    body: ClientErrorBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    def _run():
        payload = {k: v for k, v in body.model_dump().items() if v not in ("", None)}
        payload["via"] = "browser"
        _log(db, user, "explore_client_error", "explore",
             str(body.generator_id) if body.generator_id is not None else "",
             payload, ip=_client_ip(request))
        db.commit()
        return {"ok": True}
    return await run_in_threadpool(_run)


class IntentVerifyBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    generator_id: int
    intent: dict
    residue: list[str] | None = Field(default=None, max_length=8)

    @field_validator("intent")
    @classmethod
    def _check_intent(cls, v):
        if not isinstance(v, dict) or not v.get("kpi"):
            raise ValueError("intent.kpi is required")
        return v

    @field_validator("residue")
    @classmethod
    def _check_residue(cls, v):
        return [str(x)[:80] for x in v][:8] if v else None


@router.post("/api/explore/intent/verify/prepare")
async def api_intent_verify_prepare(
    body: IntentVerifyBody,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The pre-model half for the browser-LLM path: prompt + schema, no call."""
    def _run():
        sem = _intent_semantics(db, user, body.generator_id)
        _llm_gate(db, user)
        return {"prompt": nl2sql.build_verify_prompt(sem, body.question, body.intent, body.residue),
                "schema": nl2sql.VERIFY_SCHEMA}
    return await run_in_threadpool(_run)


@router.post("/api/explore/intent/verify")
async def api_intent_verify(
    body: IntentVerifyBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Server-LLM path: build the prompt, call the provider, parse the verdict."""
    sem = _intent_semantics(db, user, body.generator_id)
    _llm_gate(db, user)
    prompt = nl2sql.build_verify_prompt(sem, body.question, body.intent, body.residue)
    _audit = _intent_audit(db, user, request, body.question, body.generator_id)
    try:
        raw = await run_in_threadpool(nl2sql.call_llm, prompt, nl2sql.VERIFY_SCHEMA)
    except nl2sql.Nl2SqlError as e:
        _audit("error", {"source": "verify", "status_code": e.status_code})
        raise HTTPException(status_code=e.status_code, detail=e.message)
    verdict = nl2sql.parse_verify(raw)
    _audit("ok", {"source": "verify", "verdict_ok": bool(verdict.get("ok"))})
    return verdict


# ───────────────── tier 2: free SQL over the report's semantics ────────────
# Tier 1 (`/intent`) answers the questions somebody already defined as a KPI.
# Tier 2 answers the rest — over the SAME semantic layer, so the model is
# still reading this report's own vocabulary and not a raw 1C export. It is
# the first place in `/explore` where a MODEL writes the SQL, which is why:
#
#   * the page labels the answer "SQL was written by the model from the report's
#     semantics — check it" and shows it, then runs it;
#   * `/validate` is the gate: `is_select_only` (defence in depth) AND the
#     worker's own `guard_sql` — the very module the browser will run it
#     through again before DuckDB, so what passes here is exactly what the
#     engine accepts;
#   * the prompt is still values-free (`build_semantic_sql_prompt` only ever
#     sees `semantics.schema_outline`, which reads empty tables) — pinned by
#     `tests/smoke_explore_intent.py`.
#
# Split prepare/validate for the same reason `/intent` is: on prod the server
# has no egress and the page makes the model call itself.

class SemanticSqlBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    generator_id: int
    # The worker's own `semantics_status()` entry for this generator: which
    # declared tables/views actually stand on the folder the user indexed.
    # Absent -> everything declared is described.
    bindings_status: dict | None = None


def _semantic_plan(db: Session, sem: dict, body: SemanticSqlBody) -> dict:
    from app.config import settings
    status = body.bindings_status if isinstance(body.bindings_status, dict) else None
    # The outline is the only expensive part (an empty-table dry run in a
    # locked-down DuckDB, to resolve what each VIEW actually returns) — do it
    # once and hand it to both consumers.
    outline = semantics_svc.schema_outline(sem)
    prompt = nl2sql.build_semantic_sql_prompt(
        sem, status, body.question, extra_glossary=_platform_glossary(db, body.generator_id),
        outline=outline)
    return {"prompt": prompt,
            "relations": nl2sql.semantic_relations(sem, status, outline=outline),
            # No JSON schema here: the answer is SQL text, not a form. DeepSeek's
            # JSON mode would actively corrupt it.
            "schema": None, "structured": bool(settings.llm_structured)}


def _validate_semantic_sql(raw: str) -> str:
    """Model text -> the SQL the page may run, or `Nl2SqlError(422)`. Two
    gates, both fail-closed: our own conservative single-SELECT check, and
    the browser worker's `guard_sql` — imported, not re-implemented, so the
    server can never accept SQL the engine would refuse (or vice versa)."""
    sql = nl2sql.strip_fences(raw).strip().rstrip(";").strip()
    if not sql:
        raise nl2sql.Nl2SqlError(422, _("The model did not return SQL; rephrase the question"))
    if not nl2sql.is_select_only(sql):
        raise nl2sql.Nl2SqlError(
            422, _("Model returned not a SELECT query; try rephrasing the question"))
    try:
        semantics_svc._worker_guard().guard_sql(sql)
    except Exception as e:
        raise nl2sql.Nl2SqlError(422, _("The request was rejected by the engine's safeguard: {error}",
                                        error=str(e)[:200]))
    return sql


@router.post("/api/explore/semantic-sql/prepare")
async def api_explore_semantic_sql_prepare(
    body: SemanticSqlBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The pre-model half of tier 2: the report's semantic schema as a prompt.
    Same reach check as `/intent` (the user must be able to run a dashboard
    of this generator), no model call, and never a cell value."""
    def _run():
        _llm_gate(db, user)
        sem = _intent_semantics(db, user, body.generator_id)
        plan = _semantic_plan(db, sem, body)
        _audit = _intent_audit(db, user, request, body.question, body.generator_id,
                               action="explore_semantic_sql")
        _audit("prepared", {"relations": plan["relations"]})
        return {"prompt": plan["prompt"], "relations": plan["relations"], "schema": None}
    return await run_in_threadpool(_run)


class SemanticSqlValidateBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    generator_id: int
    raw: str = Field(min_length=1, max_length=20000)


@router.post("/api/explore/semantic-sql/validate")
async def api_explore_semantic_sql_validate(
    body: SemanticSqlValidateBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Model text the PAGE received -> SQL that passed both guards, or 422."""
    def _run():
        _intent_semantics(db, user, body.generator_id)      # reach check, 404 otherwise
        _audit = _intent_audit(db, user, request, body.question, body.generator_id,
                               action="explore_semantic_sql")
        try:
            sql = _validate_semantic_sql(body.raw)
        except nl2sql.Nl2SqlError as e:
            _audit("rejected", {"status_code": e.status_code, "via": "browser"})
            raise HTTPException(status_code=e.status_code, detail=e.message)
        rec = explore_answers.record(db, user.id, body.generator_id, body.question,
                                     "semantic_sql", "browser", sql=sql)
        _audit("ok", {"via": "browser", "sql": sql[:500]})
        return _with_answer_id(
            {"sql": sql, "source": "semantic_sql", "model": nl2sql.model_for()}, rec)
    return await run_in_threadpool(_run)


@router.post("/api/explore/semantic-sql")
async def api_explore_semantic_sql(
    body: SemanticSqlBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Tier 2, server path: this process calls the provider itself. Same
    prompt and the same two guards as the browser path — `_semantic_plan` and
    `_validate_semantic_sql` are shared, so the two can never drift."""
    sem = await run_in_threadpool(_intent_semantics, db, user, body.generator_id)
    plan = await run_in_threadpool(_semantic_plan, db, sem, body)
    _audit = _intent_audit(db, user, request, body.question, body.generator_id,
                           action="explore_semantic_sql")
    _llm_gate(db, user)
    try:
        raw = await run_in_threadpool(nl2sql.call_llm, plan["prompt"], None,
                                      nl2sql.MAX_SQL_TOKENS)
        sql = _validate_semantic_sql(raw)
    except nl2sql.Nl2SqlError as e:
        _audit("error", {"status_code": e.status_code})
        raise HTTPException(status_code=e.status_code, detail=e.message)
    _audit("ok", {"sql": sql[:500], "relations": plan["relations"]})
    return {"sql": sql, "source": "semantic_sql", "model": nl2sql.model_for(),
            "relations": plan["relations"]}


# ─────────────────────────────── saved questions ───────────────────────────

class QuestionBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    question: str | None = Field(default=None, max_length=2000)
    sql: str = Field(min_length=1, max_length=20000)
    table_ids: list[TableId] = Field(default_factory=list, max_length=64)
    shared: bool = False


def _question_json(q: ExploreQuestion, viewer: User) -> dict:
    return {
        "id": q.id,
        "title": q.title,
        "question": q.question,
        "sql": q.sql,
        "table_ids": q.get_table_ids(),
        "shared": q.shared,
        "owner_id": q.user_id,
        "mine": q.user_id == viewer.id,
        "created_at": q.created_at.isoformat() if q.created_at else None,
        "updated_at": q.updated_at.isoformat() if q.updated_at else None,
    }


def _reachable_questions(db: Session, user: User, rows: list[ExploreQuestion]) -> list[ExploreQuestion]:
    """Same reach boundary as `_catalog_snapshot`: a shared question naming
    tables the viewer cannot reach is metadata about those tables (SQL,
    column names, NL text) the viewer must not receive, so it is dropped
    from the list entirely rather than shown with fields blanked out. A
    question naming no tables at all carries no such metadata and stays
    visible to everyone (matches today's behaviour for e.g. a hand-typed
    `SELECT 1` with no `table_ids`)."""
    allowed = _reachable_source_ids(db, user)
    if allowed is None:
        return rows
    all_ids = {tid for q in rows for tid in q.get_table_ids()}
    ds_by_table: dict[str, int] = {}
    if all_ids:
        for tid, ds_id in (db.query(CatalogTable.table_id, CatalogTable.data_source_id)
                          .filter(CatalogTable.table_id.in_(all_ids)).all()):
            ds_by_table[tid] = ds_id
    out = []
    for q in rows:
        if q.user_id == user.id:
            out.append(q)
            continue
        tids = q.get_table_ids()
        if not tids:
            out.append(q)
            continue
        if any(ds_by_table.get(tid) in allowed for tid in tids):
            out.append(q)
    return out


MAX_QUESTIONS_LISTED = 500
MAX_QUESTIONS_PER_USER = 200


@router.get("/api/explore/questions")
async def api_explore_questions_list(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    def _run():
        rows = (db.query(ExploreQuestion)
               .filter((ExploreQuestion.user_id == user.id) | (ExploreQuestion.shared == True))  # noqa: E712
               .order_by(ExploreQuestion.created_at.desc())
               .limit(MAX_QUESTIONS_LISTED).all())
        rows = _reachable_questions(db, user, rows)
        return {"questions": [_question_json(q, user) for q in rows]}
    return await run_in_threadpool(_run)


@router.post("/api/explore/questions")
async def api_explore_questions_create(
    body: QuestionBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    def _run():
        owned = db.query(ExploreQuestion).filter(ExploreQuestion.user_id == user.id).count()
        if owned >= MAX_QUESTIONS_PER_USER:
            raise HTTPException(status_code=422,
                                detail=_("Saved questions limit reached ({p1})", p1=MAX_QUESTIONS_PER_USER))

        if not nl2sql.is_select_only(body.sql):
            raise HTTPException(status_code=422,
                                detail=_("Only one SELECT query can be saved"))

        if body.table_ids:
            snapshot = _catalog_snapshot(db, user, body.table_ids, values_cap=0)
            found_ids = {t["table_id"] for t in snapshot["tables"]}
            missing = [t for t in body.table_ids if t not in found_ids][:32]
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail=_("Unavailable or unknown tables: {p1}", p1=', '.join(missing)))

        q = ExploreQuestion(user_id=user.id, title=body.title, question=body.question,
                            sql=body.sql, shared=body.shared)
        q.set_table_ids(body.table_ids)
        db.add(q)
        db.flush()
        _log(db, user, "explore_question_create", "explore_question", str(q.id), {
            "title": body.title, "shared": body.shared, "table_ids": body.table_ids,
        }, ip=_client_ip(request))
        db.commit()
        return _question_json(q, user)
    return await run_in_threadpool(_run)


@router.delete("/api/explore/questions/{question_id}")
async def api_explore_questions_delete(
    question_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    def _run():
        q = db.query(ExploreQuestion).filter(ExploreQuestion.id == question_id).first()
        if q is None:
            raise HTTPException(status_code=404, detail="Question not found")
        if q.user_id != user.id:
            raise HTTPException(status_code=403, detail="Only the owner can delete a saved question")
        _log(db, user, "explore_question_delete", "explore_question", str(q.id),
            {"title": q.title}, ip=_client_ip(request))
        db.delete(q)
        db.commit()
        return {"ok": True}
    return await run_in_threadpool(_run)


# ────────────────────────── recorded AI answers (read) ─────────────────────
# Read side of `explore_answers` (see app/services/explore_answers.py for the
# write side and the docstring on why RESULTS and raw SQL never land there).
# An ordinary user sees only their own rows; `view_admin` sees everyone's —
# the same split `/api/admin/audit-log` uses for the audit trail this table
# is the LLM-shaped sibling of.

def _answer_json(a: ExploreAnswer) -> dict:
    return {
        "id": a.id,
        "user_id": a.user_id,
        "generator_id": a.generator_id,
        "dashboard_id": a.dashboard_id,
        "question": a.question,
        "tier": a.tier,
        "intent": a.get_intent(),
        "sql_sha256": a.sql_sha256,
        "generator_version": a.generator_version,
        "semantics_sha256": a.semantics_sha256,
        "run_id": a.run_id,
        "via": a.via,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        # The question author's vote is the only thing in a recorded answer
        # that changes after recording; without it the answer list cannot show
        # what has already been rated.
        "vote": a.vote,
        "voted_at": a.voted_at.isoformat() if a.voted_at else None,
    }


@router.get("/api/explore/answers")
async def api_explore_answers_list(
    generator: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    def _run():
        q = db.query(ExploreAnswer)
        if not has_permission(user.role, "view_admin"):
            q = q.filter(ExploreAnswer.user_id == user.id)
        if generator is not None:
            q = q.filter(ExploreAnswer.generator_id == generator)
        rows = q.order_by(ExploreAnswer.created_at.desc()).limit(limit).all()
        return {"answers": [_answer_json(a) for a in rows]}
    return await run_in_threadpool(_run)


# ───────────────────── 👍/👎 and the form suggestion bank (P4) ──────────────
# A vote belongs to whoever asked the question and nobody else; a form suggestion
# is created ONLY by a vote (see app/services/explore_answers.py for why
# there is no separate "suggest a form"). The queue is reviewed by the report author: whoever
# can refresh the data of this generator's dashboard.

class VoteBody(BaseModel):
    # `null` clears the vote. Nothing but these three values gets through.
    vote: str | None = Field(default=None, pattern=r"^(up|down)$")


def _curated_generator_ids(db: Session, user: User) -> set[int]:
    """Generators whose suggestion queue this person may see and
    review: the generator must be REACHABLE (the user has a dashboard on
    it) and that dashboard must grant the right to refresh data — the same
    `can_update_dashboard_data` that guards bundle upload and starting
    a run in app/api/generators.py. For a non-administrator `_generator_dashboards`
    already returns only own + can_update_data, but the check is explicit here:
    the rule "the author's queue is reviewed by the author" must not depend on how
    reachability might one day be extended.

    ADR-18: an RLS dashboard opens the queue only to someone who sees the report
    IN FULL — the same filter as in `_reports_snapshot`. A suggestion quotes
    the asker's question and intent, and their filters come from the question
    text: a curator with a narrowed scope would read through the queue what
    the slot files do not give them. The user_scopes query is made only for
    dashboards with a policy (usually there are none at all)."""
    dashboards = [d for d in _generator_dashboards(db, user)
                  if d.generator_id and not d.confidential]
    if any(d.rls_enabled for d in dashboards):
        from app.services import rls
        dashboards = [d for d in dashboards
                      if not d.rls_enabled or rls.subject(db, d, user).see_all]
    out: set[int] = set()
    for d in dashboards:
        if d.generator_id not in out \
                and can_update_dashboard_data(dashboard=d, user=user, db=db):
            out.add(d.generator_id)
    return out


def _suggestion_json(s: ExploreFormSuggestion) -> dict:
    return {
        "id": s.id,
        "generator_id": s.generator_id,
        "user_id": s.user_id,
        "answer_id": s.answer_id,
        "question": s.question,
        "kpi_key": s.kpi_key,
        "intent": s.get_intent(),
        # A hash, not SQL — see the model: a literal in a filter could quote
        # a real value.
        "sql_hash": s.sql_hash,
        "kind": s.kind,
        "status": s.status,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "decided_at": s.decided_at.isoformat() if s.decided_at else None,
        "decided_by": s.decided_by,
    }


@router.post("/api/explore/answers/{answer_id}/vote")
async def api_explore_answer_vote(
    answer_id: int,
    body: VoteBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """👍/👎 on YOUR OWN recorded answer. Someone else's (or a non-existent) id is 404, not
    403: a 403 would confirm that such an answer exists.

    Idempotent: the same vote a second time changes nothing and creates no second
    form suggestion. `suggested: true` means this vote sent the report author
    a suggestion (👎 → kind B, the third 👍 on one and the same question →
    kind A)."""
    def _run():
        out = explore_answers.vote(db, user, answer_id, body.vote)
        if out is None:
            raise HTTPException(status_code=404, detail=_("Answer not found"))
        sug = out.get("suggestion")
        if sug is not None:
            # Only id, kind and metric key — the question does not go into the shared log
            # (it already lies in the suggestion row itself, which the author sees).
            _log(db, user, "explore_form_suggest", "explore_form_suggestion", str(sug.id),
                 {"kind": sug.kind, "kpi_key": sug.kpi_key,
                  "generator_id": sug.generator_id, "answer_id": sug.answer_id},
                 ip=_client_ip(request))
        db.commit()
        return {"ok": True, "vote": out["vote"], "suggested": out["suggested"]}
    return await run_in_threadpool(_run)


@router.get("/api/explore/form-suggestions")
async def api_explore_form_suggestions(
    generator: int = Query(...),
    status: str = Query(default="new", pattern=r"^(new|approved|declined|all)$"),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The suggestion queue for one generator. An unreachable generator (or
    a reachable one without the right to refresh its data) is 404, the same way
    as `_intent_semantics`: "no access" and "no such thing" are indistinguishable from outside."""
    def _run():
        if generator not in _curated_generator_ids(db, user):
            raise HTTPException(status_code=404, detail=_("Generator unavailable"))
        q = (db.query(ExploreFormSuggestion)
             .filter(ExploreFormSuggestion.generator_id == generator))
        if status != "all":
            q = q.filter(ExploreFormSuggestion.status == status)
        rows = q.order_by(ExploreFormSuggestion.id.desc()).limit(limit).all()
        return {"suggestions": [_suggestion_json(s) for s in rows]}
    return await run_in_threadpool(_run)


def _decide(db: Session, user: User, request: Request, suggestion_id: int,
            decision: str) -> dict:
    row = (db.query(ExploreFormSuggestion)
           .filter(ExploreFormSuggestion.id == suggestion_id).first())
    if row is None or row.generator_id not in _curated_generator_ids(db, user):
        raise HTTPException(status_code=404, detail=_("Suggestion not found"))
    # The decision is made ONCE and only from `new`: a repeat (two tabs,
    # a double click) and "changed my mind" are not the same thing, and the second must not
    # look like the first. 409, not 404: the suggestion exists and is visible.
    if row.status != "new":
        raise HTTPException(status_code=409, detail=_("Suggestion has already been resolved"))
    row.status = decision
    row.decided_at = datetime.utcnow()
    row.decided_by = user.id
    # The audit holds id, kind and metric key. There is no question text here: it already
    # lies in the suggestion row itself, and duplicating it into the shared log
    # would spread the question wider than the report author.
    _log(db, user, "explore_form_decide", "explore_form_suggestion", str(row.id),
         {"kind": row.kind, "kpi_key": row.kpi_key, "status": decision,
          "generator_id": row.generator_id}, ip=_client_ip(request))
    db.commit()
    return {"ok": True, "id": row.id, "status": row.status}


@router.post("/api/explore/form-suggestions/{suggestion_id}/approve")
async def api_explore_form_suggestion_approve(
    suggestion_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await run_in_threadpool(_decide, db, user, request, suggestion_id, "approved")


@router.post("/api/explore/form-suggestions/{suggestion_id}/decline")
async def api_explore_form_suggestion_decline(
    suggestion_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await run_in_threadpool(_decide, db, user, request, suggestion_id, "declined")
