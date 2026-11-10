"""Local delivery of a report from the mirror over HTTP on 127.0.0.1.

Why this is real HTTP and not `file://` is an RFC decision
(`docs/RFC_DESKTOP_MIRROR.md`, §2), and repeating it briefly is a must,
because the temptation "we saved the html, clicked it, read it" returns at
every glance at the mirror folder. A parent page opened as `file:` cannot
`fetch` slot files — Chromium forbids it. And the report iframe lives in a
sandbox WITHOUT `allow-same-origin`, i.e. with an opaque origin, and cannot
request anything itself: that is exactly why data travels at all through the
`op_ready`→`op_file` bridge. An interactive report contains no data inside,
by construction. So a double click gives an empty report, and a server is
needed that serves the document and the slots at honest addresses. The
sandbox must not be touched in the process: the threat model rests on it (a
report is JS uploaded by a user, running while an administrator is looking at it).

This module only SERVES what already lies on disk. It does not go to the
network, does not read the device key (`agent/creds.py`) and does not know
about it: the mirror is already downloaded, there is nobody to ask for
permission, and code that holds no secret cannot write it to the log or
into an exception text. Injection (`detector.inject_bootstrap`) and the
snapshot extraction are done by the SERVER on delivering `/render/{id}/`, so
`dashboard.html` in the mirror already lies injected — it is not edited here
by a single byte.

What gets wired into `agent/server.py` (it is NOT changed by this module):

    from .mirror import serve as mirror_serve
    ...
    def do_GET(self):
        ...
        if mirror_serve.serve(self):
            return
        ...                      # then the agent's existing routes

`serve()` returns True if the address turned out to be a mirror address and the
response has already been sent; False means "not our address" — and then the agent goes its
own routes, rather than answering 404 in their place. The same call fits `do_HEAD`;
for other methods there is `handles()` — the mirror answers them 405, because
delivery from the mirror only reads (and this pairs with the decision "the device
key can only GET/HEAD", RFC §3).

Three pitfalls on which local delivery breaks SILENTLY. All three repeat the
server's behavior (`app/api/files.py::serve_slot_download`, `serve_slot_asset`,
`app/services/encoding.py`), because a divergence here gives a report that
works online and does not work offline, and the person learns about it at the
moment when there is nobody left to ask.

1. **Compressed files.** The mirror disk holds exactly what the server gave, and a slot
   may be pre-compressed (`*.bin.br`, `*.bin.gz` — that is how the SL report is built). Giving
   compressed bytes to a client that did not ask for compression means showing the report
   garbage instead of data, and without an error: `JSON.parse` will fail somewhere inside
   the report, and it will look like "the report is broken". So: the encoding is taken
   from the file NAME, checked against `Accept-Encoding`, and either goes out with an honest
   `Content-Encoding`, or is decompressed on the fly. This is the most likely error
   in this file.

   The limit of honesty: `.gz` is decompressed with the standard `zlib`, while `.br` is
   NOT DECOMPRESSED: brotli is not in the standard library, and pulling a package into
   `agent/` is forbidden (stdlib-only, see `agent/README.md`). To a client that does not
   accept `br` we answer 406 with a clear text, not "somehow". In
   practice this branch is unreachable — the mirror is read by a local Chromium/Edge,
   which always accepts `br` — but pretending the branch does not exist is not allowed.

2. **MIME.** `.js` MUST go out as `text/javascript`. The report document lives
   in a sandbox with an opaque origin, and Chromium ORB blocks a script loaded there
   with the type `application/octet-stream` — without a clear error in the console.
   The type table is a superset of the server's (`app/api/files.py::_MIME`): added are
   `.wasm` (without `application/wasm`, `instantiateStreaming` does not work), `.html`,
   `.map`, fonts and a few more that really occur among the report's
   assets. A superset is safe: on an unknown extension the server gives
   octet-stream, so something that did not work online may work offline,
   but not the other way round.

3. **Escaping the directory.** The asset name comes from the report itself, i.e.
   from untrusted HTML. We do NOT write our own check here: the name is matched against
   the slot inventory (`layout.resolve_asset`), and the path is built by
   `layout.slot_disk_path`, which also keeps it inside the version directory. This is
   structural protection: there is no code in this file that joins a path from a network
   string. The only exception is local runs, which have no slot inventory
   in the state.json schema yet (see `_slots_for`): there the comparison goes against
   the REAL names from `iterdir()`, so the requested string again
   does not become a path component.

Other decisions, each with its reason:

  * **`no-store` on everything.** The mirror is read from a local disk, the browser cache
    saves microseconds here and spoils the picture: the `/mirror/<id>/` address survives
    a resync while the content behind it changes. `If-None-Match` is
    supported anyway — the viewer may send a conditional request itself, and
    a 304 costs one `stat` instead of reading tens of megabytes. With `no-store` the browser
    almost never sends conditional requests, and that is fine: it is a cheap branch, not
    a saving mechanism.
  * **We compress nothing ourselves.** On loopback compression is pure CPU cost: the server
    compresses for the sake of a network that is not here.
  * **No CORS headers.** The document and slots are read as same-origin: the viewer
    sits on the same 127.0.0.1, and loading assets from the sandbox goes in no-cors
    mode. Echoing `Access-Control-Allow-Origin` for the platform's origin would give
    a platform page the right to read the mirror's bytes cross-domain, and no
    scenario needs that.
  * **Large files go as a stream in 1 MB chunks** (like `agent/server.py::_stream_file`):
    reports come in tens of megabytes, and there is no reason to lift them into memory.
  * **RLS is checked once more here.** The ban from ADR-18 is enforced by the
    synchronizer, and a report with a policy cannot be in the mirror. But "cannot
    be" is a poor basis for serving someone's filtered rows without the filter,
    so `rights.rls_enabled` in state.json closes delivery entirely.

Standard library only (agent/ — Python 3.12, stdlib).
"""
from __future__ import annotations

