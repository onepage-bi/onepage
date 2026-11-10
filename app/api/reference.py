"""ADR-16 "Reference tables" — phase 2: the JSON/xlsx API.

CRUD + Excel import/export/versions/consumers over `app/services/reference.py`
(pure schema/row/diff logic, phase 1) and the three models
`ReferenceTable`/`ReferenceTableVersion`/`RunReferenceUse`. No UI here (phase
6 adds `GET /reference` and `/reference/{slug}` pages) and no engine/manifest
work (phase 3/4) — this module only reads `genmanifest.platform_inputs` to
compute consumers and to seed a new table's schema from a generator's current
manifest.

Access model (owner's decision, ADR-16 + addendum 2026-08-27):
  * read  — admin, OR reach to any dashboard that consumes this table IN A RUN
    (`_run_consumers`: manifest `source: "platform"` inputs only, via
    `require_dashboard_access` semantics), OR — for a table with no such
    consumer — its creator or its responsible user.
  * write — admin, the table's `responsible_user_id`, or — when nobody is
    responsible — anyone with `can_update_data` on a RUN consumer dashboard.
  * A semantics `sources: [{"reference": slug}]` consumer ("in chat") is NOT
    reach: declaring someone else's dictionary in your own bundle must not
    hand its rows to everyone who can open your report. It is a label on the
    consumers list, nothing more.
  * create — admin, or anyone with at least one generator-backed dashboard
    they may run (`_generator_dashboards` reach, same boundary Data Sources/
    Glossary use).

Module-local `_log` (idiom: app/api/favorites.py) — every write is audited.
"""
from __future__ import annotations

import io
import json
import re
from datetime import date as _date, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from app.auth.deps import can_update_dashboard_data, get_current_user
from app.database import get_db
from app.i18n import _
from app.models import (
    AuditLog,
    Dashboard,
    DashboardPermission,
    GenerationRun,
    Generator,
    GeneratorVersion,
    ReferenceTable,
    ReferenceTableVersion,
    ROLE_ADMINISTRATOR,
    RunReferenceUse,
    User,
)
from app.api.generators import _generator_dashboards
from app.services import reference as refsvc

from sqlalchemy.orm import Session

router = APIRouter()

_MAX_IMPORT_BYTES = 5 * 1024 * 1024  # ADR-16 decision
_CHUNK = 256 * 1024
_MAX_TEMPLATE_KEYS = 500
_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MAX_ROW_ERRORS = 200


def _log(db: Session, actor: User, action: str, resource_type: str,
         resource_id: str, detail: dict, ip: str = "") -> None:
    entry = AuditLog(
        actor_id=actor.id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        ip_address=ip,
    )
    entry.set_detail(detail)
    db.add(entry)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


# ---------------------------------------------------------------------------
# Manifest bridge — phase 3's genmanifest.platform_inputs may not have landed
# yet; both call sites (consumers, schema-from-manifest) fall back to [].
# ---------------------------------------------------------------------------
def _platform_inputs_of(manifest: dict) -> list[dict]:
    try:
        from app.services.genmanifest import platform_inputs
    except ImportError:
        return []
    try:
        return platform_inputs(manifest)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Access helpers
# ---------------------------------------------------------------------------
# A generator consumes a reference table in one of TWO ways, and the page has
# to show both or a dictionary read only by the chat looks like it has nobody
# behind it: `run` — the manifest declares it as a platform input, so a RUN of
# the report reads it; `chat` — the frozen semantics declare a table with
# `sources: [{"reference": slug}]`, so "Ask" reads it (ADR-16 +
# ADR-13). Both are read off the CURRENT version of the generator, and both
# count as reach for `_can_read`/`_can_write`: the right to read a dictionary
# follows the report that uses it, whichever way it uses it.
VIA_RUN = "run"
VIA_CHAT = "chat"


def _run_consumers(db: Session, slug: str, memo: dict | None = None) -> list[dict]:
    """The consumers that count as REACH — manifest ones only (ADR-16
    addendum 2026-08-27).

    A chat consumer is NOT reach, deliberately. `sources: [{"reference":
    slug}]` is a line anyone who may register a generator can write, and it
    names a slug they may have no right to read; if that line widened
    `_can_read`, publishing a report that merely MENTIONS someone else's
    dictionary would hand its rows to everyone who can open that report. The
    manifest input is a different thing: the run actually materialises the
    workbook, so whoever may run the report has to be able to read it.
    "In chat" therefore stays a label on the consumers list and never a key."""
    return [c for c in _consumers(db, slug, memo) if VIA_RUN in (c.get("via") or [])]


def _semantics_references_of(ver: GeneratorVersion) -> list[str]:
    """The reference slugs a version's frozen semantics read, or []. Same
    tolerance as `_platform_inputs_of`: anything unreadable is simply not a
    consumer, never a 500 on a page that only wanted to draw a list."""
    raw = getattr(ver, "semantics_json", None)
    if not raw:
        return []
    try:
        from app.services import semantics as semsvc
        sem = json.loads(raw)
        return sorted({r["slug"] for r in semsvc.reference_sources(sem).values()})
    except Exception:
        return []


