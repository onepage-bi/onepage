#!/usr/bin/env bash
# OnePage: backup of the data directory (data/): the DB plus everything
# next to it.
#
# Takes a consistent copy of the database through the SQLite backup API (correct under
# WAL: it does not read the file "as is" while writes are in progress), then archives the WHOLE
# data directory (the one holding onepage.db, which is also where the admin
# runtime overlays data/llm-runtime.json and data/quests-runtime.json live; they
# take precedence over env and must not be silently lost on restore), substituting
# the live DB file with the copy taken and excluding -wal/-shm and perflog/. uploads/
# is added to the archive as a separate entry (needed when
# OP_UPLOADS_DIR points not under the data directory but to a separate path).
#
# If --dest2 is given, puts a second copy of the archive on another path (another
# disk/share, in case the first one fails).
#
# Usage:
#   deploy/backup.sh --dest DIR [--dest2 DIR2] [--db PATH] [--uploads DIR] [--retain N]
#
# By default --db and --uploads come from OP_DB_PATH / OP_UPLOADS_DIR (the same
# variables the application reads), otherwise data/onepage.db and data/uploads.
# --retain: how many of the latest archives to keep in each destination directory
# (default 14, see docs/DEPLOY.md, RPO 24h).
#
# The archive and the destination directories holding it are created with mode 0700/0600:
# it contains the whole DB, including the sessions table (value = key of a live
# session) and, if present, llm-runtime.json (LLM keys). Nobody but the one who
# wrote the archive should be able to read it.
#
# Exit code 0 means success; anything non-zero means the backup was not created or is incomplete.
set -uo pipefail
umask 077

PROG=$(basename "$0")
DB_PATH="${OP_DB_PATH:-data/onepage.db}"
UPLOADS_DIR="${OP_UPLOADS_DIR:-}"
RETAIN=14
DEST=""
DEST2=""

usage() {
  cat <<EOF
Usage: $PROG --dest DIR [--dest2 DIR2] [--db PATH] [--uploads DIR] [--retain N]
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dest) DEST="${2:-}"; shift 2 ;;
    --dest2) DEST2="${2:-}"; shift 2 ;;
    --db) DB_PATH="${2:-}"; shift 2 ;;
    --uploads) UPLOADS_DIR="${2:-}"; shift 2 ;;
    --retain) RETAIN="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "$PROG: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"; }
fail() { log "ERROR: $1"; exit 1; }

[ -n "$DEST" ] || { echo "$PROG: --dest DIR is required" >&2; usage; exit 2; }
case "$RETAIN" in ''|*[!0-9]*) fail "--retain must be a number, got: $RETAIN";; esac

[ -f "$DB_PATH" ] || fail "DB not found: $DB_PATH"

# The data directory is the DB's parent. That is where app/services/runtime_config.py and
# app/services/quests_config.py keep llm-runtime.json/quests-runtime.json
# (settings.abs_db_path.parent), so we archive it as a whole, not just
# the DB file.
DATA_DIR=$(cd "$(dirname "$DB_PATH")" && pwd) || fail "DB directory not found: $(dirname "$DB_PATH")"
DB_BASENAME=$(basename "$DB_PATH")
[ -z "$UPLOADS_DIR" ] && UPLOADS_DIR="$DATA_DIR/uploads"

mkdir -p "$DEST" || fail "could not create destination directory: $DEST"
chmod 700 "$DEST" 2>/dev/null

TS=$(date -u '+%Y%m%d-%H%M%S')
WORK=$(mktemp -d "${TMPDIR:-/tmp}/op-backup-XXXXXX") || fail "mktemp failed"
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT

log "backup started: data=$DATA_DIR db=$DB_PATH uploads=$UPLOADS_DIR dest=$DEST retain=$RETAIN"

DB_COPY="$WORK/$DB_BASENAME"

# Online SQLite copy: the backup API/`​.backup` copies a consistent snapshot
# page by page through the same mechanism as a WAL checkpoint, without needing
# an exclusive lock and without reading the file while the WAL writer appends to it.
# The copy is small (the DB size, not uploads/lake), so it can safely
# sit in a temporary directory; uploads is NOT copied there (see below).
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB_PATH" ".backup '$DB_COPY'" || fail "sqlite3 .backup failed"
else
  python3 - "$DB_PATH" "$DB_COPY" <<'PYEOF' || fail "backup via sqlite3.backup() failed"
