from __future__ import annotations

import json
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
ROLE_ADMINISTRATOR = "administrator"
ROLE_SUPERUSER = "superuser"
ROLE_VISITOR = "visitor"
ROLES = [ROLE_ADMINISTRATOR, ROLE_SUPERUSER, ROLE_VISITOR]

ROLE_LABELS = {
    ROLE_ADMINISTRATOR: "Administrator",
    ROLE_SUPERUSER: "Super User",
    ROLE_VISITOR: "Visitor",
}

# Permissions granted to each role
ROLE_PERMISSIONS: dict[str, set[str]] = {
    ROLE_ADMINISTRATOR: {
        "manage_users", "view_admin", "view_audit_log",
        "upload_dashboard", "manage_any_dashboard",
        "delete_any_dashboard", "view_any_dashboard",
        "manage_any_permissions", "update_any_data",
        "manage_generators",
    },
    ROLE_SUPERUSER: {
        "upload_dashboard", "manage_own_dashboard",
        "delete_own_dashboard", "view_granted_dashboard",
        "manage_own_permissions", "update_own_data",
    },
    ROLE_VISITOR: {
        "view_granted_dashboard",
    },
}


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())


# ---------------------------------------------------------------------------
# Dashboard types
# ---------------------------------------------------------------------------
DASH_INTERACTIVE = "interactive"
DASH_SNAPSHOT = "snapshot"
DASH_STATIC = "static"
# Navigator — a hub page: a person writes HTML with links to
# OnePage reports, to Power BI and to network folders, and the platform makes
# those links WORK (see `detector.inject_link_bridge`). It is a value of the
# existing `dash_type` column, not a new entity: a navigator needs exactly
# the same access, versions, folders and audit as a report, and giving it a second
# implementation would mean creating a second place where access could diverge.
DASH_PAGE = "page"


# ---------------------------------------------------------------------------
# Generator run statuses (docs/RFC_GENERATORS_RESEARCH.md, App. E)
# ---------------------------------------------------------------------------
RUN_RUNNING = "running"
RUN_SUCCESS = "success"
RUN_ERROR = "error"
RUN_CANCELED = "canceled"
# Declared by the NEXT run attempt when a `running` row outlives the lock TTL —
# enforced by the read path, no sweeper thread (same discipline as sessions).
RUN_LOST = "lost"


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # What the IdP actually sends (`name@example.com`), kept next to the canonical
    # numeric id so the same person never becomes two accounts again
    # (app/services/identity.py, 2026-08-28).
    upn: Mapped[str | None] = mapped_column(String(255), index=True)
    display_name: Mapped[str | None] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=ROLE_VISITOR)
    # Interface language (app/i18n.py LANGS). NULL = never chosen: the request
    # falls back to the op_lang cookie, then Accept-Language, then ru.
    lang: Mapped[str | None] = mapped_column(String(8))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    last_login: Mapped[datetime | None] = mapped_column(DateTime)

    owned_dashboards: Mapped[list["Dashboard"]] = relationship(
        back_populates="owner", foreign_keys="Dashboard.owner_id"
    )
    permissions: Mapped[list["DashboardPermission"]] = relationship(
        back_populates="user", foreign_keys="DashboardPermission.user_id"
    )


class Folder(Base):
    """Shared, single-level dashboard folders. Everyone sees them; only
    superusers/administrators (upload_dashboard permission) manage them."""
    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    dashboards: Mapped[list["Dashboard"]] = relationship(back_populates="folder")


class Dashboard(Base):
    __tablename__ = "dashboards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    slug: Mapped[str | None] = mapped_column(String(256), unique=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    dash_type: Mapped[str] = mapped_column(String(32), default=DASH_STATIC)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    slot_count: Mapped[int] = mapped_column(Integer, default=0)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id"), index=True)
    # Generator-backed dashboard (pilot is 1:1). A run always takes the
    # generator's CURRENT version; the run row records which one actually ran.
    generator_id: Mapped[int | None] = mapped_column(ForeignKey("generators.id"), index=True)
    # Bytes of the injected /render output: estimated at upload/mutation,
    # replaced with the exact value on the next real render. Lets the index
    # ship preview weights with zero disk I/O per request.
    render_size: Mapped[int | None] = mapped_column(Integer)
    # Row-level security policy (ADR-18), JSON or NULL. NULL = ordinary
    # report. Non-NULL = every slot file is filtered per viewer by
    # app/services/rls.py and share links / raw-source access are closed.
    # Set from the generator manifest's `rls` block at publish, or by the
    # owner/admin via PUT /api/dashboards/{id}/rls. Lives on the row on
    # purpose: the hot path already loads the Dashboard, so the check costs
    # no extra query for ordinary reports.
    rls_json: Mapped[str | None] = mapped_column(Text)
    # "Strictly confidential": hash of the password that opens the report as a SECOND
    # lock on top of the access list. Non-NULL = mode enabled; there is deliberately no
    # separate boolean flag — the two fields would drift apart (exactly the same
    # discipline as `rls_json` above). The hash is PBKDF2 from `app/auth/local.py`:
    # here, unlike the device key, what is hashed is a password INVENTED by a person,
    # which is dictionary-attackable, so key stretching is needed.
    # What exactly gets locked together with the password — `app/services/confidential.py`.
    conf_hash: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now(), index=True)

    folder: Mapped["Folder | None"] = relationship(back_populates="dashboards")
    owner: Mapped["User | None"] = relationship(back_populates="owned_dashboards", foreign_keys=[owner_id])
    versions: Mapped[list["DashboardVersion"]] = relationship(back_populates="dashboard", cascade="all, delete-orphan")
    files: Mapped[list["DashboardFile"]] = relationship(back_populates="dashboard", cascade="all, delete-orphan")
    permissions: Mapped[list["DashboardPermission"]] = relationship(back_populates="dashboard", cascade="all, delete-orphan")
    share_tokens: Mapped[list["ShareToken"]] = relationship(back_populates="dashboard", cascade="all, delete-orphan")

    @property
    def rls_enabled(self) -> bool:
        return bool(self.rls_json)

    @property
    def confidential(self) -> bool:
        return bool(self.conf_hash)

    def get_rls(self) -> dict | None:
        if not self.rls_json:
            return None
        try:
            v = json.loads(self.rls_json)
        except (ValueError, TypeError):
            return None
        return v if isinstance(v, dict) else None


class DashboardVersion(Base):
    __tablename__ = "dashboard_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dashboard_id: Mapped[str] = mapped_column(ForeignKey("dashboards.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    html_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    note: Mapped[str | None] = mapped_column(Text)

    dashboard: Mapped["Dashboard"] = relationship(back_populates="versions")


class DashboardFile(Base):
    __tablename__ = "dashboard_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dashboard_id: Mapped[str] = mapped_column(ForeignKey("dashboards.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    dashboard: Mapped["Dashboard"] = relationship(back_populates="files")

    __table_args__ = (
        UniqueConstraint("dashboard_id", "version_number", "slot_index"),
    )


