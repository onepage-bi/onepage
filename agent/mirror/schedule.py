"""Schedule of the background mirror sync and the state for the tray icon.

What lives here: the thread that calls the synchronizer once an hour, the
backoff when the server is unavailable, the classification of failures into
what the person must read ("server unavailable" and "key dead" call for
different actions), and the pure part -- texts, time, sizes -- that can be
checked on macOS without a network and without Windows. The sync itself
(manifest, download, atomic write) is in `agent.mirror.sync`; its logic is
NOT duplicated here.

Expected interface of the synchronizer (it did not exist when this module was
written, so the contract is named here rather than silently guessed):

    sync.sync_all(server_url: str, device_key: str, progress=None, stop=None)

      progress(done, total, downloaded_bytes) -- called from the sync thread;
             the third argument is how many bytes were downloaded in this
             pass, including a partially downloaded file: without it the
             "copy N of M" counter stands still the whole time a large report
             downloads, and that reads as a hang (owner's complaint of
             2026-10-12);
      stop -- threading.Event: the synchronizer must check it between files
             and return, otherwise "Exit" hangs until the download ends;
      returns anything (the result is not read here), a failure is an
      exception.

Classification of exceptions (`classify`): HTTPError 401 -> key; other
HTTPError, URLError, TimeoutError, ConnectionError -> server unavailable;
OSError with ENOSPC -> no space; or an explicit `kind` attribute on the
exception (`"auth" | "unavailable" | "disk" | "quota"`) and, for space,
`needed_bytes`. The attribute beats the type: the synchronizer knows more
about the cause than we do from the class.

If there is no `sync` module, the thread does not start and the icon stays as
it was (`MirrorWorker.enabled` is false). We do not try to imitate work:
"all fresh" without a sync would be a lie in the one place where a lie costs
the most.

The device key is stored in the same store as the GP password
(`creds.CredStore`, DPAPI on Windows), under its own name `KEY_NAME`. It goes
to the synchronizer as an argument and is not kept in this module's fields
longer than the call. Only the failure class and the exception text with the
key value cut out (`_scrub`) reach the log and the saved state: for some
errors urllib puts the request headers into the message, and "the key is not
logged" is not enough on words alone.

Standard library only.
"""
from __future__ import annotations

import errno
import json
import os
import random
import threading
import time
import urllib.error
from datetime import datetime
from pathlib import Path

from ..config import state_dir
from .layout import atomic_write, URL_PREFIX

# The synchronizer is written by another implementer. A top-level try, not an
# import inside a function: a relative import inside a function does not
# survive the gluing into the single-file build. We swallow ONLY the absence
# of the module itself -- an error inside it (typo, broken import) must be
# visible, not masked.
try:
    from . import sync as _sync
except ImportError:                                 # pragma: no cover
    # `from . import x` on a missing submodule gives ImportError, not
    # ModuleNotFoundError (verified: with ModuleNotFoundError the first
    # version of this try caught nothing and broke the tray import). We tell
    # "no file" from "file exists but its import is broken" by the file's
    # presence.
    if Path(__file__).with_name("sync.py").exists():
        raise
    _sync = None

# -- Schedule parameters -----------------------------------------------------
INTERVAL_S = 3600                    # regular period: once an hour
# Backoff when unavailable: the first retry is quick (the network may have
# blinked, and the person is looking at a red icon), then less often, the
# ceiling is the regular period. At night a downed server must not get a
# request a minute from every laptop.
BACKOFF_S = (60, 120, 300, 600, 1200, 1800, INTERVAL_S)
FIRST_DELAY_S = 20                   # after program start: let the network come up
JITTER = 0.10                        # +-10 %, so all machines' clocks do not align
DEFAULT_TTL_DAYS = 90                # devicetoken.DEFAULT_TTL_DAYS on the server

# Name of the key entry in CredStore. The name validator there allows a hyphen;
# a separate name is needed so the key does not overwrite the GP password or
# get mistaken for it.
KEY_NAME = "onepage-device-key"
SCHEDULE_FILE = "mirror-schedule.json"

