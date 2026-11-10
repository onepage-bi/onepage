#!/usr/bin/env bash
# Build the /explore v2 Excel parser to wasm and drop it into static/.
#
#   engine/xlsx-arrow/build.sh
#
# Prerequisites (one-time, no CDN involved at RUNTIME — this is build-time
# only, same as `deploy/fetch-pyodide.py`):
#
#   rustup target add wasm32-unknown-unknown
#   cargo install wasm-bindgen-cli --locked      # must match the wasm-bindgen
#                                                # crate version in Cargo.lock
#
# Output (all gitignored, like static/pyodide/):
#   static/xlsx-arrow/xlsx_arrow.js       ES-module glue, works in a Worker
#   static/xlsx-arrow/xlsx_arrow_bg.wasm  the parser
#   static/xlsx-arrow/xlsx_arrow.d.ts
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$ROOT/static/xlsx-arrow"

CARGO="${CARGO:-cargo}"
WASM_BINDGEN="${WASM_BINDGEN:-wasm-bindgen}"
command -v "$CARGO" >/dev/null || { echo "cargo not found (set \$CARGO)"; exit 1; }
command -v "$WASM_BINDGEN" >/dev/null || {
  echo "wasm-bindgen not found — cargo install wasm-bindgen-cli --locked (set \$WASM_BINDGEN)"; exit 1; }

echo "==> cargo build --release --target wasm32-unknown-unknown"
(cd "$HERE" && "$CARGO" build --release --target wasm32-unknown-unknown)

WASM="$HERE/target/wasm32-unknown-unknown/release/xlsx_arrow.wasm"
mkdir -p "$OUT"

# --target web: one ES module with a default `init(urlOrBytes)` export. It is
# the only bundler-free target that works BOTH inside a Web Worker (the browser
# path) and under `node --experimental-*`-free plain ESM (the bench/test path),
# which is what deliverable 3 needs.
echo "==> wasm-bindgen --target web --out-dir $OUT"
"$WASM_BINDGEN" --target web --out-name xlsx_arrow --out-dir "$OUT" "$WASM"

# wasm-opt is optional: it is a nice-to-have (~15 % smaller), never a
# correctness requirement, and it is not installed on the build host by default.
if command -v wasm-opt >/dev/null; then
  echo "==> wasm-opt -Oz"
  wasm-opt -Oz "$OUT/xlsx_arrow_bg.wasm" -o "$OUT/xlsx_arrow_bg.wasm.opt" \
    && mv "$OUT/xlsx_arrow_bg.wasm.opt" "$OUT/xlsx_arrow_bg.wasm"
else
  echo "==> wasm-opt not found, skipping (optional)"
fi

ls -la "$OUT"
