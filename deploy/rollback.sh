#!/usr/bin/env bash
# Roll OnePage back to a previous build.
#
# Run ON THE TARGET MACHINE, from the directory with docker-compose.yml (the same place
# check.sh lives, see the bundle's README.md).
#
#   deploy/rollback.sh <image-tag>            # onepage:<sha>, already loaded
#                                              # with docker load (a previous
#                                              # bundle/patch), see `docker images`
#   deploy/rollback.sh <path-to-archive.tar.gz> # a full bundle (onepage-deploy-*)
#                                              # or a data directory backup
#                                              # (onepage-backup-*.tar.gz from
#                                              # deploy/backup.sh)
#
# What it does:
#   1. validates the argument WITHOUT side effects: for a tag, that an image with that tag
#      is already loaded into docker; for an archive, that it is either a full bundle (it contains
#      onepage-image.tar.gz) or a data directory backup in one of two
#      formats: "flat" from deploy/backup.sh (onepage.db, uploads/ and the
#      runtime overlays *.json at the archive root) or "nested" (all entries under
#      data/). Anything else is rejected before any change on disk;
#   2. PREPARES the rollback while the container is still running: full bundle → docker load
#      of the image; data backup → unpack into a staging directory next to ./data
#      (via deploy/restore.sh if it is next to it; it also runs
#      PRAGMA integrity_check). Not a single byte is written into the current deployment
#      directory (docker-compose.yml/.env) or into the live ./data;
#   3. only once the image is loaded / the data is unpacked and verified does it
#      stop the container; if data was restored, it moves the
#      current ./data to ./data.before-rollback-<time> and puts staging in
#      its place with a single mv;
#   4. if an image to roll back to was found, retags it as onepage:latest
#      (that is how docker-compose.yml sees it) and starts the container on that image;
#      if it was only a data backup, it simply restarts the container on the
#      image already in place;
#   5. waits for /health and prints the build (`build`) now answering;
#      compare it with the expected tag, that is the confirmation of the rollback.
#
# Important: the container is stopped ONLY after the argument has been validated and
# everything that can break (a corrupt archive, a corrupt DB, docker load) has already
# run. A typo in the tag/path or a damaged backup must not
# leave the service down with no auto-restart (restart: unless-stopped does not
# bring a container back after an explicit stop).
#
# Needs sudo for docker if your user is not in the docker group (production
# is exactly that case): `sudo deploy/rollback.sh ...`.
set -euo pipefail

ARG="${1:?usage: deploy/rollback.sh <image-tag|path-to-archive.tar.gz>}"
CONTAINER="${ONEPAGE_CONTAINER:-onepage-onepage-1}"
PORT="${ONEPAGE_PORT:-8743}"
HERE_DIR="$(cd "$(dirname "$0")" && pwd)"

TMP=""
STAGE_DIR=""
cleanup() {
  [ -n "$TMP" ] && rm -rf "$TMP"
  [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ] && rm -rf "$STAGE_DIR"
  return 0
}
trap cleanup EXIT

# PRAGMA integrity_check of the restored DB (the same logic as in restore.sh),
# for the path without restore.sh. Without sqlite3/python3: a warning, not a refusal.
integrity_check() {
  local db="$1" result=""
  if command -v sqlite3 >/dev/null 2>&1; then
    result="$(sqlite3 "$db" 'PRAGMA integrity_check;' 2>&1)" || {
      echo "!! PRAGMA integrity_check did not run: $result" >&2; return 1; }
  elif command -v python3 >/dev/null 2>&1; then
    result="$(python3 - "$db" <<'PYEOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    row = con.execute("PRAGMA integrity_check").fetchone()
    print(row[0] if row else "no result")
finally:
    con.close()
PYEOF
)" || { echo "!! PRAGMA integrity_check did not run" >&2; return 1; }
  else
    echo "    (neither sqlite3 nor python3 found, integrity_check skipped)"
    return 0
  fi
  if [ "$result" != "ok" ]; then
    echo "!! PRAGMA integrity_check failed: $result" >&2
    return 1
  fi
  echo "    integrity_check: ok"
}

# ---------------------------------------------------------------------------
# Step 1: validate the argument. Stop nothing and touch nothing on disk
# until we are sure the argument is valid; see the explanation in the file header.
# ---------------------------------------------------------------------------
TARGET_TAG=""
BACKUP_MODE=""   # "bundle" = full bundle with an image; "data" = data backup
DATA_LAYOUT=""   # "flat" = deploy/backup.sh format; "nested" = everything under data/

