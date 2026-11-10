# Upgrading a live OnePage

This guide is for the case where OnePage is **already running in production**:
there are reports, users and links that have been sent out. If you are deploying
from scratch, you need `README.md`, not this file.

Read it in full before starting. Sections 1–3 are done in advance and without downtime.

> **If this is not your first time, use `QUICK.md`:** the same steps without
> explanations, one screen of commands. This file is for when something went
> wrong or you want to understand why the steps are what they are.

---

## 1. What survives the upgrade and what changes

**Everything that matters survives.** The data lives outside the container — in
the directory mounted at `/data`, and the image upgrade does not touch it:

| What | Where it lives | After the upgrade |
|---|---|---|
| Reports, uploaded files, slots | `data/uploads/` | in place |
| Dashboards, folders, permissions, audit | `data/onepage.db` | in place |
| **`/d/{id}` links** | the id is a row in the DB | **work** |
| **`/s/{token}` share links** | the token is a row in the DB | **work** |
| Logged-in users | sessions in SQLite | **stay logged in** |
| Perf logs | `data/perflog/` | in place |

**The application upgrades the DB schema itself** on the first start of the
new image: Alembic sees a database without `alembic_version`, adopts it as is
and brings it up to the current schema (adds missing columns and indexes —
deletes or rewrites nothing). It takes seconds, runs under a file lock, and a
repeated start changes nothing. Details and how to roll back are in
`docs/DEPLOY.md`, section "2c. Migrations". Make the backup from section 4
below in any case — it is the safety net for this step.

Links do not break because dashboard identifiers and share tokens are rows in
the database, not something generated at startup. As long as the database is
intact, so are the links.

**The DB schema migrates itself** — idempotent `ALTER TABLE` and
`CREATE INDEX IF NOT EXISTS` statements run at startup. A repeated run is harmless.

**What changes in behaviour:**

- **Requests are no longer written to the container log.** `docker compose logs`
  will no longer show lines like `"GET /render/... 200"` — this is deliberate:
  the uvicorn access log was measured at 91–96 µs per request and is turned
  off. Your reverse proxy keeps its own access log as before.
- **Under load the perf log becomes sampled.** Records get a `sample` field,
  and `/admin/perf` recalculates its estimates from it. Under low load
  everything is written, and the page shows a `sample 1.0 (all requests)` badge.
- **The application may refuse to start** on a configuration that cannot work
  (see section 7). Previously it came up in that situation and answered
  `/health: ok`.
- **The SAML assertion signature is now verified.** This is the only real
  risk — see the next section.

---

## 2. The only risk: assertion signature verification

Previously the SP accepted an **unsigned** assertion. Now it requires a
signature by default (`OP_SAML_WANT_ASSERTIONS_SIGNED=true`). ADFS signs
assertions by default, so in the vast majority of cases nothing changes.

But if your Relying Party Trust is configured differently, **login stops
working immediately after the switch**.

**The good news: rolling this back is one line, without reverting the whole release.**
If SSO stopped letting people in after the upgrade, add to `.env`:

```
OP_SAML_WANT_ASSERTIONS_SIGNED=false
```

and run `docker compose up -d`. Login is restored within seconds. After that,
ask the ADFS administrator to enable assertion signing and set it back to
`true` — without it anyone who can send a POST to `/saml/acs` can log in under
someone else's person_id.

**You can check in advance** with the ADFS administrator: in the Relying Party
Trust properties, the signature tab — whether assertions are signed. If you
cannot get an answer quickly, do not block the upgrade: keep the line above ready.

---

## 3. Inventory: what you have now (no downtime)

Run on the production machine; it changes nothing. **Save the output** — you
will need it both to carry over settings and for a rollback.

```bash
# 1. What is running and from which compose
docker ps
docker compose ls

# 2. Go to the directory with the old docker-compose.yml; everything below runs from there
cd /path/to/old/deployment

# 3. KEY POINT: where the data lives — a volume or a host directory
docker inspect "$(docker compose ps -q onepage)" \
  --format '{{json .Mounts}}' | python3 -m json.tool
```

In the output look at `"Type"`:

- `"bind"` — the data is a host directory, the path is in `"Source"`. The simple case.
- `"volume"` — the data is in a Docker volume, the name is in `"Name"`.
  **`docker compose down -v` destroys such a volume together with the database
  and reports. Do not use the `-v` flag.**

```bash
# 4. Current settings — the ADFS details are carried over from these
docker inspect "$(docker compose ps -q onepage)" \
  --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^OP_' > /tmp/old-op-env.txt
cat /tmp/old-op-env.txt

# 5. Current image — needed for a rollback
docker inspect "$(docker compose ps -q onepage)" --format '{{.Image}} {{.Config.Image}}'
```

Separately save the old `docker-compose.yml` and `.env`:

```bash
cp docker-compose.yml docker-compose.yml.before-upgrade
cp .env .env.before-upgrade 2>/dev/null || true
```

---

## 4. Backup (mandatory, no downtime)

SQLite in WAL mode allows copying on the fly, but a copy taken with the
container stopped is more reliable. The minimal sufficient option — on the fly:

```bash
# Substitute the path from step 3 (Source for bind) — ./data here as an example
tar -czf ~/onepage-backup-$(date +%Y%m%d-%H%M).tar.gz ./data
ls -lh ~/onepage-backup-*.tar.gz
```

If the data is in a **volume**:

```bash
docker run --rm -v <volume-name>:/from -v "$HOME":/to alpine \
  tar -czf /to/onepage-backup-$(date +%Y%m%d-%H%M).tar.gz -C /from .
```

**Check that the archive is not empty and contains the database:**

```bash
tar -tzf ~/onepage-backup-*.tar.gz | grep -E 'onepage\.db|uploads/' | head
```

Without this check a backup is a hope, not a copy.

---

## 5. Rehearsal on a copy (recommended, no downtime)

Lets you make sure the new version comes up on **your** data without touching
the running production. It takes about ten minutes and removes almost all of the risk.

```bash
mkdir -p ~/onepage-rehearsal && cd ~/onepage-rehearsal
tar -xzf ~/onepage-backup-*.tar.gz          # copy of the data
gunzip -c /path/to/bundle/onepage-image.tar.gz | docker load

cat > docker-compose.yml <<'YAML'
services:
  onepage:
    image: onepage:latest
    ports: ["8899:8743"]
    volumes: ["./data:/data"]
    environment:
      OP_HOST: "0.0.0.0"
      OP_PORT: "8743"
      OP_DB_PATH: "/data/onepage.db"
      OP_UPLOADS_DIR: "/data/uploads"
      # The rehearsal runs over http on localhost, so dev login is allowed:
      # preflight forbids it only on an https address.
      OP_SAML_SP_BASE_URL: "http://localhost:8899"
      OP_DEV_BYPASS_AUTH: "true"
      OP_SAML_STRICT: "false"
      OP_INITIAL_ADMIN: "YOUR_PERSON_ID"
      OP_CACHE_WARM: "5"
YAML

docker compose up -d
sleep 20
curl -s "http://localhost:8899/health?deep=1"
```

What to check in the browser at `http://localhost:8899`:

1. The report list opens and contains **all your reports**.
2. A report opens and renders (this checks both rendering and data slots).
3. `/admin/perf` opens and shows the sampler badge.

What the rehearsal **cannot** check: SSO login — the ACS address is bound to the
real host name. That is the only thing that can be checked only in production.

```bash
docker compose down          # without -v
cd ~ && rm -rf ~/onepage-rehearsal
```

---

## 6. Switch-over (downtime starts here, 2–5 minutes)

```bash
cd /path/to/old/deployment

# 1. Stop the old one. WITHOUT -v — otherwise the data volume is destroyed.
docker compose down

# 2. Make sure the port is free and nothing is left
docker ps -a | grep -i onepage
ss -ltnp | grep 8743
```

### Keep your compose — there is no need to change it

This is the main and non-obvious point: **your existing `docker-compose.yml`
works with the new image as is.** All the settings — ADFS addresses,
certificate, Traefik labels, data mounts — are already described correctly in
it, and rewriting them from the template means reproducing by hand something
that already works, with a risk of getting it wrong.

Why this is safe (checked against the configuration of your build versus the current one):

