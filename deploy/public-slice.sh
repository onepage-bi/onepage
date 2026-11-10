#!/usr/bin/env bash
# Build the public slice of this repository — repeatably.
#
#   deploy/public-slice.sh --out <dir> [--commit <message>] [--dry-run]
#   (a commit message must start with "onepage" or "sync" — that is how
#    the next run tells a sync from a contributor's commit)
#
# The slice is `git archive HEAD` minus deploy/public-exclude.txt, run through
# tools/public_sanitize.py, and then gated by the same tool in --check mode.
# Nothing is written to <dir> unless the gate passes.
#
# WHY A SCRIPT AND NOT A ONE-TIME CLEANUP
# ---------------------------------------
# The internal repository keeps living after publication. A slice made by hand
# is correct exactly once; two releases later nobody remembers what was removed
# or why, and the public tree either rots or leaks. This script is also the
# mechanism for every later sync, which is why it is written to be re-run.
#
# WHY `git archive` AND NOT A COPY
# --------------------------------
# dist/ alone is 7.6 GB, and data/ holds a live database. Neither is tracked,
# so `git archive` cannot see them — the slice gets the 719 tracked files and
# nothing else. Copying the working tree would mean remembering to exclude
# them, which is precisely the kind of memory this script exists to replace.
#
# Limits, by design:
#   * refuses to run on a dirty tree — the slice is keyed to a commit;
#   * refuses if an exclude pattern matched nothing (a stale list is a leak);
#   * refuses if the gate finds anything, and writes nothing in that case;
#   * refuses if <dir> has commits the internal repository does not have
#     (someone merged a pull request there — back-port it first).
# There is no --force. Every one of these would be silent damage.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

OUT=""
COMMIT_MSG=""
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --out)     OUT="${2:?--out needs a directory}"; shift 2 ;;
    --commit)  COMMIT_MSG="${2:?--commit needs a message}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$OUT" ] || { echo "usage: deploy/public-slice.sh --out <dir> [--commit <msg>] [--dry-run]" >&2; exit 2; }

if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty — commit first (the slice is keyed by commit)" >&2
  exit 1
fi
SHA="$(git rev-parse --short HEAD)"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/onepage-slice.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

echo "── slicing $SHA into a staging tree"
git archive HEAD | tar -x -C "$STAGE"
BEFORE=$(find "$STAGE" -type f | wc -l | tr -d ' ')

echo "── removing excluded paths"
REMOVED=0
while IFS= read -r raw; do
  line="${raw%%#*}"
  line="$(echo "$line" | xargs || true)"     # trim
  [ -n "$line" ] || continue
  # shellcheck disable=SC2086
  matches=$(cd "$STAGE" && eval ls -d $line 2>/dev/null | wc -l | tr -d ' ')
  if [ "$matches" = "0" ]; then
    echo "   exclude pattern matched nothing: $line" >&2
    echo "   (a stale list is how a file creeps back in — fix deploy/public-exclude.txt)" >&2
    exit 1
  fi
  (cd "$STAGE" && eval rm -rf $line)
  REMOVED=$((REMOVED + matches))
  echo "   removed $line"
done < deploy/public-exclude.txt

echo "── scrubbing internal values"
python3 tools/public_sanitize.py --apply "$STAGE" --allow "$ROOT/tools/public_allow.txt" | tail -1

echo "── gate"
if ! python3 tools/public_sanitize.py --check "$STAGE" --allow "$ROOT/tools/public_allow.txt"; then
  echo >&2
  echo "gate failed — nothing was written to $OUT" >&2
  echo "every finding above is either a value to rewrite by hand or a file to" >&2
  echo "add to deploy/public-exclude.txt. Do not widen tools/public_allow.txt" >&2
  echo "to make this pass." >&2
  exit 1
fi

AFTER=$(find "$STAGE" -type f | wc -l | tr -d ' ')
echo "── slice: $BEFORE tracked files → $AFTER published ($REMOVED path(s) removed)"

if [ "$DRY_RUN" = "1" ]; then
  echo "dry run — staging tree left at: $STAGE"
  trap - EXIT
  exit 0
fi

mkdir -p "$OUT"
if [ -d "$OUT/.git" ]; then
  # Refuse to clobber work that exists only in the public repository: a merged
  # pull request has to come back through tools/public_backport.sh first, or
  # this sync would silently delete a contributor's commit.
  #
  # A sync is recognised by its subject: it starts with "onepage" or "sync"
  # (case-insensitive) — the initial release commit and every later sync are
  # written that way. Anything else is a contributor's commit until a person
  # says otherwise. The first version of this check was case-sensitive and
  # refused the repository's own initial commit.
  UNKNOWN="$(git -C "$OUT" log --format=%s -n 50 2>/dev/null \
             | grep -civE '^(onepage|sync)([ :—-]|$)' || true)"
  if [ "${UNKNOWN:-0}" != "0" ] && [ -z "${ALLOW_FOREIGN_COMMITS:-}" ]; then
    echo "   note: $OUT has commits whose subject does not look like a sync." >&2
    echo "   If a pull request was merged there, back-port it before syncing." >&2
    echo "   Set ALLOW_FOREIGN_COMMITS=1 once you have checked." >&2
    exit 1
  fi
else
  # A fresh target: the public repository starts here, with no history behind
  # it. That is the point — the internal history cannot be published.
  git -C "$OUT" init -q -b main
  echo "── initialised a new repository in $OUT"
fi

rsync -a --delete --exclude '.git' "$STAGE"/ "$OUT"/
echo "── wrote $OUT"

if [ -n "$COMMIT_MSG" ]; then
  # `-f` is not optional. After `rsync --delete` the target equals the staged
  # tree exactly, and every staged file was tracked internally — but the
  # public clone's .gitignore has a `data/` rule that matches at ANY depth, so
  # a plain `add -A` silently skips app/services/data/ (the Pyodide lock, the
  # quiz banks). The first public commit shipped without them for that reason.
  git -C "$OUT" add -A -f
  git -C "$OUT" --no-pager diff --stat --cached | tail -5
  git -C "$OUT" commit -q -m "$COMMIT_MSG" || echo "   nothing to commit"
  echo "── committed in $OUT"
fi
