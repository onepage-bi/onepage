"""Smoke test of the desktop report mirror. Standard library only.

The platform is replaced by a stub on `http.server` (manifest, document, slots, ETag,
304, 401, dropped connections); the mirror local server (`agent.mirror.serve`) is
brought up as a real `ThreadingHTTPServer` on a free port and hit from
`http.client`. No outbound network, no real key, no real state directory:
`LOCALAPPDATA` points to a temporary directory for every scenario,
so `config.state_dir()` does not touch the developer's `~/.op-agent`.

The order of scenarios follows the importance of the invariants: first comes the ADR-18 ban
(a report with `mirrorable: false` does not end up on disk as a single file), then
file names and escaping the directory, and only then conveniences such as 304.

Run:  python -m agent.tests.smoke_mirror   (from the OnePage repo root)
"""
from __future__ import annotations

import contextlib
import gzip
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.mirror import layout, serve, store, sync

_PASS = 0
_FAIL = 0

# A deliberately recognizable key: we look for it as a substring in the log, exceptions and in
# all state files, so it must not occur in ordinary text.
KEY = "OPKEY-3f9a1c77-DEVICE-SECRET-zq81"


def check(label, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"PASS {label}")
    else:
        _FAIL += 1
        print(f"FAIL {label}  {extra}")


# --------------------------------------------------------------------------- #
# Platform stub                                                              #
# --------------------------------------------------------------------------- #

class Platform:
    """Predefined responses + a request log. The test changes the state."""

    def __init__(self):
        self.key = KEY
        self.items: list[dict] = []
        self.docs: dict[str, bytes] = {}
        self.snapshots: dict[str, bytes] = {}
        # id -> {slot: {"body": bytes, "enc": str|None, "etag": str}}
        self.slots: dict[str, dict[int, dict]] = {}
        self.snapshot_external = False
        self.log: list[dict] = []
        self.manifest_status = 200      # 401/500 — refusal on the manifest
        self.slot_status = None         # 401 — the key died in the middle of the pass
        self.slot_truncate = False      # promise more than is delivered
        self.doc_truncate = False
        self.redirect_to: str | None = None
        self.status_reason: str | None = None
        self.lock = threading.Lock()
        self.server = None

    def manifest(self) -> dict:
        return {"items": self.items, "rights_ttl_s": 3600,
                "snapshot_external": self.snapshot_external}

    def requests(self, prefix: str) -> list[dict]:
        return [r for r in self.log if r["path"].startswith(prefix)]


def _platform_handler(plat: Platform):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence
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
            with plat.lock:
                plat.log.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "inm": self.headers.get("If-None-Match"),
                    "ae": self.headers.get("Accept-Encoding"),
                })
            if self.headers.get("Authorization") != "Bearer " + plat.key:
                return self._send(401, b'{"error":"bad key"}')
            path = self.path.split("?", 1)[0]
            ae = (self.headers.get("Accept-Encoding") or "").lower()

            if path == "/api/sync/manifest":
                if plat.redirect_to:
                    return self._send(302, b"", {"Location": plat.redirect_to})
                if plat.manifest_status != 200:
                    # The key in the status line and in the body is the worst case: the code
                    # must not drag either of them into its messages.
                    return self._send(plat.manifest_status,
                                      ("echo " + self.headers.get("Authorization", "")).encode(),
                                      reason=plat.status_reason)
                body = json.dumps(plat.manifest()).encode()
                if "gzip" in ae:
                    return self._send(200, gzip.compress(body),
                                      {"Content-Encoding": "gzip",
                                       "Content-Type": "application/json"})
                return self._send(200, body, {"Content-Type": "application/json"})

            parts = [p for p in path.split("/") if p]
            if len(parts) >= 2 and parts[0] == "render":
                did = parts[1]
                if len(parts) == 2:
                    doc = plat.docs.get(did)
                    if doc is None:
                        return self._send(404)
                    if plat.doc_truncate:
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(doc) + 500))
                        self.end_headers()
                        self.wfile.write(doc[:10])
                        self.close_connection = True
                        return None
                    if "gzip" in ae:
                        return self._send(200, gzip.compress(doc),
                                          {"Content-Encoding": "gzip",
                                           "Content-Type": "text/html"})
                    return self._send(200, doc, {"Content-Type": "text/html"})
                if parts[2] == "snapshot.js" and did in plat.snapshots:
                    return self._send(200, plat.snapshots[did],
                                      {"Content-Type": "text/javascript"})
                if parts[2] == "files" and len(parts) == 4:
                    if plat.slot_status:
                        return self._send(plat.slot_status, b"nope")
                    slot = plat.slots.get(did, {}).get(int(parts[3]))
                    if slot is None:
                        return self._send(404)
                    if self.headers.get("If-None-Match") == slot["etag"]:
                        return self._send(304, b"", {"ETag": slot["etag"]})
                    hdr = {"ETag": slot["etag"]}
                    if slot["enc"]:
                        hdr["Content-Encoding"] = slot["enc"]
                    if plat.slot_truncate:
                        # Promise ten times more and drop the connection:
                        # this is what a breaking corporate channel looks like.
                        self.send_response(200)
                        for k, v in hdr.items():
                            self.send_header(k, v)
                        self.send_header("Content-Length", str(len(slot["body"]) * 10))
                        self.end_headers()
                        self.wfile.write(slot["body"][: max(len(slot["body"]) // 2, 1)])
                        self.close_connection = True
                        return None
                    return self._send(200, slot["body"], hdr)
            return self._send(404)

    return H


def start_platform(plat: Platform):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _platform_handler(plat))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    plat.server = srv
    return f"http://127.0.0.1:{srv.server_address[1]}"


# --------------------------------------------------------------------------- #
# Mirror local server                                                       #
# --------------------------------------------------------------------------- #

class MirrorHandler(BaseHTTPRequestHandler):
    """The same call that the agent makes from `AgentHandler.do_GET`."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _go(self):
        if serve.serve(self):
            return
        body = b'{"error":"not_ours"}'
        self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_HEAD = do_POST = _go  # noqa: N815


def start_mirror():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MirrorHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def fetch(port, path, headers=None, method="GET"):
    """A raw request without auto-decoding: what actually arrived over the wire."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.putrequest(method, path, skip_accept_encoding=True)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, body
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# Scenario scaffolding                                                        #
# --------------------------------------------------------------------------- #

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="smoke_mirror_"))
_COUNTER = 0