- Five variables were added — `OP_ACCESS_LOG`, `OP_PERF_ADAPTIVE`,
  `OP_PERF_TARGET_RPS`, `OP_PERF_SAMPLE_FLOOR`,
  `OP_SAML_WANT_ASSERTIONS_SIGNED`. All of them default to exactly what
  production needs, so their absence from your file is normal, not a problem.
- One was removed — `OP_SESSION_SECRET`. If it is still in your
  `environment:`, nothing happens: unknown `OP_*` variables are ignored.

You only need the bundle's `docker-compose.yml`, `docker-compose.traefik.yml`
and `env.template` as a **reference** (to see what is new) or if one day you
deploy from scratch.

```bash
# 3. Load the new image
gunzip -c onepage-image.tar.gz | docker load

# 4. Take only the check script from the bundle
cp check.sh .

# 5. The ONLY edit in your docker-compose.yml: comment out build,
#    otherwise compose tries to build from sources that are not on the server
#        # build: .
#        image: onepage:latest
${EDITOR:-nano} docker-compose.yml

# 6. Start and watch the logs
docker compose up -d
docker compose logs -f onepage
```

**If the data was in a volume rather than a bind directory**, there is nothing
to do: your compose already refers to that volume, and you are not touching it.

**The first start may take longer than usual:** the render cache warms up (20
recent reports plus the 5 heaviest). It is a background thread and does not
block readiness — the application responds right away.

---

## 7. If the container refused to start

This is most likely **the new version's intended behaviour**, not a failure. Read:

```bash
docker compose logs onepage | grep -A 30 "refused to start"
```

| Message | Cause during an upgrade | What to do |
|---|---|---|
| `no active administrator` | the database was not attached — the container sees an **empty** DB | stop, check the mount from section 6: the data path is wrong |
| `OP_SAML_STRICT is true but OP_SAML_IDP_CERT is empty` | the certificate was not carried over from the old config | take it from `/tmp/old-op-env.txt` |
| `https … but OP_SAML_HTTPS is false` | `OP_SAML_HTTPS` was not carried over | set it to `true` |
| `OP_UPLOADS_DIR is not usable` | the data directory is not mounted or lacks permissions | see section 6 |

The first row of the table is the most important: **"no administrator" during
an upgrade almost always means the wrong database got attached.** Do not create
a new admin — find the old database, otherwise you get an empty instance on top
of intact data.

---

## 8. Checks after the switch-over

```bash
./check.sh https://onepage.example.com
```

Then by hand, in the browser:

1. **SSO login by a real user.** The only check that cannot be automated. If it
   does not let you in — section 2, the line about
   `OP_SAML_WANT_ASSERTIONS_SIGNED`.
2. **Report list** — everything is in place.
3. **Open a report** — it renders, data loads.
4. **Open a previously sent `/s/{token}` share link** — be sure to check at
   least one real one: it proves the database was attached correctly better
   than any other check.
5. `/admin/perf` — the page opens, the sampler badge is in place.

---

## 9. Rollback

Full rollback — if something went wrong at the application level:

```bash
cd /path/to/old/deployment
docker compose -f docker-compose.traefik.yml down       # the new one, without -v
cp docker-compose.yml.before-upgrade docker-compose.yml
cp .env.before-upgrade .env 2>/dev/null || true
docker compose up -d                                    # the old image is still on the system
```

The old image has not gone anywhere — `docker load` of the new one does not
overwrite it if the tags differ. To check it is there: `docker images | grep -i onepage`.

The data needs no rollback: the new version only extends the schema
(`ALTER TABLE ADD COLUMN`, `CREATE INDEX IF NOT EXISTS`), deleting and rewriting
nothing, so the old version reads the upgraded database without problems.

If the data did get damaged after all — restore from the section 4 archive:

```bash
docker compose down
rm -rf ./data && mkdir data
tar -xzf ~/onepage-backup-<stamp>.tar.gz
docker compose up -d
```

---

## 10. One week later

Take a telemetry export — it is honest now, because this is the first
production with host networking (or Traefik without docker-proxy) and without
the access log:

```
https://onepage.example.com/api/admin/perf/export?hours=168&format=jsonl
```

It answers a question nobody has an answer to today: what OnePage's real load
is. The tuning of `OP_PERF_TARGET_RPS` and whether further optimisation is
needed at all depend on it.