import json
import os
import re
import zlib
from pathlib import Path

from . import layout

# Chunk size when serving a file. 1 MB — like `agent/server.py::_stream_file`:
# smaller is pointless (a syscall per syscall), larger starts to noticeably hold memory with
# several parallel reads.
CHUNK = 1 << 20

# Ceiling on the DECOMPRESSED size. Parity with `app/services/encoding.py`
# (`_MAX_DECODE_BYTES`): we wrote the file on disk ourselves, but "wrote it
# ourselves" does not mean "not corrupted", and a zip bomb from a corrupted gzip looks exactly
# the same as from a malicious one.
MAX_DECODE_BYTES = 256 * 1024 * 1024

# Encodings that may lie on disk. The order repeats
# `layout._ASSET_VARIANTS` and the server's `serve_slot_asset`.
_DISK_ENCODINGS = {".gz": "gzip", ".br": "br"}

# Types that report assets really have. A superset of the server table
# (`app/api/files.py::_MIME`) — see pitfall 2 in the module docstring.
MIME_BY_EXT = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    # The main item of the table: ORB blocks a script with a wrong type inside a
    # sandbox with an opaque origin, and the report breaks with no error in the console.
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",            # source map next to the script
    ".wasm": "application/wasm",           # without this type instantiateStreaming refuses
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
}
DEFAULT_MIME = "application/octet-stream"

# The representation of the response is part of the ETag: the same file served compressed and
# decompressed is DIFFERENT bytes on the wire, and one ETag for both would be a lie.
_REP_IDENTITY = "id"
_REP_DECODED = "dec"

# What may stay in an ETag that came from the manifest: quotes and control
# characters would spoil the header.
_ETAG_SAFE_RE = re.compile(r"[^A-Za-z0-9._:/-]")


# --------------------------------------------------------------------------- #
# Pure helpers (convenient to call from tests without a socket)               #
# --------------------------------------------------------------------------- #