def _consumer_generators_all(db: Session) -> dict[str, dict[int, tuple]]:
    """slug -> {generator_id: (generator_name, {via, ...})} for EVERY platform
    input any current manifest declares AND every reference source any current
    semantics declares — one query and ONE pass of parsing for the whole
    request, however many slugs are asked about afterwards."""
    out: dict[str, dict[int, tuple]] = {}

    def _add(slug, gen, via):
        if not (isinstance(slug, str) and slug):
            return
        _name, vias = out.setdefault(slug, {}).setdefault(gen.id, (gen.name, set()))
        vias.add(via)

    for ver, gen in (
        db.query(GeneratorVersion, Generator)
        .join(Generator, Generator.id == GeneratorVersion.generator_id)
        .filter(GeneratorVersion.is_current == True)  # noqa: E712
        .all()
    ):
        try:
            manifest = ver.get_manifest()
        except (ValueError, TypeError):
            manifest = None
        if manifest is not None:
            for p in _platform_inputs_of(manifest):
                _add(p.get("table"), gen, VIA_RUN)
        for slug in _semantics_references_of(ver):
            _add(slug, gen, VIA_CHAT)
    return out


def _consumer_generators(db: Session, slug: str,
                         memo: dict | None = None) -> dict[int, tuple]:
    """{generator_id: (generator_name, {via, ...})} for every generator whose
    CURRENT manifest declares a platform input for this slug or whose CURRENT
    semantics read it in the chat. One query.

    With a `memo` (a plain per-request dict the caller owns), the sweep happens
    ONCE: the list route asks about N tables and used to parse every
    generator's manifest 2N times (`_can_read` and `_table_summary` both call
    `_consumers`), which is N×M JSON parses for a page that needs M."""
    if memo is not None:
        if "gens" not in memo:
            memo["gens"] = _consumer_generators_all(db)
        return memo["gens"].get(slug, {})
    return _consumer_generators_all(db).get(slug, {})


def _consumers(db: Session, slug: str, memo: dict | None = None) -> list[dict]:
    """[{dashboard_id, title, generator_id, generator_name, last_run}] for
    every dashboard currently bound to a generator that declares this table
    as a platform input. `last_run` is the latest RunReferenceUse for that
    dashboard against this table, or None. Three queries total (generators,
    dashboards, run-uses) — never one per dashboard.

    `memo` is an optional per-request dict: pass the SAME one to every call in
    one request and each slug is computed at most once (see
    `_consumer_generators`). Never cache it across requests — a manifest
    upload or a new run must be visible on the next page load."""
    if memo is not None:
        cached = memo.setdefault("consumers", {})
        if slug in cached:
            return cached[slug]
    result = _consumers_uncached(db, slug, memo)
    if memo is not None:
        memo["consumers"][slug] = result
    return result


def _consumers_uncached(db: Session, slug: str, memo: dict | None) -> list[dict]:
    gen_name = _consumer_generators(db, slug, memo)
    if not gen_name:
        return []
    dashboards = (
        db.query(Dashboard)
        .filter(Dashboard.generator_id.in_(gen_name.keys()))
        .order_by(Dashboard.updated_at.desc())
        .all()
    )
    if not dashboards:
        return []
    dash_ids = [d.id for d in dashboards]

    last_by_dash: dict[str, dict] = {}
    table = refsvc.get_table(db, slug)
    if table is not None:
        rows = (
            db.query(RunReferenceUse, GenerationRun)
            .join(GenerationRun, GenerationRun.id == RunReferenceUse.run_id)
            .filter(RunReferenceUse.table_id == table.id,
                    GenerationRun.dashboard_id.in_(dash_ids))
            .order_by(GenerationRun.dashboard_id, GenerationRun.started_at.desc())
            .all()
        )
        for use, run in rows:
            if run.dashboard_id in last_by_dash:
                continue  # first row per dashboard, already ordered newest-first
            last_by_dash[run.dashboard_id] = {
                "run_id": run.id,
                "version_number": use.version_number,
                "applied": use.applied,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }

    def _row(d):
        name, vias = gen_name.get(d.generator_id) or (None, set())
        return {
            "dashboard_id": d.id,
            "title": d.title,
            "generator_id": d.generator_id,
            "generator_name": name,
            # Sorted so the badge order on the page is stable; ["chat"] alone
            # is exactly the case the "in chat" pill exists for.
            "via": sorted(vias) or [VIA_RUN],
            "last_run": last_by_dash.get(d.id),
        }

    return [_row(d) for d in dashboards]