def fresh_env() -> Path:
    """A fresh empty state directory for a scenario."""
    global _COUNTER
    _COUNTER += 1
    d = _TMP_ROOT / f"s{_COUNTER}"
    d.mkdir()
    os.environ["LOCALAPPDATA"] = str(d)
    return d


def item(did, version=1, fingerprint="fp1", slots=None, mirrorable=True,
         skip_reason=None, **extra):
    d = {"id": did, "title": "T " + did, "type": "html", "version": version,
         "fingerprint": fingerprint, "mirrorable": mirrorable,
         "skip_reason": skip_reason, "slots": slots or [],
         "can_update_data": False, "rls_enabled": not mirrorable,
         "updated_at": "2026-09-01T10:00:00"}
    d.update(extra)
    return d


def new_syncer(base, **kw):
    kw.setdefault("token", KEY)
    kw.setdefault("sleep", lambda s: None)
    return sync.Syncer(base, **kw)


def files_under(path: Path) -> list[Path]:
    return [p for p in Path(path).rglob("*") if p.is_file()] if Path(path).exists() else []


# --------------------------------------------------------------------------- #
# 1. RLS: mirrorable=false does not end up on disk                           #
# --------------------------------------------------------------------------- #

def test_rls_never_mirrored():
    env = fresh_env()
    plat = Platform()
    base = start_platform(plat)
    mport = start_mirror()
    try:
        # a) a report with the ban that was never in the mirror
        plat.items = [item("rls-new", mirrorable=False, skip_reason="rls",
                           slots=[{"slot": 1, "name": "d.js", "size": 3, "v": "a"}]),
                      item("ok-1", slots=[{"slot": 1, "name": "app.js", "size": 5, "v": "a"}])]
        plat.docs = {"rls-new": b"<html>SECRET-ROWS</html>", "ok-1": b"<html>ok</html>"}
        plat.slots = {"rls-new": {1: {"body": b"SECRET", "enc": None, "etag": '"e1"'}},
                      "ok-1": {1: {"body": b"x=1;", "enc": None, "etag": '"e2"'}}}
        r = new_syncer(base).sync_all()
        mdir = layout.mirror_root()
        check("rls: a report without the ban was downloaded", r.updated == 1 and r.failed == 0,
              repr(r))
        check("rls: a new banned report created no directory",
              not (mdir / "rls-new").exists())
        check("rls: there was not a single platform request for this report",
              not plat.requests("/render/rls-new"), repr(plat.log))
        check("rls: the reason for the ban is recorded for the library",
              store.blocked().get("rls-new", {}).get("reason") == "rls",
              repr(store.blocked()))
        check("rls: the removed counter counts the banned report", r.removed == 1)

        # b) the report WAS in the mirror, then a policy was turned on for it
        plat.items = [item("was-ok", slots=[{"slot": 1, "name": "d.js", "size": 6, "v": "a"}])]
        plat.docs["was-ok"] = b"<html>old</html>"
        plat.slots["was-ok"] = {1: {"body": b"UNFILT", "enc": None, "etag": '"e9"'}}
        new_syncer(base).sync_all()
        had = files_under(mdir / "was-ok")
        check("rls: precondition — the report was in the mirror (document+slot+state)",
              len(had) >= 3, repr(had))
        plat.items = [item("was-ok", mirrorable=False, skip_reason="rls",
                           slots=[{"slot": 1, "name": "d.js", "size": 6, "v": "a"}])]
        plat.log.clear()
        r = new_syncer(base).sync_all()
        left = files_under(mdir / "was-ok")
        check("rls: after the policy was turned on ALL report files vanished from disk",
              not left and not (mdir / "was-ok").exists(), repr(left))
        check("rls: the report data did not remain anywhere in the mirror tree",
              not any(b"UNFILT" in p.read_bytes() for p in files_under(mdir)))
        check("rls: after removal the local server answers 404, not a file",
              fetch(mport.server_address[1], "/mirror/was-ok/")[0] == 404)
        check("rls: again not a single request for the banned report",
              not plat.requests("/render/was-ok"))
        check("rls: the reason persisted after the directory was removed",
              "was-ok" in store.blocked())

        # c) the ban is lifted — the report returns and the reason is forgotten
        plat.items = [item("was-ok", version=2, slots=[])]
        new_syncer(base).sync_all()
        check("rls: ban lifted — the report was downloaded again, the reason forgotten",
              (mdir / "was-ok").exists() and "was-ok" not in store.blocked())

        # d) the second line of the server: a state with rls_enabled is not served
        st = layout.read_state("was-ok")
        st["rights"]["rls_enabled"] = True
        layout.write_state("was-ok", st)
        code = fetch(mport.server_address[1], "/mirror/was-ok/")[0]
        check("rls: the server itself refuses when rights.rls_enabled (403)",
              code == 403, str(code))
    finally:
        plat.server.shutdown()
        mport.shutdown()


