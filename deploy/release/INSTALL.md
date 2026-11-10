# OnePage: install on a server (no internet needed)

You need: a Linux server with Docker and the `docker compose` plugin, and the
archive you downloaded from the GitHub Release. Unpack it and work inside the
unpacked folder:

```bash
tar -xzf onepage-*.tar.gz && cd onepage-*/
sha256sum -c SHA256SUMS        # optional: check the image file arrived intact
```

The image inside is built for one CPU architecture (the file name ends in
`amd64` or `arm64`; check yours with `uname -m`: `x86_64` = amd64).

## Install in three steps

**1. Load the image**

```bash
docker load < onepage-image.tar.gz
```

**2. Configure: address, admin, password**

```bash
cp env.template .env
docker run --rm -it onepage:latest python -m app.auth.local hash
```

The second command asks for a password twice (nothing is shown while you
type) and prints one line starting with `pbkdf2_sha256$600000$...`. That line
is the password hash; the password itself is stored nowhere.

Edit `.env` and set three things:

```bash
OP_SAML_SP_BASE_URL=http://<server-name-or-ip>:8000
OP_INITIAL_ADMIN=admin
OP_LOCAL_USERS='admin=pbkdf2_sha256$600000$<salt>$<digest>'
```

* `OP_SAML_SP_BASE_URL` is the address people will type in the browser. (The
  name says SAML for historical reasons; it is just the app's own address.)
* `OP_LOCAL_USERS` is `<user>=<hash>`. The user name must equal
  `OP_INITIAL_ADMIN`. **Put the whole value in single quotes and paste the hash
  exactly as printed**: `$` characters stay as they are. (Without the quotes you
  would have to write every `$` as `$$`; with quotes, never do both.)
* Keep the `.env` file private: `chmod 600 .env`.

**3. Start**

```bash
docker compose up -d
```

Wait ~15 seconds, open the address from `.env`, and sign in as `admin` with the
password you chose. If the page does not open, `docker compose logs` shows why
(a startup check prints what is wrong and how to fix it). `curl localhost:8000/health`
answers `ok` when the server is up.

The database and uploaded reports live in `./data` next to the compose file.

## Behind HTTPS

Plain http is fine for a first look. For real use put HTTPS in front: over
plain http the browser withholds the session cookie from files a report loads
by itself (a report shipped with its own extra scripts or data files shows
"Session expired" for them). Single-file reports are not affected.

Any reverse proxy with a certificate will do (nginx, Caddy, Traefik, a
corporate gateway). Then:

1. In `.env` set `OP_SAML_SP_BASE_URL=https://onepage.yourcompany.com`
   **and** `OP_SAML_HTTPS=true`. The two must agree: with an `https://` address
   and `OP_SAML_HTTPS=false` the app refuses to start, because report pages
   would show "Session expired".
2. Set `OP_BIND=127.0.0.1` in `.env` so only the proxy can reach the app.
3. Proxy to `http://127.0.0.1:8000` and pass `Host`, `X-Forwarded-For`,
   `X-Forwarded-Proto`. A ready nginx config is in `nginx-onepage.conf`
   (edit the server name and certificate paths).
4. `docker compose up -d` again.

Do not set `OP_SAML_HTTPS=true` while you still open the site over plain http:
the browser will drop the login cookie.

## Adding users

Two steps, because an account and a password are separate things:

1. As admin, open **Administration** and add the person (user name, role).
2. Make a hash for them (`docker run --rm -it onepage:latest python -m app.auth.local hash`),
   append `,<user>=<hash>` to `OP_LOCAL_USERS` in `.env` (inside the same single
   quotes), then `docker compose up -d` to apply it.

To change a password, replace that user's hash the same way.

## Backup

```bash
./backup.sh --dest /path/to/backups        # consistent copy of ./data (database + uploads)
./restore.sh --archive <file> --dest <dir> # restore into an empty directory and verify
```

`backup.sh` needs `sqlite3` or `python3` on the host. Run it from cron; the
archive contains sessions and settings, so keep the destination private.
`restore-drill.sh` practises a restore on a scratch copy.

## Upgrade

Unpack the new archive in a new folder (or over the old one, keeping `.env` and
`data/`), then:

```bash
docker load < onepage-image.tar.gz
docker compose up -d
```

The database is migrated on start. Take a backup first.

## Optional features

Switch these on in `.env` (commented lines in `env.template`), then
`docker compose up -d`:

* `OP_EXPLORE_ENABLED=true`: the Explore page (preview, see below).
* `OP_QUESTS_ENABLED=true`: the Quests (gamification) section.
* Single sign-on: OIDC (`OP_OIDC_ISSUER`, `OP_OIDC_CLIENT_ID`, `OP_OIDC_CLIENT_SECRET`)
  and SAML. Both are described in `docs/DEPLOY.md` and `.env.example` in the
  source repository. Add the variables to the `environment:` block of
  `docker-compose.yml` as `NAME: "${NAME:-}"` lines.

## What does not work without extra assets

Be aware before you promise anything to your users:

* **Reports, access control, versions, sharing, presentations, admin: work fully offline.**
* **Report generators** (Python run in the analyst's browser) work offline
  too: the Pyodide runtime they need is inside this image and is served by
  your own server. Generators and folder access need a **Chromium-based
  browser** (Chrome, Edge).
* **Explore, engine 2 (DuckDB-WASM)** needs ~105 MB of extra files that are
  *not* in this image (built with `make assets` in the source repository, which
  needs internet). Without them Explore does not work offline. It is off by
  default and a preview feature.
* SAML single sign-on needs the optional `requirements-saml.txt` packages; the
  image contains them, but nobody outside the original company has run it
  against a live identity provider. OIDC was tested only against a stub.

The full list is in the README, sections "Optional parts" and "Status".