def _dashboard_reachable(db: Session, user: User, dashboard: Dashboard) -> bool:
    """Same rule as `require_dashboard_access` (app/auth/deps.py), as a plain
    boolean check over an already-loaded Dashboard — admin, owner, public, or
    an explicit permission on a published dashboard."""
    if user.role == ROLE_ADMINISTRATOR:
        return True
    if dashboard.owner_id == user.id:
        return True
    if dashboard.is_public:
        return True
    perm = db.query(DashboardPermission).filter(
        DashboardPermission.dashboard_id == dashboard.id,
        DashboardPermission.user_id == user.id,
    ).first()
    return bool(perm and dashboard.is_published)


def _can_read(db: Session, user: User, table: ReferenceTable,
              memo: dict | None = None) -> bool:
    if user.role == ROLE_ADMINISTRATOR:
        return True
    # Whoever may WRITE may read. Without this the responsible user of a table
    # that HAS consumers but whose dashboards they cannot reach could import a
    # new version (`_can_write` returns True for them unconditionally) yet get
    # a 403 from GET /api/reference/{slug}, from the xlsx export and from the
    # template — and the table would not even appear in their list.
    if table.responsible_user_id is not None and table.responsible_user_id == user.id:
        return True
    if table.created_by == user.id:
        return True
    consumers = _run_consumers(db, table.slug, memo)
    if not consumers:
        return table.created_by == user.id or table.responsible_user_id == user.id
    dash_ids = {c["dashboard_id"] for c in consumers}
    dashboards = db.query(Dashboard).filter(Dashboard.id.in_(dash_ids)).all()
    return any(_dashboard_reachable(db, user, d) for d in dashboards)


def _can_write(db: Session, user: User, table: ReferenceTable,
               memo: dict | None = None) -> bool:
    if user.role == ROLE_ADMINISTRATOR:
        return True
    if table.responsible_user_id is not None:
        return table.responsible_user_id == user.id
    consumers = _run_consumers(db, table.slug, memo)
    if not consumers:
        return False
    dash_ids = {c["dashboard_id"] for c in consumers}
    dashboards = db.query(Dashboard).filter(Dashboard.id.in_(dash_ids)).all()
    return any(can_update_dashboard_data(dashboard=d, user=user, db=db) for d in dashboards)


def _can_create(db: Session, user: User) -> bool:
    if user.role == ROLE_ADMINISTRATOR:
        return True
    return len(_generator_dashboards(db, user)) > 0


# ---------------------------------------------------------------------------
# JSON shaping helpers
# ---------------------------------------------------------------------------
def _resolve_responsible(db: Session, user_id: int | None,
                         person_id: str | None) -> int | None:
    """The "responsible" field accepts EITHER a numeric `users.id` or an
    `person_id`. The shared typeahead (`static/js/user-suggest.js`, the same
    widget the DeepSeek allowlist uses) fills its input with an person_id, not
    a row id — a page that only understood the number silently assigned NOBODY
    every time someone picked a person from the list."""
    if person_id:
        ovid = person_id.strip()
        if not ovid:
            return None
        row = db.query(User).filter(User.person_id == ovid).first()
        if row is None:
            raise HTTPException(status_code=400,
                                detail=f"responsible: no such user {ovid!r}")
        return row.id
    if user_id is None:
        return None
    if db.query(User).filter(User.id == user_id).first() is None:
        raise HTTPException(status_code=400, detail="responsible_user_id: no such user")
    return user_id


def _responsible_json(db: Session, table: ReferenceTable) -> dict | None:
    if not table.responsible_user_id:
        return None
    u = db.query(User).filter(User.id == table.responsible_user_id).first()
    if u is None:
        return None
    return {"id": u.id, "display_name": u.display_name or u.person_id}


def _row_count_of_current(db: Session, table: ReferenceTable) -> int:
    if not table.current_version:
        return 0
    v = db.query(ReferenceTableVersion).filter(
        ReferenceTableVersion.table_id == table.id,
        ReferenceTableVersion.version_number == table.current_version,
    ).first()
    return v.row_count if v else 0


