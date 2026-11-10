"""
Report generators API (docs/RFC_GENERATORS_RESEARCH.md, Appendix E).

Three surfaces on ONE router:
  * code registration (admin-only, `manage_generators`) — bundle upload + git,
  * generator selection / create-flow (`upload_dashboard`),
  * per-dashboard engine + run lifecycle (`can_update_dashboard_data`).

Invariants that shape this module (not a line-by-line retelling):
  * A version directory is written to disk in full BEFORE the single db.commit()
    that makes it current — the same "either-whole-or-invisible" discipline as
    storage.save_html / replace_html. Readers only ever see current_version.
  * The git token is WRITE-ONLY. It is never returned by any endpoint, never
    stored in an error field, and never audited — only `token_set: bool` leaks.
    Every place git subprocess text could carry it is sanitized before it can
    reach an HTTP body, git_last_sync_error, or the audit log.
  * The run-lock has no sweeper thread: a `running` row older than the TTL is
    declared `lost` by the NEXT run attempt (read-path enforcement, same as
    session TTLs).
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime
from typing import Any

from fastapi import (
    APIRouter, Depends, File, HTTPException, Query, Request, UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from jinja2 import Environment
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.i18n import _
from app.auth.deps import (
    can_update_dashboard_data, get_current_user, get_dashboard_or_404,
    require_permission,
)
from app.config import settings
from app.database import get_db
from app.models import (
    Annotation, AuditLog, Dashboard, DashboardFile, DashboardPermission, DashboardVersion,
    DataSource, FILE_KINDS, Generator, GeneratorVersion, GenerationRun,
    InputOverride, ReferenceTable, ReferenceTableVersion, RunReferenceUse,
    SourceFile, SourceObservation, User,
    RUN_RUNNING, RUN_SUCCESS, RUN_ERROR, RUN_CANCELED, RUN_LOST,
    DASH_INTERACTIVE, DASH_STATIC, ROLE_ADMINISTRATOR,
)
from app.services import (
    datasources, detector, genmanifest, genstorage, globmatch, lake_slots, lineage,
    lineage_export, observations, quality, rls, schemadiff, semantics, storage, warm,
)
from app.services import reference as refsvc
from app.api.dashboards import _make_slug  # flat helper (unique slug + collision loop)

router = APIRouter()

# Report DATA keeps the existing 50 MB upload cap; report CODE gets its own
# (settings.generators_bundle_max_mb), applied in the registration pipeline.
_MAX_UPLOAD = 50 * 1024 * 1024  # 50 MB — publish-time html/slot files
_MAX_AUX = 10 * 1024 * 1024  # 10 MB — run-scoped auxiliary input files (aux_inputs)
_MAX_OBS_BODY = 2 * 1024 * 1024  # 2 MB — one observations batch (A3)
_MAX_OUTPUTS_JSON = 64 * 1024  # 64 KB — publish's write_back report (A4)
# A run may keep posting observations for this long after it closes (error or
# publish) — the engine's tail batch races the close and must not 409 just
# because it lost that race.
_OBSERVATIONS_GRACE_S = 60

# Monotonic `seq` per live run, in memory only: `generation_runs` has no seq
# column (adding one is out of scope), so a worker restart forgets the last
# seq it saw. That is fine — a forgotten run accepts the next seq it is told,
# same as a run that has never posted before; it is duplicate/lower `seq`
# WITHIN one process's memory of a run that gets rejected, which is the case
# that actually happens (a retried or out-of-order batch), not a process
# restart mid-run (rare, and the worst case is one accepted stale batch).
_last_seq: dict[int, int] = {}


# _log copied (not imported) per convention — dashboards.py keeps its own private
# copy too. Adds one AuditLog row to the session; the caller owns the commit.
def _log(db: Session, actor: User, action: str, resource_type: str,
         resource_id: str, detail: dict, ip: str = "") -> None:
    entry = AuditLog(
        actor_id=actor.id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        ip_address=ip,
    )
    entry.set_detail(detail)
    db.add(entry)


# ---------------------------------------------------------------------------
# JSON request bodies
# ---------------------------------------------------------------------------

class ArchiveBody(BaseModel):
    is_archived: bool


class GitRegisterBody(BaseModel):
    repo: str
    branch: str = "main"
    # None / absent = keep the stored token; "" = clear it; non-empty = set it.
    # The distinction is resolved in the handler (a missing key defaults to None
    # here, which we treat identically to an explicit null: leave the secret be).
    token: str | None = None


class GeneratedDashboardBody(BaseModel):
    title: str
    generator_id: int
    description: str | None = None


class RunStartBody(BaseModel):
    # "browser" | "agent" | "local". `local` is a native run by the desktop
    # agent over folders declared in folders.ini: the same R2 contract as the
    # browser engine, only with real Python instead of Pyodide. So it is
    # OBSERVABLE exactly like browser (it sends files/dirs/roots), and anything
    # in the code that tells "observed" from "unobserved" must put it next to
    # browser, not next to agent.
    engine: str
    input_manifest: dict | None = None


class RunErrorBody(BaseModel):
    message: str
    detail: str | None = None


# ---------------------------------------------------------------------------
# Shared version-registration pipeline (bundle + git)
# ---------------------------------------------------------------------------

async def _parse_and_validate_manifest(zip_bytes: bytes) -> dict:
    """zip bytes -> frozen, normalized manifest dict, or raises HTTPException.

    Steps (RFC E.1): size gate -> extract manifest.json -> json.loads ->
    genmanifest.validate -> normalize -> resolve deps against PyPI OFF the event
    loop -> browser_blockers. resolve_deps is the one network hop, hence the
    threadpool. Returns the manifest that gets frozen into the immutable version
    (deps carry their server-computed wasm32 status)."""
    if len(zip_bytes) > settings.generators_bundle_max_mb * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"Bundle too large (max {settings.generators_bundle_max_mb} MB)",
        )
    try:
        # Off the loop: inflating even a bounded manifest out of a hostile
        # archive is CPU the event loop must not spend — a registration that
        # stalls here would stall every other request in the process.
        manifest_bytes = await run_in_threadpool(genstorage.read_manifest_from_zip, zip_bytes)
    except genstorage.BundleError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        m = json.loads(manifest_bytes)
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid manifest JSON: {e}")
    if not isinstance(m, dict):
        raise HTTPException(status_code=400, detail="Manifest must be a JSON object")

    errors = genmanifest.validate(m)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    m = genmanifest.normalize(m)
    deps = await run_in_threadpool(genmanifest.resolve_deps, m["deps"])
    m["deps"] = deps

    blockers = genmanifest.browser_blockers(m)
    if blockers:
        raise HTTPException(status_code=422, detail={"errors": blockers})
    return m


async def _parse_semantics(zip_bytes: bytes, manifest_name: str) -> str | None:
    """Optional root semantics.json (ADR-13) -> frozen normalized JSON text, or
    None when the bundle has none. A present-but-broken file is a 400 with
    every problem listed: the author fixes it in one pass, the operator never
    meets a definition that cannot run."""
    try:
        raw = await run_in_threadpool(genstorage.read_semantics_from_zip, zip_bytes)
    except genstorage.BundleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if raw is None:
        return None
    try:
        sem = await run_in_threadpool(semantics.parse_bytes, raw, manifest_name)
    except semantics.SemanticsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return json.dumps(sem, ensure_ascii=False)


def _reference_declarations(manifest: dict, sem_json: str | None) -> dict[str, dict]:
    """Every reference table (ADR-16) this version declares, from BOTH places
    a version can declare one, as `{slug: {"columns": [{name, type}], "via":
    [...]}}` with the key column first.

    * the manifest's `source: "platform"` inputs — the report READS the
      dictionary as a generated workbook during a run (`platform_inputs`
      already normalises them, key first);
    * the frozen semantics' `sources: [{"reference": slug}]` — "Ask" (the
      chat) reads it. Its columns are a MAP, so the declared
      `business_key` is what puts the key first; a declaration that cannot
      produce a usable schema (no key, fewer than two columns) is dropped
      here and simply stays `unbound` in the chat, which is a better outcome
      than a table created with a shape nobody can import into.

    When both declare the SAME slug the result is their UNION, not whichever
    came first: the manifest fixes the order (its column order is the layout
    of the workbook the server writes for the run) and the semantics appends
    whatever it reads on top. Letting one side win would silently drop a
    column the other side asks for — and the chat would then answer NULL for
    it forever, with nothing anywhere saying why. A column both sides declare
    with DIFFERENT types is a real contradiction inside one bundle and is
    reported here, in `conflicts`, for the caller to turn into a 400.

    Returns `{slug: {"columns": [...], "via": [...], "conflicts": [str, ...]}}`.
    """
    out: dict[str, dict] = {}

    def _merge(slug: str, columns: list[dict], via: str) -> None:
        entry = out.setdefault(slug, {"columns": [], "via": [], "conflicts": []})
        entry["via"].append(via)
        by_lower = {c["name"].casefold(): c for c in entry["columns"]}
        for col in columns:
            seen = by_lower.get(col["name"].casefold())
            if seen is None:
                entry["columns"].append(dict(col))
                by_lower[col["name"].casefold()] = entry["columns"][-1]
            elif seen["type"] != col["type"]:
                entry["conflicts"].append(_(
                    "column «{name}»: type {a} in one declaration and {b} in the other (the manifest and semantics.json must match)",
                    name=col["name"], a=repr(seen["type"]), b=repr(col["type"])))

    for p_in in genmanifest.platform_inputs(manifest or {}):
        _merge(p_in["table"], p_in["columns"], "manifest")
    if sem_json:
        try:
            sem = json.loads(sem_json)
        except ValueError:            # pragma: no cover — we just wrote it
            sem = {}
        for spec in semantics.reference_sources(sem).values():
            key, cols = spec.get("key"), spec.get("columns") or {}
            if not key or key not in cols or len(cols) < 2:
                continue
            ordered = [{"name": key, "type": cols[key]}]
            ordered += [{"name": n, "type": t} for n, t in cols.items() if n != key]
            _merge(spec["slug"], ordered, "semantics")
    return out


def _check_reference_declarations(db: Session, manifest: dict,
                                  sem_json: str | None) -> dict[str, dict]:
    """Everything about the declared dictionaries that can REFUSE this bundle,
    and nothing that writes. Runs BEFORE `_write_version`, always.

    Order matters more than it looks: `_write_version` puts bundle.zip on disk
    under `v{n}`, and a 400 raised after that rolls the DB back while the
    directory stays — so the next upload picks the same `v{n}`, hits «versions
    are immutable» in `save_bundle`, and the generator is wedged until someone
    deletes the directory by hand. Everything that can say no therefore says
    it here.

    Two kinds of no: the bundle contradicting ITSELF (manifest and
    semantics.json giving one column two types) and the bundle contradicting a
    dictionary that already EXISTS (a changed type, a moved key). A column the
    table does not have yet is neither — it is added by
    `_ensure_reference_tables` after the version is safely written.

    Returns the merged declarations for that call, so the union is computed
    once per registration."""
    declared = _reference_declarations(manifest, sem_json)
    if not declared:
        return {}
    problems: list[str] = []
    for slug in sorted(declared):
        for c in declared[slug]["conflicts"]:
            problems.append(_("reference table {slug}: {problem}", slug=slug, problem=c))
    existing = {t.slug: t for t in db.query(ReferenceTable)
                .filter(ReferenceTable.slug.in_(sorted(declared))).all()}
    for slug, table in sorted(existing.items()):
        for problem in refsvc.schema_conflicts(table, declared[slug]["columns"]):
            problems.append(_("reference table {slug}: {problem}", slug=slug, problem=problem))
    if problems:
        raise HTTPException(
            status_code=400,
            detail=_("The reference table schema doesn't match the declaration: {problems}",
                     problems="; ".join(problems[:20])))
    return declared


def _ensure_reference_tables(db: Session, actor: User, generator: Generator,
                             declared: dict[str, dict]) -> list[str]:
    """Create, EMPTY, every reference table this version declares that nobody
    has created yet — the structure comes from the declaration, the rows come
    later from a person with an Excel file. `declared` is what
    `_check_reference_declarations` already validated; this function only
    writes, and runs AFTER the version exists.

    Why here and not on the Reference tables page: the manifest (or the
    semantics) is the only place that knows the schema, and until phase 2 of
    ADR-16 someone had to retype it by hand before the first run could read
    anything. Registering a generator is already the act of saying "this
    report reads sl_client_map with these columns"; the platform can just
    believe it. The right to register a generator (`manage_generators`) is
    strictly larger than the right to create a dictionary for it, so no
    second permission is checked — refusing here would only produce a report
    that cannot run until an admin repeats the same declaration by hand.

    A table that ALREADY exists keeps EVERYTHING it has — rows, versions,
    responsible user, `allow_pin`, its key and the types of its columns. The
    single change a registration may make to it is ADDITIVE: a column the new
    declaration carries and the table does not gets appended to the schema
    (ADR-16 addendum). Stored rows are not rewritten — `reference.fit_rows`
    pads them on read — so the export carries the new column immediately and
    no version is invalidated. A DROPPED column is not a change at all (a
    generator may stop reading a column the dictionary still keeps).

    `current_version` stays 0 until the first import, and Data Quality reports
    "reference table is empty" rather than counting every key of the run as a miss.
    Returns the slugs actually created."""
    if not declared:
        return []
    existing = {t.slug: t for t in db.query(ReferenceTable)
                .filter(ReferenceTable.slug.in_(sorted(declared))).all()}
    for slug, table in sorted(existing.items()):
        try:
            added = refsvc.add_columns(db, table, declared[slug]["columns"])
        except ValueError as e:
            # The merged declaration cannot be held as a schema (too many
            # columns, a name the reference layer rejects). Silence here would
            # leave a dictionary the report cannot fully read and nothing
            # anywhere saying so.
            raise HTTPException(
                status_code=400,
                detail=_("Reference table {p1}: declared columns cannot be added to schema – {p2}", p1=slug, p2=e))
        if added:
            _log(db, actor, "reference_update", "reference_table", slug, {
                "columns_added": added, "auto": True, "generator": generator.name,
            })
    created: list[str] = []
    for slug in sorted(declared):
        if slug in existing:
            continue
        try:
            columns = refsvc.validate_schema(declared[slug]["columns"])
        except ValueError:
            # A declaration the reference layer cannot hold (a numeric key, a
            # single column). The chat/run says "reference table not created", which
            # is honest; inventing a repaired schema would not be.
            continue
        table = ReferenceTable(
            slug=slug, title=slug,
            description=f"Created automatically when the generator \"{generator.name}\" was registered",
            key_column=columns[0]["name"], kind="simple",
            allow_pin=False, responsible_user_id=None,
            current_version=0, created_by=actor.id,
        )
        table.set_columns(columns)
        db.add(table)
        try:
            # Two registrations racing on one slug: UNIQUE(slug) is the
            # arbiter, and the loser simply adopts what the winner made
            # (SAVEPOINT so the rest of this transaction survives).
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            db.expunge(table)
            found = refsvc.get_table(db, slug)
            if found is None:        # pragma: no cover — the UNIQUE said it exists
                continue
            try:
                added = refsvc.add_columns(db, found, declared[slug]["columns"])
            except ValueError:
                added = []
            if added:
                _log(db, actor, "reference_update", "reference_table", slug, {
                    "columns_added": added, "auto": True, "generator": generator.name,
                })
            continue
        refsvc.ensure_data_source(db, table)
        # Same action name the Reference tables page writes, so one audit filter
        # answers "where did this table come from" whichever way it was made.
        _log(db, actor, "reference_create", "reference_table", slug, {
            "title": slug, "columns": [c["name"] for c in columns],
            "auto": True, "generator": generator.name,
            "via": sorted(set(declared[slug]["via"])),
        })
        created.append(slug)
    return created


def _check_semantics_references(db: Session, sem_json: str | None) -> list[str]:
    """Semantics tables backed by a platform reference table (ADR-16) vs the
    dictionaries that exist RIGHT NOW. Same rule the manifest's platform
    inputs live by: a slug nobody has created yet registers fine (the binding
    appears the day the table is created), a slug that DOES exist must line up
    column-for-column or this is a 400 naming every difference — the author
    fixes the declaration once instead of shipping a report whose "Ask"
    answers NULL for every row.

    Returns the slugs it checked, for the audit line of the version write."""
    if not sem_json:
        return []
    try:
        sem = json.loads(sem_json)
    except ValueError:               # pragma: no cover — we just wrote it
        return []
    refs = semantics.reference_sources(sem)
    if not refs:
        return []
    slugs = sorted({r["slug"] for r in refs.values()})
    schemas = {
        t.slug: {"key_column": t.key_column,
                 "columns": {c.get("name"): c.get("type") for c in (t.get_columns() or [])}}
        for t in db.query(ReferenceTable).filter(ReferenceTable.slug.in_(slugs)).all()
    }
    errors = semantics.reference_schema_errors(sem, schemas)
    if errors:
        raise HTTPException(status_code=400,
                            detail="semantics.json: " + "; ".join(errors[:20])
                            + (f" (+{len(errors) - 20} more)" if len(errors) > 20 else ""))
    return slugs


def _write_version(
    db: Session, actor: User, generator: Generator, version_number: int,
    zip_bytes: bytes, m: dict, source_type: str, git_commit_sha: str | None,
    semantics_json: str | None = None,
) -> GeneratorVersion:
    """Persist the bundle to disk and add an immutable GeneratorVersion row with
    is_current flipped to it. Does NOT commit — the caller owns the single commit
    point. `generator` must already be persistent (id assigned). genstorage
    raises BundleError if the version dir already exists."""
    manifest_bytes = json.dumps(m, ensure_ascii=False).encode("utf-8")
    _rel_path, checksum = genstorage.save_bundle(
        generator.id, version_number, zip_bytes, manifest_bytes,
    )
    # Immutable versions: only the is_current pointer moves. Demote the old one
    # in the same transaction as the insert below.
    db.query(GeneratorVersion).filter(
        GeneratorVersion.generator_id == generator.id,
        GeneratorVersion.is_current == True,  # noqa: E712
    ).update({GeneratorVersion.is_current: False})

    ver = GeneratorVersion(
        generator_id=generator.id,
        version_number=version_number,
        source_type=source_type,
        bundle_path=_rel_path,
        manifest_json=json.dumps(m, ensure_ascii=False),
        semantics_json=semantics_json,
        git_commit_sha=git_commit_sha,
        is_current=True,
        checksum_sha256=checksum,
        created_by=actor.id,
    )
    db.add(ver)
    return ver


def _next_version_number(db: Session, generator_id: int) -> int:
    return (db.query(func.max(GeneratorVersion.version_number))
            .filter(GeneratorVersion.generator_id == generator_id)
            .scalar() or 0) + 1


# ---------------------------------------------------------------------------
# Code registration — admin only (manage_generators)
# ---------------------------------------------------------------------------

@router.post("/api/admin/generators")
async def create_generator(
    bundle: UploadFile = File(...),
    user: User = Depends(require_permission("manage_generators")),
    db: Session = Depends(get_db),
):
    """Register a brand-new generator from a bundle. The name is taken from the
    manifest (no Form field) — the manifest is the single source of truth, so a
    bundle cannot be filed under a name that contradicts its own code."""
    zip_bytes = await bundle.read()
    m = await _parse_and_validate_manifest(zip_bytes)

    name = m.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Manifest has no name")
    existing = db.query(Generator.id).filter(Generator.name == name).first()
    if existing:
        # The refusal MUST say where the bundle goes. This used to be a plain
        # "already exists", which sent the admin hunting for the right row in
        # the table by hand, while the hint under the error advised "fix the bundle"
        # although there is nothing to fix: it is the same version of the same
        # generator, just a different endpoint (owner, 05.10: "why make me search for it").
        raise HTTPException(status_code=409, detail={
            "error": "generator_exists",
            "name": name,
            "generator_id": int(existing[0]),
            "message": _("Generator «{name}» is already registered", name=name),
        })

    # Before the INSERT: the semantics dry run must not run while this
    # session holds SQLite's write lock.
    sem_json = await _parse_semantics(zip_bytes, name)
    # Everything that can REFUSE the bundle runs before a byte reaches the
    # disk — a 400 after `save_bundle` leaves an orphan `v{n}` directory that
    # wedges every later upload («versions are immutable»).
    ref_slugs = _check_semantics_references(db, sem_json)
    declared_refs = _check_reference_declarations(db, m, sem_json)
    generator = Generator(name=name, created_by=user.id)
    db.add(generator)
    db.flush()  # assign generator.id before the bundle path is built

    try:
        ver = _write_version(db, user, generator, 1, zip_bytes, m, "bundle", None, sem_json)
    except genstorage.BundleError as e:
        raise HTTPException(status_code=400, detail=str(e))

    checksum = ver.checksum_sha256
    # After the version exists: a dictionary the manifest/semantics declare
    # but nobody created yet is created here, empty (ADR-16). Anything that
    # still goes wrong takes the bundle directory with it.
    try:
        made = _ensure_reference_tables(db, user, generator, declared_refs)
    except Exception:
        genstorage.delete_version_dir(generator.id, 1)
        raise
    _log(db, user, "create_generator", "generator", str(generator.id), {
        "name": name, "version_number": 1,
        "checksum": checksum, "source_type": "bundle",
        # Which platform dictionaries this version's semantics will read in
        # "Ask" — the same line already carries the bundle's
        # identity, and this is the one place the link is decided.
        **({"reference_tables": ref_slugs} if ref_slugs else {}),
        **({"reference_tables_created": made} if made else {}),
    })
    db.commit()
    return JSONResponse(
        {"id": generator.id, "name": generator.name, "version_number": 1},
        status_code=201,
    )


@router.post("/api/admin/generators/{generator_id}/versions")
async def create_generator_version(
    generator_id: int,
    bundle: UploadFile = File(...),
    user: User = Depends(require_permission("manage_generators")),
    db: Session = Depends(get_db),
):
    """Register a new immutable version of an existing generator."""
    generator = db.query(Generator).filter(Generator.id == generator_id).first()
    if not generator:
        raise HTTPException(status_code=404, detail="Generator not found")

    zip_bytes = await bundle.read()
    m = await _parse_and_validate_manifest(zip_bytes)
    # Guard against filing a foreign bundle under this generator.
    if m.get("name") != generator.name:
        raise HTTPException(
            status_code=400,
            detail=f"Bundle manifest name '{m.get('name')}' does not match generator '{generator.name}'",
        )

    sem_json = await _parse_semantics(zip_bytes, generator.name)
    # Refusals first, writes second — see create_generator above.
    ref_slugs = _check_semantics_references(db, sem_json)
    declared_refs = _check_reference_declarations(db, m, sem_json)
    version_number = _next_version_number(db, generator.id)
    try:
        ver = _write_version(db, user, generator, version_number, zip_bytes, m, "bundle", None, sem_json)
    except genstorage.BundleError as e:
        raise HTTPException(status_code=400, detail=str(e))

    checksum = ver.checksum_sha256
    try:
        made = _ensure_reference_tables(db, user, generator, declared_refs)
    except Exception:
        genstorage.delete_version_dir(generator.id, version_number)
        raise
    _log(db, user, "generator_new_version", "generator", str(generator.id), {
        "name": generator.name, "version_number": version_number,
        "checksum": checksum, "source_type": "bundle",
        **({"reference_tables": ref_slugs} if ref_slugs else {}),
        **({"reference_tables_created": made} if made else {}),
    })
    db.commit()
    # After the commit: the new version is durably current, so bundles nobody
    # can roll back to anymore (older than current + previous) leave the disk.
    pruned = genstorage.prune_version_dirs(generator.id)
    if pruned:
        _log(db, user, "generator_versions_pruned", "generator",
             str(generator.id), {"pruned": pruned})
        db.commit()
    return {"version_number": version_number, "checksum_sha256": checksum}


@router.patch("/api/admin/generators/{generator_id}")
async def update_generator(
    generator_id: int,
    body: ArchiveBody,
    user: User = Depends(require_permission("manage_generators")),
    db: Session = Depends(get_db),
):
    """Archive / unarchive. Hard-delete is intentionally NOT offered: versions
    are immutable provenance and dashboards may still reference them."""
    generator = db.query(Generator).filter(Generator.id == generator_id).first()
    if not generator:
        raise HTTPException(status_code=404, detail="Generator not found")

    generator.is_archived = body.is_archived
    _log(db, user, "generator_archive", "generator", str(generator.id),
         {"is_archived": body.is_archived})
    db.commit()
    return {"ok": True, "is_archived": generator.is_archived}


@router.get("/api/admin/generators")
async def list_generators(
    user: User = Depends(require_permission("manage_generators")),
    db: Session = Depends(get_db),
):
    """Full admin listing. The git token is never serialized — only token_set."""
    generators = (db.query(Generator)
                  .options(joinedload(Generator.versions))
                  .order_by(Generator.name)
                  .all())
    out = []
    for g in generators:
        versions = sorted(g.versions, key=lambda v: v.version_number, reverse=True)
        current = next((v for v in versions if v.is_current), None)
        out.append({
            "id": g.id,
            "name": g.name,
            "is_archived": g.is_archived,
            "created_at": g.created_at.isoformat() if g.created_at else None,
            "git": {
                "repo": g.git_repo,
                "branch": g.git_branch,
                "token_set": bool(g.git_token),  # value never exposed
                "last_synced_sha": g.git_last_synced_sha,
                "last_synced_at": g.git_last_synced_at.isoformat() if g.git_last_synced_at else None,
                "last_error": g.git_last_sync_error,
            },
            "versions": [{
                "version_number": v.version_number,
                "source_type": v.source_type,
                "git_commit_sha": v.git_commit_sha,
                "checksum_sha256": v.checksum_sha256,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "is_current": v.is_current,
            } for v in versions],
            "current_version_number": current.version_number if current else None,
        })
    return out


@router.put("/api/admin/generators/{generator_id}/git")
async def register_generator_git(
    generator_id: int,
    body: GitRegisterBody,
    user: User = Depends(require_permission("manage_generators")),
    db: Session = Depends(get_db),
):
    """Attach / update a git source. The token is write-only: stored, but the
    audit detail carries only token_set, never the value."""
    if not settings.generators_git_mode:
        raise HTTPException(
            status_code=503,
            detail="Git mode is disabled — set OP_GENERATORS_GIT_MODE=true after IT-5 confirms GitHub reachability from the server",
        )
    generator = db.query(Generator).filter(Generator.id == generator_id).first()
    if not generator:
        raise HTTPException(status_code=404, detail="Generator not found")

    repo = body.repo.strip()
    if not repo.startswith("https://"):
        raise HTTPException(status_code=400, detail="repo must be an https:// URL")

    generator.git_repo = repo
    generator.git_branch = body.branch.strip() or "main"
    # None (or absent, which defaults to None) keeps the stored secret; a real
    # string sets it; "" clears it (empty string -> None).
    if body.token is not None:
        generator.git_token = body.token or None

    _log(db, user, "generator_git_register", "generator", str(generator.id), {
        "repo": generator.git_repo,
        "branch": generator.git_branch,
        "token_set": bool(generator.git_token),  # never the token itself
    })
    db.commit()
    return {
        "ok": True,
        "repo": generator.git_repo,
        "branch": generator.git_branch,
        "token_set": bool(generator.git_token),
    }


class _GitSyncError(Exception):
    """Raised inside the clone worker with an ALREADY-SANITIZED message (any git
    token has been replaced with ***). Degrades to 424 = keep last snapshot."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _git_clone_snapshot(repo: str, branch: str, token: str | None, prev_sha: str | None):
    """Shallow-clone the repo into a temp dir and return one of:
      ("unchanged", sha, None)          — HEAD already synced,
      ("ok", sha, zip_bytes)            — new snapshot to register.
    Runs in the threadpool (subprocess + disk). Every error string is sanitized
    against the token BEFORE it leaves this function, so the secret can never
    reach an HTTP body, the error column, or the audit log."""
    def sanitize(text: str) -> str:
        return text.replace(token, "***") if token else text

    with tempfile.TemporaryDirectory() as tmp:
        if token:
            # https://oauth2:{token}@host/path — credentials in the URL, never logged.
            url = "https://oauth2:" + token + "@" + repo[len("https://"):]
        else:
            url = repo
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never block on a prompt

        try:
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", branch, url, tmp],
                capture_output=True, text=True, timeout=180, env=env,
            )
        except subprocess.TimeoutExpired:
            raise _GitSyncError("git clone timed out after 180s")
        except OSError as e:
            raise _GitSyncError(sanitize(f"git unavailable: {e}"))
        if proc.returncode != 0:
            raise _GitSyncError(sanitize((proc.stderr or proc.stdout or "git clone failed").strip()))

        try:
            rev = subprocess.run(
                ["git", "-C", tmp, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=30, env=env,
            )
        except subprocess.TimeoutExpired:
            raise _GitSyncError("git rev-parse timed out")
        if rev.returncode != 0:
            raise _GitSyncError(sanitize((rev.stderr or "git rev-parse failed").strip()))
        sha = rev.stdout.strip()

        if sha == prev_sha:
            return ("unchanged", sha, None)

        if not os.path.isfile(os.path.join(tmp, "manifest.json")):
            raise _GitSyncError(f"manifest.json not found at repo root (commit {sha[:12]})")
        zip_bytes = genstorage.make_zip_from_dir(tmp)
        return ("ok", sha, zip_bytes)


@router.post("/api/admin/generators/{generator_id}/git/sync")
async def sync_generator_git(
    generator_id: int,
    user: User = Depends(require_permission("manage_generators")),
    db: Session = Depends(get_db),
):
    """Pull HEAD of the registered branch and register it as a new version.
    Unreachable repo / bad manifest / validation failure -> 424 and the pointer
    stays on the last good snapshot vN (RFC E.3 degradation)."""
    if not settings.generators_git_mode:
        raise HTTPException(
            status_code=503,
            detail="Git mode is disabled — set OP_GENERATORS_GIT_MODE=true after IT-5 confirms GitHub reachability from the server",
        )
    generator = db.query(Generator).filter(Generator.id == generator_id).first()
    if not generator:
        raise HTTPException(status_code=404, detail="Generator not found")
    if not generator.git_repo:
        raise HTTPException(status_code=400, detail="No git repository registered for this generator")

    token = generator.git_token  # captured for sanitizing manifest-side errors too
    branch = generator.git_branch or "main"

    def sanitize(text: str) -> str:
        return text.replace(token, "***") if token else text

    try:
        outcome = await run_in_threadpool(
            _git_clone_snapshot, generator.git_repo, branch, token, generator.git_last_synced_sha,
        )
    except _GitSyncError as e:
        # Clone/network failure: message is already sanitized. Persist it and 424.
        generator.git_last_sync_error = e.message
        generator.git_last_synced_at = datetime.utcnow()
        _log(db, user, "generator_git_sync", "generator", str(generator.id), {"error": e.message})
        db.commit()
        raise HTTPException(status_code=424, detail=e.message)

    state, sha = outcome[0], outcome[1]
    if state == "unchanged":
        # Pointer does not move; only the freshness timestamp is refreshed.
        generator.git_last_synced_at = datetime.utcnow()
        generator.git_last_sync_error = None
        _log(db, user, "generator_git_sync", "generator", str(generator.id),
             {"sha": sha, "unchanged": True})
        db.commit()
        return {"ok": True, "unchanged": True, "sha": sha}

    zip_bytes = outcome[2]
    try:
        m = await _parse_and_validate_manifest(zip_bytes)
        if m.get("name") != generator.name:
            raise HTTPException(
                status_code=400,
                detail=f"manifest name '{m.get('name')}' does not match generator '{generator.name}'",
            )
        sem_json = await _parse_semantics(zip_bytes, generator.name)
        # Inside the try, and BEFORE the version is written: these only read.
        # A schema conflict here becomes the sync ERROR below — which COMMITS,
        # so nothing that writes may run inside this block, and nothing that
        # can refuse may run after `_write_version` (the bundle directory
        # would outlive the rollback and wedge the next sync).
        ref_slugs = _check_semantics_references(db, sem_json)
        declared_refs = _check_reference_declarations(db, m, sem_json)
        version_number = _next_version_number(db, generator.id)
        ver = _write_version(db, user, generator, version_number, zip_bytes, m, "git", sha, sem_json)
    except (HTTPException, genstorage.BundleError) as e:
        # Bad clone content: degrade like a network failure. No version pointer
        # moved (the failure is at/before save_bundle), so vN stays current.
        detail = e.detail if isinstance(e, HTTPException) else str(e)
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        msg = f"{sanitize(text)} (commit {sha[:12]})"
        generator.git_last_sync_error = msg
        generator.git_last_synced_at = datetime.utcnow()
        _log(db, user, "generator_git_sync", "generator", str(generator.id), {"error": msg})
        db.commit()
        raise HTTPException(status_code=424, detail=msg)

    checksum = ver.checksum_sha256
    # Outside the try above on purpose: that block COMMITS its error, and a
    # dictionary created inside it would survive a failed sync.
    try:
        made = _ensure_reference_tables(db, user, generator, declared_refs)
    except Exception:
        genstorage.delete_version_dir(generator.id, version_number)
        raise
    generator.git_last_synced_sha = sha
    generator.git_last_synced_at = datetime.utcnow()
    generator.git_last_sync_error = None
    _log(db, user, "generator_git_sync", "generator", str(generator.id),
         {"sha": sha, "version_number": version_number,
          **({"reference_tables": ref_slugs} if ref_slugs else {}),
          **({"reference_tables_created": made} if made else {})})
    db.commit()
    pruned = genstorage.prune_version_dirs(generator.id)
    if pruned:
        _log(db, user, "generator_versions_pruned", "generator",
             str(generator.id), {"pruned": pruned})
        db.commit()
    return {
        "ok": True, "unchanged": False, "sha": sha,
        "version_number": version_number, "checksum_sha256": checksum,
    }


# ---------------------------------------------------------------------------
# Lightweight listing — selection + provenance (upload_dashboard)
# ---------------------------------------------------------------------------

@router.get("/api/generators")
async def generators_for_create(
    user: User = Depends(require_permission("upload_dashboard")),
    db: Session = Depends(get_db),
):
    """Non-archived generators that have a current version, for the create-flow
    picker and the provenance line 'slreport v3 · admin1 · date' (R7). One
    query, no N+1 — current version + its author joined in."""
    rows = (db.query(Generator, GeneratorVersion, User.person_id)
            .join(GeneratorVersion,
                  (GeneratorVersion.generator_id == Generator.id)
                  & (GeneratorVersion.is_current == True))  # noqa: E712
            .outerjoin(User, User.id == GeneratorVersion.created_by)
            .filter(Generator.is_archived == False)  # noqa: E712
            .order_by(Generator.name)
            .all())
    out = []
    for g, ver, author in rows:
        manifest = ver.get_manifest()
        out.append({
            "id": g.id,
            "name": g.name,
            "version_number": ver.version_number,
            "version_label": manifest.get("version"),
            "engine": manifest.get("engine"),
            "created_at": ver.created_at.isoformat() if ver.created_at else None,
            "author": author,  # person_id of the current version's creator, or None
        })
    return out


# ---------------------------------------------------------------------------
# Create-flow — draft dashboard backed by a generator (upload_dashboard)
# ---------------------------------------------------------------------------

# v1 content of a generator-backed dashboard: a self-contained platform CTA, not
# a render-time branch (RFC E.2). Dark, matches the app's 401 page palette
# (bg #0d1117, text #8b949e, accent #ffb000). The audience is Russian-speaking;
# the platform UI is English — both lines are shown on purpose.
_PLACEHOLDER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__REPORT_TITLE__</title>
<style>
  html, body { margin: 0; height: 100%; }
  body { background: #0d1117; color: #8b949e;
    font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; align-items: center; justify-content: center; text-align: center; }
  .box { max-width: 34rem; padding: 2rem; }
  h1 { color: #e6edf3; font-size: 1.4rem; margin: 0 0 1.1rem; }
  p { margin: .4rem 0; }
  .cta { color: #ffb000; }
</style>
</head>
<body>
  <div class="box">
    <h1>__REPORT_TITLE__</h1>
    <p class="cta">Report not generated yet — open the Data panel and run the generator.</p>
  </div>
</body>
</html>
"""


def _placeholder_html(title: str) -> str:
    # textContent-equivalent safety: the title is HTML-escaped before it lands
    # in <title> and <h1>, so an odd report name can't inject markup.
    return _PLACEHOLDER_HTML.replace("__REPORT_TITLE__", html.escape(title))


@router.post("/api/dashboards/generated")
async def create_generated_dashboard(
    body: GeneratedDashboardBody,
    user: User = Depends(require_permission("upload_dashboard")),
    db: Session = Depends(get_db),
):
    """Create a DRAFT generator-backed dashboard whose v1 is the CTA placeholder.
    is_published stays False (that IS the draft, RFC E.2); the first successful
    run flips it to published."""
    title = body.title.strip()
    if not title or len(title) > 512:
        raise HTTPException(status_code=400, detail="title must be 1..512 characters")

    generator = db.query(Generator).filter(Generator.id == body.generator_id).first()
    if not generator:
        raise HTTPException(status_code=404, detail="Generator not found")
    if generator.is_archived:
        raise HTTPException(status_code=400, detail="Generator is archived")
    cur = db.query(GeneratorVersion).filter(
        GeneratorVersion.generator_id == generator.id,
        GeneratorVersion.is_current == True,  # noqa: E712
    ).first()
    if cur is None:
        raise HTTPException(status_code=400, detail="Generator has no current version")

    dash_id = str(uuid.uuid4())
    slug = _make_slug(title, db)
    version = 1

    html_bytes = _placeholder_html(title).encode("utf-8")
    storage.init_dashboard(dash_id)
    html_path = storage.save_html(dash_id, version, html_bytes)

    dash = Dashboard(
        id=dash_id, slug=slug, title=title,
        description=body.description or None,
        dash_type=DASH_STATIC, owner_id=user.id,
        generator_id=generator.id, current_version=version,
        # is_published left at its False default = draft.
    )
    db.add(dash)
    db.add(DashboardVersion(
        dashboard_id=dash_id, version_number=version,
        html_path=html_path, created_by=user.id,
        note="generator draft placeholder",
    ))
    dash.render_size = storage.estimate_render_size(dash_id, version)

    _log(db, user, "create_generated_dashboard", "dashboard", dash_id, {
        "title": title, "generator_id": generator.id, "generator_name": generator.name,
    })
    db.commit()
    return JSONResponse(
        {"id": dash_id, "url": f"/d/{dash_id}?dock=data"},
        status_code=201,
    )


# ---------------------------------------------------------------------------
# Engine-facing: manifest + bundle (can_update_dashboard_data)
# ---------------------------------------------------------------------------

def _assert_can_update_data(dashboard: Dashboard, user: User, db: Session) -> None:
    # Same authz as update_slot_file: can_update_dashboard_data called as a plain
    # function (owner, admin, or a can_update_data grant).
    if not can_update_dashboard_data(dashboard=dashboard, user=user, db=db):
        raise HTTPException(status_code=403, detail="No permission to run the generator")
    # ADR-18: for a report with a policy, "can update data" alone does NOT grant
    # full access (the same rule as files._assert_rls_full_access). The whole
    # generator surface works with the UNFILTERED source of exactly the rows the
    # filter hides: a run's aux file is the whole original xlsx/csv, get_run
    # returns input_manifest and error_detail (paths and lists of source
    # files), and starting/publishing a run rewrites the slots. Hence only a
    # subject with see_all. A plain report (rls_json NULL) pays no query.
    if dashboard.rls_enabled and not rls.subject(db, dashboard, user).see_all:
        raise HTTPException(status_code=403,
                            detail="RLS: this report is filtered per viewer")


def _current_generator_version(db: Session, dashboard: Dashboard) -> GeneratorVersion | None:
    if not dashboard.generator_id:
        return None
    return db.query(GeneratorVersion).filter(
        GeneratorVersion.generator_id == dashboard.generator_id,
        GeneratorVersion.is_current == True,  # noqa: E712
    ).first()


def _parse_input_manifest(raw: str | None) -> dict:
    """run.input_manifest is a free-form JSON Text column (engine-provided
    file listing, plus our own "aux_files" key). Tolerant parse: missing or
    invalid content is never a request-time error, it degrades to {} — the
    column predates aux_inputs and may hold anything the engine wrote."""
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _run_scenario(run: GenerationRun) -> str | None:
    """The run's folder scenario name, or None. The start manifest is stored
    as a string and comes from OUTSIDE — broken JSON must not break a publish,
    so parsing is silent."""
    if not run.input_manifest:
        return None
    try:
        data = json.loads(run.input_manifest)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("scenario")
    return name.strip()[:64] if isinstance(name, str) and name.strip() else None


def _run_offline_at(run: GenerationRun) -> str | None:
    """Date of an offline run (`YYYY-MM-DD`), or None for an ordinary run.

    Catch-up (phase 7 of the desktop mirror) uploads numbers computed
    EARLIER and offline through the same `POST …/runs` — so the version's
    `started_at` is the upload time, not the computation time. The only honest
    answer is to name both dates in the version note: otherwise, six months
    later, the report looks as if it was computed on the day it was published.

    Parsing is silent for the same reason as in `_run_scenario`: the manifest
    comes from outside, and broken JSON must not break a publish.
    """
    data = _parse_input_manifest(run.input_manifest)
    off = data.get("offline")
    if not isinstance(off, dict):
        return None
    at = off.get("created_at")
    if not isinstance(at, str) or not at.strip():
        # The mark is there but the date is not: offline must be stated, but
        # there is nothing to say about "when", and inventing "today" would be
        # exactly the lie this field exists to prevent.
        return ""
    return at.strip()[:10]


def _aux_files_of(run: GenerationRun) -> list[str]:
    raw = run.input_manifest
    # Substring prefilter before the parse: a 100-file listing costs ~20 µs to
    # json.loads vs ~2.6 µs to scan (measured), and most history rows have no
    # aux files. A false positive (a data file literally named aux_files) just
    # falls through to the parse and still returns the right answer.
    if not raw or '"aux_files"' not in raw:
        return []
    files = _parse_input_manifest(raw).get("aux_files")
    return list(files) if isinstance(files, list) else []


@router.get("/api/dashboards/{dashboard_id}/generator/semantics")
async def get_generator_semantics(
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The frozen semantics.json of the dashboard's current generator version
    (ADR-13), for the browser engine and the question layer. Same reach as
    the manifest: anyone who may run the generator may read what it means.
    404 when the version ships no semantics — callers fall back to the bare
    catalog schema."""
    _assert_can_update_data(dashboard, user, db)
    if not dashboard.generator_id:
        raise HTTPException(status_code=404, detail="Dashboard has no generator")
    cur = _current_generator_version(db, dashboard)
    if cur is None or not cur.semantics_json:
        raise HTTPException(status_code=404, detail="Generator version has no semantics")
    return JSONResponse(content=json.loads(cur.semantics_json))


def _reference_payload(db: Session, manifest: dict) -> list[dict]:
    """The reference tables (ADR-16) this generator declares as platform
    inputs, with what the run panel needs to draw its checkboxes: does the
    table exist yet, what is current, may a run pin an older version, who
    answers for it, and the last 10 versions newest-first.

    TWO queries whatever the manifest declares — one for the tables (with the
    responsible user joined in), one for their versions, sliced per table in
    Python. This endpoint is hit on every viewer open by anyone who may run
    the generator, so a per-table loop of queries here would be a per-open
    cost; ten versions is a ceiling the picker can show, not a page.

    A declared table that nobody has created yet is not an error — it comes
    back `exists: false` and the engine shows "reference table not created yet"
    instead of a checkbox.
    """
    plat = genmanifest.platform_inputs(manifest)
    slugs = [p["table"] for p in plat]
    if not slugs:
        return []
    rows = (db.query(ReferenceTable, User.display_name, User.person_id)
            .outerjoin(User, User.id == ReferenceTable.responsible_user_id)
            .filter(ReferenceTable.slug.in_(slugs))
            .all())
    by_slug = {t.slug: (t, dn, ovid) for (t, dn, ovid) in rows}
    versions: dict[int, list[dict]] = {}
    if by_slug:
        vrows = (db.query(ReferenceTableVersion.table_id,
                          ReferenceTableVersion.version_number,
                          ReferenceTableVersion.created_at,
                          ReferenceTableVersion.row_count)
                 .filter(ReferenceTableVersion.table_id.in_(
                     [t.id for (t, _dn, _ov) in by_slug.values()]))
                 .order_by(ReferenceTableVersion.table_id,
                           ReferenceTableVersion.version_number.desc())
                 .all())
        for tid, number, created_at, row_count in vrows:
            lst = versions.setdefault(tid, [])
            if len(lst) < 10:   # newest first, already ordered by the query
                lst.append({
                    "n": number,
                    "created_at": created_at.isoformat() if created_at else None,
                    "row_count": row_count,
                })
    out: list[dict] = []
    for entry in plat:
        slug = entry["table"]
        found = by_slug.get(slug)
        if found is None:
            out.append({
                "slug": slug, "title": slug, "exists": False,
                "current_version": 0, "allow_pin": False,
                "responsible": None, "versions": [],
            })
            continue
        table, display_name, person_id = found
        out.append({
            "slug": table.slug,
            "title": table.title,
            "exists": True,
            "current_version": table.current_version,
            "allow_pin": bool(table.allow_pin),
            "responsible": (display_name or person_id) if person_id else None,
            "versions": versions.get(table.id, []),
        })
    return out


@router.get("/api/dashboards/{dashboard_id}/generator/manifest")
async def get_generator_manifest(
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_can_update_data(dashboard, user, db)
    if not dashboard.generator_id:
        raise HTTPException(status_code=404, detail="Dashboard has no generator")
    generator = db.query(Generator).filter(Generator.id == dashboard.generator_id).first()
    cur = _current_generator_version(db, dashboard)
    if not generator or cur is None:
        raise HTTPException(status_code=404, detail="Generator has no current version")
    manifest = cur.get_manifest()

    # Exclusion rules keyed by folder basename, scoped to THIS generator and to
    # sources THIS dashboard's own runs have actually observed — the v1 bug
    # this replaces handed every rule in the registry to every generator, so a
    # rule set for report A leaked into report B's engine the moment both
    # touched a folder with the same basename. Deliberately NOT part of the
    # frozen manifest: the manifest is what the AUTHOR declared and is
    # immutable per version, while "this file in this folder is junk" is an
    # operational decision the report's own people take and revise.
    own_source_ids = (
        db.query(SourceObservation.data_source_id)
        .join(GenerationRun, GenerationRun.id == SourceObservation.run_id)
        .filter(GenerationRun.dashboard_id == dashboard.id)
        .distinct()
    )
    source_rules = {
        key_name: override.get_excludes()
        for override, key_name in (
            db.query(InputOverride, DataSource.key_name)
            .join(DataSource, DataSource.id == InputOverride.data_source_id)
            .filter(InputOverride.generator_id == dashboard.generator_id)
            .filter(InputOverride.data_source_id.in_(own_source_ids))
            .all()
        )
        if override.get_excludes()
    }
    # Aliases travel under their own key, not inside source_rules: those have
    # the shape "folder → list of masks", and changing it would break the engine
    # that already reads it. Same scope — own generator, own observed sources.
    source_aliases = {
        key_name: override.get_file_aliases()
        for override, key_name in (
            db.query(InputOverride, DataSource.key_name)
            .join(DataSource, DataSource.id == InputOverride.data_source_id)
            .filter(InputOverride.generator_id == dashboard.generator_id)
            .filter(InputOverride.data_source_id.in_(own_source_ids))
            .all()
        )
        if override.get_file_aliases()
    }

    return {
        "generator": {"id": generator.id, "name": generator.name},
        "version_number": cur.version_number,
        "manifest": manifest,
        "checksum_sha256": cur.checksum_sha256,
        "bundle_url": f"/api/dashboards/{dashboard.id}/generator/bundle?v={cur.version_number}",
        "run_lock_ttl_s": settings.generators_run_lock_ttl_s,
        "source_rules": source_rules,
        "source_aliases": source_aliases,
        # Normalized inputs (required_files sugar folded in) — the single
        # shape the engine, this endpoint and the tests all read.
        "inputs": genmanifest.inputs_for(manifest),
        # Normalized checks (ids filled) — what the worker evaluates per file.
        "checks": genmanifest.checks_for(manifest),
        # ADR-16: the platform reference tables this generator consumes, and
        # the versions a run may pin. Two queries, ten versions each.
        "reference": _reference_payload(db, manifest),
        # python-calamine is the sanctioned reader for structure sniffing
        # (RFC decision: column-level lineage is out of reach through it, but
        # sheet-level is not) — loaded unconditionally by gen-worker.js, so the
        # engine always has it regardless of what the generator itself declares.
        "force_deps": ["python-calamine"],
    }


@router.get("/api/dashboards/{dashboard_id}/generator/bundle")
async def get_generator_bundle(
    v: int = Query(...),
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Serve an immutable version's bundle.zip. `v` is required and must belong
    to THIS dashboard's generator (a dashboard cannot fetch another's code)."""
    _assert_can_update_data(dashboard, user, db)
    if not dashboard.generator_id:
        raise HTTPException(status_code=404, detail="Dashboard has no generator")
    ver = db.query(GeneratorVersion.id).filter(
        GeneratorVersion.generator_id == dashboard.generator_id,
        GeneratorVersion.version_number == v,
    ).first()
    if ver is None:
        raise HTTPException(status_code=404, detail="Version not found")
    path = genstorage.bundle_zip_path(dashboard.generator_id, v)
    if path is None:
        raise HTTPException(status_code=404, detail="Bundle file missing")
    # Versions are immutable and content-addressed by (generator, version) — safe
    # to cache hard, like snapshot.js?v={fp12}.
    return FileResponse(
        path, media_type="application/zip",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


# ---------------------------------------------------------------------------
# Run lifecycle (can_update_dashboard_data)
# ---------------------------------------------------------------------------

# One run resolves at most this many reference tables — a manifest declaring
# more platform inputs than a person can reason about is a design problem, not
# something the run panel should silently accept.
_MAX_RUN_REFERENCE = 8


def _resolve_reference_block(db: Session, manifest: dict,
                             input_manifest: dict | None) -> list[tuple]:
    """Validate `input_manifest.reference` against the FROZEN manifest that is
    about to run, and return `[(table, version_number|None, applied)]` for the
    `RunReferenceUse` rows the caller writes once the run has an id.

    The block is the run's parameter «apply this dictionary, at this version»
    (ADR-16: the switch is a run parameter, not a report setting), so it is
    validated the same way the engine choice is — before any row is written,
    with a 400 that names what is wrong:

      * a slug the frozen manifest does not declare as a platform input —
        a run may not pull in a dictionary its generator never asked for;
      * a pinned version on a table whose `allow_pin` is off — the picker is
        the owner's explicit permission, not a default;
      * a version number that does not exist.

    A slug the manifest declares but nobody has CREATED yet is silently
    skipped rather than rejected: `reference[]` in the manifest endpoint hands
    the engine `exists: false` for exactly that case, and a table deleted
    between the panel's load and the click must not cost the analyst the run.
    Everything else about the block is ignored by `observations` — it carries
    no files and no folders, so `check_manifest_size` only ever sees it as a
    few dozen bytes of JSON.
    """
    if not isinstance(input_manifest, dict):
        return []
    raw = input_manifest.get("reference")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="input_manifest.reference must be a list")
    if len(raw) > _MAX_RUN_REFERENCE:
        raise HTTPException(status_code=400,
                            detail=f"input_manifest.reference lists more than {_MAX_RUN_REFERENCE} tables")
    declared = {p["table"] for p in genmanifest.platform_inputs(manifest)}

    parsed: list[tuple[str, int | None, bool]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="reference entries must be objects")
        slug = item.get("slug")
        if not isinstance(slug, str) or not refsvc.SLUG_RE.match(slug):
            raise HTTPException(status_code=400, detail="reference.slug is malformed")
        if slug not in declared:
            raise HTTPException(status_code=400,
                                detail=f"reference table {slug!r} is not a platform input of this generator")
        if slug in seen:
            raise HTTPException(status_code=400, detail=f"reference table {slug!r} listed twice")
        seen.add(slug)
        version = item.get("version")
        if version is not None and (not isinstance(version, int) or isinstance(version, bool) or version < 1):
            raise HTTPException(status_code=400, detail=f"reference.version for {slug!r} must be a positive integer")
        parsed.append((slug, version, bool(item.get("applied", True))))

    if not parsed:
        return []
    tables = {t.slug: t for t in db.query(ReferenceTable)
              .filter(ReferenceTable.slug.in_([s for s, _v, _a in parsed])).all()}
    pinned = [(tables[s].id, v) for s, v, _a in parsed if v is not None and s in tables]
    known: set[tuple[int, int]] = set()
    if pinned:
        known = {
            (tid, n) for tid, n in db.query(
                ReferenceTableVersion.table_id, ReferenceTableVersion.version_number)
            .filter(ReferenceTableVersion.table_id.in_([tid for tid, _v in pinned]))
            .all()
        }
    out: list[tuple] = []
    for slug, version, applied in parsed:
        table = tables.get(slug)
        if table is None:
            continue  # declared but never created — see the docstring
        if version is not None:
            if not table.allow_pin:
                raise HTTPException(
                    status_code=400,
                    detail=f"reference table {slug!r} does not allow pinning a version")
            if (table.id, version) not in known:
                raise HTTPException(status_code=400,
                                    detail=f"reference table {slug!r} has no version {version}")
        if not applied:
            # "No reference table" is a recorded decision, not a missing fact.
            out.append((table, None, False))
            continue
        resolved = version if version is not None else table.current_version
        if not resolved:
            raise HTTPException(
                status_code=400,
                detail=f"reference table {slug!r} has no data yet — it cannot be applied")
        out.append((table, resolved, True))
    return out


@router.post("/api/dashboards/{dashboard_id}/generator/runs")
async def start_run(
    body: RunStartBody,
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start a run. Resolves the generator's CURRENT version, enforces the engine
    against the manifest, and takes the run-lock (409 if a live one is held)."""
    _assert_can_update_data(dashboard, user, db)

    if not dashboard.generator_id:
        raise HTTPException(status_code=404, detail="Dashboard has no generator")
    cur = _current_generator_version(db, dashboard)
    if cur is None:
        raise HTTPException(status_code=400, detail="Generator has no current version")

    engine = body.engine
    if engine not in ("browser", "agent", "local"):
        raise HTTPException(status_code=400,
                            detail="engine must be 'browser', 'agent' or 'local'")
    manifest = cur.get_manifest()
    manifest_engine = manifest.get("engine", "auto")
    # `sources` declares a read from GreenPlum — such a manifest is executed only
    # by the agent's legacy CLI, and it never has a folder run by construction.
    has_sources = bool(manifest.get("sources"))
    if engine == "local":
        # The refusal must NAME the reason: two different things reject "local",
        # and without the reason's name the generator author fixes the wrong one.
        if manifest_engine == "agent":
            raise HTTPException(
                status_code=400,
                detail="This generator declares engine 'agent' — a local folder run "
                       "is allowed only for 'browser' or 'auto'")
        if has_sources:
            raise HTTPException(
                status_code=400,
                detail="This generator declares `sources` (GreenPlum) — it runs on the "
                       "agent's SQL path, not from local folders")
    elif manifest_engine == "browser" and engine != "browser":
        raise HTTPException(status_code=400, detail="This generator runs in the browser only")
    elif manifest_engine == "agent" and engine != "agent":
        raise HTTPException(status_code=400, detail="This generator runs on the agent only")

    # Size guard BEFORE any row is written — a 413 here must leave no
    # half-created run behind. An agent run's manifest is a small constant
    # (no `dirs`), so this only ever fires on a browser listing — or on a
    # folder run (`local`), which lists files exactly the same way.
    if body.input_manifest is not None:
        try:
            observations.check_manifest_size(body.input_manifest)
        except ValueError as e:
            raise HTTPException(status_code=413, detail=str(e))

    # ADR-16: which reference tables this run resolves, and at which version.
    # Validated BEFORE the run-lock is taken, same discipline as the size
    # guard above — a 400 here leaves no half-created run behind.
    reference_uses = _resolve_reference_block(db, manifest, body.input_manifest)

    ttl = settings.generators_run_lock_ttl_s
    now = datetime.utcnow()

    # Run-lock, read-path enforced (no sweeper): expired `running` rows are marked
    # `lost` here; a still-live one blocks with 409.
    running = db.query(GenerationRun).filter(
        GenerationRun.dashboard_id == dashboard.id,
        GenerationRun.status == RUN_RUNNING,
    ).all()
    live = None
    live_age = 0
    for r in running:
        ref = r.heartbeat_at or r.started_at
        age = (now - ref).total_seconds()
        if age > ttl:
            r.status = RUN_LOST
            r.finished_at = now
        elif live is None:
            live = r
            live_age = int(age)

    if live is not None:
        starter = (db.query(User).filter(User.id == live.started_by).first()
                   if live.started_by else None)
        detail = {
            "locked_by": starter.person_id if starter else None,
            "started_at": live.started_at.isoformat() if live.started_at else None,
            "age_s": live_age,
        }
        db.commit()  # persist any lost-markings made above; the live lock stays
        raise HTTPException(status_code=409, detail=detail)

    run = GenerationRun(
        dashboard_id=dashboard.id,
        generator_version_id=cur.id,
        engine=engine,
        status=RUN_RUNNING,
        started_by=user.id,
        started_at=now,  # explicit: TTL comparisons run in Python, not SQL
        input_manifest=(json.dumps(body.input_manifest, ensure_ascii=False)
                        if body.input_manifest is not None else None),
    )
    db.add(run)
    db.flush()  # need run.id before ingest_run_start writes the FK below

    ingest_result = observations.ingest_run_start(db, run, body.input_manifest)
    for table, version_number, applied in reference_uses:
        db.add(RunReferenceUse(run_id=run.id, table_id=table.id,
                               version_number=version_number, applied=applied))

    _log(db, user, "generation_run_start", "dashboard", dashboard.id,
         {"engine": engine, "generator_version": cur.version_number,
          **({"reference": [
              {"slug": t.slug, "version": v, "applied": a}
              for t, v, a in reference_uses]} if reference_uses else {})})
    db.commit()  # single commit: lost-markings + the new run + its observations

    return JSONResponse({
        "run_id": run.id,
        "generator_version_id": cur.id,
        "version_number": cur.version_number,
        "bundle_url": f"/api/dashboards/{dashboard.id}/generator/bundle?v={cur.version_number}",
        "run_lock_ttl_s": ttl,
        "heartbeat_every_s": max(15, ttl // 6),
        # Additive (Data Lineage v2): which observation row backs which folder
        # index, and the (size, mtime_ms) the engine already knows about from
        # the source's previous observation, so it can skip re-sniffing.
        "observation_ids": ingest_result["observation_ids"],
        "last_known": ingest_result["last_known"],
    }, status_code=201)


@router.patch("/api/dashboards/{dashboard_id}/generator/runs/{run_id}")
async def heartbeat_run(
    run_id: int,
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Heartbeat: renews the lock. No audit — it would be noise."""
    _assert_can_update_data(dashboard, user, db)
    run = db.query(GenerationRun).filter(
        GenerationRun.id == run_id,
        GenerationRun.dashboard_id == dashboard.id,
    ).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != RUN_RUNNING:
        raise HTTPException(status_code=409, detail={"status": run.status})
    run.heartbeat_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.delete("/api/dashboards/{dashboard_id}/generator/runs/{run_id}")
async def cancel_run(
    run_id: int,
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel a running run. Idempotent on an already-finished run."""
    _assert_can_update_data(dashboard, user, db)
    run = db.query(GenerationRun).filter(
        GenerationRun.id == run_id,
        GenerationRun.dashboard_id == dashboard.id,
    ).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status == RUN_RUNNING:
        now = datetime.utcnow()
        run.status = RUN_CANCELED
        run.finished_at = now
        run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
        _log(db, user, "generation_run_cancel", "dashboard", dashboard.id, {"run_id": run.id})
        db.commit()
        return {"ok": True, "status": RUN_CANCELED}
    return {"ok": False, "status": run.status}


@router.post("/api/dashboards/{dashboard_id}/generator/runs/{run_id}/error")
async def fail_run(
    run_id: int,
    body: RunErrorBody,
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record a human-readable failure from the engine."""
    _assert_can_update_data(dashboard, user, db)
    run = db.query(GenerationRun).filter(
        GenerationRun.id == run_id,
        GenerationRun.dashboard_id == dashboard.id,
    ).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != RUN_RUNNING:
        raise HTTPException(status_code=409, detail={"status": run.status})

    message = (body.message or "")[:2000]
    detail = body.detail[:20000] if body.detail else None
    now = datetime.utcnow()
    run.status = RUN_ERROR
    run.error_message = message
    run.error_detail = detail
    run.finished_at = now
    run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
    _log(db, user, "generation_run_error", "dashboard", dashboard.id,
         {"run_id": run.id, "message": message[:500]})
    db.commit()
    return {"ok": True}


@router.post("/api/dashboards/{dashboard_id}/generator/runs/{run_id}/aux")
async def upload_run_aux(
    run_id: int,
    file: UploadFile = File(...),
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload an auxiliary input file for a LIVE run (aux_inputs pilot: an
    analyst's filled reasons workbook, downloaded from a write_back slot of
    the FIRST run, feeding a special second run of the same generator).
    Run-scoped, not dashboard-scoped: only the run's own starter may hand it
    in, and only while that run is still the live one."""
    _assert_can_update_data(dashboard, user, db)
    run = db.query(GenerationRun).filter(
        GenerationRun.id == run_id,
        GenerationRun.dashboard_id == dashboard.id,
    ).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != RUN_RUNNING or run.started_by != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the run's own starter can upload its aux file while it is running",
        )

    # The run's OWN (immutable, frozen) manifest is the contract — not
    # whatever the generator's current_version happens to be today.
    ver = db.query(GeneratorVersion).filter(
        GeneratorVersion.id == run.generator_version_id
    ).first()
    manifest = ver.get_manifest() if ver else {}
    aux_names = {a.get("name") for a in (manifest.get("aux_inputs") or [])
                 if isinstance(a, dict)}
    filename = file.filename or ""
    if filename not in aux_names:
        raise HTTPException(
            status_code=400,
            detail=f"{filename!r} is not one of this generator's aux_inputs",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(content) > _MAX_AUX:
        raise HTTPException(
            status_code=413,
            detail=f"aux file too large (max {_MAX_AUX // (1024 * 1024)} MB)",
        )

    try:
        genstorage.save_run_aux(dashboard.generator_id, run.id, filename, content)
    except genstorage.BundleError as e:
        raise HTTPException(status_code=400, detail=str(e))

    obj = _parse_input_manifest(run.input_manifest)
    existing = obj.get("aux_files")
    names = set(existing) if isinstance(existing, list) else set()
    names.add(filename)
    obj["aux_files"] = sorted(names)
    run.input_manifest = json.dumps(obj, ensure_ascii=False)

    n = len(content)
    _log(db, user, "generation_run_aux_upload", "dashboard", dashboard.id,
         {"run_id": run_id, "name": filename, "size": n})
    db.commit()
    return {"ok": True, "name": filename, "size": n}


@router.get("/api/dashboards/{dashboard_id}/generator/runs/{run_id}/aux")
def download_run_aux(
    run_id: int,
    name: str | None = Query(None),
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download a run's auxiliary input file. Sync def: the path.exists() stat
    below is disk I/O, so Starlette runs the whole handler in the threadpool
    (repo convention — see get_slot_file/share_get_slot)."""
    _assert_can_update_data(dashboard, user, db)
    run = db.query(GenerationRun).filter(
        GenerationRun.id == run_id,
        GenerationRun.dashboard_id == dashboard.id,
    ).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    aux_files = _aux_files_of(run)
    if name is None:
        if len(aux_files) == 1:
            name = aux_files[0]
        else:
            raise HTTPException(
                status_code=400,
                detail="name is required (this run has zero or multiple aux files)",
            )
    if name not in aux_files:
        raise HTTPException(status_code=404, detail="No such aux file on this run")

    path = genstorage.run_aux_path(dashboard.generator_id, run.id, name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Aux file missing on disk")

    media_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if name.lower().endswith(".xlsx") else "application/octet-stream"
    )
    return FileResponse(path, media_type=media_type, filename=name)


def _validate_outputs_json(raw: str) -> dict:
    """`{"write_back": [{name, size, targets: [folder], failed: [folder]}]}` —
    the page's own record of what a run wrote back into the analyst's folders
    (`generation_runs.outputs_json`). Shape-checked, not schema-validated: a
    malformed report from a misbehaving engine build should not be able to
    fail a publish that otherwise succeeded, so every problem here is a 400
    the CALLER decides whether to retry without the field, not a 500."""
    if len(raw.encode("utf-8")) > _MAX_OUTPUTS_JSON:
        raise HTTPException(status_code=413, detail="outputs_json too large (max 64 KB)")
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="outputs_json is not valid JSON")
    if not isinstance(obj, dict) or not isinstance(obj.get("write_back"), list):
        raise HTTPException(status_code=400, detail="outputs_json must be {write_back: [...]}")
    for item in obj["write_back"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise HTTPException(status_code=400, detail="write_back[] entries need a string name")
        if "size" in item and not isinstance(item["size"], int):
            raise HTTPException(status_code=400, detail="write_back[].size must be an integer")
        if "targets" in item and not isinstance(item["targets"], list):
            raise HTTPException(status_code=400, detail="write_back[].targets must be a list")
        if "failed" in item and not isinstance(item["failed"], list):
            raise HTTPException(status_code=400, detail="write_back[].failed must be a list")
    return obj


@router.post("/api/dashboards/{dashboard_id}/generator/runs/{run_id}/publish")
async def publish_run(
    run_id: int,
    request: Request,
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Atomic publish of a run's output (RFC E.3/E.6, replace_html pattern):
    the whole v{n+1} lands on disk, then a SINGLE db.commit() moves the pointer,
    marks the run success, and (first success) reveals the draft. A lost/canceled
    run cannot publish."""
    _assert_can_update_data(dashboard, user, db)
    run = db.query(GenerationRun).filter(
        GenerationRun.id == run_id,
        GenerationRun.dashboard_id == dashboard.id,
    ).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != RUN_RUNNING:
        raise HTTPException(status_code=409, detail={"status": run.status})

    # Multipart parsed by hand: an optional html_file plus any number of slot_N.
    form = await request.form()
    html_upload = form.get("html_file")
    slot_uploads: dict[int, UploadFile] = {}
    for key, value in form.multi_items():
        m = re.match(r"^slot_(\d+)$", key)
        if m and hasattr(value, "read"):
            slot_uploads[int(m.group(1))] = value

    # Read + size-check everything BEFORE touching disk, so a 413 leaves no
    # half-written version behind.
    if html_upload is not None and hasattr(html_upload, "read"):
        html_bytes = await html_upload.read()
        if not html_bytes:
            raise HTTPException(status_code=400, detail="Empty html_file")
        if len(html_bytes) > _MAX_UPLOAD:
            raise HTTPException(status_code=413, detail="html_file too large (max 50 MB)")
        html_provided = True
    else:
        # outputs.html == false: keep the current HTML, refresh only the slots.
        html_bytes = storage.read_html(dashboard.id, dashboard.current_version)
        html_provided = False

    uploaded: dict[int, tuple[str, bytes]] = {}
    total_bytes = len(html_bytes)
    for idx, up in slot_uploads.items():
        content = await up.read()
        if not content:
            raise HTTPException(status_code=400, detail=f"Empty slot_{idx}")
        if len(content) > _MAX_UPLOAD:
            raise HTTPException(status_code=413, detail=f"slot_{idx} too large (max 50 MB)")
        uploaded[idx] = (up.filename, content)
        total_bytes += len(content)

    # Optional write_back report (Data Lineage v2): what the engine wrote back
    # into the analyst's own folders, if anything. Validated before any disk
    # write, same discipline as html_file/slot_N above.
    outputs_raw = form.get("outputs_json")
    outputs_obj: dict | None = None
    if isinstance(outputs_raw, str) and outputs_raw.strip():
        outputs_obj = _validate_outputs_json(outputs_raw)

    new_version = dashboard.current_version + 1

    # (a) disk: the full v{n+1} directory before any DB row references it.
    html_path = storage.save_html(dashboard.id, new_version, html_bytes)
    storage.copy_slot_files(dashboard.id, dashboard.current_version, new_version)
    stored_slots: dict[int, str] = {}
    for idx, (fname, content) in uploaded.items():
        stored_slots[idx] = storage.save_slot_file(dashboard.id, new_version, idx, fname, content)

    # (b) DB rows (still uncommitted).
    # The version signature says which run made it AND which dictionaries that
    # run resolved (ADR-16) — "no reference table bv" is as load-bearing an answer
    # months later as "reference table bv v7", so both are spelled out. Appended,
    # never replacing: `generated: run N` is what every existing reader greps
    # for.
    note = f"generated: run {run_id}"
    # Scenario name from folders.ini (K3: `input_manifest.scenario`). An unnamed
    # set is an empty string, nothing to print; but "scenario without-Irkutsk"
    # six months later explains a mismatch in the numbers better than any
    # analysis, so it goes into the version note next to the reference tables.
    scenario = _run_scenario(run)
    if scenario:
        note += f" · scenario {scenario}"
    # The version note is a DB string, not interface (see "Not translated" in
    # CLAUDE.md): it is read by eye six months later in the same place as
    # `generated: run N`, and translating it would split the history by language.
    offline_at = _run_offline_at(run)
    if offline_at is not None:
        when = f" {offline_at}" if offline_at else ""
        note += f" · built offline{when}"
    for slug, version_number, applied in (
        db.query(ReferenceTable.slug, RunReferenceUse.version_number, RunReferenceUse.applied)
        .join(RunReferenceUse, RunReferenceUse.table_id == ReferenceTable.id)
        .filter(RunReferenceUse.run_id == run.id)
        .order_by(ReferenceTable.slug).all()
    ):
        note += (f" · reference table {slug} v{version_number}"
                 if applied and version_number else f" · no reference table {slug}")
    db.add(DashboardVersion(
        dashboard_id=dashboard.id, version_number=new_version,
        html_path=html_path, created_by=user.id,
        note=note,
    ))
    for idx, (fname, content) in uploaded.items():
        existing = db.query(DashboardFile).filter(
            DashboardFile.dashboard_id == dashboard.id,
            DashboardFile.version_number == new_version,
            DashboardFile.slot_index == idx,
        ).first()
        if existing:
            existing.original_name = fname
            existing.stored_path = stored_slots[idx]
            existing.size_bytes = len(content)
        else:
            db.add(DashboardFile(
                dashboard_id=dashboard.id, version_number=new_version,
                slot_index=idx, original_name=fname,
                stored_path=stored_slots[idx], size_bytes=len(content),
            ))

    if html_provided:
        html_str = html_bytes.decode("utf-8", errors="replace")
        dashboard.dash_type = detector.detect_type(html_str)
        dashboard.slot_count = (detector.count_file_slots(html_str)
                                if dashboard.dash_type == DASH_INTERACTIVE else 0)

    now = datetime.utcnow()
    dashboard.current_version = new_version
    dashboard.render_size = storage.estimate_render_size(dashboard.id, new_version)
    dashboard.updated_at = now

    run.status = RUN_SUCCESS
    run.finished_at = now
    run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
    run.output_version_number = new_version
    run.bytes_published = total_bytes
    if outputs_obj is not None:
        run.outputs_json = json.dumps(outputs_obj, ensure_ascii=False)

    first_publish = not dashboard.is_published
    if first_publish:
        # First success reveals the draft: emptiness is visible only to whoever
        # can remove it; the removed report is then visible to everyone entitled.
        dashboard.is_published = True

    gv = db.query(GeneratorVersion.version_number, GeneratorVersion.manifest_json).filter(
        GeneratorVersion.id == run.generator_version_id
    ).first()

    # Diagnostics against the FROZEN manifest of the version that just ran. They
    # never reject — a publish that succeeds today keeps succeeding — but the
    # conditions they name used to pass in silence, and the author is the only
    # person who can act on them. One JSON parse on a rare human-initiated route.
    warnings: list[str] = []
    quality_url = None  # only set when this run actually produced quality notes
    try:
        frozen = json.loads(gv[1]) if gv and gv[1] else {}
        if isinstance(frozen, dict):
            # Same run, freshest data quality results this run produced —
            # never blocks publish, exactly like the schema diagnostics below.
            quality_notes = quality.run_summary(db, run)
            if quality_notes.get("failed_checks") or quality_notes.get("new_values"):
                quality_url = quality_notes.get("quality_url")
            observed_diff = None
            manifest_inputs = genmanifest.inputs_for(frozen)
            if manifest_inputs:
                obs_ids = [
                    oid for (oid,) in db.query(SourceObservation.id)
                    .filter(SourceObservation.run_id == run.id).all()
                ]
                files_for_diff = [
                    {
                        "name": sf.name, "matched_input": sf.matched_input,
                        "schema": json.loads(sf.schema_json) if sf.schema_json else None,
                        "consumed": json.loads(sf.consumed_json) if sf.consumed_json else None,
                    }
                    for sf in db.query(SourceFile)
                    .filter(SourceFile.observation_id.in_(obs_ids)).all()
                ] if obs_ids else []
                observed_diff = schemadiff.diff(manifest_inputs, files_for_diff)
            warnings = genmanifest.publish_warnings(
                frozen,
                {idx: (fname, len(content)) for idx, (fname, content) in uploaded.items()},
                render_size=dashboard.render_size,
                preview_max=settings.preview_max_bytes,
                upload_max=_MAX_UPLOAD,
                first_publish=first_publish,
                observed_diff=observed_diff,
                quality=quality_notes,
            )
    except (ValueError, TypeError):
        pass  # a diagnostic must never be the reason a good publish fails

    # ADR-18: the RLS policy ships with the report — it describes the COLUMNS of
    # its slots, and the columns are set by the same manifest that was just published.
    # A broken policy does not fail the publish (the data is already on disk) and
    # does NOT remove the one in place: dropping a filter silently is a leak, not a degradation.
    try:
        frozen_rls = json.loads(gv[1]) if gv and gv[1] else {}
        policy = frozen_rls.get("rls") if isinstance(frozen_rls, dict) else None
        if policy is not None:
            errors = rls.validate_policy(policy)
            if errors:
                warnings = list(warnings) + [
                    _("RLS: the policy from the manifest was not applied — {detail}", detail="; ".join(errors[:3]))]
            elif (reason := rls.eligibility_error(
                    dashboard, html_bytes.decode("utf-8", errors="replace"))) is not None:
                # An interactive or EMPTY snapshot (agent reports such as
                # "Deltas" keep an empty bootstrap-snapshot and get their data
                # via op_file) is fine; data inside the HTML is not (the same rule as
                # in PUT /api/dashboards/{id}/rls).
                warnings = list(warnings) + [
                    _("RLS: the policy from the manifest was not applied — {detail}", detail=reason)]
            else:
                dashboard.rls_json = json.dumps(policy, ensure_ascii=False)
                # The cache key already carries the policy fingerprint (rls.policy_hash),
                # so this is a per-process safety net, not the invalidation mechanism.
                rls.cache.clear()
    except (ValueError, TypeError):
        pass

    _log(db, user, "generation_run_publish", "dashboard", dashboard.id, {
        "version": new_version, "bytes": total_bytes, "run_id": run.id,
        "generator_version": gv[0] if gv else None,
        # Audited: a report that silently lost its grid preview or published an
        # undeclared slot should be answerable months later, not only in the tab
        # of whoever happened to press Run.
        **({"warnings": warnings} if warnings else {}),
    })

    # (c) single commit point, THEN re-warm (fingerprint rotates on the new mtimes).
    db.commit()

    # ADR-14b addendum: slot -> lake conversion, best-effort AFTER the commit —
    # a publish that succeeded stays succeeded whatever happens to the lake.
    # None = this generator's semantics claim no slot tables (the usual case).
    lake_info = None
    try:
        lake_info = await run_in_threadpool(
            lake_slots.convert_run, db, dashboard, run, new_version, user)
        if lake_info is not None:
            _log(db, user, "lake_publish", "dashboard", dashboard.id, {
                "source": "slots", "run_id": run.id,
                "files": lake_info.get("files"), "bytes": lake_info.get("bytes"),
                **({"errors": lake_info["errors"]} if lake_info.get("errors") else {}),
            })
            db.commit()
    except Exception as e:  # noqa: BLE001 — conversion must never fail a publish
        lake_info = {"files": 0, "bytes": 0, "errors": [{"error": str(e)[:200]}]}

    # A conversion error kept only in `lake.errors` is invisible (prod
    # 2026-08-24: a slot name without its extension left the chat saying
    # "index not published" with nothing anywhere else) — surface it where
    # the person who pressed Run actually looks.
    if lake_info and lake_info.get("errors"):
        warnings = list(warnings) + [
            _("Report index (chat): {detail}",
              detail=((e.get("slot") + ": ") if e.get("slot") else "") + str(e.get("error") or ""))
            for e in lake_info["errors"]]

    warm.schedule(dashboard.id)
    return {"ok": True, "version": new_version, "published": True,
            "warnings": warnings, "quality_url": quality_url,
            **({"lake": lake_info} if lake_info is not None else {})}


# ---------------------------------------------------------------------------
# Run history / detail (can_update_dashboard_data)
# ---------------------------------------------------------------------------

@router.get("/api/dashboards/{dashboard_id}/generator/runs")
async def list_runs(
    limit: int = Query(20),
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Run history, newest first. Author + generator-version joined in (no N+1)."""
    _assert_can_update_data(dashboard, user, db)
    limit = max(1, min(limit, 100))
    rows = (db.query(GenerationRun, User.person_id, GeneratorVersion.version_number)
            .outerjoin(User, User.id == GenerationRun.started_by)
            .outerjoin(GeneratorVersion, GeneratorVersion.id == GenerationRun.generator_version_id)
            .filter(GenerationRun.dashboard_id == dashboard.id)
            .order_by(GenerationRun.started_at.desc())
            .limit(limit)
            .all())
    return [{
        "id": r.id,
        "engine": r.engine,
        "status": r.status,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "duration_ms": r.duration_ms,
        "output_version_number": r.output_version_number,
        "error_message": r.error_message,
        "started_by": person_id,
        "version_number": ver_num,
        "aux_files": _aux_files_of(r),
    } for r, person_id, ver_num in rows]


@router.get("/api/dashboards/{dashboard_id}/generator/runs/{run_id}")
async def get_run(
    run_id: int,
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Full run detail (history fields + error_detail, input_manifest, bytes)."""
    _assert_can_update_data(dashboard, user, db)
    row = (db.query(GenerationRun, User.person_id, GeneratorVersion.version_number)
           .outerjoin(User, User.id == GenerationRun.started_by)
           .outerjoin(GeneratorVersion, GeneratorVersion.id == GenerationRun.generator_version_id)
           .filter(GenerationRun.id == run_id,
                   GenerationRun.dashboard_id == dashboard.id)
           .first())
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    r, person_id, ver_num = row
    input_manifest = json.loads(r.input_manifest) if r.input_manifest else None
    return {
        "id": r.id,
        "engine": r.engine,
        "status": r.status,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "duration_ms": r.duration_ms,
        "output_version_number": r.output_version_number,
        "error_message": r.error_message,
        "error_detail": r.error_detail,
        "input_manifest": input_manifest,
        "aux_files": _aux_files_of(r),
        "bytes_published": r.bytes_published,
        "started_by": person_id,
        "version_number": ver_num,
    }


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def _generator_dashboards(db: Session, user: User) -> list[Dashboard]:
    """Generator-backed dashboards this user may RUN — the scope both the Data
    Sources page and its edit endpoint work in. An administrator sees all;
    everyone else sees their own plus their can_update_data grants."""
    q = db.query(Dashboard).filter(Dashboard.generator_id.isnot(None))
    if user.role != ROLE_ADMINISTRATOR:
        granted = (db.query(DashboardPermission.dashboard_id)
                   .filter(DashboardPermission.user_id == user.id,
                           DashboardPermission.can_update_data == True)  # noqa: E712
                   .scalar_subquery())
        q = q.filter(or_(Dashboard.owner_id == user.id, Dashboard.id.in_(granted)))
    return q.order_by(Dashboard.updated_at.desc()).all()


class DataSourceBody(BaseModel):
    kind: str
    key_name: str
    declared_path: str | None = None
    label: str | None = None
    notes: str | None = None
    # DEPRECATED (Data Lineage v2): junk exclusion moved to input_overrides,
    # keyed by (generator, source) — see PUT …/overrides/{generator_id}. Kept
    # in the body only so an older client that still sends it does not 422;
    # this endpoint never reads it.
    excludes: list[str] | None = None
    # Optimistic concurrency: the version the client last saw. None skips the
    # check (a first-time claim, or a caller that has not been updated yet).
    version: int | None = None
    # Data Quality thresholds. All optional and PATCH-like: a key absent from
    # the body leaves the column alone, an explicit null clears it back to
    # "evaluator default". Bounds are sanity, not policy — 3650 days and 5000
    # files are past anything a folder source can plausibly mean.
    stale_after_days: int | None = Field(default=None, ge=1, le=3650)
    min_files: int | None = Field(default=None, ge=0, le=5000)
    # {"min_rows": {"<match>/<sheet>": N}, "drop_pct": 1..100}. Validated by
    # _clean_thresholds, not by pydantic, so the error names the offending key.
    thresholds: dict | None = None
    # Impact Analysis addendum: who answers for this source. Must be an
    # existing user id (checked in the handler, not here — pydantic has no DB
    # access); explicit null clears it back to "nobody claimed it".
    owner_id: int | None = None


_MAX_THRESHOLD_KEYS = 32
_MAX_THRESHOLD_PATH = 200


def _clean_thresholds(raw: dict | None) -> dict:
    """Normalised {"min_rows": {path: int}, "drop_pct": int|None}; raises
    422 with a message naming the key, like pydantic would. `min_rows` keys
    are "<match>/<sheet>" paths the evaluator resolves later — they are NOT
    checked against the manifest here because a source is read by several
    generators and the path is only meaningful per generator; a key nothing
    resolves is simply inert."""
    if raw is None:
        return {"min_rows": {}, "drop_pct": None}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="thresholds must be an object")
    unknown = set(raw) - {"min_rows", "drop_pct"}
    if unknown:
        raise HTTPException(status_code=422,
                            detail=f"thresholds: unknown keys {sorted(unknown)}")
    out: dict = {"min_rows": {}, "drop_pct": None}
    mr = raw.get("min_rows")
    if mr is not None:
        if not isinstance(mr, dict):
            raise HTTPException(status_code=422, detail="thresholds.min_rows must be an object")
        if len(mr) > _MAX_THRESHOLD_KEYS:
            raise HTTPException(status_code=422,
                                detail=f"thresholds.min_rows: at most {_MAX_THRESHOLD_KEYS} keys")
        for k, n in mr.items():
            key = (k or "").strip() if isinstance(k, str) else ""
            if not key or len(key) > _MAX_THRESHOLD_PATH:
                raise HTTPException(status_code=422,
                                    detail=f"thresholds.min_rows: key must be 1..{_MAX_THRESHOLD_PATH} chars")
            if not isinstance(n, int) or isinstance(n, bool) or not (0 <= n <= 10_000_000):
                raise HTTPException(status_code=422,
                                    detail=f"thresholds.min_rows[{key!r}] must be an integer 0..10000000")
            out["min_rows"][key] = n
    dp = raw.get("drop_pct")
    if dp is not None:
        if not isinstance(dp, int) or isinstance(dp, bool) or not (1 <= dp <= 100):
            raise HTTPException(status_code=422, detail="thresholds.drop_pct must be 1..100")
        out["drop_pct"] = dp
    return out


@router.put("/api/data-sources")
async def upsert_data_source(
    body: DataSourceBody,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Claim or edit a source: state its path, label it.

    Authorisation is by REACH, not by role: you may edit a source if you can run
    at least one report that reads it. That is the same boundary as starting a
    run — whoever answers for a report's data decides what counts as junk in it
    — and it means nobody can touch a source they have no stake in.

    Rows are created here rather than when the page renders, so opening Data
    Sources stays a pure read.
    """
    kind = (body.kind or "").strip()
    key_name = (body.key_name or "").strip()
    # KIND_TABLE joined the list with ADR-16: a reference table is a source
    # like any other on the Data Sources page (owner, label, notes), and the
    # reach rule below is what decides who may edit it.
    if kind not in (datasources.KIND_FOLDER, datasources.KIND_GREENPLUM,
                    datasources.KIND_TABLE):
        raise HTTPException(status_code=400, detail="Unknown source kind")
    if not key_name or len(key_name) > 255:
        raise HTTPException(status_code=400, detail="key_name must be 1..255 chars")

    dashboards = _generator_dashboards(db, user)
    graph = await run_in_threadpool(datasources.build, db, dashboards)
    key = f"{kind}:{key_name}"
    match = next((s for s in graph["sources"] if s["key"] == key), None)
    if match is None:
        raise HTTPException(status_code=404,
                            detail="No report you can run reads this source")

    row = (db.query(DataSource)
           .filter(DataSource.kind == kind, DataSource.key_name == key_name)
           .first())
    if row is not None and body.version is not None and body.version != row.version:
        raise HTTPException(status_code=409, detail={
            "detail": "version conflict",
            "current": {
                "declared_path": row.declared_path, "label": row.label,
                "notes": row.notes, "version": row.version,
                "stale_after_days": row.stale_after_days, "min_files": row.min_files,
                "thresholds": row.get_thresholds(), "owner_id": row.owner_id,
            },
        })
    is_new = row is None
    if is_new:
        row = DataSource(kind=kind, key_name=key_name)
        db.add(row)

    def _clean(v, limit):
        v = (v or "").strip()
        return v[:limit] if v else None

    if body.declared_path is not None:
        row.declared_path = _clean(body.declared_path, 1024)
    if body.label is not None:
        row.label = _clean(body.label, 255)
    if body.notes is not None:
        row.notes = _clean(body.notes, 4000)
    # Thresholds: only the keys the client SENT move (model_fields_set), so a
    # label edit from an older drawer cannot wipe a tuned threshold.
    sent = body.model_fields_set
    if "stale_after_days" in sent:
        row.stale_after_days = body.stale_after_days
    if "min_files" in sent:
        row.min_files = body.min_files
    if "thresholds" in sent:
        row.set_thresholds(_clean_thresholds(body.thresholds))
    if "owner_id" in sent:
        if body.owner_id is not None and not db.query(User.id).filter(
                User.id == body.owner_id).first():
            raise HTTPException(status_code=422, detail="owner_id: no such user")
        row.owner_id = body.owner_id
    # Bump on every write (optimistic concurrency): a freshly created row's
    # default of 1 already counts as its first version, an edit moves it on.
    if not is_new:
        row.version += 1
    row.updated_by = user.id
    row.updated_at = datetime.utcnow()

    _log(db, user, "data_source_update", "data_source", key, {
        # The blast radius, recorded at the moment of the change: a rule edited
        # today is answered for months later, when who-else-reads-this has moved.
        "reports": match["reports"],
        "declared_path": row.declared_path,
        "version": row.version,
    })
    db.commit()
    return {
        "ok": True, "kind": kind, "key_name": key_name,
        "declared_path": row.declared_path, "label": row.label,
        "notes": row.notes, "version": row.version,
        "stale_after_days": row.stale_after_days, "min_files": row.min_files,
        "thresholds": row.get_thresholds(), "owner_id": row.owner_id,
        "reports": match["reports"],
    }


@router.get("/data-sources", response_class=HTMLResponse)
async def data_sources_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Which report reads what, and which sources feed more than one report.

    Scoped exactly like /generators — an administrator sees every
    generator-backed dashboard, everyone else sees their own plus the ones they
    hold a can_update_data grant on. A source list is only as trustworthy as the
    reports it was built from, so showing a user edges into reports they cannot
    open would be both a leak and a lie.

    All aggregation lives in services/datasources.build(); this route is the
    same three-query shape as the launcher above.
    """
    dashboards = _generator_dashboards(db, user)
    graph = await run_in_threadpool(datasources.build, db, dashboards)

    # What the lineage canvas draws from. Only the fields the cards actually
    # show — the file lists and query paths stay out, so a workspace with 500
    # files per report does not ship them twice.
    graph_json = json.dumps({
        "sources": [{"key": s["key"], "kind": s["kind"], "name": s["name"],
                     "certain": s["certain"], "shared": s["shared"],
                     "stale": s["stale"], "report_count": s["report_count"],
                     "files": s["files"], "bytes": s["bytes"],
                     "data_age_days": s["data_age_days"],
                     "queries": len(s["queries"])}
                    for s in graph["sources"]],
        "reports": [{"id": r["id"], "title": r["title"], "sources": r["sources"],
                     "generator": r["generator"], "engine": r["engine"],
                     "status": r["last_run_status"],
                     "last_run_at": r["last_run_at"].strftime("%Y-%m-%d") if r["last_run_at"] else None}
                    for r in graph["reports"]],
    }, ensure_ascii=False)

    # The editor's payload. Separate from graph_json on purpose: the canvas
    # redraws on every resize and has no use for file lists, while the drawer
    # needs them to show which files a rule would actually drop.
    # Thresholds are read off the registry rows here rather than threaded
    # through datasources.build(): the canvas never needs them, only the
    # drawer's "Quality thresholds" field does.
    _thr_rows = {
        r.id: r for r in db.query(DataSource).filter(DataSource.id.in_(
            [s["source_id"] for s in graph["sources"] if s.get("source_id")] or [0]
        )).all()
    }

    def _thresholds_of(s):
        r = _thr_rows.get(s.get("source_id"))
        if r is None:
            return {"stale_after_days": None, "min_files": None,
                    "thresholds": {"min_rows": {}, "drop_pct": None},
                    "owner_id": None}
        return {"stale_after_days": r.stale_after_days, "min_files": r.min_files,
                "thresholds": r.get_thresholds(), "owner_id": r.owner_id}

    # Annotations (P3.6): {source_id: [{match, sheet, column, description,
    # tags}]}, "" read back as None for the column so the drawer's per-column
    # loop never special-cases the DB's UNIQUE workaround (see Annotation
    # docstring). One query for every claimed source in scope.
    _ann_rows = (
        db.query(Annotation).filter(Annotation.data_source_id.in_(_thr_rows.keys() or [0])).all()
        if _thr_rows else []
    )
    _ann_by_source: dict[int, list[dict]] = {}
    for a in _ann_rows:
        _ann_by_source.setdefault(a.data_source_id, []).append({
            "match": a.match, "sheet": a.sheet,
            "column": a.column if a.column else None,
            "description": a.description, "tags": a.get_tags(),
        })

    sources_json = json.dumps([{
        "key": s["key"], "kind": s["kind"], "name": s["name"],
        **_thresholds_of(s),
        "certain": s["certain"], "shared": s["shared"], "stale": s["stale"],
        "report_count": s["report_count"], "reports": s["reports"],
        "files": s["files"], "bytes": s["bytes"],
        "data_age_days": s["data_age_days"], "read_age_days": s["read_age_days"],
        "declared_path": s["declared_path"], "label": s["label"],
        # What the app sent (not what a person declared by hand): the root,
        # its fingerprint and full file paths. The path travels under the same
        # right as the whole page (_generator_dashboards → can_update_data).
        "root_path": s["root_path"], "root_id": s["root_id"],
        "file_paths": s["file_paths"],
        "excludes": s["excludes"],
        "file_names": s["file_names"], "names_capped": s["names_capped"],
        # source_id/version drive the drawer's identity Save (PUT
        # /api/data-sources with an optimistic-concurrency version) and
        # overrides feeds its per-generator Overrides section — both null
        # for an unclaimed/unobserved source, same as datasources.build().
        "source_id": s["source_id"], "version": s["version"],
        "overrides": s["overrides"],
        # {generator_name: inputs_for(...)} — the drawer renders one
        # schema_diff.render_diff(none, inputs) per reader generator from
        # this; `diff` is always None here (no single run's SourceFile set
        # applies across every generator that reads a shared source), so it
        # renders the same honest "not yet observed — declared inputs only"
        # fallback the viewer's Data dock uses before a run exists.
        "generator_inputs": s["generator_inputs"],
        # Manifest-authored descriptions (P3.6) per generator, keyed
        # "<match>/<sheet>" -> {column: description} — the drawer/lineage
        # fall back to these when no annotation exists.
        "generator_column_descriptions": s.get("generator_column_descriptions", {}),
        # Data-owner annotations on this source (P3.6): [] for an
        # unclaimed/unobserved source, same discipline as overrides.
        "annotations": _ann_by_source.get(s["source_id"], []) if s["source_id"] else [],
    } for s in graph["sources"]], ensure_ascii=False)
    report_titles_json = json.dumps(
        {r["id"]: r["title"] for r in graph["reports"]}, ensure_ascii=False)

    env: Environment = request.app.state.jinja
    html_out = await run_in_threadpool(
        env.get_template("pages/data_sources.html").render,
        user=user, request=request,
        sources=graph["sources"], reports=graph["reports"], stats=graph["stats"],
        graph_json=graph_json, sources_json=sources_json,
        report_titles_json=report_titles_json,
        stale_days=datasources.STALE_DAYS,
    )
    return HTMLResponse(html_out)


@router.get("/generators", response_class=HTMLResponse)
async def generators_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Launcher (RFC E.5): every generator-backed dashboard, a "/themes" clone
    but scoped to what the user can RUN (update), not merely view — an
    administrator sees all of them, everyone else sees their own plus any
    dashboard they hold a can_update_data grant on. The create button/dialog is
    gated in the template (admin/superuser); this route itself only needs an
    authenticated user, since a grantee can open the page to launch a run.

    Three queries total, no N+1: the dashboard list, one GenerationRun sweep
    across all their ids (newest first, first-per-id taken in Python — run
    counts are small per RFC), and one Generator id->name map."""
    if user.role == ROLE_ADMINISTRATOR:
        dashboards = (db.query(Dashboard)
                      .filter(Dashboard.generator_id.isnot(None))
                      .order_by(Dashboard.updated_at.desc())
                      .all())
    else:
        granted = (db.query(DashboardPermission.dashboard_id)
                   .filter(DashboardPermission.user_id == user.id,
                           DashboardPermission.can_update_data == True)  # noqa: E712
                   .scalar_subquery())
        dashboards = (db.query(Dashboard)
                      .filter(Dashboard.generator_id.isnot(None))
                      .filter(or_(Dashboard.owner_id == user.id,
                                  Dashboard.id.in_(granted)))
                      .order_by(Dashboard.updated_at.desc())
                      .all())

    dash_ids = [d.id for d in dashboards]
    generator_ids = {d.generator_id for d in dashboards}

    last_runs: dict[str, GenerationRun] = {}
    if dash_ids:
        # Newest run per dashboard via GROUP BY + self-join on the max — the
        # result set is bounded by the dashboard count, not by run history
        # (a naive "fetch all runs, take first" grows forever with weekly runs).
        latest = (db.query(GenerationRun.dashboard_id.label("did"),
                           func.max(GenerationRun.started_at).label("ts"))
                  .filter(GenerationRun.dashboard_id.in_(dash_ids))
                  .group_by(GenerationRun.dashboard_id)
                  .subquery())
        runs = (db.query(GenerationRun)
                .join(latest, (GenerationRun.dashboard_id == latest.c.did)
                              & (GenerationRun.started_at == latest.c.ts))
                .all())
        for r in runs:
            last_runs.setdefault(r.dashboard_id, r)  # ties: one row wins, any is fine

    generator_names: dict[int, str] = {}
    if generator_ids:
        generator_names = dict(
            db.query(Generator.id, Generator.name)
            .filter(Generator.id.in_(generator_ids))
            .all()
        )

    env: Environment = request.app.state.jinja
    html_out = await run_in_threadpool(
        env.get_template("pages/generators.html").render,
        user=user, dashboards=dashboards, last_runs=last_runs,
        generator_names=generator_names, request=request,
    )
    return HTMLResponse(html_out)


# ===========================================================================
# Data Lineage v2 — contracts (Phase 0). Each route below is the frozen
# signature other phases code against; bodies are filled by the task named
# in the docstring. 501 until then, so a half-deployed build fails loudly.
# ===========================================================================

class ObservationFile(BaseModel):
    dir: int = 0
    name: str
    kind: str                       # listed|parsed|cached|service|unreadable|excluded
    schema_: dict | None = None     # serialised under the key "schema"
    consumed: dict | None = None
    checks: list[dict] | None = None      # run_checks() results for this file
    profiles: list[dict] | None = None    # column profiles for this file
    # Full file path — sent only by the desktop app. Typed Any, not str: an
    # optional field must not cost the batch (see observations.clean_path).
    path: Any = None

    model_config = {"populate_by_name": True}

    def __init__(self, **data):
        if "schema" in data and "schema_" not in data:
            data["schema_"] = data.pop("schema")
        super().__init__(**data)


class ObservationBody(BaseModel):
    seq: int
    files: list[ObservationFile]
    final: bool = False
    # P4.1 stages: the run's `onepage.stage()` snapshots, top-level (they
    # belong to the RUN, not to any one file) and optional — omitted by every
    # generator that never calls stage(), and by every older engine build.
    # Kept as raw dicts: observations._apply_stages validates and trims them,
    # the same division of labour as `checks`/`profiles` above.
    # Typed `list`, not `list[dict]`: one malformed entry must cost that
    # entry, not the whole batch (a 400 here would drop the final flush of a
    # run that otherwise succeeded). _apply_stages drops non-dicts itself.
    stages: list | None = None
    # Folder roots `[{dir, root_path, root_id}]` — only from the app; parsing is
    # silent, an unrecognised entry is skipped (observations.roots_by_dir);
    # typed Any, not list: a string instead of a list must not 400 the batch.
    roots: Any = None


@router.post("/api/dashboards/{dashboard_id}/generator/runs/{run_id}/observations")
async def post_run_observations(
    run_id: int,
    request: Request,
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Batched structure/consumption records the engine streams during a run.

    Auth = the run's starter (not `_assert_can_update_data`: a run belongs to
    whoever started it, not to everyone who could have). Accepted while the
    run is `running`, and for `_OBSERVATIONS_GRACE_S` after it closed — the
    engine's last batch races the error/publish call and must not 409 just
    because it lost that race. `seq` is read by hand off the raw body (not a
    FastAPI-injected model) so the 2 MB size guard runs before any JSON
    parsing.
    """
    run = db.query(GenerationRun).filter(
        GenerationRun.id == run_id,
        GenerationRun.dashboard_id == dashboard.id,
    ).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.started_by != user.id:
        raise HTTPException(status_code=403,
                            detail="Only the run's own starter can post observations")

    now = datetime.utcnow()
    if run.status == RUN_RUNNING:
        pass
    elif run.finished_at and (now - run.finished_at).total_seconds() <= _OBSERVATIONS_GRACE_S:
        pass
    else:
        raise HTTPException(status_code=409, detail="run is closed")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_OBS_BODY:
                raise HTTPException(status_code=413,
                                    detail="observations body too large (max 2 MB)")
        except ValueError:
            pass  # not a real content-length; the actual-bytes check below still applies
    raw = await request.body()
    if len(raw) > _MAX_OBS_BODY:
        raise HTTPException(status_code=413, detail="observations body too large (max 2 MB)")

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    try:
        body = ObservationBody.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    last_seq = _last_seq.get(run.id)
    if last_seq is not None and body.seq <= last_seq:
        raise HTTPException(status_code=409, detail={
            "error": "seq must increase within a run", "last_seq": last_seq,
        })

    try:
        result = observations.apply_batch(db, run, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if body.final:
        observations.finalize(db, run)
        _log(db, user, "generation_run_observation", "dashboard", dashboard.id, {
            "run_id": run.id, "files_updated": result["files_updated"], "final": True,
        })

    db.commit()
    _last_seq[run.id] = body.seq  # after commit: a failed commit must not advance it

    if body.final:
        # Leg 1: the folder just changed under us, so the catalogue is stale.
        # Debounced 60 s and off the request path entirely (its own thread,
        # its own session) — a user re-running a report three times costs one
        # inference, not three. Never raises into the response.
        try:
            from app.services import catalog
            catalog.schedule_for_run(run.id)
        except Exception:
            pass

    return {"ok": True, "seq": body.seq,
            "files_updated": result["files_updated"], "final": body.final}


class InputOverrideBody(BaseModel):
    exclude_rules: list[str]
    version: int | None = None
    # {"<match>/<sheet>": {"<declared column>": "<observed column>"}}.
    # Optional so an older drawer that only sends rules keeps working and
    # leaves the aliases alone; send {} to clear them.
    column_aliases: dict[str, dict[str, str]] | None = None


_MAX_OVERRIDE_RULES = 32
# File aliases: "THIS file is that declared input".
# The ceilings are low on purpose — this is a manual fix for "Anya renamed it",
# not a second way to declare inputs. If they are not enough, it is not the
# folder that drifted but the manifest, and that is what needs fixing.
_MAX_ALIAS_INPUTS = 8
_MAX_ALIAS_NAMES = 8

# The rule ceiling of the PRE-RUN SCREEN is its own, two orders of magnitude
# above the lineage drawer's. There a person types masks by hand, and thirty-two
# already means "someone is building a second system". Here rules come from one
# click on "exclude all in folder": the owner's live folder holds 171 exports,
# excluding them one by one is not humanly possible, and a single "*" mask is
# not allowed — globmatch forbids it on purpose (it would also disable future files).
# Hence names, one per file: the decision stays literal, and a file that
# appears in the folder tomorrow will be read, not retroactively
# disabled.
_MAX_PREVIEW_RULES = 1000
# A separate byte limit: a thousand names of 255 characters is a quarter of a
# megabyte in one column, something nobody ever does by hand.
_MAX_PREVIEW_RULES_BYTES = 64 * 1024
_MAX_ALIAS_SHEETS = 32
_MAX_ALIAS_PAIRS = 64
_MAX_ALIAS_NAME = 64


def _clean_aliases(raw: dict[str, dict[str, str]], inputs: list[dict]) -> dict[str, dict[str, str]]:
    """Validated aliases, or a 422 naming the offending key.

    The `<match>` half of every key must be one of the generator's CURRENT
    inputs (compared case-folded, as genmanifest does): an alias for a
    pattern the generator does not declare can never be applied, and
    accepting it would only leave a silent no-op in the row. The `<sheet>`
    half is NOT checked — sheets are declared as globs ("*") and the observed
    name is what the drawer offers — and neither is the declared column, so a
    manifest edit that renames a column yields a dangling alias rather than a
    400 on an unrelated save. schemadiff ignores what it cannot place."""
    if len(raw) > _MAX_ALIAS_SHEETS:
        raise HTTPException(status_code=422,
                            detail=f"column_aliases: at most {_MAX_ALIAS_SHEETS} sheets")
    matches = {str(i.get("match", "")).casefold(): str(i.get("match")) for i in inputs}
    out: dict[str, dict[str, str]] = {}
    for key, pairs in raw.items():
        k = (key or "").strip()
        match, sep, sheet = k.partition("/")
        if not sep or not match.strip() or not sheet.strip():
            raise HTTPException(status_code=422,
                                detail=f"column_aliases: key {key!r} must be '<match>/<sheet>'")
        canon = matches.get(match.strip().casefold())
        if canon is None:
            raise HTTPException(status_code=422,
                                detail=f"column_aliases: {match.strip()!r} is not one of this "
                                       f"generator's inputs")
        if len(sheet.strip()) > _MAX_ALIAS_NAME:
            raise HTTPException(status_code=422,
                                detail=f"column_aliases: sheet name in {key!r} exceeds {_MAX_ALIAS_NAME} chars")
        if len(pairs) > _MAX_ALIAS_PAIRS:
            raise HTTPException(status_code=422,
                                detail=f"column_aliases[{key!r}]: at most {_MAX_ALIAS_PAIRS} pairs")
        clean_pairs: dict[str, str] = {}
        for declared, observed in pairs.items():
            d, o = (declared or "").strip(), (observed or "").strip()
            if not d or not o or len(d) > _MAX_ALIAS_NAME or len(o) > _MAX_ALIAS_NAME:
                raise HTTPException(status_code=422,
                                    detail=f"column_aliases[{key!r}]: names must be 1..{_MAX_ALIAS_NAME} chars")
            clean_pairs[d] = o
        if clean_pairs:
            out[f"{canon}/{sheet.strip()}"] = clean_pairs
    return out


def _reachable_generator_dashboards(db: Session, user: User, generator_id: int | None = None) -> list[str]:
    """Dashboard ids this user can RUN (`_generator_dashboards`'s scope),
    optionally narrowed to one generator. Shared by both overrides routes so
    "reach" means exactly the same thing whether you are listing or editing."""
    q = _generator_dashboards(db, user)
    if generator_id is not None:
        q = [d for d in q if d.generator_id == generator_id]
    return [d.id for d in q if d.generator_id]


def _sources_observed_by(db: Session, dash_ids: list[str], source_id: int | None = None) -> "Query":
    """Dashboard ids among `dash_ids` whose own runs hold a SourceObservation
    on `source_id` (or, with source_id=None, every (dashboard, source) pair) —
    the join that turns "this generator reads this folder" from a manifest
    guess into an observed fact."""
    q = (db.query(Dashboard.id, SourceObservation.data_source_id)
         .join(GenerationRun, GenerationRun.dashboard_id == Dashboard.id)
         .join(SourceObservation, SourceObservation.run_id == GenerationRun.id)
         .filter(Dashboard.id.in_(dash_ids)))
    if source_id is not None:
        q = q.filter(SourceObservation.data_source_id == source_id)
    return q.distinct()


@router.get("/api/data-sources/{source_id}/overrides")
async def list_input_overrides(
    source_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """[{generator_id, generator_name, exclude_rules, column_aliases, version, updated_at}]
    for every generator this user can run that has actually OBSERVED this
    source through one of its own runs — reach by observation, not by the
    manifest's say-so, so a generator that has never touched this folder
    cannot see (or shape) rules for it."""
    ds = db.query(DataSource).filter(DataSource.id == source_id).first()
    if ds is None:
        raise HTTPException(status_code=404, detail="Source not found")

    dash_ids = _reachable_generator_dashboards(db, user)
    if not dash_ids:
        return []
    gen_ids = {
        d.generator_id for d in db.query(Dashboard.id, Dashboard.generator_id)
        .filter(Dashboard.id.in_(
            [did for did, _gen in _sources_observed_by(db, dash_ids, source_id).all()]
        )).all()
    }
    if not gen_ids:
        return []

    gen_names = dict(
        db.query(Generator.id, Generator.name).filter(Generator.id.in_(gen_ids)).all()
    )
    existing = {
        row.generator_id: row
        for row in db.query(InputOverride).filter(
            InputOverride.generator_id.in_(gen_ids),
            InputOverride.data_source_id == source_id).all()
    }
    out = []
    for gid in sorted(gen_ids):
        row = existing.get(gid)
        out.append({
            "generator_id": gid,
            "generator_name": gen_names.get(gid, "—"),
            "exclude_rules": row.get_excludes() if row else [],
            "column_aliases": row.get_aliases() if row else {},
            "version": row.version if row else 0,
            "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        })
    return out


@router.put("/api/data-sources/{source_id}/overrides/{generator_id}")
async def put_input_override(
    source_id: int,
    generator_id: int,
    body: InputOverrideBody,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reach check = user can run a dashboard of THIS generator that has
    observed THIS source; every rule validated through
    globmatch.validate_pattern (so an override can never claim to match a
    subfolder the flat walk will never see); `column_aliases`, when sent,
    validated by _clean_aliases against the generator's current inputs;
    `version` mismatch -> 409 {current}."""
    ds = db.query(DataSource).filter(DataSource.id == source_id).first()
    if ds is None:
        raise HTTPException(status_code=404, detail="Source not found")
    generator = db.query(Generator).filter(Generator.id == generator_id).first()
    if generator is None:
        raise HTTPException(status_code=404, detail="Generator not found")

    dash_ids = _reachable_generator_dashboards(db, user, generator_id)
    reports_affected = (
        [did for did, _gen in _sources_observed_by(db, dash_ids, source_id).all()]
        if dash_ids else []
    )
    if not reports_affected:
        raise HTTPException(
            status_code=404,
            detail="No report you can run reads this source through this generator")

    seen, rules = set(), []
    for raw in body.exclude_rules:
        pat = (raw or "").strip()
        err = globmatch.validate_pattern(pat)
        if err:
            raise HTTPException(status_code=400, detail=f"{pat!r}: {err}")
        folded = pat.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        rules.append(pat)
    if len(rules) > _MAX_OVERRIDE_RULES:
        raise HTTPException(status_code=400,
                            detail=f"At most {_MAX_OVERRIDE_RULES} patterns")

    aliases = None
    if body.column_aliases is not None:
        cur = (db.query(GeneratorVersion)
               .filter(GeneratorVersion.generator_id == generator_id,
                       GeneratorVersion.is_current == True)  # noqa: E712
               .first())
        inputs = genmanifest.inputs_for(cur.get_manifest()) if cur else []
        aliases = _clean_aliases(body.column_aliases, inputs)

    row = db.query(InputOverride).filter(
        InputOverride.generator_id == generator_id,
        InputOverride.data_source_id == source_id).first()
    if row is not None and body.version is not None and body.version != row.version:
        raise HTTPException(status_code=409, detail={
            "detail": "version conflict",
            "current": {"exclude_rules": row.get_excludes(),
                        "column_aliases": row.get_aliases(), "version": row.version},
        })

    if row is None:
        row = InputOverride(generator_id=generator_id, data_source_id=source_id)
        db.add(row)
        row.set_excludes(rules)
    else:
        row.set_excludes(rules)
        row.version += 1
    if aliases is not None:
        row.set_aliases(aliases)
    row.updated_by = user.id
    row.updated_at = datetime.utcnow()

    _log(db, user, "input_override_update", "input_override", f"{generator_id}:{source_id}", {
        "generator_id": generator_id, "source_id": source_id, "rules": rules,
        "column_aliases": row.get_aliases(),
        "reports": reports_affected,
    })
    db.commit()
    return {
        "ok": True, "generator_id": generator_id, "data_source_id": source_id,
        "exclude_rules": row.get_excludes(), "column_aliases": row.get_aliases(),
        "version": row.version,
    }


# ── Read rules from the Run button's side ───────────────────────────────
# The same `input_overrides` the drawer on /d/{id}/data-sources edits, but
# addressed the way a person sees them BEFORE a run: not "source #5" but
# "the OPS_RDC_Health folder I just connected". The difference is not
# cosmetic — on the sources page a rule can only be set on what a run has
# ALREADY observed, while an extra file must be excludable before the first
# run too. So here the source is CREATED by folder name when needed (the same
# way observation ingest does it), and the right is exactly the right to run
# (`_assert_can_update_data`), because the rule is part of the run.
#
# No values from the data: folder names, file names and masks — and nothing else.


def _clean_file_aliases(raw: dict, inputs: list) -> dict[str, list[str]]:
    """`{mask: [file name, …]}` → the same, validated.

    The key must be a mask declared by the CURRENT generator version: an alias
    to an undeclared input means nothing and would silently not fire. The value
    is a literal file name, not a path and not a mask: an alias names a file,
    it does not widen the glob.
    """
    declared = {str(i.get("match")) for i in inputs if isinstance(i, dict) and i.get("match")}
    out: dict[str, list[str]] = {}
    for match, names in (raw or {}).items():
        match = str(match).strip()
        if match not in declared:
            # In English, like the neighbouring checks in this module: the text
            # is read by the engine and turned into its own phrase, no person sees it.
            raise HTTPException(status_code=422, detail=(
                f"{match!r}: not an input declared by the current generator version"))
        if not isinstance(names, list):
            raise HTTPException(status_code=400, detail=f"{match!r}: expected a list of file names")
        clean, seen = [], set()
        for raw_name in names:
            name = str(raw_name or "").strip()
            if not name or len(name) > 255:
                raise HTTPException(status_code=400, detail="file name must be 1..255 chars")
            if "/" in name or "\\" in name:
                raise HTTPException(status_code=400,
                                    detail=f"{name!r}: a file name, not a path")
            folded = name.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            clean.append(name)
        if len(clean) > _MAX_ALIAS_NAMES:
            raise HTTPException(status_code=400,
                                detail=f"At most {_MAX_ALIAS_NAMES} file names per input")
        if clean:
            out[match] = clean
    if len(out) > _MAX_ALIAS_INPUTS:
        raise HTTPException(status_code=400,
                            detail=f"At most {_MAX_ALIAS_INPUTS} inputs with aliases")
    return out


class FolderRulesBody(BaseModel):
    folder: str
    exclude_rules: list[str]
    # None = "leave aliases alone", {} = "remove all". The difference matters:
    # the screen sends only what the person changed.
    file_aliases: dict[str, list[str]] | None = None
    version: int | None = None


def _folder_rule_rows(db: Session, dashboard: Dashboard) -> dict[str, DataSource]:
    """{folder name: DataSource} — folders this report has something to say
    about: the ones its own runs saw, plus the ones THIS generator already has
    a rule for (a rule lives on the
    "generator × source" pair, so it is shared by all reports of the generator —
    and must be shown everywhere it will be read by)."""
    observed = (
        db.query(SourceObservation.data_source_id)
        .join(GenerationRun, GenerationRun.id == SourceObservation.run_id)
        .filter(GenerationRun.dashboard_id == dashboard.id)
        .distinct()
    )
    ruled = (
        db.query(InputOverride.data_source_id)
        .filter(InputOverride.generator_id == dashboard.generator_id)
    )
    rows = (db.query(DataSource)
            .filter(DataSource.kind == datasources.KIND_FOLDER)
            .filter(or_(DataSource.id.in_(observed), DataSource.id.in_(ruled)))
            .all())
    return {r.key_name: r for r in rows}


@router.get("/api/dashboards/{dashboard_id}/generator/input-rules")
async def get_folder_input_rules(
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """{"folders": {"<folder name>": {source_id, version, exclude_rules}}}.

    Loaded on an explicit action by the person ("What will be read…"), not when
    the report opens: `/d/{id}` gains no new requests. A folder that is not here
    is a folder with no rules (`version: 0`), and the engine reads all of it."""
    _assert_can_update_data(dashboard, user, db)
    if not dashboard.generator_id:
        raise HTTPException(status_code=404, detail="Dashboard has no generator")
    rows = _folder_rule_rows(db, dashboard)
    overrides = {
        ov.data_source_id: ov
        for ov in db.query(InputOverride).filter(
            InputOverride.generator_id == dashboard.generator_id,
            InputOverride.data_source_id.in_([r.id for r in rows.values()] or [0])).all()
    } if rows else {}
    out = {}
    for name, src in rows.items():
        ov = overrides.get(src.id)
        out[name] = {
            "source_id": src.id,
            "version": ov.version if ov else 0,
            "exclude_rules": ov.get_excludes() if ov else [],
            "file_aliases": ov.get_file_aliases() if ov else {},
        }
    return {"generator_id": dashboard.generator_id, "folders": out}


@router.put("/api/dashboards/{dashboard_id}/generator/input-rules")
async def put_folder_input_rules(
    body: FolderRulesBody,
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """A "do not read" rule on a folder by NAME. `version` is the same
    optimistic lock as in `put_input_override`: 0 means "the row did not exist
    yet", a mismatch is a 409 with the current state, so two people do not
    silently overwrite each other's decisions."""
    _assert_can_update_data(dashboard, user, db)
    if not dashboard.generator_id:
        raise HTTPException(status_code=404, detail="Dashboard has no generator")

    folder = (body.folder or "").strip()
    if not folder or len(folder) > 255:
        raise HTTPException(status_code=400, detail="folder must be 1..255 chars")
    if "/" in folder or "\\" in folder:
        raise HTTPException(status_code=400, detail="folder must be a folder name, not a path")

    # Masks only through globmatch: one and the same answer to "what was
    # read" in the browser, the server and the desktop app.
    seen, rules = set(), []
    for raw in body.exclude_rules:
        pat = (raw or "").strip()
        err = globmatch.validate_pattern(pat)
        if err:
            raise HTTPException(status_code=400, detail=f"{pat!r}: {err}")
        folded = pat.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        rules.append(pat)
    if len(rules) > _MAX_PREVIEW_RULES:
        raise HTTPException(status_code=400,
                            detail=f"At most {_MAX_PREVIEW_RULES} patterns")
    if sum(len(r) for r in rules) > _MAX_PREVIEW_RULES_BYTES:
        raise HTTPException(status_code=400, detail="rules payload too large")

    aliases = None
    if body.file_aliases is not None:
        cur = (db.query(GeneratorVersion)
               .filter(GeneratorVersion.generator_id == dashboard.generator_id,
                       GeneratorVersion.is_current == True)  # noqa: E712
               .first())
        aliases = _clean_file_aliases(body.file_aliases,
                                      genmanifest.inputs_for(cur.get_manifest()) if cur else [])

    src = (db.query(DataSource)
           .filter(DataSource.kind == datasources.KIND_FOLDER,
                   DataSource.key_name == folder).first())
    if src is None:
        # The folder was just connected and no run has seen it yet — so there
        # is no source row either. Creating it here is cheaper and more honest
        # than making the person run the report with the extra files first.
        src = DataSource(kind=datasources.KIND_FOLDER, key_name=folder)
        db.add(src)
        db.flush()

    row = (db.query(InputOverride)
           .filter(InputOverride.generator_id == dashboard.generator_id,
                   InputOverride.data_source_id == src.id).first())
    cur_version = row.version if row is not None else 0
    if body.version is not None and body.version != cur_version:
        raise HTTPException(status_code=409, detail={
            "detail": "version conflict",
            "current": {"exclude_rules": row.get_excludes() if row else [],
                        "file_aliases": row.get_file_aliases() if row else {},
                        "version": cur_version},
        })

    if row is None:
        row = InputOverride(generator_id=dashboard.generator_id, data_source_id=src.id)
        db.add(row)
        row.set_excludes(rules)
    else:
        row.set_excludes(rules)
        row.version += 1
    if aliases is not None:
        row.set_file_aliases(aliases)
    row.updated_by = user.id
    row.updated_at = datetime.utcnow()

    # The action is the same as from the lineage drawer, so it has the same
    # name in the audit too — otherwise existing journal queries would stop
    # seeing half of the edits. Where the edit came from is told by `via`.
    _log(db, user, "input_override_update", "input_override",
         f"{dashboard.generator_id}:{src.id}", {
             "generator_id": dashboard.generator_id, "source_id": src.id,
             "folder": folder, "rules": rules, "via": "run_preview",
             # File and mask names — yes; not a single value from the data.
             "file_aliases": row.get_file_aliases(),
             "reports": [dashboard.id],
         })
    db.commit()
    return {"ok": True, "folder": folder, "source_id": src.id,
            "exclude_rules": row.get_excludes(),
            "file_aliases": row.get_file_aliases(), "version": row.version}


class AnnotationItem(BaseModel):
    match: str
    sheet: str
    column: str | None = None
    description: str
    tags: list[str] = []


class AnnotationKey(BaseModel):
    match: str
    sheet: str
    column: str | None = None


class AnnotationsBody(BaseModel):
    items: list[AnnotationItem]
    # Every (match, sheet, column) the caller actually rendered as a row —
    # whether it carried a description or was left blank. Only a key that
    # appears here is a candidate for delete-on-missing: an existing row
    # whose key is NOT in known_keys (a column a newer manifest version
    # dropped, a target from a generator this caller cannot reach, a row
    # written by a different caller entirely) is left untouched even though
    # `items` does not repeat it — the drawer only ever shows the CURRENT
    # user's CURRENT view of the declared targets, never the full history of
    # everything ever annotated on this source. Omitted (an older caller
    # that predates this field) -> nothing is deleted, only upserts happen;
    # that is the safe default over silently wiping rows the caller never
    # rendered.
    known_keys: list[AnnotationKey] | None = None


_MAX_ANNOTATIONS = 256
_MAX_ANNOTATION_TAGS = 8
_MAX_ANNOTATION_TAG_LEN = 32


def _annotation_key(match: str, sheet: str, column: str | None) -> tuple[str, str, str]:
    """(match, sheet, column) with the column NULL-vs-"" workaround applied
    (Annotation docstring): "" means sheet-level, never a real column name."""
    return (match, sheet, column or "")


def _clean_annotation_items(items: list[AnnotationItem]) -> list[dict]:
    """Validated, deduplicated-by-key items ready to upsert, or a 422 naming
    the offending entry. `match`/`sheet` are free text at this layer, same as
    column_aliases — an annotation on a match/sheet a generator later drops
    is a dangling note, not a broken save."""
    if len(items) > _MAX_ANNOTATIONS:
        raise HTTPException(status_code=422,
                            detail=f"annotations: at most {_MAX_ANNOTATIONS} items")
    seen: set[tuple] = set()
    out: list[dict] = []
    for i, item in enumerate(items):
        match = (item.match or "").strip()
        sheet = (item.sheet or "").strip()
        column = (item.column or "").strip() or None
        description = (item.description or "").strip()
        if not match or len(match) > 128:
            raise HTTPException(status_code=422,
                                detail=f"annotations[{i}].match must be 1..128 chars")
        if not sheet or len(sheet) > 64:
            raise HTTPException(status_code=422,
                                detail=f"annotations[{i}].sheet must be 1..64 chars")
        if column is not None and len(column) > 64:
            raise HTTPException(status_code=422,
                                detail=f"annotations[{i}].column must be at most 64 chars")
        if not description or len(description) > 500:
            raise HTTPException(status_code=422,
                                detail=f"annotations[{i}].description must be 1..500 chars")
        tags = [t.strip() for t in (item.tags or []) if isinstance(t, str) and t.strip()]
        if len(tags) > _MAX_ANNOTATION_TAGS:
            raise HTTPException(status_code=422,
                                detail=f"annotations[{i}].tags: at most {_MAX_ANNOTATION_TAGS}")
        if any(len(t) > _MAX_ANNOTATION_TAG_LEN for t in tags):
            raise HTTPException(status_code=422,
                                detail=f"annotations[{i}].tags: each at most "
                                       f"{_MAX_ANNOTATION_TAG_LEN} chars")
        key = _annotation_key(match, sheet, column)
        if key in seen:
            raise HTTPException(status_code=422,
                                detail=f"annotations[{i}] is a duplicate of an earlier item "
                                       f"({match!r}/{sheet!r}/{column!r})")
        seen.add(key)
        out.append({"match": match, "sheet": sheet, "column": column,
                    "description": description, "tags": tags})
    return out


def _clean_annotation_known_keys(keys: list["AnnotationKey"]) -> set[tuple[str, str, str]]:
    """(match, sheet, column) keys the caller declares it actually rendered —
    see AnnotationsBody.known_keys. Loose on purpose (no length/shape errors):
    this is scope information, not user-entered content, so a malformed entry
    is just ignored rather than failing the whole save."""
    out: set[tuple[str, str, str]] = set()
    for k in keys:
        match = (k.match or "").strip()
        sheet = (k.sheet or "").strip()
        column = (k.column or "").strip() or None
        if not match or not sheet:
            continue
        out.add(_annotation_key(match, sheet, column))
    return out


@router.get("/api/data-sources/{source_id}/annotations")
async def list_annotations(
    source_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """[{match, sheet, column, description, tags}] for a claimed source —
    reach checked the same way GET …/overrides checks it (any generator this
    user can run has actually observed this source)."""
    ds = db.query(DataSource).filter(DataSource.id == source_id).first()
    if ds is None:
        raise HTTPException(status_code=404, detail="Source not found")
    dash_ids = _reachable_generator_dashboards(db, user)
    observed = bool(dash_ids) and bool(list(_sources_observed_by(db, dash_ids, source_id).limit(1)))
    if not observed:
        return []
    rows = db.query(Annotation).filter(Annotation.data_source_id == source_id).all()
    return [
        {"match": a.match, "sheet": a.sheet, "column": a.column if a.column else None,
         "description": a.description, "tags": a.get_tags()}
        for a in rows
    ]


@router.put("/api/data-sources/{source_id}/annotations")
async def put_annotations(
    source_id: int,
    body: AnnotationsBody,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replace the full annotation set for this source in one call: upsert
    every item in the body, delete every existing row not named by it. This
    is a source-wide edit (an analyst annotates several sheets/columns at
    once from the drawer), so "delete what's missing" is the honest match for
    a form that shows the whole list and lets the user remove a row by
    deleting its field — a partial PATCH-per-row endpoint would need its own
    delete verb anyway.

    Reach = user can run at least one generator that has actually observed
    this source (same test as the overrides list, not narrowed to a single
    generator — annotations are not scoped to one)."""
    ds = db.query(DataSource).filter(DataSource.id == source_id).first()
    if ds is None:
        raise HTTPException(status_code=404, detail="Source not found")
    dash_ids = _reachable_generator_dashboards(db, user)
    observed = bool(dash_ids) and bool(list(_sources_observed_by(db, dash_ids, source_id).limit(1)))
    if not observed:
        raise HTTPException(
            status_code=404,
            detail="No report you can run reads this source")

    items = _clean_annotation_items(body.items)
    known = (_clean_annotation_known_keys(body.known_keys)
            if body.known_keys is not None else None)
    existing = {
        _annotation_key(a.match, a.sheet, a.column): a
        for a in db.query(Annotation).filter(Annotation.data_source_id == source_id).all()
    }
    now = datetime.utcnow()
    wanted_keys = set()
    for item in items:
        key = _annotation_key(item["match"], item["sheet"], item["column"])
        wanted_keys.add(key)
        row = existing.get(key)
        if row is None:
            row = Annotation(
                data_source_id=source_id, match=item["match"], sheet=item["sheet"],
                # "" (never NULL) for a sheet-level row — see Annotation docstring.
                column=item["column"] if item["column"] is not None else "",
            )
            db.add(row)
        row.description = item["description"]
        row.set_tags(item["tags"])
        row.updated_by = user.id
        row.updated_at = now
    # Delete-on-missing is scoped to known_keys (what the caller actually had
    # in view): a row outside that scope survives an unrelated save even
    # though `items` does not repeat it. See AnnotationsBody.known_keys.
    for key, row in existing.items():
        if key not in wanted_keys and known is not None and key in known:
            db.delete(row)

    _log(db, user, "annotations_update", "data_source", str(source_id), {
        "source_id": source_id, "count": len(items),
    })
    db.commit()
    rows = db.query(Annotation).filter(Annotation.data_source_id == source_id).all()
    return [
        {"match": a.match, "sheet": a.sheet, "column": a.column if a.column else None,
         "description": a.description, "tags": a.get_tags()}
        for a in rows
    ]


@router.get("/api/data-sources/lineage")
async def get_lineage_graph(
    format: str | None = Query(default=None),
    focus: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The lineage graph for the reports THIS user may run — {nodes, edges,
    meta}, shape pinned by tests/fixtures/lineage_sample.json. Layers:
    0 folder/greenplum, 1 pattern, 2 sheet, 3 generator, 4 output, 5 report.

    `format` is omitted for the page's own fetch (unchanged shape, no
    download headers — this is a live in-page request, not a save-as), or
    an EXPLICIT `json`, `md` or `mermaid` for the Export menu — all three
    scoped exactly like the page (`focus` walks the same closure the canvas
    highlights), rendered by services/lineage_export, and sent with a
    Content-Disposition that downloads rather than renders/replaces the tab.
    An explicit `format=json` still downloads: it is not the bare default,
    it is the Export menu's JSON option, and the page said so by asking for
    the param.

    Not cached: the page is opened by hand, and a graph that lags behind the
    run that just finished is worse than one that costs a second."""
    explicit_format = format is not None
    if explicit_format and format not in ("json", "md", "mermaid"):
        raise HTTPException(status_code=422, detail="format must be json, md or mermaid")
    fmt = format or "json"
    dashboards = _generator_dashboards(db, user)
    graph = await run_in_threadpool(lineage.build_graph, db, dashboards)
    scoped = lineage_export.subgraph(graph, focus) if focus else graph
    if not explicit_format:
        return scoped

    date_bit = datetime.utcnow().strftime("%Y-%m-%d")
    focus_bit = re.sub(r"[^A-Za-z0-9._-]+", "_", focus) if focus else "all"
    if fmt == "json":
        return Response(content=json.dumps(scoped), media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="lineage-{focus_bit}-{date_bit}.json"'})

    profiles = await run_in_threadpool(lineage_export.collect_profiles, db, dashboards)
    if fmt == "md":
        body = lineage_export.to_markdown(scoped, profiles=profiles)
        return Response(content=body, media_type="text/markdown", headers={
            "Content-Disposition": f'attachment; filename="lineage-{focus_bit}-{date_bit}.md"'})
    body = lineage_export.to_mermaid(scoped, profiles=profiles)
    return Response(content=body, media_type="text/plain", headers={
        "Content-Disposition": f'attachment; filename="lineage-{focus_bit}-{date_bit}.mmd"'})
