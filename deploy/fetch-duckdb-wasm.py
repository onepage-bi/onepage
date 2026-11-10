#!/usr/bin/env python3
"""Populate static/duckdb-wasm/ with a self-hosted @duckdb/duckdb-wasm build.

Why this exists at all is the same reason `fetch-pyodide.py` exists: the
corporate deployment has NO CDN access at runtime. A worker `import` of
jsdelivr is blocked from the page origin (measured 2026-07-20), so every byte
the /explore v2 engine loads has to come from OUR origin.

What lands in static/duckdb-wasm/ (gitignored, ~30 MB):

  duckdb-browser.mjs               the ESM entry (AsyncDuckDB, selectBundle…)
  duckdb-browser-{mvp,eh,coi}.worker.js
  duckdb-browser-coi.pthread.worker.js
  duckdb-{mvp,eh,coi}.wasm
  duckdb-node.cjs                  Node entry — tools/bench_engine2.mjs and the
  duckdb-node-{mvp,eh}.worker.cjs  Node tests boot from these
  VERSION                          the pinned version, read by engine.js/docs

Three bundles, not one, on purpose:
  * mvp  — baseline wasm, works everywhere;
  * eh   — +exception handling, the one every current browser actually gets;
  * coi  — +threads +SharedArrayBuffer, usable ONLY on a cross-origin isolated
           page (COOP/COEP headers — see docs/ENGINE2.md). Vendored so the
           option exists the day nginx sets those headers; nothing selects it
           without isolation.

Stdlib only, runs with any python3. Needs outbound network to the npm registry
(build-time, same as `fetch-pyodide.py`).

    python3 deploy/fetch-duckdb-wasm.py [--version 1.32.0] [--force]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path

# Pinned. Bump deliberately: the engine's OPFS calls below depend on the API
# shape of this version (registerOPFSFileName + opfs:// exist since 1.29).
DUCKDB_WASM_VERSION = "1.32.0"

REGISTRY = "https://registry.npmjs.org/@duckdb/duckdb-wasm"

# Paths inside the npm tarball (all under `package/`). Everything else in the
# package — source maps, .d.ts, the blocking/CJS browser targets, docs images —
# is dead weight on a corporate connection and is left behind.
WANTED = [
    "dist/duckdb-browser.mjs",
    "dist/duckdb-browser-mvp.worker.js",
    "dist/duckdb-browser-eh.worker.js",
    "dist/duckdb-browser-coi.worker.js",
    "dist/duckdb-browser-coi.pthread.worker.js",
    "dist/duckdb-mvp.wasm",
    "dist/duckdb-eh.wasm",
    "dist/duckdb-coi.wasm",
    "dist/duckdb-node.cjs",
    "dist/duckdb-node-mvp.worker.cjs",
    "dist/duckdb-node-eh.worker.cjs",
]

DEST = Path(__file__).resolve().parent.parent / "static" / "duckdb-wasm"

# ── apache-arrow, and why duckdb-browser.mjs gets PATCHED ────────────────
# Measured in a real Chrome, 2026-08-22, the first time the v2 worker booted
# outside Node:
#
#   Engine error: Failed to resolve module specifier "apache-arrow".
#
# `dist/duckdb-browser.mjs` does `import * as arrow from "apache-arrow"` — a
# BARE specifier. Node resolves it through node_modules (which is why every
# tools/bench_engine2.mjs run and every tests/engine2 test passed while the
# browser could not load the file at all); a browser has no resolver, and
# import maps are document-scoped, so a module WORKER cannot be given one.
#
# So the arrow ESM tree is vendored next to it and the specifier is rewritten
# to a relative path. Two rewrites, both mechanical and both asserted below:
#   duckdb-browser.mjs   "apache-arrow" -> "../arrow-esm/apache-arrow/Arrow.dom.mjs"
#   apache-arrow/**.mjs  "flatbuffers"  -> the vendored flatbuffers ESM (40 files)
#   apache-arrow/**.mjs  "tslib"        -> the vendored tslib ESM       (17 files)
# `bin/` (the arrow2csv CLI, which imports node:fs and command-line-args) is
# left behind, and nothing Arrow.dom.mjs reaches imports `io/node/`, which is
# why no `node:` specifier survives into the browser.
ARROW_VERSION = "17.0.0"          # must match package.json's devDependency
ARROW_DEST = Path(__file__).resolve().parent.parent / "static" / "arrow-esm"
ARROW_ENTRY = "Arrow.dom.mjs"

# bare specifier -> (npm package, version, prefix inside the tarball, suffixes
#                    to keep, the module the specifier must resolve to)
ARROW_DEPS = {
    "flatbuffers": ("flatbuffers", "24.12.23", "mjs/", (".js",), "flatbuffers/flatbuffers.js"),
    "tslib": ("tslib", "2.8.1", "", ("tslib.es6.mjs",), "tslib/tslib.es6.mjs"),
}

# ── DuckDB extensions ────────────────────────────────────────────────────
# Measured 2026-08-22, and it is the single finding that would have broken the
# corporate deploy silently: `parquet` is NOT statically linked into
# duckdb-{mvp,eh}.wasm 1.32.0. `duckdb_extensions()` reports it NOT_INSTALLED
# at boot, and the first `COPY … (FORMAT PARQUET)` auto-installs it from
# extensions.duckdb.org — a CDN fetch, from a Worker, at query time. On a
# laptop behind the SSL-inspecting proxy that is exactly the failure the
# Pyodide self-host exists to avoid, and it would surface as "the partition
# cache does not work" rather than as a network error.
#
# So we mirror the extension repository layout under static/duckdb-ext/ and
# the engine points `custom_extension_repository` at it:
#     {repo}/v{DUCKDB_VERSION}/{platform}/{name}.duckdb_extension.wasm
# The bytes are copied verbatim, so DuckDB's own signature check still passes.
#
# DUCKDB_VERSION is the engine version INSIDE the wasm build, not the npm
# version — `SELECT version()` on @duckdb/duckdb-wasm 1.32.0 says v1.4.3.
DUCKDB_VERSION = "v1.4.3"
EXT_REPO = "https://extensions.duckdb.org"
EXT_PLATFORMS = ["wasm_mvp", "wasm_eh", "wasm_threads"]
# parquet: the partition cache. json: cheap, and every DuckDB SQL snippet a
# model writes reaches for it sooner or later.
EXT_NAMES = ["parquet", "json"]
EXT_DEST = Path(__file__).resolve().parent.parent / "static" / "duckdb-ext"


def fetch(url: str) -> bytes:
    # A real User-Agent is not cosmetic: extensions.duckdb.org answers 403 to
    # urllib's default one.
    req = urllib.request.Request(url, headers={
        "User-Agent": "onepage-deploy/1.0 (+deploy/fetch-duckdb-wasm.py)",
    })
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def integrity_ok(data: bytes, integrity: str) -> bool:
    """npm ships `sha512-<base64>` in the packument's dist block."""
    algo, _, b64 = integrity.partition("-")
    if algo not in ("sha512", "sha256", "sha1"):
        return False
    digest = hashlib.new(algo, data).digest()
    return base64.b64encode(digest).decode() == b64


