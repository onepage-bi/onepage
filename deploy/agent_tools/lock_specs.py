"""Wheel set of the offline kit: ONE list, with versions taken from the Pyodide lock.

    lock_specs.py                 # all specs, one per line
    lock_specs.py --only locked   # only what the lock pins
    lock_specs.py --json          # the same as a JSON list (for machines)

WHY THE LOCK AND NOT THE GENERATOR MANIFEST. Versions of the analytics packages come from
`app/services/data/pyodide_lock_314.json`, the same file
`genmanifest.resolve_deps()` uses to decide whether a dependency goes to the browser. For
distribution packages the browser IGNORES the manifest pin entirely: micropip installs the
version that ships in the Pyodide distribution (hence the `wasm32_note` "pyodide
dist ships X"). So if we built the kit from the manifest, the native run would compute
on a DIFFERENT pandas version than the browser, and a divergence in numbers between the two engines
would be built in. Hence there is exactly one source of versions, and it is the lock.

A consequence that cannot be worked around by "fitting": a package that is not in the lock is not
shipped by the kit. Such a manifest must get a `gen_missing_dep` refusal at the start of
the run (`agent/r2host.py::_check_deps`), with the package name and BEFORE the
run takes the cache lock.

The NON-LOCK part of the list (`EXTRA`) is not analytics but agent infrastructure:
the GreenPlum driver and its windows-only companions. They are not in Pyodide and cannot
be (libpq over TCP is unreachable from wasm), so they are pinned here rather than
by the lock.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOCK = REPO / "app" / "services" / "data" / "pyodide_lock_314.json"

# Analytics: NAMES here, VERSIONS from the lock. This is the minimum without which
# a native folder run is impossible: pandas reads and computes, numpy under it,
# python-calamine opens workbooks (the same parser as in the browser).
LOCKED = ("pandas", "numpy", "python-calamine")

# Agent infrastructure that Pyodide lacks. `psycopg[binary]` pulls in
# windows-only dependencies that pip will NOT fetch by itself (it evaluates markers against
# the CURRENT system, not against --platform), so they are named explicitly.
EXTRA = ("psycopg[binary]", "tzdata", "typing-extensions>=4.6")


def _pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_specs(lock_path: Path = LOCK) -> list[str]:
    """`["pandas==3.0.2", ...]`. A package missing from the lock is a refusal, not a skip.

    Silently dropping a package means building a kit that looks complete and fails
    only on the analyst's machine, in the middle of a run.
    """
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"cannot read the Pyodide lock {lock_path}: {exc}") from exc
    packages = data.get("packages") or {}
    index = {_pep503(k): v for k, v in packages.items()}
    out = []
    for name in LOCKED:
        version = index.get(_pep503(name))
        if not version:
            raise SystemExit(
                f"package {name!r} is not in the Pyodide lock ({lock_path.name}): "
                "the kit must not be built: the native run would diverge from the browser.")
        out.append(f"{name}=={version}")
    return out


def all_specs(lock_path: Path = LOCK) -> list[str]:
    return locked_specs(lock_path) + list(EXTRA)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lock_specs", description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=("all", "locked", "extra"), default="all")
    ap.add_argument("--json", action="store_true", help="print as a JSON list")
    ap.add_argument("--lock", default=str(LOCK), help="path to the Pyodide lock")
    args = ap.parse_args(argv)

    lock = Path(args.lock)
    specs = {"all": all_specs, "locked": locked_specs,
             "extra": lambda _p: list(EXTRA)}[args.only](lock)
    if args.json:
        print(json.dumps(specs, ensure_ascii=False))
    else:
        for s in specs:
            print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
