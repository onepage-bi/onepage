#!/usr/bin/env bash
# OnePage: restore drill (run once a quarter, see
# docs/DEPLOY.md, section "Backup and restore").
#
# Full cycle: back up the current DB/uploads → restore into a temporary directory
# → start the application on a free port with OP_DB_PATH/OP_UPLOADS_DIR
# pointing at the restored data → GET /health → stop.
# Touches nothing in the "production" data/: the whole restore happens in a
# temporary directory that is removed on exit.
#
# How the application is started (in order of priority):
#   1. --image / OP_DRILL_IMAGE, if such an image exists locally (`docker
#      image inspect`): `docker run` on it, exactly the way production
#      starts (docker-compose.yml from deploy/bundle/). This is the only mode
#      that proves anything on a docker-only host (see docs/DEPLOY.md
#      §0: sources and a venv never reach the target machine).
#   2. Otherwise, if entrypoint.py is next to it and the given --python exists: start
#      with a local interpreter (for development from a repository checkout; there
#      is no such checkout in production).
#   If neither works, the drill fails with a clear message
#   rather than silently checking nothing.
#
# IMPORTANT about OP_SAML_SP_BASE_URL / OP_DEV_BYPASS_AUTH: the drill never
# enables the passwordless dev-bypass. In production app/services/preflight.py already
# refuses to start with "bypass=true + SP-URL looks like
# https" (PreflightError), and besides, the bypass itself on a copy of production data
# would mean an open passwordless /dev-login over real rows,
# for no reason: GET /health (app/main.py) is not authenticated at all. Instead,
# the SP-URL here is ALWAYS forcibly rewritten to
# http://127.0.0.1:<port> (the drill listens on loopback only), so
# preflight never trips over any value inherited from the environment.
#
# Usage:
#   deploy/restore-drill.sh [--db PATH] [--uploads DIR] [--image TAG] [--python PATH]
#
# By default --db/--uploads come from OP_DB_PATH/OP_UPLOADS_DIR or
# data/onepage.db, data/uploads. --image is the image tag for docker mode
# (default onepage:latest, the same tag deploy/make-bundle.sh produces
# and deploy/bundle/docker-compose.yml loads). --python is the interpreter for
# mode 2 (default python3).
#
# Prints the time of each step and exits 0 when the drill fully succeeds.
set -uo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
# In the repository the script lives in deploy/, and entrypoint.py one level up.
# In the deployment bundle (deploy/make-bundle.sh) it sits next to an entrypoint-less
# set of files at the same level; there python mode is not needed and
# only docker mode works (see --image above), so here it is enough that
# entrypoint.py is "next to it or one level up", with no strict requirements.
if [ -f "$DIR/entrypoint.py" ]; then
  REPO_ROOT="$DIR"
else
  REPO_ROOT=$(cd "$DIR/.." && pwd)
fi

DB_PATH="${OP_DB_PATH:-data/onepage.db}"
UPLOADS_DIR="${OP_UPLOADS_DIR:-}"
IMAGE="${OP_DRILL_IMAGE:-onepage:latest}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--db PATH] [--uploads DIR] [--image TAG] [--python PATH]
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --db) DB_PATH="${2:-}"; shift 2 ;;
    --uploads) UPLOADS_DIR="${2:-}"; shift 2 ;;
    --image) IMAGE="${2:-}"; shift 2 ;;
    --python) PYTHON_BIN="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"; }
