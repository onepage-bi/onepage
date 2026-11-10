#!/usr/bin/env bash
# OnePage: restore from an archive made by deploy/backup.sh.
#
# Usage:
#   deploy/restore.sh --archive PATH --dest DIR
#
# Unpacks the archive into DEST (onepage.db + uploads/) and checks the integrity
# of the restored database with `PRAGMA integrity_check`. Touches nothing outside
# DEST: it does not overwrite the "live" DB itself, that is the operator's decision (usually:
# stop the application, swap data/, start again; see docs/DEPLOY.md).
#
# Exit code 0 means restored and integrity confirmed; anything non-zero
# means the restore failed or the database is corrupt.
set -uo pipefail
umask 077

PROG=$(basename "$0")
ARCHIVE=""
DEST=""

usage() {
  cat <<EOF
Usage: $PROG --archive PATH --dest DIR
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --archive) ARCHIVE="${2:-}"; shift 2 ;;
    --dest) DEST="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "$PROG: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"; }
fail() { log "ERROR: $1"; exit 1; }

[ -n "$ARCHIVE" ] || { echo "$PROG: --archive PATH is required" >&2; usage; exit 2; }
[ -n "$DEST" ] || { echo "$PROG: --dest DIR is required" >&2; usage; exit 2; }
[ -f "$ARCHIVE" ] || fail "archive not found: $ARCHIVE"

mkdir -p "$DEST" || fail "could not create destination directory: $DEST"
chmod 700 "$DEST" 2>/dev/null

log "restoring $ARCHIVE -> $DEST"
tar -xzf "$ARCHIVE" -C "$DEST" || fail "tar -x failed"

DB_PATH="$DEST/onepage.db"
[ -f "$DB_PATH" ] || fail "archive does not contain onepage.db: restore is incomplete"

if command -v sqlite3 >/dev/null 2>&1; then
  RESULT=$(sqlite3 "$DB_PATH" "PRAGMA integrity_check;" 2>&1) || fail "PRAGMA integrity_check did not run: $RESULT"
else
  RESULT=$(python3 - "$DB_PATH" <<'PYEOF'
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1])
try:
    row = con.execute("PRAGMA integrity_check").fetchone()
    print(row[0] if row else "no result")
finally:
    con.close()
PYEOF
) || fail "PRAGMA integrity_check did not run"
fi

[ "$RESULT" = "ok" ] || fail "PRAGMA integrity_check failed: $RESULT"
log "integrity_check: ok"

UPLOADS_COUNT=$(find "$DEST/uploads" -type f 2>/dev/null | wc -l | tr -d ' ')
log "uploads restored: $DEST/uploads ($UPLOADS_COUNT files)"
log "restore completed successfully: $DEST"
exit 0
