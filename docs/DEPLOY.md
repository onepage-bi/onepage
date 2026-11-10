# Deploying OnePage (production)

## 0. How to deploy: build a bundle

To deploy on a corporate machine, **do not clone the repository on site** —
it is private, and building the image pulls `apt-get` and `pip` packages from
the internet, which the internal network usually does not have. Instead, build
a portable bundle here:

```bash
deploy/make-bundle.sh              # linux/amd64 by default
deploy/make-bundle.sh linux/arm64  # if the target machine is ARM
```

The result is `dist/onepage-deploy-<date>-<arch>/` with a ready-made image,
compose file, `.env` template, sample nginx configuration, a check script and
step-by-step instructions (`README.md` inside the bundle). The directory is
self-contained and is moved as a whole.

**Architecture matters.** The development machine is arm64, the target is
almost certainly x86_64; an image built without `--platform` will not start
there and fails with `exec format error`. The script sets the platform
explicitly and records it in `BUILD-INFO.txt` together with the git commit it
was built from.

The sections below are reference material: why particular decisions were made.
The step-by-step actions on the machine are in the bundle's `README.md`.

## 0a. What changed in the delivery (2026-09)

The foundation sprint that followed four enterprise reviews
(`docs/PERF_DECISIONS.md` ADR-19) changed the deployment mechanics
themselves, not only the code. In short, here is what is different compared
with the previous delivery:

- **Revision `0005_explore_feedback` — feedback on AI answers.** Two columns
  on an existing table (`explore_answers.vote`, `voted_at` — a plain
  `ALTER TABLE ADD COLUMN`, no table rebuild) and one new table,
  `explore_form_suggestions`, with three indexes, the third of which is a
  PARTIAL unique index (`WHERE status='new'`). It moves and rewrites nothing;
  on an empty queue it costs a single `PRAGMA`; re-running it is idempotent
  (like 0003/0004, the revision checks for the table/columns/indexes before
  each step), so a database created by `create_all()` is stamped rather than
  upgraded. Its `downgrade` is complete and reversible — and with it
  **`alembic downgrade base` works again across the whole chain**; this is
  checked by `tests/smoke_migration.py`, which now compares the schema IN FULL
  (tables, columns, types, nullability, indexes and their uniqueness) between
  "built by `create_all()`" and "arrived via migrations". What the tables
  mean is described in `docs/LLM_PROVIDERS.md` and `docs/PERF_DECISIONS.md`
  ADR-19/ADR-20.
- **`requirements.txt` changed (`alembic` was added) → a patch is no longer
  possible, only a full bundle.** `deploy/make-patch.sh` refuses on its own
  if `requirements.txt` or `Dockerfile` changed between versions (the list of
  patchable paths is only `app/`, `static/`, `templates/`, `tools/`), so for
  this delivery use only `deploy/make-bundle.sh` (§0 above), no
  `make-patch.sh`. The chat pane (ADR-20) did not touch `requirements.txt`
  itself, but it ships in the same full bundle: relative to what runs in
  production, the dependencies have already changed — "nothing changed since
  last time" does not count here, because the patch compares against
  PRODUCTION, not against the previous branch.
- **Before starting, run `tools/fk_orphans.py` on a copy of the production
  DB, first WITHOUT `--fix`.** Before this delivery SQLite ran with
  `foreign_keys=OFF`, so orphan rows may have accumulated in the file (a
  permission on a deleted dashboard, `generation_runs` of a report that no
  longer exists); turning the check on does not retroactively check what is
  already in the file. Details and the order of steps are in §2d below. It
  is the same section as in the previous delivery, not a new item, but it
  must not be skipped before THIS update: without the clean-up,
  `foreign_keys=ON` (see below) may refuse to delete a row that already has
  a dangling reference. Revision `0005` adds four more foreign keys
  (`generators`, `users` ×2, `explore_answers`) — a new empty table cannot
  contain orphans, but that does not replace checking the whole database.
