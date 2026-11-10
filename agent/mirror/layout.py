"""Mirror layout on disk, the state.json schema, and local addresses.

This module is a CONTRACT that the synchronizer, the local server, and the
viewer all rely on. There is no network code here at all: only paths, names,
the state schema, and parsing/building of local URLs. The reason: three
different parts of the program must compute the same file name and the same
address, and if each computes its own, the mismatch shows up not in a test but
on the person's machine, when the server is already down.

Standard library only (agent/ is Python 3.12, stdlib; see agent/README).

Layout
------
    <state_dir()>/mirror/<dashboard_id>/server/v<n>/   — mirror of the published version
    <state_dir()>/mirror/<dashboard_id>/local/L<k>/    — local runs
    <state_dir()>/mirror/<dashboard_id>/state.json     — mirror state

`server/v<n>` is numbered by the SERVER version number: that, and only that,
is what "mine is out of date" is checked against. `local/L<k>` is its OWN
number space, and this is not whimsy: a local run has no server number before
publication, and if it were numbered "next after the server one", the very
first publication by someone else would produce two different directories with
the same number. The `L` in the name keeps any tool that looks for `v<number>`
from picking up a local run by accident.

The root lives UNDER `config.state_dir()` (`%LOCALAPPDATA%\\op-agent` on
Windows), i.e. outside the portable agent folder: copying the mirror together
with the program on a USB stick is impossible by construction, and the DPAPI
blob of the device key sits next to it for the same reason (agent/creds.py).

What is DELIBERATELY absent here: reports with `mirrorable: false`. The ADR-18
ban is enforced higher up, in the synchronizer, but there is also not a single
function here that would build a "where to put an RLS report" path — so that
such a path cannot be obtained by carelessness.

Two things that make this module exist separately
-------------------------------------------------

**1. The slot file name.**

In the `/api/sync/manifest` response a slot has a `name` — the ORIGINAL name,
the very one by which the server resolves the report's relative assets
(`storage.slot_asset_path`, route `render_asset`). It must not be lost: a
report written by the model carries `<script src="chart.js">`, and if the file
on disk is called `slot_3_a1b2c3`, the document opens empty.

But this name came from the network and can contain anything. The person who
put it there is not an attacker from the internet but the report author, who
uploaded a file named `../../config.json` simply because that was the column
name in the export; the outcome is the same. The list of pitfalls, each
checked in an ad-hoc run (see the "Verification" section below):

  * a path: `a/b.js`, `..\\..\\evil.js`, `/etc/passwd`, `C:\\win\\sys.dll`;
  * `..` and `.` as the whole name;
  * an NTFS stream: `report.js:evil` — Windows creates a hidden stream on
    `report.js`;
  * reserved devices: `CON`, `NUL`, `PRN`, `AUX`, `COM1`, `LPT1`, and they are
    reserved TOGETHER WITH AN EXTENSION — `NUL.js` is a device too;
  * a trailing dot or space: Windows silently strips them on creation, so
    `data.js.` and `data.js ` are ONE file, and `...` is an empty name;
  * forbidden characters `< > : " | ? *` and control characters 0x00–0x1F;
  * a too-long name: the 260-character path limit applies in Python too unless
    long paths are enabled on the machine, and ~100 are already used up by the
    time we reach our directory.

What `safe_slot_filename` DELIBERATELY does not do: it does not strip bidi
characters (`U+202E` and relatives). They cannot write a file to the wrong
place — they only change how the name READS in Explorer and in the log
(`evil\u202esj.txt` looks like `evil txt.js`). That is trickery for the eyes,
not an escape from the directory, and it must be fixed where the name is shown
to a person, not where it is put on disk.

The solution is split into TWO entities, and this is the main decision of this
module:

  * `name` in state.json — the original name. It is authoritative for
    RESOLVING and is never repaired, truncated, or normalized.
  * `file` in state.json — the name on disk, the result of
    `safe_slot_filename()`. In 99 % of cases it equals `name` (a plain
    `data.bin.gz` passes straight through), and then the requirement "store
    under the original names" is met literally.

Asset resolution (`resolve_asset`) matches the REQUESTED name against `name`
and returns `file`. So a name from the network NEVER becomes a path component:
the path is taken from state.json, which we wrote. This is a structural
defence, not a check — a check can be forgotten to be called, while here there
is simply no code that would glue a path from a network string.
`safe_slot_filename()` is the second line, in case someone does glue one.

**2. Local addresses.**

The document must be served at an address WITH A TRAILING SLASH, like
`/render/{id}/` on the server. This is not cosmetics: a relative
`<script src="x.js">` and the externalized `snapshot.js?v=…`
(`OP_SNAPSHOT_EXTERNAL`, `detector.externalize_snapshot`) are resolved by the
browser from the DIRECTORY of the current document. For the address
`/mirror/<id>` the directory is `/mirror/`, and `x.js` would go to
`/mirror/x.js`, i.e. to another report or a 404. So a request without the
slash is not "also the document" but a 307 to the address with the slash;
`parse_local_path` returns a separate kind `KIND_REDIRECT` for it, so that the
server cannot "forget" about it.

The shape of the addresses deliberately repeats the server's: the fewer the
differences between `/render/{id}/` and the mirror, the lower the chance that
a report that works in the browser fails to work offline.

    /mirror/<id>/                 the document (mirror of the published one)
    /mirror/<id>/snapshot.js       the externalized snapshot (?v= is ignored)
    /mirror/<id>/files/<slot>      a slot by NUMBER — this is how the viewer asks
    /mirror/<id>/<asset>           a slot by its ORIGINAL name — this is how
                                   the document itself asks
    /mirror/<id>/local/L<k>/       the same for a local run,
    /mirror/<id>/local/L<k>/...    including all three forms above

The `/mirror/` prefix was chosen because the agent already uses `/health`,
`/pair*`, `/runs*` (agent/server.py), and overlapping with them would be a
silent breakage.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote

from ..config import state_dir

# --------------------------------------------------------------------------- #
# Layout constants                                                           #
# --------------------------------------------------------------------------- #

MIRROR_DIRNAME = "mirror"
STATE_NAME = "state.json"

# Version of the state.json schema. It will have to change: the set of slots,
# the list of local runs, and the set of fetched rights are exactly the places
# that will grow. The rule is simple: an INCOMPATIBLE change -> +1 and a branch
# in `migrate_state`; adding an optional field does not move the version (the
# reader must already survive its absence, see `new_state`).
STATE_SCHEMA = 1

# How long a fetched right lives if the server did not say otherwise. The
# duplicate of `app/api/sync.py::RIGHTS_TTL_S` here is INTENTIONAL: offline
# there is nobody to ask the server, and the value from the last manifest is
# put into state.json (`rights.ttl_s`) and used from there. This is the
# default for state created before the first successful sync.
DEFAULT_RIGHTS_TTL_S = 24 * 3600

URL_PREFIX = "/mirror"

# Request kinds that the local server can tell apart.
KIND_DOCUMENT = "document"
KIND_SNAPSHOT = "snapshot"
KIND_SLOT = "slot"
KIND_ASSET = "asset"
KIND_REDIRECT = "redirect"      # came without a trailing slash -> 307

SPACE_SERVER = "server"
SPACE_LOCAL = "local"

# Directory of the generator bundle cache: `<mirror>/<id>/bundle/<sha256>.zip`.
# A separate directory, not inside `server/v<n>`, for one reason: a bundle
# lives by ITS OWN generator version, not by the report version, and
# `prune_versions`, which sweeps past `server/v<n>`, must not take with it the
# only thing a person can recompute the report with when the platform is down.
BUNDLE_DIRNAME = "bundle"

# The bundle file name is its own checksum: the very `checksum_sha256` by which
# the version manifest is named on the server and against which unpacking is
# verified (`runs.verify_and_unpack`). A name derived from the sum means that a
# substituted or partially downloaded bundle cannot take the place of the right
# one, and a new generator version does not overwrite the old one but lands
# next to it.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

SNAPSHOT_NAME = "snapshot.js"
DOCUMENT_NAME = "dashboard.html"

# --------------------------------------------------------------------------- #
# Report identifier                                                          #
# --------------------------------------------------------------------------- #

# On the server this is a uuid4 (`Dashboard.id`, String(36)), but one must not
# rely on the length: the field is a string, and "36 characters" is not an
# invariant but today's default. So we take a demonstrably safe alphabet and
# CHECK rather than repair: an id is a key, not text. A repaired id is someone
# else's directory: `a/b` and `a_b` would become one mirror of two different
# reports after normalization.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def is_valid_dashboard_id(value: object) -> bool:
    """Whether the string is acceptable as a mirror directory name.

    Names made only of dots are rejected separately: `_ID_RE` allows a dot
    inside, while `..` must be rejected (it is an escape from the directory,
    not a name).
    """
    if not isinstance(value, str) or not _ID_RE.match(value):
        return False
    return value.strip(".") != ""


# --------------------------------------------------------------------------- #
# Paths                                                                       #
# --------------------------------------------------------------------------- #

def mirror_root(create: bool = False) -> Path:
    """Mirror root — a subdirectory of `state_dir()`.

    `create=False` by default: read paths (viewer, library) must not create an
    empty directory just by being called — otherwise "no mirror yet" and
    "the mirror is empty" become indistinguishable.
    """
    root = state_dir() / MIRROR_DIRNAME
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def dashboard_dir(dashboard_id: str, create: bool = False) -> Path:
    """`<mirror>/<dashboard_id>`. An invalid id raises ValueError, no normalization."""
    if not is_valid_dashboard_id(dashboard_id):
        raise ValueError("invalid dashboard_id for a mirror directory")
    path = mirror_root(create=create) / dashboard_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def server_version_dir(dashboard_id: str, version: int, create: bool = False) -> Path:
    """`<mirror>/<id>/server/v<n>` — mirror of the published version."""
    n = _as_version(version, "server_version")
    path = dashboard_dir(dashboard_id, create=create) / SPACE_SERVER / f"v{n}"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def local_run_dir(dashboard_id: str, n: int, create: bool = False) -> Path:
    """`<mirror>/<id>/local/L<k>` — a local run, its own number space."""
    k = _as_version(n, "local_run")
    path = dashboard_dir(dashboard_id, create=create) / SPACE_LOCAL / f"L{k}"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def bundle_dir(dashboard_id: str, create: bool = False) -> Path:
    """`<mirror>/<id>/bundle` — cache of this report's generator bundles."""
    path = dashboard_dir(dashboard_id, create=create) / BUNDLE_DIRNAME
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def bundle_path(dashboard_id: str, checksum_sha256: object,
                create: bool = False) -> Path:
    """Path to a bundle by its checksum.

    The sum is CHECKED, not repaired: it comes from the network (the sync
    manifest) and becomes a file name, i.e. exactly the same input as the
    version number in `_as_version`. `../../x` in this field is not a reason
    to normalize the string but a reason to refuse.
    """
    if not isinstance(checksum_sha256, str) or not _SHA256_RE.match(checksum_sha256):
        raise ValueError("bundle checksum does not look like a sha256")
    return bundle_dir(dashboard_id, create=create) / f"{checksum_sha256}.zip"