# --------------------------------------------------------------------------- #
# 2. Name safety                                                             #
# --------------------------------------------------------------------------- #

EVIL_NAMES = [
    "../../evil.js", "..\\..\\evil.js", "/etc/passwd", "C:\\Windows\\evil.js",
    "NUL.js", "con", "COM1.txt", "report.js:stream", "a/b/c.js", "dots...",
    "..", "", "x" * 400 + ".js", "tail. ",
]


def test_filename_safety():
    # A pure function: the guarantees from the docstring, on all evil names.
    bad = []
    taken: set[str] = set()
    for i, nm in enumerate(EVIL_NAMES):
        out = layout.safe_slot_filename(nm, slot=i, taken=taken)
        taken.add(out)
        if (not out or out in (".", "..") or any(c in out for c in '/\\:*?"<>|')
                or out.rstrip(". ") != out or len(out) > layout.MAX_NAME_CHARS
                or out.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}):
            bad.append((nm[:20], out))
    check("names: a safe name for every evil original", not bad, repr(bad))
    check("names: ../x.js and ..\\x.js do not collapse into one file",
          layout.safe_slot_filename("../x.js") != layout.safe_slot_filename("..\\x.js"))
    check("names: an honest name passes straight through without edits",
          layout.safe_slot_filename("data.bin.gz") == "data.bin.gz")
    check("names: two slots with one name do not overwrite each other",
          len({s["file"] for s in layout.plan_slot_files(
              [{"slot": 1, "name": "a.js"}, {"slot": 2, "name": "a.js"}])}) == 2)

    # Integration: a real pass with evil names in the manifest.
    env = fresh_env()
    plat = Platform()
    base = start_platform(plat)
    mport = start_mirror()
    try:
        slots = [{"slot": i + 1, "name": nm, "size": 4, "v": f"v{i}"}
                 for i, nm in enumerate(EVIL_NAMES)]
        plat.items = [item("evil", slots=slots)]
        plat.docs["evil"] = b"<html></html>"
        plat.slots["evil"] = {i + 1: {"body": b"BODY", "enc": None, "etag": f'"t{i}"'}
                              for i in range(len(EVIL_NAMES))}
        r = new_syncer(base).sync_all()
        vdir = layout.server_version_dir("evil", 1).resolve()
        check("names: the pass with evil names finished without failures",
              r.failed == 0 and r.updated == 1, repr(r))
        outside = [p for p in files_under(env)
                   if vdir not in p.resolve().parents
                   and p.resolve().parent != vdir.parent.parent
                   and p.name != "state.json"]
        check("names: no file escaped the version directory", not outside,
              repr(outside))
        names = {p.name for p in files_under(vdir)}
        check("names: there is no evil.js/passwd file with a bare name on disk",
              not ({"evil.js", "passwd", "NUL.js", "con"} & names), repr(names))
        check("names: nothing appeared in the state tree outside the report directory",
              all(env.resolve() in p.resolve().parents for p in files_under(env)))
        stray = [p for p in _TMP_ROOT.rglob("evil.js")]
        check("names: ../../evil.js was not created anywhere in the temporary tree", not stray,
              repr(stray))
        # a slot that went to disk under a different name is still served by number
        code, _, body = fetch(mport.server_address[1], "/mirror/evil/files/1")
        check("names: the slot named ../../evil.js is served by number",
              code == 200 and body == b"BODY", f"{code} {body!r}")
    finally:
        plat.server.shutdown()
        mport.shutdown()


# --------------------------------------------------------------------------- #
# 3. Directory traversal on the local server                                  #
# --------------------------------------------------------------------------- #

