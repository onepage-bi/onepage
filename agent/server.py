"""HTTP layer of the agent: routing, CORS/PNA, Bearer auth, NDJSON stream.

Listens on 127.0.0.1 only (bind in __main__). Multi-threaded (ThreadingHTTPServer):
one control request /events is held open for the whole run, while /outputs and
/health are served in parallel. Authentication is the pairing Bearer token (NOT a
cookie): the agent has no cookies, so an XSS on OnePage cannot "ride" on
it; a foreign origin is cut off by the allowlist (ACAO is echoed only for allowed ones).

Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import sys
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote

from . import APP, PROTOCOL, VERSION, folders
from .config import Config
from .creds import CredStore
# [window] The "Report folders" screen (`/folders-ui/...`). A top-level import
# for the same reason as the mirror below: a relative import inside a
# function does not survive the merge into the single-file build.
from . import foldersui
# [mirror] The library shell (`/library/...`) and report delivery
# (`/mirror/<id>/...`). A top-level import: a relative import inside a
# function does not survive the merge into the single-file build.
from .mirror import libui as mirror_libui
from .mirror import localrun as mirror_localrun
from .mirror import serve as mirror_serve
from .pairing import Pairing
from .runs import Run, run_echo, run_generator


class AgentServer(ThreadingHTTPServer):
    """ThreadingHTTPServer + shared state (config, pairing, runs)."""

    daemon_threads = True
    allow_reuse_address = False   # a successful bind = the single-instance mutex

    def handle_error(self, request, client_address):  # noqa: N802
        """A browser opens a spare connection to 127.0.0.1 and drops it
        without a request (Chrome/Edge preconnect); socketserver would print
        a 15-line traceback for that ConnectionResetError, and analysts
        reading the log for a failed run mistake it for the cause
        (2026-09-09). Real handler errors still get the full traceback."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

    def __init__(self, addr, config: Config, pairing: Pairing,
                 cred_store: CredStore | None = None):
        super().__init__(addr, AgentHandler)
        self.config = config
        self.pairing = pairing
        self.cred_store = cred_store or CredStore()
        self.runs: dict[str, Run] = {}
        self.runs_lock = threading.Lock()
        self.busy = threading.Event()
        self.state_root = None   # filled in __main__ (per-run temp lives under it)
        # Injection of the GP connection for tests; in prod None -> gpsource installs psycopg.
        self.gp_connect = None
        self.python_exe = None   # None -> sys.executable (dev); the portable python at M3
        # The UI (tray icon) is able to show the "Allow?" question. Set
        # in __main__ when the tray starts successfully. None -> pairing by a code in the console:
        # dev, macOS, headless — there is nobody to show the question to.
        self.ui = None
        self.controller = None   # the icon policy (agent/controller.py), also from __main__
        # Rate limit of /pair/request (see _PAIR_RL_* and AgentHandler._pair_rate_limit):
        # the key is the client IP, a sliding window, purely in process memory.
        self.pair_rl_lock = threading.Lock()
        self.pair_attempts: dict[str, list[float]] = {}

    # ── offline run from the library ───────────────────────────────────
    def start_local_run(self, dashboard_id: str, scenario: str = "") -> tuple[str, str]:
        """Start a run from folders WITHOUT the platform. `(code, reason)`.

        `("started", "")` — the thread is up; everything else is a refusal with a code
        that the library turns into a caption.

        It lives here, not in `mirror/libui.py`, for two reasons, and both are
        essential. First: the run lock is one per program
        (`self.busy`) — the same one with which the OnePage page occupies the agent;
        two simultaneous runs would write to one generator cache. Second: the
        folder roots for `input_manifest` are computed by `folders_payload` (the
        root name is parsed by the agent, not by JS), and the mirror must not import
        `server.py` — it is imported BY it anyway.
        """
        if self.busy.is_set():
            return "busy", "the agent is busy with another run right now"
        kind, why = mirror_localrun.precheck(dashboard_id)
        if kind != "ok":
            return kind, why
        self.busy.set()

        def _task():
            try:
                info = None
                try:
                    status, payload = folders_payload(dashboard_id, scenario)
                    info = payload if status == 200 else None
                except Exception:                              # noqa: BLE001
                    # Roots are an ornament of the lineage: without them the run
                    # honestly proceeds, and `input_manifest` just will not name paths.
                    info = None
                mirror_localrun.run_local(dashboard_id, scenario,
                                          folders_info=info,
                                          python_exe=self.python_exe)
            except Exception as e:                             # noqa: BLE001
                # The reason is already recorded in `localrun.PROGRESS` and will reach
                # the library; here — only the log.
                print(f"[local-run] {dashboard_id}: {type(e).__name__}: {e}",
                      flush=True)
            finally:
                self.busy.clear()

        threading.Thread(target=_task, name=f"local-run-{dashboard_id}",
                         daemon=True).start()
        return "started", ""