- **On startup the container applies Alembic migrations.** Schema changes
  used to be a block of `ALTER TABLE`/`CREATE INDEX IF NOT EXISTS` in the
  lifespan; now they are versioned revisions (`app/migrations/versions/`)
  under an exclusive `flock`, applied once at boot before anything reads a
  row — see §2c. Rolling back to the previous build (if something went wrong
  after the schema upgrade) is `deploy/rollback.sh` (§6.2): it restores both
  the image and the data from a backup/previous archive. It does not roll
  Alembic migrations back "forward" by itself — the whole DB is rolled back,
  together with the data taken BEFORE the upgrade.
- **Backup cron and a quarterly restore drill.** `deploy/backup.sh` is now
  expected to be in the host's cron (§7, "Cron on the host"), and
  `deploy/restore-drill.sh` is a quarterly check that a backup actually
  restores and brings up a working instance, rather than just sitting on
  disk as an archive.
- **i18n needs no server configuration.** The ru/en/zh-HK interface is JSON
  catalogues in `app/locales/`; they ship with `app/` and need neither
  environment variables nor a separate build step. The exception is the
  zh-HK font on WORKSTATIONS (not the server), see §2z, an existing section.
  The Latin fonts (Inter Tight) have been vendored in `static/fonts/` since
  the previous delivery — there is nothing new to ship.
- **The ADFS section below was not changed by this sprint** — the integration
  has been running in production since 2026-07-20
  (`docs/PROD_2026-07-20.md`) and the foundation sprint did not touch it; see
  `CLAUDE.md` → "ADFS integration — as deployed" for the current list of what
  is confirmed in production and what remains open.

## 1. Why host networking

The C1 benchmark campaign (bare-metal KVM stand) showed that the
docker bridge adds noticeable latency because of `docker-proxy` — the userland
proxy that every published port goes through. Host networking removes that
layer: packets go straight to the uvicorn process on the host.

| Mode  | p50 vs bare | p99 vs bare |
|--------|-------------|-------------|
| bare-metal | 1.00× (baseline) | 1.00× (baseline) |
| host networking | ≈ +7% | ≈ +7% |
| docker bridge | ≈ +34% | ≈ +41% |

Switching bridge → host-net gives ≈ **−20% p50 / −24% p99** on server RTTs.

Source: the benchmark campaign's `RUNBOOK.md` / `REPORT_RU.md`, campaign **C1** (internal, not published).

## 2. Prerequisites

- **A Linux host.** Host networking does not work in Docker Desktop on
  Mac/Windows (there is no real Linux network namespace to pass through) — for
  local development on Mac/Windows, restore the `ports:` block in
  `docker-compose.yml`.
- Port 8743 is free on the host before the container starts:
  ```
  ss -ltnp | grep 8743
  ```
  (empty output means the port is free).
- uvicorn listens on `0.0.0.0:8743` — this is already set in compose
  (`OP_HOST: "0.0.0.0"`), and with `network_mode: host` it effectively means
  "listen directly on the host".

## 2z. Interface languages and the font for 繁體中文（香港）

The interface is translated into ru / en / zh-HK. **The server needs nothing
for this:** the catalogues are ordinary JSON files in `app/locales/`, they
ship with `app/`, and fonts are not used on the server.

The requirement is on **workstations**, and only those where zh-HK will be
turned on: the system must have a font with Traditional Chinese characters.
We deliberately do **not** ship it in the image — a full Noto Sans HK is
4–6 MB per weight, and a subset would have to be rebuilt on every edit of the
Chinese catalogue (the project has no frontend build, on principle).

| OS | Font | Availability |
|---|---|---|
| Windows | Microsoft JhengHei / JhengHei UI | bundled since Vista |
| macOS | PingFang HK | bundled since 10.11 |
| Linux | Noto Sans CJK TC / Noto Sans HK | installed with the `fonts-noto-cjk` package |

Latin letters, digits and codes stay on Inter Tight (`fonts.css` declares
`unicode-range`); only the CJK characters fall through to the system font. On
a machine without a CJK font the Chinese interface shows "tofu" — empty
rectangles; the fix is to install the font, not to redeploy the server.

