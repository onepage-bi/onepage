"""Safe extraction of a generator bundle (zip-slip guard).

Unlike the browser engine, which unpacks the bundle into a Pyodide sandbox
(writes outside the sandbox are impossible), the agent writes to the REAL
file system - so the guard against zip-slip (`../` and absolute paths in
archive member names) is mandatory and explicit. Both separators (`/` and
`\\`) are checked: the bundle may have been built on Windows.

The second guard is a total budget of EXTRACTED bytes (zip-bomb): the
compressed body is already capped by the HTTP layer (`_MAX_RUN_BODY` in
server.py), but a few KB of zip can expand to gigabytes on disk. The budget
counts the bytes actually written from the stream, not the declared
`ZipInfo.file_size` - an archive member header is not to be trusted.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

# A sane ceiling for a report bundle (generator code + data). If it is
# exceeded the bundle is treated as hostile, not as "just big".
MAX_EXTRACT_BYTES = 512 * 1024 * 1024


class BundleError(Exception):
    """Corrupt or unsafe bundle."""


def safe_extract(zip_path: Path, dest_dir: Path, max_bytes: int = MAX_EXTRACT_BYTES) -> None:
    """Extract a zip into dest_dir, rejecting any member that escapes it.

    Rejected: an absolute path, a path with a '..' component, a symlink, any
    resolve that leaves dest_dir - and also a bundle whose total extracted
    size exceeds `max_bytes` (zip-bomb). Directories are created as needed.
    """
    dest_dir = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            name = member.filename
            # Normalise the Windows separator and cut the obviously dangerous.
            norm = name.replace("\\", "/")
            if norm.startswith("/") or ".." in norm.split("/"):
                raise BundleError(f"unsafe name in bundle: {name!r}")
            target = (dest_dir / norm).resolve()
            # Final check: the resolved path must lie INSIDE dest_dir.
            if target != dest_dir and dest_dir not in target.parents:
                raise BundleError(f"bundle member escapes the directory: {name!r}")
            if norm.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                # Copy as a stream - the budget is checked against what was
                # actually written, not the size declared in the member header.
                while True:
                    chunk = src.read(1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise BundleError(
                            f"bundle expands to more than {max_bytes} bytes "
                            "- looks like a zip-bomb")
                    dst.write(chunk)