def test_asset_traversal():
    fresh_env()
    plat = Platform()
    base = start_platform(plat)
    mport = start_mirror()
    port = mport.server_address[1]
    try:
        plat.items = [item("tr", slots=[
            {"slot": 1, "name": "../../../etc/passwd", "size": 4, "v": "a"},
            {"slot": 2, "name": "app.js", "size": 4, "v": "b"}])]
        plat.docs["tr"] = b"<html></html>"
        plat.slots["tr"] = {1: {"body": b"PWND", "enc": None, "etag": '"1"'},
                            2: {"body": b"ok;", "enc": None, "etag": '"2"'}}
        new_syncer(base).sync_all()
        attempts = [
            "/mirror/tr/../../../etc/passwd",
            "/mirror/tr/..%2f..%2f..%2fetc%2fpasswd",
            "/mirror/tr/%2e%2e/%2e%2e/etc/passwd",
            "/mirror/tr/..%5c..%5cetc%5cpasswd",
            "/mirror/tr/%252e%252e%252fetc%252fpasswd",
            "/mirror/tr/passwd",
            "/mirror/tr/etc/passwd",
            "/mirror/tr/state.json",
            "/mirror/..%2f..%2fetc/passwd",
        ]
        leaked = []
        for p in attempts:
            code, _, body = fetch(port, p)
            if code == 200 or b"root:" in body or b"PWND" in body or b'"slots"' in body:
                leaked.append((p, code))
        check("assets: not a single attempt to escape the directory returned a file",
              not leaked, repr(leaked))
        code, _, body = fetch(port, "/mirror/tr/app.js")
        check("assets: an honest asset next to it is served", code == 200 and body == b"ok;")
        code, _, _ = fetch(port, "/mirror/tr", method="GET")
        check("assets: an address without a trailing slash — 307, not the document", code == 307)
        code, _, _ = fetch(port, "/mirror/tr/", method="POST")
        check("assets: POST to a mirror address — 405", code == 405)
    finally:
        plat.server.shutdown()
        mport.shutdown()


# --------------------------------------------------------------------------- #
# 4-5. Compression and MIME                                                  #
# --------------------------------------------------------------------------- #

def test_encoding_and_mime():
    fresh_env()
    plat = Platform()
    base = start_platform(plat)
    mport = start_mirror()
    port = mport.server_address[1]
    try:
        payload = b"COL1,COL2\n" + b"1,2\n" * 500
        gz = gzip.compress(payload)
        plat.items = [item("enc", slots=[
            {"slot": 1, "name": "data.csv.gz", "size": len(gz), "v": "a"},
            {"slot": 2, "name": "lib.js", "size": 5, "v": "b"},
            {"slot": 3, "name": "style.css", "size": 5, "v": "c"},
            {"slot": 4, "name": "mod.wasm", "size": 5, "v": "d"},
            {"slot": 5, "name": "pic.png", "size": 5, "v": "e"},
            {"slot": 6, "name": "x.bin.br", "size": 5, "v": "f"}])]
        plat.docs["enc"] = b"<html></html>"
        plat.slots["enc"] = {
            1: {"body": gz, "enc": "gzip", "etag": '"g1-gzip"'},
            2: {"body": b"var a;", "enc": None, "etag": '"g2"'},
            3: {"body": b"a{}", "enc": None, "etag": '"g3"'},
            4: {"body": b"\0asm", "enc": None, "etag": '"g4"'},
            5: {"body": b"\x89PNG", "enc": None, "etag": '"g5"'},
            6: {"body": b"BRBLOB", "enc": "br", "etag": '"g6-br"'},
        }
        r = new_syncer(base).sync_all()
        check("compression: a pass with compressed slots without failures", r.failed == 0, repr(r))
        st = layout.read_state("enc")
        enc_by_slot = {s["slot"]: s.get("enc") for s in st["slots"]}
        check("compression: the slot encoding is recorded in state.json (enc=gzip)",
              enc_by_slot.get(1) == "gzip" and enc_by_slot.get(2) is None,
              repr(enc_by_slot))

        # a client that accepts gzip: Content-Encoding + raw bytes
        for url in ("/mirror/enc/files/1", "/mirror/enc/data.csv"):
            code, h, body = fetch(port, url, {"Accept-Encoding": "gzip, deflate, br"})
            ce = h.get("content-encoding")
            ok = False
            if code == 200:
                if ce == "gzip":
                    ok = gzip.decompress(body) == payload
                elif ce in (None, "identity"):
                    ok = body == payload
            check(f"compression: a gzip client reads {url}", ok, f"{code} {ce} {len(body)}")
            check(f"compression: {url} is served to a gzip client compressed as it lies",
                  ce == "gzip" and body == gz, f"{ce}")

        # a client without gzip: a decompressed body, no gzip header
        for url in ("/mirror/enc/files/1", "/mirror/enc/data.csv"):
            code, h, body = fetch(port, url, {"Accept-Encoding": "identity"})
            ce = h.get("content-encoding")
            ok = code == 200 and ce in (None, "identity") and body == payload
            check(f"compression: a client without gzip gets a readable body {url}", ok,
                  f"{code} {ce} {body[:20]!r}")
        code, h, body = fetch(port, "/mirror/enc/files/1")
        check("compression: a client with no Accept-Encoding at all gets a readable body",
              code == 200 and h.get("content-encoding") in (None, "identity")
              and body == payload, f"{code} {h.get('content-encoding')}")
        code, h, body = fetch(port, "/mirror/enc/files/1",
                              {"Accept-Encoding": "gzip;q=0, identity"})
        check("compression: gzip;q=0 is respected — the body is decompressed",
              code == 200 and h.get("content-encoding") in (None, "identity")
              and body == payload)
        # br: a client that accepts it — straight through, one that does not — an honest refusal, not garbage
        code, h, body = fetch(port, "/mirror/enc/files/6", {"Accept-Encoding": "br"})
        check("compression: .br is served straight through to a client with br",
              code == 200 and h.get("content-encoding") == "br" and body == b"BRBLOB")
        code, h, body = fetch(port, "/mirror/enc/files/6", {"Accept-Encoding": "identity"})
        check("compression: .br to a client without br — not raw bytes posing as data",
              body != b"BRBLOB", f"{code}")

        # MIME
        for name, want in (("lib.js", "text/javascript"), ("style.css", "text/css"),
                           ("mod.wasm", "application/wasm"), ("pic.png", "image/png")):
            code, h, _ = fetch(port, f"/mirror/enc/{name}")
            check(f"mime: {name} -> {want}",
                  code == 200 and h.get("content-type", "").startswith(want),
                  f"{code} {h.get('content-type')}")
        code, h, _ = fetch(port, "/mirror/enc/files/2")
        check("mime: .js by slot number is text/javascript too",
              h.get("content-type", "").startswith("text/javascript"),
              h.get("content-type", ""))
        check("mime: .js is not application/octet-stream (ORB)",
              "octet-stream" not in h.get("content-type", ""))
        # snapshot
        plat.snapshot_external = True
        plat.snapshots["enc"] = b"window.S=1;"
        plat.items = [item("enc", fingerprint="fp2", slots=plat.items[0]["slots"])]
        new_syncer(base).sync_all()
        code, h, body = fetch(port, "/mirror/enc/snapshot.js?v=abc")
        check("mime: snapshot.js is served with text/javascript",
              code == 200 and body == b"window.S=1;"
              and h.get("content-type", "").startswith("text/javascript"),
              f"{code} {h.get('content-type')}")
        code, h, body = fetch(port, "/mirror/enc/")
        check("mime: the document is served as text/html",
              code == 200 and h.get("content-type", "").startswith("text/html")
              and body == b"<html></html>", f"{code} {h.get('content-type')}")
    finally:
        plat.server.shutdown()
        mport.shutdown()


