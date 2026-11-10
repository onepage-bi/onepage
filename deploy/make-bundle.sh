#!/usr/bin/env bash
# Build a portable OnePage deployment bundle.
#
# Run HERE (on the development machine, which has internet and the sources), not
# on the target machine. The output is one directory that can be carried over as a whole.
#
# Why a bundle at all, rather than `git clone && docker compose up` on site:
#   * the repository is private, the target machine cannot clone it;
#   * building the image needs outbound internet (apt-get + pip inside the
#     Dockerfile), which the internal network usually does not have;
#   * this machine is arm64 while the target is almost certainly x86_64: an image built
#     without --platform simply will not start there ("exec format error").
#
# Usage:
#   deploy/make-bundle.sh                 # linux/amd64 (default)
#   deploy/make-bundle.sh linux/arm64     # if the target machine is ARM
#
# Editions (default: the upgrade kit for the author's own SAML/ADFS production):
#   deploy/make-bundle.sh --edition release [PLATFORM]    (or EDITION=release)
#     The "download and run" kit for a company that has never seen OnePage:
#     files from deploy/release/ (password sign-in, first install, INSTALL.md).
#     Output: dist/onepage-<VERSION>-<arch>/ ; VERSION env var, default = date.
set -euo pipefail

EDITION="${EDITION:-upgrade}"
PLATFORM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --edition) EDITION="${2:?--edition needs a value}"; shift 2 ;;
    --edition=*) EDITION="${1#*=}"; shift ;;
    *) PLATFORM="$1"; shift ;;
  esac
done
PLATFORM="${PLATFORM:-linux/amd64}"
case "$EDITION" in upgrade|release) ;; *) echo "!! unknown edition: $EDITION (upgrade|release)" >&2; exit 1 ;; esac
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d)"
ARCH_TAG="${PLATFORM##*/}"
if [ "$EDITION" = release ]; then
  OUT="$REPO/dist/onepage-${VERSION:-$STAMP}-$ARCH_TAG"
else
  OUT="$REPO/dist/onepage-deploy-$STAMP-$ARCH_TAG"
fi

echo "==> Bundle: $OUT"
echo "==> Platform: $PLATFORM"

if ! docker info >/dev/null 2>&1; then
  echo "!! Docker is not running. Start Docker Desktop and try again." >&2
  exit 1
fi

rm -rf "$OUT"
mkdir -p "$OUT"

echo "==> Self-hosted Pyodide: subset in static/pyodide/ (idempotent, ~20 MB on the first run)"
# The self-hosted copy must get into the image: the browser generator engine on corporate
# machines cannot load wasm from a CDN (CSP/proxy), as measured by the first live
# run on 2026-07-20. The directory is in .gitignore, so it is populated at build time.
python3 "$REPO/deploy/fetch-pyodide.py"

echo "==> Translation catalogues: checking sources against static/locales"
# The linter keeps three promises: no interface text bypasses the catalogue, en/zh-HK do not
# contain keys that no longer exist in ru.json, and the generated
# static/locales/*.js match app/locales/*.json. A mismatch in the last one
# would mean an image where a page asks for strings the server does not have.
python3 "$REPO/tools/i18n_check.py" --stats

GIT_SHA="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "==> Build version: $GIT_SHA (app/BUILD, goes into the image via COPY app/)"
echo "$GIT_SHA" > "$REPO/app/BUILD"

echo "==> Building the image for $PLATFORM (slow the first time: gcc and libxmlsec1 get installed)"
# --provenance=false: without it buildx puts a second manifest with the
# unknown/unknown platform (attestation) into the artefact. It is harmless, but the Docker version on
# the target machine is not known in advance, and the simpler the artefact, the fewer chances
# of a surprise on docker load.
# The engine assets are excluded from the main context by .dockerignore, so
# they travel as a named context. Upgrade edition: everything that is vendored
# here. Release edition: Pyodide only — generators work with no internet, and
# Explore (off by default) stays out to keep the archive lean.
ASSETS="$(mktemp -d)"
trap 'rm -rf "$ASSETS"' EXIT
if [ "$EDITION" = release ]; then ASSET_DIRS="pyodide"; else ASSET_DIRS="pyodide duckdb-wasm duckdb-ext arrow-esm xlsx-arrow"; fi
for d in $ASSET_DIRS; do
  if [ -d "$REPO/static/$d" ]; then
    cp -R "$REPO/static/$d" "$ASSETS/$d"
    echo "    engine asset: static/$d ($(du -sh "$REPO/static/$d" | cut -f1))"
  else
    echo "    engine asset: static/$d is absent here — not in the image"
  fi