# -- State kinds (what the icon sees) ----------------------------------------
M_IDLE = "idle"                      # key present, no sync yet
M_SYNCING = "syncing"
M_FRESH = "fresh"
M_UNAVAILABLE = "unavailable"        # server unavailable
M_NO_KEY = "no_key"
M_KEY_EXPIRED = "key_expired"        # 401, the key used to work
M_KEY_REJECTED = "key_rejected"      # 401, the key was never accepted yet
M_NO_DISK = "no_disk"             # disk is out of space
# The mirror ceiling is a SEPARATE kind, not a variety of "no space". The
# person's action is different: freeing the disk will not help, the quota must
# be raised or reports removed. One kind for two causes would give wrong advice
# in half the cases, and the icon and the page advise out loud.
M_QUOTA = "quota"

KEY_DEAD = (M_KEY_EXPIRED, M_KEY_REJECTED)

# -- Texts -------------------------------------------------------------------
# The path is looked up by assets: in the single-file build the resources do
# not sit next to the code.
from . import assets as _assets
_strings_cache: dict | None = None

# Labels of menu items for which strings.json has no keys yet. The owner named
# them in the task; when strings.json gets keys with such names, they override
# the literals -- no code change needed.
_MENU_FALLBACK = {
    "action_open_library": "Open library",
    "action_show_state": "Status",
}


def strings() -> dict:
    """strings.json, once. No file or a corrupt one -- an empty dict: the text
    then equals the key, which is visible at once and does not bring down the icon."""
    global _strings_cache
    if _strings_cache is None:
        path = _assets.strings_path()
        try:
            if path is None:
                raise OSError("strings.json not found")
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            _strings_cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _strings_cache = {}
    return _strings_cache


def text(key: str, **params) -> str:
    """String by key with {p1}, {p2} substitution. str.replace, not format:
    the text may contain curly braces, and format_map would fail on them."""
    s = strings().get(key) or _MENU_FALLBACK.get(key) or key
    for name, value in params.items():
        s = s.replace("{" + name + "}", str(value))
    return s


def fmt_size(n: int | float | None) -> str:
    """"1.2 GB". A dot is the decimal separator of English text."""
    if n is None or n < 0:
        return "—"
    n = float(n)
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            v = n / div
            return (f"{v:.1f}" if v < 10 else f"{v:.0f}") + " " + unit
    return f"{int(n)} B"


