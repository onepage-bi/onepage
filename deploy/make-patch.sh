#!/usr/bin/env bash
# Build a PATCH instead of a full bundle: only the files that changed since the
# commit running on the server, plus an apply script that copies them into the
# running container and restarts it. Kilobytes instead of 127 MB — for the
# iterate-on-prod loop where every round trip costs the operator real time.
#
#   deploy/make-patch.sh <deployed-git-sha>        # -> dist/onepage-patch-<from>-<to>.tar.gz
#
# On the server, from the directory the archive was copied to:
#   tar -xzf onepage-patch-*.tar.gz && sudo bash onepage-patch-*/apply.sh
#
# Limits (by design — use the full bundle when any of these is false):
#   * only app/, static/, templates/, tools/ are patchable (no requirements,
#     Dockerfile, compose or migrations);
#   * the container must already run an image whose code matches <from>.
set -euo pipefail
cd "$(dirname "$0")/.."

# Catalogues first: a patch that ships templates whose keys are missing from
# app/locales, or a static/locales bundle that no longer matches the JSON it is
# generated from, would put an untranslated (or half-translated) UI on the
# server with no error anywhere. tools/i18n_check.py --stats also prints how
# much of en/zh-HK is actually translated, which belongs in the release note.
python3 tools/i18n_check.py --stats

FROM="${1:?usage: deploy/make-patch.sh <deployed-git-sha>}"
TO="$(git rev-parse HEAD)"
FROM_SHORT="$(git rev-parse --short "$FROM")"
TO_SHORT="$(git rev-parse --short "$TO")"
if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty — commit first (the patch is keyed by commit)" >&2
  exit 1
fi

FILES=()
while IFS= read -r line; do
  [ -n "$line" ] && FILES+=("$line")
done < <(git diff --name-only --diff-filter=ACMR "$FROM" "$TO" -- app static templates tools)
if [ "${#FILES[@]}" -eq 0 ]; then
  echo "nothing patchable changed between $FROM_SHORT and $TO_SHORT" >&2
  exit 1
fi
BLOCKED="$(git diff --name-only "$FROM" "$TO" -- requirements.txt Dockerfile || true)"
if [ -n "$BLOCKED" ]; then
  echo "these changes need a full bundle, not a patch:" >&2
  echo "$BLOCKED" >&2
  exit 1
fi
# Compose/env TEMPLATE changes only matter for fresh installs; the running
# server keeps its own docker-compose.yml. Mention them so the operator adds
# the new lines by hand.
TEMPLATES="$(git diff --name-only "$FROM" "$TO" -- deploy/bundle || true)"
if [ -n "$TEMPLATES" ]; then
  echo "note: deploy templates changed (add any new env lines to the server's compose by hand):" >&2
  echo "$TEMPLATES" >&2
fi

NAME="onepage-patch-${FROM_SHORT}-${TO_SHORT}"
OUT="dist/$NAME"
rm -rf "$OUT" && mkdir -p "$OUT/files"
for f in "${FILES[@]}"; do
  mkdir -p "$OUT/files/$(dirname "$f")"
  cp "$f" "$OUT/files/$f"
done

# Build stamp: not tracked by git (see .gitignore), so it never shows up in
# the diff above — write it explicitly so /health and /admin on the patched
# container report the sha the patch actually brought in, not the stale one
# baked into the image it is being applied on top of.
mkdir -p "$OUT/files/app"
echo "$TO_SHORT" > "$OUT/files/app/BUILD"
FILES+=("app/BUILD")

printf '%s\n' "${FILES[@]}" > "$OUT/MANIFEST"
git log --oneline "$FROM".."$TO" > "$OUT/CHANGES"

cat > "$OUT/apply.sh" <<'EOF'
#!/usr/bin/env bash
# Copies every patched file into the running container and restarts it.
# Templates and Python need the restart; static files would not, but one
# restart is simpler than remembering which is which.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
C="${ONEPAGE_CONTAINER:-onepage-onepage-1}"
# The destination directory may not exist in the image yet: a new directory
# appears exactly in the patch that introduces it. In that case `docker cp`
# does NOT create the path but treats the last element as a file name, and
# creates a file named after the directory. The next file of the same directory fails with
# "Could not find the file", the script aborts halfway and never reaches
# the restart (production, 05.10: this happened with `static/video`). So every
# directory is created up front; `mkdir -p` on an existing one does nothing.
while IFS= read -r f; do
  [ -z "$f" ] && continue
  d="$(dirname "/app/$f")"
  [ "$d" = "/app" ] || docker exec "$C" sh -c "mkdir -p '$d'"
  docker cp "$HERE/files/$f" "$C:/app/$f"
  echo "patched /app/$f"
done < "$HERE/MANIFEST"
docker restart "$C" >/dev/null
echo "restarted $C — reload the page with Ctrl+F5 (static files are cached for 5 min)"
EOF
chmod +x "$OUT/apply.sh"
(cd dist && tar -czf "$NAME.tar.gz" "$NAME")
echo "patch: dist/$NAME.tar.gz ($(du -h "dist/$NAME.tar.gz" | cut -f1)), ${#FILES[@]} files:"
printf '  %s\n' "${FILES[@]}"
