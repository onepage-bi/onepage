"""Smoke test of the mirror's WIRING to the agent server. Standard library only.

`smoke_mirror.py` raises ITS OWN `ThreadingHTTPServer` and calls `serve.serve()`
directly, so it checks delivery, but not that it can be reached at all:
a mirror not wired into `AgentHandler` passed all 89 checks and still
did not open in any browser. This test goes through a REAL `AgentServer`
— the same class and the same handler as in `__main__` — and walks the addresses
of the contract: `/library/...` (the shell) and `/mirror/<id>/` (report delivery).

The mirror is seeded straight onto disk through `layout`/`store`, without a
platform stub: the synchronizer is not the subject of the check here, and no network is needed.
`LOCALAPPDATA` points to a temporary directory, `~/.op-agent` is not touched.

The last three scenarios (7–9) close defects found after the first
"done": `null` in time fields (the page printed "31.12.1969"), a kit without
`mirror/` or without `strings.json` (the library opened empty and silent), and the branch
without a schedule (`data.json` and `POST /library/sync` pretended to be a working
sync). The schedule-synchronizer joint is in `smoke_mirror_sync.py`.

Run:  python -m agent.tests.smoke_mirror_wiring   (from the OnePage repo root)
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

# Key file in the dev store on non-Windows: without it CredStore refuses, and
# the "key accepted" check would be red for a reason unrelated to the mirror.
os.environ.setdefault("OP_AGENT_DEV_CRED_STORE", "1")

from agent.config import Config                                    # noqa: E402
from agent.creds import CredStore                                  # noqa: E402
from agent.mirror import assets, layout, schedule, store           # noqa: E402
from agent.pairing import Pairing                                  # noqa: E402
from agent.server import AgentServer                               # noqa: E402

_PASS = 0
_FAIL = 0

# A recognizable key: we look for it as a substring in responses, the log and captured output.
KEY = "OPKEY-7c2e55d1-WIRING-SECRET-hv40"

# Content markers: they show WHAT exactly was served, without peering at the markup.
DOC_OK = "WIRING-DOC-OK-MARKER"
DOC_RLS = "WIRING-DOC-RLS-MARKER"

ID_OK = "wire-ok"
ID_RLS_STATE = "wire-rls-state"     # in the mirror, but with rights.rls_enabled
ID_RLS_BLOCKED = "wire-rls-blocked"  # on the ban list, nothing on disk

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="smoke_mirror_wiring_"))
_COUNTER = 0


def check(label, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"PASS {label}")
    else:
        _FAIL += 1
        print(f"FAIL {label}  {extra}")


# --------------------------------------------------------------------------- #
# Stand: a real agent + a seeded mirror                                       #
# --------------------------------------------------------------------------- #

def fresh_env() -> Path:
    global _COUNTER
    _COUNTER += 1
    d = _TMP_ROOT / f"s{_COUNTER}"
    d.mkdir()
    os.environ["LOCALAPPDATA"] = str(d)
    return d


def seed(dashboard_id: str, doc: str, *, rls_in_state: bool = False) -> None:
    """Put one report into the mirror the way the synchronizer leaves it."""
    st = store.fresh_state(dashboard_id, title="T " + dashboard_id, dash_type="html")
    st["server_version"] = 1
    st["fingerprint"] = "fp1"
    st["rights"]["rls_enabled"] = rls_in_state
    vdir = store.ensure_version_dir(dashboard_id, 1)
    store.write_bytes(vdir / layout.DOCUMENT_NAME,
                      f"<html><body>{doc}</body></html>".encode())
    store.save_state(dashboard_id, st)


def start_agent(env: Path):
    cfg = Config(allowed_origins=("https://onepage.example",), connections={})
    pairing = Pairing(code_factory=lambda: "123456")
    creds = CredStore(root=env)
    srv = AgentServer(("127.0.0.1", 0), cfg, pairing, cred_store=creds)
    srv.state_root = env
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1], creds


def raw(port, path, *, method="GET", host="default", body=None, headers=None):
    """A raw request: the path goes out as is, without normalization by the client."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", f"127.0.0.1:{port}" if host == "default" else host)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        if body is not None:
            c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        r = c.getresponse()
        data = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data
    finally:
        c.close()


def web_dir() -> Path:
    p = assets.res_path("library.html", web=True)
    return p.parent if p else Path("/nonexistent")


# --------------------------------------------------------------------------- #
# 1. The shell answers                                                        #
# --------------------------------------------------------------------------- #

