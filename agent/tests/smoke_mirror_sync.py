"""Smoke test of the SEAM between the schedule and the mirror synchronizer. Standard library only.

Why a separate file. `smoke_mirror.py` calls the `sync.Syncer` class directly,
and `schedule.MirrorWorker` was checked only with a substituted `sync_fn`. Each
half was green on its own, and no check saw that the schedule calls
`sync.sync_all(server, key, progress=, stop=)`, which the module did not have:
`MirrorWorker.enabled` was false, the thread did not start, the mirror never
synchronized even once -- with 89 + 69 green checks. What is checked here is
exactly the GAP: the worker is built the way `agent/controller.py` builds it
(without `sync_fn`), and everything after that goes through the real
`schedule`, `sync`, `store`, `libui`, `AgentServer`. There are no synchronizer
substitutions in this file anywhere except one place with a caveat (scenario
3: "no synchronizer" cannot be obtained other than by removing it).

The platform is replaced by a stub on `http.server` (127.0.0.1, port 0): the
manifest, documents, slots, including a slot that is served BIT BY BIT so that
it can be interrupted in the middle. The test does not go out to a real
network. There is no data of real reports: file bodies are deterministic noise
from sha256. `LOCALAPPDATA` points to its own temporary directory per scenario.

A red check here is a finding, not a reason to fit the test to the code: the
expectations are derived from the contracts (docstrings of `schedule`, `sync`,
`libui`, README), not from today's behaviour.

Run:  python -m agent.tests.smoke_mirror_sync   (from the OnePage repo root)
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import gzip
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

# A key file in the dev store on non-Windows: without it CredStore refuses, and
# the key checks would be red for a reason unrelated to the mirror.
os.environ.setdefault("OP_AGENT_DEV_CRED_STORE", "1")

from agent.config import Config                                    # noqa: E402
from agent.controller import AgentController                       # noqa: E402
from agent.creds import CredStore                                  # noqa: E402
from agent.mirror import assets, layout, libui, schedule, store, sync   # noqa: E402
from agent.pairing import Pairing                                  # noqa: E402
from agent.server import AgentServer                               # noqa: E402

_PASS = 0
_FAIL = 0

# A recognizable key: we search for it as a substring in output, log, exceptions and files.
KEY = "OPKEY-5b8d20e4-SYNCSEAM-SECRET-qx73"

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="smoke_mirror_sync_"))
_COUNTER = 0
_REAL_SLEEP = time.sleep          # the test's own sleep must not fall under the substitution


def check(label, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"PASS {label}")
    else:
        _FAIL += 1
        print(f"FAIL {label}  {extra}")


# --------------------------------------------------------------------------- #
# Output capture: the key must not appear in ANY line over the whole run       #
# --------------------------------------------------------------------------- #

class _Tee:
    """Writes both to the real stream and to a shared buffer. Needed so that at
    the end one check can say "over the whole run the key was not in the console"
    -- covering the standard output `print` from `sync`, the worker's log, and
    tracebacks."""

    def __init__(self, real, buf):
        self._real, self._buf = real, buf

    def write(self, s):
        self._buf.append(s if isinstance(s, str) else str(s))
        return self._real.write(s)

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):          # isatty, encoding, etc.
        return getattr(self._real, name)


# --------------------------------------------------------------------------- #
# Data: noise instead of real content                                          #
# --------------------------------------------------------------------------- #

def blob(tag: str, n: int) -> bytes:
    """n deterministic bytes. Two different tags differ from the first block,
    so "the file equals exactly these bytes" and "the file is a stump of these
    bytes" are distinguishable byte by byte."""
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hashlib.sha256(f"{tag}:{i}".encode()).digest()
        i += 1
    return bytes(out[:n])


# --------------------------------------------------------------------------- #
# Platform stub                                                               #
# --------------------------------------------------------------------------- #