done
[ -d "$ASSETS/pyodide" ] || { echo "!! static/pyodide is missing after fetch-pyodide.py — refusing to build an image without it." >&2; exit 1; }

docker buildx build \
  --platform "$PLATFORM" \
  --build-context "engine-assets=$ASSETS" \
  --provenance=false \
  --tag onepage:latest \
  --tag "onepage:$GIT_SHA" \
  --load \
  "$REPO"

echo "==> Exporting the image (a few hundred MB, compresses ~3x)"
# Both tags point to the same image: onepage:latest is what
# docker-compose.yml picks up; onepage:<sha> survives the NEXT load (which
# will move only latest to the new image) and stays on the target machine as
# the target for deploy/rollback.sh.
docker save onepage:latest "onepage:$GIT_SHA" | gzip -9 > "$OUT/onepage-image.tar.gz"

echo "==> Copying deployment files ($EDITION edition)"
if [ "$EDITION" = release ]; then
  cp "$REPO/deploy/release/docker-compose.yml"   "$OUT/"
  cp "$REPO/deploy/release/env.template"         "$OUT/"
  cp "$REPO/deploy/release/INSTALL.md"           "$OUT/"
  cp "$REPO/deploy/release/nginx-onepage.conf"   "$OUT/"
  [ -f "$REPO/deploy/release/install.sh" ] && { cp "$REPO/deploy/release/install.sh" "$OUT/"; chmod +x "$OUT/install.sh"; }
else
  cp "$REPO/deploy/bundle/docker-compose.yml"         "$OUT/"
  cp "$REPO/deploy/bundle/docker-compose.traefik.yml" "$OUT/"
  cp "$REPO/deploy/bundle/env.template"       "$OUT/"
  cp "$REPO/deploy/bundle/nginx-onepage.conf" "$OUT/"
  cp "$REPO/deploy/bundle/check.sh"           "$OUT/"
  cp "$REPO/deploy/rollback.sh"               "$OUT/"
  cp "$REPO/deploy/bundle/README.md"          "$OUT/"
  cp "$REPO/deploy/bundle/MIGRATION.md"       "$OUT/"
  cp "$REPO/deploy/bundle/QUICK.md"           "$OUT/"
  chmod +x "$OUT/check.sh" "$OUT/rollback.sh"
fi

# Backup/restore/drill: the same machine that loads the image via
# docker-compose.yml must be able both to take and to verify a backup;
# without this docs/DEPLOY.md refers to scripts that do not exist on the target host
# (the repository is not cloned there, see the header of this file).
cp "$REPO/deploy/backup.sh"        "$OUT/"
cp "$REPO/deploy/restore.sh"       "$OUT/"
cp "$REPO/deploy/restore-drill.sh" "$OUT/"
chmod +x "$OUT/backup.sh" "$OUT/restore.sh" "$OUT/restore-drill.sh"

# Provenance: without it, a month later nobody can say what exactly went to the machine.
{
  echo "OnePage deployment bundle"
  [ "$EDITION" = release ] && echo "edition:   $EDITION"
  echo "built:     $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  echo "platform:  $PLATFORM"
  echo "git:       $(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo 'no git')"
  echo "branch:    $(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '-')"
  echo "tree:      $(git -C "$REPO" status --porcelain 2>/dev/null | grep -qv '^??' && echo 'DIRTY (uncommitted changes present)' || echo 'clean')"
} > "$OUT/BUILD-INFO.txt"

echo "==> Checksums"
( cd "$OUT" && shasum -a 256 onepage-image.tar.gz > SHA256SUMS )

SIZE="$(du -sh "$OUT" | cut -f1)"
echo
echo "==> Done: $OUT  ($SIZE)"
echo
cat "$OUT/BUILD-INFO.txt"
echo
if [ "$EDITION" = release ]; then
  echo "Next: attach the .tar.gz below to a GitHub Release; users start from INSTALL.md."
else
  echo "Next: carry the whole directory over to the target machine and open README.md."
fi
echo "To pack it into one file for transfer:"
echo "  tar -czf ${OUT##*/}.tar.gz -C '$(dirname "$OUT")' '${OUT##*/}'"