def handles(target: str) -> bool:
    """Whether this is our address. Needed so the agent can answer 405 to POST/DELETE at
    a mirror address rather than 404: a 404 on a right address reads as "no such report"
    and sends diagnostics sideways."""
    return layout.parse_local_path(target) is not None


def mime_for(name: str) -> str:
    """Type by extension. The name is already WITHOUT the compression suffix: the type is determined by
    `data.bin` in `data.bin.gz`, not by `.gz` itself (the server does the same —
    `media_filename = filename[:-3]`)."""
    ext = os.path.splitext(name or "")[1].lower()
    return MIME_BY_EXT.get(ext, DEFAULT_MIME)


def disk_encoding(name: str) -> tuple[str | None, str]:
    """`("gzip", "data.bin")` for `data.bin.gz`. Otherwise `(None, name)`.

    The encoding is determined by the NAME, not the content: that is how the server determines it
    when a slot is uploaded, and we must not diverge — otherwise the same file is served
    differently online and offline.
    """
    if not isinstance(name, str):
        return None, ""
    ext = os.path.splitext(name)[1].lower()
    enc = _DISK_ENCODINGS.get(ext)
    if enc is None:
        return None, name
    return enc, name[: -len(ext)]


def accepted_encodings(header: str | None) -> set[str]:
    """Encodings the client ACCEPTS (q > 0).

    A shortened copy of `app/services/encoding.py::accepted_encodings`, and the copy is
    deliberate: pulling the server module into `agent/` is not allowed (it drags in fastapi), and
    diverging in parsing `q=0` means giving `br` to someone who forbade it.
    `*` expands only to what can occur on the mirror disk at all.
    """
    accepted: set[str] = set()
    refused: set[str] = set()
    for part in (header or "").lower().split(","):
        token, _, params = part.partition(";")
        token = token.strip()
        if not token:
            continue
        q = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip() == "q":
                try:
                    q = float(value.strip())
                except ValueError:
                    q = 0.0      # garbage in q — treated as a ban, not a permission
                break
        (refused if q <= 0.0 else accepted).add(token)
    if "*" in accepted:
        accepted |= {"gzip", "br"} - refused
    return accepted


def pick_representation(enc: str | None, header: str | None) -> str:
    """What to answer with: `id` (file not compressed), `gzip`/`br` (serve as it lies) or
    `dec` (decompress). A separate function — because this is exactly the place where an
    error is invisible to the eye, and one wants to test it directly."""
    if enc is None:
        return _REP_IDENTITY
    return enc if enc in accepted_encodings(header) else _REP_DECODED


def etag_for(st: os.stat_result, rep: str, stored: object = None) -> str:
    """The response ETag.

    `stored` — the ETag from the manifest (`state.json` → slot → `etag`), if
    the synchronizer saved it: it is the server's identity of exactly these bytes, and
    it is more stable than the local `mtime` (copying the mirror folder changes mtime).
    The representation is always appended as a suffix: decompressed and compressed responses
    are different bytes.
    """
    inner = ""
    if isinstance(stored, str) and stored.strip():
        raw = stored.strip()
        if raw.startswith(("W/", "w/")):
            raw = raw[2:]
        inner = _ETAG_SAFE_RE.sub("", raw.strip('"'))
        # The server ETag itself has the form `"{fp}-{rep}"` (`storage.slot_fp` plus
        # the representation), and its `rep` is the server's: it says what the
        # server gave the SYNCHRONIZER, not what we serve. We cut it off, otherwise
        # we get `-br-gzip`, where the first half is misleading.
        for suffix in ("-id", "-dec", "-gzip", "-br", "-zstd", "-identity"):
            if inner.endswith(suffix):
                inner = inner[: -len(suffix)]
                break
    if not inner:
        inner = f"{st.st_mtime_ns:x}-{st.st_size:x}"
    return f'"{inner}-{rep}"'


def if_none_match(header: str | None, etag: str) -> bool:
    """Whether the ETag matched what the client sent. `*` is a match too (RFC 9110)."""
    if not header:
        return False
    for token in header.split(","):
        token = token.strip()
        if token == "*" or token == etag or token.lstrip("W/").strip() == etag:
            return True
    return False