def test_shell_served():
    env = fresh_env()
    srv, port, _ = start_agent(env)
    try:
        s, h, b = raw(port, "/library/")
        check("shell: /library/ serves a page", s == 200 and b"<html" in b.lower(),
              f"{s} {b[:80]!r}")
        check("shell: /library/ is text/html",
              h.get("content-type", "").startswith("text/html"), h.get("content-type"))
        s, h, b = raw(port, "/library/view/")
        check("shell: /library/view/ serves the viewer",
              s == 200 and b"<html" in b.lower(), f"{s}")
        for name in ("common.js", "library.js", "viewer.js"):
            s, h, b = raw(port, "/library/" + name)
            ct = h.get("content-type", "")
            check(f"shell: /library/{name} is text/javascript",
                  s == 200 and ct.startswith("text/javascript"), f"{s} {ct}")
        # The window icon. Edge --app takes it from the PAGE favicon, not from
        # the shortcut, and without it shows a globe — the program looks foreign.
        # The drawing must match the shortcut, so we compare the numbers: the SVG and
        # tools/make_app_icon.py draw the same thing by different means.
        st_i, h_i, b_i = raw(port, "/library/icon.svg")
        check("shell: /library/icon.svg is image/svg+xml",
              st_i == 200 and "image/svg+xml" in h_i.get("content-type", ""),
              (st_i, h_i.get("content-type")))
        svg = b_i.decode("utf-8")
        ico_src = (Path(__file__).resolve().parents[2]
                   / "tools" / "make_app_icon.py").read_text("utf-8")
        for name, token in (("background blue", "0x1F, 0x5F, 0xBF"),
                            ("corner fold", "0xBF, 0xD4, 0xF2")):
            check(f"the {name} color is the same as in the shortcut", token in ico_src)
        check("the window icon paints with the same blue", "#1F5FBF" in svg)
        check("the window icon paints with the same fold color", "#BFD4F2" in svg)
        for num in ("0.26", "0.74", "0.16", "0.84", "0.40", "0.54", "0.68", "0.055"):
            check(f"geometry {num} matches the shortcut",
                  num in svg and num.lstrip("0") in ico_src.replace("0.", "."),
                  num)
        check("the library page declares an icon",
              'rel="icon"' in (Path(__file__).resolve().parents[1]
                               / "mirror" / "web" / "library.html").read_text("utf-8"))

        s, h, b = raw(port, "/library/mirror.css")
        check("shell: /library/mirror.css is text/css",
              s == 200 and h.get("content-type", "").startswith("text/css"),
              f"{s} {h.get('content-type')}")
        s, h, b = raw(port, "/library/strings.json")
        try:
            d = json.loads(b)
        except ValueError:
            d = None
        check("shell: strings.json is a non-empty dictionary of strings",
              s == 200 and isinstance(d, dict) and len(d) > 0, f"{s} {b[:80]!r}")
        s, h, b = raw(port, "/library/data.json")
        try:
            d = json.loads(b)
        except ValueError:
            d = None
        check("shell: data.json is a JSON object with an items list",
              s == 200 and isinstance(d, dict) and isinstance(d.get("items"), list),
              f"{s} {b[:120]!r}")
        check("shell: data.json is not cached",
              "no-store" in h.get("cache-control", "") or "no-cache" in h.get("cache-control", ""),
              h.get("cache-control"))
        # The pages link to resources strictly inside /library/ — there are no external ones.
        s, h, b = raw(port, "/library/")
        check("shell: the page has no external links (http://, https://, //)",
              s == 200 and b"http://" not in b and b"https://" not in b
              and b'src="//' not in b and b'href="//' not in b)
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# 2. Report delivery is wired in                                              #
# --------------------------------------------------------------------------- #

def test_report_served_through_agent():
    env = fresh_env()
    seed(ID_OK, DOC_OK)
    srv, port, _ = start_agent(env)
    try:
        s, h, b = raw(port, f"/mirror/{ID_OK}/")
        check("delivery: /mirror/<id>/ serves the document, not a 404",
              s == 200 and DOC_OK.encode() in b, f"{s} {b[:120]!r}")
        s, h, b = raw(port, f"/mirror/{ID_OK}")
        check("delivery: without a slash — a redirect to the address with a slash",
              s == 307 and h.get("location", "").endswith(f"/mirror/{ID_OK}/"),
              f"{s} {h.get('location')}")
        s, h, b = raw(port, "/mirror/no-such-report/")
        check("delivery: an unseeded report is a 404 (not a 500 and not silence)", s == 404, str(s))
        s, h, b = raw(port, f"/mirror/{ID_OK}/", method="POST", body=b"{}")
        check("delivery: POST to a report is 405, not a 404 from the agent's router",
              s == 405, str(s))
        # The agent's old routes are not broken by wiring in the new ones.
        s, h, b = raw(port, "/health")
        check("agent: /health still answers", s == 200 and b"app" in b, str(s))
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# 3. A name from the address does not become a path                           #
# --------------------------------------------------------------------------- #

