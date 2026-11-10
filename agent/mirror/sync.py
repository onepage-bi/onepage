"""Mirror synchronizer: fetch what changed from the platform and store it on disk.

The only place in the agent that talks to the OnePage server. This is exactly
the price that RFC section 1 names out loud: the promise of `agent/README.md`
"does not go to the OnePage server" is cancelled here, while the other two
parts of it (we listen on loopback only, we accept nothing from the network)
remain in force.

What one pass of `Syncer.sync_all()` does:

  1. `GET /api/sync/manifest` with the device key in a header.
  2. For each report with `mirrorable: true`, compares the version AND the
     fingerprint with the local `state.json`. The fingerprint is mandatory:
     an edit of the theme, text, localstorage or an overwrite of a slot changes
     the document but does NOT move `current_version`, and `/render/{id}/` has
     neither an ETag nor a cache (`no-store`) -- such an edit cannot be caught
     with a conditional request (see the docstring of `app/api/sync.py`).
  3. Slots one by one, with `If-None-Match` on the saved ETag.
  4. `snapshot.js` -- if the manifest says `snapshot_external`.
  5. Puts the rights and their expiry into `state.json`.
  6. Reports with `mirrorable: false` are removed from the mirror and the
     reason is remembered.

SEVEN decisions, each of which would cost dearly if decided otherwise
=====================================================================

**1. What we ask for over the network and what we put on disk are two
different answers, and they differ for the document and for a slot.** By
default `urllib` sends no `Accept-Encoding` at all, so there is no silent
choice here: if you do not decide yourself, you get identity and pay the full
size on the channel that all this saving with 304 was started for.

  * **Document and snapshot -- we ask for `gzip`, and put the UNPACKED bytes on
    disk.** The channel is saved, and readable HTML lies on disk. The second
    matters more than it seems: the document is the only mirror file that the
    local server and the viewer may need to READ (post-injection, diagnostics,
    "why is it empty"), and a `.br` blob that nothing can unpack would become
    a trap for years. It is `gzip`, not `br`: the standard library has no
    brotli, `zstd` appeared only in 3.14, and we are promised 3.12 -- we can
    unpack exactly gzip, and asking for what we cannot unpack would be a lie.
  * **Slots -- we ask for `br, gzip`, and put EXACTLY WHAT THE SERVER SENT on
    disk**, and next to it, in the slot record, write `enc` -- the encoding
    the bytes came in. The reason is that slots on the server often already
    lie compressed (`*.br`, `*.gz`), and `serve_slot_download` passes them
    through to a client that accepts compression, and UNPACKS them itself for
    a client that does not. That is, if we asked for identity, the server
    would spend CPU to decompress the file, and we would spend the channel to
    download it decompressed, all for bytes that the browser would have
    accepted compressed anyway.

  Hence the obligation to the local server (`agent/mirror/serve.py`, written
  by another agent): a slot file on disk may be compressed, and the
  `Content-Encoding` when serving is taken from the `enc` field of the slot
  record in `state.json` -- NOT from the file name extension. An extension is
  not enough here: a raw `data.bin` could well have arrived compressed in br,
  and the name would say nothing about it. `enc = null` means identity. This
  field is our entire negotiation protocol, and it lives in the `slots` list
  because `layout.migrate_state` drops unknown top-level keys on read, while
  list elements are carried over as is.

**2. `Accept-Encoding` for slots is a CONSTANT, and this is part of 304
working, not style.** A slot's ETag on the server is computed as
`"{fp}-{rep}"`, where `rep` is the chosen representation, i.e. it depends on
what we asked for (`app/api/files.py::serve_slot_download`). If we went once
with `br, gzip` and another time with `gzip`, the saved ETag would stop
matching: every pass would download everything again, and nobody would notice
except the traffic counter.

**3. `If-None-Match` is sent only when the file IS on disk.** Otherwise the
server honestly answers 304 "yours is current", but we have no file -- and the
mirror would be left with an inventory entry for a report that is not there.
This is exactly the kind of breakage that is invisible until the very day the
program was written for.

**4. A redirect to another host is rejected.** `urllib` carries the request
headers over a redirect -- including `Authorization`. So a `302` from our
server to a foreign address would hand the device key to a third party. The
key is read only by GET (`app/auth/deps.py::_device_token_user` checks the
METHOD), but that is no reason to give it to anyone. So redirects are allowed
only within the same host.

**5. 401 is not a network error.** An expired or revoked key is not cured by
waiting and backoff: the person must be told "the device key is no longer
valid, issue a new one on the OnePage page". So a 401 interrupts the pass and
ends up as a separate field `key_rejected`, not as a line in the list of
errors on a par with a timeout.

**6. Stopping is COOPERATIVE, and is asked about inside a download, not only
between reports.** "Exit" from the icon sets an event and waits for the
thread for three seconds (`schedule.MirrorWorker.stop`), so checking only
between reports would mean the person waits for the end of a multi-megabyte
slot. `stop` is asked before each chunk -- by a wrapper around the response
body (`_StoppableReader`), NOT by a new parameter of `store.write_stream`: it
already has exactly the gap that was left for this ("anything with a `read(n)`
method"), and changing its signature for one caller means changing it for
everyone. Leaving from inside `layout.atomic_write` matters in itself: a stop
leaves no stump committed as a whole version, by the same mechanism as a
dropped connection (`ShortRead`). A stump written as a successful version was
once a real defect here -- the protection against it (the `Content-Length`
check) is not bypassed by a stop but reused.

**7. The result of a pass reaches the schedule as an EXCEPTION, not as a
field.** The module-level `sync_all()` (at the bottom of the file) is the entry
point for `schedule.py`, and it reads not `SyncResult` but the exception class
(`schedule.classify`): a normal return means to it "all fresh, the icon is
green". So a pass that is not `result.ok` must be raised -- otherwise a lost
connection would be shown to the person as "Copies updated", the very green
check mark over a mirror that has not synced for a week. `Syncer.sync_all()`
itself still raises nothing but `NoDeviceKey`: the tray needs counters, not
exceptions, and two contracts in one method is a sure way to break one of them.

The key does not get into the log. It lives in a request header, which is in
no `urllib` message (`HTTPError` carries the URL and the code, `URLError` the
reason), and `_scrub()` cleans it out of any text before the text goes
anywhere -- by the same discipline as `gpsource._safe_msg` for the GP password.

Standard library only: HTTP via `urllib.request`, no requests/httpx
(agent/README; the project's only exception -- psycopg -- has nothing to do
with the mirror).
"""
from __future__ import annotations

import contextlib
import errno
import gzip
import hashlib
import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

