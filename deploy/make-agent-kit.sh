#!/usr/bin/env bash
# Build a portable op-agent kit for an analyst's machine.
#
# The result is ONE zip that works on the target machine WITHOUT A NETWORK: python and
# the GreenPlum driver are inside. This is not a convenience but the removal of two risks at once,
# the corporate proxy and TLS inspection: nothing is downloaded on the analyst's
# machine, so nothing can break there.
#
# Run here (on the development machine, which has internet). Windows is not needed:
# python comes as a ready relocatable build from nuget.org, the wheels are binary
# win_amd64, and PyInstaller is not used at all.
#
# Usage:  deploy/make-agent-kit.sh
set -euo pipefail

# CPython 3.14, not 3.12: in the browser the engine runs on CPython 3.14
# (Pyodide 314.0.2), and different Python minor versions are a whole axis along which
# a native run can diverge from the browser in numbers. win_amd64 cp314 wheels
# exist for all five kit packages, so the axis is removed for free.
PYVER="3.14.8"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KIT="$REPO/dist/op-agent-kit"
SRC="$REPO/deploy/agent_tools"
AGENT="$REPO/docs/agent_m2_kit/op_agent.py"

PY="$(command -v python3)"
$PY -m pip --version >/dev/null 2>&1 || { echo "!! python3 with pip is required" >&2; exit 1; }

# The wheel set is ONE list for download, verification and installation on the
# analyst's side: if two diverge, the kit ships with a wheel nobody expected
# (or without one that is expected). Versions of the analytics packages come from the
# Pyodide lock, NOT from the generator manifest; why exactly is written in the
# header of lock_specs.py. In short: for distribution packages the browser ignores the manifest pin,
# so matching the manifest would GUARANTEE that the engines diverge.
# Read line by line rather than with `mapfile`: on macOS /bin/bash is 3.2, which lacks it.
WHEELS=()
while IFS= read -r line; do
  [ -n "$line" ] && WHEELS+=("$line")
done < <($PY "$REPO/deploy/agent_tools/lock_specs.py")
[ "${#WHEELS[@]}" -ge 4 ] || { echo "!! lock_specs.py did not produce a wheel list" >&2; exit 1; }
echo "==> Wheel set (from the Pyodide lock + agent infrastructure)"
printf '    %s\n' "${WHEELS[@]}"

echo "==> Rebuilding the single-file agent (build stamp = current commit)"
$PY "$REPO/deploy/build_agent_single.py"

echo "==> Kit directory: $KIT"
rm -rf "$KIT"
mkdir -p "$KIT/wheels"

echo "==> Python $PYVER (nuget.org, full relocatable build)"
curl -sSL --fail --max-time 600 \
     -o "$KIT/python-$PYVER.nupkg.zip" \
     "https://www.nuget.org/api/v2/package/python/$PYVER"

echo "==> Wheels for win_amd64 / python $PYVER"
# psycopg[binary] pulls in windows-only dependencies that pip will NOT
# fetch by itself: it evaluates environment markers against the CURRENT system, not against
# --platform, so `tzdata; sys_platform == "win32"` silently drops out on macOS/Linux.
# On the analyst's machine this would surface as ModuleNotFoundError
# in the middle of a run, so both dependencies are requested explicitly.
$PY -m pip download --quiet --only-binary=:all: \
    --platform win_amd64 --python-version "${PYVER%.*}" \
    "${WHEELS[@]}" \
    -d "$KIT/wheels"

echo "==> Checking that the wheels suffice for an offline install"
# pip is useless as a check here: it evaluates environment markers against the CURRENT
# system. We evaluate them for the target (win32 / cp314) explicitly; otherwise a missing
# windows-only dependency would only surface on the analyst's machine.
$PY "$SRC/check_wheels.py" "$KIT/wheels" --python "$PYVER" "${WHEELS[@]}"

echo "==> Install list for the analyst (wheels/requirements.txt)"
# The third consumer of the same list. setup.bat cannot read the Pyodide lock, and
# listing packages in the .bat by hand would create a fourth copy of the set
# that would one day diverge from the first three. So the list goes into the kit
# as a file, and the .bat installs strictly from it. CRLF is not needed here: the file is read by pip,
# not Notepad, and --check-kit does not count it as a kit .txt.
printf '%s\n' "${WHEELS[@]}" > "$KIT/wheels/requirements.txt"