# --------------------------------------------------------------------------- #
# 6-7. Incrementality and fingerprint                                        #
# --------------------------------------------------------------------------- #

def test_incremental_and_fingerprint():
    fresh_env()
    plat = Platform()
    base = start_platform(plat)
    try:
        slots_v = [{"slot": 1, "name": "a.js", "size": 4, "v": "m1"}]
        slots_nov = [{"slot": 1, "name": "a.js", "size": 4}]     # without v
        plat.items = [item("inc", slots=slots_nov)]
        plat.docs["inc"] = b"<html>one</html>"
        plat.slots["inc"] = {1: {"body": b"a=1;", "enc": None, "etag": '"S1"'}}

        r1 = new_syncer(base).sync_all()
        slot_reqs = plat.requests("/render/inc/files/")
        check("incremental: the first pass downloaded the slot without a conditional request",
              len(slot_reqs) == 1 and slot_reqs[0]["inm"] is None and r1.updated == 1,
              repr(slot_reqs))
        check("incremental: the key goes in a Bearer header on every request",
              all(x["auth"] == "Bearer " + KEY for x in plat.log))
        check("incremental: the slot is requested with a constant Accept-Encoding",
              slot_reqs[0]["ae"] == sync.SLOT_ACCEPT_ENCODING, str(slot_reqs[0]["ae"]))

        plat.log.clear()
        r2 = new_syncer(base).sync_all()
        slot_reqs = plat.requests("/render/inc/files/")
        check("incremental: second pass — the slot is requested conditionally (If-None-Match)",
              len(slot_reqs) == 1 and slot_reqs[0]["inm"] == '"S1"', repr(slot_reqs))
        check("incremental: the server answered 304, bytes were not downloaded",
              r2.bytes_downloaded == 0 and r2.unchanged == 1 and r2.updated == 0,
              repr(r2))
        check("incremental: the document is not re-downloaded when the server is unchanged",
              not [x for x in plat.log if x["path"] == "/render/inc/"], repr(plat.log))

        # the file was lost from disk -> without If-None-Match, otherwise 304 leaves a hole
        vdir = layout.server_version_dir("inc", 1)
        (vdir / "a.js").unlink()
        plat.log.clear()
        r3 = new_syncer(base).sync_all()
        slot_reqs = plat.requests("/render/inc/files/")
        check("incremental: the slot is not on disk — the request is unconditional, the file came back",
              len(slot_reqs) == 1 and slot_reqs[0]["inm"] is None
              and (vdir / "a.js").read_bytes() == b"a=1;", repr(slot_reqs))

        # with a slot that has v: not a single request for the slot
        plat.items = [item("inc", slots=slots_v)]
        new_syncer(base).sync_all()        # remembers v
        plat.log.clear()
        r4 = new_syncer(base).sync_all()
        check("incremental: a slot with an unchanged v is not requested at all",
              not plat.requests("/render/inc/files/") and r4.unchanged == 1,
              repr(plat.log))

        # 7. fingerprint with the same version number
        plat.log.clear()
        plat.items = [item("inc", version=1, fingerprint="fp-NEW", slots=slots_v)]
        plat.docs["inc"] = b"<html>two</html>"
        r5 = new_syncer(base).sync_all()
        doc = (layout.server_version_dir("inc", 1) / layout.DOCUMENT_NAME).read_bytes()
        check("fingerprint: same version number, new fingerprint — the document was re-downloaded",
              doc == b"<html>two</html>" and r5.updated == 1, repr(doc))
        check("fingerprint: the new fingerprint is saved in state.json",
              layout.read_state("inc").get("fingerprint") == "fp-NEW")
        plat.log.clear()
        new_syncer(base).sync_all()
        check("fingerprint: the next pass with the same fingerprint does not download the document",
              not [x for x in plat.log if x["path"] == "/render/inc/"])

        # a new version number: a new directory, the old one swept
        plat.items = [item("inc", version=2, fingerprint="fp-NEW", slots=slots_v)]
        new_syncer(base).sync_all()
        sdir = layout.dashboard_dir("inc") / "server"
        check("version: one server version remains in the mirror",
              sorted(p.name for p in sdir.iterdir()) == ["v2"],
              repr(list(sdir.iterdir())))
    finally:
        plat.server.shutdown()