def state_file(dashboard_id: str) -> Path:
    """`<mirror>/<id>/state.json`."""
    return dashboard_dir(dashboard_id) / STATE_NAME


def _as_version(value: object, what: str) -> int:
    """A version number is an integer >= 1. A separate function because the
    number also comes from the network (`version` in the manifest) and also
    becomes a directory name: `v-1` or `v../..` in a name is exactly the same
    hole as in a file name."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what}: expected an integer, got {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{what}: version number must be >= 1, got {value}")
    return value


def dir_for(dashboard_id: str, space: str, n: int, create: bool = False) -> Path:
    """Directory by number-space kind — so the server does not branch by hand."""
    if space == SPACE_SERVER:
        return server_version_dir(dashboard_id, n, create=create)
    if space == SPACE_LOCAL:
        return local_run_dir(dashboard_id, n, create=create)
    raise ValueError(f"unknown version space: {space!r}")


# --------------------------------------------------------------------------- #
# Safe slot file name — the main hole of this module                          #
# --------------------------------------------------------------------------- #

# Windows: `< > : " / \ | ? *` are forbidden in names, and so are 0x00–0x1F.
_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')

# Reserved device names. They are reserved together with any extension:
# `NUL.js` is a device, not a file. `CONIN$`/`CONOUT$` are in the list because
# they behave the same way, although the documentation lists them separately.
_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

# Name length budget. The arithmetic the number comes from:
#   C:\Users\<login>\AppData\Local\op-agent\mirror\<id-36>\server\v<n>\<name>
#   ~ 9 + len(login) + 29 + 1 + 36 + 1 + 6 + 1 + 4 + 1  ~ 90 + len(login)
# The 260-character limit stays in force until long paths are enabled on the
# machine (and on a corporate machine without admin rights they cannot be
# enabled). A login like `10000001_corp` eats another ten or so, so we leave
# the name 100 and keep ~60 characters of reserve for future subdirectories.
MAX_NAME_CHARS = 100

# Length of the distinguishing tail `~<hash8>` (see below).
_HASH_LEN = 8


def _name_digest(original: str) -> str:
    """A short fingerprint of the ORIGINAL name.

    It is computed from the original, not from the already cleaned string: two
    different evil names (`../x.js` and `..\\x.js`) are cleaned to the same
    `x.js`, and if the tail were computed after cleaning, they would collapse
    into one file again.
    """
    raw = original.encode("utf-8", "surrogatepass")
    return hashlib.sha1(raw).hexdigest()[:_HASH_LEN]


def _split_suffix(name: str) -> tuple[str, str]:
    """Split a name into a stem and an extension tail (up to two, like `.bin.gz`).

    Two extensions, not one, because our slots are exactly like that:
    `data.bin.gz`, `payload.bin.br` — cutting to `data.bin` means losing the
    compression marker by which the server sets `Content-Encoding`.
    """
    parts = name.split(".")
    if len(parts) == 1 or not parts[0]:
        return name, ""
    tail: list[str] = []
    for chunk in reversed(parts[1:]):
        if not chunk or len(chunk) > 8 or not chunk.isalnum() or len(tail) == 2:
            break
        tail.insert(0, chunk)
    if not tail:
        return name, ""
    stem = ".".join(parts[: len(parts) - len(tail)])
    return stem, "." + ".".join(tail)


def safe_slot_filename(original: object, slot: int = 0, taken=()) -> str:
    """The name under which a slot will be placed on disk. Never leaves the directory.

    Guarantees on the result (all checked by an ad-hoc run over a list of evil
    names):
      * non-empty, one path component — no `/`, no `\\`, no `:`;
      * not `.` and not `..`;
      * no forbidden or control characters;
      * does not end with a dot or space;
      * is not a reserved Windows device;
      * no longer than `MAX_NAME_CHARS` characters;
      * not present in `taken`.

    `original` deliberately accepts `object`: it is a value from JSON, and
    `None` or a number are possible there. Such input is not an exception but
    ordinary junk — we derive the name from the slot number and move on.

    `taken` is needed because original names within one version are NOT
    unique: the server stores slots as `slot_<n>_<name>`, and two slots can
    carry the same name (the server itself in `slot_asset_path` takes the first
    match, so it is already ambiguous). Two files with one name on disk is a
    silently lost slot, so the second one gets `~<slot>` appended.

    What the function does NOT do: it does not promise that the result equals
    the original. For asset resolution, the original from state.json
    (`resolve_asset`) is used, not this name — see the module docstring.
    """
    if not isinstance(original, str):
        original = ""
    raw = original

    # 1. Only the last path component. We cut on BOTH separators: the agent
    #    writes to a real file system, and on Windows `/` is a separator too
    #    (the same reason zipguard checks both).
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]

    # 2. The colon. Two different cases, and they must not be confused:
    #    `C:data.js` is a path relative to the current directory of drive C,
    #    and the meaningful part here is AFTER the colon (cutting on the first
    #    part would give a file named `C`);
    #    `report.js:evil` is an NTFS stream, i.e. a hidden entry INSIDE
    #    `report.js`, and the meaningful part here is BEFORE the colon.
    if re.match(r"^[A-Za-z]:", name):
        name = name[2:]
    if ":" in name:
        name = name.split(":", 1)[0]

    # 3. Forbidden and control characters become an underscore. It is a
    #    replacement here, not a removal: removal brings different names closer.
    name = _BAD_CHARS_RE.sub("_", name)

    # 4. Windows strips trailing dots and spaces ITSELF when creating a file,
    #    i.e. `data.js.` and `data.js` are one file. We strip them ourselves so
    #    that the name on disk matches what we wrote to state.json.
    name = name.rstrip(". ")

    # 5. What remains of `..`, `.`, `...`, `   ` and the empty string is not a name.
    if not name or name in (".", ".."):
        # The fingerprint is put in right away in the `~<hash>` form that
        # step 8 recognizes: otherwise it would consider the name "repaired
        # without a marker" and attach the fingerprint a second time
        # (`slot_9_9d891e73~9d891e73` — the first run produced exactly that).
        name = f"slot_{slot}~{_name_digest(raw)}"

    # 6. Devices. We check the stem BEFORE the first dot: `NUL.js` is reserved
    #    just like `NUL`. A prefix, not a replacement — so that `CON.js`
    #    stays recognizable in the log.
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name

    # 7. Length. We truncate the STEM and keep the extension: the server sets
    #    Content-Type and Content-Encoding from it. The tail with the
    #    fingerprint is mandatory — otherwise two long names with a common
    #    beginning would become one file.
    if len(name) > MAX_NAME_CHARS:
        stem, suffix = _split_suffix(name)
        suffix = suffix[:16]
        keep = MAX_NAME_CHARS - len(suffix) - _HASH_LEN - 1
        if keep < 1:
            stem, suffix, keep = "slot", "", MAX_NAME_CHARS - _HASH_LEN - 1
        name = f"{stem[:keep]}~{_name_digest(raw)}{suffix}"

    # 8. If the name had to be EDITED, we add a fingerprint of the original:
    #    otherwise `../x.js` and `..\\x.js` would both become `x.js` and
    #    overwrite each other. A name that passed straight through stays
    #    literally the original — which is exactly what the contract "store
    #    under the original names" requires.
    if name != raw:
        stem, suffix = _split_suffix(name)
        marker = f"~{_name_digest(raw)}"
        if marker not in name:
            name = f"{stem}{marker}{suffix}"
            if len(name) > MAX_NAME_CHARS:
                cut = MAX_NAME_CHARS - len(marker) - len(suffix)
                name = f"{stem[:max(cut, 1)]}{marker}{suffix}"

    # 9. Occupancy. `~<slot>` — the slot number is unique within a version by
    #    construction, so the loop terminates rather than spinning.
    if name in taken:
        stem, suffix = _split_suffix(name)
        name = f"{stem}~{slot}{suffix}"
        attempt = 0
        while name in taken:
            attempt += 1
            name = f"{stem}~{slot}_{attempt}{suffix}"

    return name


def plan_slot_files(slots) -> list[dict]:
    """Lay out the slot list from the manifest into file names on disk.

    The input is `slots` from `/api/sync/manifest` (`[{slot, name, size, v}]`),
    the output is what goes into `state.json["slots"]`. Deterministic: ordered
    by slot number, so a re-sync gives the same names rather than new ones.

    A slot without a usable number is dropped silently: the number is what
    `viewer` asks for the file by (`/files/<slot>`), and without it the file
    cannot be served to anyone anyway.
    """
    planned: list[dict] = []
    taken: set[str] = set()
    for item in sorted(_iter_dicts(slots), key=lambda s: _slot_no(s)):
        no = _slot_no(item)
        if no is None:
            continue
        original = item.get("name")
        original = original if isinstance(original, str) else ""
        disk = safe_slot_filename(original, slot=no, taken=taken)
        taken.add(disk)
        planned.append({
            "slot": no,
            # The original is authoritative for asset resolution, never repaired.
            "name": original,
            # The name on disk. Equals `name` as long as the name is safe.
            "file": disk,
            "size": item.get("size") if isinstance(item.get("size"), int) else None,
            # `v` from the manifest (mtime+size on the server) — the server
            # addresses the slot by it (`?v=`), and we use it to decide
            # "re-download or not".
            "v": item.get("v") if isinstance(item.get("v"), str) else None,
            # ETag of the slot download response. Filled in by the
            # synchronizer; the field is created ahead of time here so that
            # the schema does not change because of it.
            "etag": None,
        })
    return planned


def _iter_dicts(value):
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


def _slot_no(item: dict):
    no = item.get("slot")
    if isinstance(no, bool) or not isinstance(no, int) or no < 0:
        return None
    return no


# --------------------------------------------------------------------------- #
# Asset resolution: network name -> name on disk                              #
# --------------------------------------------------------------------------- #

# Variants the server tries in `render_asset`: the author writes
# `<script src="x.js">`, and a pre-compressed `x.js.br` lies on disk. We repeat
# the order, otherwise the report shows a 404 offline where it works online.
_ASSET_VARIANTS = ("", ".gz", ".br")


def resolve_asset(slots, requested: str):
    """Find a slot by its ORIGINAL name. Returns the slot record or None.

    Matching goes against the data from state.json, so the requested string
    never becomes a path component. That is the protection: even
    `requested = "../../state.json"` simply matches nothing.

    Comparison is exact, without casefold: the server (`slot_asset_path`)
    compares exactly, and a discrepancy here would give a report that works
    offline but not online — worse than the other way round.

    With a DUPLICATE original name (two slots with the same `name` — the
    server allows this) the LOWER slot number wins. This is not arbitrary: the
    server in `slot_asset_path` takes the first match while walking the
    directory, and the names there are `slot_<n>_<name>`, i.e. effectively the
    lower number too. Offline must serve the same file the server would, even
    if the choice is questionable in itself.
    """
    if not isinstance(requested, str) or not requested:
        return None
    by_name: dict = {}
    for item in sorted(_iter_dicts(slots), key=lambda s: (_slot_no(s) is None,
                                                          _slot_no(s) or 0)):
        key = item.get("name")
        if isinstance(key, str) and key and key not in by_name:
            by_name[key] = item
    for suffix in _ASSET_VARIANTS:
        hit = by_name.get(requested + suffix)
        if hit is not None:
            return hit
    return None


def slot_by_number(slots, slot: int):
    """Find a slot by number — this is how the `op_ready`->`op_file` bridge asks."""
    for s in _iter_dicts(slots):
        if _slot_no(s) == slot:
            return s
    return None


def slot_disk_path(version_dir: Path, slot_record: dict) -> Path:
    """Path to a slot file inside a version directory.

    The containment check lives HERE, at the single point where a slot record
    turns into a path: we write state.json ourselves, but it sits on disk and
    could have been substituted or damaged by an interruption, and "we wrote it
    ourselves" is a poor basis for writing to somebody else's path.
    """
    name = slot_record.get("file") or slot_record.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("the slot record has no file name")
    path = (version_dir / name).resolve()
    root = version_dir.resolve()
    if path == root or root not in path.parents:
        raise ValueError(f"slot name escapes the version directory: {name!r}")
    return path


# --------------------------------------------------------------------------- #
# Local addresses                                                            #
# --------------------------------------------------------------------------- #

class MirrorRequest:
    """A parsed local request path.

    `kind` is one of `KIND_*`. For `KIND_REDIRECT`, `location` is filled in: the
    server must answer 307 to it rather than serve the document (see the module
    docstring about the trailing slash).
    """

    __slots__ = ("kind", "dashboard_id", "space", "n", "slot", "asset", "location")

    def __init__(self, kind, dashboard_id=None, space=SPACE_SERVER, n=None,
                 slot=None, asset=None, location=None):
        self.kind = kind
        self.dashboard_id = dashboard_id
        self.space = space
        self.n = n                  # local run number; None -> server
        self.slot = slot
        self.asset = asset
        self.location = location

    def __repr__(self) -> str:    # for the log; we are not shy about the asset name here
        return (f"MirrorRequest(kind={self.kind!r}, id={self.dashboard_id!r}, "
                f"space={self.space!r}, n={self.n!r}, slot={self.slot!r}, "
                f"asset={self.asset!r})")


def parse_local_path(target: str):
    """Parse a request path into a `MirrorRequest` or return None.

    None means "this is not a mirror address" — the server answers 404 and does
    not guess.

    What the function does and in what order, because the order matters here:
      1. cuts off `?query` — `?v=` on `snapshot.js` is only for cache busting;
      2. percent-decodes EACH segment separately and AFTER splitting on `/`.
         If decoded earlier, `%2F` would turn into a separator and `a%2F..`
         would become an escape from the directory — classic double encoding;
      3. rejects the segments `.`, `..` and anything with `\\` or NUL inside:
         there is no such thing in an honest address, and working out what the
         client actually meant is not our business.
    """
    if not isinstance(target, str) or not target.startswith(URL_PREFIX):
        return None
    path = target.split("?", 1)[0].split("#", 1)[0]
    rest = path[len(URL_PREFIX):]
    if rest and not rest.startswith("/"):
        return None                     # `/mirrorXXX` is not our address

    trailing = rest.endswith("/")
    raw_parts = [p for p in rest.split("/") if p]
    parts: list[str] = []
    for p in raw_parts:
        seg = unquote(p)
        if seg in (".", "..") or "\\" in seg or "\x00" in seg or "/" in seg:
            return None
        parts.append(seg)

    if not parts:
        return None
    dashboard_id = parts[0]
    if not is_valid_dashboard_id(dashboard_id):
        return None
    tail = parts[1:]

    space, n = SPACE_SERVER, None
    if tail and tail[0] == SPACE_LOCAL:
        if len(tail) < 2:
            return None
        m = re.match(r"^L([1-9][0-9]{0,8})$", tail[1])
        if not m:
            return None
        space, n = SPACE_LOCAL, int(m.group(1))
        tail = tail[2:]
        if not tail and not trailing:
            return MirrorRequest(KIND_REDIRECT, dashboard_id, space, n,
                                 location=local_document_url(dashboard_id, n))

    if not tail:
        # The document itself. Without a trailing slash — only a redirect:
        # otherwise relative assets would go one level up.
        if not trailing:
            return MirrorRequest(KIND_REDIRECT, dashboard_id, space, n,
                                 location=document_url(dashboard_id))
        return MirrorRequest(KIND_DOCUMENT, dashboard_id, space, n)

    if tail == [SNAPSHOT_NAME]:
        return MirrorRequest(KIND_SNAPSHOT, dashboard_id, space, n)

    if len(tail) == 2 and tail[0] == "files":
        if not re.match(r"^[0-9]{1,4}$", tail[1]):
            return None
        return MirrorRequest(KIND_SLOT, dashboard_id, space, n, slot=int(tail[1]))

    if len(tail) == 1:
        # A relative report asset. We pass the name AS IT CAME: it must be
        # matched against the slot's original name (`resolve_asset`), not
        # repaired.
        return MirrorRequest(KIND_ASSET, dashboard_id, space, n, asset=tail[0])

    # Assets have no subdirectories: slots are a flat list, and the server
    # resolves them just as flatly. A nested path is a reason to answer 404,
    # not to look for "something similar".
    return None


def document_url(dashboard_id: str) -> str:
    """Mirror document address. ALWAYS with a trailing slash."""
    return f"{URL_PREFIX}/{quote(dashboard_id, safe='')}/"


def local_document_url(dashboard_id: str, n: int) -> str:
    """Local run document address. Also with a slash, for the same reason."""
    k = _as_version(n, "local_run")
    return f"{URL_PREFIX}/{quote(dashboard_id, safe='')}/{SPACE_LOCAL}/L{k}/"


def base_url(dashboard_id: str, space: str = SPACE_SERVER, n: int | None = None) -> str:
    """Base address (with a slash) from which assets are resolved."""
    if space == SPACE_LOCAL:
        return local_document_url(dashboard_id, n if n is not None else 1)
    return document_url(dashboard_id)


def snapshot_url(dashboard_id: str, fingerprint: str | None = None,
                 space: str = SPACE_SERVER, n: int | None = None) -> str:
    """Address of the externalized snapshot. `?v=` — exactly as on the server."""
    url = base_url(dashboard_id, space, n) + SNAPSHOT_NAME
    if fingerprint:
        url += "?v=" + quote(fingerprint[:12], safe="")
    return url


def slot_url(dashboard_id: str, slot: int, v: str | None = None,
             space: str = SPACE_SERVER, n: int | None = None) -> str:
    """Slot address by number — requested by the `op_file` bridge from the viewer."""
    url = f"{base_url(dashboard_id, space, n)}files/{int(slot)}"
    if v:
        url += "?v=" + quote(v, safe="")
    return url


def asset_url(dashboard_id: str, original_name: str,
              space: str = SPACE_SERVER, n: int | None = None) -> str:
    """Asset address by original name. Rarely needed — usually the report
    writes this address itself as a relative link, and there is no need to
    build it."""
    return base_url(dashboard_id, space, n) + quote(original_name, safe="")


# --------------------------------------------------------------------------- #
# state.json                                                                  #
# --------------------------------------------------------------------------- #

def new_state(dashboard_id: str, title: str = "", dash_type: str = "") -> dict:
    """An empty mirror state — the one place that describes the state.json composition.

    The fields and why they are here:
      schema            version of THIS FILE's schema (see `STATE_SCHEMA`);
      dashboard_id      duplicate of the directory name: a file that lost its
                        connection to the path must remain identifiable;
      title, dash_type  the library and viewer draw the list offline, when
                        there is nobody to ask the server;
      folder            the platform folder name (or empty — "No folder"). The
                        name, not the id: list sections are labelled offline,
                        and at that moment there is nobody to ask the server;
      size              weight of the rendered document per the manifest: the
                        tile uses it to decide whether to show a live frame
                        or a placeholder;
      server_version    the server number of what lies in `server/v<n>`;
      fingerprint       the fingerprint from the manifest — catches a theme/
                        text/slot edit that does NOT move `server_version`
                        (`app/api/sync.py`, `storage.version_fingerprint`);
      updated_at        server time of the edit, WITH the time, not just the day;
      slots             see `plan_slot_files`: slot, original name, name on
                        disk, size, `v`, ETag;
      snapshot_external whether the snapshot is externalized as a separate
                        file: forgetting it means showing an empty report;
      rights            fetched rights and their expiry. can_update_data and
                        rls_enabled live INSIDE, next to `fetched_at`/
                        `expires_at`, precisely so that a right cannot be read
                        without seeing when it was fetched;
      last_sync_at      the last SUCCESSFUL sync. Separate from
                        `rights.fetched_at`: a manifest check without a
                        download is successful too;
      server_seen       what the manifest named in the last pass (version,
                        fingerprint, when) — BEFORE downloading. Comparing it
                        with `server_version`/`fingerprint` answers the
                        question "is the copy out of date?" offline;
      generator         the report's generator and its bundle in the cache:
                        `{id, name, version, checksum_sha256}`. Needed by a
                        local run and nobody else;
      local_runs        local runs: `k`, when created, which server version
                        it was counted from, whether already published to the
                        server;
      next_local        the next free `k`. Stored, not computed from
                        max(local_runs): a deleted run must not hand its
                        number to a new one, otherwise the `local/L3`
                        directory would turn out to be someone else's.
    """
    if not is_valid_dashboard_id(dashboard_id):
        raise ValueError("invalid dashboard_id for state.json")
    return {
        "schema": STATE_SCHEMA,
        "dashboard_id": dashboard_id,
        "title": title or "",
        "dash_type": dash_type or "",
        "folder": "",
        "size": 0,
        "server_version": None,
        "fingerprint": None,
        "updated_at": None,
        "slots": [],
        "snapshot_external": False,
        "rights": {
            "can_update_data": False,
            "rls_enabled": False,
            "fetched_at": None,
            "expires_at": None,
            "ttl_s": DEFAULT_RIGHTS_TTL_S,
        },
        "last_sync_at": None,
        # What the manifest NAMED in the last pass — before we started
        # downloading. Without this field there is nothing to say "mine is out
        # of date" with: all the other state is written AFTER a successful
        # download, so a partially downloaded report would look exactly like an
        # up-to-date one (the library really did show "Copy from …" in both
        # cases).
        "server_seen": {"version": None, "fingerprint": None, "at": None},
        # The report's generator and its bundle in the cache: without them a
        # local run is impossible in principle — the platform is down, and
        # there is nobody to ask for the bundle.
        "generator": None,
        "local_runs": [],
        "next_local": 1,
    }


def migrate_state(obj: object) -> dict:
    """Bring the state that was read to the current schema.

    The rule that makes this function needed at all: state from a FUTURE schema
    (the person rolled the program back to an old version) is neither repaired
    nor read partially — an empty state is returned, and the mirror is
    re-synchronized. Reading an unfamiliar schema "as best we can" means giving
    the person a report assembled from fields nobody understands, and they
    would not know about it.

    Schema 1 is the first, there is nothing to migrate from yet; the
    `schema < STATE_SCHEMA` branch exists as the place where the first real
    migration will go.
    """
    if not isinstance(obj, dict):
        return {}
    schema = obj.get("schema")
    if not isinstance(schema, int) or schema > STATE_SCHEMA:
        return {}
    dashboard_id = obj.get("dashboard_id")
    if not is_valid_dashboard_id(dashboard_id):
        return {}
    base = new_state(dashboard_id)
    for key, default in base.items():
        if key in ("schema", "dashboard_id"):
            continue
        value = obj.get(key, default)
        if isinstance(default, dict) and isinstance(value, dict):
            merged = dict(default)
            merged.update(value)
            value = merged
        elif isinstance(default, list) and not isinstance(value, list):
            value = default
        base[key] = value
    base["schema"] = STATE_SCHEMA
    return base


def read_state(dashboard_id: str) -> dict:
    """Read state.json. Absence, broken JSON, and a foreign schema -> empty dict.

    No exception is raised here DELIBERATELY: an interrupted sync is a normal
    event for this program, and "no state" means to the caller exactly the same
    as "must sync again".
    """
    try:
        path = state_file(dashboard_id)
    except ValueError:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return migrate_state(json.load(fh))
    except (OSError, ValueError):
        return {}


def write_state(dashboard_id: str, state: dict) -> Path:
    """Write state.json atomically. Returns the path.

    Atomicity here is not overcaution: state.json is the INVENTORY of the
    mirror, and a half-file that looks whole means a report assembled from
    nonexistent slots. A `.tmp` next to it + `os.replace` (not a `rename`
    across different file systems, and not an in-place write), plus `fsync`
    before the swap — otherwise on Windows after a sudden power-off one can get
    a file of the right length made of zeros.
    """
    path = state_file(dashboard_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["schema"] = STATE_SCHEMA
    payload["dashboard_id"] = dashboard_id
    data = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    with atomic_write(path, "w") as fh:
        fh.write(data)
    return path


@contextlib.contextmanager
def atomic_write(path: Path, mode: str = "wb"):
    """Write a file atomically: `.tmp` next to it -> `fsync` -> `os.replace`.

    The only place where the invariant "an interrupted sync leaves no
    half-file" lives. Downloading a slot must go THROUGH this context and write
    to the stream it hands out as data is read from the network: then an
    interruption leaves a `.tmp`, not a file with the right name and the wrong
    length — and it is exactly such a file that looks whole both to us and to
    the report.

    The temporary file name carries the pid: two agent processes do not live at
    the same time (a successful port bind is the mutex, agent/config.py), but
    after a crash someone else's `.tmp` may be left, and there is no need to
    overwrite it on the fly.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    fh = open(tmp, mode, **({} if "b" in mode else {"encoding": "utf-8"}))
    try:
        yield fh
        fh.flush()
        os.fsync(fh.fileno())
    except BaseException:
        # Close and remove: on Windows an open file cannot be deleted, and
        # without close() garbage would stay here that even `sweep_tmp` would
        # not sweep.
        try:
            fh.close()
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        raise
    else:
        fh.close()
        os.replace(tmp, path)