echo "==> Kit files"
cp "$AGENT" "$KIT/op_agent.py"
# cmd.exe reads a .bat line by line; a lone LF breaks multi-line
# constructs. Convert to CRLF in place. ONLY .bat and .txt: the mirror resources
# (.html/.js/.css/.json) must not be converted to CRLF, or line breaks inside
# JS strings and the length/hash check of the pages would shift.
for f in setup.bat op-agent.bat README.txt README-MIRROR.txt folders.ini.example; do
  perl -pe 's/\r?\n/\r\n/' "$SRC/$f" > "$KIT/$f"
done

echo "==> Program icon (onepage.ico)"
# The icon is drawn by tools/make_app_icon.py (pure stdlib); the file lives in the repo next
# to the .bat. The --check run BREAKS the build if the file is stale or missing: a shortcut
# without an icon does not fail, it silently looks like a nameless cmd, and that is discovered
# on the person's machine. Byte-for-byte copy (not perl): CRLF would corrupt a binary.
$PY "$REPO/tools/make_app_icon.py" --check
cp "$SRC/onepage.ico" "$KIT/onepage.ico"
[ -s "$KIT/onepage.ico" ] || { echo "!! onepage.ico did not make it into the kit" >&2; exit 1; }

echo "==> Files next to op_agent.py (mirror/strings.json, mirror/web/*, r2host.py, _lineage.py)"
# Gluing the modules carries only CODE: texts and pages live as files and
# must sit NEXT TO op_agent.py, in the mirror/ subdirectory, exactly where
# assets.res_path looks for them. r2host.py travels on the same list: the R2 contract host
# is STARTED as a separate process, not imported, so by construction it does not
# get into the glued file, and without it the very first generator run over local
# folders fails with gen_no_host. And next to them is _lineage.py, the lineage probe:
# the ORIGINAL static/py/_lineage.py is taken (the very file the browser engine
# loads); it deliberately has no copy of its own in the repository, and
# --check-kit below compares the file in the kit with the original byte for byte.
# The builder copies everything (byte for byte, one list with a name check), not cp
# here: otherwise the list would live in two places.
$PY "$REPO/deploy/build_agent_single.py" --install-resources "$KIT"

# BUILD-INFO.txt is read in Notepad, like the other kit .txt files, so CRLF
# here too. The block used to write straight into the file, bypassing the perl loop above, and it was
# the only .txt without CRLF; now the output goes through the same perl, and --check-kit
# below counts bare LFs in all .bat/.txt files and breaks the build if there are any.
{
  echo "op-agent kit"
  echo "built:   $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  echo "git:     $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '-')"
  echo "python:  $PYVER (win_amd64)"
  echo "order:   $(printf '%s ' "${WHEELS[@]}")"
  echo "wheels:  $(cd "$KIT/wheels" && ls *.whl | tr '\n' ' ')"
  echo "size:    unpacked $(du -sh "$KIT" | awk "{print \$1}"), of which wheels $(du -sh "$KIT/wheels" | awk "{print \$1}")"
  echo "beside:  $($PY "$REPO/deploy/build_agent_single.py" --list-resources | tr '\n' ' ')"
} | perl -pe 's/\r?\n/\r\n/' > "$KIT/BUILD-INFO.txt"

echo "==> Checking the kit contents (it must break here, not on the person's machine)"
# A kit without the mirror used to build silently: the code is not listed in MODULES, yet the file still
# compiles, the agent still starts, and "the library does not open"
# is discovered on the analyst's machine. Same with r2host.py: without it the kit builds and
# starts, and only the folder run fails. _lineage.py is the quietest: the run
# passes, the report is published, and only the provenance stays empty, which makes it
# all the more important that the check here is BYTE-FOR-BYTE against static/py/_lineage.py, not
# "the file is there". We check both the code and the files next to it.
$PY "$REPO/deploy/build_agent_single.py" --check-kit "$KIT"

echo "==> Packing"
ZIP="$REPO/dist/op-agent-kit.zip"
rm -f "$ZIP"
( cd "$REPO/dist" && zip -qr "$ZIP" op-agent-kit )

echo
echo "==> Done: $ZIP"
echo "    zip:           $(du -h "$ZIP" | awk "{print \$1}")"
echo "    unpacked:      $(du -sh "$KIT" | awk "{print \$1}")"
echo "    wheels:        $(du -sh "$KIT/wheels" | awk "{print \$1}")  ($(cd "$KIT/wheels" && ls -1 *.whl | wc -l | tr -d ' ') files)"
echo "    python:        $(du -h "$KIT/python-$PYVER.nupkg.zip" | awk "{print \$1}")"
echo
tr -d '\r' < "$KIT/BUILD-INFO.txt"