from .. import VERSION
from ..config import load_config
from ..creds import CredStore
from . import layout, store
from .store import MirrorFull

# The label under which the device key lies in the DPAPI store (agent/creds.py).
# Not a "connection password" but a separate name: GP passwords have their own
# entropy by connection name, and the platform key must not share a namespace
# with the logical connection names from agent.ini.
DEVICE_KEY_SLOT = "onepage-device-key"

MANIFEST_PATH = "/api/sync/manifest"

USER_AGENT = f"op-agent/{VERSION} (mirror)"

# Timeout of one request. Thirty seconds is not about speed but about the fact
# that a slot can be tens of megabytes and the channel is corporate; the
# timeout in urllib applies to each socket operation, not to the whole
# download, so a slow but live download does not fall under it.
DEFAULT_TIMEOUT_S = 30.0

# Mirror size ceiling. Four gigabytes is deliberately modest: the mirror is a
# working copy of what is available to the person, not an archive (RFC:
# "version history is not needed"). On exceeding -- an honest error and a
# refusal to download further.
DEFAULT_QUOTA_BYTES = 4 * 1024 * 1024 * 1024

# Backoff when the server is unavailable: 2 s, 4, 8, 16... with a ceiling. The
# ceiling is mandatory: the agent lives in autostart for months, and without
# it after a week of downtime the interval would go to a day, i.e. a returned
# server would have to be waited for another day.
BACKOFF_START_S = 2.0
BACKOFF_FACTOR = 2.0
BACKOFF_MAX_S = 300.0
MANIFEST_ATTEMPTS = 4

# What we ask the server for. See decisions 1 and 2 in the module docstring.
SLOT_ACCEPT_ENCODING = "br, gzip"
DOC_ACCEPT_ENCODING = "gzip"

# How many server versions we keep on disk. Exactly one: version history is not
# needed by the mirror by the owner's decision, and unswept versions eat the quota.
KEEP_VERSIONS = 1


# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #

class SyncError(Exception):
    """Root of the sync errors -- so the caller can catch them with one except."""


class KeyRejected(SyncError):
    """401: the device key is expired or revoked.

    A separate type, not 'one more network error': waiting and backoff do not
    cure it, and the person must be told exactly that -- issue a new key.
    """

    # `kind` is read by `schedule.classify`, and the attribute is STRONGER than
    # the type: "the synchronizer knows more about the cause than we do from
    # the class". Without it a dead key would be sorted by type -- i.e. as
    # "server unavailable", and the person would be told to wait instead of
    # issuing a new key.
    kind = "auth"


class TransportError(SyncError):
    """Network, timeout, unreadable response, 5xx. Cured by retry and backoff."""

    kind = "unavailable"


class NoDeviceKey(SyncError):
    """There is no device key in the store -- nothing to synchronize with."""

    # There is deliberately no `kind` of its own: "no key" is not a kind of
    # sync failure but a state that the schedule finds out BEFORE the call
    # (`M_NO_KEY`), reading the store itself. Inventing a branch for it in
    # `classify` would create a second place that decides the same question.


class SyncStopped(SyncError):
    """The pass is interrupted on request (`stop`): "Exit" from the icon.

    Neither a refusal nor a success: the unfinished part here is a consequence
    of the person's decision to close the program. A separate type is needed so
    that a stop does not fall into the per-report `except SyncError` and turn
    into `failed`, i.e. into a red icon for a pressed "Exit". It also has no
    state kind (`kind`) on purpose: the schedule sees its own `stop` set and
    does not reach `classify` (`MirrorWorker._run_once`).
    """


# --------------------------------------------------------------------------- #
# Result of a pass                                                            #
# --------------------------------------------------------------------------- #