def test_no_path_traversal():
    env = fresh_env()
    seed(ID_OK, DOC_OK)
    srv, port, _ = start_agent(env)
    try:
        wd = web_dir()
        outside = [p for p in wd.parent.iterdir() if p.is_file()]   # strings.json, *.py
        # Contents of files outside web/: if it ever appears in a body, it is a leak.
        needles = [p.read_bytes() for p in outside if p.stat().st_size > 0]
        needles = [n for n in needles if len(n) >= 16]
        check("traversal: there is something to compare (files next to web/)", bool(needles),
              str(wd.parent))
        # Without a live shell, a 404 on "bad" addresses would be not protection but
        # the absence of a route, and all the checks below would pass in vain.
        live = raw(port, "/library/common.js")[0] == 200
        check("traversal: control — the honest name /library/common.js is served", live)
        bad = [
            "/library/../strings.json",
            "/library/..%2fstrings.json",
            "/library/..%2Fx",
            "/library/%2e%2e/strings.json",
            "/library/%2e%2e%2fserve.py",
            "/library/..%5cstrings.json",
            "/library/..\\strings.json",
            "/library/web",
            "/library/web/",
            "/library/web/common.js",
            "/library/sub/common.js",
            "/library/common.js/",
            "/library/common.js/x",
            "/library/strings.json/x",
            "/library/serve.py",
            "/library/layout.py",
            "/library/library.html",
            "/library/%00common.js",
            "/library/common.js%00.png",
            "/library/.hidden",
            "/library/view/../library.html",
            "/library/view/..%2fcommon.js",
            "/library//etc/passwd",
        ]
        for path in bad:
            s, h, b = raw(port, path)
            leaked = [i for i, n in enumerate(needles) if n in b]
            check(f"traversal: {path} -> 404 and nothing from outside web/",
                  live and s == 404 and not leaked, f"{s} leaked={leaked} {b[:60]!r}")
        # Resources not in web/ but lying in mirror/: strings.json is honestly
        # available ONLY under its own name, not as "any file of the directory".
        s, h, b = raw(port, "/library/strings.json")
        check("traversal: strings.json under its own name is still served", s == 200, str(s))
        # Report delivery: an asset name is not a path either.
        for path in (f"/mirror/{ID_OK}/..%2fstate.json",
                     f"/mirror/{ID_OK}/../{ID_OK}/state.json",
                     f"/mirror/{ID_OK}/%2e%2e/state.json",
                     f"/mirror/{ID_OK}/state.json"):
            s, h, b = raw(port, path)
            check(f"traversal: {path} does not serve state.json",
                  live and (s != 200 or b"server_version" not in b), f"{s} {b[:60]!r}")
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# 4. The key does not leak                                                    #
# --------------------------------------------------------------------------- #

def test_key_not_leaked():
    env = fresh_env()
    seed(ID_OK, DOC_OK)
    sink = io.StringIO()
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    srv, port, creds = start_agent(env)
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            s, h, b = raw(port, "/library/key", method="POST",
                          body=json.dumps({"key": KEY}).encode(),
                          headers={"Content-Type": "application/json"})
            post_status, post_body = s, b
            # A key the agent did not accept must not get into the log either.
            raw(port, "/library/key", method="POST",
                body=json.dumps({"key": "x" * 3 + KEY + "\n"}).encode(),
                headers={"Content-Type": "application/json"})
            raw(port, "/library/key", method="POST", body=b"{not json " + KEY.encode())
            s, h, data = raw(port, "/library/data.json")
            s2, h2, strings = raw(port, "/library/strings.json")
            s3, h3, page = raw(port, "/library/")
            s4, h4, doc = raw(port, f"/mirror/{ID_OK}/")
        check("key: POST /library/key accepted (2xx)", 200 <= post_status < 300,
              f"{post_status} {post_body[:80]!r}")
        accepted = 200 <= post_status < 300   # without acceptance the leak checks are empty
        check("key: the POST response does not contain the key",
              accepted and KEY.encode() not in post_body)
        check("key: /library/data.json does not contain the key",
              accepted and s == 200 and KEY.encode() not in data,
              data[:120].decode("utf-8", "replace"))
        check("key: neither the shell, nor strings.json, nor delivery contain the key",
              accepted and s2 == 200 and s3 == 200 and s4 == 200
              and all(KEY.encode() not in x for x in (strings, page, doc)))
        out = sink.getvalue() + log_stream.getvalue()
        check("key: the agent's console and log do not have the key",
              accepted and KEY not in out,
              "leak into stdout/stderr/logging")
        # The key went into the protected store, not an ordinary state file.
        plain = []
        for p in env.rglob("*"):
            if p.is_file():
                try:
                    if KEY.encode() in p.read_bytes():
                        plain.append(p.relative_to(env).as_posix())
                except OSError:
                    pass
        check("key: no state file keeps it in plain text",
              accepted and not plain, repr(plain))
    finally:
        srv.shutdown()
        root.removeHandler(handler)
        root.setLevel(old_level)