_RUN_EVENTS_RE = re.compile(r"^/runs/([A-Za-z0-9_-]+)/events$")
_RUN_OUTPUT_RE = re.compile(r"^/runs/([A-Za-z0-9_-]+)/outputs/([A-Za-z0-9_.-]+)$")
_RUN_RE = re.compile(r"^/runs/([A-Za-z0-9_-]+)$")


# Ceilings on the request body. Different, because the trust boundary differs:
# /pair answers anyone (there is no authorization there yet, by definition), while
# /runs accepts the generator bundle already under a Bearer token.
_MAX_UNAUTH_BODY = 64 * 1024
_MAX_RUN_BODY = 64 * 1024 * 1024   # bundle (platform limit 20 MB) + metadata

# /pair/request is unauthorized by definition (starting the authorization is
# ITS job), which means any open tab can call it as many
# times as it likes — spam the UI confirmation dialog or recreate the code,
# not letting the person finish entering the previous one. N attempts/minute per client.
_PAIR_RL_MAX = 5
_PAIR_RL_WINDOW_S = 60.0


class _BodyTooLarge(Exception):
    def __init__(self, n: int, limit: int):
        super().__init__(f"body of {n} bytes exceeds {limit}")
        self.n = n


class AgentHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{APP}/{VERSION}"

    # Quieter in the console — we print pairing codes and meaningful things ourselves, not every GET.
    def log_message(self, fmt, *args):  # noqa: N802
        pass

    # ── low-level responses ─────────────────────────────────────────────
    def _cors(self):
        origin = self.headers.get("Origin")
        # ACAO is echoed ONLY for an origin from the allowlist — never '*'.
        if origin and self.server.config.origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def _json(self, status: int, obj: dict, extra_headers: dict | None = None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _bytes(self, status: int, data: bytes, content_type: str, filename: str | None = None):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _stream_file(self, path, filename: str, chunk: int = 1 << 20):
        """Serve a file from disk without loading it into memory — large slots go
        as a stream in 1 MB chunks (O(1) memory when serving a report)."""
        size = path.stat().st_size
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            while True:
                buf = f.read(chunk)
                if not buf:
                    break
                self.wfile.write(buf)

    def _authed(self) -> bool:
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        return self.server.pairing.valid(token)

    def _host_ok(self) -> bool:
        """The Host header must name exactly the loopback.

        The origin allowlist alone does NOT protect against DNS rebinding: an attacker
        serves a page at attacker.com:<our port>, rebinds that name to
        127.0.0.1 with a short TTL — and for the browser requests to
        attacker.com become same-origin. Origin is then not checked at all
        (CORS is not involved), the response is read in full, and all the pairing protection
        is bypassed. Binding to 127.0.0.1 does not help: the connection is opened by the
        analyst's own browser, which is on loopback anyway. This attack can be told apart
        only by Host — in it, it is foreign.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        name = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
        return name.strip("[]") in ("127.0.0.1", "localhost", "::1")

    def _pair_rate_limit(self) -> float:
        """No more than `_PAIR_RL_MAX` attempts of `/pair/request` per
        `_PAIR_RL_WINDOW_S` from one client — a sliding window, in process
        memory. The key is the client IP, not the Origin: the agent listens only on
        loopback (see `_host_ok`), so this is one physical user
        regardless of how many tabs they have open. Returns 0.0
        if the attempt is allowed (and counts it immediately), otherwise — how many
        seconds to wait (for the Retry-After header)."""
        key = self.client_address[0]
        now = time.monotonic()
        with self.server.pair_rl_lock:
            hits = self.server.pair_attempts.setdefault(key, [])
            cutoff = now - _PAIR_RL_WINDOW_S
            while hits and hits[0] < cutoff:
                hits.pop(0)
            if len(hits) >= _PAIR_RL_MAX:
                return hits[0] + _PAIR_RL_WINDOW_S - now
            hits.append(now)
            return 0.0

    def _read_body(self, limit: int = _MAX_UNAUTH_BODY) -> bytes:
        # A body to an unauthorized route can be sent by any open
        # tab: text/plain is a simple request, CORS does not block sending,
        # only reading the response. We do not allocate by the declared Content-Length.
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n > limit:
            raise _BodyTooLarge(n, limit)
        return self.rfile.read(n) if n > 0 else b""

    # ── routes ──────────────────────────────────────────────────────────
    def do_OPTIONS(self):  # noqa: N802
        if not self._host_ok():
            self._json(421, {"error": "bad_host"})
            return
        # [mirror] A preflight request to a mirror address must get the answer of the
        # MIRROR, not the generic 204 with an ACAO echo and `Allow-Private-Network`:
        # otherwise the promise "a foreign origin is rejected" (`libui`, the decision about
        # Origin) held only on the POST itself, while the platform page passed the preflight —
        # i.e. the browser considered a cross-domain POST to
        # `/library/key` allowed and sent it. The shell and delivery
        # answer OPTIONS with an honest 405 without CORS headers, and the preflight
        # does not pass.
        if (foldersui.serve_folders_ui(self) or mirror_libui.serve_library(self)
                or mirror_serve.serve(self)):
            return
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if not self._host_ok():
            self._json(421, {"error": "bad_host"})
            return
        self._route_read()

    def do_HEAD(self):  # noqa: N802
        """HEAD goes through the same routes as GET — just without a body.

        `BaseHTTPRequestHandler` has no `do_HEAD` of its own, and without this method
        ANY HEAD got 501 — even though both mirror delivery and the shell
        are written as "body unless HEAD" (`serve._respond_file`, `libui._reply`,
        `_stream_file` below), and the shell's `Allow` header explicitly
        promises HEAD. A promise that only GET keeps is a 501 in reply to
        an honest request from a browser, proxy or monitor.
        """
        if not self._host_ok():
            self._json(421, {"error": "bad_host"})
            return
        self._route_read()

    def _route_read(self):
        """Read routes: the shared body of `do_GET` and `do_HEAD` (Host is already checked).

        One body for two methods, not `do_HEAD = do_GET`: the run event stream
        cannot manage a body, and on HEAD it needs a separate answer (see
        below). These two routings must not diverge — otherwise HEAD will again
        start answering differently from GET.
        """
        # [window/mirror] The folders screen (`/folders-ui/...`), the library shell
        # (`/library/...`) and report delivery (`/mirror/...`) go BEFORE the
        # agent routes and answer by themselves: False means "not their address", and
        # then parsing continues below. The three prefixes intersect neither
        # with each other nor with the agent routes, so the order here is a matter
        # of readability, not correctness. The Host check (`_host_ok`) has already
        # passed above and applies to all three.
        if (foldersui.serve_folders_ui(self) or mirror_libui.serve_library(self)
                or mirror_serve.serve(self)):
            return
        path, _, query = self.path.partition("?")
        if path == "/health":
            p = self.server.pairing
            self._json(200, {
                "app": APP, "version": VERSION, "protocol": PROTOCOL,
                "paired": p.paired, "busy": self.server.busy.is_set(),
                "agent_id": p.agent_id,
                # From this field the page knows what to ask of the person:
                # confirm at the icon or enter a code.
                "ui": self.server.ui is not None,
            })
            return
        if path == "/pair/status":
            self._pair_status(query)
            return
        if path == "/folders":
            self._folders(query)
            return
        if path == "/local-runs":
            self._local_runs(query)
            return
        if path == "/local-runs/output":
            self._local_run_output(query)
            return
        m = _RUN_EVENTS_RE.match(path)
        if m:
            if self.command == "HEAD":
                # The NDJSON stream is held open for the whole run and is written to the socket
                # regardless of the method: on HEAD it would send a body and occupy the
                # connection until the end of the run. An honest refusal is better.
                self._json(405, {"error": "method_not_allowed"},
                           extra_headers={"Allow": "GET"})
                return
            self._events(m.group(1))
            return
        m = _RUN_OUTPUT_RE.match(path)
        if m:
            self._output(m.group(1), m.group(2))
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        if not self._host_ok():
            self._json(421, {"error": "bad_host"})
            return
        # [window/mirror] The folders screen (`/folders-ui/...`), the library shell
        # (`/library/...`) and report delivery (`/mirror/...`) go BEFORE the
        # agent routes and answer by themselves: False means "not their address", and
        # then parsing continues below. The three prefixes intersect neither
        # with each other nor with the agent routes, so the order here is a matter
        # of readability, not correctness. The Host check (`_host_ok`) has already
        # passed above and applies to all three.
        if (foldersui.serve_folders_ui(self) or mirror_libui.serve_library(self)
                or mirror_serve.serve(self)):
            return
        path = self.path.split("?", 1)[0]
        if path == "/pair/request":
            retry_after = self._pair_rate_limit()
            if retry_after > 0:
                secs = max(1, int(retry_after) + 1)
                self._json(429, {"error": "rate_limited", "retry_after": secs},
                           extra_headers={"Retry-After": str(secs)})
                return
            self._pair_request()
            return
        if path == "/pair":
            self._pair()
            return
        if path == "/runs":
            self._start_run()
            return
        if path == "/local-runs/published":
            self._local_run_published()
            return
        self._json(404, {"error": "not_found"})

    def do_DELETE(self):  # noqa: N802
        if not self._host_ok():
            self._json(421, {"error": "bad_host"})
            return
        # [window/mirror] The folders screen (`/folders-ui/...`), the library shell
        # (`/library/...`) and report delivery (`/mirror/...`) go BEFORE the
        # agent routes and answer by themselves: False means "not their address", and
        # then parsing continues below. The three prefixes intersect neither
        # with each other nor with the agent routes, so the order here is a matter
        # of readability, not correctness. The Host check (`_host_ok`) has already
        # passed above and applies to all three.
        if (foldersui.serve_folders_ui(self) or mirror_libui.serve_library(self)
                or mirror_serve.serve(self)):
            return
        m = _RUN_RE.match(self.path.split("?", 1)[0])
        if m:
            self._cancel(m.group(1))
            return
        self._json(404, {"error": "not_found"})

    # ── pairing ─────────────────────────────────────────────────────────
    def _pair_request(self):
        """Start pairing. The AGENT chooses the channel, not the page: if there is a UI —
        the person confirms with a click, otherwise — enters a code from the console."""
        ui = self.server.ui
        if ui is not None:
            rid, ttl = self.server.pairing.request_approval(self.headers.get("Origin"))
            # We only wake the UI thread. The dialog itself is shown there: a modal window
            # from an HTTP thread would freeze the whole agent until the person answers.
            ui.wake()
            self._json(200, {"mode": "approve", "request_id": rid, "expires_in": int(ttl)})
            return
        code, ttl = self.server.pairing.request_code()
        # We print the code to the agent's CONSOLE — a channel owned by whoever sits at the machine.
        print("\n" + "=" * 40, flush=True)
        print(f"  PAIRING CODE: {code}   (valid for {int(ttl)} s)", flush=True)
        print("  Enter it on the OnePage page.", flush=True)
        print("=" * 40 + "\n", flush=True)
        self._json(200, {"mode": "code", "expires_in": int(ttl)})

    def _pair_status(self, query: str):
        rid = ""
        for part in query.split("&"):
            k, _, v = part.partition("=")
            if k == "id":
                rid = unquote(v)
        if not rid:
            self._json(400, {"error": "missing_id"})
            return
        st = self.server.pairing.approval_status(rid)
        if st.get("state") == "approved":
            st = dict(st, agent_id=self.server.pairing.agent_id)
        self._json(200, st)

    def _pair(self):
        try:
            body = json.loads(self._read_body() or b"{}")
        except _BodyTooLarge:
            self._json(413, {"error": "body_too_large"})
            return
        except ValueError:
            self._json(400, {"error": "bad_json"})
            return
        token = self.server.pairing.redeem(str(body.get("code", "")))
        if not token:
            self._json(403, {"error": "pairing_failed"})
            return
        self._json(200, {"token": token, "agent_id": self.server.pairing.agent_id})

    # ── folders ─────────────────────────────────────────────────────────
    def _folders(self, query: str):
        """`GET /folders?dashboard=<id>[&scenario=<name>]` — contract K1.

        The same pairing Bearer check as for a run: a list of paths on the person's
        machine is information about their workplace, and there is no reason to hand it to any
        open tab. There is NOT A SINGLE mutation: the handler only reads
        `folders.ini` and the disk, so it is safe to call from the page before and
        instead of a run.
        """
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        args = dict(parse_qsl(query, keep_blank_values=True))
        dashboard = (args.get("dashboard") or "").strip()
        scenario = (args.get("scenario") or "").strip()
        if not dashboard:
            # An empty `dashboard` is not "no folders" but an unasked question.
            self._json(400, {"ok": False, "error": "missing_dashboard"})
            return
        try:
            status, body = folders_payload(dashboard, scenario)
        except Exception as e:                      # noqa: BLE001
            # `folders.load/resolve` by contract do not raise, but the handler also reads
            # the disk: it can break here, and we must not kill the handler thread
            # (and leave the page without an answer).
            self._json(500, {"ok": False, "error": "internal",
                             "message": str(e)[:300]})
            return
        self._json(status, body)

    # ── offline runs and the catch-up queue ─────────────────────────────
    def _local_runs(self, query: str):
        """`GET /local-runs[?dashboard=<id>]` — what was built offline and not yet
        put on the platform.

        It answers exactly the catch-up question: the OnePage page, opening after
        a lull, takes from here a run together with the SAVED `input_manifest`,
        fetches the outputs through the neighbouring handler and publishes them via the ordinary
        `POST …/runs/{id}/publish`. No new handler on the SERVER is needed for
        this — this one, on the agent, is.

        The same Bearer check as for a run: the list says which reports the
        person computed locally and when — information about their work, not
        a public reference. Zero mutations.
        """
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        args = dict(parse_qsl(query, keep_blank_values=True))
        dashboard = (args.get("dashboard") or "").strip() or None
        try:
            items = mirror_localrun.list_unpublished(dashboard, with_input=True)
        except Exception as e:                                 # noqa: BLE001
            self._json(500, {"ok": False, "error": "internal",
                             "message": str(e)[:300]})
            return
        self._json(200, {"ok": True, "runs": items})

    def _local_run_output(self, query: str):
        """`GET /local-runs/output?dashboard=&k=&name=` — bytes of one output.

        The name is checked against the run's inventory inside `localrun.output_path`, not
        concatenated with a directory: the string comes over the network, and `..` in it is
        reading foreign files over loopback.
        """
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        args = dict(parse_qsl(query, keep_blank_values=True))
        dashboard = (args.get("dashboard") or "").strip()
        name = args.get("name") or ""
        try:
            k = int(args.get("k") or "")
        except ValueError:
            self._json(400, {"error": "bad_k"})
            return
        path = mirror_localrun.output_path(dashboard, k, name)
        if path is None:
            self._json(404, {"error": "no_such_output"})
            return
        self._stream_file(path, name)

    def _local_run_published(self):
        """`POST /local-runs/published {dashboard, k}` — the run is published.

        The mark is set by the page and only AFTER a successful publish: marking
        earlier means losing from the queue a run that did not
        reach the platform. So the handler does nothing by itself — it records the fact
        it was told about.
        """
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            body = self._read_body()
        except _BodyTooLarge:
            self._json(413, {"error": "body_too_large"})
            return
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            self._json(400, {"error": "bad_json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "bad_json"})
            return
        dashboard = str(payload.get("dashboard") or "").strip()
        k = payload.get("k")
        if isinstance(k, bool) or not isinstance(k, int):
            self._json(400, {"error": "bad_k"})
            return
        if not mirror_localrun.mark_published(dashboard, k):
            self._json(404, {"error": "no_such_run"})
            return
        self._json(200, {"ok": True})

    # ── run ─────────────────────────────────────────────────────────────
    def _start_run(self):
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        if self.server.busy.is_set():
            self._json(409, {"error": "busy"})
            return
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._json(400, {"error": "expected_multipart"})
            return
        try:
            fields = _parse_multipart(self._read_body(_MAX_RUN_BODY), ctype)
        except _BodyTooLarge:
            self._json(413, {"error": "body_too_large"})
            return
        bundle = fields.get("bundle")
        meta_raw = fields.get("meta")
        if bundle is None or meta_raw is None:
            self._json(400, {"error": "missing_bundle_or_meta"})
            return
        try:
            meta = json.loads(meta_raw.decode("utf-8"))
        except ValueError:
            self._json(400, {"error": "bad_meta_json"})
            return

        run_id = secrets.token_urlsafe(9)
        work_dir = (self.server.state_root / "runs" / run_id) if self.server.state_root else None
        run = Run(run_id, work_dir)
        with self.server.runs_lock:
            self.server.runs[run_id] = run
        self.server.busy.set()

        # M1-echo (the latency-measuring kit) stays an echo task; everything else is
        # the real generator (GP sources + subprocess). An explicit marker mode="echo";
        # generator=="m1-echo" is a compatible fallback for an already distributed kit.
        is_echo = meta.get("mode") == "echo" or meta.get("generator") == "m1-echo"

        def _task():
            try:
                if is_echo:
                    run_echo(run, bundle, meta)
                else:
                    run_generator(run, bundle, meta,
                                  config=self.server.config,
                                  cred_store=self.server.cred_store,
                                  python_exe=self.server.python_exe,
                                  connect=self.server.gp_connect)
            finally:
                self.server.busy.clear()
                _announce(self.server, run)

        threading.Thread(target=_task, name=f"run-{run_id}", daemon=True).start()
        self._json(202, {"agent_run_id": run_id})

    def _get_run(self, run_id: str) -> Run | None:
        with self.server.runs_lock:
            return self.server.runs.get(run_id)

    def _events(self, run_id: str):
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        run = self._get_run(run_id)
        if run is None:
            self._json(404, {"error": "no_such_run"})
            return
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for ev in run.stream():
                line = (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")
                self.wfile.write(b"%X\r\n" % len(line))
                self.wfile.write(line)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The page disconnected (closed the tab/lid) — cancel the run.
            run.cancel()

    def _output(self, run_id: str, name: str):
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        run = self._get_run(run_id)
        if run is None:
            self._json(404, {"error": "no_such_run"})
            return
        if name in run.outputs:                       # small (echo) — from memory
            self._bytes(200, run.outputs[name], "application/octet-stream", filename=name)
            return
        path = run.output_paths.get(name)             # large (slots) — from disk
        if path is not None and path.is_file():
            self._stream_file(path, name)
            return
        self._json(404, {"error": "no_such_output"})

    def _cancel(self, run_id: str):
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        run = self._get_run(run_id)
        if run is None:
            self._json(404, {"error": "no_such_run"})
            return
        run.cancel()
        self._json(200, {"canceled": True})


def _announce(server, run) -> None:
    """Run outcome -> icon colour and pop-up notification.

    Without this a failure is visible only on the page, and the analyst may have already closed
    the window with it. The icon is the only thing that stays on screen always.
    """
    ui = getattr(server, "ui", None)
    ctl = getattr(server, "controller", None)
    if ui is None and ctl is None:
        return
    ev = run.terminal() or {}
    failed = ev.get("t") == "error"
    msg = (ev.get("message") or "the run failed") if failed else None
    try:
        if ctl is not None:
            ctl.set_error(msg)
        if ui is not None:
            if failed:
                ui.notify("Report not built", msg, error=True)
            else:
                ui.notify("Report ready", "Data exported, report published.")
    except Exception:                      # noqa: BLE001
        pass                               # a notification must not be allowed to break a run


# ── minimal multipart parser (cgi was removed in 3.13; email is finicky with binary) ──
def _parse_multipart(body: bytes, content_type: str) -> dict[str, bytes]:
    """Parse multipart/form-data into {name: bytes}. Byte-exact for binary
    fields (bundle-zip): strips EXACTLY the framing CRLF, not touching the content."""
    m = re.search(r'boundary=("?)([^";]+)\1', content_type)
    if not m:
        return {}
    delim = b"--" + m.group(2).encode("utf-8")
    fields: dict[str, bytes] = {}
    for raw in body.split(delim):
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        if raw.startswith(b"\r\n"):
            raw = raw[2:]
        if raw in (b"", b"--"):
            continue
        if b"\r\n\r\n" not in raw:
            continue
        head, content = raw.split(b"\r\n\r\n", 1)
        nm = re.search(r'name="([^"]+)"', head.decode("utf-8", "replace"))
        if nm:
            fields[nm.group(1)] = content
    return fields


# ── /folders: what exactly the agent will read (READ ONLY) ─────────────────
# Time limit for enumeration. A network share answers in minutes (or does
# not answer at all), and the handler is synchronous: without a limit one call to
# a dead `\\fs01\...` would hold the page's connection for as long, and the person
# would see "the page could not reach the agent" instead of "the folder is not responding".
# So the answer is PARTIAL and honestly says so, rather than empty or eternal.
_FOLDERS_BUDGET_S = 8.0


def _root_name(root_path: str) -> str:
    """Short folder NAME — the source key on the platform.

    Why a separate field at all, and not the path. `DataSource` is looked up by
    `key_name`. The browser knows no paths in principle (File System Access gives
    only a handle) and puts the folder NAME there; a folder run that put
    the full path there would create a SECOND source for the very same folder,
    and the history of one report would split in two (owner's decision of 19.10 — send the name).
    The full path is not lost: it travels alongside in `root_path`, and the sources
    page shows exactly it.

    The name is computed by the AGENT, not the page: `D:\\Exports` and
    `\\\\share\\katalog` must be parsed by whoever knows their OS, not JS, which
    is on Windows at the analyst's and on macOS at the developer's.
    """
    raw = (root_path or "").rstrip("\\/")
    if not raw:
        return root_path or ""
    # UNC: `\\server\share` as a whole is the name — `share` cannot be cut off from the server,
    # they are not recognisable separately.
    if raw.startswith("\\\\"):
        parts = [p for p in raw[2:].split("\\") if p]
        if len(parts) <= 2:
            return raw
        return parts[-1]
    name = raw.replace("\\", "/").rstrip("/").rpartition("/")[2]
    return name or raw


def _root_of(path: str) -> str:
    """Directory of a file by an already NORMALIZED path from `folders.ResolvedFile`.

    Its own function instead of `os.path.dirname`, because the path may have come with a
    Windows separator even on POSIX (UNC `\\\\server\\share\\a.xlsx`
    is normalized by the `folders._norm` branch, while `os.path.dirname` on POSIX
    does not treat a backslash as a separator and would return the whole path — the share root
    would become a "file", and `root_id` would diverge from what the run
    shows).
    """
    cut = max(path.rfind("/"), path.rfind("\\"))
    return path[:cut] if cut > 0 else path


def _resolve_budgeted(src, budget_s: float):
    """`folders.resolve`, but with a time limit and entry by entry.

    Enumeration runs in a separate thread and accumulates the result PER ENTRY:
    an entry that made it goes into the answer whole; the one on which the
    budget ran out goes into `pending`. The thread stays a daemon and finishes by itself —
    there is no way to interrupt `os.scandir` on a hung share, and holding the
    connection because of it is not allowed. The answer does not lie because of it: we do not pass off the uncounted as
    complete.

    Parsing one entry at a time (a `Source` from one `Entry`) instead of one call
    for the whole source is needed exactly for this — and it is safe: `resolve`
    of one entry ALWAYS yields either file(s) or a missing one, so the "empty
    section" (the only case `resolve` completes by itself) does not get
    here. We handle it in a separate branch below so that the text of the missing
    stays the same as what the run will see.
    """
    entries = tuple(getattr(src, "entries", ()) or ())
    if not entries:
        return folders.resolve(src), []

    done: dict[int, tuple[list, list]] = {}
    lock = threading.Lock()

    def work():
        for i, e in enumerate(entries):
            one = folders.Source(dashboard_id=src.dashboard_id, entries=(e,),
                                 scenario=src.scenario)
            r = folders.resolve(one)
            # `entry_index` in a single-entry source is always 0 — we give it back
            # the real section entry number, otherwise `dir` in the answer would point
            # to the first entry for all files.
            files = [replace(f, entry_index=i) for f in r.files]
            with lock:
                done[i] = (files, list(r.missing))

    th = threading.Thread(target=work, name="folders-scan", daemon=True)
    th.start()
    th.join(budget_s)

    with lock:
        snapshot = dict(done)

    files: list = []
    missing: list = []
    pending: list[dict] = []
    for i, e in enumerate(entries):
        got = snapshot.get(i)
        if got is None:
            pending.append({"index": i, "raw": e.raw})
            continue
        files.extend(got[0])
        missing.extend(got[1])
    return folders.Resolved(files=tuple(files), missing=tuple(missing)), pending


def folders_payload(dashboard: str, scenario: str = "",
                    budget_s: float = _FOLDERS_BUDGET_S) -> tuple[int, dict]:
    """Contract K1: what the agent will read for this report. Changes nothing.

    The answer MUST match how a run will behave
    (`agent/runs.py::_resolve_folder_files`): the same `folders.load`, the same
    key `<id>[:<scenario>]`, the same order of refusals (first "no scenario",
    if the ini says anything about the report at all, otherwise "no section") and exactly
    the same count of duplicate names. To diverge here is to show a person
    a list of files and then refuse on the run for another reason; worse than that
    is only a silent empty run.
    """
    ini = os.path.abspath(str(folders.default_ini_path()))
    sources = folders.load(ini)
    key = f"{dashboard}:{scenario}" if scenario else dashboard
    src = sources.get(key)
    if src is None:
        known = folders.scenarios_for(sources, dashboard)
        if known:
            return 404, {"ok": False, "error": "no_scenario", "scenarios": known}
        return 404, {"ok": False, "error": "no_section"}

    res, pending = _resolve_budgeted(src, budget_s)

    files = [{"name": f.name, "size": f.size, "mtime_ms": f.mtime_ms,
              "path": f.path, "dir": f.entry_index} for f in res.files]

    # Roots — one per section entry that yielded files. `root_path` is taken from
    # the file itself, not from `raw`: `raw` holds the person's spelling (slashes, `..`,
    # trailing slash), while `root_id` must be stable against spelling, otherwise
    # two entries of one folder would give two different keys.
    roots: list[dict] = []
    seen_dirs: set[int] = set()
    for f in res.files:
        if f.entry_index in seen_dirs:
            continue
        seen_dirs.add(f.entry_index)
        root_path = _root_of(f.path)
        roots.append({"dir": f.entry_index, "root_path": root_path,
                      "root_id": folders.identity(root_path),
                      "name": _root_name(root_path)})

    # Duplicate names — the same count as in the run: the generator tells files apart
    # by NAME, so one name from two folders it will read as one file.
    by_name: dict[str, list[str]] = {}
    for f in res.files:
        by_name.setdefault(f.name, []).append(f.path)
    duplicates = [{"name": n, "paths": p} for n, p in by_name.items() if len(p) > 1]

    out = {
        "ok": True,
        "ini": ini,
        "scenario": src.scenario,
        "scenarios": folders.scenarios_for(sources, dashboard),
        "entries": [{"index": i, "kind": e.kind, "raw": e.raw,
                     "include": list(e.include), "exclude": list(e.exclude)}
                    for i, e in enumerate(src.entries)],
        "files": files,
        "roots": roots,
        "missing": [{"raw": m.raw, "reason": m.reason} for m in res.missing],
        "duplicates": duplicates,
        "totals": {"count": len(files), "bytes": sum(f["size"] for f in files)},
    }
    if pending:
        # The incompleteness flag is not cosmetics: without it a partial list
        # reads as complete, and the person will decide that half the folders are empty.
        out["partial"] = True
        out["budget_s"] = budget_s
        out["pending"] = pending
    return 200, out


def folders_cli(argv) -> int:
    """`python -m agent folders [dashboard_id] [scenario]` — the same answer as
    `GET /folders`, as human-readable text.

    Needed exactly for the case when the page did NOT reach the agent (not
    running, port taken, proxy, pairing lost): at that moment the handler's answer is
    unavailable, while the question "which files does it see at all" is the first one. So the
    command goes not over HTTP but calls the same `server.folders_payload`: two
    sources of truth would diverge exactly when it is hardest
    to investigate.
    """
    ini = folders.default_ini_path()
    if not argv:
        # With no argument — what is declared at all. A person often does not remember the
        # report number, and that is the section name.
        sources = folders.load(ini)
        print(f"config: {ini}  [{'present' if os.path.isfile(str(ini)) else 'MISSING'}]")
        if not sources:
            print("no [folders:<id>] sections found — nothing to read.")
            print("Sample: deploy/agent_tools/folders.ini.example")
            return 1
        for key in sorted(sources):
            src = sources[key]
            name = src.scenario or "(unnamed)"
            print(f"  report {src.dashboard_id}, scenario {name}: "
                  f"entries {len(src.entries)}")
        # We suggest the very command the person called us with: in the kit it is
        # `python op_agent.py`, in the repository `python -m agent`.
        prog = os.path.basename(sys.argv[0] or "")
        prog = "python -m agent" if prog in ("", "__main__.py") else f"python {prog}"
        print(f"\nDetails:  {prog} folders <id> [scenario]")
        return 0

    dashboard = argv[0].strip()
    scenario = argv[1].strip() if len(argv) > 1 else ""
    status, body = folders_payload(dashboard, scenario)
    if status != 200:
        err = body.get("error")
        if err == "no_scenario":
            print(f"{ini} has no scenario {scenario!r} for report {dashboard}.",
                  file=sys.stderr)
            print("Available: " + ", ".join(n or "(unnamed)"
                                       for n in body.get("scenarios") or []),
                  file=sys.stderr)
        else:
            print(f"No data folders have been chosen for report {dashboard} yet.",
                  file=sys.stderr)
            print("Choose with the mouse: op-agent.bat folders -> \"Add report\" -> "
                  "\"Browse…\".", file=sys.stderr)
            print(f"Type by hand: {ini} (the sample is alongside — "
                  "folders.ini.example).", file=sys.stderr)
        return 1

    print(f"config   : {body['ini']}")
    print(f"report   : {dashboard}")
    print(f"scenario : {body['scenario'] or '(unnamed)'}"
          + (f"   (all: {', '.join(n or '(unnamed)' for n in body['scenarios'])})"
             if len(body["scenarios"]) > 1 else ""))
    for e in body["entries"]:
        masks = ""
        if e["include"]:
            masks += "  include=" + ", ".join(e["include"])
        if e["exclude"]:
            masks += "  exclude=" + ", ".join(e["exclude"])
        print(f"  [{e['index']}] {e['kind']:4} {e['raw']}{masks}")
    for r in body["roots"]:
        print(f"  root [{r['dir']}] {r['root_path']}  id={r['root_id']}")
    print(f"files    : {body['totals']['count']}, "
          f"{body['totals']['bytes'] / (1 << 20):.1f} MB")
    for f in body["files"][:50]:
        print(f"    {f['name']}  {f['size']} B  [{f['dir']}]  {f['path']}")
    if len(body["files"]) > 50:
        print(f"    … and {len(body['files']) - 50} more")
    # Missing items and duplicates are ALWAYS printed, and to stderr: these are the very reasons
    # why a run will refuse, and staying silent about them is the main mistake here.
    for m in body["missing"]:
        print(f"  [!] {m['raw']}: {m['reason']}", file=sys.stderr)
    for d in body["duplicates"]:
        print(f"  [!] one name in different folders — {d['name']}: "
              + " and ".join(d["paths"]), file=sys.stderr)
    if body.get("partial"):
        print(f"  [!] the list is INCOMPLETE: in {body['budget_s']:.0f} s not all "
              f"entries were done, not enumerated:", file=sys.stderr)
        for pend in body["pending"]:
            print(f"      [{pend['index']}] {pend['raw']}", file=sys.stderr)
    # The return code is applied: a run with this config will NOT go now.
    bad = (not body["files"]) or body["duplicates"] or body.get("partial")
    return 1 if bad else 0