def sweep_tmp(directory: Path) -> int:
    """Remove `*.tmp` left over from interrupted writes. Returns the count."""
    removed = 0
    try:
        entries = list(Path(directory).iterdir())
    except OSError:
        return 0
    for p in entries:
        if p.name.endswith(".tmp") and p.is_file():
            with contextlib.suppress(OSError):
                p.unlink()
                removed += 1
    return removed


# --------------------------------------------------------------------------- #
# Fetched rights                                                             #
# --------------------------------------------------------------------------- #

def rights_expired(state: dict, now_iso: str | None = None) -> bool:
    """Whether the fetched right has expired.

    Expiry blocks LOCAL UPDATE and does NOT block reading (the owner's
    decision, RFC §5: a person is entitled to read what is already downloaded
    even on the third day of downtime — otherwise the whole undertaking loses
    its point). That is why the function is named after the right, not after
    the mirror.

    A missing `expires_at` counts as expiry: a right without a term is a right
    that cannot be revoked, and by decision we have no such thing. The
    comparison is string-based: both ends are ISO-8601 UTC without a zone, in
    the same form in which the time comes from the server (`updated_at` in the
    manifest), and string comparison of such strings coincides with
    chronological. Parsing dates by hand here would mean building a second ISO
    implementation for a worse result.
    """
    rights = state.get("rights") if isinstance(state, dict) else None
    expires = rights.get("expires_at") if isinstance(rights, dict) else None
    if not isinstance(expires, str) or not expires:
        return True
    if now_iso is None:
        import datetime
        now_iso = datetime.datetime.now(datetime.UTC).replace(
            tzinfo=None).isoformat(timespec="seconds")
    return now_iso >= expires