@dataclass
class SyncResult:
    """What a pass ended with. Always counted, even if half failed.

    The point of separate counters is that "nothing updated" and "nothing
    succeeded" are different news, and merging them into one "done" means
    showing the person a green check mark over a mirror that has not synced
    for a week.
    """

    checked: int = 0            # how many reports came in the manifest
    updated: int = 0            # downloaded (document and/or slots)
    unchanged: int = 0          # compared, nothing to download
    removed: int = 0            # removed from the mirror (mirrorable: false)
    failed: int = 0             # failed -- one report does not drop the others
    bytes_downloaded: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    key_rejected: bool = False  # 401 -- the key is dead, a separate state
    full: bool = False          # hit the MIRROR CEILING (own quota)
    disk_full: bool = False     # the DISK ITSELF is out of space (ENOSPC/EDQUOT)
    manifest_error: str = ""    # no manifest at all: there was no sync
    stopped: bool = False       # interrupted by `stop` -- neither refusal nor success
    # How many bytes were missing. Needed for the line "need 1.2 GB more"
    # (`strings.json::sync_no_disk_space`): without a number a dash is printed.
    needed_bytes: int | None = None

    @property
    def ok(self) -> bool:
        """Whether everything went without losses. Used for `last_sync_at`."""
        return not (self.failed or self.key_rejected or self.full
                    or self.disk_full
                    or self.manifest_error or self.stopped)

    def note(self, dashboard_id: str, message: str) -> None:
        self.failed += 1
        self.errors.append((dashboard_id, message))


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    """UTC without a zone, with seconds -- the same form in which time comes from
    the server (`updated_at` in the manifest) and in which
    `layout.rights_expired` compares it as a string."""
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def _plus_seconds(iso: str, seconds: int) -> str:
    """ISO + N seconds, in the same format. A broken input is returned as is:
    it is better to leave a right's expiry suspicious than to invent one."""
    try:
        base = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds")


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect only within the same host.

    On a redirect `urllib` carries the headers of the original request over to
    the new address, including `Authorization`. So a single `302` to a foreign
    host and the device key has gone to a third party. The check is here, not
    at the caller: the caller is not obliged to know about this quirk of
    `urllib`.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        if urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            raise urllib.error.HTTPError(
                req.full_url, code,
                "redirect to another host rejected (we do not hand out the key)",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _StoppableReader:
    """A response body that asks "is it time to leave?" before each chunk.

    This is how a stop gets INSIDE a slot download without changing the
    signature of `store.write_stream` for other callers: it accepts "anything
    with a `read(n)` method" -- exactly the gap that was left for this. The
    exception flies out from inside `layout.atomic_write`, so the `.tmp` is
    removed and the previous whole file stays in place: a stopped pass leaves
    no half-file committed as a finished version.

    `read` returns exactly what the socket returned, including a short chunk:
    a normal download must not be cut by chunk length, this was decided in
    `store.write_stream` and is not revisited here.
    """

    def __init__(self, resp, stop_point, on_chunk=None):
        self._resp = resp
        self._stop_point = stop_point
        # A progress counter inside ONE file. Without it "copy 3 of 12" stands
        # exactly as long as a twenty-megabyte report downloads, and the person
        # reads a motionless number as a hung program -- exactly this complaint
        # is what led here. The gap is the same as for the stop: a wrapper over
        # the response body, no new parameters on `store.write_stream`.
        self._on_chunk = on_chunk

    def read(self, size: int = -1) -> bytes:
        self._stop_point()
        buf = self._resp.read(size)
        if buf and self._on_chunk is not None:
            self._on_chunk(len(buf))
        return buf


def _build_opener() -> urllib.request.OpenerDirector:
    """An opener without cookies and without trust in redirects to a foreign host.

    `build_opener` is deliberately called without `HTTPCookieProcessor`: the
    agent has no OnePage session and must not have one, its only mandate is the
    device key, which can only read.
    """
    return urllib.request.build_opener(_SameHostRedirect())


def default_base_url() -> str:
    """Platform address.

    Taken from the allowlist in `agent.ini` (`[security] allowed_origins`), the
    first element. This is not a config substitution: the list already
    contains exactly the OnePage origins the agent trusts, and creating a
    SECOND place where the platform address is written means eventually getting
    an agent that goes not where it is allowed. Without an ini the production
    default from `config._DEFAULT_ORIGINS` is used.
    """
    origins = load_config().allowed_origins
    return origins[0].rstrip("/") if origins else ""


class Backoff:
    """Exponential backoff with a ceiling. A separate object so that it can be
    checked without sleeping and without a network."""

    def __init__(self, start: float = BACKOFF_START_S,
                 factor: float = BACKOFF_FACTOR, cap: float = BACKOFF_MAX_S):
        self.start, self.factor, self.cap = start, factor, cap
        self._next = start

    def delay(self) -> float:
        """How long to wait before the next attempt; moves the state forward."""
        d = min(self._next, self.cap)
        self._next = min(self._next * self.factor, self.cap)
        return d

    def reset(self) -> None:
        self._next = self.start


# --------------------------------------------------------------------------- #
# Synchronizer                                                                #
# --------------------------------------------------------------------------- #

class Syncer:
    """One pass of the mirror sync.

    Everything external is taken as arguments (`opener`, `sleep`, `cred_store`,
    `now`), so that a pass can be checked without a network and without
    sleeping. This is not an abstraction "for the future": without it the only
    way to check backoff, 304 and the quota would be a live server, and it is
    exactly the one that is down in the scenario all this is written for.
    """

    def __init__(self, base_url: str | None = None, *,
                 cred_store: CredStore | None = None,
                 token: str | None = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 quota_bytes: int = DEFAULT_QUOTA_BYTES,
                 opener=None, sleep=None, now=None,
                 progress=None, stop=None):
        self.base_url = (base_url if base_url is not None
                         else default_base_url()).rstrip("/")
        self._creds = cred_store or CredStore()
        self._token = token
        self.timeout_s = timeout_s
        self.quota_bytes = quota_bytes
        self._opener = opener or _build_opener()
        self._now = now or _now_iso
        if sleep is None:
            import time as _time
            sleep = _time.sleep
        self._sleep = sleep
        # The progress callback and the stop event are FOREIGN objects (the
        # icon, the library page, the schedule thread). The first is called via
        # `_tick`, which does not let it bring down the pass; the second is only
        # polled (`is_set`), and knowledge about threading does not leak in
        # here -- anything with such a method will do, so a pass can be checked
        # without threads.
        self._progress_cb = progress
        self._stop = stop
        self._progress_warned = False
        # The budget is computed ONCE per pass by walking the directory and then
        # reduced by what was downloaded. A walk per file would cost hundreds of
        # stat calls per slot, and a counter in state.json would lie after an
        # interruption -- and lie downward, i.e. the quota would stop holding.
        self._used = 0
        # How many bytes were downloaded in the pass. Needed separately from
        # `_used`: `_used` is reduced by swept old data, and it cannot tell the
        # person "this much downloaded" -- and this is the number they compare
        # with their data plan.
        self._downloaded = 0
        # Where we are now ("copy N of M") and how many bytes were downloaded
        # inside the current file. The first is needed so that a tick from the
        # middle of a download knows the same pair as a tick between reports;
        # the second so that the number grows without waiting for the end of the
        # file. `_live` is reset at the start of each download: an interrupted
        # file is removed from disk, and counting it as downloaded would be
        # untrue.
        self._pos = (0, 0)
        self._live = 0
        self._live_ticked = 0

    # ----------------------------------------------------------------- key --
    def token(self) -> str:
        """The device key from the DPAPI store. No key -- `NoDeviceKey`.

        Read on first access and kept in memory for the duration of the pass:
        it goes into the log neither here nor anywhere else, and re-reading
        DPAPI for each of a hundred requests is pointless.
        """
        if self._token:
            return self._token
        try:
            value = self._creds.get_password(DEVICE_KEY_SLOT)
        except Exception as exc:          # noqa: BLE001 -- CredError and ctypes failures
            raise NoDeviceKey(
                f"could not read the device key: {type(exc).__name__}"
            ) from None
        if not value:
            raise NoDeviceKey("the device key is not saved -- issue it on the "
                              "OnePage page and enter it in the program")
        self._token = value
        return value

    def _scrub(self, text: str) -> str:
        """Scrub the key from any text before the log or an exception.

        A second line of defence: `urllib` does not put the key into messages
        (their source is the URL and the reason), but one cannot rely on this --
        the messages are formed not by our code, and one foreign handler that
        prints headers would cost us the key in a log file that the person then
        sends to support.
        """
        out = " ".join((text or "").split())
        if self._token:
            out = out.replace(self._token, "***")
        return out

    # -------------------------------------------------- stop and pass progress --
    def stopping(self) -> bool:
        """Whether a stop was requested."""
        return self._stop is not None and self._stop.is_set()

    def _stop_point(self, what: str) -> None:
        """Exit point. It stands only where interrupting is safe: between
        reports and before the next chunk of a download -- i.e. inside
        `layout.atomic_write`, which will remove an unfinished `.tmp`. After
        `store.save_state` there are no such points: the written inventory IS
        the finished version, and throwing the pass between it and
        `prune_versions` would leave an extra version (garbage that will be
        swept) instead of a whole one."""
        if self.stopping():
            raise SyncStopped(f"pass stopped ({what})")

    def _tick(self, done: int, total: int) -> None:
        """Report progress: "copy N of M" in the icon and on the library page.

        The callback is foreign code, and its exception has no right to bring
        down the sync: the mirror matters more than the counter. Staying silent
        about it is not allowed either -- a frozen counter will be read by the
        person as a hung sync, and without a log line the cause cannot be found.
        We write once per pass: a broken callback breaks on every report, and
        fifty identical log lines are not diagnostics.
        """
        self._pos = (done, total)
        if self._progress_cb is None:
            return
        try:
            self._progress_cb(done, total, self._downloaded + self._live)
        except Exception as exc:          # noqa: BLE001 -- foreign code
            if not self._progress_warned:
                self._progress_warned = True
                print(f"[mirror] progress counter not updating: "
                      f"{type(exc).__name__}: {self._scrub(str(exc))}")

    _LIVE_TICK_BYTES = 1 << 19          # half a megabyte: no more often than the eye sees

    def _chunk(self, n: int) -> None:
        """A chunk of the response body was read. We report progress not on every
        chunk (that is thousands of calls per file) but once per half megabyte:
        the counter must move visibly, not expensively."""
        self._live += n
        if self._live - self._live_ticked >= self._LIVE_TICK_BYTES:
            self._live_ticked = self._live
            self._tick(*self._pos)

    def _live_reset(self) -> None:
        self._live = 0
        self._live_ticked = 0

    def _pause(self, delay: float) -> None:
        """A pause between attempts that "Exit" interrupts.

        If the stop event can `wait` (a `threading.Event` can) -- we wait ON
        IT: otherwise a person closing the program would wait for the end of
        the backoff, and that is up to five minutes. Without an event the
        injected `sleep` remains -- checks substitute it so as not to sleep at
        all.
        """
        waiter = getattr(self._stop, "wait", None)
        if waiter is not None:
            if waiter(delay):
                raise SyncStopped("pass stopped (pause between attempts)")
            return
        self._sleep(delay)

    # ----------------------------------------------------------------- HTTP --
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _open(self, url: str, accept_encoding: str,
              etag: str | None = None):
        """GET with the key in a header. Returns an open response (the caller
        closes it) or a 304 response -- that is also a result, not an error.

        `urllib` treats everything but 2xx as an exception, so a 304 has to be
        fished out of `HTTPError`. This is no bypass of the contract:
        `HTTPError` has the code, headers and body -- it IS the response.
        """
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", "Bearer " + self.token())
        req.add_header("Accept-Encoding", accept_encoding)
        req.add_header("User-Agent", USER_AGENT)
        if etag:
            req.add_header("If-None-Match", etag)
        try:
            return self._opener.open(req, timeout=self.timeout_s)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return exc
            exc.close()
            if exc.code == 401:
                raise KeyRejected(
                    "the device key is no longer valid (expired or "
                    "revoked) -- issue a new one on the OnePage page") from None
            if exc.code == 403:
                raise TransportError(
                    "the server refused access to the report (403)") from None
            raise TransportError(
                self._scrub(f"the server answered HTTP {exc.code}")) from None
        except (urllib.error.URLError, http.client.HTTPException,
                OSError) as exc:
            # DNS, connection refused, timeout and a TLS drop land here. We pass
            # the exception text on sanitized: without it all failures look the
            # same, and there is nothing to tell the person for a diagnosis.
            raise TransportError(self._scrub(
                f"{type(exc).__name__}: {exc}")) from None

    def _read_text(self, url: str) -> dict:
        """Read a JSON response. We ask for gzip and unpack ourselves."""
        resp = self._open(url, DOC_ACCEPT_ENCODING)
        try:
            raw = resp.read()
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
        except (OSError, EOFError, http.client.HTTPException) as exc:
            raise TransportError(self._scrub(
                f"server response not fully read: {type(exc).__name__}")) from None
        finally:
            resp.close()
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TransportError(
                f"server response not parsed as JSON: {type(exc).__name__}"
            ) from None
        if not isinstance(obj, dict):
            raise TransportError("server response does not look like a manifest")
        return obj

    def _read_body(self, resp, what: str) -> bytes:
        """Read the whole body, but in CHUNKS, asking about a stop.

        A single `resp.read()` cannot be interrupted: "Exit" would wait for the
        end of a multi-megabyte document download. We still assemble everything
        whole -- unpacked gzip cannot be written otherwise, and a truncated HTML
        would look whole (see `_fetch_document`), so it is the response that is
        read in chunks, not the file that is written.

        The comparison with `Content-Length` is MANDATORY here, and this is not
        over-caution: `resp.read()` without an argument on an under-read
        response raises `IncompleteRead`, while `resp.read(n)` does NOT, it
        silently closes the connection (`http.client`: "ideally we would raise
        IncompleteRead, but it might break compatibility"). That is, reading in
        chunks would by itself remove the very protection for which a drop is
        caught at all, and a truncated document would land in the mirror as a
        whole one. We compare the length before unpacking: with
        `Content-Encoding: gzip` it is the compressed length that is promised.
        """
        promised = resp.headers.get("Content-Length")
        try:
            expected = int(promised) if promised is not None else None
        except (TypeError, ValueError):
            expected = None
        parts: list[bytes] = []
        size = 0
        self._live_reset()
        while True:
            self._stop_point(what)
            buf = resp.read(store.CHUNK)
            if not buf:
                break
            parts.append(buf)
            size += len(buf)
            self._chunk(len(buf))
        if expected is not None and size != expected:
            raise TransportError(
                f"{what} not fully read: received {size} B of the promised "
                f"{expected} B")
        return b"".join(parts)

    # ------------------------------------------------------------ manifest --
    def fetch_manifest(self, attempts: int = MANIFEST_ATTEMPTS) -> dict:
        """The manifest, with exponential backoff when the server is unavailable.

        Backoff is applied ONLY to transport failures. Retrying a 401 or a
        missing key is pointless: the server answered clearly, and the answer
        will not change because we wait five minutes.
        """
        backoff = Backoff()
        last = ""
        for attempt in range(1, max(attempts, 1) + 1):
            self._stop_point("before the manifest request")
            try:
                return self._read_text(self._url(MANIFEST_PATH))
            except TransportError as exc:
                last = str(exc)
                if attempt >= attempts:
                    break
                delay = backoff.delay()
                print(f"[mirror] manifest not received ({last}); "
                      f"retry in {delay:.0f} s")
                self._pause(delay)
        raise TransportError(last or "manifest not received")

    # ------------------------------------------------------------ one pass --
    def sync_all(self) -> SyncResult:
        """One full pass. Raises no exceptions except `NoDeviceKey`.

        There is one exception and it is deliberate: without a key there is
        nothing to synchronize with, and counting it as "a result with zeros"
        would be a lie. Everything else -- including a dead key and a hit quota
        -- arrives as result fields, because the caller (tray, schedule) must
        show the person a state, not catch an exception.
        """
        result = SyncResult()
        store.sweep()
        try:
            manifest = self.fetch_manifest()
        except SyncStopped as exc:
            # Stopped before starting: this is not "the manifest failed" (that
            # string means a platform refusal), so we do not occupy the field.
            result.stopped = True
            result.errors.append(("", str(exc)))
            return result
        except KeyRejected as exc:
            result.key_rejected = True
            result.manifest_error = str(exc)
            return result
        except TransportError as exc:
            result.manifest_error = str(exc)
            return result

        items = manifest.get("items")
        # Non-dicts are thrown out HERE, not in the loop: otherwise the "M" in
        # the "copy N of M" counter would promise work that does not exist, and
        # the counter would never reach M.
        items = items if isinstance(items, list) else []
        items = [i for i in items if isinstance(i, dict)]
        ttl = manifest.get("rights_ttl_s")
        ttl = ttl if isinstance(ttl, int) and ttl > 0 else layout.DEFAULT_RIGHTS_TTL_S
        snapshot_external = bool(manifest.get("snapshot_external"))

        self._used = store.usage_bytes()

        # "Copy N of M": M is how many reports the manifest NAMED, including
        # forbidden ones (removing them from the mirror is also work of this
        # pass, and a gap in the counter would read as a hang). We report the
        # zero step at once: otherwise the icon would show "0 of 0" until the
        # end of the first report.
        total = len(items)
        done = 0
        self._tick(done, total)
        for item in items:
            dashboard_id = item.get("id")
            try:
                if not layout.is_valid_dashboard_id(dashboard_id):
                    # Skipping silently is not allowed: this is a contract
                    # mismatch, and it means a report the person will not see
                    # on their machine without explanation.
                    result.note(str(dashboard_id), "invalid report id in the manifest")
                    continue
                result.checked += 1
                self._stop_point("between reports")
                self._sync_item(item, result, ttl=ttl,
                                snapshot_external=snapshot_external)
            except SyncStopped as exc:
                # This branch comes BEFORE the general one: a stop is not "an
                # error of one report", and `result.note` would show the person
                # a failure where they themselves pressed "Exit".
                result.stopped = True
                result.errors.append((dashboard_id, str(exc)))
                break
            except KeyRejected as exc:
                # The key died in the middle of the pass -- no point going on,
                # every next request will get the same 401.
                result.key_rejected = True
                result.errors.append((dashboard_id, str(exc)))
                break
            except MirrorFull as exc:
                result.full = True
                result.needed_bytes = getattr(exc, "needed_bytes", None)
                result.errors.append((dashboard_id, str(exc)))
                break
            except (SyncError, OSError, ValueError,
                    http.client.HTTPException, store.ShortRead) as exc:
                if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC,
                                                              errno.EDQUOT):
                    # The disk ran out -- this is not a failure of one report: the
                    # next write will fail the same way. The field is SEPARATE from
                    # our own ceiling: here the person must free space, while at
                    # our own ceiling freeing space does not help at all -- the
                    # quota must be raised or reports removed from the mirror.
                    # Merging them into one would give wrong advice in half the
                    # cases.
                    result.disk_full = True
                    result.errors.append((dashboard_id, self._scrub(str(exc))))
                    break
                # One unavailable report does not zero the others: this is the
                # main requirement for a pass.
                result.note(dashboard_id, self._scrub(str(exc)) or
                            type(exc).__name__)
            finally:
                # We count PASSED reports, not successful ones: the counter shows
                # progress, not the outcome, and freezing on a failure it would
                # lie about a hang.
                done += 1
                self._tick(done, total)
        result.bytes_downloaded = self._downloaded
        return result

    # ----------------------------------------------------------- one report --
    def _sync_item(self, item: dict, result: SyncResult, *, ttl: int,
                   snapshot_external: bool) -> None:
        dashboard_id = item["id"]
        title = item.get("title") if isinstance(item.get("title"), str) else ""

        # --- the ADR-18 ban is enforced FIRST ------------------------------
        # A report with `mirrorable: false` (in particular RLS) is never
        # downloaded and, if it once lay in the mirror, is DELETED. It is
        # important that this is not "skip": the policy may have been turned on
        # yesterday, and a copy with yesterday's unfiltered rows lies on the
        # person's disk -- exactly the leak that ADR-18 forbids. The reason is
        # remembered separately (`store.mark_blocked`), so that the library shows
        # "not downloaded: RLS" instead of a silent disappearance.
        if not item.get("mirrorable"):
            reason = item.get("skip_reason")
            reason = reason if isinstance(reason, str) else ""
            freed = store.mark_blocked(dashboard_id, title, reason, self._now())
            self._used = max(self._used - freed, 0)
            result.removed += 1
            return

        store.unmark_blocked(dashboard_id)

        version = item.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise SyncError("the manifest did not name the report's version number")
        fingerprint = item.get("fingerprint")
        fingerprint = fingerprint if isinstance(fingerprint, str) else None

        state = store.load_state(dashboard_id)
        if not state:
            state = store.fresh_state(dashboard_id, title=title,
                                      dash_type=item.get("type") or "")
        state["title"] = title or state.get("title") or ""
        if isinstance(item.get("type"), str):
            state["dash_type"] = item["type"]
        # The platform folder. The report MAY have been taken out of the folder
        # -- then the manifest sends `null`, and the previous name must be
        # erased: a section that no longer exists on the platform would lie to
        # the person about where the report lives.
        folder = item.get("folder")
        name = folder.get("name") if isinstance(folder, dict) else None
        state["folder"] = name if isinstance(name, str) else ""
        # The weight of the rendered document -- for the tiles: a live frame of
        # a heavy report in the preview costs seconds and megabytes, and the
        # platform does not show such reports as a frame at all. The number is
        # given by the manifest, we cannot compute it ourselves: a compressed
        # copy lies on disk, and the unpacked one would go into the frame.
        size = item.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            state["size"] = size


        # What the manifest NAMED -- we record BEFORE downloading and, if it is
        # new, straight to disk. Otherwise an interruption in the middle of a
        # pass leaves a state indistinguishable from "all downloaded": the other
        # fields are written only after a successful download, and the library
        # honestly could not say "a newer one is on the platform" (the
        # `outdated` label hung dead). An extra write of a small json is the
        # price of one honest answer to this program's main question.
        prev_seen = state.get("server_seen")
        prev_seen = prev_seen if isinstance(prev_seen, dict) else {}
        state["server_seen"] = {"version": version, "fingerprint": fingerprint,
                                "at": self._now()}
        if (prev_seen.get("version"), prev_seen.get("fingerprint")) != (
                version, fingerprint):
            store.save_state(dashboard_id, state)

        vdir = store.ensure_version_dir(dashboard_id, version)

        # The document is re-downloaded if the version number OR the fingerprint
        # moved. The fingerprint here is not over-caution: an edit of the
        # theme/text/slot changes the document without moving the version, and a
        # conditional request cannot catch it -- `/render/{id}/` is served
        # `no-store` and without an ETag.
        doc_path = vdir / layout.DOCUMENT_NAME
        need_doc = (state.get("server_version") != version
                    or state.get("fingerprint") != fingerprint
                    or not doc_path.is_file())

        # Slots: the plan of names is taken from layout (original name -> name
        # on disk), and the ETag and `enc` are carried over from the previous
        # state by slot number -- otherwise every pass would download everything
        # again.
        planned = layout.plan_slot_files(item.get("slots"))
        previous = {s.get("slot"): s for s in state.get("slots") or []
                    if isinstance(s, dict)}
        downloaded_any = False

        for record in planned:
            self._stop_point(f"before slot of report {dashboard_id}")
            old = previous.get(record["slot"])
            path = layout.slot_disk_path(vdir, record)
            exists = path.is_file()
            if old and exists and old.get("v") and old.get("v") == record.get("v"):
                # `v` is mtime+size on the server: it matched, so the file is the
                # same. A zero request is cheaper than a 304, so we do not even ask.
                record["etag"] = old.get("etag")
                record["enc"] = old.get("enc")
                continue
            # We send If-None-Match ONLY when the file is on disk. Otherwise the
            # server honestly answers 304, but we would have no file -- and the
            # inventory would describe something nonexistent.
            etag = old.get("etag") if (old and exists) else None
            got = self._fetch_slot(dashboard_id, record, path, etag=etag)
            if got:
                downloaded_any = True

        if need_doc:
            self._stop_point(f"before document of report {dashboard_id}")
            self._fetch_document(dashboard_id, doc_path)
            downloaded_any = True
            if snapshot_external:
                # Forgetting the snapshot means showing an empty report: with
                # `OP_SNAPSHOT_EXTERNAL` on, the document refers to `snapshot.js`
                # and has no data inside.
                self._fetch_snapshot(dashboard_id, vdir / layout.SNAPSHOT_NAME)

        # The generator bundle comes AFTER the report and only with the right to
        # update: without the right a local run will not start anyway, and
        # keeping megabytes on disk for a button the person does not have is
        # pointless.
        if bool(item.get("can_update_data")):
            try:
                self._cache_bundle(dashboard_id, item.get("generator"), state)
            except MirrorFull:
                raise
            except (SyncError, OSError, ValueError, store.ShortRead,
                    http.client.HTTPException) as exc:
                # The report is already downloaded and readable -- that is the
                # main thing. The bundle is needed only for a local run, so its
                # failure does not cancel the copy, but does not stay silent
                # either: without it "Update from folders" simply will not
                # appear, and the person will not understand why.
                result.note(dashboard_id,
                            "generator bundle not cached: "
                            + (self._scrub(str(exc)) or type(exc).__name__))

        state["slots"] = planned
        state["server_version"] = version
        state["fingerprint"] = fingerprint
        state["updated_at"] = (item.get("updated_at")
                               if isinstance(item.get("updated_at"), str) else None)
        state["snapshot_external"] = snapshot_external

        # The rights and their expiry. The expiry is counted from the MOMENT OF
        # RECEIPT, not from the server `updated_at`: expiry must mean "the right
        # was not rechecked for a day", not "the report was not edited for a day".
        fetched = self._now()
        rights = state.get("rights")
        rights = rights if isinstance(rights, dict) else {}
        rights.update({
            "can_update_data": bool(item.get("can_update_data")),
            # An RLS report cannot be in the mirror (return above), so here this
            # is always false; the field is written honestly rather than wiped
            # with a zero, so that a corrupted state is visible, not masked.
            "rls_enabled": bool(item.get("rls_enabled")),
            "fetched_at": fetched,
            "expires_at": _plus_seconds(fetched, ttl),
            "ttl_s": ttl,
        })
        state["rights"] = rights
        state["last_sync_at"] = fetched

        store.save_state(dashboard_id, state)
        # Old versions are swept AFTER writing the inventory: if we crash between
        # writing the files and writing state.json, an extra version stays on disk
        # -- garbage that will be swept next time. The reverse (tearing down the
        # old version without writing the new inventory) means being left without
        # a report.
        freed = store.prune_versions(dashboard_id, version)
        self._used = max(self._used - freed, 0)

        if downloaded_any:
            result.updated += 1
        else:
            result.unchanged += 1

    # ----------------------------------------------------------- downloads --
    def _budget(self) -> int:
        """How many bytes can still be written before hitting the ceiling."""
        return max(self.quota_bytes - self._used, 0)

    def _check_room(self, expected: int | None, what: str) -> None:
        """Refuse BEFORE downloading if the promised size no longer fits.

        `expected` is the server's promise (`size` from the manifest,
        `Content-Length`). We do not believe it as a fact (`write_stream` catches
        that by what was actually written), but if even the promised amount does
        not fit, there is no point downloading -- an honest error is cheaper than
        thirty megabytes into nowhere.
        """
        if expected and expected > self._budget():
            exc = MirrorFull(
                f"the mirror is full: {what} does not fit "
                f"({expected} B with {self._budget()} B left). "
                f"Free up space or raise the ceiling.")
            # How much was missing is the only number the person can use:
            # "need this much more".
            exc.needed_bytes = expected - self._budget()
            raise exc

    def _fetch_slot(self, dashboard_id: str, record: dict, path,
                    etag: str | None) -> bool:
        """Download one slot. `True` -- downloaded, `False` -- the server said 304.

        EXACTLY the bytes the server sent are put on disk, and the encoding is
        written to `record["enc"]` -- see decision 1 in the module docstring. So
        there is neither unpacking nor repacking here: a slot may already be
        compressed on the server, and the only way not to lie is to save it as it
        came and honestly record how it came.
        """
        self._check_room(record.get("size"), f"slot {record['slot']}")
        url = self._url(f"/render/{quote(dashboard_id)}/files/{record['slot']}")
        v = record.get("v")
        if isinstance(v, str) and v:
            # `?v=` matched -- the server will serve `immutable`. We do not need
            # the browser cache, but there is no reason to hinder it, and above
            # all this makes the address match the one the page itself requests.
            url = f"{url}?v={quote(v)}"
        resp = self._open(url, SLOT_ACCEPT_ENCODING, etag=etag)
        try:
            if getattr(resp, "code", 200) == 304:
                record["etag"] = etag
                return False
            # The promised length is taken from `Content-Length`, not from the
            # manifest `size`: with `Content-Encoding: gzip` the compressed length
            # goes over the wire, and the file size on the server does not match.
            promised = resp.headers.get("Content-Length")
            try:
                expected = int(promised) if promised is not None else None
            except (TypeError, ValueError):
                expected = None
            try:
                # The stream goes through a wrapper: a stop is asked before each
                # chunk and flies out from INSIDE `atomic_write`, so neither a
                # `.tmp` nor a file with the right name and the wrong length is
                # left on disk. The check against `expected` (`Content-Length`)
                # stays in place -- a stop does not bypass it, it just never
                # reaches it.
                self._live_reset()
                written = store.write_stream(
                    path,
                    _StoppableReader(resp, lambda: self._stop_point(
                        f"slot {record['slot']} of report {dashboard_id}"),
                        on_chunk=self._chunk),
                    budget=self._budget(), expected=expected)
            except store.ShortRead as exc:
                # A drop is cured by a retry, so this is a transport error, not
                # corruption: nothing is left on disk, the previous version is whole.
                raise TransportError(str(exc)) from exc
            self._used += written
            self._downloaded += written
            self._live_reset()
            record["etag"] = resp.headers.get("ETag")
            enc = (resp.headers.get("Content-Encoding") or "").strip().lower()
            # The server sets `identity` explicitly (`encoding.encoded_response`),
            # and storing it as a string would force the serving side to parse two
            # ways of saying "not compressed". We normalize to None.
            record["enc"] = enc if enc and enc != "identity" else None
            record["bytes"] = written
        finally:
            resp.close()
        return True

    def _fetch_document(self, dashboard_id: str, path) -> None:
        """Download the report document and put it in UNPACKED.

        We ask for gzip (the only encoding the standard library unpacks), unpack
        ourselves and write readable HTML: this is the one mirror file that the
        local server and the person may need to read with their eyes.

        The document is read into memory whole, not streamed, because unpacking
        a gzip stream in chunks with the standard library is possible, but the
        result still has to be assembled before writing -- otherwise on a drop
        we would get a seemingly valid truncated HTML, exactly the "half-file
        that looks whole" that is forbidden.
        """
        resp = self._open(self._url(f"/render/{quote(dashboard_id)}/"),
                          DOC_ACCEPT_ENCODING)
        try:
            raw = self._read_body(resp, f"document of report {dashboard_id}")
            enc = (resp.headers.get("Content-Encoding") or "").strip().lower()
        finally:
            resp.close()
        if enc == "gzip":
            try:
                raw = gzip.decompress(raw)
            # http.client.IncompleteRead is an HTTPException, and without it a
            # cut-off response flew out past the per-report handling.
            except (OSError, EOFError, http.client.HTTPException) as exc:
                raise TransportError(
                    f"document not unpacked: {type(exc).__name__}") from None
        elif enc and enc != "identity":
            # This should not happen: we asked for gzip only. But if the server
            # ever answers br to a request without br, an honest refusal is better
            # than a compressed blob written under the name .html.
            raise TransportError(
                f"the server sent the document in encoding {enc}, which we did not ask for")
        self._check_room(len(raw), "document")
        if len(raw) > self._budget():
            raise MirrorFull("the mirror is full: the document does not fit")
        written = store.write_bytes(path, raw)
        self._used += written
        self._downloaded += written
        self._live_reset()

    def _fetch_snapshot(self, dashboard_id: str, path) -> None:
        """Download the externalized `snapshot.js`.

        Without `?v=`: its value is `sha1(repr(fingerprint tuple))`
        (`app/api/files.py::snapshot_fp12`), while in the manifest the
        fingerprint arrives already as a hyphenated string, and the very `repr`
        cannot be restored from it. The server handles a `v` mismatch itself --
        it serves the current data without caching (`no-store`), which is
        exactly what we need. Inventing a `v` for the sake of `immutable` would
        be worse: get the format wrong and you get someone else's data from the
        cache.
        """
        resp = self._open(self._url(f"/render/{quote(dashboard_id)}/snapshot.js"),
                          DOC_ACCEPT_ENCODING)
        try:
            raw = self._read_body(resp, f"snapshot of report {dashboard_id}")
            enc = (resp.headers.get("Content-Encoding") or "").strip().lower()
        finally:
            resp.close()
        if enc == "gzip":
            try:
                raw = gzip.decompress(raw)
            # http.client.IncompleteRead is an HTTPException, and without it a
            # cut-off response flew out past the per-report handling.
            except (OSError, EOFError, http.client.HTTPException) as exc:
                raise TransportError(
                    f"snapshot not unpacked: {type(exc).__name__}") from None
        elif enc and enc != "identity":
            raise TransportError(
                f"the server sent the snapshot in encoding {enc}, which we did not ask for")
        if len(raw) > self._budget():
            raise MirrorFull("the mirror is full: the snapshot does not fit")
        written = store.write_bytes(path, raw)
        self._used += written
        self._downloaded += written
        self._live_reset()


    # ---------------------------------------------------------- bundle --
    def _cache_bundle(self, dashboard_id: str, gen: object, state: dict) -> None:
        """Put the generator bundle next to the report copy.

        Why at all. A local run (`mirror/localrun.py`) must work when the
        platform is absent altogether, yet the bundle lives on the platform. So
        the only moment it can be taken is a successful sync; if we do not take
        it then, "Update from folders" turns out to be a button that answers "no
        bundle" on the very day it is needed.

        The file name is its own sha256, and the sum is VERIFIED after writing.
        The check does not duplicate `runs.verify_and_unpack` but stands before
        it: a cache in which something else lies under a sum's name lies to
        everyone who looks into it, and sorting that out in the middle of being
        offline is the worst moment.

        Old bundles of this report are swept: there is one generator version,
        keeping the previous ones is pointless (the platform cannot roll a run
        back to a past version, and offline even less so).
        """
        if not isinstance(gen, dict):
            # The report has no generator -- nothing to run. We clear the field
            # rather than leave the old one: the generator may have been unlinked.
            state["generator"] = None
            return
        version = gen.get("version")
        checksum = gen.get("checksum_sha256")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise SyncError("the manifest named a generator without a version number")
        try:
            path = layout.bundle_path(dashboard_id, checksum, create=True)
        except ValueError as exc:
            raise SyncError(f"the manifest named a generator without a checksum: {exc}") from None

        record = {
            "id": gen.get("id") if isinstance(gen.get("id"), int) else None,
            "name": gen.get("name") if isinstance(gen.get("name"), str) else "",
            "version": version,
            "checksum_sha256": checksum,
            "file": path.name,
        }

        if not path.is_file():
            self._stop_point(f"before bundle of report {dashboard_id}")
            url = self._url(f"/api/dashboards/{quote(dashboard_id)}"
                            f"/generator/bundle?v={version}")
            # We ask for identity: the zip is already compressed, and unpacking
            # the response in order to verify the sha256 of the source file right
            # here would mean introducing a second way to go wrong.
            resp = self._open(url, "identity")
            try:
                promised = resp.headers.get("Content-Length")
                try:
                    expected = int(promised) if promised is not None else None
                except (TypeError, ValueError):
                    expected = None
                self._check_room(expected, "generator bundle")
                self._live_reset()
                try:
                    written = store.write_stream(
                        path,
                        _StoppableReader(resp, lambda: self._stop_point(
                            f"bundle of report {dashboard_id}"),
                            on_chunk=self._chunk),
                        budget=self._budget(), expected=expected)
                except store.ShortRead as exc:
                    raise TransportError(str(exc)) from exc
            finally:
                resp.close()
            self._used += written
            self._downloaded += written
            self._live_reset()
            got = _sha256_file(path)
            if got != checksum:
                # We remove the file: a cache with a wrong sum is worse than an empty cache.
                with contextlib.suppress(OSError):
                    self._used = max(self._used - path.stat().st_size, 0)
                    path.unlink()
                raise SyncError("the checksum of the downloaded bundle did not match the manifest")

        self._prune_bundles(dashboard_id, keep=path.name)
        state["generator"] = record

    def _prune_bundles(self, dashboard_id: str, keep: str) -> None:
        """Remove the bundles of past generator versions of this report."""
        try:
            entries = list(layout.bundle_dir(dashboard_id).iterdir())
        except (OSError, ValueError):
            return
        for p in entries:
            if p.name == keep or not p.is_file():
                continue
            try:
                size = p.stat().st_size
                p.unlink()
            except OSError:
                continue
            self._used = max(self._used - size, 0)