import sqlite3
import sys

src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
try:
    with dst:
        src.backup(dst)
finally:
    src.close()
    dst.close()
PYEOF
fi

[ -s "$DB_COPY" ] || fail "DB copy is empty or was not created"
log "DB copy taken: $(du -h "$DB_COPY" 2>/dev/null | cut -f1)"

if [ -d "$UPLOADS_DIR" ]; then
  UPLOADS_ABS=$(cd "$UPLOADS_DIR" && pwd)
else
  UPLOADS_ABS=""
  log "warning: uploads directory not found ($UPLOADS_DIR), archiving without it"
fi
# uploads may live either under the data directory (by default data/uploads)
# or separately (a custom OP_UPLOADS_DIR). Both cases are reduced to one without
# copying content to disk: we assemble a "flat" view of the data directory
# in $WORK/view out of SYMLINKS (O(1) space, not GBs of uploads/_lake), then
# archive it with `tar -h`, which dereferences symlinks into real
# files/directories: portable, without --transform (macOS bsdtar does not know it).
VIEW="$WORK/view"
mkdir -p "$VIEW"
ln -s "$DB_COPY" "$VIEW/$DB_BASENAME"
while IFS= read -r -d '' entry; do
  name=$(basename "$entry")
  case "$name" in
    "$DB_BASENAME"|"$DB_BASENAME-wal"|"$DB_BASENAME-shm"|perflog|uploads) continue ;;
  esac
  ln -s "$entry" "$VIEW/$name"
done < <(find "$DATA_DIR" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)
if [ -n "$UPLOADS_ABS" ]; then
  ln -s "$UPLOADS_ABS" "$VIEW/uploads"
fi

ARCHIVE_NAME="onepage-backup-$TS.tar.gz"
ARCHIVE_TMP="$DEST/.$ARCHIVE_NAME.tmp"
# The archive is written straight into --dest (not $TMPDIR): uploads/_lake on the production host
# can weigh ~1 GB (OP_LAKE_MAX_MB), and an extra intermediate copy risks
# filling the live host's root FS. A .tmp name + mv ensures an incomplete archive
# never ends up under the final name.
tar -h -czf "$ARCHIVE_TMP" -C "$VIEW" . || { rm -f "$ARCHIVE_TMP"; fail "tar failed"; }
chmod 600 "$ARCHIVE_TMP"
mv "$ARCHIVE_TMP" "$DEST/$ARCHIVE_NAME" || fail "could not move the archive to $DEST"
log "archive created: $DEST/$ARCHIVE_NAME ($(du -h "$DEST/$ARCHIVE_NAME" 2>/dev/null | cut -f1))"

if [ -n "$DEST2" ]; then
  mkdir -p "$DEST2" || fail "could not create the second destination: $DEST2"
  chmod 700 "$DEST2" 2>/dev/null
  DEST2_TMP="$DEST2/.$ARCHIVE_NAME.tmp"
  cp "$DEST/$ARCHIVE_NAME" "$DEST2_TMP" || fail "could not copy the second copy to $DEST2"
  chmod 600 "$DEST2_TMP"
  mv "$DEST2_TMP" "$DEST2/$ARCHIVE_NAME" || fail "could not finish the second copy in $DEST2"
  log "second copy: $DEST2/$ARCHIVE_NAME"
fi

# Retention: keep only the $RETAIN latest archives in each directory.
prune() {
  local dir="$1"
  local count
  count=$(find "$dir" -maxdepth 1 -name 'onepage-backup-*.tar.gz' -type f 2>/dev/null | wc -l | tr -d ' ')
  if [ "$count" -gt "$RETAIN" ]; then
    ls -1t "$dir"/onepage-backup-*.tar.gz | tail -n "+$((RETAIN + 1))" | while IFS= read -r old; do
      rm -f "$old" && log "retention: removed old archive $old"
    done
  fi
}
prune "$DEST"
[ -n "$DEST2" ] && prune "$DEST2"

log "backup completed successfully"
exit 0