Post-deployment check: open any page with `Cookie: op_lang=zh-HK` (or use the
language switcher in the top bar) and make sure the characters render.

## 2a. Optional flags: Quests (ADR-17)

`OP_QUESTS_ENABLED` is not one of the required variables in `env.template` —
it defaults to `true` (owner decision 2026-09-04: the programme starts with
the bundle without extra configuration; the season starts at
`OP_QUESTS_SEASON_START`, default 2026-09-04). It can be turned off with the
environment variable `OP_QUESTS_ENABLED=false` (the `/quests` router returns
404 entirely) or with the switch in the admin area. Turning it on/off is
possible via `/admin/quests` (the switch writes to the runtime overlay
`data/quests-runtime.json`, which takes precedence over env) **without a
redeploy** — the same way DeepSeek is enabled in `/admin`. The feature's
assets (cards, capsule, the Unbounded font) ship with the patch/bundle as
ordinary `static/` files, once, ≈1.6 MB (`static/img/quests/` +
`static/fonts/unbounded-*`) — they do not grow after the first delivery, so
no separate step is needed for them.

## 2a2. A stand outside the perimeter: password login (`OP_LOCAL_USERS`)

The product authenticates only through ADFS; the password-less
`OP_DEV_BYPASS_AUTH` must not be exposed on a public host (preflight forbids
it as well). For a demo host on the internet there is a third door —
`app/auth/local.py`: a list of accounts with password hashes in a single
environment variable, no migrations.

```
docker compose exec onepage python -m app.auth.local hash    # → pbkdf2_sha256$600000$…
OP_LOCAL_USERS=admin1=pbkdf2_sha256$$600000$$…,tmr1=pbkdf2_sha256$$600000$$…   # in .env every $ → $$
```

Compose substitutes `${…}` inside `.env`, and `$<hex-salt>` silently
disappears — hence `$$` (verified on the demo stand on 2026-09-07: without
escaping, `/local-login` answered 500 "malformed entry").

Rules: empty (the default) means no form and no `/local-login` route; only an
account that already exists in `users` can log in (the list grants a
password, not an account); when `OP_SAML_IDP_SSO_URL` is empty the SSO button
is hidden; every attempt is written to `audit_log` (`local_login` /
`local_login_failed` with the IP); a wrong password costs one PBKDF2 round
(600,000 iterations) plus a 0.5 s pause, an unknown name costs the same (so
usernames cannot be enumerated by timing). Choose long random passwords —
there is no attempt limit in the table. Test: `tests/smoke_local_login.py`.
In corporate production the variable stays empty.

## 2b. Backup: enable the audit export

Add one line to the daily backup (whatever performs it — cron, a systemd
timer, an external script) that exports yesterday's audit_log next to the
rest of the backup — on the host that is `~/onepage/data/backups/audit/`
(inside the container, `/data/backups/audit/`; the CLI creates the directory
itself, `os.makedirs(..., exist_ok=True)` on the parent of `--out`, so the
very first run on a fresh host does not fail with `FileNotFoundError`):

```cron
5 0 * * * cd ~/onepage && docker compose exec -T onepage python3 -m app.tools.audit_export --from "$(date -d yesterday +\%F)" --to "$(date -d yesterday +\%F)" --out /data/backups/audit/audit-$(date -d yesterday +\%F).jsonl
```

(`%` is escaped for crontab; `/data` is the container volume, the same one as
`onepage.db`/`uploads` (the host's `~/onepage/data`, bind-mounted from
`docker-compose.yml`), so the file is immediately included in the backup of
the whole volume.) The audit copy is kept together with the other backups —
that is the backup process's job, not the application's: the application
administrator (the `Administrator` role in `/admin`) does not write to this
export and does not influence it; they only read `/admin/audit` and click
"Export JSONL" for one-off manual exports (`GET
/api/admin/audit-log/export?from=...&to=...`, same line format, range limit
366 days).

## 2c. Migrations