class Platform:
    def __init__(self):
        self.key = KEY
        self.items: list[dict] = []
        self.docs: dict[str, bytes] = {}
        self.slots: dict[str, dict[int, bytes]] = {}
        self.doc_delay = 0.0               # pause before EACH document
        # (id, slot) -> (chunk, pause, threshold): the slot is served in chunks
        # with a pause; at the threshold `mid` is set -- "client mid-download".
        self.trickle: dict[tuple[str, int], tuple[int, float, int]] = {}
        self.mid = threading.Event()
        self.slot_done: dict[tuple[str, int], bool] = {}
        self.manifest_status = 200
        self.slot_status: int | None = None
        self.log: list[str] = []
        self.lock = threading.Lock()
        self.server: ThreadingHTTPServer | None = None
        self.base = ""

    def add(self, did, slots=None, doc_bytes=None, size_override=None, version=1):
        """Create a report: document, slots and a manifest row."""
        self.docs[did] = doc_bytes if doc_bytes is not None else blob("doc-" + did, 700)
        rows = []
        for n, body in sorted((slots or {}).items()):
            self.slots.setdefault(did, {})[n] = body
            rows.append({"slot": n, "name": f"data{n}.bin",
                         "size": size_override if size_override is not None else len(body),
                         "v": f"1-{len(body)}"})
        self.items.append({
            "id": did, "title": "T " + did, "type": "html", "version": version,
            "fingerprint": "fp-" + did, "mirrorable": True, "skip_reason": None,
            "slots": rows, "can_update_data": False, "rls_enabled": False,
            "updated_at": "2026-09-01T10:00:00"})

    def hits(self, prefix: str) -> list[str]:
        with self.lock:
            return [p for p in self.log if p.startswith(prefix)]

    def stop_server(self) -> None:
        """The platform "died": the port is closed, connections are refused."""
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def _handler(plat: Platform):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", headers=None, reason=None):
            self.send_response(code, reason)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            with plat.lock:
                plat.log.append(path)
            auth = self.headers.get("Authorization")
            if auth != "Bearer " + plat.key:
                return self._send(401, b'{"error":"bad key"}')
            if path == "/api/sync/manifest":
                if plat.manifest_status != 200:
                    # The key in both body and status line is the worst case: the
                    # code must pull neither into its messages.
                    return self._send(plat.manifest_status, ("echo " + auth).encode(),
                                      reason="echo " + auth)
                body = json.dumps({"items": plat.items, "rights_ttl_s": 3600,
                                   "snapshot_external": False}).encode()
                return self._send(200, body, {"Content-Type": "application/json"})
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 2 and parts[0] == "render":
                did = parts[1]
                if len(parts) == 2:
                    doc = plat.docs.get(did)
                    if doc is None:
                        return self._send(404)
                    if plat.doc_delay:
                        _REAL_SLEEP(plat.doc_delay)
                    return self._send(200, doc, {"Content-Type": "text/html"})
                if len(parts) == 4 and parts[2] == "files":
                    if plat.slot_status:
                        return self._send(plat.slot_status, ("echo " + auth).encode())
                    n = int(parts[3])
                    body = plat.slots.get(did, {}).get(n)
                    if body is None:
                        return self._send(404)
                    etag = f'"{did}-{n}-{len(body)}"'
                    tr = plat.trickle.get((did, n))
                    if tr is None:
                        return self._send(200, body, {"ETag": etag})
                    chunk, pause, mid_at = tr
                    self.send_response(200)
                    self.send_header("ETag", etag)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    sent = 0
                    try:
                        for i in range(0, len(body), chunk):
                            self.wfile.write(body[i:i + chunk])
                            self.wfile.flush()
                            sent += min(chunk, len(body) - i)
                            if sent >= mid_at:
                                plat.mid.set()
                            _REAL_SLEEP(pause)
                        plat.slot_done[(did, n)] = True
                    except OSError:
                        # The client dropped the connection -- this is the expected
                        # outcome of a stop; "slot served whole" stays a lie.
                        self.close_connection = True
                    return None
            return self._send(404)

    return H


def start_platform(plat: Platform) -> str:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(plat))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    plat.server = srv
    plat.base = f"http://127.0.0.1:{srv.server_address[1]}"
    return plat.base


def dead_base() -> str:
    """An address on which nobody is guaranteed to be listening: a port is taken
    and immediately released. The connection will be refused at once, without waiting for a timeout."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------- #
# Scaffold                                                                    #
# --------------------------------------------------------------------------- #

def fresh_env() -> Path:
    global _COUNTER
    _COUNTER += 1
    d = _TMP_ROOT / f"s{_COUNTER}"
    d.mkdir()
    os.environ["LOCALAPPDATA"] = str(d)
    return d


class FakeCreds:
    """A key store in memory: exactly what the worker asks it for."""

    def __init__(self, key: str | None = KEY):
        self.key = key

    def has_password(self, name):
        return bool(self.key) and name == schedule.KEY_NAME

    def get_password(self, name):
        return self.key if name == schedule.KEY_NAME else None

    def set_password(self, name, value):
        self.key = value


class Worker:
    """MirrorWorker, built AS IN controller.py:41 -- without sync_fn -- plus a
    log that shows the pass happened at all."""

    def __init__(self, env: Path, base, creds=None):
        self.lines: list[str] = []
        self.w = schedule.MirrorWorker(
            creds if creds is not None else FakeCreds(),
            (lambda: base) if isinstance(base, str) or base is None else base,
            persist_path=env / "sched.json",
            log=self.lines.append)

    def __getattr__(self, name):
        return getattr(self.w, name)


@contextlib.contextmanager
def fast_backoff():
    """The synchronizer's backoff on unavailability (2+4+8 s) is shortened: the
    unavailability scenarios check the state KIND, not the length of the pause."""
    orig = time.sleep
    time.sleep = lambda s: _REAL_SLEEP(min(float(s), 0.005))
    try:
        yield
    finally:
        time.sleep = orig


def wait_until(pred, timeout=10.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        _REAL_SLEEP(step)
    return pred()


def wait_pass(wk: Worker, plat: Platform | None, a0: int, timeout=40.0, quiet=0.4):
    """Wait until a PASS happened and went quiet. Returns (snapshot, waited).

    The sign of activity is growth of the platform log and the worker log, not
    a specific message and not a specific state: the test must not know how
    exactly the implementation marks the end of a pass. A lull of `quiet`
    seconds in a state other than "syncing" is the end. The schedule puts the
    next attempt at least a minute ahead, so the lull is not deceived.
    """
    def activity():
        return len(wk.lines) + (len(plat.log) if plat else 0)

    if not wait_until(lambda: activity() > a0, timeout):
        return wk.snapshot(), False
    last, since = activity(), time.monotonic()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        a = activity()
        if a != last:
            last, since = a, time.monotonic()
        if wk.snapshot()["kind"] != schedule.M_SYNCING and time.monotonic() - since >= quiet:
            return wk.snapshot(), True
        _REAL_SLEEP(0.01)
    return wk.snapshot(), False


def run_pass(wk: Worker, plat: Platform | None, timeout=40.0):
    """Start the worker, nudge a pass and wait for its end."""
    a0 = len(wk.lines) + (len(plat.log) if plat else 0)
    wk.start()
    wk.sync_now()
    return wait_pass(wk, plat, a0, timeout)


def activity_of(wk: Worker, plat: Platform | None) -> int:
    return len(wk.lines) + (len(plat.log) if plat else 0)


def page_statuses() -> set[str]:
    """The statuses the library page can draw: a parse of `web/library.js`.
    Needed so that a state's "own kind" does not turn out to be a kind that has
    neither a text nor a button on the page."""
    p = assets.res_path("library.js", web=True)
    src = p.read_text(encoding="utf-8") if p else ""
    return set(re.findall(r"""\bst\s*===\s*['"]([a-z_]+)['"]""", src)) | \
        set(re.findall(r"""sync\.status\s*===\s*['"]([a-z_]+)['"]""", src))


