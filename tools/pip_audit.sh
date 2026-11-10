#!/usr/bin/env bash
# OnePage — pip-audit over requirements.txt.
#
#   ./tools/pip_audit.sh              # uses the default venv
#   ./tools/pip_audit.sh /path/py     # explicit python (e.g. .venv/bin/python)
#
# The server is offline (see AI data policy / prod-server-facts): pip-audit needs network
# access to the PyPI vulnerability database, so it only makes sense to run it where
# there is network (developer machine, CI) — not on prod. If the package is not installed
# in the chosen interpreter, the script does not fail and does not try to install it —
# it only prints how to do that manually.
#
# Changes nothing: only reads requirements.txt and prints a report.
set -uo pipefail

DEFAULT_PY=".venv/bin/python"
PY="${1:-$DEFAULT_PY}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REQ="$REPO_ROOT/requirements.txt"

if [ ! -f "$REQ" ]; then
  echo "[BAD] requirements.txt not found: $REQ" >&2
  exit 1
fi

if [ ! -x "$PY" ]; then
  echo "[~~] interpreter not found or not executable: $PY"
  echo "     specify it explicitly: ./tools/pip_audit.sh /path/to/venv/bin/python"
  exit 1
fi

if "$PY" -m pip_audit --version >/dev/null 2>&1; then
  echo "== pip-audit over $REQ (interpreter: $PY) =="
  "$PY" -m pip_audit -r "$REQ"
  exit $?
fi

cat <<EOF
[~~] pip-audit is not installed in $PY

This is a separate dev tool — it does NOT touch requirements.txt (the prod set).
Install it into a TEMPORARY environment and run it from there:

    $PY -m venv /tmp/op-pip-audit-venv
    /tmp/op-pip-audit-venv/bin/pip install --quiet pip-audit
    /tmp/op-pip-audit-venv/bin/pip-audit -r "$REQ"

(or, if the network is only reachable through the corporate proxy — see user-context —
add --index-url/HTTPS_PROXY as the site requires).

While pip-audit is unavailable, a minimal offline summary of versions/dates is given by:

    $PY tools/deps_report.py
EOF
exit 0