def npm_tarball(pkg: str, version: str) -> bytes:
    """One npm package's tarball, integrity-checked before it is unpacked."""
    meta = json.loads(fetch(f"https://registry.npmjs.org/{pkg}/{version}").decode())
    dist = meta["dist"]
    tgz = fetch(dist["tarball"])
    integrity = dist.get("integrity") or ("sha1-" + dist["shasum"])
    if not integrity_ok(tgz, integrity):
        raise SystemExit(f"!! npm integrity mismatch for {pkg}@{version} — refusing to unpack")
    return tgz


def vendor_arrow(force: bool) -> int:
    """apache-arrow + flatbuffers as a browser-loadable ESM tree.

    Only `.mjs`/`.js` module files are kept — no maps, no `.d.ts`, no `bin/`.
    Every bare specifier is rewritten to a relative path, because that is the
    one thing a browser cannot do for itself.
    """
    total = 0
    stamp = ARROW_DEST / "VERSION"
    if stamp.exists() and stamp.read_text().strip() == ARROW_VERSION and not force:
        print(f"==> apache-arrow@{ARROW_VERSION} already vendored in {ARROW_DEST}")
        # A freshly re-downloaded duckdb-browser.mjs carries the bare specifier
        # again, so the patch is re-applied here even when the tree is current.
        patch_duckdb_entry()
        return 0
    # The version comes from the dependency table, not from a separate constant:
    # a constant by that name never existed, and the line failed with
    # NameError, but only on the COLD path when the assets are not there yet, i.e.
    # exactly for the person running `make assets` for the first time. On a machine where
    # they are already downloaded, the function returns one line above and never reaches
    # the failure. ruff found it on its very first run.
    flatbuffers_version = ARROW_DEPS["flatbuffers"][1]
    print(f"==> apache-arrow@{ARROW_VERSION} + flatbuffers@{flatbuffers_version} -> {ARROW_DEST}")

    for spec, (pkg, ver, prefix, suffixes, _entry) in ARROW_DEPS.items():
        root = ARROW_DEST / pkg
        under = f"package/{prefix}"
        with tarfile.open(fileobj=io.BytesIO(npm_tarball(pkg, ver)), mode="r:gz") as tf:
            for m in tf.getmembers():
                if not m.isfile() or not m.name.startswith(under):
                    continue
                rel = m.name[len(under):]
                if not any(rel.endswith(sfx) for sfx in suffixes):
                    continue
                out = root / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                data = tf.extractfile(m).read()
                out.write_bytes(data)
                total += len(data)
        print(f"    {pkg}@{ver} vendored for the bare '{spec}' import")

    arrow_root = ARROW_DEST / "apache-arrow"
    patched = 0
    with tarfile.open(fileobj=io.BytesIO(npm_tarball("apache-arrow", ARROW_VERSION)),
                      mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.endswith(".mjs"):
                continue
            rel = m.name[len("package/"):]
            if rel.startswith("bin/"):
                continue
            text = tf.extractfile(m).read().decode("utf-8")
            # How deep this file sits below apache-arrow/ decides how far up
            # the relative path has to climb to reach its sibling package.
            up = "../" * (rel.count("/") + 1)
            hit = False
            for spec, (_pkg, _v, _p, _s, entry) in ARROW_DEPS.items():
                for q in ("'", '"'):
                    if f"{q}{spec}{q}" in text:
                        text = text.replace(f"{q}{spec}{q}", f"{q}{up}{entry}{q}")
                        hit = True
            patched += 1 if hit else 0
            out = arrow_root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            total += len(text.encode("utf-8"))
    print(f"    {patched} arrow modules re-pointed at their vendored dependencies")

    entry = arrow_root / ARROW_ENTRY
    if not entry.exists():
        raise SystemExit(f"!! {ARROW_ENTRY} missing from the apache-arrow tarball")

    patch_duckdb_entry()
    stamp.write_text(ARROW_VERSION + "\n")
    return total


def patch_duckdb_entry() -> None:
    """The reason all of the above exists: duckdb's own ESM entry."""
    mjs = DEST / "duckdb-browser.mjs"
    if not mjs.exists():
        return
    text = mjs.read_text(encoding="utf-8")
    n = text.count('"apache-arrow"')
    if n:
        text = text.replace('"apache-arrow"', f'"../arrow-esm/apache-arrow/{ARROW_ENTRY}"')
        mjs.write_text(text, encoding="utf-8")
        print(f"    duckdb-browser.mjs: {n} bare 'apache-arrow' import(s) made relative")
    if '"apache-arrow"' in mjs.read_text(encoding="utf-8"):
        raise SystemExit("!! duckdb-browser.mjs still holds a bare specifier")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=DUCKDB_WASM_VERSION)
    ap.add_argument("--force", action="store_true",
                    help="re-download even if static/duckdb-wasm is complete")
    args = ap.parse_args()
    version = args.version

    total = 0
    stamp = DEST / "VERSION"
    complete = (stamp.exists() and stamp.read_text().strip() == version
                and all((DEST / Path(w).name).exists() for w in WANTED))
    if complete and not args.force:
        print(f"==> static/duckdb-wasm already at {version}")
    else:
        print(f"==> @duckdb/duckdb-wasm@{version} -> {DEST}")
        meta = json.loads(fetch(f"{REGISTRY}/{version}"))
        dist = meta["dist"]
        tgz = fetch(dist["tarball"])

        # Verify before unpacking, not after: a tarball we do not trust should
        # never touch the filesystem.
        integrity = dist.get("integrity") or ("sha1-" + dist["shasum"])
        if not integrity_ok(tgz, integrity):
            raise SystemExit("!! npm integrity mismatch — refusing to unpack")
        print(f"    tarball {len(tgz)//1024} KB, integrity ok")

        DEST.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tf:
            for rel in WANTED:
                member = tf.getmember(f"package/{rel}")
                data = tf.extractfile(member).read()
                out = DEST / Path(rel).name
                out.write_bytes(data)
                total += len(data)
                print(f"    {out.name}  ({len(data)//1024} KB)")

        stamp.write_text(version + "\n")

    total += vendor_arrow(args.force)

    print(f"==> DuckDB {DUCKDB_VERSION} extensions -> {EXT_DEST}")
    for platform in EXT_PLATFORMS:
        for name in EXT_NAMES:
            rel = f"{DUCKDB_VERSION}/{platform}/{name}.duckdb_extension.wasm"
            out = EXT_DEST / rel
            if out.exists() and not args.force:
                continue
            try:
                data = fetch(f"{EXT_REPO}/{rel}")
            except Exception as e:            # noqa: BLE001 — a missing
                # platform/extension pair is not fatal: the engine only needs
                # the one matching `PRAGMA platform` on the user's browser.
                print(f"    !! {rel}: {e}")
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            total += len(data)
            print(f"    {rel}  ({len(data)//1024} KB)")

    print(f"==> done: {total//1024//1024} MB in {DEST} (+ {EXT_DEST.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