class DashboardPermission(Base):
    __tablename__ = "dashboard_permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dashboard_id: Mapped[str] = mapped_column(ForeignKey("dashboards.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    can_update_data: Mapped[bool] = mapped_column(Boolean, default=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    dashboard: Mapped["Dashboard"] = relationship(back_populates="permissions")
    user: Mapped["User"] = relationship(back_populates="permissions", foreign_keys=[user_id])

    __table_args__ = (UniqueConstraint("dashboard_id", "user_id"),)


class ShareToken(Base):
    __tablename__ = "share_tokens"

    token: Mapped[str] = mapped_column(String(36), primary_key=True)
    dashboard_id: Mapped[str] = mapped_column(ForeignKey("dashboards.id"), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    label: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    dashboard: Mapped["Dashboard"] = relationship(back_populates="share_tokens")


class Generator(Base):
    """A registered report generator (Python code businesses run from the web
    UI — docs/RFC_GENERATORS_RESEARCH.md). Registration is admin-only
    (manage_generators); code arrives as an uploaded bundle or a Git link."""
    __tablename__ = "generators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    # Git-mode registration (R4b). The token is WRITE-ONLY: API responses may
    # expose only git_token_set — never the value itself.
    git_repo: Mapped[str | None] = mapped_column(String(512))
    git_branch: Mapped[str | None] = mapped_column(String(128))
    git_token: Mapped[str | None] = mapped_column(String(512))
    git_last_synced_sha: Mapped[str | None] = mapped_column(String(64))
    git_last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    git_last_sync_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    versions: Mapped[list["GeneratorVersion"]] = relationship(
        back_populates="generator", cascade="all, delete-orphan"
    )


class GeneratorVersion(Base):
    """Immutable (R7): a row is written once and never mutated afterwards; the
    only moving part is the is_current pointer. manifest_json is the frozen
    snapshot taken at registration, including the server-computed wasm32
    status of every dependency."""
    __tablename__ = "generator_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generator_id: Mapped[int] = mapped_column(ForeignKey("generators.id"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)  # bundle | git
    bundle_path: Mapped[str] = mapped_column(Text, nullable=False)  # relative to uploads_dir
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    # Frozen, normalized semantics.json (ADR-13) — NULL for bundles without one.
    semantics_json: Mapped[str | None] = mapped_column(Text)
    git_commit_sha: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    generator: Mapped["Generator"] = relationship(back_populates="versions")

    __table_args__ = (UniqueConstraint("generator_id", "version_number"),)

    def get_manifest(self) -> dict:
        return json.loads(self.manifest_json)


class GenerationRun(Base):
    """One attempt to (re)generate a dashboard. Doubles as the run-lock: a
    `running` row younger than the TTL blocks new runs with 409."""
    __tablename__ = "generation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dashboard_id: Mapped[str] = mapped_column(ForeignKey("dashboards.id"), nullable=False, index=True)
    # Which immutable version ACTUALLY executed (the start resolves the
    # generator's current version, like the launcher resolves a branch).
    generator_version_id: Mapped[int] = mapped_column(ForeignKey("generator_versions.id"), nullable=False)
    # browser | agent | local. `local` — a native run of the desktop agent from
    # folders (folders.ini): the same R2 contract as the browser, only with real
    # Python. It is OBSERVABLE, unlike `agent` (SQL in a subprocess).
    engine: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RUN_RUNNING)
    started_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    input_manifest: Mapped[str | None] = mapped_column(Text)  # JSON: files+sizes+mtimes
    error_message: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    output_version_number: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    bytes_published: Mapped[int | None] = mapped_column(Integer)
    # JSON: {"write_back": [{name, size, targets: [folder], failed: [folder]}]}
    # — reported by the page at publish; the only server-side record of what
    # a run wrote back into the analyst's folders.
    outputs_json: Mapped[str | None] = mapped_column(Text)


class DataSource(Base):
    r"""A source of report input, promoted from a string to an object.

    The Data Sources page used to derive sources from run history alone, which
    made a source a BASENAME and nothing more — two folders called Stock_RDC on
    two machines were indistinguishable, and there was nowhere to hang a
    setting. This table is what a source becomes once someone needs to manage
    it: exclude junk from it, correct its identity, say what it actually is.

    `key_name` is what the platform observes — a folder's basename from the
    File System Access API, or a GreenPlum connection alias. It stays the join
    key against run history, so an entry can exist before anyone edits it.

    `declared_path` is what a HUMAN states: `\\share\OPS\RDC\Stock_RDC`. The
    browser cannot discover a path (the API exposes none) and cannot verify
    this one, but it does not need to — a stated path gives the source a real
    identity, which basename matching never could. Two entries may share a
    key_name and be told apart by their declared paths.

    `exclude_rules` is a JSON list of glob patterns applied when the engine
    walks the folder, so junk stops being read instead of being read and then
    ignored. Patterns match the path RELATIVE to the source root, which today
    is just the filename (the walk is flat), and keeps working unchanged if the
    walk ever descends.
    """
    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)   # folder | greenplum
    key_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    declared_path: Mapped[str | None] = mapped_column(String(1024))
    # What the app SENT, as opposed to declared_path, which a person
    # confirms by hand: the absolute root path and its fingerprint
    # (`agent/folders.identity`). The browser engine fundamentally doesn't know them
    # (the File System Access API doesn't expose paths), so both columns are empty for all
    # old sources; the latest observation overwrites the previous one.
    root_path: Mapped[str | None] = mapped_column(String(512))
    root_id: Mapped[str | None] = mapped_column(String(32))
    label: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    # DEPRECATED: folder-level rules. Migrated at startup into input_overrides
    # (generator x source) and set to NULL; kept so an older build can still
    # read the column. Remove once every deployment has booted the migration.
    exclude_rules: Mapped[str | None] = mapped_column(Text)
    # Optimistic concurrency for PUT: the client sends the version it edited,
    # a mismatch is a 409, never a silent overwrite of someone else's claim.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Summary of the latest observation, maintained on ingest so the catalogue
    # page can sort and filter without touching source_observations.
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    last_file_count: Mapped[int | None] = mapped_column(Integer)
    # Data Quality thresholds, per source (Data Quality / evaluator). NULL means
    # "use the evaluator's default" — a source that nobody has tuned should not
    # look as if someone chose its numbers. `thresholds_json` holds the rest:
    # {"min_rows": {"<match>/<sheet>": N}, "drop_pct": 50}. Kept as one JSON
    # column rather than a table because the keys are per-generator sheet paths
    # that only the evaluator interprets; the DB never joins on them.
    stale_after_days: Mapped[int | None] = mapped_column(Integer)
    min_files: Mapped[int | None] = mapped_column(Integer)
    thresholds_json: Mapped[str | None] = mapped_column(Text)
    # Impact Analysis addendum: who answers for this source when a schema
    # finding fires. NULL is a real state, not a gap to fill in — it is the
    # "shared, nobody claimed it" tile the Data Quality page shows, and
    # blast-radius counting reads it as-is rather than guessing an owner.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (UniqueConstraint("kind", "key_name", name="uq_data_sources_kind_key"),)

    def get_thresholds(self) -> dict:
        """{"min_rows": {path: int}, "drop_pct": int|None} — always both keys,
        so readers never branch on shape. Malformed JSON (a hand edit, an
        older build) reads as empty rather than raising in a page render."""
        out: dict = {"min_rows": {}, "drop_pct": None}
        if not self.thresholds_json:
            return out
        try:
            v = json.loads(self.thresholds_json)
        except (ValueError, TypeError):
            return out
        if not isinstance(v, dict):
            return out
        mr = v.get("min_rows")
        if isinstance(mr, dict):
            out["min_rows"] = {str(k): int(n) for k, n in mr.items()
                               if isinstance(k, str) and isinstance(n, int) and not isinstance(n, bool)}
        dp = v.get("drop_pct")
        if isinstance(dp, int) and not isinstance(dp, bool):
            out["drop_pct"] = dp
        return out

    def set_thresholds(self, thresholds: dict | None) -> None:
        """Stores only what is set: an empty dict becomes NULL, so "cleared"
        and "never set" are the same row state."""
        t = thresholds or {}
        clean: dict = {}
        if t.get("min_rows"):
            clean["min_rows"] = dict(t["min_rows"])
        if t.get("drop_pct") is not None:
            clean["drop_pct"] = t["drop_pct"]
        self.thresholds_json = json.dumps(clean, ensure_ascii=False) if clean else None

    def get_excludes(self) -> list[str]:
        if not self.exclude_rules:
            return []
        try:
            v = json.loads(self.exclude_rules)
        except (ValueError, TypeError):
            return []
        return [str(x) for x in v if isinstance(x, str)] if isinstance(v, list) else []

    def set_excludes(self, rules: list[str]) -> None:
        self.exclude_rules = json.dumps(rules, ensure_ascii=False)


class SourceObservation(Base):
    """What ONE run saw in ONE source. Sources used to be derived from run
    history at page-render time — a basename pulled out of `input_manifest`
    JSON — which made them vanish when a report switched folders and left
    nowhere to attach a measurement. An observation is the durable record:
    the run, the source, and the measurements the engine took. History is
    the list of these rows; identity drift is visible as a fingerprint that
    changes while the basename stays."""
    __tablename__ = "source_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("generation_runs.id"), nullable=False, index=True)
    data_source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"), nullable=False, index=True)
    dir_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    newest_mtime: Mapped[datetime | None] = mapped_column(DateTime)
    # sha256 over the sorted, case-folded file-name set: the only identity the
    # browser can observe for a folder. Two readers of "Stock_RDC" whose
    # fingerprints never overlap are reading two different folders.
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    skipped_json: Mapped[str | None] = mapped_column(Text)     # [{name, reason}] for this source
    # The per-run structure budget was hit: schema_json of later files is NULL
    # because the engine stopped sniffing, not because the files were unchanged.
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (UniqueConstraint("run_id", "data_source_id", name="uq_source_obs_run_source"),)


FILE_KINDS = ("listed", "parsed", "cached", "service", "unreadable", "excluded", "legacy")


class SourceFile(Base):
    """One file inside one observation.

    `kind` starts as "listed" when the run begins (the page has only walked
    the folder) and is upgraded by the worker as it streams: what `add_file`
    returned, or why the file never reached it.

    `schema_json` is the workbook's observed structure — sheet names, the
    header row, a row count — captured by the engine BEFORE `add_file`, so it
    exists even for files the generator short-circuits as "cached". NULL with
    kind != "legacy" means "unchanged since the previous observation of this
    source" (the engine skips files whose name/size/mtime it already knows);
    readers resolve it by walking back.

    `consumed_json` is what the generator's code ACTUALLY opened, as seen by
    the read hooks: {"sheets": {name: {"columns": [...], "all": bool}}}.
    NULL for cached files (never opened), for readers the hooks do not cover,
    and for every run on the agent engine. Sheet-level is all python-calamine
    can yield; column-level only appears through pandas.read_excel."""
    __tablename__ = "source_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observation_id: Mapped[int] = mapped_column(ForeignKey("source_observations.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Full path of the file read, as the app saw it. NULL for everything
    # that came from the browser. Shown only to those who already see the source
    # pages; never goes into prompts or onto share surfaces.
    path: Mapped[str | None] = mapped_column(String(512))
    size: Mapped[int | None] = mapped_column(Integer)
    mtime_ms: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="listed")
    matched_input: Mapped[str | None] = mapped_column(String(128))   # the inputs[].match that admitted it
    schema_json: Mapped[str | None] = mapped_column(Text)
    consumed_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_source_files_obs_name", "observation_id", "name"),)