fail() { log "ERROR: $1"; cleanup; exit 1; }
now_ms() { "$PYTHON_BIN" -c 'import time; print(int(time.time()*1000))' 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))'; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/op-drill-XXXXXX") || { echo "mktemp failed" >&2; exit 1; }
chmod 700 "$WORK"
APP_PID=""
CONTAINER=""
CLEANED=0
cleanup() {
  [ "$CLEANED" = "1" ] && return
  CLEANED=1
  [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null
  [ -n "$CONTAINER" ] && docker stop -t 5 "$CONTAINER" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

BACKUP_DIR="$WORK/backups"
RESTORE_DIR="$WORK/restored"
mkdir -p "$BACKUP_DIR" "$RESTORE_DIR"

log "=== Restore drill: start (db=$DB_PATH uploads=${UPLOADS_DIR:-<next to db>/uploads}) ==="

t0=$(now_ms)
BACKUP_ARGS=("$DIR/backup.sh" --dest "$BACKUP_DIR" --db "$DB_PATH" --retain 14)
[ -n "$UPLOADS_DIR" ] && BACKUP_ARGS+=(--uploads "$UPLOADS_DIR")
"${BACKUP_ARGS[@]}" || fail "step 1/4 (backup) failed"
t1=$(now_ms)
log "step 1/4 backup: $((t1 - t0)) ms"

ARCHIVE=$(ls -1t "$BACKUP_DIR"/onepage-backup-*.tar.gz 2>/dev/null | head -1)
[ -n "$ARCHIVE" ] || fail "archive not found after backup"

"$DIR/restore.sh" --archive "$ARCHIVE" --dest "$RESTORE_DIR" \
  || fail "step 2/4 (restore) failed"
t2=$(now_ms)
log "step 2/4 restore + integrity_check: $((t2 - t1)) ms"

PORT=$("$PYTHON_BIN" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()') \
  || fail "could not find a free port"

# Always loopback and http: the drill never presents itself as a public
# host (see the comment in the file header about PreflightError and dev-bypass).
SP_URL="http://127.0.0.1:$PORT"

t3=$(now_ms)
MODE=""
if command -v docker >/dev/null 2>&1 && docker image inspect "$IMAGE" >/dev/null 2>&1; then
  MODE="docker"
  log "mode: docker run --rm on image $IMAGE (as in production, deploy/bundle/docker-compose.yml)"
  CONTAINER="op-drill-$$-$(date -u +%s)"
  docker run -d --rm --name "$CONTAINER" \
    -p "127.0.0.1:$PORT:$PORT" \
    -v "$RESTORE_DIR:/data" \
    -e OP_HOST=0.0.0.0 \
    -e OP_PORT="$PORT" \
    -e OP_DB_PATH=/data/onepage.db \
    -e OP_UPLOADS_DIR=/data/uploads \
    -e OP_SAML_SP_BASE_URL="$SP_URL" \
    -e OP_SAML_STRICT=false \
    -e OP_SAML_HTTPS=false \
    -e OP_DEV_BYPASS_AUTH=false \
    -e OP_INITIAL_ADMIN="${OP_INITIAL_ADMIN:-drill-admin}" \
    -e OP_CACHE_WARM=0 \
    -e OP_PERF_DIR=/data/perflog \
    "$IMAGE" >"$WORK/container.id" 2>"$WORK/docker.err" \
    || { cat "$WORK/docker.err" >&2; fail "docker run did not start"; }
  log "container started ($CONTAINER) on port $PORT from the restored data, waiting for /health..."
  IS_UP() { docker ps -q -f "name=^${CONTAINER}\$" | grep -q .; }
elif [ -f "$REPO_ROOT/entrypoint.py" ] && command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  MODE="python"
  log "mode: local interpreter ($PYTHON_BIN entrypoint.py): image $IMAGE not found locally."
  log "  (this is expected in a development repository checkout; on docker-only production"
  log "   you need --image with a locally loaded tag of the built image.)"
  export OP_DB_PATH="$RESTORE_DIR/onepage.db"
  export OP_UPLOADS_DIR="$RESTORE_DIR/uploads"
  export OP_HOST=127.0.0.1
  export OP_PORT="$PORT"
  export OP_SAML_SP_BASE_URL="$SP_URL"
  export OP_SAML_STRICT=false
  export OP_SAML_HTTPS=false
  export OP_DEV_BYPASS_AUTH=false
  export OP_INITIAL_ADMIN="${OP_INITIAL_ADMIN:-drill-admin}"
  export OP_PERF_DIR="$WORK/perflog"
  export OP_CACHE_WARM=0
  ( cd "$REPO_ROOT" && exec "$PYTHON_BIN" entrypoint.py ) > "$WORK/app.log" 2>&1 &
  APP_PID=$!
  log "application started (pid $APP_PID) on port $PORT from the restored data, waiting for /health..."
  IS_UP() { kill -0 "$APP_PID" 2>/dev/null; }
else
  fail "nothing to start the application with: neither image '$IMAGE' locally (docker image inspect) nor entrypoint.py+interpreter next to the script. Build/load the image (deploy/make-bundle.sh) and pass --image, or run the drill from a repository checkout with --python."
fi

UP=0
for _ in $(seq 1 60); do
  if ! IS_UP; then
    break
  fi
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then
    UP=1
    break
  fi
  sleep 0.5
done
t4=$(now_ms)

if [ "$UP" != "1" ]; then
  log "the application did not start or did not answer /health, log:"
  if [ "$MODE" = "docker" ]; then
    docker logs "$CONTAINER" 2>&1 | tail -n 40
  else
    tail -n 40 "$WORK/app.log" 2>/dev/null
  fi
  fail "step 3/4 (start + /health) failed"
fi
log "step 3/4 start ($MODE) + /health: $((t4 - t3)) ms"

DEEP=$(curl -s "http://127.0.0.1:$PORT/health?deep=1" 2>/dev/null || echo '')
if echo "$DEEP" | grep -q '"status": *"ok"'; then
  log "/health?deep=1: ok"
else
  log "warning: /health?deep=1 did not return ok: $DEEP"
fi

if [ "$MODE" = "docker" ]; then
  docker stop -t 5 "$CONTAINER" >/dev/null 2>&1
  CONTAINER=""
else
  kill "$APP_PID" 2>/dev/null
  wait "$APP_PID" 2>/dev/null
  APP_PID=""
fi
t5=$(now_ms)
log "step 4/4 stop: $((t5 - t4)) ms"

TOTAL=$((t5 - t0))
log "=== Drill completed successfully: backup=$((t1 - t0))ms restore=$((t2 - t1))ms start=$((t4 - t3))ms stop=$((t5 - t4))ms total=${TOTAL}ms ==="
exit 0