def page_block(wk: Worker) -> dict:
    """The `sync` block of data.json as the shell builds it from the worker snapshot."""
    return libui.sync_block(SimpleNamespace(
        controller=SimpleNamespace(mirror=wk.w), cred_store=None))


def audit_disk(plat: Platform) -> tuple[list[str], list[str], list[str]]:
    """What lies in the mirror after a pass. Only a byte comparison with the platform.

    Returns (stumps, broken_committed, committed):
      * a stump -- a file (not `.tmp`) whose content is the own beginning of one
        of the files the platform served. This is how an interrupted download
        taken for a finished one looks; it cannot be caught by size, so bytes
        are compared;
      * committed -- a report whose `state.json` names the server version from
        the manifest. Such a report must lie whole and byte-for-byte equal to
        what was served.
    """
    root = layout.mirror_root()
    payloads = list(plat.docs.values()) + [b for s in plat.slots.values() for b in s.values()]
    partial, broken, committed = [], [], []
    if root.exists():
        for f in root.rglob("*"):
            if not f.is_file() or f.name.endswith(".tmp"):
                continue
            data = f.read_bytes()
            if data and any(len(data) < len(p) and p.startswith(data) for p in payloads):
                partial.append(f.relative_to(root).as_posix())
    for it in plat.items:
        did = it["id"]
        st = store.load_state(did)
        if not st or st.get("server_version") != it["version"]:
            continue
        committed.append(did)
        vdir = layout.server_version_dir(did, it["version"])
        doc = vdir / layout.DOCUMENT_NAME
        if not doc.is_file() or doc.read_bytes() != plat.docs[did]:
            broken.append(f"{did}: document")
        for rec in st.get("slots") or []:
            body = plat.slots.get(did, {}).get(rec.get("slot"))
            p = layout.slot_disk_path(vdir, rec)
            if body is None or not p.is_file() or p.read_bytes() != body:
                broken.append(f"{did}: slot {rec.get('slot')}")
    return partial, broken, committed


def same_on_disk(plat: Platform, ids) -> bool:
    """All listed reports lie whole and equal to what the platform served."""
    partial, broken, committed = audit_disk(plat)
    return not partial and not broken and all(i in committed for i in ids)


def key_in_files(root: Path) -> list[str]:
    hits = []
    for p in root.rglob("*"):
        if p.is_file():
            try:
                if KEY.encode() in p.read_bytes():
                    hits.append(p.relative_to(root).as_posix())
            except OSError:
                pass
    return hits