# --------------------------------------------------------------------------- #
# 8. 401                                                                      #
# --------------------------------------------------------------------------- #

class FakeCreds:
    """An in-memory key store: we check UNDER WHICH label the key is looked up."""

    def __init__(self, value=KEY):
        self.value = value
        self.asked: list[str] = []

    def get_password(self, name):
        self.asked.append(name)
        return self.value


def test_key_rejected():
    fresh_env()
    plat = Platform()
    base = start_platform(plat)
    try:
        plat.items = [item("k1", slots=[{"slot": 1, "name": "a.js", "size": 4, "v": "a"}])]
        plat.docs["k1"] = b"<html></html>"
        plat.slots["k1"] = {1: {"body": b"a=1;", "enc": None, "etag": '"x"'}}

        creds = FakeCreds()
        s = sync.Syncer(base, cred_store=creds, sleep=lambda s: None)
        r = s.sync_all()
        check("401: the key is taken from the store under the label onepage-device-key",
              creds.asked == [sync.DEVICE_KEY_SLOT] and sync.DEVICE_KEY_SLOT == "onepage-device-key",
              repr(creds.asked))
        check("key: a working key passes", r.ok and r.updated == 1, repr(r))

        # the manifest answers 401
        plat.key = "OTHER-KEY"
        plat.log.clear()
        sleeps: list[float] = []
        r = sync.Syncer(base, token=KEY, sleep=sleeps.append).sync_all()
        check("401: a platform 401 response is recognized as \u201cthe key died\u201d",
              r.key_rejected is True, repr(r))
        check("401: this is not a network error — no retries or backoffs",
              len(plat.requests("/api/sync/manifest")) == 1 and not sleeps,
              f"{len(plat.log)} {sleeps}")
        check("401: the result is not ok, the person has something to be shown",
              not r.ok and bool(r.manifest_error))
        exc = None
        try:
            sync.Syncer(base, token=KEY, sleep=lambda s: None).fetch_manifest()
        except Exception as e:  # noqa: BLE001
            exc = e
        check("401: fetch_manifest raises KeyRejected, not TransportError",
              isinstance(exc, sync.KeyRejected)
              and not isinstance(exc, sync.TransportError), repr(exc))

        # 500: this is the network side — with backoff and without key_rejected
        plat.key = KEY
        plat.manifest_status = 500
        plat.log.clear()
        sleeps = []
        r = sync.Syncer(base, token=KEY, sleep=sleeps.append).sync_all()
        check("401: 500 is not \u201cthe key died\u201d but a manifest error",
              r.key_rejected is False and bool(r.manifest_error), repr(r))
        check("401: on 500 an exponential backoff (2, 4, 8)",
              sleeps == [2.0, 4.0, 8.0], repr(sleeps))
        plat.manifest_status = 200

        # the key died in the middle of the pass (the slot answers 401)
        plat.items = [item("k1", version=2, fingerprint="z",
                           slots=[{"slot": 1, "name": "a.js", "size": 4, "v": "NEW"}])]
        plat.slot_status = 401
        r = sync.Syncer(base, token=KEY, sleep=lambda s: None).sync_all()
        check("401: a 401 in the middle of the pass is key_rejected too, not failed",
              r.key_rejected and r.failed == 0, repr(r))
        plat.slot_status = None

        # no key in the store
        exc = None
        try:
            sync.Syncer(base, cred_store=FakeCreds(None)).sync_all()
        except Exception as e:  # noqa: BLE001
            exc = e
        check("key: an empty store — NoDeviceKey, not a silent zero",
              isinstance(exc, sync.NoDeviceKey), repr(exc))
    finally:
        plat.server.shutdown()