# --------------------------------------------------------------------------- #
# Looking up the file for a parsed request                                    #
# --------------------------------------------------------------------------- #

class MirrorMiss(Exception):
    """Nothing to serve. `code` is the HTTP code, `reason` the machine reason,
    `message` the text for a human.

    The reason as a code, not a text, for the same reason as in `app/api/sync.py`:
    the text is the interface's business, the code is pinned by a test.
    """

    def __init__(self, code: int, reason: str, message: str):
        super().__init__(f"{code} {reason}: {message}")
        self.code = code
        self.reason = reason
        self.message = message


class Target:
    """Exactly what we serve: path on disk, type and encoding on disk.

    `logical_name` is the name from which MIME is computed (for an asset it is the name from the
    REQUEST, as on the server: `<script src="x.js">` must get the `.js` type,
    even if `x.js.br` lies on disk).
    """

    __slots__ = ("path", "logical_name", "enc", "etag_hint")

    def __init__(self, path: Path, logical_name: str, enc: str | None,
                 etag_hint: object = None):
        self.path = path
        self.logical_name = logical_name
        self.enc = enc
        self.etag_hint = etag_hint


def _version_dir(req: layout.MirrorRequest, state: dict) -> Path:
    """The version directory we serve from. The server version number is taken from
    state.json, NOT from the address: the mirror address deliberately has no version number —
    a resync must not change the link by which the person opened the report."""
    if req.space == layout.SPACE_LOCAL:
        n = req.n
    else:
        n = state.get("server_version")
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise MirrorMiss(404, "not_synced",
                             "report not synced yet — there is no mirror of the version")
    try:
        vdir = layout.dir_for(req.dashboard_id, req.space, n)
    except ValueError as exc:
        raise MirrorMiss(404, "bad_version", str(exc)) from None
    if not vdir.is_dir():
        raise MirrorMiss(404, "no_version_dir",
                         f"the version directory is not on disk: {vdir.name}")
    return vdir


def _slots_for(req: layout.MirrorRequest, state: dict) -> list:
    """The slot inventory for the requested version space.

    For a server version the inventory lies in `state["slots"]` (written by the synchronizer
    through `layout.plan_slot_files`). A LOCAL run has no inventory in the
    state.json schema at the moment — `local_runs` carries only `k`/`created_at`/
    `based_on_server_version`/`note`/`published`. So we read the optional
    `slots` field of the run record (when it appears, it will work with no edits here), and
    while it is absent we return an empty inventory: asset resolution then goes to
    `_lookup_on_disk`, while a slot by NUMBER has nothing to be served from, and that is an honest 404 with
    the reason `no_slot_index`, not guessing.
    """
    if req.space != layout.SPACE_LOCAL:
        slots = state.get("slots")
        return slots if isinstance(slots, list) else []
    for run in state.get("local_runs") or []:
        if isinstance(run, dict) and run.get("k") == req.n:
            slots = run.get("slots")
            return slots if isinstance(slots, list) else []
    return []


def _lookup_on_disk(vdir: Path, requested: str) -> Target | None:
    """Find a file by an EXACT name match among the directory's real entries
    (plus the `.gz`/`.br` variants).

    Needed for the document, the snapshot and for the assets of a local run, which has
    no slot inventory yet. The comparison goes against names from `iterdir()`, not
    by joining a path with the requested string: `..`, `a/b` and an NTFS stream simply
    match nothing, because there are no such entries in the directory.
    """
    if not isinstance(requested, str) or not requested:
        return None
    try:
        entries = {p.name: p for p in vdir.iterdir() if p.is_file()}
    except OSError:
        return None
    for suffix in ("", ".gz", ".br"):
        hit = entries.get(requested + suffix)
        if hit is not None:
            enc, _ = disk_encoding(hit.name)
            return Target(hit, requested, enc)
    return None