class RunStage(Base):
    """One `onepage.stage(name, df)` call inside a run — the generator's own
    intermediate DataFrame, snapshotted where the code says it matters.

    Everything else in this file records what the run READ (files, sheets,
    columns); this is the first row that records what the run BUILT on the way
    to its output. It is declared nowhere: there is no manifest contract for
    stages, the order is simply the order the calls happened in, and a
    generator that never calls `stage()` has none. That is deliberate — a
    stage is a comment the author writes in code, next to the transformation
    it describes, and a second place to keep it in sync would rot.

    Stored per RUN, not per generator version: the shape of an intermediate
    frame is a fact about one execution over one set of files, the same way a
    SourceFile's schema is. `order` is the 0-based call index and is the
    node's identity in the lineage graph (`stage:{generator_id}:{order}`);
    UNIQUE(run_id, order) with a replace-all-per-run ingest is what makes a
    re-sent observation batch idempotent, exactly like quality_results.

    `inputs_json` is the author's own upstream declaration (`inputs=[...]`,
    stage names) or NULL, which the graph reads as "the previous stage".
    `read_files_json` is what the engine's hooks saw being added BETWEEN this
    stage call and the one before it — the read-order edge from a file to the
    step that consumed it, which nothing else in the schema can express.
    """
    __tablename__ = "run_stages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("generation_runs.id"), nullable=False, index=True)
    # "order" is a SQL keyword; SQLAlchemy quotes it. Kept as the contract
    # names it (the engine sends `order`) rather than renamed here.
    order: Mapped[int] = mapped_column("order", Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    rows: Mapped[int | None] = mapped_column(Integer)
    columns_json: Mapped[str | None] = mapped_column(Text)     # ["col", ...]
    profiles_json: Mapped[str | None] = mapped_column(Text)    # [{column, type, ...}]
    inputs_json: Mapped[str | None] = mapped_column(Text)      # ["stage", ...] | NULL
    read_files_json: Mapped[str | None] = mapped_column(Text)  # ["file.xlsx", ...]
    ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (UniqueConstraint("run_id", "order", name="uq_run_stage_order"),)

    def _list(self, raw: str | None) -> list:
        if not raw:
            return []
        try:
            v = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return v if isinstance(v, list) else []

    def get_columns(self) -> list[str]:
        return [str(x) for x in self._list(self.columns_json) if isinstance(x, str)]

    def get_profiles(self) -> list[dict]:
        return [x for x in self._list(self.profiles_json) if isinstance(x, dict)]

    def get_read_files(self) -> list[str]:
        return [str(x) for x in self._list(self.read_files_json) if isinstance(x, str)]

    def get_inputs(self) -> list[str] | None:
        """NULL and [] are DIFFERENT here: NULL means the author declared no
        upstream (the graph falls back to the previous stage), [] means they
        declared explicitly that there is none (a source stage)."""
        if self.inputs_json is None:
            return None
        return [str(x) for x in self._list(self.inputs_json) if isinstance(x, str)]


class InputOverride(Base):
    """Operational exclusions, on the (generator, source) pair.

    A rule like "*_template.xlsx is junk" is knowledge about what a GENERATOR
    can read, applied to a particular folder — not a property of the folder.
    The same generator over a different folder wants the same rule; another
    generator over the same folder may need exactly that file. The first
    version of this feature stored rules on the folder and shipped them to
    every generator in the instance; this is the correction."""
    __tablename__ = "input_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generator_id: Mapped[int] = mapped_column(ForeignKey("generators.id"), nullable=False, index=True)
    data_source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"), nullable=False, index=True)
    exclude_rules: Mapped[str | None] = mapped_column(Text)          # JSON list of globs
    # {"<match>/<sheet>": {"<declared column>": "<observed column>"}} — a
    # folder whose header says "SKU_Discription" where the manifest declares
    # "SKU Description". Declared -> observed, on the (generator, source) pair
    # for the same reason exclude_rules are: the typo is in THIS folder, the
    # declaration belongs to THIS generator. schemadiff reads it to count the
    # column as found; nothing renames data.
    column_aliases: Mapped[str | None] = mapped_column(Text)
    # {"<match>": ["<file name>", …]} — "THIS file is that very
    # input". A separate column rather than an entry in `column_aliases` under a service
    # sheet name: such an entry is inert for schemadiff and would work, but
    # in half a year nobody would understand what lives in the column about columns.
    #
    # The name is LITERAL, not a mask: an alias NAMES a file, it doesn't widen the glob,
    # so no fourth "what was read" semantics appears and globmatch
    # stays the only answer. Same pair (generator × source): Anya
    # renamed a file in THIS folder, and the input is declared by THIS generator.
    file_aliases: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (UniqueConstraint("generator_id", "data_source_id", name="uq_input_override_gen_source"),)

    def get_aliases(self) -> dict[str, dict[str, str]]:
        if not self.column_aliases:
            return {}
        try:
            v = json.loads(self.column_aliases)
        except (ValueError, TypeError):
            return {}
        if not isinstance(v, dict):
            return {}
        return {
            str(k): {str(a): str(b) for a, b in m.items() if isinstance(a, str) and isinstance(b, str)}
            for k, m in v.items() if isinstance(k, str) and isinstance(m, dict)
        }

    def set_aliases(self, aliases: dict[str, dict[str, str]] | None) -> None:
        # Empty sheets are dropped and an empty map is NULL, so "no aliases"
        # has exactly one representation.
        clean = {k: dict(m) for k, m in (aliases or {}).items() if m}
        self.column_aliases = json.dumps(clean, ensure_ascii=False) if clean else None

    def get_excludes(self) -> list[str]:
        if not self.exclude_rules:
            return []
        try:
            v = json.loads(self.exclude_rules)
        except (ValueError, TypeError):
            return []
        return [str(x) for x in v if isinstance(x, str)] if isinstance(v, list) else []

    def set_excludes(self, rules: list[str]) -> None:
        self.exclude_rules = json.dumps(rules, ensure_ascii=False)

    def get_file_aliases(self) -> dict[str, list[str]]:
        if not self.file_aliases:
            return {}
        try:
            v = json.loads(self.file_aliases)
        except (ValueError, TypeError):
            return {}
        if not isinstance(v, dict):
            return {}
        return {str(k): [str(n) for n in names if isinstance(n, str)]
                for k, names in v.items()
                if isinstance(k, str) and isinstance(names, list)}

    def set_file_aliases(self, aliases: dict[str, list[str]] | None) -> None:
        # An empty list of names means "no alias", and it has exactly one
        # representation (NULL), like column_aliases above.
        clean = {k: list(v) for k, v in (aliases or {}).items() if v}
        self.file_aliases = json.dumps(clean, ensure_ascii=False) if clean else None


class QualityResult(Base):
    """One check (or one column profile) evaluated against ONE observed file.

    The engine runs the manifest's `checks` in the browser while it already
    has the workbook open; this row is what comes back. Storing per FILE —
    not per run — is what lets the evaluator say "since run N" for a finding:
    the same check_id on successive observations of the same filename is the
    history. Profiles share the table as `check_type="profile"` (metric_json =
    the profile) because they are produced by the same pass and expire with
    the same file; a second table would only duplicate the FK and the delete.

    UNIQUE(source_file_id, check_id) makes ingest idempotent: a re-sent batch
    replaces, never doubles. `sampled` is the honesty flag — the engine stops
    at MAX_CHECK_ROWS, and a "pass" over a sample is not a pass over the file.
    """
    __tablename__ = "quality_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id"), nullable=False, index=True)
    observation_id: Mapped[int] = mapped_column(ForeignKey("source_observations.id"), nullable=False, index=True)
    check_id: Mapped[str] = mapped_column(String(256), nullable=False)   # "{type}:{match}:{sheet}:{column}"
    check_type: Mapped[str] = mapped_column(String(16), nullable=False)  # genmanifest.CHECK_TYPES | profile
    sheet: Mapped[str | None] = mapped_column(String(64))
    column: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(8), nullable=False)       # pass | fail | skipped
    count: Mapped[int | None] = mapped_column(Integer)                   # rows examined
    failed: Mapped[int | None] = mapped_column(Integer)                  # rows that failed
    sampled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metric_json: Mapped[str | None] = mapped_column(Text)                # examples/distinct/profile
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (UniqueConstraint("source_file_id", "check_id", name="uq_quality_result_file_check"),)

    def get_metric(self) -> dict:
        if not self.metric_json:
            return {}
        try:
            v = json.loads(self.metric_json)
        except (ValueError, TypeError):
            return {}
        return v if isinstance(v, dict) else {}

    def set_metric(self, data: dict | None) -> None:
        self.metric_json = json.dumps(data, ensure_ascii=False) if data else None