# --------------------------------------------------------------------------- #
# 5. _host_ok on the new routes                                               #
# --------------------------------------------------------------------------- #

def test_host_check_on_new_routes():
    env = fresh_env()
    seed(ID_OK, DOC_OK)
    srv, port, creds = start_agent(env)
    try:
        evil = f"attacker.example:{port}"
        for path in ("/library/", "/library/view/", "/library/common.js",
                     "/library/strings.json", "/library/data.json",
                     f"/mirror/{ID_OK}/"):
            ctl = raw(port, path)[0]      # control: with its own Host the route is alive
            s, h, b = raw(port, path, host=evil)
            check(f"host: GET {path} with a foreign Host -> 421",
                  ctl == 200 and s == 421 and DOC_OK.encode() not in b,
                  f"control={ctl} {s} {b[:60]!r}")
        for path in ("/library/key", "/library/sync"):
            ctl = raw(port, path, method="POST", body=b"{}")[0]
            s, h, b = raw(port, path, method="POST", host=evil,
                          body=json.dumps({"key": KEY}).encode())
            # Control: with its own Host the route exists (not 404) — otherwise 421
            # here would only prove that _host_ok stands at the start of do_POST.
            check(f"host: POST {path} with a foreign Host -> 421",
                  ctl != 404 and s == 421, f"control={ctl} {s}")
        # A refusal must have no side effect: a key from a foreign Host is not saved.
        s = creds.has_password("onepage-device-key")
        check("host: a key sent from a foreign Host is not saved",
              not s and not creds.list_connections())
        # And its own Host (localhost is loopback too) works — the check is not "everything closed".
        s, h, b = raw(port, "/library/", host=f"localhost:{port}")
        check("host: localhost:<port> is allowed", s == 200, str(s))
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# 6. The RLS boundary                                                         #
# --------------------------------------------------------------------------- #

def test_rls_boundary():
    env = fresh_env()
    seed(ID_OK, DOC_OK)
    # a) a report that GOT onto disk with rls_enabled (a corrupted state)
    seed(ID_RLS_STATE, DOC_RLS, rls_in_state=True)
    # b) a report the synchronizer recorded a ban for, with nothing on disk
    store.mark_blocked(ID_RLS_BLOCKED, "T " + ID_RLS_BLOCKED, "rls", "2026-09-01T10:00:00")
    srv, port, _ = start_agent(env)
    try:
        for did in (ID_RLS_STATE, ID_RLS_BLOCKED):
            s, h, b = raw(port, f"/mirror/{did}/")
            ctl = raw(port, f"/mirror/{ID_OK}/")[0]   # delivery is wired in at all
            check(f"RLS: delivery {did} does not serve the document",
                  ctl == 200 and s != 200 and DOC_RLS.encode() not in b,
                  f"control={ctl} {s} {b[:80]!r}")
        s, h, b = raw(port, f"/mirror/{ID_RLS_STATE}/state.json")
        check("RLS: the forbidden report's service files are not served either",
              raw(port, f"/mirror/{ID_OK}/")[0] == 200 and s != 200, str(s))

        s, h, data = raw(port, "/library/data.json")
        try:
            d = json.loads(data)
        except ValueError:
            d = {}
        items = d.get("items") if isinstance(d, dict) else None
        items = items if isinstance(items, list) else []
        by_id = {i.get("dashboard_id"): i for i in items if isinstance(i, dict)}
        check("RLS: the ordinary report is in the library", ID_OK in by_id, repr(list(by_id)))
        # The forbidden one may be in the "skipped" list with a reason, but NOT as an
        # openable one: the shell must not lead a person to its document.
        for did in (ID_RLS_STATE, ID_RLS_BLOCKED):
            it = by_id.get(did)
            openable = bool(it) and not any(
                it.get(k) for k in ("blocked", "skipped", "mirrorable") if k in it
                and k != "mirrorable") and it.get("mirrorable", True) is not False
            check(f"RLS: {did} is not given out by the shell as openable",
                  ID_OK in by_id and not openable, repr(it))
        check("RLS: the forbidden document's content is not in data.json",
              s == 200 and DOC_RLS.encode() not in data)
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# 7. Time fields: not a single null                                           #
# --------------------------------------------------------------------------- #