def resolve(req: layout.MirrorRequest, state: dict) -> Target:
    """Parsed request + state → what to serve. A miss is `MirrorMiss`."""
    # ADR-18, the second line of defence. The synchronizer does not download a report with
    # a policy at all; if one still ended up in the mirror, that is a corrupted state, and
    # not a reason to serve someone's filtered rows without the filter.
    rights = state.get("rights")
    if isinstance(rights, dict) and rights.get("rls_enabled"):
        raise MirrorMiss(403, "rls",
                         "a report with a row-level policy is not served from the mirror")

    vdir = _version_dir(req, state)

    if req.kind == layout.KIND_DOCUMENT:
        hit = _lookup_on_disk(vdir, layout.DOCUMENT_NAME)
        if hit is None:
            raise MirrorMiss(404, "no_document",
                             "the version directory has no report document")
        return hit

    if req.kind == layout.KIND_SNAPSHOT:
        # The snapshot may be extracted into a separate file (`OP_SNAPSHOT_EXTERNAL`), or
        # embedded in the document. A missing file is not a breakage but the second
        # configuration: a 404 here is a normal answer, the document does not ask for it.
        hit = _lookup_on_disk(vdir, layout.SNAPSHOT_NAME)
        if hit is None:
            raise MirrorMiss(404, "no_snapshot",
                             "the snapshot was not extracted into a separate file")
        return hit

    slots = _slots_for(req, state)

    if req.kind == layout.KIND_SLOT:
        record = layout.slot_by_number(slots, req.slot)
        if record is None:
            if not slots:
                raise MirrorMiss(404, "no_slot_index",
                                 "this version has no slot inventory — there is nothing to serve slot "
                                 f"{req.slot} by number from")
            raise MirrorMiss(404, "no_such_slot",
                             f"slot {req.slot} is not in this version's inventory")
        return _target_from_record(vdir, record, logical_name=None)

    if req.kind == layout.KIND_ASSET:
        record = layout.resolve_asset(slots, req.asset)
        if record is not None:
            return _target_from_record(vdir, record, logical_name=req.asset)
        if not slots:
            hit = _lookup_on_disk(vdir, req.asset)
            if hit is not None:
                return hit
        raise MirrorMiss(404, "no_such_asset",
                         f"no slot with the original name {req.asset!r}")

    raise MirrorMiss(404, "unknown_kind", f"unknown kind of request: {req.kind!r}")


def _target_from_record(vdir: Path, record: dict, logical_name: str | None) -> Target:
    """Slot record → path. The only point where the inventory turns into a path, and
    the check that it stays inside the version directory is done by `layout.slot_disk_path`, not by us."""
    try:
        path = layout.slot_disk_path(vdir, record)
    except ValueError as exc:
        # Such a state.json is corrupted: the slot name leads outside the version directory. This
        # deserves a reason code of its own — it will be seen in the log.
        raise MirrorMiss(404, "bad_slot_record", str(exc)) from None
    if not path.is_file():
        raise MirrorMiss(404, "slot_file_missing",
                         f"slot file not downloaded: {path.name}")
    # The LOGICAL NAME is taken from the original (`name`), not from the on-disk name:
    # `safe_slot_filename` may have renamed the file, but the report knows its own name.
    name = record.get("name") if isinstance(record.get("name"), str) else path.name
    enc, bare = disk_encoding(name)
    # But the ENCODING is not determined by the name, and that cost a separate finding.
    # The synchronizer puts on disk EXACTLY the bytes the server gave, and writes the
    # arrived `Content-Encoding` into `record["enc"]` (decision 1 in the docstring
    # of `sync`). The server, in turn, compresses a slot ON THE FLY if the client asks
    # (`app/api/files.py`: with `pre is None` → `encoding.encoded_response`), and the
    # synchronizer asks for `br, gzip`. So a slot named `data1.bin` may well
    # lie on disk compressed — and by the extension that is not visible at all. Deriving
    # the encoding from the name, delivery gave the report gzip disguised as `identity`:
    # not an error, not an empty screen, but SILENTLY WRONG NUMBERS, and precisely at the moment
    # when the server is down and there is nothing to check them against.
    # So the inventory is stronger than the name, in BOTH directions: if the field is there, it is
    # the truth about the bytes on disk (including `None` — "not compressed"). The name stays
    # the fallback for records without the field.
    if "enc" in record:
        rec_enc = record.get("enc")
        enc = rec_enc if isinstance(rec_enc, str) and rec_enc else None
    return Target(path, logical_name or bare, enc, etag_hint=record.get("etag"))