def can_update_locally(state: dict, now_iso: str | None = None) -> bool:
    """Whether a LOCAL data update can be started right now.

    Three conditions, and all three must be checked here, not by the caller:
      * the right is fetched (`can_update_data`);
      * the right has not expired (there is nobody to revoke it offline — RFC §5);
      * the report is not under RLS. A local run of an RLS report is forbidden
        to everyone except `see_all` (the server does this in
        `_assert_can_update_data`), and there is no such report in the mirror
        at all (ADR-18) — so `rls_enabled: true` in state.json means a
        corrupted state, and the answer here is "no".
    """
    rights = state.get("rights") if isinstance(state, dict) else None
    if not isinstance(rights, dict):
        return False
    if rights.get("rls_enabled"):
        return False
    if not rights.get("can_update_data"):
        return False
    return not rights_expired(state, now_iso)


# --------------------------------------------------------------------------- #
# Local runs                                                                 #
# --------------------------------------------------------------------------- #

def next_local_n(state: dict) -> int:
    """The next local run number. Taken from `next_local`, not from
    max(local_runs): a deleted run does not give its number to a new one,
    otherwise the `local/L3` directory in the log would be two different runs."""
    n = state.get("next_local") if isinstance(state, dict) else None
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        return 1
    return n


def add_local_run(state: dict, created_at: str, based_on: int | None = None,
                  note: str = "") -> dict:
    """Register a local run in the state. Returns its record.

    `based_on` is the server version the run was counted from: without it one
    cannot answer "the server is back, is your run still current?", and that is
    exactly the question that will have to be put to the person (owner's
    decision: ask, do not publish silently).
    """
    k = next_local_n(state)
    record = {
        "k": k,
        "created_at": created_at,
        "based_on_server_version": based_on,
        "note": note or "",
        # Whether it has been published to the server. The catch-up queue reads
        # exactly this field.
        "published": False,
    }
    runs = state.setdefault("local_runs", [])
    if not isinstance(runs, list):
        runs = []
        state["local_runs"] = runs
    runs.append(record)
    state["next_local"] = k + 1
    return record