def _sha256_file(path, chunk: int = 1 << 20) -> str:
    """sha256 of a file by chunks: a bundle can be tens of megabytes, and reading
    it into memory for one sum is pointless."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def sync_once(**kwargs) -> SyncResult:
    """One pass for a call from the tray and the schedule.

    A separate function so the caller does not need to know about the class: the
    tray has no reason to keep a synchronizer between passes, and the budget
    and the key are re-read on every pass by construction.

    The result arrives as fields, there are no exceptions (except
    `NoDeviceKey`) -- the tray has somewhere to show the counters. The
    schedule needs a different contract, and it is below.
    """
    return Syncer(**kwargs).sync_all()


def _first_error(result: SyncResult) -> str:
    """The first message from the result -- the one that broke the pass."""
    return result.errors[0][1] if result.errors else ""


def _failure(result: SyncResult) -> Exception:
    """What a failed pass turns into for `schedule.classify`.

    The order of branches is the order of urgency for the person: a dead key
    and a full disk are not cured by waiting (they fix it themselves), while
    "part did not arrive" is cured by a retry and gets backoff.
    """
    if result.stopped:
        exc: Exception = SyncStopped("pass stopped on request")
    elif result.key_rejected:
        exc = KeyRejected(_first_error(result) or result.manifest_error
                          or "the device key was not accepted by the platform")
    elif result.disk_full:
        exc = MirrorFull(_first_error(result) or "no space left on the disk")
        # `MirrorFull` lives in `store` and knows nothing about the icon's state
        # kinds; we attach the fields here because the contract with the schedule is ours.
        exc.kind = "disk"
        exc.needed_bytes = result.needed_bytes
    elif result.full:
        exc = MirrorFull(_first_error(result) or "the mirror is full")
        # Our own ceiling is a SEPARATE kind: freeing disk space is useless here,
        # and offering it to the person is not allowed.
        exc.kind = "quota"
        exc.needed_bytes = result.needed_bytes
    elif result.manifest_error:
        exc = TransportError(result.manifest_error)
    else:
        exc = TransportError(
            f"reports not delivered: {result.failed} of {result.checked}"
            + (f"; first failure: {_first_error(result)}" if result.errors else ""))
    # We attach the counters to the exception: the log and diagnostics need them
    # no less than the cause, and losing them because the pass ended in a
    # refusal is pointless. There is no report data in the result -- only numbers
    # and our own texts.
    exc.result = result
    return exc


def sync_all(server_url: str, device_key: str, *, progress=None, stop=None,
             **kw) -> SyncResult:
    """One pass for the schedule (`agent/mirror/schedule.py`).

    The contract is named in the docstring of `schedule.py` and is executed
    here verbatim: two positional arguments, `progress(done, total)`, `stop` --
    `threading.Event`. It is NOT rewritten for the convenience of this module:
    the schedule thread already calls it, and the icon already draws what the
    contract promises.

    The schedule reads not the result fields but the exception class, so a
    pass that did not go without losses (`SyncResult.ok`) goes out as an
    exception. What goes where in `schedule.classify`:

      key not accepted      `KeyRejected`    `kind="auth"`        -> key_expired
                                                                  / key_rejected
      disk is full          `MirrorFull`     `kind="disk"`        -> M_NO_DISK
      mirror ceiling        `MirrorFull`     `kind="quota"`       -> M_QUOTA
                            (+`needed_bytes` -- "need ... more")
      manifest not obtained
      or part of the reports `TransportError` `kind="unavailable"` -> M_UNAVAILABLE
      stop on "Exit"        `SyncStopped`                         -> not a failure:
          the schedule sees its own `stop` set and leaves `_run_once`
          before `classify`, touching neither the state kind nor `last_ok`.

    Why "part of the reports did not arrive" is `unavailable` and not a
    success: a green check mark over a mirror half of which did not update is
    exactly the lie the docstring of `SyncResult` warns about. And why exactly
    this kind -- `classify` itself decides that: everything that is neither a
    key nor a disk means to it "the platform answered not as it should", and
    introducing a second opinion about that in the synchronizer would be worse
    than adopting it.
    """
    result = Syncer(server_url, token=device_key or None,
                    progress=progress, stop=stop, **kw).sync_all()
    if result.ok:
        return result
    raise _failure(result)