# --------------------------------------------------------------------------- #
# Response                                                                    #
# --------------------------------------------------------------------------- #

def serve(handler) -> bool:
    """Handle a mirror request. True — answered, False — not our address.

    Exceptions are not let out ON PURPOSE: failing here means taking down
    one `ThreadingHTTPServer` thread and leaving an open connection without an answer,
    i.e. hanging the report on "loading data". Any surprise is a 500 with
    a log entry.
    """
    req = layout.parse_local_path(handler.path)
    if req is None:
        return False

    if handler.command not in ("GET", "HEAD"):
        # Delivery from the mirror only reads — this pairs with the decision "the device key
        # can only GET/HEAD" (RFC §3).
        _json(handler, 405, {"error": "method_not_allowed"},
              extra={"Allow": "GET, HEAD"})
        return True

    if req.kind == layout.KIND_REDIRECT:
        # Without a trailing slash, relative assets and `snapshot.js?v=`
        # go one level up — the report opens empty. 307, not 301:
        # the mirror address survives a resync, and a permanent redirect
        # to a local address lives indecently long in the browser cache.
        handler.send_response(307)
        handler.send_header("Location", req.location)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True

    try:
        state = layout.read_state(req.dashboard_id)
        if not state:
            raise MirrorMiss(404, "no_state",
                             "no mirror for this report (or the state is unreadable)")
        target = resolve(req, state)
        _respond_file(handler, target)
    except MirrorMiss as miss:
        # We do not log `no_snapshot`: the absence of an extracted snapshot is
        # the report's second normal configuration, and a line for every open
        # would turn the log into noise in which a real miss cannot be found.
        if miss.reason != "no_snapshot":
            print(f"[mirror] {miss.code} {miss.reason}: {miss.message} ({req!r})")
        _json(handler, miss.code, {"error": miss.reason, "detail": miss.message})
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        # A person closed the tab in the middle of serving 40 MB — a normal event, not an
        # error. There is nobody left to answer, and writing about it to the log is pointless.
        pass
    except Exception as exc:                                     # noqa: BLE001
        print(f"[mirror] 500 {type(exc).__name__}: {exc} ({req!r})")
        _json(handler, 500, {"error": "mirror_failed"})
    return True


def _respond_file(handler, target: Target) -> None:
    """Serve a file: encoding, MIME, ETag, stream."""
    st = target.path.stat()
    rep = pick_representation(target.enc, handler.headers.get("Accept-Encoding"))

    if rep == _REP_DECODED and target.enc == "br":
        # The one honest hole: brotli is not in the standard library, and
        # external packages in `agent/` are forbidden. Serving compressed bytes to someone who
        # did not ask for them is not allowed — the report would get garbage and not say so.
        raise MirrorMiss(406, "br_not_accepted",
                         "the file is stored brotli-compressed, and the client does not accept it; "
                         "brotli cannot be decompressed with the standard library")

    etag = etag_for(st, rep, target.etag_hint)
    headers = {
        "ETag": etag,
        # The mirror is read locally: the browser cache saves microseconds here and
        # gets in the way — the address survives a resync, the content behind it does not.
        "Cache-Control": "no-store, must-revalidate",
        "Vary": "Accept-Encoding",
    }
    if if_none_match(handler.headers.get("If-None-Match"), etag):
        handler.send_response(304)
        for key, value in headers.items():
            handler.send_header(key, value)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return

    media_type = mime_for(target.logical_name)
    if rep in ("gzip", "br"):
        headers["Content-Encoding"] = rep
        _send_whole_file(handler, target.path, st, media_type, headers)
        return
    if rep == _REP_IDENTITY:
        _send_whole_file(handler, target.path, st, media_type, headers)
        return
    _send_decoded_gzip(handler, target.path, media_type, headers)


