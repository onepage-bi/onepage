# OnePage — deployment on a corporate machine

> ## ⚠ If OnePage is ALREADY running for you
>
> If the machine hosts a live instance with reports and links already sent
> out — **you need `MIGRATION.md`, not this file.** It has the upgrade
> procedure: how to keep the data, how to rehearse on a copy without downtime,
> what exactly can break (exactly one thing — SAML signature verification) and
> how to roll back.
>
> `/d/{id}` and `/s/{token}` links **do not break** on upgrade, and users are
> not logged out — all of that is rows in the database, and the database lives
> outside the container. But the order of steps matters: one wrong flag in
> `docker compose down` destroys the data volume.
>
> This file is for installing **from scratch**.

This directory is self-contained: the target machine **does not need** internet
access. The image is already built, with dependencies inside it.

```
MIGRATION.md                 ← upgrade of an ALREADY running instance
onepage-image.tar.gz         prebuilt image (no internet needed to build)
docker-compose.yml           host networking variant (nginx on the host)
docker-compose.traefik.yml   variant for a site with Traefik
env.template                 → copy to .env and fill in
nginx-onepage.conf           sample reverse proxy (for the first variant)
check.sh                     post-start check
backup.sh                    backup of the data directory (see §7)
restore.sh                   restore from a backup + integrity_check
restore-drill.sh             restore drill (once a quarter)
BUILD-INFO.txt               which commit it was built from
SHA256SUMS                   image checksum
```

## 0. Which variant to choose

| You have | File | Why |
|---|---|---|
| **Traefik** (or another proxy in a container) | `docker-compose.traefik.yml` | the proxy goes over the Docker network; no port is published, no `docker-proxy` in the path |
| nginx **on the host** | `docker-compose.yml` | host networking, the proxy goes to `127.0.0.1:8743` |

If you have Traefik, **do not use host networking**, even though the main file
sets it. The reason is not convenience: the measured +34% p50 / +41% p99 for
bridge applies to the path through a **published port** (`docker-proxy`,
userland forwarding), while Traefik reaches the application over the Docker
network, where that hop does not exist at all. There is no port to publish and
nothing to work around. On top of that, host networking breaks Traefik's
label-based discovery: such a container simply has no address on the Docker network.

From here on all commands are the same; just pass your file with `-f`.

---

## 1. Machine requirements

| Requirement | Check |
|---|---|
| **Linux** (not Docker Desktop) | `uname -s` → `Linux` |
| Architecture matches the image | `uname -m` → compare with `BUILD-INFO.txt` |
| Docker with the compose plugin | `docker compose version` |
| Port **8743** is free | `ss -ltnp \| grep 8743` → empty |
| Space for data | image ~400 MB + reports |

**Why Linux specifically.** The configuration uses `network_mode: host`, which
does not work on Docker Desktop (Mac/Windows). The reason is not cosmetic:
publishing a port through `docker-proxy` costs +34% p50 and +41% p99 latency,
measured on a test bench. If host networking is unavailable for some reason,
replace the line `network_mode: host` in `docker-compose.yml` with

```yaml
    ports:
      - "8743:8743"
```

and know that you are paying those percentages.

---

## 2. Prepare in advance (before going to the machine)

Three things that come from people, not from the machine:

1. **person_id of the first administrator** — usually your employee number.
   Without it nobody will be able to log in later.
2. **ADFS signing certificate** — from the ADFS administrator, or from the
   federation metadata:
   ```bash
   curl -s https://adfs.example.com/FederationMetadata/2007-06/FederationMetadata.xml \
     | grep -o '<X509Certificate>[^<]*' | head -1 | cut -c18-
   ```
   You need the body as a single line, without `BEGIN`/`END`.
3. **TLS certificate** for `onepage.example.com` on the reverse proxy, and a DNS record.

Items 2 and 3 can be finished later — see section 6. Item 1 is needed right away.

---

## 3. Deployment

```bash
# 0. Unpack the bundle and enter it
tar -xzf onepage-deploy-*.tar.gz && cd onepage-deploy-*

# 1. Verify the image integrity
sha256sum -c SHA256SUMS

# 2. Load the image (a minute or two)
gunzip -c onepage-image.tar.gz | docker load

# 3. The data directory — BEFORE the first start.
#    If you skip this, Docker creates the mount point itself, and there is a
#    chance the database ends up inside the container and is lost on re-creation.
mkdir -p data

# 4. Fill in the site settings
cp env.template .env
${EDITOR:-nano} .env          # five values, each explained in the file

# 5. Start
docker compose up -d

# 6. Check
./check.sh
```

If `check.sh` showed only "ok", go to section 5.

---

## 4. If the container does not start — that is most likely by design

OnePage now **refuses to start** on a configuration that cannot work, and
prints a list of problems with what to do about each. Previously such an
instance came up successfully and answered `/health: ok` while being a system
nobody could log in to — that is exactly why the check was added.

```bash
docker compose logs onepage | tail -40
```

Look for the `OnePage refused to start` block. The most common cases:

| Message | What to do |
|---|---|
| `OP_SAML_STRICT is true but OP_SAML_IDP_CERT is empty` | no ADFS certificate yet — uncomment `OP_SAML_STRICT=false` in `.env`, start up, finish section 6, set it back to `true` |
| `no active administrator` | fill in `OP_INITIAL_ADMIN` in `.env` |
| `https … but OP_SAML_HTTPS is false` | set `OP_SAML_HTTPS=true` |
| `OP_UPLOADS_DIR is not usable` | `./data` was not created or is inaccessible — go back to step 3 |