class ValueBaseline(Base):
    """The accepted set of values for a `known_values` check — the reference
    list a column is compared against. It lives on the SERVER and not in the
    manifest because the list changes without the code changing (a new branch
    opens), and the people who know a value is legitimate are the ones who run
    the report, not the ones who wrote the generator. Keyed by (source,
    generator) like input_overrides: the same column in another folder may
    legitimately hold other values.

    `seeded_run_id` marks a baseline the ingest created from the FIRST run's
    distinct values (no alerts on day one — there is nothing to compare to
    yet); `accepted_by/at` mark a human decision. `version` is the optimistic
    lock for accepting new values: a 409 means someone else already accepted.
    """
    __tablename__ = "value_baselines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data_source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"), nullable=False, index=True)
    generator_id: Mapped[int] = mapped_column(ForeignKey("generators.id"), nullable=False, index=True)
    match: Mapped[str] = mapped_column(String(128), nullable=False)    # inputs[].match
    sheet: Mapped[str] = mapped_column(String(64), nullable=False)
    column: Mapped[str] = mapped_column(String(64), nullable=False)
    values_json: Mapped[str] = mapped_column(Text, nullable=False)     # sorted unique list
    seeded_run_id: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    accepted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint("data_source_id", "generator_id", "match", "sheet", "column",
                         name="uq_value_baseline"),
    )

    def get_values(self) -> list[str]:
        if not self.values_json:
            return []
        try:
            v = json.loads(self.values_json)
        except (ValueError, TypeError):
            return []
        return [str(x) for x in v if isinstance(x, str)] if isinstance(v, list) else []

    def set_values(self, values) -> None:
        # Sorted + unique so two baselines with the same members serialise
        # identically — diffs and the 409 `current` payload stay readable.
        self.values_json = json.dumps(sorted({str(x) for x in values}), ensure_ascii=False)


class QualityAck(Base):
    """A human saying "I know" about one finding. `finding_id` is the
    evaluator's stable sha1 prefix (kind|source|generator|file|sheet|column),
    not an FK — findings are computed on read, nothing to point at. The ack
    is bounded by `until_run_id` (until the next run reproduces it) or
    `until_at` (N days); both NULL is open-ended. Acked findings stay listed,
    they just stop counting as open. `data_source_id` is denormalised for the
    authorisation check (reach over the source) without re-evaluating."""
    __tablename__ = "quality_acks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    finding_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    data_source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id"))
    until_run_id: Mapped[int | None] = mapped_column(Integer)
    until_at: Mapped[datetime | None] = mapped_column(DateTime)
    note: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class Annotation(Base):
    """A human-written note on one sheet or one column of a data source's
    declared inputs — "what is this, actually" for the analysts who read
    lineage/Data Quality but did not write the generator. Distinct from a
    manifest column `description` (P3.6): the manifest is the AUTHOR's
    statement, frozen per generator version; an annotation is the DATA
    OWNER's statement, keyed to the source and editable independently of any
    registration. Where both exist, the caller decides which wins (lineage
    prefers the annotation — it is the more specific, more current voice).

    `match` is the generator's `inputs[].match` pattern, `sheet` a sheet name
    (never a glob here — an annotation is written against what a run actually
    observed, not a declaration). `column` NULL means "about the sheet as a
    whole"; a sheet-level row and a column-level row on the same sheet are
    different rows because `column` differs (NULL vs a name) — see the UNIQUE
    note below for why the sheet-level row is NOT NULL in the column slot.

    UNIQUE(data_source_id, match, sheet, column): SQLite treats NULL as
    DISTINCT from every other NULL in a UNIQUE index (it is not equal to
    itself), so a real NULL here would let a caller upsert the same
    sheet-level annotation twice, unconstrained. The fix is to never store
    NULL for a sheet-level row — `""` (empty string) fills the column slot
    instead, and `""` is never a valid column name (columns are validated
    non-empty everywhere else in this codebase), so it cannot collide with a
    real column annotation. `column` is still typed nullable at the ORM level;
    the API layer maps "" <-> None at the boundary (app/api/generators.py) so
    nothing outside this module has to know about the workaround."""
    __tablename__ = "annotations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data_source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"), nullable=False, index=True)
    match: Mapped[str] = mapped_column(String(128), nullable=False)
    sheet: Mapped[str] = mapped_column(String(64), nullable=False)
    # "" (not NULL) means "sheet-level" — see class docstring.
    column: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    tags_json: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint("data_source_id", "match", "sheet", "column", name="uq_annotation"),
    )

    def get_tags(self) -> list[str]:
        if not self.tags_json:
            return []
        try:
            v = json.loads(self.tags_json)
        except (ValueError, TypeError):
            return []
        return [str(x) for x in v if isinstance(x, str)] if isinstance(v, list) else []

    def set_tags(self, tags: list[str] | None) -> None:
        self.tags_json = json.dumps(list(tags), ensure_ascii=False) if tags else None


class CatalogTable(Base):
    r"""One canonical table inferred from what the engine actually observed.

    Not a declaration and not a guess presented as a fact: ADR-12 (G1/G4
    failed on production) makes this a SUGGESTER, so a row is born `inferred`
    and only a human turns it into `confirmed` or `rejected`. `/explore` and
    `onepage.table()` read confirmed rows only.

    `table_id` is the stable identity — `"t_" + sha1(f"{ds}|{name}|{sha1(core
    cols)}")[:12]`, the same trick as `quality.finding_id`: the same table
    carries the same id across rebuilds, and a change of the CORE columns
    mints a new table whose predecessor points at it through `superseded_by`.
    A change outside the core is the same table with a new column list.

    Which fields are whose is the whole contract with `catalog.rebuild()`:
    `status`, `owner_id` and `confirmed_by/at` are only ever written by a
    human, `name` is inference-owned (it feeds `table_id`), and
    `display_name` becomes human-owned once somebody edits the row —
    `version` (the optimistic lock the API bumps and rebuild never does) is
    what says so.

    `evidence_json` is at most 20 `{source_file_id, run_id, sim, file, sheet}`
    records: every row in this table can answer "why do you think so".
    """
    __tablename__ = "catalog_tables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[str] = mapped_column(String(16), nullable=False, unique=True, index=True)
    data_source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"), nullable=False, index=True)
    # snake_case identifier derived from the file-name stem; unique per source
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    name_stem: Mapped[str | None] = mapped_column(String(128))
    sheets_json: Mapped[str | None] = mapped_column(Text)      # ["TDSheet", ...]
    core_cols_json: Mapped[str | None] = mapped_column(Text)   # normalised names in >=80 % of files
    # [{norm, display, present_ratio, type}] — `norm` joins against Data
    # Quality (same schemadiff.norm), `display` is what the workbook says.
    columns_json: Mapped[str | None] = mapped_column(Text)
    header_row: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence_json: Mapped[str | None] = mapped_column(Text)
    n_files: Mapped[int | None] = mapped_column(Integer)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    # NOT refreshed when a table drops out of the inference — an untouched
    # last_seen_at IS the staleness signal the page reads.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="inferred")
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    superseded_by: Mapped[str | None] = mapped_column(String(16))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    def _list(self, raw: str | None) -> list:
        if not raw:
            return []
        try:
            v = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return v if isinstance(v, list) else []

    def get_sheets(self) -> list[str]:
        return [str(x) for x in self._list(self.sheets_json) if isinstance(x, str)]

    def get_core_cols(self) -> list[str]:
        return [str(x) for x in self._list(self.core_cols_json) if isinstance(x, str)]

    def get_columns(self) -> list[dict]:
        return [x for x in self._list(self.columns_json) if isinstance(x, dict)]

    def get_evidence(self) -> list[dict]:
        return [x for x in self._list(self.evidence_json) if isinstance(x, dict)]


class CatalogPartition(Base):
    """One FILE inside one inferred table, plus the key its name yields.

    `key` is "" and not NULL when a file yields no key: SQLite treats NULLs as
    distinct inside a UNIQUE index, so a NULL would let the next rebuild
    insert the same file again. Two files under one key with different
    (size, mtime) are a re-issue — reported, never silently merged."""
    __tablename__ = "catalog_partitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # A table may span several sheets of ONE workbook (prod: the `Check`
    # family in `ExRDC postDFM'05.xlsb`), so the sheet is part of the key.
    sheet: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    size: Mapped[int | None] = mapped_column(Integer)
    mtime_ms: Mapped[int | None] = mapped_column(Integer)
    rows: Mapped[int | None] = mapped_column(Integer)
    last_run_id: Mapped[int | None] = mapped_column(Integer)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("table_id", "key", "file_name", "sheet", name="uq_catalog_partition"),
    )