def _send_whole_file(handler, path: Path, st: os.stat_result,
                     media_type: str, headers: dict) -> None:
    """Serve a file as it lies: a known `Content-Length` + a stream in `CHUNK`s.

    There is deliberately NO Content-Disposition here: the slot is read by `fetch` from
    the viewer and put into `op_file`, while `attachment` on such a response is noise
    that in some browsers turns opening a report into downloading a file.
    """
    handler.send_response(200)
    handler.send_header("Content-Type", media_type)
    handler.send_header("Content-Length", str(st.st_size))
    for key, value in headers.items():
        handler.send_header(key, value)
    handler.end_headers()
    if handler.command == "HEAD":
        return
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(CHUNK)
            if not buf:
                break
            handler.wfile.write(buf)


def _send_decoded_gzip(handler, path: Path, media_type: str, headers: dict) -> None:
    """Decompress `.gz` on the fly and serve identity.

    The decompressed size is not known in advance (the ISIZE field at the end of gzip is
    the size modulo 2³², so for a file over 4 GB it lies, and one cannot rely
    on it), so the response goes as `Transfer-Encoding: chunked`, not with
    Content-Length. `wbits=47` is gzip/zlib auto-detection, as in
    `app/services/encoding.py::decode_bytes`.

    Exceeding the ceiling aborts the CONNECTION without the terminating zero chunk:
    the client must see a network error. A neatly closed stream would give
    truncated data that looks whole — exactly what this module avoids
    everywhere.
    """
    handler.send_response(200)
    handler.send_header("Content-Type", media_type)
    handler.send_header("Content-Encoding", "identity")
    handler.send_header("Transfer-Encoding", "chunked")
    for key, value in headers.items():
        handler.send_header(key, value)
    handler.end_headers()
    if handler.command == "HEAD":
        return
    dec = zlib.decompressobj(wbits=47)
    total = 0
    with open(path, "rb") as fh:
        while True:
            raw = fh.read(CHUNK)
            if not raw:
                break
            part = dec.decompress(raw, CHUNK * 8)
            while True:
                if part:
                    total += len(part)
                    if total > MAX_DECODE_BYTES:
                        handler.close_connection = True
                        print(f"[mirror] decompression of {path.name} went past "
                              f"{MAX_DECODE_BYTES} bytes — dropping the connection")
                        return
                    _write_chunk(handler, part)
                if not dec.unconsumed_tail:
                    break
                part = dec.decompress(dec.unconsumed_tail, CHUNK * 8)
    tail = dec.flush()
    if tail:
        _write_chunk(handler, tail)
    handler.wfile.write(b"0\r\n\r\n")
    handler.wfile.flush()


def _write_chunk(handler, data: bytes) -> None:
    handler.wfile.write(b"%X\r\n" % len(data))
    handler.wfile.write(data)
    handler.wfile.write(b"\r\n")


def _json(handler, status: int, obj: dict, extra: dict | None = None) -> None:
    """A short JSON response. Separate from `AgentHandler._json`, so that mirror
    delivery does not depend on the handler's private methods and does not send
    CORS headers (see the module docstring)."""
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    for key, value in (extra or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


# There is deliberately no mixin for the handler here. It existed (`serve_mirror()` →
# `serve(self)`), nobody wired it in, and in the single-file build it was also
# broken: the name `serve` there shares a namespace with the module `serve` (`from . import
# serve` in libui), and a bare call would land on the module, not the function. Dead code
# that no build check catches is worse than none: it looks
# ready. The mirror is wired in with one call, `mirror_serve.serve(self)`.