def local_runs(state: dict) -> list[dict]:
    """Local run records in number order. Foreign junk is discarded."""
    raw = state.get("local_runs") if isinstance(state, dict) else None
    out = [r for r in (raw or []) if isinstance(r, dict)
           and isinstance(r.get("k"), int) and not isinstance(r.get("k"), bool)]
    return sorted(out, key=lambda r: r["k"])


def local_run(state: dict, k: object) -> dict | None:
    """The record of one local run by number, or None."""
    for rec in local_runs(state):
        if rec["k"] == k:
            return rec
    return None


def unpublished_local_runs(state: dict) -> list[dict]:
    """Runs that are not on the platform yet — exactly the catch-up queue.

    There is one criterion (`published`), and it is deliberately not computed
    from dates: a run could have been made AFTER the last sync and still turn
    out to be published (the person opened the report page and pressed
    "publish"), and a comparison of times knows nothing about that.
    """
    return [r for r in local_runs(state) if not r.get("published")]


def mark_local_published(state: dict, k: object, at: str | None = None) -> dict | None:
    """Mark a run as published. Returns the changed record or None.

    The mark is set by whoever LEARNED of the publication (the page after a
    successful `publish`), not by whoever started it: otherwise the queue would
    be cleared of a run that never reached the platform.
    """
    rec = local_run(state, k)
    if rec is None:
        return None
    rec["published"] = True
    if at:
        rec["published_at"] = at
    return rec