if [ -f "$ARG" ]; then
  echo "==> Argument is a file: $ARG"
  case "$ARG" in
    *.tar.gz | *.tgz) ;;
    *)
      echo "unknown archive type: $ARG (expected .tar.gz/.tgz)" >&2
      exit 1
      ;;
  esac

  TAR_LIST="$(tar -tzf "$ARG" 2>/dev/null)" || {
    echo "could not read $ARG as tar.gz (corrupt or not an archive)" >&2
    exit 1
  }

  # No absolute paths and no "..", in any format: unpacking goes into
  # a staging directory, and writing outside it is not allowed.
  ESCAPES="$(printf '%s\n' "$TAR_LIST" | grep -E '^/|(^|/)\.\.(/|$)' || true)"
  if [ -n "$ESCAPES" ]; then
    echo "!! archive $ARG has entries with an absolute path or '..', refusing to unpack:" >&2
    printf '%s\n' "$ESCAPES" | head -20 >&2
    exit 1
  fi

  if printf '%s\n' "$TAR_LIST" | grep -q 'onepage-image\.tar\.gz$'; then
    echo "==> Looks like a full bundle (deploy/make-bundle.sh)"
    BACKUP_MODE="bundle"
  elif printf '%s\n' "$TAR_LIST" | grep -qE '^(\./)?onepage\.db$'; then
    # The "flat" deploy/backup.sh format: onepage.db, uploads/ and the data directory's
    # runtime overlays (*.json) at the archive root. It is unpacked ONLY into
    # staging, but just in case we reject an archive where something from the
    # deployment directory (docker-compose.yml, .env, *.sh) sits next to the DB:
    # that is certainly not a data backup but someone else's archive.
    echo "==> Looks like a deploy/backup.sh data backup (onepage.db at the archive root)"
    BAD="$(printf '%s\n' "$TAR_LIST" | grep -E '^(\./)?(docker-compose\.ya?ml|\.env|[^/]*\.sh|onepage-image\.tar\.gz)$' || true)"
    if [ -n "$BAD" ]; then
      echo "!! refusing: deployment directory files sit next to onepage.db in the archive, this is not a data backup:" >&2
      printf '%s\n' "$BAD" | head -20 >&2
      exit 1
    fi
    BACKUP_MODE="data"
    DATA_LAYOUT="flat"
  else
    echo "==> Looks like a backup of the data/ directory, checking that the archive holds only ./data"
    # The current directory is the deployment directory (docker-compose.yml, .env with
    # SAML certs/OP_LLM_*_KEY/OP_INITIAL_ADMIN, check.sh). We require that
    # EVERY entry in the archive lies under ./data/ (or data/); otherwise it is not
    # a data backup but something else.
    BAD="$(printf '%s\n' "$TAR_LIST" | grep -v -E '^(\./)?data(/.*)?$' || true)"
    if [ -n "$BAD" ]; then
      echo "!! refusing to unpack $ARG: this does not look like a data backup, there is no onepage.db at the root and there are entries outside data/:" >&2
      printf '%s\n' "$BAD" | head -20 >&2
      echo "   expected either a deploy/backup.sh archive (onepage-backup-*.tar.gz) or an archive of the data/ directory." >&2
      exit 1
    fi
    if ! printf '%s\n' "$TAR_LIST" | grep -qE '^(\./)?data/onepage\.db$'; then
      echo "!! no data/onepage.db found in archive $ARG, does not look like a regular data backup" >&2
      exit 1
    fi
    BACKUP_MODE="data"
    DATA_LAYOUT="nested"
  fi
else
  # A bare tag/sha: must already be loaded into docker (by a previous
  # make-bundle.sh/make-patch.sh + docker load, or left on the host
  # by a previous run of this same script).
  case "$ARG" in
    onepage:*) TARGET_TAG="$ARG" ;;
    *) TARGET_TAG="onepage:$ARG" ;;
  esac
  if ! docker image inspect "$TARGET_TAG" >/dev/null 2>&1; then
    echo "image $TARGET_TAG not found locally. Available:" >&2
    docker images --format '  {{.Repository}}:{{.Tag}}' | grep '^  onepage:' >&2 || true
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Step 2: prepare the rollback while the container is still running. The image is loaded into docker,
# the data is unpacked and verified in staging; the live ./data and the deployment
# directory are not touched. Any error here exits with no downtime.
# ---------------------------------------------------------------------------
STAGED_DATA=""

