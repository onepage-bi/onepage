#!/usr/bin/env python3
"""Populate static/pyodide/ with a self-hosted Pyodide subset (RFC App. D.4).

Why self-host at all: the browser engine runs on corporate laptops behind an
SSL-inspecting proxy, and the OnePage page itself may sit behind a reverse
proxy that sets Content-Security-Policy — a worker importScripts() to
cdn.jsdelivr.net (or a micropip fetch to pypi.org) can be blocked by either.
Serving everything from OUR origin removes the whole failure class. Measured
trigger: the first live pilot run failed exactly there (2026-07-20).

Downloads (idempotent — existing files with a matching sha256 are skipped):
  * the Pyodide core runtime (pyodide.js / .asm.js / .asm.wasm / stdlib / lock)
  * the dist wheels for the dependency closure of PACKAGES (from the lock,
    sha256-verified against it)
  * pure-Python wheels from PyPI listed in PYPI_WHEELS (sha256-verified
    against the PyPI JSON API) into wheels/ — micropip installs them from our
    origin instead of pypi.org (gen-worker.js tries wheels/ first).

Stdlib only — runs with any python3. Needs outbound network (the same machine
that runs make-bundle.sh already needs it for docker build).
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

PYODIDE_VERSION = "v314.0.2"          # pinned everywhere: config.py, vendored lock
CDN = f"https://cdn.jsdelivr.net/pyodide/{PYODIDE_VERSION}/full/"

# Dependency roots: micropip (installer) + slreport's dist-bundled deps + the
# /explore query engine. `duckdb` is NOT loaded by a normal generator run — the
# worker only calls loadPackage('duckdb') in mode 'explore' (Leg 3), so the
# extra megabytes cost boot time on /explore alone and ride the HTTP cache from
# the second visit on. It is mirrored here for the same reason as everything
# else: the corporate proxy may block cdn.jsdelivr.net outright.
PACKAGES = ["micropip", "pandas", "python-calamine", "xlrd", "duckdb"]

# Pure wheels the pilot needs that are NOT in the Pyodide dist (App. A).
PYPI_WHEELS = [("openpyxl", "3.1.5"), ("et_xmlfile", "2.0.0")]

# The 314 runtime is ESM-first: pyodide.mjs imports pyodide.asm.mjs which
# instantiates pyodide.asm.wasm. pyodide.js is a page-context shim only —
# workers MUST be module workers (classic ones are refused by the runtime).
# Subset completeness is gated by a Node boot test (same dist runs in Node).
CORE_FILES = [
    "pyodide.js", "pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm",
    "python_stdlib.zip", "pyodide-lock.json",
]

DEST = Path(__file__).resolve().parent.parent / "static" / "pyodide"


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def put(path: Path, data: bytes, sha256: str | None) -> str:
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise SystemExit(f"!! sha256 mismatch for {path.name} — refusing to write")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return "ok"


def have(path: Path, sha256: str | None) -> bool:
    if not path.exists():
        return False
    if sha256 is None:
        return True
    return hashlib.sha256(path.read_bytes()).hexdigest() == sha256


def closure(lock: dict, roots: list[str]) -> list[dict]:
    pkgs = lock["packages"]
    seen: dict[str, dict] = {}

    def walk(name: str) -> None:
        key = name.lower().replace("_", "-")
        entry = pkgs.get(key) or pkgs.get(name)
        if entry is None:
            raise SystemExit(f"!! {name} is not in the Pyodide lock")
        if key in seen:
            return
        seen[key] = entry
        for dep in entry.get("depends", []):
            walk(dep)

    for r in roots:
        walk(r)
    return [seen[k] for k in sorted(seen)]


def main() -> None:
    print(f"==> Pyodide subset -> {DEST}")
    lock_raw = fetch(CDN + "pyodide-lock.json")
    lock = json.loads(lock_raw)
    # The 314 lock's info block carries no release version — pin via the ABI,
    # which is what actually has to match the wheels we serve.
    abi = lock["info"].get("abi_version")
    if abi != "2026_0":
        raise SystemExit(f"!! lock ABI {abi!r} != expected 2026_0 for {PYODIDE_VERSION}")

    total = skipped = 0
    for name in CORE_FILES:
        dest = DEST / name
        if name == "pyodide-lock.json":
            put(dest, lock_raw, None)
            continue
        if have(dest, None):
            skipped += 1
            continue
        data = fetch(CDN + name)
        put(dest, data, None)
        total += len(data)
        print(f"   core   {name}  ({len(data)//1024} KB)")

    for entry in closure(lock, PACKAGES):
        fn, sha = entry["file_name"], entry["sha256"]
        dest = DEST / fn
        if have(dest, sha):
            skipped += 1
            continue
        data = fetch(CDN + fn)
        put(dest, data, sha)
        total += len(data)
        print(f"   wheel  {fn}  ({len(data)//1024} KB)")

    for name, version in PYPI_WHEELS:
        meta = json.loads(fetch(f"https://pypi.org/pypi/{name}/{version}/json"))
        url = next(u for u in meta["urls"] if u["filename"].endswith("-none-any.whl"))
        dest = DEST / "wheels" / url["filename"]
        if have(dest, url["digests"]["sha256"]):
            skipped += 1
            continue
        data = fetch(url["url"])
        put(dest, data, url["digests"]["sha256"])
        total += len(data)
        print(f"   pypi   wheels/{url['filename']}  ({len(data)//1024} KB)")

    print(f"==> done: {total//1024//1024} MB downloaded, {skipped} files already present")


if __name__ == "__main__":
    sys.exit(main())