def platform_status(state: dict) -> str:
    """What we KNOW about the copy's freshness relative to the platform.

    Three answers, and none of them is guessed:
      `fresh`    — in the last pass the manifest named exactly the version and
                   fingerprint that lie on disk;
      `outdated` — the manifest named something DIFFERENT, i.e. there is a newer
                   one on the platform and we did not manage to download it
                   (the connection dropped in the middle of the pass);
      `unknown`  — no manifest has been seen yet (not a single sync) or the
                   state was inherited from an earlier schema.

    The statement stays true after three days of downtime too: it is not "on
    the platform right now" but "as of the last contact". The library labels it
    that way — with a date next to it, otherwise "Up to date" would read as a
    promise for today.
    """
    seen = state.get("server_seen") if isinstance(state, dict) else None
    if not isinstance(seen, dict):
        return "unknown"
    version = seen.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return "unknown"
    if version != state.get("server_version"):
        return "outdated"
    # We compare the fingerprint only when it is known on both sides: it may be
    # absent from state written before the field appeared, and "don't know"
    # must not turn into "out of date".
    seen_fp, have_fp = seen.get("fingerprint"), state.get("fingerprint")
    if isinstance(seen_fp, str) and isinstance(have_fp, str) and seen_fp != have_fp:
        return "outdated"
    return "fresh"
