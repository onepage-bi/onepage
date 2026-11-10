# Quick version

Upgrading a live instance. Details are in `MIGRATION.md`.

**You do not need to change your existing `docker-compose.yml`.** The settings
(ADFS addresses, certificate, Traefik labels) stay as they are — the new image
works with them. The five new variables have exactly the defaults production
needs, and the removed `OP_SESSION_SECRET` is simply ignored. **Only the image**
changes.

## On the Mac

```bash
deploy/make-bundle.sh linux/amd64
tar -czf dist/onepage-deploy.tar.gz -C dist onepage-deploy-*-amd64
scp dist/onepage-deploy.tar.gz SERVER:/tmp/
```

## On the server

```bash
cd /path/to/deployment          # where the working docker-compose.yml lives
docker info >/dev/null && echo "docker without sudo" || echo "ALL docker commands need sudo"
```

If the second command mentioned `sudo`, your user is not in the `docker`
group, and **the prefix is needed everywhere**, including `docker compose
down/up` and `check.sh` itself. Without it compose will not see the running
container and you will think the service is gone. `sudo` is included below; if
you do not need it, just drop it.

**1. Backup and a copy of the config:**

```bash
cp docker-compose.yml ~/compose.before-upgrade
sudo tar -czf ~/onepage-backup-$(date +%Y%m%d-%H%M).tar.gz ./data
```

`sudo` is mandatory for the backup: the container runs as root, and the
database files belong to it. Without it the archive is built with gaps, silently.

If you are not sure the data is in `./data`:
```bash
docker inspect "$(docker compose ps -q onepage)" --format '{{json .Mounts}}'
```

**2. Stop** (do NOT add `-v` — it wipes the volume with the database):

```bash
sudo docker compose down
```

**3. Load the image** (substitute your own path to the archive — it is wherever
you put it with `scp`, not necessarily `/tmp`):

```bash
tar -xzf ~/onepage-deploy.tar.gz -C /tmp
gunzip -c /tmp/onepage-deploy-*-amd64/onepage-image.tar.gz | sudo docker load
cp /tmp/onepage-deploy-*-amd64/check.sh . && chmod +x check.sh
```

`docker load` should print `Loaded image: onepage:latest`.

**4. One edit in your `docker-compose.yml`** — comment out the line
`build: .`:

```yaml
    # build: .
    image: onepage:latest
```

This is not a formality. If the sources are lying on the server after all
(`app/`, `static/`, `Dockerfile` next to compose), they are an old version, and
compose may build the image from them, silently rolling you back with a deploy
that looks successful.

**5. Start:**

```bash
sudo docker compose up -d
sudo docker compose logs -f onepage
```

**6. Check:**

```bash
sudo ./check.sh https://onepage.example.com
```

The `[ ~~]` mark means "the script could not determine this", not a failure.
Two typical ones: the server itself often cannot resolve its own external name
(split DNS) — then check from your machine with
`curl -sI https://onepage.example.com/health`; and the published port under
Traefik — the ADR-2 penalty was not measured on that path, it does not concern you.

Then by hand: SSO login → report list → open a report → **open an old
`/s/{token}` link**.

---

## If something is wrong

**SSO login fails.** The only change that can cause this: the SAML assertion
signature is now verified. Add to the `environment:` of your compose and restart:
```yaml
      OP_SAML_WANT_ASSERTIONS_SIGNED: "false"
```
Then ask the ADFS administrator to enable signing and set it back to `"true"`.

**`no active administrator` in the log** — the wrong database got attached. Do
NOT create a new admin: check that the same data directory/volume is mounted.

**Full rollback** (the old image has not gone anywhere):
```bash
docker compose down
cp ~/compose.before-upgrade docker-compose.yml
docker compose up -d
```

**Restore the data:**
```bash
docker compose down
rm -rf ./data && mkdir data
tar -xzf ~/onepage-backup-*.tar.gz
docker compose up -d
```

---

## Optional, but worth doing later

**Real client IP in the logs.** Traefik connects from a `172.x.x.x` address,
and uvicorn trusts `X-Forwarded-*` only from `127.0.0.1` — so the perf and audit
logs currently record Traefik's address. It was like that before too, not a
regression. Fixed with one line in `environment:`:
```yaml
      FORWARDED_ALLOW_IPS: "172.18.0.0/16"   # docker network inspect traefik \
                                             #   --format '{{(index .IPAM.Config 0).Subnet}}'
```
Never `"*"` — that would let any client spoof its IP.

**Sampler settings.** The defaults (`TARGET_RPS=200`) are sized for a load we
know nothing about yet. After a week, take an export and tune them:
```
https://onepage.example.com/api/admin/perf/export?hours=168&format=jsonl
```