# --------------------------------------------------------------------------- #
# 9. The key does not leak                                                   #
# --------------------------------------------------------------------------- #

class _Boom:
    def get_password(self, name):
        raise RuntimeError("dpapi failed, the key was " + KEY)


def test_key_not_leaked():
    env = fresh_env()
    plat = Platform()
    other = Platform()                       # a "foreign host" for the redirect
    base = start_platform(plat)
    other_base = start_platform(other)
    sink = io.StringIO()
    texts: list[str] = []

    class Cap(io.StringIO):
        pass

    import logging
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    logging.getLogger().addHandler(handler)
    old_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)

    def grab(fn):
        try:
            res = fn()
            texts.append(repr(res))
            texts.append(repr(getattr(res, "errors", "")))
            texts.append(str(getattr(res, "manifest_error", "")))
            return res
        except Exception as e:  # noqa: BLE001
            texts.append(str(e))
            texts.append(repr(e))
            texts.append("".join(traceback.format_exception(e)))
            return e

    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            # 1. the server is off (connection refused)
            dead = Platform()
            dead_base = start_platform(dead)
            dead.server.shutdown()
            dead.server.server_close()
            grab(lambda: sync.Syncer(dead_base, token=KEY, sleep=lambda s: None).sync_all())
            grab(lambda: sync.Syncer(dead_base, token=KEY, sleep=lambda s: None).fetch_manifest())
            # 2. 500, where the key is in both the status line and the body
            plat.manifest_status = 500
            plat.status_reason = "boom " + KEY
            grab(lambda: sync.Syncer(base, token=KEY, sleep=lambda s: None).sync_all())
            grab(lambda: sync.Syncer(base, token=KEY, sleep=lambda s: None).fetch_manifest())
            plat.manifest_status = 200
            plat.status_reason = None
            # 3. 401
            plat.key = "other"
            grab(lambda: sync.Syncer(base, token=KEY, sleep=lambda s: None).sync_all())
            grab(lambda: sync.Syncer(base, token=KEY, sleep=lambda s: None).fetch_manifest())
            plat.key = KEY
            # 4. a redirect to a foreign host
            plat.redirect_to = other_base + "/api/sync/manifest"
            grab(lambda: sync.Syncer(base, token=KEY, sleep=lambda s: None).sync_all())
            plat.redirect_to = None
            # 5. the key store fails with the key in the text
            grab(lambda: sync.Syncer(base, cred_store=_Boom()).sync_all())
            # 6. the manifest is not JSON; the slot returns 500
            plat.items = [item("lk", slots=[{"slot": 1, "name": "a.js", "size": 4, "v": "a"}])]
            plat.docs["lk"] = b"<html></html>"
            plat.slots["lk"] = {1: {"body": b"a=1;", "enc": None, "etag": '"x"'}}
            plat.slot_status = 500
            grab(lambda: sync.Syncer(base, token=KEY, sleep=lambda s: None).sync_all())
            plat.slot_status = None
            # 7. a dropped slot
            plat.slot_truncate = True
            plat.items = [item("lk", version=2, slots=[{"slot": 1, "name": "a.js", "size": 4, "v": "b"}])]
            grab(lambda: sync.Syncer(base, token=KEY, sleep=lambda s: None).sync_all())
            plat.slot_truncate = False
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(old_level)
        for p in (plat, other):
            p.server.shutdown()

    blob = "\n".join(texts) + sink.getvalue() + log_stream.getvalue()
    check("leak: the key is not in exceptions, results and tracebacks",
          KEY not in "\n".join(texts), "found in: " + "\n".join(
              t[:200] for t in texts if KEY in t))
    check("leak: the key is not in stdout/stderr/logging across all failure scenarios",
          KEY not in sink.getvalue() + log_stream.getvalue())
    check("leak: after a failure there was at least some diagnostics in the check",
          len(blob) > 100)
    check("leak: the redirect to a foreign host did not carry the key away (no requests)",
          not other.log, repr(other.log))
    leaked_files = [p for p in files_under(env) if KEY.encode() in p.read_bytes()]
    check("leak: the key is not in a single file of the state directory",
          not leaked_files, repr(leaked_files))
    check("leak: _scrub cleans the key out of arbitrary text",
          KEY not in sync.Syncer("http://x", token=KEY)._scrub("a " + KEY + " b"))


# --------------------------------------------------------------------------- #
# 10. Atomicity                                                               #
# --------------------------------------------------------------------------- #