if [ "$BACKUP_MODE" = "bundle" ]; then
  echo "==> Unpacking the image from the bundle"
  TMP="$(mktemp -d)"
  tar -xzf "$ARG" -C "$TMP"
  IMG_TGZ="$(find "$TMP" -name 'onepage-image.tar.gz' | head -1)"
  if [ -z "$IMG_TGZ" ]; then
    echo "onepage-image.tar.gz not found in the archive" >&2
    exit 1
  fi
  LOADED="$(gunzip -c "$IMG_TGZ" | docker load -q | sed -n 's/^Loaded image: //p')"
  if [ -z "$LOADED" ]; then
    echo "docker load did not report an image name, check the output by hand" >&2
    exit 1
  fi
  # make-bundle.sh puts TWO tags on the image (onepage:latest and onepage:<sha>),
  # so docker load prints two "Loaded image: ..." lines. We take the tag
  # other than onepage:latest, which unambiguously points to a specific
  # build; if there is none (an old single-tag bundle), we take the first line.
  TARGET_TAG="$(printf '%s\n' "$LOADED" | grep -v '^onepage:latest$' | head -1)"
  if [ -z "$TARGET_TAG" ]; then
    TARGET_TAG="$(printf '%s\n' "$LOADED" | head -1)"
  fi
  rm -rf "$TMP"; TMP=""
elif [ "$BACKUP_MODE" = "data" ]; then
  # Staging sits next to ./data (same filesystem): the final mv is atomic
  # and does not double the space taken by uploads/.
  STAGE_DIR="$(mktemp -d "$(pwd)/.rollback-stage.XXXXXX")"
  chmod 700 "$STAGE_DIR"
  if [ "$DATA_LAYOUT" = "flat" ] && [ -x "$HERE_DIR/restore.sh" ]; then
    echo "==> Unpacking the backup into staging via deploy/restore.sh (with integrity_check)"
    "$HERE_DIR/restore.sh" --archive "$ARG" --dest "$STAGE_DIR"
    STAGED_DATA="$STAGE_DIR"
  else
    echo "==> Unpacking the backup into a staging directory via tar"
    tar -xzf "$ARG" -C "$STAGE_DIR"
    if [ "$DATA_LAYOUT" = "nested" ]; then
      STAGED_DATA="$STAGE_DIR/data"
    else
      STAGED_DATA="$STAGE_DIR"
    fi
    if [ ! -f "$STAGED_DATA/onepage.db" ]; then
      echo "!! onepage.db not found in staging after unpacking, the archive is not in the regular format" >&2
      exit 1
    fi
    integrity_check "$STAGED_DATA/onepage.db"
  fi
fi

# ---------------------------------------------------------------------------
# Step 3: everything is ready; now the container can be stopped and the data swapped.
# ---------------------------------------------------------------------------
echo "==> Stopping container $CONTAINER"
if [ -f docker-compose.yml ]; then
  docker compose stop onepage 2>/dev/null || true
else
  docker stop "$CONTAINER" >/dev/null 2>&1 || true
fi

RESTORED_DATA=0
if [ -n "$STAGED_DATA" ]; then
  echo "==> Replacing ./data with the restored data"
  if [ -d ./data ]; then
    SAVE_AS="./data.before-rollback-$(date +%Y%m%d%H%M%S)"
    mv ./data "$SAVE_AS"
    echo "    current data saved to $SAVE_AS (do not delete until you have verified the rollback)"
  fi
  mv "$STAGED_DATA" ./data
  RESTORED_DATA=1
fi

if [ -n "$TARGET_TAG" ]; then
  echo "==> Retagging $TARGET_TAG -> onepage:latest (that is how docker-compose.yml sees it)"
  docker tag "$TARGET_TAG" onepage:latest
elif [ "$RESTORED_DATA" -eq 1 ]; then
  echo "==> Image unchanged, only data was rolled back, starting the current onepage:latest"
fi

echo "==> Starting the container"
if [ -f docker-compose.yml ]; then
  docker compose up -d --force-recreate onepage
else
  docker start "$CONTAINER"
fi

echo "==> Waiting for /health"
BODY=""
RETRIES="${ONEPAGE_HEALTH_RETRIES:-30}"
for _ in $(seq 1 "$RETRIES"); do
  BODY="$(curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null || true)"
  [ -n "$BODY" ] && break
  sleep 1
done

if [ -z "$BODY" ]; then
  echo "!! /health did not answer within ${RETRIES}s, see docker compose logs onepage" >&2
  exit 1
fi

echo "==> /health: $BODY"
echo
echo "Compare the \"build\" field above with the expected one: this confirms the rollback was applied."