def _table_summary(db: Session, table: ReferenceTable,
                   memo: dict | None = None, user: User | None = None) -> dict:
    consumers = _consumers(db, table.slug, memo)
    # The list page filters by report/generator client-side ("Reference tables
    # of this report" is a link, not a query), so the identities travel with
    # the row. Generators come from the DECLARATIONS, not from the consumer
    # rows: a generator registered before anyone made a dashboard from it has
    # no consumer row at all, and the filter would then show nothing for a
    # report that plainly declares the dictionary.
    gens = {gid: name for gid, (name, _vias) in
            _consumer_generators(db, table.slug, memo).items()}
    # Dashboards, on the other hand, are identities the reader may not be
    # allowed to see: only the ones they can reach are named, the rest are
    # left to the `consumers` count.
    dashes: list[dict] = []
    if consumers:
        ids = [c["dashboard_id"] for c in consumers]
        by_id = {d.id: d for d in
                 db.query(Dashboard).filter(Dashboard.id.in_(ids)).all()}   # one query
        for c in consumers:
            d = by_id.get(c["dashboard_id"])
            if d is None or (user is not None and not _dashboard_reachable(db, user, d)):
                continue
            dashes.append({"id": c["dashboard_id"], "title": c["title"]})
    return {
        "slug": table.slug,
        "title": table.title,
        "key_column": table.key_column,
        "columns": table.get_columns(),
        "row_count": _row_count_of_current(db, table),
        "current_version": table.current_version,
        "allow_pin": table.allow_pin,
        "responsible": _responsible_json(db, table),
        "consumers": len(consumers),
        "consumer_generators": [{"id": gid, "name": name}
                                for gid, name in sorted(gens.items(), key=lambda kv: kv[1])],
        # `consumer_dashboards` names only what this reader may open; the
        # `consumers` count above is the honest total either way.
        "consumer_dashboards": dashes,
        "created_at": table.created_at.isoformat() if table.created_at else None,
        "updated_at": table.updated_at.isoformat() if table.updated_at else None,
    }


def _versions_payload(db: Session, table: ReferenceTable, limit: int = 20) -> list[dict]:
    versions_q = (
        db.query(ReferenceTableVersion)
        .filter(ReferenceTableVersion.table_id == table.id)
        .order_by(ReferenceTableVersion.version_number.desc())
        .limit(limit)
        .all()
    )
    creator_ids = {v.created_by for v in versions_q if v.created_by}
    creators: dict[int, str] = {}
    if creator_ids:
        for u in db.query(User).filter(User.id.in_(creator_ids)).all():
            creators[u.id] = u.display_name or u.person_id
    out = []
    for v in versions_q:
        summary: dict = {}
        if v.summary_json:
            try:
                summary = json.loads(v.summary_json)
            except (ValueError, TypeError):
                summary = {}
        out.append({
            "number": v.version_number,
            "row_count": v.row_count,
            "summary": summary,
            "note": v.note,
            "created_by": creators.get(v.created_by),
            "created_at": v.created_at.isoformat() if v.created_at else None,
        })
    return out


def _resolve_version(table: ReferenceTable, n: str) -> int:
    if n == "current":
        return table.current_version
    try:
        v = int(n)
    except ValueError:
        raise HTTPException(status_code=400, detail="version must be an integer or 'current'")
    if v < 0:
        raise HTTPException(status_code=400, detail="version must be >= 0")
    return v


def _rows_for_version(db: Session, table: ReferenceTable, version_number: int) -> list[list]:
    if version_number == 0:
        return []
    v = db.query(ReferenceTableVersion).filter(
        ReferenceTableVersion.table_id == table.id,
        ReferenceTableVersion.version_number == version_number,
    ).first()
    if v is None:
        raise HTTPException(status_code=404, detail="Version not found")
    try:
        rows = json.loads(v.rows_json)
    except (ValueError, TypeError):
        rows = []
    if not isinstance(rows, list):
        return []
    # Same fitting `refsvc.current_rows` does: export, restore and the diff
    # all see the CURRENT schema's width whatever version they read.
    return refsvc.fit_rows(table.get_columns(), rows)


def _get_table_or_404(db: Session, slug: str) -> ReferenceTable:
    table = refsvc.get_table(db, slug)
    if table is None:
        raise HTTPException(status_code=404, detail="Reference table not found")
    return table