class _FlakyReader:
    """Hands out a piece and fails like a breaking connection."""

    def __init__(self, chunks, exc=ConnectionResetError("dropped")):
        self.chunks = list(chunks)
        self.exc = exc

    def read(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        raise self.exc


def test_atomicity():
    env = fresh_env()
    target = env / "dir" / "slot.bin"
    try:
        store.write_stream(target, _FlakyReader([b"A" * 1000, b"B" * 1000]))
    except ConnectionResetError:
        pass
    check("atomicity: an interrupted stream did not leave a file with the final name",
          not target.exists(), repr(list(target.parent.iterdir())))
    check("atomicity: an interrupted stream did not leave a .tmp either",
          not [p for p in target.parent.iterdir() if p.name.endswith(".tmp")],
          repr(list(target.parent.iterdir())))

    target.write_bytes(b"OLD-COMPLETE")
    try:
        store.write_stream(target, _FlakyReader([b"N" * 10]))
    except ConnectionResetError:
        pass
    check("atomicity: an interruption does not damage the previous whole file",
          target.read_bytes() == b"OLD-COMPLETE")

    try:
        with layout.atomic_write(target, "wb") as fh:
            fh.write(b"half")
            raise RuntimeError("failed midway")
    except RuntimeError:
        pass
    check("atomicity: an exception in atomic_write leaves the old file",
          target.read_bytes() == b"OLD-COMPLETE")

    with layout.atomic_write(target, "wb") as fh:
        fh.write(b"NEW")
        check("atomicity: during the write the final name is still the old one",
              target.read_bytes() == b"OLD-COMPLETE")
        tmps = [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]
        check("atomicity: the write goes to a temporary .tmp next to it", len(tmps) == 1)
    check("atomicity: after success — the new file, no .tmp",
          target.read_bytes() == b"NEW"
          and not [p for p in target.parent.iterdir() if p.name.endswith(".tmp")])

    orphan_dir = layout.mirror_root(create=True) / "some-id" / "server" / "v1"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "left.7.tmp").write_bytes(b"x")
    swept = store.sweep()
    check("atomicity: sweep sweeps up orphaned .tmp",
          swept >= 1 and not list(orphan_dir.glob("*.tmp")), f"swept={swept}")

    # quota: exceeding it is a refusal without a half-file
    big = env / "q" / "big.bin"
    try:
        store.write_stream(big, io.BytesIO(b"Z" * 5000), budget=100, chunk=1000)
    except store.MirrorFull:
        pass
    check("atomicity: hit the quota — no file",
          not big.exists())

    # end-to-end scenario: the server promised more than it delivered
    fresh_env()
    plat = Platform()
    base = start_platform(plat)
    try:
        plat.items = [item("cut", slots=[{"slot": 1, "name": "big.bin", "size": 2000, "v": "a"}])]
        plat.docs["cut"] = b"<html></html>"
        plat.slots["cut"] = {1: {"body": b"Q" * 2000, "enc": None, "etag": '"q"'}}
        plat.slot_truncate = True
        res = None
        try:
            res = new_syncer(base).sync_all()
        except Exception as e:  # noqa: BLE001
            res = e
        vdir = layout.server_version_dir("cut", 1)
        slot_file = vdir / "big.bin"
        looks_whole = slot_file.exists() and slot_file.stat().st_size != 2000
        check("atomicity: an interruption mid-slot did not leave a truncated file posing as whole",
              not looks_whole,
              f"on disk {slot_file.stat().st_size if slot_file.exists() else '-'} B "
              f"of 2000, result={res!r}")
        st = layout.read_state("cut")
        recorded = bool(st and st.get("server_version") == 1)
        check("atomicity: a slot interruption is not recorded in state.json as a successful version",
              not (recorded and looks_whole))
        check("atomicity: a slot interruption is visible in the pass result (failed), not \u201cdone\u201d",
              isinstance(res, sync.SyncResult) and res.failed >= 1,
              repr(res))

        # an interrupted document
        plat.slot_truncate = False
        plat.doc_truncate = True
        plat.items = [item("cut2", slots=[])]
        plat.docs["cut2"] = b"<html>" + b"x" * 2000 + b"</html>"
        try:
            res = new_syncer(base).sync_all()
        except Exception as e:  # noqa: BLE001
            res = e
        doc = layout.server_version_dir("cut2", 1) / layout.DOCUMENT_NAME
        check("atomicity: an interrupted document did not leave the document on disk",
              not doc.exists(), f"{doc.stat().st_size if doc.exists() else '-'}")
        check("atomicity: an interrupted document — result failed, no exception escaped",
              isinstance(res, sync.SyncResult) and res.failed >= 1, repr(res))
    finally:
        plat.server.shutdown()


# --------------------------------------------------------------------------- #

def main() -> int:
    try:
        for fn in (test_rls_never_mirrored, test_filename_safety,
                   test_asset_traversal, test_encoding_and_mime,
                   test_incremental_and_fingerprint, test_key_rejected,
                   test_key_not_leaked, test_atomicity):
            print(f"--- {fn.__name__}")
            try:
                fn()
            except Exception:  # noqa: BLE001
                check(f"{fn.__name__} did not raise an exception", False,
                      "\n" + traceback.format_exc())
    finally:
        os.environ.pop("LOCALAPPDATA", None)
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
