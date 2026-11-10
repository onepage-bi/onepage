"""Disk layer for the mirror: state, atomic writes, space accounting.

All network code lives in `sync.py`, the whole layout contract in `layout.py`,
and this module has exactly one duty: turn the decision "these bytes must be
stored" into a write on disk that either happened completely or did not
happen at all.

Why this is a separate module rather than a couple of functions inside the
synchronizer. The synchronizer must be INTERRUPTIBLE: the connection drops,
the laptop is closed, the agent dies mid-update -- these are routine events,
not accidents. Each of them happens in the middle of a write, and the only
protection against "a file with the right name and the wrong length" is a
single write point for everybody. Spread the writing across call sites, and
one path out of five will eventually write directly.

Three things settled here, each of which cost separate thought:

**1. The reason for a refusal lives OUTSIDE state.json.** A report with
`mirrorable: false` (RLS, ADR-18) is removed from the mirror together with its
directory, and `state.json` goes with the directory. So the reason cannot be
written there: we would delete it ourselves. The state schema
(`layout.new_state`) has no field for it, and `layout.migrate_state` drops
unknown keys on read -- so "I will add a field on the side" would silently not
work. Therefore the reason lives in `<mirror>/blocked.json`, an inventory of
what is not in the mirror and why. The library reads it so that the person
sees "report not downloaded: RLS" rather than concluding that the report got
lost (RFC section 4: such a report must not vanish from the list).

**2. Space is counted by walking, not by a counter.** The temptation to keep
"this much used" in state.json is understandable, but a counter lies after any
interruption, and it drifts in the dangerous direction: it will understate,
and the quota stops holding. Walking the directory costs hundreds of stat
calls for a mirror of dozens of reports -- once per sync cycle, not per
request.

**3. The ceiling is checked WHILE writing, not before.** `Content-Length`
may be missing or lie: until it is confirmed by bytes actually read, it is the
server's promise, not a fact. So `write_stream` counts what it has written and
bails out with `MirrorFull` as soon as the budget is exhausted, leaving behind
a `.tmp` that `sweep` will clear.

Standard library only (agent/ is Python 3.12, stdlib).
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from . import layout

# Inventory of what is NOT in the mirror. Lives in the mirror root, not in the
# report directory: that directory is exactly what gets deleted.
BLOCKED_NAME = "blocked.json"

# Chunk size for streaming writes. 256 KB is the same order as the server's
# streaming compression (`app/services/encoding.py`): smaller gives extra
# system calls on a multi-megabyte slot, larger gains nothing and increases
# the latency of reacting to a dropped connection.
CHUNK = 256 * 1024


class ShortRead(Exception):
    """The stream ended earlier than the server promised.

    A separate type because this is NOT a "write error" and not "low space",
    but silent data corruption: the connection dropped, `read()` returned
    nothing, and without checking against the promised size the stump would
    reach `os.replace` and land in the mirror as a finished slot. The report
    would show wrong numbers without saying anything -- exactly the kind of
    failure this project avoids.
    """


class MirrorFull(Exception):
    """The mirror hit the size ceiling.

    A separate exception rather than `OSError`: "the disk is out of space" and
    "we decided not to download any more" need different words for the person.
    The second is our policy, and it must be stated plainly, not passed off as
    a disk failure.
    """


# --------------------------------------------------------------------------- #
# State                                                                     #
# --------------------------------------------------------------------------- #

def load_state(dashboard_id: str) -> dict:
    """Read a report's state.json. No file / corrupt / foreign schema -> `{}`.

    A subtle point: an empty dict here is not an error but the regular answer
    "this report has no mirror". The caller must read it as "download
    everything again", and `layout.read_state` deliberately does not raise, so
    that an interruption does not need a separate branch in every reader.
    """
    return layout.read_state(dashboard_id)


def save_state(dashboard_id: str, state: dict) -> Path:
    """Write state.json atomically (via `layout.write_state`)."""
    return layout.write_state(dashboard_id, state)


def fresh_state(dashboard_id: str, title: str = "", dash_type: str = "") -> dict:
    """Empty state for a report that was not in the mirror yet."""
    return layout.new_state(dashboard_id, title=title, dash_type=dash_type)


# --------------------------------------------------------------------------- #
# File writing                                                              #
# --------------------------------------------------------------------------- #

def ensure_version_dir(dashboard_id: str, version: int) -> Path:
    """The `server/v<n>` directory for writing. Creates the path if needed."""
    return layout.server_version_dir(dashboard_id, version, create=True)


def write_bytes(path: Path, data: bytes) -> int:
    """Put ready bytes in place atomically. Returns the size written.

    For the document and snapshot: they arrive whole and fit in memory, so
    there is no reason to assemble them in pieces. Slots go through
    `write_stream`.
    """
    with layout.atomic_write(path, "wb") as fh:
        fh.write(data)
    return len(data)


def write_stream(path: Path, reader, budget: int | None = None,
                 chunk: int = CHUNK, expected: int | None = None) -> int:
    """Pour a stream into a file atomically. Returns the bytes written.

    `reader` is anything with a `read(n)` method: a urllib response, an open
    file, a test stub. Knowledge of HTTP deliberately does not leak in here --
    otherwise atomicity could not be tested without a network.

    `budget` is how many more bytes may be written into the mirror AS A WHOLE
    (not into this file). Exceeding it raises `MirrorFull`, and this is a
    refusal to download further, not a truncated file: on the exception
    `layout.atomic_write` removes the `.tmp`, and nothing that looks complete
    is left on disk. The check comes AFTER writing a chunk and before the next
    read, so the ceiling is not exceeded by more than one chunk.

    `expected` is how many bytes the server promised (`Content-Length`). If
    there is a promise and less was written, `ShortRead` is raised, and this is
    the MAIN check here. Atomic writing protects against a crash mid-write, but
    not against a cut-off response: the stream ended early, the write
    "succeeded", and half a file would land in the mirror as a whole one. The
    exception is raised INSIDE `atomic_write`, so the `.tmp` is removed and the
    previous complete file stays in place.

    We compare precisely with `Content-Length`, not with the size from the
    manifest: with `Content-Encoding: gzip` the compressed length goes over the
    wire, and the file size on the server does not match it by construction.

    A pitfall that is easy to step on here: a socket's `read()` returns less
    than requested perfectly routinely, and exiting on `len(buf) < chunk`
    aborts a normal download on the very first short read. There is only one
    exit condition -- an empty response.
    """
    written = 0
    with layout.atomic_write(path, "wb") as fh:
        while True:
            if budget is not None and written > budget:
                raise MirrorFull(
                    f"mirror hit the size ceiling at file {path.name}")
            buf = reader.read(chunk)
            if not buf:
                break
            fh.write(buf)
            written += len(buf)
        if expected is not None and written != expected:
            raise ShortRead(
                f"{path.name}: received {written} B of the promised "
                f"{expected} B")
    return written


# --------------------------------------------------------------------------- #
# Deletion and inventory of the unavailable                                 #
# --------------------------------------------------------------------------- #

def _rmtree(path: Path) -> int:
    """Tear down a directory, return how many bytes were freed.

    A custom implementation instead of `shutil.rmtree` for exactly one reason:
    we need the size, and computing it with a separate walk before deleting is
    a second walk and a race with ourselves. Errors on individual files are
    swallowed: half a deleted directory is better than an exception in the
    middle of a sync, and the rest will go on the next pass.
    """
    freed = 0
    if not path.exists():
        return 0
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            p = Path(root) / name
            try:
                freed += p.stat().st_size
            except OSError:
                pass
            with contextlib.suppress(OSError):
                p.unlink()
        for name in dirs:
            with contextlib.suppress(OSError):
                (Path(root) / name).rmdir()
    with contextlib.suppress(OSError):
        path.rmdir()
    return freed


def remove_dashboard(dashboard_id: str) -> int:
    """Remove a report from the mirror entirely. Returns the bytes freed."""
    try:
        path = layout.dashboard_dir(dashboard_id)
    except ValueError:
        return 0
    return _rmtree(path)


def prune_versions(dashboard_id: str, keep: int) -> int:
    """Tear down all `server/v<n>` except `keep`. Returns the bytes freed.

    The mirror does not need version history, by the owner's decision (RFC: we
    guard against downtime of days, not against data loss -- safekeeping is
    the job of `deploy/backup.sh`). If it is not swept, every publication
    doubles the space used, and the quota runs into its own garbage.
    `local/L<k>` is never touched: the person made the local run by hand, and
    it cannot be thrown away silently.
    """
    try:
        base = layout.dashboard_dir(dashboard_id) / layout.SPACE_SERVER
    except ValueError:
        return 0
    freed = 0
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for p in entries:
        if not p.is_dir() or not p.name.startswith("v"):
            continue
        if p.name == f"v{keep}":
            continue
        freed += _rmtree(p)
    return freed


def _blocked_path() -> Path:
    return layout.mirror_root() / BLOCKED_NAME


def blocked() -> dict:
    """Inventory of "what is not in the mirror and why".

    Shape: `{id: {title, reason, at}}`.

    A corrupt file reads as an empty inventory: this is a diagnostic for the
    library, and it must not fail because of it.
    """
    try:
        with open(_blocked_path(), "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(obj, dict):
        return {}
    return {k: v for k, v in obj.items() if isinstance(v, dict)}


def _write_blocked(data: dict) -> None:
    root = layout.mirror_root(create=True)
    with layout.atomic_write(root / BLOCKED_NAME, "w") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))


def mark_blocked(dashboard_id: str, title: str, reason: str, at: str) -> int:
    """Remove a report from the mirror and remember why it is not there.

    Two actions in one function on purpose: separately they drift apart.
    Delete without recording, and the person sees a vanished report with no
    explanation; record without deleting, and a copy of a report that must not
    be there stays on disk (for RLS this is exactly the ADR-18 leak). Returns
    the bytes freed.
    """
    freed = remove_dashboard(dashboard_id)
    data = blocked()
    data[dashboard_id] = {"title": title or "", "reason": reason or "", "at": at}
    _write_blocked(data)
    return freed


def unmark_blocked(dashboard_id: str) -> None:
    """Forget the reason: the report is allowed to be mirrored again.

    Needed because the ban gets lifted (the RLS policy was removed from the
    report, a draft was published), and a stuck row would show the person "no
    access" for a report that is already on disk.
    """
    data = blocked()
    if data.pop(dashboard_id, None) is None:
        return
    _write_blocked(data)


# --------------------------------------------------------------------------- #
# Space accounting and listing                                              #
# --------------------------------------------------------------------------- #

def dir_size(path: Path) -> int:
    """Total size of the files under a directory. Inaccessible ones skipped.

    We take `st_size`, not what is occupied on disk: we need to compare with a
    ceiling the person set in megabytes, not reproduce `du`.
    """
    total = 0
    try:
        walker = os.walk(path, onerror=lambda e: None)
    except OSError:
        return 0
    for root, _dirs, files in walker:
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def usage_bytes() -> int:
    """How much space the whole mirror takes. No directory -> 0."""
    root = layout.mirror_root()
    if not root.exists():
        return 0
    return dir_size(root)


def list_mirrored() -> list[dict]:
    """What lies in the mirror -- for the library and for the tray.

    For each report its state (`state`) and the actual size on disk are given.
    A report without a readable state.json also gets into the list, with
    `state: {}` -- so "the directory exists, the inventory got lost" is
    visible rather than masked as "no such report". Sorted by id so the list
    does not jump between calls.
    """
    root = layout.mirror_root()
    if not root.exists():
        return []
    out: list[dict] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for p in entries:
        if not p.is_dir() or not layout.is_valid_dashboard_id(p.name):
            continue
        out.append({
            "dashboard_id": p.name,
            "state": load_state(p.name),
            "bytes": dir_size(p),
        })
    return out


def sweep(dashboard_id: str | None = None) -> int:
    """Sweep up `*.tmp` left over from interrupted writes.

    Without an argument it walks the whole mirror. It is worth calling at the
    start of a sync, not at the end: there may have been no end -- which is
    exactly why the `.tmp` remained.
    """
    removed = 0
    root = layout.mirror_root()
    if not root.exists():
        return 0
    if dashboard_id is not None:
        try:
            base = layout.dashboard_dir(dashboard_id)
        except ValueError:
            return 0
    else:
        base = root
    removed += layout.sweep_tmp(base)
    try:
        walker = os.walk(base, onerror=lambda e: None)
    except OSError:
        return removed
    for cur, _dirs, _files in walker:
        if Path(cur) == base:
            continue
        removed += layout.sweep_tmp(Path(cur))
    return removed