def fmt_when(ts: float | None, now: float | None = None) -> str:
    """Relative time by the time_* block; beyond three days -- a date with
    time. The README asks for a date "older than a day", but the key table has
    "d ago (1-3)"; I follow the table and add the date after three days."""
    if not ts:
        return "—"
    now = time.time() if now is None else now
    d = max(0.0, now - ts)
    if d < 60:
        return text("time_just_now")
    if d < 3600:
        return text("time_minutes_ago", p1=int(d // 60))
    if d < 86400:
        return text("time_hours_ago", p1=int(d // 3600))
    if d < 4 * 86400:
        return text("time_days_ago", p1=int(d // 86400))
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def retry_minutes(delay_s: float | None) -> int:
    """Minutes until the next attempt, at least one: "in 0 min" reads as
    "now", while a scheduled attempt is not now."""
    if not delay_s:
        return 1
    return max(1, int(-(-delay_s // 60)))


# -- Backoff and classification ----------------------------------------------
def next_delay(kind: str, failures: int, rng=random.random) -> float | None:
    """In how many seconds the next attempt. None -- wait only for a manual
    start or a new key.

    A dead key is not rechecked on the clock: it will not come back to life on
    its own, and a request an hour from every machine gives nothing but a line
    in the server log.
    """
    if kind in KEY_DEAD or kind == M_NO_KEY:
        return None
    if kind == M_UNAVAILABLE:
        base = BACKOFF_S[min(max(failures, 1) - 1, len(BACKOFF_S) - 1)]
    else:
        base = INTERVAL_S
    return base * (1 + JITTER * (2 * rng() - 1))


def classify(exc: BaseException) -> tuple[str, int | None]:
    """(kind, bytes_needed) for a synchronizer exception.

    Order matters: HTTPError is a subclass of URLError, which is a subclass of
    OSError, so the general branches must come AFTER the specific ones,
    otherwise a 401 becomes "server unavailable" and ENOSPC becomes "no
    network".
    """
    declared = getattr(exc, "kind", None)
    needed = getattr(exc, "needed_bytes", None)
    if declared == "auth":
        return "auth", None
    if declared == "disk":
        return M_NO_DISK, needed
    if declared == "quota":
        return M_QUOTA, needed
    if declared == "unavailable":
        return M_UNAVAILABLE, None
    if isinstance(exc, urllib.error.HTTPError):
        # 401 is the key. 403 too: the server understood the key but refused;
        # for the person it is cured the same way -- a new key. The rest (5xx,
        # 404 during a rollout) -- the platform is not well, copies open, we
        # try later.
        return ("auth", None) if exc.code in (401, 403) else (M_UNAVAILABLE, None)
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (errno.ENOSPC, errno.EDQUOT):
        return M_NO_DISK, needed
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return M_UNAVAILABLE, None
    # Unknown is neither "key" nor "disk"; it is most honest to assume the
    # platform did not answer as it should. Detail stays in the log.
    return M_UNAVAILABLE, None


def _scrub(message: str, key: str | None) -> str:
    """Cut the key value out of the text. An empty key is not cut: replace('', ..)
    would insert the mask between every two characters."""
    if key and len(key) >= 4:
        message = message.replace(key, "***")
    return message


# -- State on disk -----------------------------------------------------------
def _load_persist(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


class MirrorWorker:
    """Sync thread + state snapshot for the tray.

    The tray polls `snapshot()` twice a second from the UI thread, so the
    snapshot is cheap and under its own lock; the work itself is only in the
    `op-mirror-sync` thread.
    """

    def __init__(self, cred_store, server_url, sync_fn=None, persist_path: Path | None = None,
                 log=None, rng=random.random, clock=time.time):
        self._creds = cred_store
        self._server_url = server_url            # callable: the address is taken at attempt time
        self._sync_fn = sync_fn or (getattr(_sync, "sync_all", None) if _sync else None)
        self._persist = persist_path or (state_dir() / SCHEDULE_FILE)
        self._log = log or (lambda m: print(m, flush=True))
        self._rng = rng
        self._clock = clock

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

        saved = _load_persist(self._persist)
        self._last_ok: float | None = saved.get("last_ok") or None
        # "The key used to work" is needed to tell "expired" from "not accepted".
        self._key_accepted = bool(saved.get("key_accepted"))
        self._kind = M_IDLE
        self._failures = 0
        self._next_at: float | None = None
        self._needed: int | None = None
        self._progress = (0, 0)
        self._bytes = 0
        self._running = False

    # -- control -----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._sync_fn is not None

    def start(self) -> bool:
        """Start the thread. False -- no synchronizer, no thread needed."""
        if not self.enabled:
            self._log("[mirror] module agent.mirror.sync not found -- schedule not started")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        # daemon=True is insurance in case the synchronizer hangs on the
        # network; the stop event is the regular path. Neither works alone: a
        # daemon cuts off a write mid-file, an event without a daemon keeps the
        # process alive.
        self._thread = threading.Thread(target=self._loop, name="op-mirror-sync", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()                      # wake from waiting
        t = self._thread
        if t and t is not threading.current_thread():
            t.join(timeout)

    def sync_now(self) -> bool:
        """Start a sync outside the schedule. False -- not started.

        Two different "no"s, and both must be honest, because the tray menu
        item and the button on the library page tell the person "started" by
        this answer:

          * already running -- a second parallel one would write into the same
            `.tmp` files and show the counter jumbled;
          * there is NO schedule (`enabled` is false or the thread is not up) --
            setting `_wake` on a nonexistent thread is pointless: nobody will
            pick the event up, there will be no pass. The earlier unconditional
            `True` here was exactly that silent refusal -- the person presses
            "Update now", gets "started", and nothing ever happens.
        """
        if not self.enabled:
            return False
        t = self._thread
        if not (t and t.is_alive()):
            return False
        with self._lock:
            if self._running:
                return False
        self._wake.set()
        return True

    def key_changed(self) -> None:
        """The key was replaced: earlier conclusions about it are void."""
        with self._lock:
            self._key_accepted = False
            self._failures = 0
            self._kind = M_IDLE
        self._save()
        self._wake.set()

    # -- snapshot for the tray ---------------------------------------------
    def has_key(self) -> bool:
        try:
            return self._creds.has_password(KEY_NAME)
        except Exception:                                  # noqa: BLE001
            return False

    def snapshot(self) -> dict:
        has_key = self.has_key()
        with self._lock:
            kind = self._kind
            if not has_key:
                kind = M_NO_KEY
            elif kind == M_NO_KEY:
                kind = M_IDLE
            if self._running:
                kind = M_SYNCING
            done, total = self._progress
            downloaded = self._bytes
            now = self._clock()
            delay = (self._next_at - now) if self._next_at else None
            return {
                "enabled": self.enabled,
                "kind": kind,
                "has_key": has_key,
                "last_ok": self._last_ok,
                "done": done, "total": total, "bytes": downloaded,
                "retry_in_s": delay if (delay and delay > 0) else None,
                "needed_bytes": self._needed,
            }

    # -- loop --------------------------------------------------------------
    def _loop(self) -> None:
        delay: float | None = FIRST_DELAY_S
        while not self._stop.is_set():
            # None -- wait only for a manual start or a new key.
            self._wake.wait(delay)
            if self._stop.is_set():
                break
            # clear AFTER waiting and BEFORE working: a sync_now pressed during
            # a sync is not lost, and one pressed before it does not cause a second.
            self._wake.clear()
            try:
                delay = self._run_once()
            except Exception as e:                         # noqa: BLE001
                # Nothing should get here: _run_once catches everything itself.
                # If something did, the thread must live, otherwise the mirror
                # freezes silently.
                self._log(f"[mirror] cycle failure: {type(e).__name__}")
                delay = INTERVAL_S

    def _run_once(self) -> float | None:
        key = None
        try:
            key = self._creds.get_password(KEY_NAME)
        except Exception as e:                             # noqa: BLE001
            # The store could not decrypt (another Windows account, a broken
            # blob). For the person this is "no key": it must be entered again.
            self._log(f"[mirror] key not read: {type(e).__name__}")
        if not key:
            self._finish(M_NO_KEY)
            return None

        server = None
        try:
            server = self._server_url()
        except Exception:                                  # noqa: BLE001
            pass
        if not server:
            self._log("[mirror] platform address not set")
            self._finish(M_UNAVAILABLE)
            return next_delay(M_UNAVAILABLE, self._failures, self._rng)

        with self._lock:
            self._running = True
            self._progress = (0, 0)
            self._bytes = 0
            self._next_at = None
        self._log("[mirror] sync started")
        kind, needed = M_FRESH, None
        try:
            self._sync_fn(server, key, progress=self._on_progress, stop=self._stop)
        except BaseException as e:                         # noqa: BLE001
            if self._stop.is_set():
                # Stopped by "Exit": unfinished is neither an error nor a refusal.
                with self._lock:
                    self._running = False
                return None
            kind, needed = classify(e)
            self._log(f"[mirror] failure ({kind}): {type(e).__name__}: "
                      f"{_scrub(str(e), key)[:200]}")
            if kind == "auth":
                kind = M_KEY_EXPIRED if self._key_accepted else M_KEY_REJECTED
            if not isinstance(e, Exception):
                raise
        finally:
            del key                                        # do not hold it in the frame longer than needed
        if kind == M_FRESH:
            self._log("[mirror] sync finished")
        return self._finish(kind, needed)

    def _finish(self, kind: str, needed: int | None = None) -> float | None:
        with self._lock:
            self._running = False
            self._kind = kind
            self._needed = needed
            if kind == M_FRESH:
                self._failures = 0
                self._last_ok = self._clock()
                self._key_accepted = True
            elif kind == M_UNAVAILABLE:
                self._failures += 1
            delay = next_delay(kind, self._failures, self._rng)
            self._next_at = (self._clock() + delay) if delay else None
        if kind == M_FRESH:
            self._save()
        return delay

    def _on_progress(self, done, total, downloaded=0) -> None:
        # The third argument with a default is not politeness to old callers
        # but protection: the synchronizer in the kit and the one in the
        # repository are updated in different ways (the single-file build is
        # rebuilt separately), and a two-argument call must not bring down the
        # schedule thread.
        try:
            with self._lock:
                self._progress = (int(done), int(total))
                self._bytes = max(0, int(downloaded))
        except (TypeError, ValueError):
            pass

    def _save(self) -> None:
        try:
            with atomic_write(self._persist, "w") as fh:
                json.dump({"last_ok": self._last_ok, "key_accepted": self._key_accepted}, fh)
        except OSError as e:
            # The disk is full exactly when it matters most. The state simply
            # will not survive a restart; this does not hinder the work.
            self._log(f"[mirror] state not saved: {type(e).__name__}")


# -- State texts (pure functions over the snapshot) --------------------------
def status_text(snap: dict | None, now: float | None = None) -> str:
    """One state line -- exactly one key from strings.json, as strings_README
    asks. For the icon and for the "Status" menu item."""
    if not snap or not snap.get("enabled"):
        return ""
    kind = snap.get("kind")
    if kind == M_SYNCING:
        # Three different states that the person must tell apart: "still
        # asking the platform" (there is no counter and cannot be -- the list of
        # reports is unknown), "downloading, here is how much" and
        # "downloading, bytes have not started yet". One text for all three is
        # what produced the question "unclear when it will finish".
        if not snap.get("total"):
            return text("sync_manifest")
        if snap.get("bytes"):
            return text("sync_running_bytes", p1=snap["done"], p2=snap["total"],
                        p3=fmt_size(snap["bytes"]))
        return text("sync_running_progress", p1=snap["done"], p2=snap["total"])
    if kind == M_FRESH:
        return text("sync_done", p1=fmt_when(snap.get("last_ok"), now))
    if kind == M_UNAVAILABLE:
        return text("sync_server_unavailable", p1=text("time_in_minutes",
                    p1=retry_minutes(snap.get("retry_in_s"))))
    if kind == M_NO_KEY:
        return text("sync_no_key")
    if kind == M_KEY_EXPIRED:
        return text("sync_key_expired")
    if kind == M_KEY_REJECTED:
        return text("sync_key_rejected")
    if kind == M_NO_DISK:
        # How much is missing the synchronizer may not know (ENOSPC in the
        # middle of a write). Then we put a dash rather than invent a number.
        return text("sync_no_disk_space", p1=fmt_size(snap.get("needed_bytes")))
    if kind == M_QUOTA:
        # Own quota: say nothing about the disk here -- it may be empty.
        return text("sync_mirror_full", p1=fmt_size(snap.get("needed_bytes")))
    # M_IDLE: the key is present, no first sync yet. strings.json has no
    # separate line for it, and "Updating copies..." here would be untrue.
    if snap.get("last_ok"):
        return text("sync_done", p1=fmt_when(snap["last_ok"], now))
    return ""


def library_url(port: int) -> str:
    """Local address of the library. Loopback only: the mirror is not served outward.

    `/library/`, not `URL_PREFIX` (`/mirror/`): the second address serves the
    reports themselves (`serve.py`), and a person who opened it from the icon
    menu would get a 404 instead of the list of copies. The trailing slash is
    mandatory -- the page resolves its `common.js`/`mirror.css` from it.

    This function is the ONLY place where the "/library" prefix is written as
    a string: the shell (`libui.LIBRARY_PREFIX`) extracts the path from this
    same address rather than repeating the literal. A reverse import would be
    circular (the shell itself imports this module for texts and state
    kinds), and two literals eventually drift apart -- and then the icon menu
    opens a 404 on the program's own page. So the address may be changed only
    here.
    """
    return f"http://127.0.0.1:{int(port)}/library/"


def default_server_url(config) -> str | None:
    """Platform address: OP_MIRROR_SERVER, otherwise the first origin from the allowlist.

    agent.ini has no separate field for the server (config.py is not mine),
    and the allowlist is by meaning the list of platforms the agent trusts;
    the production one comes first. Only https/http -- no other scheme belongs
    here.
    """
    url = (os.environ.get("OP_MIRROR_SERVER") or "").strip()
    if not url:
        origins = getattr(config, "allowed_origins", ()) or ()
        url = origins[0] if origins else ""
    return url.rstrip("/") if url.startswith(("https://", "http://")) else None