class CatalogConsumer(Base):
    """A generator that reads an inferred table. Derived from the observation
    chain, so it is what a generator DOES read, not what it declares —
    `matched_input` carries the declared pattern when there is one."""
    __tablename__ = "catalog_consumers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    generator_id: Mapped[int] = mapped_column(ForeignKey("generators.id"), nullable=False, index=True)
    matched_input: Mapped[str | None] = mapped_column(String(128))
    last_run_id: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("table_id", "generator_id", name="uq_catalog_consumer"),
    )


class CatalogNonTable(Base):
    """A sheet the inference refused to treat as a table — a pivot corner or a
    fragment. Kept, not discarded: production is full of these (68 of 417
    units in the Leg 0 run) and every one is a sheet a user will open
    expecting a table. This is the corpus behind "Header not recognized"."""
    __tablename__ = "catalog_nontables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data_source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"), nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sheet: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(16), nullable=False)     # pivot | fragment
    reason: Mapped[str | None] = mapped_column(String(255))
    headers_json: Mapped[str | None] = mapped_column(Text)            # at most 16
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("data_source_id", "file_name", "sheet", name="uq_catalog_nontable"),
    )


class ExploreQuestion(Base):
    """A saved `/explore` question: the natural-language text (if any), the
    SQL that answered it, and which tables it targets. Owned by the user who
    saved it; `shared` makes it visible (read-only) to every other user, the
    same "explicit opt-in to cross visibility" the plan's Leg 3 section
    requires for anything that leaves one person's view.

    Deliberately thin: RESULTS never land here — only the recipe. Re-running
    a saved question re-executes its `sql` against whatever the viewer's own
    browser has indexed, so two users can save the same question and see
    different (or differently-stale) answers, which is the honest outcome
    under a client-side-only lake (no ingestion, no shared source of truth)."""
    __tablename__ = "explore_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    question: Mapped[str | None] = mapped_column(String(2000))
    sql: Mapped[str] = mapped_column(String(20000), nullable=False)
    table_ids_json: Mapped[str | None] = mapped_column(Text)
    shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    def get_table_ids(self) -> list[str]:
        if not self.table_ids_json:
            return []
        try:
            v = json.loads(self.table_ids_json)
        except (ValueError, TypeError):
            return []
        return [str(x) for x in v if isinstance(x, str)] if isinstance(v, list) else []

    def set_table_ids(self, ids) -> None:
        self.table_ids_json = json.dumps([str(x) for x in ids], ensure_ascii=False)


class GlossaryTerm(Base):
    """Platform glossary (H2): terms operators use ("SL", "hub", "network") that
    may belong to one report generator or to the company as a whole.
    `generator_id` NULL means company-wide — visible to every user, folded
    into every generator's NL->SQL/intent prompt; a non-NULL value scopes the
    term to that generator, reach-gated the same way its dashboards are
    (`_generator_dashboards`). Unique on (term, generator_id) so the same
    word can mean one thing company-wide and something else inside one
    generator's own vocabulary, but never two definitions in the same scope."""
    __tablename__ = "glossary_terms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    term: Mapped[str] = mapped_column(String(120), nullable=False)
    definition: Mapped[str] = mapped_column(String(500), nullable=False)
    generator_id: Mapped[int | None] = mapped_column(ForeignKey("generators.id"), index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (UniqueConstraint("term", "generator_id", name="uq_glossary_term_generator"),)


class Presentation(Base):
    """An ordered list of slides shown one after another (v1: owner-only).

    A `dashboard` slide is not a copy of anything — show mode routes to the
    report's own `/d/{id}`, so what the audience sees is the live report. Only
    `image`/`pdf` slides carry bytes, and those live under the uploads dir."""
    __tablename__ = "presentations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    items: Mapped[list["PresentationItem"]] = relationship(
        back_populates="presentation", cascade="all, delete-orphan",
        order_by="PresentationItem.position",
    )


PRES_DASHBOARD = "dashboard"
PRES_IMAGE = "image"
PRES_PDF = "pdf"
PRES_HTML = "html"
PRES_KINDS = (PRES_DASHBOARD, PRES_IMAGE, PRES_PDF, PRES_HTML)


class PresentationItem(Base):
    """One slide. `file_path` is RELATIVE to the uploads dir (like every other
    stored path in this app) and is written only by storage.save_presentation_file
    — never assembled from a client-supplied filename."""
    __tablename__ = "presentation_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    presentation_id: Mapped[str] = mapped_column(
        ForeignKey("presentations.id"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # dashboard | image | pdf
    dashboard_id: Mapped[str | None] = mapped_column(ForeignKey("dashboards.id"))
    file_path: Mapped[str | None] = mapped_column(String(512))
    original_name: Mapped[str | None] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)  # ≤ 2000 chars, enforced in the API
    start_section: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    presentation: Mapped["Presentation"] = relationship(back_populates="items")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)  # JSON string
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)

    # (actor_id, created_at): the quest-completion evidence scan walks one
    # actor's recent actions (QuestProfile.verified_at) — the created_at-only
    # index above would make it a full scan filtered in Python.
    __table_args__ = (
        Index("ix_audit_log_actor_created", "actor_id", "created_at"),
    )

    def set_detail(self, data: dict) -> None:
        self.detail = json.dumps(data, ensure_ascii=False)

    def get_detail(self) -> dict:
        return json.loads(self.detail) if self.detail else {}


class DashboardFavorite(Base):
    """One user's "★ Favorites" pin on one dashboard, with the user's own
    drag-and-drop order. Own-order, not shared: two users favouriting the
    same dashboard get two independent rows and two independent positions —
    `PUT /api/favorites/order` renumbers only the caller's own rows, so one
    user's reorder never touches another user's list."""
    __tablename__ = "dashboard_favorites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    dashboard_id: Mapped[str] = mapped_column(ForeignKey("dashboards.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "dashboard_id", name="uq_dashboard_favorites_user_dash"),
        Index("ix_dashboard_favorites_user_id", "user_id"),
    )


class Employee(Base):
    """HR directory row mirrored from Greenplum `warehouse.v_employees`
    (the role-relevant subset only — no birthdate, phone, gender or vacation
    fields, by owner decision 2026-08-23). Loaded by an administrator's CSV
    upload on /admin/employees; the join key to OnePage accounts is
    `person_id`. Dates stay as the source's text — nothing computes on them."""
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # Greenplum id
    person_id: Mapped[str | None] = mapped_column(String(64))
    employeeid: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(255))
    firstname_ru: Mapped[str | None] = mapped_column(String(128))
    middleinitial_ru: Mapped[str | None] = mapped_column(String(128))
    lastname_ru: Mapped[str | None] = mapped_column(String(128))
    firstname_en: Mapped[str | None] = mapped_column(String(128))
    lastname_en: Mapped[str | None] = mapped_column(String(128))
    companycode: Mapped[str | None] = mapped_column(String(64))
    companyname: Mapped[str | None] = mapped_column(String(255))
    deptcode: Mapped[str | None] = mapped_column(String(64))
    deptname_ru: Mapped[str | None] = mapped_column(String(255))
    deptname_en: Mapped[str | None] = mapped_column(String(255))
    jobtitle_ru: Mapped[str | None] = mapped_column(String(255))
    jobtitle_en: Mapped[str | None] = mapped_column(String(255))
    jobgrade: Mapped[str | None] = mapped_column(String(32))
    managerid: Mapped[str | None] = mapped_column(String(64))
    hrbpid: Mapped[str | None] = mapped_column(String(64))
    officecitycode: Mapped[str | None] = mapped_column(String(64))
    officeregion: Mapped[str | None] = mapped_column(String(128))
    officecity_ru: Mapped[str | None] = mapped_column(String(128))
    employmenttype: Mapped[str | None] = mapped_column(String(64))
    hrstatus: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    positionstartdate: Mapped[str | None] = mapped_column(String(32))
    hiredate: Mapped[str | None] = mapped_column(String(32))
    inactivationdate: Mapped[str | None] = mapped_column(String(32))
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    # Lower-cased (in Python — SQLite can't case-fold Cyrillic) names + ids,
    # written at import so the suggest endpoint narrows with LIKE in SQL.
    search_blob: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_employees_person_id", "person_id"),
    )


class LlmAccess(Base):
    """Per-user allowlist for the PAID external LLM (DeepSeek). No row = no
    DeepSeek: the server-held browser key is not handed out and the server
    path declines, whatever role the user has. Ollama (local, free) is
    unaffected. Granting is gated by `OP_LLM_ACCESS_ADMINS` — see config."""
    __tablename__ = "llm_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # unique + index: SQLAlchemy expresses that as a single UNIQUE INDEX named
    # ix_llm_access_user_id — the same object migration 0002 creates on
    # databases whose llm_access table predates it.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False,
                                         unique=True, index=True)
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class EmployeeSync(Base):
    """One row per employee import — feeds the status card on /admin/employees
    ("when it was refreshed, by whom, how many rows") without parsing the audit."""
    __tablename__ = "employee_syncs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="csv")
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deactivated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