def post(port, path, body=b"{}"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request("POST", path, body=body,
                  headers={"Content-Type": "application/json",
                           "Content-Length": str(len(body))})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def get_json(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"null")
    finally:
        c.close()


def start_live(env: Path, base: str):
    """The real agent as `__main__._serve` builds it for the icon:
    AgentServer + AgentController, `server.controller = controller`, the schedule
    is up. Only the icon itself is missing -- there is nothing to draw it with on macOS."""
    cfg = Config(allowed_origins=(base,), connections={})
    pairing = Pairing(code_factory=lambda: "123456")
    creds = CredStore(root=env)
    srv = AgentServer(("127.0.0.1", 0), cfg, pairing, cred_store=creds)
    srv.state_root = env
    ctl = AgentController(srv, pairing, creds, cfg, "conn")
    srv.controller = ctl
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, ctl, srv.server_address[1]


# --------------------------------------------------------------------------- #
# 1. Entry point contract                                                     #
# --------------------------------------------------------------------------- #

def test_entry_contract():
    # The contract is named in the docstring of schedule.py (line 13), and is
    # called by MirrorWorker._run_once: `self._sync_fn(server, key, progress=, stop=)`.
    fn = getattr(sync, "sync_all", None)
    check("entry point: sync.sync_all exists at module level and is callable",
          callable(fn), "sync.py has only Syncer.sync_all() and sync_once(**kwargs)")
    if not callable(fn):
        return
    try:
        sig = inspect.signature(fn)
        sig.bind("http://127.0.0.1:1", KEY, progress=lambda d, t, b: None,
                 stop=threading.Event())
        bound = True
        why = ""
    except (TypeError, ValueError) as e:
        bound, why = False, str(e)
    check("entry point: the signature accepts (server_url, device_key, progress=, stop=)",
          bound, why)

    # A signature without a call lets through a TypeError that happened inside,
    # and lets through a function that accepts arguments but trips on the first one.
    env = fresh_env()
    plat = Platform()
    base = start_platform(plat)
    calls: list = []
    try:
        res = fn(base, KEY, progress=lambda d, t, b: calls.append((d, t, b)),
                 stop=threading.Event())
        err = None
    except BaseException as e:                           # noqa: BLE001
        res, err = None, e
    check("entry point: a real call exactly as the schedule calls it does not fail "
          "on an empty manifest", err is None,
          "".join(traceback.format_exception_only(type(err), err)).strip() if err else "")
    try:
        fn(base, KEY)
        err2 = None
    except BaseException as e:                           # noqa: BLE001
        err2 = e
    check("entry point: progress and stop are optional (a call with two arguments)",
          err2 is None,
          "".join(traceback.format_exception_only(type(err2), err2)).strip() if err2 else "")
    check("entry point: the platform got the key in the header (the call reached the network)",
          len(plat.hits("/api/sync/manifest")) >= 2, repr(plat.log))
    plat.stop_server()
    del env, res


# --------------------------------------------------------------------------- #
# 2. The worker built by the controller is alive                              #
# --------------------------------------------------------------------------- #

def test_controller_worker_enabled():
    env = fresh_env()
    plat = Platform()
    plat.add("rep-a", {0: blob("a0", 5000)})
    plat.add("rep-b")
    base = start_platform(plat)

    # Exactly the line from controller.py:41: MirrorWorker(cred_store, lambda: url)
    # -- WITHOUT sync_fn. Substituting sync_fn in this check would be the very
    # substitution because of which everything used to pass green.
    bare = schedule.MirrorWorker(FakeCreds(), lambda: base, persist_path=env / "bare.json",
                                 log=lambda m: None)
    check("schedule: MirrorWorker without sync_fn (as in the controller) enabled=True",
          bare.enabled is True,
          "sync_fn was not found: the schedule does not start, the mirror never updates")
    check("schedule: snapshot() reports enabled=True", bare.snapshot().get("enabled") is True)
    started = bare.start()
    check("schedule: start() brings up the thread (True)", started is True)
    bare.stop(timeout=5)

    # Now through the real AgentController and the real key store.
    cfg = Config(allowed_origins=(base,), connections={})
    pairing = Pairing(code_factory=lambda: "123456")
    srv_stub = SimpleNamespace(busy=threading.Event(), server_address=("127.0.0.1", 0),
                               shutdown=lambda: None)
    ctl = AgentController(srv_stub, pairing, CredStore(root=env), cfg, "conn")
    check("controller: ctl.mirror.enabled=True without a single substitution", ctl.mirror.enabled is True)
    check("controller: start_mirror() returns True", ctl.start_mirror() is True)
    lines: list[str] = []
    ctl.mirror._log = lines.append            # the log is only to see activity
    a0 = len(lines) + len(plat.log)
    msg = ctl.set_device_key(KEY)
    check("controller: key accepted (key_added text)", msg == schedule.text("key_added"), msg)
    # "Key added. Starting the update." is a promise; we check it is not a lie.
    wk = SimpleNamespace(lines=lines, snapshot=ctl.mirror.snapshot)
    snap, done = wait_pass(wk, plat, a0)
    check("controller: after entering the key the update really started "
          "(the platform got the manifest)", len(plat.hits("/api/sync/manifest")) >= 1,
          repr(plat.log))
    check("controller: the pass finished (a lull, not \"syncing\")", done, str(snap))
    check("controller: the outcome is fresh, last_ok is set",
          snap["kind"] == schedule.M_FRESH and bool(snap.get("last_ok")), str(snap))
    check("controller: both reports landed on disk whole, byte for byte",
          same_on_disk(plat, ["rep-a", "rep-b"]), str(audit_disk(plat)))
    ctl.mirror.stop(timeout=5)
    check("key: it is not in the state files in clear text",
          not key_in_files(env), repr(key_in_files(env)))
    plat.stop_server()


# --------------------------------------------------------------------------- #
# 3. sync_now does not lie                                                    #
# --------------------------------------------------------------------------- #

def test_sync_now_honest():
    env = fresh_env()
    plat = Platform()
    plat.add("rep-a")
    base = start_platform(plat)

    # a) there is no synchronizer. This state can be obtained only by removing
    # it: the "no module" path is deliberately built into schedule (`_sync = None`).
    saved = schedule._sync
    schedule._sync = None
    try:
        dead = Worker(env, base)
    finally:
        schedule._sync = saved
    if dead.enabled:                           # a future implementation could find it differently
        dead.w._sync_fn = None
    check("sync_now: stand -- a worker without a synchronizer is inoperable (enabled=False)",
          dead.enabled is False)
    check("sync_now: start() without a synchronizer returns False", dead.start() is False)
    answer = dead.sync_now()
    check("sync_now: without a synchronizer it does NOT answer \"started\" (True)", answer is False,
          f"sync_now()={answer!r}: _wake is set on a thread that does not exist")
    check("sync_now: no request went to the platform", not plat.log, repr(plat.log))

    # b) the worker is fine but the thread is not started: "started" is allowed
    # only if a pass really started.
    idle = Worker(env, base)
    answer = idle.sync_now()
    began = wait_until(lambda: len(plat.hits("/api/sync/manifest")) >= 1, 2.5)
    check("sync_now: a worker without a running thread -- \"started\" only if a pass happened",
          (answer is False) or bool(began),
          f"sync_now()={answer!r}, but the platform got no request in 2.5 s")
    idle.stop(timeout=3)

    # c) control: a running worker -- a pass goes, a repeated start is rejected.
    plat.items.clear()
    plat.add("rep-slow")
    plat.doc_delay = 0.8
    live = Worker(env, base)
    live.start()
    n0 = len(plat.hits("/api/sync/manifest"))
    first = live.sync_now()
    seen = wait_until(lambda: live.snapshot()["kind"] == schedule.M_SYNCING, 5)
    second = live.sync_now() if seen else None
    check("sync_now: control -- a running worker answers \"started\"", first is True, repr(first))
    check("sync_now: control -- a pass really goes (kind=syncing)", bool(seen))
    check("sync_now: while a pass goes, a second start is rejected (False)",
          second is False, repr(second))
    snap, done = wait_pass(live, plat, 0)
    check("sync_now: control -- the platform got the manifest from this start",
          len(plat.hits("/api/sync/manifest")) > n0)
    live.stop(timeout=5)
    plat.stop_server()


# --------------------------------------------------------------------------- #
# 4. progress arrives                                                         #
# --------------------------------------------------------------------------- #

def _three_reports(plat: Platform):
    for i in range(3):
        plat.add(f"prog-{i}", {0: blob(f"p{i}", 16000)})
    plat.doc_delay = 0.3


def test_running_texts():
    """A person must tell the three stages of a pass apart by the state line.

    A pure function is checked: the schedule calls it both for the icon and for
    the library page, and it is its single text for all stages that gave rise
    to the question "updating copies... -- and when will it finish?".
    """
    def snap(**kw):
        base = {"enabled": True, "kind": schedule.M_SYNCING,
                "done": 0, "total": 0, "bytes": 0}
        base.update(kw)
        return base

    t_manifest = schedule.status_text(snap())
    t_count = schedule.status_text(snap(done=3, total=12))
    t_bytes = schedule.status_text(snap(done=3, total=12, bytes=24 << 20))
    check("stages: before the manifest the text does NOT promise a counter",
          bool(t_manifest) and "0" not in t_manifest, repr(t_manifest))
    check("stages: with a counter it is visible how many reports are done",
          "3" in t_count and "12" in t_count, repr(t_count))
    check("stages: with bytes both reports and volume are visible",
          "3" in t_bytes and "12" in t_bytes and "24" in t_bytes, repr(t_bytes))
    check("stages: three stages -- three different lines",
          len({t_manifest, t_count, t_bytes}) == 3,
          repr([t_manifest, t_count, t_bytes]))


def test_progress():
    # a) direct call: the callback gets (done, total) from the sync thread.
    env = fresh_env()
    plat = Platform()
    _three_reports(plat)
    base = start_platform(plat)
    calls: list = []
    fn = getattr(sync, "sync_all", None)
    err = None
    if callable(fn):
        try:
            fn(base, KEY, progress=lambda d, t, b: calls.append((d, t, b)),
               stop=threading.Event())
        except BaseException as e:                       # noqa: BLE001
            err = e
    check("progress: sync.sync_all worked on a manifest of three reports",
          callable(fn) and err is None, repr(err))
    check("progress: the callback was called", bool(calls), "no calls at all")
    ok_types = all(isinstance(d, int) and isinstance(t, int) and isinstance(b, int)
                   for d, t, b in calls)
    check("progress: arguments are integers (done, total, bytes)",
          bool(calls) and ok_types, repr(calls[:4]))
    totals = {t for _, t, _ in calls if t}
    check("progress: total is the number of reports (3) and does not change along the way",
          totals == {3}, repr(calls))
    dones = [d for d, _, _ in calls]
    check("progress: done does not decrease", dones == sorted(dones), repr(dones))
    check("progress: the pass ended at (3, 3)",
          bool(calls) and calls[-1][:2] == (3, 3), repr(calls[-1:]))
    # Bytes are the very answer to "unclear when it will finish": while a report
    # downloads, the (done, total) pair stands still, and the only thing that
    # tells a live program from a hung one is a growing number of megabytes.
    bytes_seen = [b for _, _, b in calls]
    check("progress: the byte counter does not decrease", bytes_seen == sorted(bytes_seen),
          repr(bytes_seen))
    check("progress: by the end of the pass bytes are counted (not zero)",
          bool(bytes_seen) and bytes_seen[-1] > 0, repr(bytes_seen[-1:]))
    plat.stop_server()

    # b) what the person sees: data.json of the real agent DURING the pass.
    env = fresh_env()
    plat = Platform()
    _three_reports(plat)
    base = start_platform(plat)
    srv, ctl, port = start_live(env, base)
    try:
        check("live agent: the schedule is up (start_mirror)", ctl.start_mirror() is True)
        a0 = len(plat.log)
        st, body = post(port, "/library/key", json.dumps({"key": KEY}).encode())
        check("live agent: POST /library/key accepted", st == 200, f"{st} {body[:60]!r}")
        samples: list[dict] = []
        end = time.monotonic() + 40
        last_act, since = len(plat.log), time.monotonic()
        while time.monotonic() < end:
            s, data = get_json(port, "/library/data.json")
            sy = data.get("sync", {}) if s == 200 else {}
            samples.append(sy)
            if len(plat.log) != last_act:
                last_act, since = len(plat.log), time.monotonic()
            if (len(plat.log) > a0 and sy.get("status") != "running"
                    and time.monotonic() - since > 0.5):
                break
            _REAL_SLEEP(0.03)
        running = [s for s in samples if s.get("status") == "running"]
        check("live agent: during the pass data.json shows status=running",
              bool(running), "the pass was going, and the page saw \"idle\"")
        check("live agent: running frames name total=3",
              bool(running) and all(s.get("total") == 3 for s in running if s.get("total")),
              repr(running[:3]))
        mid = [s for s in running if 0 < s.get("done", 0) < s.get("total", 0)]
        check("live agent: an intermediate frame 0 < done < total is visible", bool(mid),
              repr([(s.get("done"), s.get("total")) for s in running]))
        seq = [s.get("done", 0) for s in running]
        check("live agent: done does not decrease along the way", seq == sorted(seq), repr(seq))
        final = samples[-1] if samples else {}
        check("live agent: at the end status is not running, last_sync_at is present (a number)",
              final.get("status") != "running" and isinstance(final.get("last_sync_at"), int),
              repr(final))
        s, data = get_json(port, "/library/data.json")
        check("live agent: the library has three reports", s == 200 and len(data.get("items", [])) == 3,
              repr(data.get("items"))[:200])
        check("live agent: reports open via /mirror/<id>/",
              all(http_status(port, f"/mirror/prog-{i}/") == 200 for i in range(3)))

        # c) POST /library/sync with a live schedule: "started" is the truth.
        n0 = len(plat.hits("/api/sync/manifest"))
        st, body = post(port, "/library/sync")
        started = json.loads(body or b"{}").get("started")
        happened = wait_until(lambda: len(plat.hits("/api/sync/manifest")) > n0, 5)
        check("live agent: POST /library/sync -> started=true and the pass really happened",
              st == 200 and started is True and bool(happened),
              f"{st} {body!r} pass={bool(happened)}")
    finally:
        ctl.mirror.stop(timeout=5)
        srv.shutdown()
        plat.stop_server()


def http_status(port, path) -> int:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request("GET", path)
        r = c.getresponse()
        r.read()
        return r.status
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# 5. stop interrupts a pass in the middle of a slot                           #
# --------------------------------------------------------------------------- #

BIG = 3 * 1024 * 1024


def _stop_platform() -> Platform:
    """a-first is small, it finishes whole. z-big is a slot in bits: without
    a stop the transfer would go about two seconds. zz-after is a report that
    a stopped pass must not reach."""
    plat = Platform()
    plat.add("a-first", {0: blob("first", 3000)})
    plat.add("z-big", {0: blob("big", BIG)})
    plat.add("zz-after", {0: blob("after", 3000)})
    plat.trickle[("z-big", 0)] = (32 * 1024, 0.02, BIG // 3)
    return plat


def _audit_stopped(tag: str, plat: Platform, elapsed, alive):
    partial, broken, committed = audit_disk(plat)
    check(f"{tag}: the pass finished quickly after stop (the thread did not hang)",
          not alive and elapsed < 4.0, f"alive={alive} elapsed={elapsed:.1f}")
    check(f"{tag}: the client cut the slot download (the platform did not serve it whole)",
          not plat.slot_done.get(("z-big", 0)), "the slot reached the end: stop did not work")
    check(f"{tag}: the pass did not reach the next report",
          not plat.hits("/render/zz-after"), repr(plat.log))
    check(f"{tag}: there is no stump on disk -- a file whose bytes are only the beginning of what was served",
          not partial, repr(partial))
    check(f"{tag}: the interrupted report is not committed as a successful version",
          "z-big" not in committed, repr(committed))
    check(f"{tag}: everything committed equals what the platform served, byte for byte",
          not broken, repr(broken))
    check(f"{tag}: the report finished before the stop is whole and committed",
          "a-first" in committed, repr(committed))


def test_stop():
    # a) via the worker: "Exit" in the icon -> MirrorWorker.stop() -> the stop event.
    env = fresh_env()
    plat = _stop_platform()
    base = start_platform(plat)
    wk = Worker(env, base)
    wk.start()
    wk.sync_now()
    reached = plat.mid.wait(15)
    check("stop (worker): the pass reached the middle of a slot download", reached)
    t0 = time.monotonic()
    wk.stop(timeout=10)
    elapsed = time.monotonic() - t0
    alive = any(t.name == "op-mirror-sync" and t.is_alive() for t in threading.enumerate())
    _REAL_SLEEP(0.3)                      # let the server notice the drop
    _audit_stopped("stop (worker)", plat, elapsed, alive)
    snap = wk.snapshot()
    failure_kinds = {schedule.M_UNAVAILABLE, schedule.M_NO_DISK, schedule.M_KEY_EXPIRED,
                     schedule.M_KEY_REJECTED}
    check("stop (worker): the stop was not shown to the person as a failure",
          snap["kind"] not in failure_kinds, str(snap))
    check("stop (worker): the stopped pass is not declared fresh and last_ok is not set",
          snap["kind"] != schedule.M_FRESH and not snap.get("last_ok"), str(snap))
    check("stop (worker): the log has no \"failure\" about the stop",
          not any("[mirror] failure (" in l for l in wk.lines), repr(wk.lines))
    plat.stop_server()

    # b) direct call of sync_all with an event: the contract "must check between
    # files and return" without the schedule's involvement.
    env = fresh_env()
    plat = _stop_platform()
    base = start_platform(plat)
    ev = threading.Event()
    box: dict = {}

    def target():
        try:
            box["result"] = sync.sync_all(base, KEY, progress=None, stop=ev)
        except BaseException as e:                       # noqa: BLE001
            box["error"] = e

    th = threading.Thread(target=target, daemon=True)
    th.start()
    reached = plat.mid.wait(15)
    check("stop (direct call): the pass reached the middle of a slot download", reached)
    t0 = time.monotonic()
    ev.set()
    th.join(10)
    elapsed = time.monotonic() - t0
    _REAL_SLEEP(0.3)
    _audit_stopped("stop (direct call)", plat, elapsed, th.is_alive())
    plat.stop_server()


# --------------------------------------------------------------------------- #
# 6. Boundaries                                                               #
# --------------------------------------------------------------------------- #

def leaked(text: str) -> bool:
    return KEY in text


def test_key_rejected():
    # a) the key was never accepted -> "not accepted", not "fresh" and not "server".
    env = fresh_env()
    plat = Platform()
    plat.add("rep-a")
    plat.manifest_status = 401
    base = start_platform(plat)
    wk = Worker(env, base)
    snap, done = run_pass(wk, plat)
    check("401 (key never accepted): the pass finished", done, str(snap))
    check("401 (key never accepted): the kind is \"key not accepted\" (key_rejected)",
          snap["kind"] == schedule.M_KEY_REJECTED, str(snap))
    check("401: not \"fresh\" and last_ok is not set",
          snap["kind"] != schedule.M_FRESH and not snap.get("last_ok"), str(snap))
    check("401: scheduled retries are not planned (a dead key will not come back by itself)",
          not snap.get("retry_in_s"), str(snap))
    check("401: the kind has a text for the person", bool(schedule.status_text(snap)))
    check("401: the page gets status=key_rejected", page_block(wk).get("status") == "key_rejected",
          str(page_block(wk)))
    check("401: the key did not get into the worker's log", not leaked("\n".join(wk.lines)))
    check("401: the key did not land in the schedule state file",
          not leaked((env / "sched.json").read_text() if (env / "sched.json").exists() else ""))
    wk.stop(timeout=3)

    # direct call: the exception and traceback carry no key (the platform
    # deliberately echoes it in the body and in the status line).
    fn = getattr(sync, "sync_all", None)
    text = ""
    if callable(fn):
        try:
            r = fn(base, KEY, progress=None, stop=None)
            text = repr(r)
        except BaseException as e:                       # noqa: BLE001
            text = "".join(traceback.format_exception(e)) + repr(e) + repr(e.args)
    check("401: the exception/result/traceback of sync_all contain no key",
          callable(fn) and not leaked(text), text[:200])
    plat.stop_server()

    # b) the key worked, then died -> "expired", copies untouched.
    env = fresh_env()
    plat = Platform()
    plat.add("rep-a", {0: blob("a0", 4000)})
    base = start_platform(plat)
    wk = Worker(env, base)
    snap, done = run_pass(wk, plat)
    check("401 (key worked): the first pass is fresh", snap["kind"] == schedule.M_FRESH, str(snap))
    last_ok = snap.get("last_ok")
    plat.manifest_status = 401
    a0 = activity_of(wk, plat)
    wk.sync_now()
    snap, done = wait_pass(wk, plat, a0)
    check("401 (key worked): the kind is \"key expired\" (key_expired)",
          snap["kind"] == schedule.M_KEY_EXPIRED, str(snap))
    check("401 (key worked): the time of the last successful check is not lost",
          snap.get("last_ok") == last_ok and bool(last_ok), str(snap))
    check("401 (key worked): the page gets status=key_expired",
          page_block(wk).get("status") == "key_expired", str(page_block(wk)))
    check("401 (key worked): the downloaded copy is in place and intact byte for byte",
          same_on_disk(plat, ["rep-a"]), str(audit_disk(plat)))
    wk.stop(timeout=3)
    plat.stop_server()

    # c) the key died in the middle of a pass (manifest accepted, slot -- 401).
    env = fresh_env()
    plat = Platform()
    plat.add("rep-a", {0: blob("a0", 4000)})
    plat.slot_status = 401
    base = start_platform(plat)
    wk = Worker(env, base)
    snap, done = run_pass(wk, plat)
    check("401 mid-pass: the kind is key dead (key_rejected/key_expired), not \"fresh\"",
          snap["kind"] in schedule.KEY_DEAD, str(snap))
    check("401 mid-pass: the key did not get into the log", not leaked("\n".join(wk.lines)))
    wk.stop(timeout=3)
    plat.stop_server()


def test_unavailable():
    # a) the platform was alive, a copy exists, then the port is closed.
    env = fresh_env()
    plat = Platform()
    plat.add("rep-a", {0: blob("a0", 4000)})
    base = start_platform(plat)
    wk = Worker(env, base)
    snap, done = run_pass(wk, plat)
    check("unavailability: the first pass is fresh", snap["kind"] == schedule.M_FRESH, str(snap))
    last_ok = snap.get("last_ok")
    plat.stop_server()
    with fast_backoff():
        a0 = activity_of(wk, None)
        wk.sync_now()
        snap, done = wait_pass(wk, None, a0)
    check("unavailability (port closed): the pass finished", done, str(snap))
    check("unavailability (port closed): the kind is \"server unavailable\", not \"fresh\"",
          snap["kind"] == schedule.M_UNAVAILABLE, str(snap))
    check("unavailability: a retry is scheduled (retry_in_s > 0)",
          bool(snap.get("retry_in_s")) and snap["retry_in_s"] > 0, str(snap))
    check("unavailability: last_ok stayed the same -- no freshness was added",
          snap.get("last_ok") == last_ok, str(snap))
    check("unavailability: the kind has a text for the person", bool(schedule.status_text(snap)))
    block = page_block(wk)
    check("unavailability: the page gets status=unavailable and minutes until the retry",
          block.get("status") == "unavailable" and block.get("next_retry_min", 0) >= 1,
          str(block))
    check("unavailability: the copy downloaded earlier is intact (this very case is what it is all for)",
          audit_disk(plat)[0] == [] and store.load_state("rep-a").get("server_version") == 1)
    wk.stop(timeout=3)

    # b) the platform answers 503 to the manifest.
    env = fresh_env()
    plat = Platform()
    plat.add("rep-a")
    plat.manifest_status = 503
    base = start_platform(plat)
    wk = Worker(env, base)
    with fast_backoff():
        snap, done = run_pass(wk, plat)
    check("unavailability (HTTP 503): the kind is \"server unavailable\"",
          snap["kind"] == schedule.M_UNAVAILABLE, str(snap))
    check("unavailability (HTTP 503): the key did not get into the log (echo in body and status)",
          not leaked("\n".join(wk.lines)))
    fn = getattr(sync, "sync_all", None)
    text = ""
    if callable(fn):
        with fast_backoff():
            try:
                text = repr(fn(base, KEY, progress=None, stop=None))
            except BaseException as e:                   # noqa: BLE001
                text = "".join(traceback.format_exception(e)) + repr(e.args)
    check("unavailability (HTTP 503): the key is neither in the exception nor in the traceback",
          callable(fn) and not leaked(text), text[:200])
    wk.stop(timeout=3)
    plat.stop_server()

    # c) the platform address does not answer from the very start (nothing downloaded).
    env = fresh_env()
    wk = Worker(env, dead_base())
    with fast_backoff():
        snap, done = run_pass(wk, None)
    check("unavailability (from scratch): the kind is \"server unavailable\"",
          snap["kind"] == schedule.M_UNAVAILABLE, str(snap))
    check("unavailability (from scratch): last_ok is empty -- \"fresh\" is not shown",
          not snap.get("last_ok"), str(snap))
    wk.stop(timeout=3)


def test_mirror_quota():
    # The mirror ceiling is 4 GB. The manifest promises a 5 GB slot: the synchronizer
    # must refuse BEFORE downloading. There is no need to send 5 GB -- the `size`
    # promise on which `_check_room` fires is enough.
    env = fresh_env()
    plat = Platform()
    plat.add("rep-huge", {0: blob("h0", 2000)}, size_override=5 * 1024 ** 3)
    base = start_platform(plat)
    wk = Worker(env, base)
    snap, done = run_pass(wk, plat)
    own = snap["kind"] not in {schedule.M_FRESH, schedule.M_IDLE, schedule.M_SYNCING,
                               schedule.M_UNAVAILABLE, schedule.M_NO_KEY, schedule.M_NO_DISK,
                               *schedule.KEY_DEAD}
    check("ceiling: the pass finished", done, str(snap))
    check("ceiling: the kind is its OWN -- not fresh, not \"server unavailable\", not \"key\", not \"disk full\"",
          own, f"kind={snap['kind']!r}: the mirror ceiling and a lack of disk space are "
               f"different causes with different actions for the person")
    check("ceiling: the kind has a text for the person (otherwise the icon is silent)",
          bool(schedule.status_text(snap)), f"kind={snap['kind']!r}")
    block = page_block(wk)
    known = page_statuses() - {"idle", "running"}
    check("ceiling: the page gets a status it can draw",
          block.get("status") in known and block.get("status") not in
          {"unavailable", "key_expired", "key_rejected", "no_disk_space", "no_key"},
          f"status={block.get('status')!r}, the page knows {sorted(known)}")
    check("ceiling: the 5 GB slot was not requested (refusal before download)",
          not plat.hits("/render/rep-huge/files"), repr(plat.log))
    wk.stop(timeout=3)
    plat.stop_server()


# --------------------------------------------------------------------------- #

def test_slot_encoding_is_taken_from_manifest():
    """The server must trust the INVENTORY, not the slot name's extension.

    The most expensive gap found, and the only one that gives not a refusal but
    silently wrong numbers. The synchronizer puts on disk exactly the bytes the
    server sent and writes the received `Content-Encoding` into `record["enc"]`
    (decision 1 in the docstring of `sync`). The server compresses the slot ON
    THE FLY when the client asks, and the synchronizer asks for `br, gzip`
    (`SLOT_ACCEPT_ENCODING`). So a slot with a name without `.gz` may well lie
    compressed, and it cannot be seen from the name in any way. As long as
    serving derived the encoding from the extension, the report got gzip
    disguised as `identity` -- exactly at the moment when the server was down
    and there was nothing to check the numbers against.

    The fixtures of all earlier tests carried `.gz`/`.br` in the name, so the gap
    was green for both halves. Here the name DELIBERATELY says nothing about compression.
    """
    from agent.mirror import serve                      # locally: only here

    fresh_env()                        # LOCALAPPDATA -> its own temporary directory
    vdir = layout.server_version_dir("rep-enc", 1, create=True)
    raw = b"col_a,col_b\n1,2\n" * 64
    packed = gzip.compress(raw)
    (vdir / "data1.bin").write_bytes(packed)

    # The inventory is as the synchronizer writes it: the name WITHOUT a
    # compression extension, but `enc` honestly says that gzip is on disk.
    state = layout.new_state("rep-enc")
    state["server_version"] = 1
    state["slots"] = [{"slot": 0, "name": "data1.bin", "file": "data1.bin",
                       "enc": "gzip", "etag": '"e1"', "bytes": len(packed)}]
    layout.write_state("rep-enc", state)

    req = layout.parse_local_path("/mirror/rep-enc/files/0")
    target = serve.resolve(req, state)
    check("slot encoding is taken from the inventory, not from the name's extension",
          target.enc == "gzip", f"enc={target.enc!r} (the name data1.bin says nothing about gzip)")

    # And the other way round: the name promises .gz, and the inventory says "not
    # compressed" -- so what is on disk is unpacked, and declaring gzip is wrong.
    state["slots"] = [{"slot": 0, "name": "data2.bin.gz", "file": "data2.bin.gz",
                       "enc": None, "etag": '"e2"', "bytes": len(raw)}]
    (vdir / "data2.bin.gz").write_bytes(raw)
    layout.write_state("rep-enc", state)
    target = serve.resolve(layout.parse_local_path("/mirror/rep-enc/files/0"), state)
    check("the inventory is stronger than the name even when it says \"not compressed\"",
          target.enc is None, f"enc={target.enc!r} (what is on disk is unpacked)")

    # A record without the `enc` field (a foreign or old inventory) -- the fallback is by name.
    state["slots"] = [{"slot": 0, "name": "data2.bin.gz", "file": "data2.bin.gz",
                       "etag": '"e3"'}]
    layout.write_state("rep-enc", state)
    target = serve.resolve(layout.parse_local_path("/mirror/rep-enc/files/0"), state)
    check("without the `enc` field the encoding is still derived from the name",
          target.enc == "gzip", f"enc={target.enc!r}")


def main() -> int:
    buf: list[str] = []
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Tee(real_out, buf), _Tee(real_err, buf)
    try:
        for fn in (test_entry_contract, test_controller_worker_enabled,
                   test_sync_now_honest, test_running_texts, test_progress, test_stop,
                   test_key_rejected, test_unavailable, test_mirror_quota,
                   test_slot_encoding_is_taken_from_manifest):
            print(f"--- {fn.__name__}")
            try:
                fn()
            except Exception:  # noqa: BLE001
                check(f"{fn.__name__} did not fail with an exception", False,
                      "\n" + traceback.format_exc())
        print("--- key over the whole run")
        out = "".join(buf)
        check("key: it is in no console line over the whole run", KEY not in out)
    finally:
        sys.stdout, sys.stderr = real_out, real_err
        os.environ.pop("LOCALAPPDATA", None)
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    total = _PASS + _FAIL
    print(f"\n{_PASS}/{total}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
