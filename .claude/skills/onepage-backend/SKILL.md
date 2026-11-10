---
name: onepage-backend
description: FastAPI/SQLAlchemy conventions in OnePage: auth deps and reach-checks, audit _log, migration idiom, OriginGuard, perf hot-path rule, data-boundary rule. Load before adding any route, model, or query.
---

# Backend: Auth, Audit, Migrations, Middleware

## Auth deps (app/auth/deps.py)

**Hot-path async deps** (run in event loop, not threadpool):

```python
async def get_current_user_optional(op_session: Optional[str] = Cookie(...)) -> Optional[User]
async def get_current_user(user: Optional[User] = Depends(get_current_user_optional)) -> User
def require_role(*roles: str) -> Depends
def require_permission(permission: str) -> Depends  # param: "upload_dashboard", "manage_generators"
async def get_dashboard_or_404(dashboard_id: str, db: Session = Depends(get_db)) -> Dashboard
async def require_dashboard_access(dashboard, user, db) -> Dashboard  # admin | owner | public | explicit grant
def can_update_dashboard_data(dashboard, user, db) -> bool  # admin | owner
```

**Reach checks for generators** (`app/api/generators.py`):

- `_generator_dashboards(db, generator_id) -> list[Dashboard]` — dashboards currently bound to this generator
- `_reachable_generator_dashboards(db, user, generator_id)` — of those, which the user can update
- `_assert_can_update_data(db, user, generator_id)` — raise 403 if no reach

**Param naming:** `user` is the convention for the current authenticated user (from a dep).

## Audit logging (_log function)

**Idiom** (duplicated per module — `app/api/dashboards.py` lines 54-61, `app/api/generators.py` lines 86-94):

```python
def _log(db: Session, actor: User, action: str, resource_type: str, 
         resource_id: str, detail: dict, ip: str = "") -> None:
    entry = AuditLog(
        actor_id=actor.id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        ip_address=ip,
    )
    entry.set_detail(detail)
    db.add(entry)
    # Caller owns the commit()
```

**Call site:** Always before `db.commit()`. Do not return early without committing.

**Indexes:** `CREATE INDEX IF NOT EXISTS ix_audit_log_created_at ON audit_log (created_at)` — created in lifespan.

## Migrations (app/main.py lines 84–156)

**Table creation:** `create_all()` called once in lifespan (lines ~50-65). New tables are defined in models, DDL is auto-generated.

**Column adds to existing tables** (try/except idiom, lines 87-109):

```python
with _engine.connect() as _conn:
    for _ddl in (
        "ALTER TABLE generation_runs ADD COLUMN outputs_json TEXT",
        "ALTER TABLE data_sources ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
        # ... one per new column
    ):
        try:
            _conn.execute(text(_ddl))
            _conn.commit()
        except Exception:
            _conn.rollback()  # column already exists
```

**Index adds** (idempotent CREATE INDEX IF NOT EXISTS, lines 121-156):

```python
for _ddl in (
    "CREATE INDEX IF NOT EXISTS ix_dashboards_owner_id ON dashboards (owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_dashboards_is_public ON dashboards (is_public)",
    "CREATE INDEX IF NOT EXISTS ix_dashboards_updated_at ON dashboards (updated_at)",
    # naming: ix_<table>_<column>
):
    _conn.execute(text(_ddl))
_conn.commit()
```

**Never break** `tests/smoke_migration.py` — pins second-boot byte-for-byte no-op (verifies migrations are idempotent).

## OriginGuard middleware (app/middleware.py lines 355–408)

**Registration order is load-bearing:** added BEFORE RenderFastLane in `create_app()` — warm renders bypass the guard, saving 75–91 µs per request.

**Behavior:**

- Safe methods (GET, HEAD, OPTIONS) pass through.
- Exempt paths: `/saml/acs` (ADFS posts from IdP origin), `/health`.
- Non-GET requests: require `Origin` or `Referer` header in `OP_SAML_SP_BASE_URL` or `OP_TRUSTED_ORIGINS`.
- **Body without Content-Type and non-zero Content-Length:** reject 403. (Browser fetch must set `Content-Type`; uploads via FormData auto-set it.)

**Reason:** Sandboxed iframe sends `Origin: null` + no CSRF token; cookie is `SameSite=None` for cross-origin iframe access.

**Exempt paths:** hardcoded in `_ORIGIN_EXEMPT` set; `/saml/acs` is the only one besides `/health`.

## Perf hot-path rules

**No new queries in:**

- `/d/{id}` — render path (cached)
- `/render/*` — static asset serve (fast lane, bypasses Django routing)
- Fast-lane auth via micro-cache (1 s TTL, per-request DB miss on expiry)

**Measure via:** `/admin/perf` — shows `serve_us` (cache lookup → response, target ≤ 3 µs), `fast_us` (fast-lane entry → response), `hdr_ms` (time-to-headers).

**Perf telemetry:** JSONL in `data/perflog/`, exported via `GET /api/admin/perf/export?hours=…`.

**Thread pool rule:** Sync def routes (disk I/O like `get_slot_file`, `render_dashboard` miss) run in threadpool. Jinja renders via `run_in_threadpool()` (GIL-bound CPU).

## Data boundary rule (docs/LLM_PROVIDERS.md)

**Cell values NEVER cross the network or reach an LLM:**

- Line 7-11: Intent route uses only frozen semantics (names, types, descriptions), never row data.
- Line 28-29: Quality findings store only aggregate stats + schema diff, never cell values (except 5 examples per check, trimmed to 64 chars, on FIRST run only — real cell contents cross the network, requiring privacy review before deployment).

**Audit:** Grepping `/api/explore/*` handlers for cell-value reads; grepping LLM prompts for `cells=` or `data=` fields (should not exist).

## Storage paths (app/services/storage.py)

**Slot files:** `settings.abs_uploads_dir / {dashboard_id} / v{version} / files / slot_{index}_{safe_name}`

**MIME map** (app/api/files.py lines 71–91):

```python
".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
".csv": "text/csv",
".js": "text/javascript; charset=utf-8",  # ORB needs correct MIME for scripts
".svg": "image/svg+xml",
# ... default: "application/octet-stream"
```

**Safe filename:** `re.sub(r'[^\w.\-]', '_', original_name)` before store.

**Size constants** (app/api/generators.py lines 65-68):

```python
_MAX_UPLOAD = 50 * 1024 * 1024      # 50 MB — publish-time html/slot files
_MAX_AUX = 10 * 1024 * 1024          # 10 MB — run-scoped aux input files
_MAX_OBS_BODY = 2 * 1024 * 1024      # 2 MB — one observations batch
_MAX_OUTPUTS_JSON = 64 * 1024        # 64 KB — publish's write_back report
```

## Template conventions (templates/base.html)

**Blocks:** `{% block content %}` (page-specific), `{% block title %}` (head title).

**Active nav:** `{% set active = '...' %}` before `{% include "sidebar.html" %}` — sets active nav item.

**Page data to JS:** `{{ dashboards | tojson }}` (pipe filter escapes for JSON context).

**Page scripts:** `<script defer src="...?v={{ asset_v }}">` (no `type=module` for page scripts, must run in order).

**asset_v:** Jinja global = max mtime of `static/` directory (cache busting for versioned URLs).