# ---------------------------------------------------------------------------
# Reference tables (ADR-16)
# ---------------------------------------------------------------------------
class ReferenceTable(Base):
    """A platform-owned lookup — «one row per key» — that a generator
    consumes as a declared input (`source: platform`) instead of hardcoding a
    mapping in its own code. The pilot is `NWD BV → code + name`: codes arrive
    on their own schedule, the person who maps them is not the generator's
    author, and today's fallback ("show the raw value nobody mapped yet") is
    silent — this table exists so that fallback becomes a visible bucket
    instead.

    `slug` is the stable identity a manifest references (`table: <slug>`);
    its shape is enforced in the service layer (`reference.SLUG_RE`), not
    here, so a bad value fails with a service-level message rather than an
    IntegrityError. `columns_json` freezes the schema at creation — the
    CONSUMER declares the types (a manifest's `column_types()`), because it
    is the consumer who needs `text|number|date` to hold; the schema does not
    change afterwards, only the rows do (see `ReferenceTableVersion`).

    `kind` is reserved for a future "dated" table (effective-from ranges);
    `kind="simple"` is the only value v1 writes or reads — see ADR-16.
    `allow_pin` lets a run choose an older version deliberately (rollback,
    reproduce-old-numbers); when it is False every run resolves to
    `current_version`, no picker shown. A table with `responsible_user_id`
    NULL is not an error state — it is the "nobody claimed it yet" case the
    Data Quality feed watches for and nags everyone with `can_update_data` on
    a consuming report, the same shape as `DataSource.owner_id`.
    """
    __tablename__ = "reference_tables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    key_column: Mapped[str] = mapped_column(String(64), nullable=False)
    # JSON list of {"name": str, "type": "text"|"number"|"date"}; the first
    # entry is always the key column (mirrors key_column, kept for a single
    # read to get the whole ordered schema without a second column).
    columns_json: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="simple")
    allow_pin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    responsible_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    def get_columns(self) -> list[dict]:
        """Ordered [{name, type}], key column first. Malformed JSON reads as
        empty rather than raising in a page render, same convention as
        DataSource.get_thresholds/get_excludes above."""
        if not self.columns_json:
            return []
        try:
            v = json.loads(self.columns_json)
        except (ValueError, TypeError):
            return []
        if not isinstance(v, list):
            return []
        out = []
        for c in v:
            if isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("type"), str):
                out.append({"name": c["name"], "type": c["type"]})
        return out

    def set_columns(self, columns: list[dict]) -> None:
        self.columns_json = json.dumps(
            [{"name": c["name"], "type": c["type"]} for c in columns], ensure_ascii=False
        )


class ReferenceTableVersion(Base):
    """One imported snapshot of a table's rows — immutable once written, like
    `GeneratorVersion`. A version is created on EVERY import, not just when
    `allow_pin` is set (the owner's explicit call — ADR-16): the picker only
    controls whether a RUN may choose an older one, not whether history is
    kept. That makes "undo" always available as "make version N current
    again" (a new version-copy, so history stays linear — never a mutation
    of an old row).

    `rows_json` is a list of lists in the table's column order (values are
    str | float | None; a date cell is an ISO `YYYY-MM-DD` string) rather
    than a list of dicts — at up to `reference.MAX_ROWS` rows the column-name
    repetition of the dict shape would roughly double storage for no reader
    benefit, since the column order is already frozen on `ReferenceTable`.
    `sha256` is over that same canonical encoding, so two versions with
    identical data (a re-upload of the unchanged file) are cheap to compare
    without a row-by-row diff. `summary_json` keeps the diff the import
    computed (`added/changed/removed/note`) so the version list can show it
    without re-diffing against the previous version on every read.
    """
    __tablename__ = "reference_table_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("reference_tables.id"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    rows_json: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # {"added": int, "changed": int, "removed": int, "note": str|None} — the
    # diff this import produced against the previous current version.
    summary_json: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint("table_id", "version_number", name="uq_reference_table_version"),
    )


class RunReferenceUse(Base):
    """Which reference table(s) a run resolved against, and at which version
    — the record `rerun_needed` (Data Quality, ADR-16) reads to tell a stale
    publish from a current one: a run whose `version_number` is below the
    table's `current_version` is a report showing numbers a later import has
    since corrected. `version_number` NULL pairs with `applied=False` — the
    run's owner explicitly chose "without the reference table" (NWD codes
    shown raw), a legitimate mode ADR-16 requires to keep working, not a
    missing fact. `applied=True` with a version number is the normal case;
    `run_id`/`table_id` are unique together because one run resolves a given
    table at most once, however many columns of that run's output the table
    fed."""
    __tablename__ = "run_reference_uses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("generation_runs.id"), nullable=False, index=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("reference_tables.id"), nullable=False, index=True)
    version_number: Mapped[int | None] = mapped_column(Integer)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("run_id", "table_id", name="uq_run_reference_use"),
    )


# ---------------------------------------------------------------------------
# "Quests" (ADR-17) — gamification: daily openings, a card collection, quests
# and a spaced-repetition quiz. Everything here is additive and behind
# `settings.quests_enabled` — see app/services/quests_config.py for the
# runtime overlay (season dates, holidays) and app/config.py for the flag.
# ---------------------------------------------------------------------------
class QuestProfile(Base):
    """One row per user — the running score the rest of the feature reads
    and writes. `user_id` is the primary key (not a surrogate id) because a
    profile is 1:1 with a user by construction: there is never a second row
    to disambiguate, and every write is already keyed by the caller's own
    session user, so a separate `id` would only be an unused column.

    `last_open_date` and `streak`/`best_streak` implement the daily-opening
    mechanic: opening on the calendar day right after `last_open_date`
    extends the streak, opening later breaks it — unless `freeze_week`
    already names the ISO week (`2026-W36`) the break falls in, in which
    case one skipped day per week is forgiven instead of resetting to zero.
    `freeze_week` is a week key, not a boolean, so "already spent this
    week's freeze" survives a restart without a second table.

    `leaderboard_optin`/`optin_at` gate whether the user's `points` appear
    on the shared leaderboard at all (opt-in, not opt-out — nothing here is
    public by default). `verified_at` is a cursor: the audit_log scan that
    checks a completed quest's evidence against real actions only looks at
    rows newer than this timestamp, so a re-run never re-verifies the same
    history. `backfilled` marks a profile whose `points`/`streak` were
    computed retroactively from existing audit_log rows when the feature
    launched, rather than accrued going forward — kept so a support
    question ("why did I start with points already?") has an answer without
    digging through history.
    """
    __tablename__ = "quest_profiles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    best_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_open_date: Mapped[date | None] = mapped_column(Date)
    # ISO week key (e.g. "2026-W36") in which the streak freeze was spent.
    freeze_week: Mapped[str | None] = mapped_column(String(8))
    quiz_correct_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    leaderboard_optin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    optin_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Cursor for the audit_log scan that verifies completed-quest evidence —
    # only rows newer than this are re-checked on the next pass.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    backfilled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ADR-17b "Companion": the creature behind the full 8-card collection.
    # `companion_since` is stamped once, the first time the profile is read
    # with all eight cards (the intro plays off that moment); the two flags
    # are the person's own switches in the Collection block. All three are
    # nullable on purpose: SQLite adds them to a live table with a plain
    # ALTER (revision 0006), and NULL reads as «default» — never a rebuild.
    companion_since: Mapped[datetime | None] = mapped_column(DateTime)
    companion_off: Mapped[bool | None] = mapped_column(Boolean)
    autumn_theme: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class QuestOpening(Base):
    """One card-opening event. `source` distinguishes WHY a card was rolled
    — `daily` (today's free open), `quest:<id>` (a completed quest's
    reward), `quiz` (the day's quiz reward), `dup:<n>` (a duplicate-card
    compensation roll) or `milestone` (a streak/points milestone bonus) —
    so the unique constraint on `(user_id, day, source)` can enforce "at
    most one daily open per day" and "at most one quiz open per day"
    independently, while still allowing many `quest:<id>`/`dup:<n>` rows on
    the same day (each carries its own distinguishing suffix, so the triple
    stays unique per actual event).

    `seed`/`weights_json`/`roll` are kept verbatim (not just the resulting
    `card_id`/`rarity`) so a disputed drop can be replayed and shown to be
    fair — the weighting that produced it, and the random draw against it,
    both survive the event itself.
    """
    __tablename__ = "quest_openings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    card_id: Mapped[str] = mapped_column(String(16), nullable=False)
    rarity: Mapped[str] = mapped_column(String(12), nullable=False)
    seed: Mapped[str] = mapped_column(String(32), nullable=False)
    weights_json: Mapped[str] = mapped_column(Text, nullable=False)
    roll: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    # NULL = the card is rolled but not yet shown: quest/quiz/milestone/dup
    # rewards wait in the hero as a "surprise" until the person opens them
    # (the roll itself is final and audited at grant time — the reveal is
    # only theatre, so a refresh cannot re-roll).
    revealed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "day", "source", name="uq_quest_openings_user_day_source"),
        Index("ix_quest_openings_user_created", "user_id", "created_at"),
    )