The DB schema is managed by Alembic. Revisions live in
`app/migrations/versions/` (inside `app/`, so they get into both the image
via `COPY app/ app/` and the `deploy/make-patch.sh` patch); `alembic.ini` at
the repository root is only for the developer CLI, the application does not
need it.

**What happens at startup.** `app/services/migrate.py` is called in the
lifespan before anything else that reads or writes rows, and takes an
exclusive `flock` on `<DB>.migrate.lock` — with `OP_WORKERS > 1` exactly one
worker migrates, the others wait and see the finished schema. Then, depending
on the state of the file:

| What we find | What we do |
| --- | --- |
| no file/tables | `create_all()` + `stamp head` |
| tables exist, no `alembic_version` (production before this release) | `create_all()` (adds only new tables), `stamp 0001`, `upgrade head` |
| `alembic_version` exists | `upgrade head` |

Revision `0001` is the current schema from `app/models.py`; it is normally
only "stamped". Revision `0002` is that very block of `ALTER TABLE` /
`CREATE INDEX IF NOT EXISTS` that used to run in the lifespan on every
start; it is idempotent not through `try/except` but through the inspector
(it checks for each column and index), so it survives a DB where those
ALTERs have already been applied.

**Back up before migrating.** Mandatory, before `docker compose up -d`:

```bash
cd ~/onepage
./backup.sh --dest ~/onepage-backups        # see section 7
```

To check which revision the database is at (container logs in production are
empty by design, so we look at the DB itself):

```bash
sudo docker exec -i onepage-onepage-1 \
  python3 -c "import sqlite3;print(sqlite3.connect('/data/onepage.db').execute('select version_num from alembic_version').fetchall())"
```

**A new revision (for developers).** Change `app/models.py`, then from the
repository root:

```bash
alembic revision --autogenerate -m "short description"
# You MUST read the generated file: under SQLite, autogenerate does not
# see type/NOT NULL changes and likes to propose spurious DROP INDEX.
alembic upgrade head          # apply to your local DB
alembic downgrade -1          # check that the downgrade works
alembic upgrade head
python3 tests/smoke_migration.py
```

The revision file is named `<rev>_<slug>.py`; `--rev-id 0003` sets the number
explicitly. New tables are still created by `create_all()` on a clean DB, but
in production only the migration creates them — so a new table has to be
added both to the models and to a revision.

**Rollback.** Locally, `alembic downgrade -1` from the repository root. In
production the image has no `alembic.ini`, so the same command is invoked
through the config the application builds itself:

```bash
sudo docker compose stop onepage        # nobody must be writing to the DB
sudo docker compose run --rm onepage python3 -c \
  "from alembic import command; from app.services import migrate; command.downgrade(migrate.config(), '-1')"
sudo docker compose up -d onepage
```

Important: the `downgrade` of revision `0002` **drops columns together with
their data** (`dashboards.rls_json`, `dashboards.render_size`,
`generator_versions.semantics_json` and the other 17) — it is a real reverse
step, not cosmetics. That is why the standard production rollback is a build
rollback (section 6) plus restoring the DB from a backup (section 7), and
`downgrade` is kept for the case where only the schema needs to go back and
a fresh copy exists. The downgrade itself is covered by
`tests/smoke_migration.py` (item "d").

## 2d. Integrity: before deploying this version — `fk_orphans.py`

Since 2026-09-06 SQLite enforces foreign keys (`PRAGMA foreign_keys=ON`,
turned on in the lifespan **after** migrations). Before that there was no
check, and the production database may contain rows whose parent has already
been deleted: a permission on a deleted dashboard, a run of a report that no
longer exists, an `audit_log.actor_id` of a deleted account.

Turning the check on does **not break** such rows: SQLite looks only at new
writes and does not re-check what is already in the file. But while they are
there, any deletion of their parent may run into them, and provenance reports
will be wrong. So — before deploying, **on a copy**:

```bash
cd ~/onepage
./backup.sh --dest ~/onepage-backups           # section 7; you need the copy anyway
cp ~/onepage-backups/<latest>/onepage.db /tmp/fk-check.db

sudo docker compose run --rm -v /tmp:/tmp onepage \
  python3 tools/fk_orphans.py --db /tmp/fk-check.db
```

The report contains only the table name, column name, parent and row count —
no values, so it can be shown to anyone. Exit codes:

| Code | What it means | What to do |
| --- | --- | --- |
| 0 | no orphans | deploy |
| 1 | there are orphans, all fixable automatically | run `--fix` on the copy, make sure the result is 0, then do the same on the live DB with the container stopped |
| 2 | there are orphans referencing `users` | sort them out by hand (see below), only then deploy |
| 3 | file missing / cannot be opened | check the path |

`--fix` repairs orphans in one transaction, in several passes (deleting an
orphaned run orphans its observations), following the same policy the
application applies to live deletions — and it is NOT "delete everything
found":

* **NOT NULL** — the row exists only together with its parent, so it is
  deleted: runs and versions of a deleted dashboard, observations and files
  of those runs;
* **nullable** — this is a reference, not ownership: the column is set to
  NULL and the row stays. A presentation slide and a recorded AI answer
  outlive the dashboard they referred to (such orphans are legitimate in
  production: before 2026-09-06 deleting a dashboard did not touch them),
  and a dashboard with a dangling reference to a folder or generator moves
  to the root rather than being deleted.

The `--fix` output keeps these two numbers apart: "Rows deleted" and
"References nulled". It **never** touches references to `users`: behind such
a row is someone's work — a granted permission, an audit entry, an accepted
Data Quality finding. The usual reason they appear is deleted duplicate
accounts; the standard resolution is to move the rows to the canonical
account (`app/services/identity.py`, `merge_duplicates`), not to delete
them.

Clean the live DB with the application stopped:

```bash
sudo docker compose stop onepage
sudo docker compose run --rm onepage python3 tools/fk_orphans.py --db /data/onepage.db --fix
sudo docker compose up -d onepage
```

## 3. Reverse proxy

