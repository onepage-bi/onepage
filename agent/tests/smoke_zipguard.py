"""Smoke test for agent/zipguard.py: zip-slip guard + zip-bomb (total budget
of extracted bytes). Standard library only.

Run:  python -m agent.tests.smoke_zipguard   (from the OnePage repo root)
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from agent.zipguard import BundleError, safe_extract

_PASS = 0
_FAIL = 0


def check(label, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"PASS {label}")
    else:
        _FAIL += 1
        print(f"FAIL {label}  {extra}")


def _zip_with(tmp: Path, name: str, entries: dict[str, bytes]) -> Path:
    zpath = tmp / name
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for member, data in entries.items():
            z.writestr(member, data)
    return zpath


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="op-zipguard-test-"))
    try:
        # 1. An honest bundle extracts as is.
        good = _zip_with(tmp, "good.zip", {
            "manifest.json": b'{"name": "t"}',
            "sql/01_glo.sql": b"select 1",
        })
        dest1 = tmp / "d1"
        safe_extract(good, dest1)
        check("honest bundle: files in place",
              (dest1 / "manifest.json").exists() and (dest1 / "sql/01_glo.sql").exists())

        # 2. zip-slip via '../' -> BundleError, nothing is written outside.
        slip = _zip_with(tmp, "slip.zip", {"../../evil.txt": b"pwned"})
        dest2 = tmp / "d2"
        try:
            safe_extract(slip, dest2)
            check("zip-slip '../' -> BundleError", False, "did not raise")
        except BundleError:
            check("zip-slip '../' -> BundleError", True)
        check("zip-slip: file did not escape the directory", not (tmp / "evil.txt").exists())

        # 3. zip-slip via '\\' (Windows bundle) -> BundleError.
        slip_win = _zip_with(tmp, "slip_win.zip", {"..\\..\\evil2.txt": b"pwned"})
        dest3 = tmp / "d3"
        try:
            safe_extract(slip_win, dest3)
            check("zip-slip '\\..' -> BundleError", False, "did not raise")
        except BundleError:
            check("zip-slip '\\..' -> BundleError", True)

        # 4. Absolute path -> BundleError.
        abs_zip = _zip_with(tmp, "abs.zip", {"/etc/passwd": b"pwned"})
        dest4 = tmp / "d4"
        try:
            safe_extract(abs_zip, dest4)
            check("absolute path -> BundleError", False, "did not raise")
        except BundleError:
            check("absolute path -> BundleError", True)

        # 5. zip-bomb: a small compressed file that expands past the limit.
        # ZIP_DEFLATED on uniform data compresses enormously - a few MB of
        # zeros shrink to a few KB, which is exactly the threat model.
        bomb_member = b"\x00" * (2 * 1024 * 1024)   # 2 MB per member
        bomb = _zip_with(tmp, "bomb.zip", {
            f"blob_{i}.bin": bomb_member for i in range(4)   # 8 MB total
        })
        dest5 = tmp / "d5"
        try:
            safe_extract(bomb, dest5, max_bytes=5 * 1024 * 1024)   # budget 5 MB < 8 MB
            check("zip-bomb: extraction budget -> BundleError", False, "did not raise")
        except BundleError as e:
            check("zip-bomb: extraction budget -> BundleError", True, str(e))

        # 6. Same bundle, but the budget exceeds the total - extracts fully.
        dest6 = tmp / "d6"
        safe_extract(bomb, dest6, max_bytes=32 * 1024 * 1024)
        check("under budget: whole bundle extracted",
              all((dest6 / f"blob_{i}.bin").stat().st_size == len(bomb_member)
                  for i in range(4)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if _FAIL == 0 else 'FAILURES: ' + str(_FAIL)}  ({_PASS} passed)")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