class QuestCard(Base):
    """The user's collection, one row per distinct `card_id` ever drawn.
    `count` accumulates duplicates rather than a second `quest_openings` row
    per draw being the source of truth for "does the user own this card" —
    the collection page reads this table directly instead of aggregating
    the (much larger, append-only) openings log."""
    __tablename__ = "quest_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    card_id: Mapped[str] = mapped_column(String(16), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    last_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "card_id", name="uq_quest_cards_user_card"),
    )


class QuestCompletion(Base):
    """A quest the user has finished. `evidence_json` freezes what the
    verification pass (see `QuestProfile.verified_at`) saw in `audit_log`
    at completion time, so a later dispute or an audit_log retention sweep
    does not erase the reason the quest was accepted. `surprise` marks a
    quest that was not shown to the user in advance — discovered as
    "already done" from existing activity — the reward flow (and its copy)
    differs from a quest the user deliberately set out to complete."""
    __tablename__ = "quest_completions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    quest_id: Mapped[str] = mapped_column(String(24), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    evidence_json: Mapped[str | None] = mapped_column(Text)
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    surprise: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("user_id", "quest_id", name="uq_quest_completions_user_quest"),
    )


class QuizQuestion(Base):
    """One bank question, imported in bulk (`imported_at`) rather than
    authored through the app. `id` is set explicitly by the importer — it
    is the bank's own stable id, not an autoincrement surrogate, so the
    same import re-run (or a partial re-import) lands on the same rows
    instead of duplicating them. `day` is the bank day (1..15, see
    `app/config.py` season comment) a question belongs to, not a calendar
    date — `QuizAttempt.bank_day` is what maps a calendar day to one of
    these. `active` lets a bad question be pulled without deleting history
    that already references it from `QuizAttempt.q_ids_json`."""
    __tablename__ = "quiz_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[str] = mapped_column(Text, nullable=False)
    answer_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    sec: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    # {"en": {"t","q","o","e"}, "zh-HK": {…}} — the translations the bank file
    # carried, or NULL for a question that has none. The Russian columns above
    # stay the source and the fallback, and `answer_idx` is shared: a
    # translation reorders nothing, it only replaces the four strings.
    i18n_json: Mapped[str | None] = mapped_column(Text)

    def get_i18n(self) -> dict:
        if not self.i18n_json:
            return {}
        try:
            v = json.loads(self.i18n_json)
        except (ValueError, TypeError):
            return {}
        return v if isinstance(v, dict) else {}

    def set_i18n(self, data: dict | None) -> None:
        self.i18n_json = json.dumps(data, ensure_ascii=False) if data else None

    def localized(self, lang: str) -> dict:
        """`{"t","q","o","e"}` in `lang`, falling back field by field to the
        Russian source. Field by field and not whole-record, because a
        half-filled translation must not blank a question: an empty `q` in the
        catalogue is a missing translation, not an empty question."""
        tr = self.get_i18n().get(lang) or {}
        opts = tr.get("o")
        if not isinstance(opts, list) or len(opts) != len(self.get_options()):
            opts = None
        return {
            "t": tr.get("t") or self.topic,
            "q": tr.get("q") or self.question,
            "o": list(opts) if opts else self.get_options(),
            "e": tr.get("e") or self.explanation,
        }

    def get_options(self) -> list:
        if not self.options_json:
            return []
        try:
            v = json.loads(self.options_json)
        except (ValueError, TypeError):
            return []
        return v if isinstance(v, list) else []

    def set_options(self, options: list) -> None:
        self.options_json = json.dumps(list(options), ensure_ascii=False)


class QuizAttempt(Base):
    """One user's run of one calendar day's quiz. `bank_day` freezes which
    bank day (`QuizQuestion.day`) was served — the season-start/holiday
    mapping in `app/services/quests_config.py` can change over time, but a
    past attempt must keep pointing at the questions it actually asked.
    `q_ids_json` is that frozen question list; `answers_json` is the list
    of `{qid, served_at, pick, ms, correct, pts}` entries the client
    reports back as the user answers — kept per-question (not just a
    final tally) so a disputed score, or a later scoring-rule change, can
    be recomputed from the raw picks instead of trusted blindly.
    `finished_at` NULL is an attempt still in progress; the unique
    constraint on `(user_id, day)` is what makes "attempt already exists"
    the resume path instead of a second attempt."""
    __tablename__ = "quiz_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    bank_day: Mapped[int] = mapped_column(Integer, nullable=False)
    token: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    q_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    answers_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    correct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("user_id", "day", name="uq_quiz_attempts_user_day"),
    )

    def get_q_ids(self) -> list:
        if not self.q_ids_json:
            return []
        try:
            v = json.loads(self.q_ids_json)
        except (ValueError, TypeError):
            return []
        return v if isinstance(v, list) else []

    def set_q_ids(self, q_ids: list) -> None:
        self.q_ids_json = json.dumps(list(q_ids), ensure_ascii=False)

    def get_answers(self) -> list:
        if not self.answers_json:
            return []
        try:
            v = json.loads(self.answers_json)
        except (ValueError, TypeError):
            return []
        return v if isinstance(v, list) else []

    def set_answers(self, answers: list) -> None:
        self.answers_json = json.dumps(list(answers), ensure_ascii=False)


class QuestClientEvent(Base):
    """One row per `(user_id, kind)` for the two onboarding quests the
    server cannot observe itself — "change the theme" and "switch to grid"
    live in the browser's localStorage, so the client reports them. The
    UNIQUE constraint is the whole anti-abuse story: a kind is credited at
    most once per user, the whitelist is two values, and these quests pay
    points only (never a surprise), so the ceiling for a forged POST is
    twenty points. IP goes to the audit row for the post-mortem."""
    __tablename__ = "quest_client_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "kind", name="uq_quest_client_events_user_kind"),
    )


# ---------------------------------------------------------------------------
# Row-level security (ADR-18): who sees which rows of an RLS report.
# ---------------------------------------------------------------------------

class SalesScope(Base):
    """One row per (person, hierarchy node) from the sales-positions view
    (`analytics_dw.v_positions`), loaded by an administrator's CSV
    upload on /admin/scopes (long format: person, role, node, parent). This is
    NOT a "Reference table" (ADR-16) — reports never read it; only the server does,
    to build `user_scopes`. `person_key` is the raw identifier from the file
    (casefolded); `person_id` is its resolution against OnePage accounts /
    the HR directory, NULL when nothing matched (counted, never guessed).
    `role` TMD with an empty node means «sees everything» (explicit grant —
    absence from the table is default DENY, never «sees all»)."""
    __tablename__ = "sales_scopes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_key: Mapped[str] = mapped_column(String(128), nullable=False)
    person_id: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    node: Mapped[str | None] = mapped_column(String(256))        # as in the file
    node_norm: Mapped[str | None] = mapped_column(String(256))   # rls.norm(node)
    parent_norm: Mapped[str | None] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="import")  # import | manual
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (
        Index("ix_sales_scopes_person_id", "person_id"),
        Index("ix_sales_scopes_node_norm", "node_norm"),
    )


class UserScope(Base):
    """Flattened visibility per OnePage account, recomputed on every import /
    manual grant: `keys_json` is the JSON list of normalized node keys the
    person may see (their nodes plus the whole subtree beneath each), so a
    slot-file serve costs ONE indexed read and no recursion. No row = the
    user sees no rows of any RLS report (default deny)."""
    __tablename__ = "user_scopes"

    person_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    see_all: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    roles: Mapped[str | None] = mapped_column(String(128))       # "HOU,TMR"
    keys_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    key_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    def get_keys(self) -> list[str]:
        try:
            v = json.loads(self.keys_json or "[]")
        except (ValueError, TypeError):
            return []
        return v if isinstance(v, list) else []