TLS is terminated by an external proxy (nginx/traefik/Caddy) in front of the
container; the upstream points to `127.0.0.1:8743`. `OP_SAML_HTTPS=true` is
already set in `docker-compose.yml` — the application assumes it is served
over HTTPS (needed for `Secure`/`SameSite=None` on the session cookie and for
the dashboards' iframe subresources).

A minimal nginx location block:

```nginx
location / {
    proxy_pass http://127.0.0.1:8743;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## 4. Security

Host networking removes the container's network isolation: the application
is the only listener on port 8743 directly on the host, with no NAT/filtering
by Docker between the outside world and the process. The host firewall must
block 8743 from outside — access only through the reverse proxy (80/443).

**Client IP in the logs works and needs no code.** (This section used to say
the opposite; that was wrong and was removed after verification.) uvicorn
0.32.1 enables `proxy_headers` **by default**, and `forwarded_allow_ips`
resolves to `127.0.0.1`. Its `ProxyHeadersMiddleware` wraps the application
**outside** our entire ASGI stack, so `PerfLogMiddleware` reads the already
rewritten `scope["client"]`: in the topology above (proxy on the same host,
`X-Forwarded-For` set as in the nginx example) the perf logs record the real
client, not the proxy's address. The `scheme` from `X-Forwarded-Proto` comes
from the same place.

**The only case where this breaks:** a reverse proxy on a DIFFERENT host.
Then the immediate peer is not `127.0.0.1`, is not considered trusted, and the
headers are ignored (this is protection against a forged `X-Forwarded-For`,
not a bug). The fix is uvicorn's own environment variable, with no changes to
OnePage:

```yaml
environment:
  FORWARDED_ALLOW_IPS: "192.0.2.5"   # proxy host address; NEVER "*"
```

`"*"` trusts the header from anyone and lets any client forge its IP in the
logs and the audit trail.

## 5. Post-deployment smoke checklist

```bash
# 1. Build and start
docker compose up -d --build

# 2. Application health check
curl -fsS http://localhost:8743/health

# 3. Make sure the port is held by the application process, not docker-proxy
ss -ltnp | grep 8743
# expected: LISTEN ... python3/uvicorn, WITHOUT docker-proxy in the output

# 4. Open a dashboard render through the reverse proxy (not directly on :8743)
#    and make sure the page/dashboard renders

# 5. Check the container healthcheck
docker compose ps
# STATUS must be "healthy"
```

## 6. Rollback

### 6.1. Build version

`/health` always returns the short git sha of the current build and the
process start time:

```json
{"status": "ok", "build": "1bd6c93", "started_at": "2026-09-06T12:00:00+00:00"}
```

(`/health?deep=1` carries the same fields plus `checks`.) The same build is
shown in the `/admin` header. The value reaches the image as the file
`app/BUILD`, written by `deploy/make-bundle.sh` and `deploy/make-patch.sh`
when the bundle/patch is built — **do not edit `app/BUILD` by hand** and do
not commit it (see `.gitignore`): when running locally from the repository
without this file, the sha is taken from `git rev-parse --short HEAD`, and
without git, from the constant `"unknown"`.

Every `deploy/make-bundle.sh` tags the built image TWICE: `onepage:latest`
(the one `docker-compose.yml` sees) and `onepage:<sha>`. Both tags travel in
`onepage-image.tar.gz` inside the bundle. On the next load, `docker load`
moves only `onepage:latest` to the new image — `onepage:<old-sha>` stays on
the machine untouched, and that is the rollback target.

### 6.2. Build rollback — `deploy/rollback.sh`

Run on the target machine, from the bundle directory (where
`docker-compose.yml` and `check.sh` also live):

```bash
# By image tag — if onepage:<sha> was already loaded by a previous bundle/patch
# (check: docker images | grep onepage):
./rollback.sh 1bd6c93

# By full bundle archive — if the required tag is no longer present locally,
# the script unpacks it and docker-loads the image from it:
./rollback.sh /path/to/onepage-deploy-20260901-amd64.tar.gz

# By data backup archive (not an image) — e.g. if you need to roll back because
# of corrupted data rather than a bad build. Accepted: the standard
# deploy/backup.sh archive (onepage.db, uploads/ and *.json at the archive root)
# and an archive of the data/ directory (all entries under data/):
./rollback.sh /path/to/onepage-backup-20260906-030000.tar.gz
```

The script (see the comment at the top of the file for details):
1. **first validates the argument without touching anything** — for a tag:
   that an image with that tag is already loaded in docker; for an archive:
   that it is either a full bundle (contains `onepage-image.tar.gz`) or a
   data backup in one of two formats: "flat" from `deploy/backup.sh`
   (`onepage.db` at the archive root; there must be no
   `docker-compose.yml`/`.env`/`*.sh` next to it) or "nested" (**all**
   entries under `data/`). Absolute paths and `..` in the archive, and
   foreign/unrelated archives, are rejected without unpacking: the current
   directory is the deployment directory with `docker-compose.yml`/`.env`,
   where the SAML certificates and `OP_LLM_*_KEY` live, and silently
   unpacking in the wrong place would ruin it. A typo in the tag or a path to
   the wrong file stops here — the container has not been stopped yet;
2. **prepares the rollback while the container is running**: full bundle →
   `docker load` of the image; data backup → unpacked into a staging
   directory `./.rollback-stage.*` next to `./data` (via `./restore.sh
   --archive … --dest …` if it is present alongside — it also runs
   `PRAGMA integrity_check`; otherwise `tar` + integrity_check with
   `sqlite3`/`python3`). A broken archive or a corrupted DB stops here — the
   container is still running and the staging directory is removed;
3. only then stops the container; if data was being restored, moves the
   current `./data` aside to `./data.before-rollback-<time>` (in case the
   rollback itself turns out to be a mistake) and puts the staging directory
   in its place with a single `mv`;
4. if an image to roll back to was found (by tag or from the bundle), retags
   it as `onepage:latest` and starts the container with that image;
5. waits for `/health` (up to 30 s) and prints the response — its `build`
   field confirms which build is actually answering now.

Requires Docker permissions; in production use `sudo ./rollback.sh ...`
(see the production server facts: docker is only available via sudo).

### 6.3. Network rollback (host networking)

Separately from the build version: if you suddenly need to drop
`network_mode: host` temporarily (for example, to work around a port conflict
on the host):

1. Remove `network_mode: host` from `docker-compose.yml`.
2. Restore the block:
   ```yaml
   ports:
     - "${OP_PORT:-8743}:8743"
   ```
3. `docker compose up -d`.

## 7. Backup and restore

All OnePage state lives in a single data directory (`./data`, mounted at
`/data` — see `docker-compose.yml`): `onepage.db` (SQLite, WAL), `uploads/`
(dashboard/presentation/lake files on disk) and, next to the DB, the admin
runtime overlays `llm-runtime.json` (LLM keys, the file is kept at `0600`)
and `quests-runtime.json` (§2a). The data directory is backed up **as a
whole**: the DB and the files are useless separately (the DB holds references
to the files, the files mean nothing without the DB that knows whose
dashboard they belong to), and overlays left out of the archive mean silently
lost LLM keys and Quests falling back to env after a restore.

The archive and the directories it is written to are created with `0700`/
`0600` permissions: inside is the entire DB, including the `sessions` table
(value = the key of a live session) and `llm-runtime.json`. Nobody but the
one who wrote the backup should be able to read it.

### Scripts

On the host where the bundle's `docker-compose.yml` is deployed
(`deploy/make-bundle.sh` puts them in the same place, next to
`docker-compose.yml` — the repository is not cloned on the target machine,
see §0), the scripts are directly in `~/onepage/`: `backup.sh`, `restore.sh`,
`restore-drill.sh`. In the repository the same files are in `deploy/`.

- **`backup.sh --dest DIR [--dest2 DIR2] [--db PATH] [--uploads DIR] [--retain N]`**
  Takes a consistent copy of the DB through the SQLite backup API
  (`sqlite3 .backup`, or `sqlite3.backup()` in Python if the `sqlite3` binary
  is not on the host) — this is safe under WAL, does not require stopping the
  application, and does not read the DB file while it is being written. It
  then archives the ENTIRE directory containing the DB (the parent of
  `--db`), substituting the snapshot for the live file and excluding
  `-wal`/`-shm` (the live file's unfinished WAL) and `perflog/` (telemetry,
  not data); `uploads/` is added to the archive as a separate entry without
  an intermediate copy on disk (important: `uploads/_lake` can weigh ~1 GB,
  `OP_LAKE_MAX_MB`) — this works both when `--uploads` is inside the DB
  directory (the default) and when it is given separately. All of this goes
  into one `onepage-backup-<UTC-timestamp>.tar.gz` in `--dest` (permissions
  `0600`, destination directory `0700`); if `--dest2` is given, the archive is
  copied there as well (a second disk/share, in case the first location
  fails). Only the last `--retain` archives are kept in each destination
  directory (default 14). It writes nothing to and locks nothing in
  `--db`/`--uploads` for longer than the copy itself takes. The exit code is
  ≠ 0 if the backup failed (no source DB, destination not writable, `tar`
  failed).

- **`restore.sh --archive PATH --dest DIR`**
  Unpacks the archive into `DEST` (`onepage.db`, `uploads/`,
  `llm-runtime.json`, `quests-runtime.json` — whatever was in the archive is
  all restored side by side, permissions `0700`/contents as in the archive)
  and immediately checks the restored DB with `PRAGMA integrity_check` — if
  it does not return `ok`, the script exits with a non-zero code and does NOT
  report the restore as successful. It replaces nothing "in place" by itself
  — the operator decides what to do with `DEST` next (usually: stop the
  container, replace `data/`, start it again).

- **`restore-drill.sh [--db PATH] [--uploads DIR] [--image TAG] [--python PATH]`**
  A full drill cycle: backup → restore into a temporary directory → start the
  application on a free port (**only** `127.0.0.1`, `http`) with
  `OP_DB_PATH`/`OP_UPLOADS_DIR` pointing at the restored data → `GET /health`
  (and, for reference, `?deep=1`) → stop. Prints the time of each step and
  the outcome. It touches nothing in `data/` — the restore happens in a
  temporary directory that is removed at the end. The application is started
  either with `docker run --rm` on the `--image` image (default
  `onepage:latest` — the same tag `docker load` puts in from
  `onepage-image.tar.gz`; **this is the only mode that proves anything in
  production** — there are no sources and no `venv` there, only the loaded
  image), or, if no such image exists locally, with `--python entrypoint.py`
  from a repository checkout (for development). The script **always**
  rewrites `OP_SAML_SP_BASE_URL` to `http://127.0.0.1:<port>` and keeps
  `OP_DEV_BYPASS_AUTH=false` — the drill never brings up a password-less
  `/dev-login` on top of a copy of live data, and never inherits the
  production config (`OP_SAML_SP_BASE_URL=https://…`), which would otherwise
  fail `check_config()` (`app/services/preflight.py`) before startup. On a
  docker host, `docker run`/`docker load` usually require `sudo` (see "Prod
  server facts" — `docker` there is only available via `sudo`).

### Cron on the host

Daily at 02:00 (lowest load), rotating 14 copies, with a second copy on a
separate volume/share. Paths follow the bundle layout (`~/onepage/backup.sh`,
not `deploy/backup.sh` — there is no `deploy/` directory in production, see
above):

```cron
0 2 * * * cd ~/onepage && ./backup.sh \
    --dest /var/backups/onepage --dest2 /mnt/offsite/onepage-backups \
    --db data/onepage.db --retain 14 \
    >> /var/log/onepage-backup.log 2>&1
```

`--uploads` is deliberately omitted: by default `backup.sh` takes it from
next to the DB (`data/uploads`), which is exactly the production layout
(`docker-compose.yml` mounts a single `./data:/data`) — so the data directory
is archived as a whole in one pass, with no risk of drifting apart when
another file is added to `data/` in the future and nobody remembers to update
the cron entry.

Check the exit code (`$?` in a cron wrapper, or monitor the log file for the
`ERROR:` prefix that `backup.sh`'s `fail()` writes) — a backup that silently did
not run is worse than no backup: it gives a false sense of safety.

### RPO / RTO

- **RPO (Recovery Point Objective) — 24 hours.** A daily backup at 02:00
  means that in the worst case a data loss costs roughly the last day of
  changes. If that is unacceptable for a particular deployment, run cron more
  often (`backup.sh` is idempotent and safe to run more frequently; the cost
  is the `.backup` time and the archive size).
- **RTO (Recovery Time Objective) — 1 hour.** The time from the decision "we
  are restoring" to a working instance on the restored data: copy the archive
  to the target host, `restore.sh` into `data/` (or next to it, switching
  `docker-compose.yml`), `docker compose up -d`, smoke checklist (section 5).
  The restore itself takes seconds/minutes — the hour budgets for making the
  decision, getting access to the backup storage and the operator's manual
  steps.

### Restore drill — once a quarter

`restore-drill.sh` is the same restore as in a real incident, but in a
temporary directory and without stopping production, so it can be run on the
live host at no risk:

In production (the image is already loaded with `docker load`, see section 0/
the bundle README):

```bash
cd ~/onepage && sudo ./restore-drill.sh --db data/onepage.db
```

From a repository checkout (development, no image — starts `entrypoint.py`
in `.venv`):

```bash
deploy/restore-drill.sh --db data/onepage.db --python /path/to/.venv/bin/python
```

Once a quarter (the nearest scheduled day is the start of each calendar
quarter) run it in production and record the result (step times,
success/failure, application version) in `docs/DRILLS.md` (create it at the
first drill if it does not exist yet) or in the same tracker where the
OnePage operations log is kept. A drill whose result is not written down
anywhere is, from the next audit's point of view, no different from a drill
that never happened.