After editing `.env`: `docker compose up -d` (re-creates the container).

---

## 5a. If you have Traefik

The application publishes its port **nowhere** — Traefik finds it by labels
inside a shared Docker network. The firewall plays no role here: nothing is
exposed to the outside.

```bash
# Name of the network Traefik lives in, and its subnet
docker network ls
docker network inspect traefik --format '{{(index .IPAM.Config 0).Subnet}}'
```

Add the Traefik block to `.env` (it is commented out in `env.template`):
`OP_HOSTNAME`, `TRAEFIK_NETWORK`, `TRAEFIK_ENTRYPOINT`, `TRAEFIK_CERTRESOLVER` and
— **mandatory** — `FORWARDED_ALLOW_IPS` with the subnet from the command above.

About `FORWARDED_ALLOW_IPS`: by default uvicorn trusts `X-Forwarded-*` headers
only from `127.0.0.1`, while Traefik connects from an address like
`172.18.0.x`. Without this line the perf and audit logs get Traefik's address
instead of the client's — quietly, without a single error. Give a subnet,
**never `"*"`**: the asterisk lets any client spoof its IP.

Start:

```bash
docker compose -f docker-compose.traefik.yml up -d
docker compose -f docker-compose.traefik.yml logs -f onepage
```

In this variant `./check.sh` will not find a listener on 8743 on the host —
that is expected, the port is not published. The other five checks apply; run
the check against the external address straight away:
`./check.sh https://onepage.example.com`.

## 5b. Reverse proxy and firewall (host networking variant)

OnePage listens on **HTTP** on 8743 and does not terminate TLS — a proxy is mandatory.

```bash
sudo cp nginx-onepage.conf /etc/nginx/conf.d/onepage.conf
sudo ${EDITOR:-nano} /etc/nginx/conf.d/onepage.conf   # certificate paths, server_name
sudo nginx -t && sudo systemctl reload nginx
```

**Close 8743 to the outside.** With host networking the application listens on
all of the machine's interfaces directly, with no Docker network isolation
between it and the outside world:

```bash
sudo ufw deny 8743/tcp        # or the equivalent firewalld/iptables rule
```

Only 80 and 443 should be reachable from outside.

> If the proxy is on **another** machine, uncomment `FORWARDED_ALLOW_IPS`
> in `docker-compose.yml` and put its address there. Never `"*"`: that lets
> any client spoof its IP in the perf and audit logs.

---

## 6. Connecting ADFS

1. Give the ADFS administrator the SP metadata:
   ```bash
   curl -s https://onepage.example.com/saml/metadata -o onepage-sp-metadata.xml
   ```
   They create a Relying Party Trust from it.
2. Get the signing certificate from them and put it into `OP_SAML_IDP_CERT` in `.env`.
3. Confirm the name of the claim carrying person_id. If you get it wrong, login
   fails with an error that **lists all received attributes**; use it to fix
   `OP_SAML_CLAIM_PERSON_ID`.
4. Set `OP_SAML_STRICT=true` back if you turned it off temporarily.
5. `docker compose up -d` and log in via SSO.

**A login by a real user is the only check that cannot be automated.**
`check.sh` has already verified everything else.

---

## 7. Operations

```bash
docker compose logs -f onepage      # logs (requests are not logged — by design)
docker compose restart onepage      # restart
docker compose down                 # stop
```

**Backup — `./backup.sh --dest DIR --db data/onepage.db`** (next to this
README — it is also in `~/onepage/` if the bundle was unpacked there). It takes
a consistent copy of the DB through the SQLite backup API and archives the
whole data directory with it (uploaded reports, LLM keys in
`llm-runtime.json`, the Quests overlay), excluding junk (the live file's WAL,
perf logs); the archive and the directories containing it get `0700`/`0600`
permissions. There is no need to stop the container (SQLite in WAL mode).
Restore — `./restore.sh --archive ARCHIVE --dest DIR` (runs
`PRAGMA integrity_check`). Restore drill (once a quarter, without touching
`data/`) — `sudo ./restore-drill.sh --db data/onepage.db`. Details, a cron
example and RPO/RTO are in `docs/DEPLOY.md` section 7 (in the repository; not
shipped in the bundle — keep a link to or copy of that section at hand).

**Upgrade.** Bring the new bundle, then:
```bash
gunzip -c onepage-image.tar.gz | docker load
docker compose up -d
```
`data/` is not touched. Rollback — load the previous image the same way.

---

## 8. One week after launch

Deploy and forget about it for 7 days, then collect the telemetry — it answers
a question nobody can answer today: **what OnePage's real load is.**

Open `/admin/perf` (administrator rights required) and export:

```
https://onepage.example.com/api/admin/perf/export?hours=168&format=jsonl
```

The `/admin/perf` page has a badge with the current sampling probability. If
it shows `sample 1.0 (all requests)`, the load is below the threshold, the
counters are exact, and the telemetry costs you nothing. That is also the
answer to the load question.

Send the export — it is used to tune `OP_PERF_TARGET_RPS` and to decide whether
further optimisation is needed at all.