class SalesScopeSync(Base):
    """History of /admin/scopes imports (mirror of EmployeeSync)."""
    __tablename__ = "sales_scope_syncs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)       # rows in file
    persons: Mapped[int] = mapped_column(Integer, nullable=False, default=0)     # distinct person keys
    resolved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)    # matched to an account
    unresolved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nodes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)       # distinct hierarchy nodes
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ExploreAnswer(Base):
    """Server-side record of one AI-produced answer — never the answer's
    RESULT, never the SQL text (a filter literal can quote a data value; see
    `_validate_semantic_sql`'s comment in app/api/explore.py). What survives
    is exactly enough to audit «what was asked, against which frozen
    semantics, and did the SQL that ran match what a re-run of the same
    generator version would produce»:

      * `intent_json` — the validated structured intent AS IS. Its filter
        VALUES come from the user's own question text (a KPI/dim/op picked
        from the frozen semantics, a literal typed by the person asking) —
        never from a query result, so this is not a data leak.
      * `sql_sha256` — a hash of the SQL that actually ran, not the SQL
        itself: tier-2/lake SQL is model- or intent-written and can embed a
        filter literal that quotes real data.
      * `generator_version` / `semantics_sha256` — which frozen semantics
        (ADR-13) the answer was grounded against, so a later semantics change
        does not retroactively change what an old answer meant.

    One row per successful answer; the caller commits (same discipline as
    `_log`/`AuditLog` elsewhere in this module)."""
    __tablename__ = "explore_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    generator_id: Mapped[int] = mapped_column(ForeignKey("generators.id"), nullable=False, index=True)
    dashboard_id: Mapped[int | None] = mapped_column(ForeignKey("dashboards.id"))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)  # form|intent|semantic_sql|nl2sql|lake
    intent_json: Mapped[str | None] = mapped_column(Text)
    sql_sha256: Mapped[str | None] = mapped_column(String(64))
    generator_version: Mapped[int] = mapped_column(Integer, nullable=False)
    semantics_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[int | None] = mapped_column(Integer)
    via: Mapped[str] = mapped_column(String(16), nullable=False)  # browser|server
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    # 👍/👎 of the person who ASKED (nobody else can vote on someone else's
    # answer — see `explore_answers.vote()`): NULL = not voted, and voting the
    # same way twice is a no-op rather than a second row. The vote is the only
    # thing about an answer that changes after it was written.
    vote: Mapped[str | None] = mapped_column(String(8))  # null|up|down
    voted_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_explore_answers_user_created", "user_id", "created_at"),
        Index("ix_explore_answers_generator_created", "generator_id", "created_at"),
    )

    def get_intent(self) -> dict | None:
        if not self.intent_json:
            return None
        try:
            v = json.loads(self.intent_json)
        except (ValueError, TypeError):
            return None
        return v if isinstance(v, dict) else None


class ExploreFormSuggestion(Base):
    """Suggestion box for the report author: "this question deserves to become a form".

    One row is one suggestion, and, like `explore_answers`, it does NOT contain
    the answer: no result row, no SQL text. What it holds:

      * `question` + `question_hash` — the question as the person asked it, and the sha256
        of its NORMALIZED form (see `explore_answers.normalize_question`).
        The hash is the dedup key: "how many SL per week?" and "How many  SL   per
        week" are one suggestion, not two;
      * `kpi_key` / `intent_json` — what exactly was computed (the metric key from
        the frozen semantics and the parsed intent; the filter values in it
        come from the text of the question itself — not from the result);
      * `sql_hash` — a hash of the SQL, not the SQL: a literal in a filter could quote
        a real value (the same reason `ExploreAnswer` stores
        `sql_sha256`).

    `kind`:
      * `B` — "the answer is wrong" (👎): the author should look at the definition;
      * `A` — "this question gets asked and it is correct": an INDEPENDENTLY confirmed 👍
        on one normalized question for one generator — three 👍 from one
        person on three different calendar days OR 👍 from two different people (see
        `explore_answers._upvote_signal`): a candidate for the semantics'
        `question_forms`.

    Foreign-key policy is the same as across the whole app (see
    app/services/integrity.py: `ON DELETE` is not declared on columns, the rule
    is stated once in Python and follows from NULL-ability):
      * `generator_id` NOT NULL — a suggestion exists only together with its
        generator and is deleted with it;
      * `user_id`, `answer_id`, `decided_by` — nullable references: the author's queue
        survives a deactivated account, a purged answer and a deleted
        decider; the reference is nulled, the row stays.
    """
    __tablename__ = "explore_form_suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generator_id: Mapped[int] = mapped_column(ForeignKey("generators.id"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    answer_id: Mapped[int | None] = mapped_column(ForeignKey("explore_answers.id"))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    kpi_key: Mapped[str | None] = mapped_column(String(64))
    intent_json: Mapped[str | None] = mapped_column(Text)
    sql_hash: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(1), nullable=False)      # A|B
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="new")  # new|approved|declined
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        # Author's queue: "what was suggested to me for this report and not yet reviewed".
        Index("ix_explore_form_suggestions_generator_status", "generator_id", "status"),
        # What a specific person suggested (and dedup of their own repeats).
        Index("ix_explore_form_suggestions_user_created", "user_id", "created_at"),
        # Dedup of UNREVIEWED suggestions — at the database level, not only
        # by a check before insert: two simultaneous 👎 on one question both
        # passed the SELECT and inserted a row each. The index is PARTIAL (`WHERE
        # status='new'`) on purpose: after `declined`/`approved` the same question
        # must land in the queue again — "declined half a year ago, and they're
        # asking again" is a new signal, not a duplicate.
        Index("uq_explore_form_suggestions_open",
              "generator_id", "question_hash", "kind",
              unique=True, sqlite_where=text("status='new'")),
    )

    def get_intent(self) -> dict | None:
        if not self.intent_json:
            return None
        try:
            v = json.loads(self.intent_json)
        except (ValueError, TypeError):
            return None
        return v if isinstance(v, dict) else None


# ---------------------------------------------------------------------------
# "Working on the platform" (course, 2026-09-12)
# ---------------------------------------------------------------------------


class LearnProgress(Base):
    """One completed lesson of one person.

    The row appears ONLY when the lesson is completed in full: all checks answered
    correctly (retakes are unlimited — this is a course, not an exam) and, if the
    lesson has a platform task, it has been confirmed by the server. So
    course state is just a set of rows, not a state machine:
    "available" is computed from the lesson order in the content, not stored.

    UNIQUE(user_id, lesson_id) — a repeat completion does not award XP a second
    time; a race between two tabs is caught by the database, as in quests.
    """

    __tablename__ = "learn_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    lesson_id: Mapped[str] = mapped_column(String(32), nullable=False)
    xp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "lesson_id", name="uq_learn_progress_user_lesson"),
    )


class DeviceToken(Base):
    """A long-lived key with which the DESKTOP program reads the platform.

    Why it was needed. The only authentication on the platform is the
    `op_session` cookie with an eight-hour TTL. For a browser that is right, but background
    mirror sync would lose its login every night: SSO requires a person at the
    keyboard (ADFS is a redirect and a signed assertion, a program can't
    get through them), and a password from `OP_LOCAL_USERS` doesn't exist on prod at all.

    **The key is strictly read-only, and that is enforced by code, not by convention**:
    `app/auth/deps.py` rejects it on any method other than GET and HEAD. That way
    a new mutating route can't accidentally become reachable by the key — there is one
    check and it knows about the method, not about a list of paths.

    ONLY the sha256 of the key itself is stored, like a password. A KDF is deliberately
    NOT applied here, unlike `app/auth/local.py` with its PBKDF2 at 600,000
    iterations: there a person-invented password is hashed, which can be
    dictionary-attacked, while here it's 256 bits from `secrets.token_urlsafe`, where
    there is nothing to brute-force, and stretching would only add latency to every
    request. Lookup goes through an index directly on the hash, so no string
    comparison is performed at all.

    `revoked_at` instead of deleting the row: revocation must be a visible fact, not
    a disappearance. A key always has `expires_at` — there are no eternal ones.
    """

    __tablename__ = "device_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # sha256 in hex — 64 chars. Unique: two keys with the same hash would mean
    # a sha256 collision, but the index must be unique anyway so that lookup
    # by it is unambiguous.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # A human label "what machine is this" — so that in the list of your keys
    # you can tell which one to revoke.
    label: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Updated at most once an hour: this is not a hot path, but there's no point
    # writing on every GET either.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class DashboardUnlock(Base):
    """The lifted lock of a "strictly confidential" report — for ONE session.

    Why a database row and not a dict in process memory: there can be more than one
    `OP_WORKERS`, and a person who unlocked a report would get "open" on one request
    and "enter password" on the next — behaviour that is
    impossible to explain and in which people suspect a leak, not the load balancer.
    One record per (session, report), living for minutes; one indexed
    read query per request to SUCH a report is cheaper than an inexplicable UI.
    Ordinary reports never look here at all.

    Binding to `session_id`, not to the user: closed the browser (or an
    admin killed the session) — the lock is back in place. That is the whole point of a second
    lock, otherwise it would be lifted once and for all.

    The row does NOT survive a password change: `conf_fp` stores a fingerprint of the hash
    in effect at the moment of unlocking, and a password change by the owner
    invalidates all issued unlocks without needing to delete them.
    """

    __tablename__ = "dashboard_unlocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dashboard_id: Mapped[str] = mapped_column(ForeignKey("dashboards.id"), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # sha256(conf_hash)[:16] — a fingerprint of the password, not the hash itself: in this table
    # there is no business for brute-force material.
    conf_fp: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("session_id", "dashboard_id", name="uq_unlock_session_dash"),)