# ---------------------------------------------------------------------------
# xlsx building (openpyxl write_only, run in threadpool per the plan)
# ---------------------------------------------------------------------------
def _build_xlsx(columns: list[dict], rows: list[list]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("data")

    def _cell(ws, value):
        """A stored value that begins with "=" is DATA, never a formula.
        openpyxl infers the type from the string and writes `<f>…</f>` for a
        leading "=" — so an imported cell `=cmd|'/c calc'!A1` would come back
        out of this export as a live formula/DDE payload in whoever opens the
        downloaded workbook. Pinning data_type="s" makes it an inlineStr; it
        round-trips through _parse_import_workbook unchanged."""
        if isinstance(value, str) and value.startswith("="):
            c = WriteOnlyCell(ws, value=value)
            c.data_type = "s"
            return c
        return value

    ws.append([_cell(ws, c["name"]) for c in columns] + ["_key_norm"])
    for row in rows:
        key_norm = refsvc.norm_key(row[0]) if row else ""
        out_row = []
        for i, col in enumerate(columns):
            value = row[i] if i < len(row) else None
            if col["type"] == "date" and isinstance(value, str) and value:
                try:
                    value = _date.fromisoformat(value)
                except ValueError:
                    pass
            out_row.append(_cell(ws, value))
        out_row.append(key_norm)
        ws.append(out_row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xlsx_response(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type=_XLSX_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=0",
        },
    )


# ---------------------------------------------------------------------------
# xlsx parsing (import) — run in threadpool
# ---------------------------------------------------------------------------
def _parse_import_workbook(columns: list[dict], content: bytes) -> tuple[list[list], list[dict]]:
    """(rows, header_errors). rows are lists in `columns` order, RAW cell
    values (coerce_row does the typed coercion, one caller up) — a header
    problem is returned instead of raised so the route can 400 with a
    consistent payload shape.

    The row ceiling is enforced HERE, while streaming, not after. A valid
    5 MB xlsx compresses to ~500k rows (measured: 700k rows = 7 MB zip,
    4.7 s parse, 264 MB RSS), and `refsvc.create_version`'s MAX_ROWS check
    only runs on COMMIT — a dry_run had no ceiling at all. Stopping one row
    past the limit turns that into a cheap 400."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb["data"] if "data" in wb.sheetnames else wb[wb.sheetnames[0]]
    it = ws.iter_rows(values_only=True)
    try:
        header = next(it)
    except StopIteration:
        header = None
    if not header:
        return [], [{"row": 0, "column": None, "reason": _("empty sheet")}]

    header_norm = [str(h).strip().casefold() if h is not None else "" for h in header]
    col_idx: dict[str, int] = {}
    missing: list[str] = []
    for c in columns:
        key = c["name"].strip().casefold()
        if key in header_norm:
            col_idx[c["name"]] = header_norm.index(key)
        else:
            missing.append(c["name"])
    if missing:
        return [], [{"row": 0, "column": None,
                     "reason": _("the file is missing columns: {columns}",
                                 columns=", ".join(missing))}]

    rows: list[list] = []
    for raw_row in it:
        if raw_row is None:
            continue
        if all(v is None or (isinstance(v, str) and not v.strip()) for v in raw_row):
            continue  # blank row, skipped per the plan
        if len(rows) >= refsvc.MAX_ROWS:
            return [], [{"row": 0, "column": None,
                         "reason": _("the file has more than {max} rows",
                                     max=refsvc.MAX_ROWS)}]
        picked = []
        for c in columns:
            idx = col_idx[c["name"]]
            picked.append(raw_row[idx] if idx < len(raw_row) else None)
        rows.append(picked)
    return rows, []


async def _read_capped_xlsx(file: UploadFile) -> bytes:
    first = await file.read(_CHUNK)
    if not first:
        raise HTTPException(status_code=400, detail="Empty file")
    if not first.startswith(b"PK\x03\x04"):
        raise HTTPException(status_code=415, detail="Only .xlsx files are accepted")
    buf = bytearray(first)
    while len(buf) <= _MAX_IMPORT_BYTES:
        chunk = await file.read(_CHUNK)
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
    raise HTTPException(
        status_code=413,
        detail=f"file too large (max {_MAX_IMPORT_BYTES // (1024 * 1024)} MB)",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/api/reference")
async def list_reference_tables(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tables = db.query(ReferenceTable).order_by(ReferenceTable.title).all()
    memo: dict = {}   # one manifest sweep for the whole list, not 2 per table
    return [_table_summary(db, t, memo, user) for t in tables if _can_read(db, user, t, memo)]


class ReferenceColumnBody(BaseModel):
    name: str
    type: str


class ReferenceCreateBody(BaseModel):
    slug: str
    title: str
    description: str | None = None
    allow_pin: bool = False
    responsible_user_id: int | None = None
    responsible_person_id: str | None = None   # what the typeahead fills in
    columns: list[ReferenceColumnBody] | None = None
    from_generator_id: int | None = None


@router.post("/api/reference", status_code=201)
async def create_reference_table(
    body: ReferenceCreateBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _can_create(db, user):
        raise HTTPException(status_code=403, detail="No reach to create a reference table")

    slug = (body.slug or "").strip()
    if not refsvc.SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    title = (body.title or "").strip()
    if not title or len(title) > 120:
        raise HTTPException(status_code=400, detail="title must be 1..120 characters")
    if body.description is not None and len(body.description) > 500:
        raise HTTPException(status_code=400, detail="description must be at most 500 characters")

    if refsvc.get_table(db, slug) is not None:
        raise HTTPException(status_code=409, detail="slug already taken")

    columns: list[dict] | None = None
    if body.columns:
        columns = [{"name": c.name, "type": c.type} for c in body.columns]
    elif body.from_generator_id is not None:
        gen_version = db.query(GeneratorVersion).filter(
            GeneratorVersion.generator_id == body.from_generator_id,
            GeneratorVersion.is_current == True,  # noqa: E712
        ).first()
        if gen_version is None:
            raise HTTPException(status_code=404, detail="Generator has no current version")
        try:
            manifest = gen_version.get_manifest()
        except (ValueError, TypeError):
            manifest = {}
        match = next((p for p in _platform_inputs_of(manifest) if p.get("table") == slug), None)
        if match is None:
            raise HTTPException(
                status_code=400,
                detail="Generator's current manifest has no platform input for this slug",
            )
        columns = match["columns"]

    if not columns:
        raise HTTPException(status_code=400, detail="columns or from_generator_id required")
    try:
        columns = refsvc.validate_schema(columns)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    responsible_id = _resolve_responsible(db, body.responsible_user_id,
                                          body.responsible_person_id)

    table = ReferenceTable(
        slug=slug, title=title, description=body.description or None,
        key_column=columns[0]["name"], kind="simple",
        allow_pin=bool(body.allow_pin), responsible_user_id=responsible_id,
        current_version=0, created_by=user.id,
    )
    table.set_columns(columns)
    db.add(table)
    db.flush()
    refsvc.ensure_data_source(db, table)
    _log(db, user, "reference_create", "reference_table", slug, {
        "title": title, "columns": [c["name"] for c in columns],
        "from_generator_id": body.from_generator_id,
    }, _client_ip(request))
    db.commit()
    return _table_summary(db, table, None, user)


@router.get("/api/reference/{slug}")
async def get_reference_table(
    slug: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    table = _get_table_or_404(db, slug)
    if not _can_read(db, user, table):
        raise HTTPException(status_code=403, detail="Access denied")

    rows = refsvc.current_rows(db, table)
    total = len(rows)
    page = rows[offset:offset + limit]

    return {
        "slug": table.slug,
        "title": table.title,
        "description": table.description,
        "key_column": table.key_column,
        "columns": table.get_columns(),
        "current_version": table.current_version,
        "allow_pin": table.allow_pin,
        "responsible": _responsible_json(db, table),
        "rows": page,
        "row_offset": offset,
        "row_limit": limit,
        "row_total": total,
        "versions": _versions_payload(db, table, limit=20),
        "consumers": _consumers(db, table.slug),
        "created_at": table.created_at.isoformat() if table.created_at else None,
        "updated_at": table.updated_at.isoformat() if table.updated_at else None,
    }


class ReferenceUpdateBody(BaseModel):
    title: str | None = None
    description: str | None = None
    allow_pin: bool | None = None
    responsible_user_id: int | None = None
    responsible_person_id: str | None = None   # what the typeahead fills in


@router.put("/api/reference/{slug}")
async def update_reference_table(
    slug: str,
    body: ReferenceUpdateBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    table = _get_table_or_404(db, slug)
    if not _can_write(db, user, table):
        raise HTTPException(status_code=403, detail="No write access")

    fields = body.model_fields_set
    changed: dict = {}
    if "title" in fields:
        t = (body.title or "").strip()
        if not t or len(t) > 120:
            raise HTTPException(status_code=400, detail="title must be 1..120 characters")
        table.title = t
        changed["title"] = t
    if "description" in fields:
        if body.description is not None and len(body.description) > 500:
            raise HTTPException(status_code=400, detail="description must be at most 500 characters")
        table.description = body.description or None
        changed["description"] = table.description
    if "allow_pin" in fields:
        table.allow_pin = bool(body.allow_pin)
        changed["allow_pin"] = table.allow_pin
    if "responsible_user_id" in fields or "responsible_person_id" in fields:
        resolved = _resolve_responsible(db, body.responsible_user_id,
                                        body.responsible_person_id)
        table.responsible_user_id = resolved
        changed["responsible_user_id"] = resolved

    table.updated_at = datetime.utcnow()
    refsvc.ensure_data_source(db, table)  # keeps the data_sources label in sync
    _log(db, user, "reference_update", "reference_table", slug, changed, _client_ip(request))
    db.commit()
    return _table_summary(db, table, None, user)


@router.delete("/api/reference/{slug}", status_code=204)
async def delete_reference_table(
    slug: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a reference table, its whole version history and the run-uses
    that point at it. Only a PERSON does this, and only with write rights on
    the table (`_can_write`): a generator that stops declaring a table never
    removes it — a dictionary outlives the report that introduced it, and the
    rows in it are somebody's work, not a build artefact.

    The `data_sources` row is kept: it is what lineage, Data Quality history
    and old audit lines refer to, and deleting it would silently rewrite the
    past. The UI asks for confirmation before calling this."""
    table = _get_table_or_404(db, slug)
    if not _can_write(db, user, table):
        raise HTTPException(status_code=403, detail="No write access")
    versions = db.query(ReferenceTableVersion).filter(
        ReferenceTableVersion.table_id == table.id).count()
    # `reference_table_versions.table_id` and `run_reference_uses.table_id` are
    # the only two columns pointing at `reference_tables.id`, both NOT NULL,
    # and both are cleared here — so the DELETE below stands under
    # foreign_keys=ON. Kept explicit rather than routed through
    # integrity.detach(): the version count above goes into the audit line.
    db.query(RunReferenceUse).filter(RunReferenceUse.table_id == table.id).delete(
        synchronize_session=False)
    db.query(ReferenceTableVersion).filter(
        ReferenceTableVersion.table_id == table.id).delete(synchronize_session=False)
    _log(db, user, "reference_delete", "reference_table", slug, {
        "title": table.title, "versions": versions,
        "consumers": [c["dashboard_id"] for c in _consumers(db, slug)],
    }, _client_ip(request))
    db.delete(table)
    db.commit()
    return Response(status_code=204)


@router.get("/api/reference/{slug}/versions")
async def list_reference_versions(
    slug: str, user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    table = _get_table_or_404(db, slug)
    if not _can_read(db, user, table):
        raise HTTPException(status_code=403, detail="Access denied")
    return {"versions": _versions_payload(db, table, limit=100)}


@router.get("/api/reference/{slug}/versions/{n}/keys")
async def get_reference_version_keys(
    slug: str, n: str, user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    table = _get_table_or_404(db, slug)
    if not _can_read(db, user, table):
        raise HTTPException(status_code=403, detail="Access denied")
    version_number = _resolve_version(table, n)
    rows = _rows_for_version(db, table, version_number)
    return {"version": version_number, "keys": refsvc.keys_norm(table.get_columns(), rows)}


@router.get("/api/reference/{slug}/versions/{n}/xlsx")
async def export_reference_version_xlsx(
    slug: str, n: str, user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    table = _get_table_or_404(db, slug)
    if not _can_read(db, user, table):
        raise HTTPException(status_code=403, detail="Access denied")
    version_number = _resolve_version(table, n)
    rows = _rows_for_version(db, table, version_number)
    content = await run_in_threadpool(_build_xlsx, table.get_columns(), rows)
    return _xlsx_response(content, f"{table.slug}-v{version_number}.xlsx")


@router.get("/api/reference/{slug}/template")
async def reference_template_xlsx(
    slug: str,
    keys: str | None = Query(default=None),
    unmapped: int = Query(default=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    table = _get_table_or_404(db, slug)
    if not _can_read(db, user, table):
        raise HTTPException(status_code=403, detail="Access denied")
    columns = table.get_columns()
    rows: list[list] = []
    if unmapped:
        # NEWLINE-separated, matching what quality._template_url builds: a
        # reference key may legitimately contain a comma ('Acme "Daisy" LLP, BV'),
        # so splitting on one turned every multi-key template into a SINGLE
        # bogus row holding the whole joined blob.
        key_list = [k.strip() for k in re.split(r"[\r\n]+", keys or "") if k.strip()]
        if len(key_list) > _MAX_TEMPLATE_KEYS:
            raise HTTPException(status_code=400, detail=f"at most {_MAX_TEMPLATE_KEYS} keys")
        for k in key_list:
            rows.append([k] + [None] * (len(columns) - 1))
    content = await run_in_threadpool(_build_xlsx, columns, rows)
    return _xlsx_response(content, f"{table.slug}-template.xlsx")


@router.post("/api/reference/{slug}/import")
async def import_reference_table(
    slug: str,
    request: Request,
    file: UploadFile = File(...),
    dry_run: str = Form("1"),
    expected_version: int = Form(...),
    delete_missing: str = Form("0"),
    note: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    table = _get_table_or_404(db, slug)
    if not _can_write(db, user, table):
        raise HTTPException(status_code=403, detail="No write access")

    content = await _read_capped_xlsx(file)
    columns = table.get_columns()

    raw_rows, header_errors = await run_in_threadpool(_parse_import_workbook, columns, content)
    if header_errors:
        raise HTTPException(status_code=400, detail={"errors": header_errors})

    coerced_rows: list[list] = []
    row_errors: list[dict] = []
    for i, raw in enumerate(raw_rows):
        row, errs = refsvc.coerce_row(columns, raw)
        coerced_rows.append(row)
        for e in errs:
            if len(row_errors) < _MAX_ROW_ERRORS:
                row_errors.append({"row": i, "column": e["column"], "reason": e["reason"]})

    old_rows = refsvc.current_rows(db, table)
    diff = refsvc.diff_rows(columns, old_rows, coerced_rows, delete_missing=(delete_missing == "1"))

    errors = (row_errors + diff["errors"])[:_MAX_ROW_ERRORS]
    is_dry = dry_run == "1"

    payload = {
        "dry_run": is_dry,
        "version": table.current_version,
        "added": diff["added"],
        "changed": diff["changed"],
        "removed": diff["removed"],
        "unchanged": diff["unchanged"],
        "errors": errors,
        "duplicates": diff["duplicates"],
        # A key that appears twice in the file blocks the commit as hard as a
        # typed row error does. `diff_rows` drops BOTH copies (an ambiguous
        # import must not silently pick "whichever came first"), so letting it
        # through would mean: with delete_missing=0 the row is silently NOT
        # updated, and with delete_missing=1 the existing row is silently
        # DELETED — data loss reported only as an advisory list.
        "can_commit": not errors and not diff["duplicates"],
    }

    if is_dry:
        return payload
    if not payload["can_commit"]:
        raise HTTPException(status_code=400, detail=payload)
    if expected_version != table.current_version:
        raise HTTPException(status_code=409, detail={"current": table.current_version})

    summary = {"added": diff["added"], "changed": diff["changed"], "removed": diff["removed"]}
    try:
        version = refsvc.create_version(db, table, diff["rows"], user.id, summary, note=note or None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _log(db, user, "reference_import", "reference_table", slug,
         {"version": version.version_number, **summary}, _client_ip(request))
    db.commit()

    payload["version"] = version.version_number
    return payload


@router.post("/api/reference/{slug}/versions/{n}/restore")
async def restore_reference_version(
    slug: str, n: str, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    table = _get_table_or_404(db, slug)
    if not _can_write(db, user, table):
        raise HTTPException(status_code=403, detail="No write access")
    version_number = _resolve_version(table, n)
    # v0 is not a version — it is "no import has happened yet", and
    # `_rows_for_version` answers [] for it without a 404. Restoring it would
    # therefore be a one-URL way to blank a populated table while looking like
    # an ordinary rollback.
    if version_number < 1:
        raise HTTPException(status_code=404, detail="Version not found")
    rows = _rows_for_version(db, table, version_number)
    summary = {"added": 0, "changed": 0, "removed": 0, "restored_from": version_number}
    try:
        version = refsvc.create_version(
            db, table, rows, user.id, summary, note=f"restore v{version_number}",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _log(db, user, "reference_restore", "reference_table", slug,
         {"from_version": version_number, "new_version": version.version_number},
         _client_ip(request))
    db.commit()
    return {"version": version.version_number, "row_count": version.row_count}


@router.get("/api/reference/{slug}/consumers")
async def get_reference_consumers(
    slug: str, user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    table = _get_table_or_404(db, slug)
    if not _can_read(db, user, table):
        raise HTTPException(status_code=403, detail="Access denied")
    return _consumers(db, table.slug)


# ---------------------------------------------------------------------------
# Pages (phase 6) — render like app/api/catalog.py:307 (`catalog_page`): the
# JSON the JSON API would return is baked into the page as `data-*` (list)
# or one `data_json` blob (detail) so the page costs exactly one request —
# static/js/reference.js reads it straight off the DOM, never re-fetching
# on load. No new query shapes here, only the existing helpers above.
# ---------------------------------------------------------------------------
@router.get("/reference", response_class=HTMLResponse)
async def reference_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    def _load():
        tables = db.query(ReferenceTable).order_by(ReferenceTable.title).all()
        memo: dict = {}   # see list_reference_tables — one sweep per request
        return [_table_summary(db, t, memo, user) for t in tables if _can_read(db, user, t, memo)]

    tables = await run_in_threadpool(_load)
    env = request.app.state.jinja
    html_out = await run_in_threadpool(
        env.get_template("pages/reference.html").render,
        user=user, request=request,
        can_create=_can_create(db, user),
        tables_json=json.dumps(tables, ensure_ascii=False),
    )
    return HTMLResponse(html_out)


@router.get("/reference/{slug}", response_class=HTMLResponse)
async def reference_detail_page(
    slug: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    table = _get_table_or_404(db, slug)
    if not _can_read(db, user, table):
        raise HTTPException(status_code=403, detail="Access denied")

    def _load():
        rows = refsvc.current_rows(db, table)
        return {
            "slug": table.slug,
            "title": table.title,
            "description": table.description,
            "key_column": table.key_column,
            "columns": table.get_columns(),
            "current_version": table.current_version,
            "allow_pin": table.allow_pin,
            "responsible": _responsible_json(db, table),
            "rows": rows[:500],
            "row_offset": 0,
            "row_limit": 500,
            "row_total": len(rows),
            "versions": _versions_payload(db, table, limit=20),
            "consumers": _consumers(db, table.slug),
            # Independent of dashboards: a generator that declares this table
            # but has no dashboard yet still has to show up in "Created from
            # manifest …" on an empty table.
            "consumer_generators": [
                {"id": gid, "name": name} for gid, (name, _v)
                in sorted(_consumer_generators(db, table.slug).items(),
                          key=lambda kv: kv[1][0] or "")],
            "created_at": table.created_at.isoformat() if table.created_at else None,
            "updated_at": table.updated_at.isoformat() if table.updated_at else None,
            "can_write": _can_write(db, user, table),
        }

    data = await run_in_threadpool(_load)
    env = request.app.state.jinja
    html_out = await run_in_threadpool(
        env.get_template("pages/reference_detail.html").render,
        user=user, request=request,
        table_title=table.title, table_slug=table.slug,
        data=data,
        data_json=json.dumps(data, ensure_ascii=False),
    )
    return HTMLResponse(html_out)
