from __future__ import annotations

import subprocess
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_build_sha() -> str:
    """Best-effort short git sha identifying the running build.

    Order: `app/BUILD` (written into the image by deploy/make-bundle.sh and
    deploy/make-patch.sh at package time — this is what actually answers on a
    deployed instance), then `git rev-parse --short HEAD` (dev boxes running
    straight from the repo, no BUILD file), then "unknown" (neither available
    — e.g. a source tree copied without .git and without a BUILD file).
    """
    build_file = Path(__file__).resolve().parent / "BUILD"
    try:
        content = build_file.read_text(encoding="utf-8").strip()
        if content:
            return content
    except OSError:
        pass
    try:
        repo_dir = Path(__file__).resolve().parent.parent
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir, capture_output=True, text=True, timeout=2,
        )
        sha = out.stdout.strip()
        if out.returncode == 0 and sha:
            return sha
    except Exception:
        pass
    return "unknown"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8000

    # Short git sha of the running build, shown on /health and in the admin
    # header. OP_BUILD_SHA env (if set) wins outright; otherwise resolved once
    # at startup from app/BUILD (see _resolve_build_sha docstring).
    build_sha: str = Field(default_factory=_resolve_build_sha)
    # uvicorn worker processes. >1 is safe: sessions live in SQLite (shared),
    # perflog writes one file per pid. Note the render LRU is per-process.
    workers: int = 1

    db_path: Path = Path("data/onepage.db")
    uploads_dir: Path = Path("data/uploads")
    # NOTE: there is deliberately no session_secret here. Sessions are opaque
    # server-side rows keyed by secrets.token_hex(32) (app/auth/session.py) —
    # 256 bits of randomness, never signed and never parsed by the client, so
    # there is nothing for a secret to sign. The setting used to exist, was
    # referenced by docker-compose and .env.example, and was read by no code at
    # all: a knob that promised security it did not provide.

    initial_admin: str = ""
    dev_bypass_auth: bool = False
    # Password sign-in for a stand outside the perimeter (app/auth/local.py):
    # `id=pbkdf2_sha256$…,id=…`. Empty = the form and the route do not exist.
    local_users: str = ""

    # Extra browser origins allowed to make state-changing requests, on top of
    # OP_SAML_SP_BASE_URL (comma-separated, scheme+host[:port]). Needed only when
    # the app is reached under a second name — a vanity DNS alias, or the raw
    # host:port next to the proxy URL. Empty is the right value for one URL.
    trusted_origins: str = ""
    # Operator switch for the cross-origin guard. On by default; it is the CSRF
    # defence the SameSite=None cookie cannot provide. False takes it out of the
    # middleware stack entirely — the escape hatch if a deployment turns out to
    # answer under a name nobody wrote down and every write starts returning
    # 403. Turning it off restores the pre-existing exposure, so it is a way to
    # buy an hour, not a setting to leave alone.
    origin_guard: bool = True

    # SAML
    saml_sp_base_url: str = "http://localhost:8000"
    saml_idp_entity_id: str = ""
    saml_idp_sso_url: str = ""
    saml_idp_cert: str = ""
    saml_claim_person_id: str = (
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn"
    )
    saml_strict: bool = False
    saml_https: bool = False
    # Require the IdP to sign the assertion. This is the flag that actually
    # authenticates a login: OP_SAML_STRICT only turns on python3-saml's strict
    # mode, and with signing off the SP would accept an UNSIGNED assertion —
    # anyone able to POST to /saml/acs could sign in as any person_id. ADFS
    # signs assertions by default, so true is both safe and correct here.
    # Turning it off is only ever a temporary debugging step; the startup
    # preflight refuses to boot with strict on and this off.
    saml_want_assertions_signed: bool = True

    # Performance logging (see app/services/perflog.py)
    perf_enabled: bool = True
    # Sampling CEILING (0.0–1.0): the highest share of requests ever recorded.
    # With perf_adaptive on, the effective probability floats between
    # perf_sample_floor and this value, steered toward perf_target_rps.
    perf_sample: float = 1.0
    # Adaptive sampling control loop (runs in the perflog writer thread).
    # Off = fixed perf_sample probability, the legacy behaviour. Measured
    # rationale (benchmark campaign C1): at sample=1.0 and ~2k rps the writer's
    # serialization batches stall the event loop via the GIL — bare RTT p99
    # 9.27 ms vs 0.73 ms with telemetry off.
    perf_adaptive: bool = True
    perf_target_rps: float = 200.0    # records/s budget the sampler steers to
    perf_sample_floor: float = 0.01   # lower bound — telemetry never goes blind
    perf_sql: bool = False            # record individual SQL statements
    perf_client: bool = True          # collect browser-side timings
    perf_dir: Path = Path("data/perflog")
    perf_retention_days: int = 14

    # Grid previews heavier than this many bytes render as a placeholder tile
    # instead of a live iframe (0 disables the cutoff)
    preview_max_bytes: int = 1_500_000

    # Serve snapshot data as a content-addressed /render/{id}/snapshot.js
    # (immutable-cacheable, URL changes with the data) instead of inlining it
    # into every render. OFF by default — enable only with owner sign-off.
    snapshot_external: bool = False

    # Keep compressed variants next to the raw body in the server render LRU
    # (same cell, same fingerprint key — stale variants are impossible). Warm
    # hits then skip compression entirely: ~1-3 ms responses.
    cache_encoded: bool = True

    # Pre-render+compress the N most recently updated published dashboards at
    # startup (plus the 5 heaviest — their cold miss is the multi-second one),
    # and re-warm a dashboard in the background after content mutations
    # (0 = off). Kills user-facing cold-cache misses.
    cache_warm: int = 20

    # Render LRU byte budget (raw bodies + compressed variants). One ~26 MB
    # giant costs ~30 MB with its br variant — raise to 512+ on RAM-rich hosts
    # so warm giants are never evicted by smaller dashboards.
    cache_max_mb: int = 256

    # Report generators (docs/RFC_GENERATORS_RESEARCH.md, Appendix E)
    # Code-bundle size cap; report DATA keeps the existing 50 MB upload cap.
    generators_bundle_max_mb: int = 20
    # A `running` row whose heartbeat (or start) is older than this is declared
    # `lost` by the NEXT run attempt — TTL enforced by the read path, same
    # discipline as sessions; no sweeper thread. The engine heartbeats every
    # ~15-30 s from the main thread (the Worker does the computing).
    generators_run_lock_ttl_s: int = 180

    # Server lake cache (Phase 9.1, docs/agile-petting-dewdrop.md "Phase 9"):
    # row-level Parquet partitions of a report's source folder, written by
    # the generator's run and readable by anyone with dashboard access.
    # Per-partition upload ceiling (checked BEFORE the part is written).
    lake_part_max_mb: int = 64
    # Total size of one dashboard's lake dir (all kept partitions together).
    lake_max_mb: int = 1024
    # ADR-14b: server-side /explore execution over the lake (app/services/
    # lake_exec.py). Instances = resident engine copies (frames in RAM), LRU.
    lake_exec: bool = True
    lake_exec_instances: int = 2
    # Watchdog for one op/sql/intent/export call (DuckDB queries are not
    # preemptible from Python otherwise — a bad plan would hold the instance
    # lock, and every other viewer of the dashboard, forever).
    lake_exec_timeout_s: int = 60
    # "Strictly confidential" (the second lock, app/services/confidential.py).
    # How long an opened lock lives within one session. An hour — not out of caution but
    # out of behaviour: a person works with a report in one stretch, not all day, and
    # asking for the password on every open would teach people to keep
    # it in the clipboard. The unlock is bound to the session, so a closed
    # browser brings the lock back before the deadline.
    # Navigators — landing pages (app/api/navigators.py). The flag follows
    # the Quests pattern: off means a 404 for the whole router and a vanished
    # menu item, i.e. a rollback without a redeploy.
    navigators_enabled: bool = True
    confidential_unlock_ttl_s: int = 3600
    # Guessing here comes from INSIDE the perimeter (ADR-19 covers only the outside),
    # so attempts are counted. Five per fifteen minutes per pair
    # (person, report): survives a typo, not a dictionary.
    confidential_attempts: int = 5
    confidential_attempt_window_s: int = 900
    # Git-link registration (R4b). IT-5: the corporate proxy is unreachable
    # from the server but api.github.com answers DIRECTLY — the flag is an
    # operator switch, not a technical block; the /git endpoints answer 503
    # while disabled. Bundle upload works regardless.
    generators_git_mode: bool = False
    # Where the browser engine loads Pyodide from. "auto" (default) resolves at
    # startup: /static/pyodide/ when the self-hosted subset is present
    # (deploy/fetch-pyodide.py populates it; make-bundle images always carry
    # it), else the CDN. Self-host is the production recommendation (RFC App.
    # D.4) — measured trigger: a worker importScripts() to the CDN is blocked
    # from the corporate page origin (CSP/SWG), while file:// microtests pass.
    # Set explicitly to pin either source. Must end with a trailing slash.
    generators_pyodide_base: str = "auto"
    generators_pyodide_cdn: str = "https://cdn.jsdelivr.net/pyodide/v314.0.2/full/"

    @property
    def resolved_pyodide_base(self) -> str:
        if self.generators_pyodide_base != "auto":
            return self.generators_pyodide_base
        if (self.base_dir / "static" / "pyodide" / "pyodide.js").exists():
            return "/static/pyodide/"
        return self.generators_pyodide_cdn

    # Canonical-table inference (Leg 1, app/services/catalog.py). The kill
    # switch from the plan: off means `rebuild` never runs — neither the daily
    # `_gc` tick nor the debounced job after a generator run — and the tables
    # simply stop being refreshed. Nothing else in the app reads them yet, so
    # turning it off is a complete rollback without a migration.
    catalog_enabled: bool = True

    # `/explore` (Leg 3, app/api/explore.py). Off means every route under the
    # router 404s — the query builder, the NL->SQL bridge, saved questions —
    # same rollback contract as OP_CATALOG_ENABLED: a flag flip, no migration.
    # Off by default: Explore is a preview (see README "Status"). When
    # off, the sidebar item, the palette entry, the `g e` hotkey and the
    # viewer's chat pane (which embeds /explore) are hidden too. Deployments
    # that rely on it pin OP_EXPLORE_ENABLED=true (deploy/bundle does).
    explore_enabled: bool = False

    # Row-level security (ADR-18): in-memory LRU budget (MB) for per-viewer
    # filtered slot files, keyed by (dashboard, version, slot, scope hash).
    # 0 disables the cache (every serve re-filters from disk).
    rls_cache_mb: int = 64

    # "Quests" (ADR-17). Off means the router 404s — same rollback contract
    # as OP_EXPLORE_ENABLED/OP_CATALOG_ENABLED: a flag flip, no migration.
    # Off by default: gamification is opt-in for the public edition. Admins can
    # still switch it on at runtime from /admin/quests (the overlay wins over
    # this default). Deployments that rely on it pin OP_QUESTS_ENABLED=true
    # (deploy/bundle does).
    quests_enabled: bool = False
    # Day boundary for streaks/daily openings — the DB itself stays UTC-naive
    # (created_at/day columns), this is only used to decide which calendar
    # day "now" falls on for a given user.
    quests_tz: str = "Asia/Almaty"
    # ISO date of the first working day of the season. The quiz bank day is
    # derived from it: (working-day index since season start) mod 15 + 1.
    quests_season_start: str = "2026-09-04"
    # Season length in working days.
    quests_season_length: int = 60
    # ADR-17b: the creature behind the full collection. Admin overlay wins.
    quests_companion_enabled: bool = True
    # Active season of collectible cards: 1 — foxes (frozen), 2 — toads.
    # Switched WITHOUT a redeploy by the data/quests-runtime.json overlay, which
    # takes precedence over this variable (quests_config.OVERRIDE_FIELDS).
    # Active draw season. Defaults to the FIRST, and that is not caution
    # for its own sake: a card takes its picture from the path
    # /static/img/quests/<card_id>.jpg, and while there is no toad art the second season
    # would hand out broken images to everyone at once. Switched by the overlay
    # (data/quests-runtime.json) without a redeploy — exactly when the art lands.
    quests_season: int = 1
    # Which /explore ENGINE the page boots (Leg 3 / workstream H1):
    #   1 = gen-worker.js in explore mode — Pyodide + python-calamine + pandas
    #       + DuckDB inside the wasm CPython heap. The shipped engine.
    #   2 = static/js/explore2/worker.js — xlsx-arrow (Rust/wasm) → Arrow →
    #       duckdb-wasm, no Python at all. Same message protocol, so the page
    #       only picks a worker URL.
    # `?engine=1`/`?engine=2` on the URL overrides this per visit, which is how
    # the flag gets exercised without a redeploy. Default stays 1: v2 answers
    # "Check folder" with a note instead of findings, and its column TYPING
    # differs from pandas' inference in ways docs/ENGINE2.md enumerates.
    explore_engine: int = 1
    # Ollama base URL the server calls for NL->SQL (e.g. http://ollama:11434).
    # Empty is the kill switch for that ONE feature: the query builder and
    # every other /explore route keep working, only POST …/nl2sql answers
    # 503. Nothing here is a client of any other provider — the whole point
    # of running against Ollama on this same network is that the schema
    # prompt (table/column names, types, partitions — never a row of data)
    # never leaves the corporate network.
    llm_endpoint: str = ""
    llm_model: str = "qwen2.5-coder:7b"
    llm_timeout_s: int = 20
    # Real cell contents (Data Quality's own profile "top" values / known-
    # value baselines) to fold into the schema prompt as "example values",
    # capped at 3 per column even when this is raised. The plan's
    # "Security" section is explicit: "to the LLM — only the schema … never
    # values" — so this defaults to 0 (schema-only, no values leave the
    # network) and sending any example values at all is an opt-in an
    # operator makes deliberately, the same posture as OP_LLM_LOG.
    llm_example_values: int = 0
    # Constrained decoding for the intent route: /api/explore/intent sends a
    # JSON schema (KPI keys and dim names as enums) as Ollama's `format`, so
    # a weak CPU model cannot spell a metric that does not exist. Needs
    # Ollama >= 0.5; an older one answers 4xx, which nl2sql detects once and
    # falls back from. Off = the plain free-text prompt.
    llm_structured: bool = True
    # Log the schema-only prompt sent to the LLM. Off by default (docs/plan
    # "Security"): even a schema-only prompt is corporate metadata, and
    # logging it is a deliberate opt-in, not a default.
    llm_log: bool = False

    # ── LLM provider (docs/LLM_PROVIDERS.md) ──────────────────────────────
    # Which service `nl2sql.call_llm` talks to. `ollama` (default) keeps the
    # historical posture: a model on our own network, nothing leaves it.
    # `deepseek` is the reviewed external option — an OpenAI-compatible
    # `POST {endpoint}/chat/completions` with a bearer key. What leaves the
    # company on that path is the question plus KPI names/definitions/dims
    # and glossary terms; never a cell value (the prompt builders are pinned
    # by tests/smoke_explore_intent.py).
    llm_provider: str = "ollama"
    # Bearer key for an external provider. Server-side only — never
    # serialized into any response, never logged, never part of an error
    # message (nl2sql scrubs it defensively).
    llm_api_key: str = ""
    # Let the USER'S BROWSER call the provider directly (the prod server may
    # have no egress while users' browsers reach the internet through the
    # corporate proxy). The server still does everything except the model
    # call: `/api/explore/intent/prepare` builds the prompt, the page posts
    # it to the provider, `/api/explore/intent/validate` validates the
    # answer against the semantics. Off = the page never talks to anyone but
    # this server.
    llm_browser: bool = False
    # Optional server-held key handed to authenticated users who may run a
    # generator (`GET /api/explore/llm-config`, audited). Leave empty for
    # BYOK: every user pastes their own key, which then lives only in that
    # browser's localStorage and never reaches this server.
    llm_browser_key: str = ""
    # Who may GRANT DeepSeek access (comma-separated person_ids). Empty =
    # any administrator. Set on the server (env), deliberately NOT part of
    # the runtime overlay: only someone with server access can change who
    # controls the paid API. Access itself is per-user rows (`llm_access`),
    # managed from the /admin LLM card.
    llm_access_admins: str = ""

    # Jinja template auto-reload (turn off in production for zero stat() calls)
    templates_auto_reload: bool = True

    # uvicorn access log: one formatted line per request, emitted SYNCHRONOUSLY
    # on the event loop (Python logging → stderr → journald/Docker log driver).
    # Every warm render hit pays it.
    #
    # OFF by default because it was MEASURED, not guessed: campaign C6 priced it
    # at 91–96 µs of RTT p50 at 2 krps — about 45% of the whole 0.208 ms that C2
    # had attributed to "stack thickness" and used to argue for rewriting the
    # stack. Confirmed twice through independent mechanisms (this flag on the
    # full app, CH_ACCESS_LOG on a bare-uvicorn rung) agreeing within 5 µs.
    # Nothing is lost: the reverse proxy in front of prod keeps its own access
    # log, so this was duplicate bookkeeping on the hot path. Set true to
    # restore it (useful when running without a proxy in front).
    access_log: bool = False

    @property
    def base_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def abs_db_path(self) -> Path:
        p = self.db_path
        return p if p.is_absolute() else self.base_dir / p

    @property
    def abs_uploads_dir(self) -> Path:
        p = self.uploads_dir
        return p if p.is_absolute() else self.base_dir / p


    # ── Sign-in via OIDC ─────────────────────────────────────────────────
    # The third door next to password and SAML: Keycloak, Authentik, Okta, Google,
    # Microsoft Entra. Unlike SAML it needs no additional
    # dependency — httpx, which is already installed, is enough.
    #
    # OIDC counts as enabled when issuer and client_id are filled in:
    # there is deliberately no separate flag, following `conf_hash` and `rls_json` —
    # a second boolean switch would one day drift apart from the settings.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid profile email"
    # Which claim to treat as the account name. `preferred_username` usually
    # carries the UPN, i.e. exactly what the platform expects from SSO and can match to
    # a directory record (`identity.resolve`). Anyone for whom it differs will change it.
    oidc_claim_person_id: str = "preferred_username"
    # The discovery response is cached: it changes once in years, and a request to the IdP on
    # every sign-in would put someone else's network on the sign-in path.
    oidc_discovery_ttl_s: int = 3600

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer.strip() and self.oidc_client_id.strip())

    # ── Employee directory: headers of a foreign export ──────────────────
    # `COLUMN_MAP` in app/api/employees.py is a whitelist of CSV headers, and
    # the names in it are neutral. Every company's export names the columns
    # its own way, and an unknown header silently never reaches the database — the field
    # just stays empty, with no error. So a site can declare
    # its own mappings: "their header=our field", comma-separated.
    #   OP_EMPLOYEE_COLUMN_ALIASES=datejoined=hiredate,startdate=positionstartdate
    # The header is compared in lower case, like the built-in ones.
    employee_column_aliases: str = ""


settings = Settings()