# What counts as a time field: the name, not the value — a null cannot be recognized by
# value. The page feeds such fields into `new Date(x)`, and `new Date(null)` is
# 01.01.1970, i.e. "31.12.1969" in a zone west of Greenwich: a false date
# where blank should be. A defect that already happened, not a hypothesis.
_TIME_KEY = re.compile(r"(_at|_ms|_ts|_time|_date)$|^(when|time|date|timestamp)$")
# The first decade of millisecond stamps: anything smaller is either 0 (the same
# epoch) or seconds fed as milliseconds (a date in January 1970).
_MIN_EPOCH_MS = 10 ** 11


def time_fields(obj, path="$"):
    """(path, value) of every time field in a JSON tree."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            here = f"{path}.{k}"
            if isinstance(k, str) and _TIME_KEY.search(k):
                yield here, v
            yield from time_fields(v, here)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from time_fields(v, f"{path}[{i}]")


def audit_times(label, data):
    fields = list(time_fields(data))
    nulls = [p for p, v in fields if v is None]
    check(f"time ({label}): data.json has not a single null in the time fields",
          not nulls, f"null in {nulls}")
    bad = [(p, v) for p, v in fields
           if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                                 or v < _MIN_EPOCH_MS)]
    check(f"time ({label}): every non-empty time field is epoch milliseconds, not 0 and not seconds",
          not bad, repr(bad))
    return fields


def test_no_null_times():
    env = fresh_env()
    seed(ID_OK, DOC_OK)                  # state without last_sync_at: nothing to fill fetched_at with
    seed("wire-dated", DOC_OK)
    st = store.load_state("wire-dated")
    st["last_sync_at"] = "2026-10-11T09:00:00"        # UTC without a zone, as the synchronizer writes it
    store.save_state("wire-dated", st)
    srv, port, creds = start_agent(env)
    try:
        # a) no icon, no controller, there has never been a sync.
        s, h, b = raw(port, "/library/data.json")
        data = json.loads(b)
        check("time (no controller): data.json served", s == 200 and bool(data.get("items")),
              f"{s}")
        audit_times("no controller", data)
        by_id = {i.get("dashboard_id"): i for i in data.get("items", [])}
        want = int(datetime(2026, 10, 11, 9, 0, 0, tzinfo=UTC).timestamp() * 1000)
        check("time: a known date is converted from UTC to milliseconds exactly",
              by_id.get("wire-dated", {}).get("fetched_at") == want,
              repr(by_id.get("wire-dated", {}).get("fetched_at")))

        # b) there is a controller, the schedule is sound, but it has never run yet.
        worker = schedule.MirrorWorker(creds, lambda: None,
                                       sync_fn=lambda *a, **k: None,
                                       persist_path=env / "sched.json",
                                       log=lambda m: None)
        srv.controller = SimpleNamespace(mirror=worker)
        s, h, b = raw(port, "/library/data.json")
        data = json.loads(b)
        check("time (worker with not a single sync): data.json served",
              s == 200 and isinstance(data.get("sync"), dict), f"{s}")
        audit_times("worker without syncs", data)

        # c) there was a successful one: last_sync_at is a number and a sane one.
        worker._last_ok = 1791709200.0
        s, h, b = raw(port, "/library/data.json")
        data = json.loads(b)
        audit_times("after a successful sync", data)
        check("time: last_sync_at after a successful sync is milliseconds",
              data.get("sync", {}).get("last_sync_at") == 1791709200000,
              repr(data.get("sync", {}).get("last_sync_at")))
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# 8. A kit without mirror/ or without strings.json: the refusal is visible    #
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def kit_layout(root: Path, *, mirror_dir: bool, strings: bool):
    """The kit layout as `assets.res_path` sees it: next to `op_agent.py`
    there is (or is not) a `mirror/` subdirectory with `strings.json` and `web/*`.

    `assets._roots` is substituted — the only seam through which the agent looks for
    resources; the modules' code is not touched. The schedule's text cache
    is reset, otherwise it would remember the strings of the package layout.
    """
    web_src = web_dir()
    strings_src = assets.strings_path()
    kit = root / ("kit-%d" % int(time.monotonic() * 1000))
    kit.mkdir()
    if mirror_dir:
        (kit / "mirror" / "web").mkdir(parents=True)
        for f in web_src.iterdir():
            if f.is_file():
                shutil.copy2(f, kit / "mirror" / "web" / f.name)
        if strings and strings_src:
            shutil.copy2(strings_src, kit / "mirror" / "strings.json")
    saved_roots, saved_cache = assets._roots, schedule._strings_cache
    assets._roots = lambda: (kit, kit / assets.RES_DIRNAME)
    schedule._strings_cache = None
    try:
        yield kit
    finally:
        assets._roots, schedule._strings_cache = saved_roots, saved_cache


def test_incomplete_kit_is_loud():
    env = fresh_env()
    seed(ID_OK, DOC_OK)
    check("kit: the assets._roots seam exists (otherwise the layout cannot be substituted in the test)",
          callable(getattr(assets, "_roots", None)))
    srv, port, _ = start_agent(env)
    try:
        # Control: the full kit works — otherwise the reds below would mean nothing.
        with kit_layout(_TMP_ROOT, mirror_dir=True, strings=True):
            s1 = raw(port, "/library/")
            s2 = raw(port, "/library/strings.json")
            check("kit (full): /library/ and strings.json are served",
                  s1[0] == 200 and s2[0] == 200 and b"<html" in s1[2].lower(),
                  f"{s1[0]} {s2[0]}")

        cases = (
            ("without the mirror/ directory", dict(mirror_dir=False, strings=False),
             ("strings.json", "mirror/", "mirror\\")),
            ("without strings.json", dict(mirror_dir=True, strings=False),
             ("strings.json",)),
        )
        for label, kw, names in cases:
            with kit_layout(_TMP_ROOT, **kw):
                check(f"kit ({label}): the agent is alive — /health answers",
                      raw(port, "/health")[0] == 200)
                for path in ("/library/", "/library/view/"):
                    s, h, b = raw(port, path)
                    text = b.decode("utf-8", "replace").lower()
                    named = any(n in text for n in names)
                    # What is NOT allowed: a 200 with a page that then silently gets a
                    # 404 on the texts and draws nameless buttons; and a 404 with a
                    # faceless JSON from which the person will not understand what to do.
                    # What IS allowed: any status, if the body names what is missing.
                    check(f"kit ({label}): {path} names what is missing "
                          f"(not silent and not an empty page)",
                          named, f"{s} {h.get('content-type')} {b[:100]!r}")
                s, h, b = raw(port, "/library/strings.json")
                check(f"kit ({label}): strings.json does not pretend to be alive (not a 200 with an empty body)",
                      not (s == 200 and not b.strip()), f"{s} {b[:60]!r}")
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# 9. The no-schedule branch: an honest status and an honest answer            #
# --------------------------------------------------------------------------- #

# Statuses each of which claims "the schedule works": idle,
# running, waiting for a retry, waiting for a key, etc. A status outside this set is a new,
# deliberately introduced state ("no schedule"), or an explicit flag.
_SCHEDULED_STATUSES = {"idle", "running", "unavailable", "no_key", "key_expired",
                       "key_rejected", "no_disk_space"}


def sync_block_honest(block: dict) -> bool:
    return (block.get("enabled") is False or block.get("scheduled") is False
            or block.get("status") not in _SCHEDULED_STATUSES)


class PlatformStub:
    """A platform that cares about one thing: whether the manifest arrived."""

    def __init__(self):
        self.hits = 0
        plat = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/api/sync/manifest"):
                    plat.hits += 1
                body = b'{"items": [], "rights_ttl_s": 3600}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _branch_without_schedule(label, srv, port, plat):
    st, _h, body = raw(port, "/library/key", method="POST",
                       body=json.dumps({"key": KEY}).encode(),
                       headers={"Content-Type": "application/json"})
    check(f"{label}: the key is accepted (otherwise the check below would be about a missing key)",
          200 <= st < 300, f"{st}")
    s, _h, b = raw(port, "/library/data.json")
    block = (json.loads(b) or {}).get("sync", {})
    s2, _h, b2 = raw(port, "/library/sync", method="POST", body=b"{}")
    try:
        started = json.loads(b2).get("started")
    except ValueError:
        started = None
    deadline = time.monotonic() + 2.5
    while plat.hits == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    really = plat.hits > 0
    # The response code is not prescribed: an honest refusal may be a 200 with
    # started=false, or a 503 with a reason. Only a parseable body with a boolean
    # `started` is prescribed — the page reads exactly that.
    check(f"{label}: POST /library/sync answers a parseable JSON with a boolean started",
          isinstance(started, bool), f"{s2} {b2[:80]!r}")
    check(f"{label}: the \"started\" answer matches what happened "
          f"(true — only if the platform received the request)",
          started is really, f"started={started!r}, platform received the manifest={really}")
    # A schedule that does not exist must not look like a working one. If it
    # exists (a pass took place), the "waiting" status is legitimate.
    check(f"{label}: the status in data.json does not pretend to be a working schedule",
          really or sync_block_honest(block),
          f"there is no schedule, and sync={block!r}")


def test_branch_without_schedule():
    plat = PlatformStub()
    saved_env = os.environ.get("OP_MIRROR_SERVER")
    os.environ["OP_MIRROR_SERVER"] = plat.base
    try:
        # a) the icon did not come up: server.controller is not set (as with --no-tray).
        env = fresh_env()
        srv, port, creds = start_agent(env)
        try:
            check("no schedule (no controller): the stand — controller is not set",
                  srv.controller is None)
            _branch_without_schedule("no schedule (no controller)", srv, port, plat)
        finally:
            srv.shutdown()

        # b) there is a controller, but the worker did not get a synchronizer — the schedule
        # is inoperable (`enabled` is false). This state can be obtained only by
        # removing the synchronizer: the "no module" path is built into schedule deliberately.
        plat.hits = 0
        env = fresh_env()
        srv, port, creds = start_agent(env)
        saved = schedule._sync
        schedule._sync = None
        try:
            worker = schedule.MirrorWorker(creds, lambda: plat.base,
                                           persist_path=env / "sched.json",
                                           log=lambda m: None)
        finally:
            schedule._sync = saved
        if worker.enabled:
            worker._sync_fn = None
        srv.controller = SimpleNamespace(mirror=worker)
        try:
            check("no schedule (worker disabled): the stand — enabled=False", worker.enabled is False)
            _branch_without_schedule("no schedule (worker disabled)", srv, port, plat)
        finally:
            srv.shutdown()
    finally:
        if saved_env is None:
            os.environ.pop("OP_MIRROR_SERVER", None)
        else:
            os.environ["OP_MIRROR_SERVER"] = saved_env
        plat.stop()


# --------------------------------------------------------------------------- #
# 11. the report update is available from the viewer HEADER, not only from the list#
# --------------------------------------------------------------------------- #

def test_viewer_update_action():
    """The "Update from folders" button must be where the question is asked.

    The question "where is the update button, then" was asked by the owner while INSIDE a report:
    on top is the date of yesterday's copy, there is no action, and the only way is to
    go back to the list. We check three things that the eye catches only
    on a live Windows: a place for the button in the viewer markup, the same
    mechanism (`U.updateNow` from common.js, not a second copy of the request) and that
    the action's POST still checks its loopback Origin.
    """
    web = Path(__file__).resolve().parents[1] / "mirror" / "web"
    html = (web / "viewer.html").read_text("utf-8")
    js = (web / "viewer.js").read_text("utf-8")
    common = (web / "common.js").read_text("utf-8")

    check("viewer: there is a place for actions in the header",
          'id="vacts"' in html, html[:200])
    check("viewer: there is a bar under the header for the reason and progress",
          'id="vnotice"' in html and 'id="vnotice-msg"' in html)
    check("viewer: the button is named by the same key as in the list",
          "action_update_from_folders" in js)

    # The owner's second request, after the first: "there is no button to sync with
    # onepage, a user inside a report cannot fetch the data from the
    # server". Contact with the platform used to appear ONLY as a "road" on
    # three refusals (expired rights, no bundle, no copy) — that is, at the
    # moment when it is already too late. Now it is a permanent action.
    check("viewer: contact with the platform is a permanent button, not only a road",
          js.count("action_sync_now") >= 2, str(js.count("action_sync_now")))
    check("viewer: sync is called by the shared code, there is no request of its own",
          "U.syncNow" in js and "/library/sync" not in js)
    check("viewer: the sync state is read from data.sync",
          "data.sync" in js)
    check("viewer: a disabled sync is explained, not silent",
          "sync_schedule_off" in js and "sync_no_key" in js and "sync_running" in js)
    check("viewer: without a key the button leads to where one is added",
          "action_add_key" in js)
    check("viewer: it calls the SHARED action, not its own",
          "U.updateNow(" in js and "updateNow: updateNow" in common)
    check("viewer: there is no second update request in it",
          "/library/local-run" not in js, "viewer.js sends the POST itself")
    check("shared module: the update request lives in exactly one place",
          common.count("'/library/local-run'") == 1)
    check("the list calls the same shared action",
          "U.updateNow(it, refresh)" in (web / "library.js").read_text("utf-8"))
    # An honest state: every `precheck` refusal code says something, and for
    # refusals from which there is a road, that road is named.
    check("viewer: a refusal is explained, not silent",
          "whyBlocked" in js and "U.errText(" in js)
    check("viewer: the road out of the folders refusal is the “Report folders” screen",
          "U.FOLDERS_URL" in js and "action_open_folders_ui" in js)
    check("the folders screen address is shared with the program window",
          "'/folders-ui/'" in common)
    check("viewer: the road out of the rights and bundle refusal is contact with the platform",
          "rights_expired" in js and "action_sync_now" in js)

    env = fresh_env()
    srv, port, _ = start_agent(env)
    try:
        seed("wire-upd", DOC_OK)
        s_ok, _, b_ok = raw(port, "/library/view/")
        check("the viewer is served with a place for the button",
              s_ok == 200 and b'id="vacts"' in b_ok, str(s_ok))

        body = json.dumps({"dashboard_id": "wire-upd"}).encode()
        # A foreign Origin is a refusal. The report in the viewer frame lives in a sandbox
        # with an opaque origin, and its `fetch` will not get here; a page from
        # a neighboring site even less so.
        st, _, b = raw(port, "/library/local-run", method="POST", body=body,
                       headers={"Content-Type": "application/json",
                                "Origin": "https://evil.example"})
        check("an update from a foreign Origin -> 403 bad_origin",
              st == 403 and json.loads(b).get("error") == "bad_origin", f"{st} {b[:120]!r}")
        st, _, b = raw(port, "/library/local-run", method="POST", body=body,
                       headers={"Content-Type": "application/json",
                                "Origin": "null"})
        check("an update from the report sandbox (Origin: null) -> 403",
              st == 403 and json.loads(b).get("error") == "bad_origin", f"{st} {b[:120]!r}")
        # Its own origin passes the check and gets an HONEST refusal on the merits:
        # this report has no generator, and that is said with a code.
        st, _, b = raw(port, "/library/local-run", method="POST", body=body,
                       headers={"Content-Type": "application/json",
                                "Origin": f"http://127.0.0.1:{port}"})
        j = json.loads(b)
        check("an update from its own Origin gets to the point",
              st == 200 and j.get("started") is False and j.get("reason"),
              f"{st} {b[:160]!r}")
        check("the refusal is named by a code and in words",
              isinstance(j.get("why"), str) and j["why"], str(j)[:160])
        strings = json.loads((web.parent / "strings.json").read_text("utf-8"))
        check("the precheck refusal codes have their own captions",
              all(("local_run_err_" + k) in strings
                  for k in ("busy", "no_right", "rights_expired", "no_bundle",
                            "no_generator")))
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# 10. the string keys the page asks for exist in strings.json                 #
# --------------------------------------------------------------------------- #

def test_js_string_keys_exist():
    """Every `U.fmt('key')`/`U.str('key')` from web/*.js must be found in strings.json.

    A miss does not crash and is not written to the log: the page shows the key itself
    (`sync_running_bytes` instead of a progress string). It can be seen only
    with the eyes and only in the state that happened to be caught — and states like
    "downloading the third report" are also short-lived. So the check is by machine.
    """
    web = Path(__file__).resolve().parents[1] / "mirror" / "web"
    strings = json.loads((web.parent / "strings.json").read_text("utf-8"))
    used: set[str] = set()
    for js in sorted(web.glob("*.js")):
        # And `str(`, not only `fmt(`: half of the buttons call the short form
        # (`U.str('action_open_folders_ui')`), and a key without a string would
        # look exactly the same on it — a key name on a button.
        used |= set(re.findall(r"""(?:fmt|str)\(\s*['"]([a-z0-9_]+)['"]""",
                               js.read_text("utf-8")))
    check("strings: the page uses the dictionary at all", bool(used))
    missing = sorted(k for k in used if k not in strings)
    check("strings: all keys from web/*.js are in strings.json",
          not missing, ", ".join(missing))


# --------------------------------------------------------------------------- #

def main() -> int:
    try:
        for fn in (test_shell_served, test_report_served_through_agent,
                   test_no_path_traversal, test_key_not_leaked,
                   test_host_check_on_new_routes, test_rls_boundary,
                   test_no_null_times, test_incomplete_kit_is_loud,
                   test_branch_without_schedule,
                   test_js_string_keys_exist, test_viewer_update_action):
            print(f"--- {fn.__name__}")
            try:
                fn()
            except Exception:  # noqa: BLE001
                check(f"{fn.__name__} did not fail with an exception", False,
                      "\n" + traceback.format_exc())
    finally:
        os.environ.pop("LOCALAPPDATA", None)
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    total = _PASS + _FAIL
    print(f"\n{_PASS}/{total}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
