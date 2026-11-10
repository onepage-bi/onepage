"""Smoke test of the M1 skeleton of op-agent. Standard library only.

Starts the daemon in-process on an ephemeral port and runs the whole transport
that the owner will measure on a laptop: health, pairing, Bearer authorization,
multipart with a real zip bundle + sha256, the NDJSON event stream, serving bytes,
checksum-mismatch detection, single-instance bind.

Run:     python -m agent.tests.smoke_agent   (from the OnePage repo root)
"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import sys
import threading
import time
import zipfile

# Import of the agent package — the test is run from the repo root (python -m agent.tests...).
from agent.config import Config
from agent.pairing import Pairing
from agent.server import AgentServer

_PASS = 0
_FAIL = 0
ORIGIN = "https://onepage.example.com"
BAD_ORIGIN = "https://evil.example"


def check(label, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"PASS {label}")
    else:
        _FAIL += 1
        print(f"FAIL {label}  {extra}")


def build_bundle() -> tuple[bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"name": "echo-test", "version": "1"}))
        z.writestr("sql/01_sales.sql", "select 1")
        # A binary member ending in \r\n — a trap for a naive multipart parser.
        z.writestr("data/blob.bin", b"\x00\xffbinary-with-crlf-tail\r\n")
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def multipart(bundle: bytes, meta: dict) -> tuple[bytes, str]:
    boundary = "----optest-boundary-xyz"
    b = boundary.encode()
    out = [
        b"--" + b + b"\r\n",
        b'Content-Disposition: form-data; name="bundle"; filename="b.zip"\r\n',
        b"Content-Type: application/zip\r\n\r\n", bundle, b"\r\n",
        b"--" + b + b"\r\n",
        b'Content-Disposition: form-data; name="meta"\r\n\r\n',
        json.dumps(meta).encode("utf-8"), b"\r\n",
        b"--" + b + b"--\r\n",
    ]
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


class Client:
    def __init__(self, port, token=None):
        self.port = port
        self.token = token

    def req(self, method, path, body=None, ctype=None, origin=None, auth=True,
            host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if host:
            headers["Host"] = host
        if ctype:
            headers["Content-Type"] = ctype
        if origin:
            headers["Origin"] = origin
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        data = r.read()
        return r.status, dict(r.getheaders()), data


def main() -> int:
    cfg = Config(allowed_origins=(ORIGIN,), connections={})
    pairing = Pairing(code_factory=lambda: "123456")
    srv = AgentServer(("127.0.0.1", 0), cfg, pairing)
    import tempfile
    from pathlib import Path
    srv.state_root = Path(tempfile.mkdtemp(prefix="op-agent-test-"))
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)

    cl = Client(port)

    # 1. health without auth
    st, hdr, body = cl.req("GET", "/health", auth=False)
    j = json.loads(body)
    check("health -> 200 app=op-agent paired=false", st == 200 and j.get("app") == "op-agent" and j.get("paired") is False, f"{st} {j}")

    # 2. CORS: OPTIONS with an allowed origin echoes ACAO
    st, hdr, _ = cl.req("OPTIONS", "/runs", origin=ORIGIN, auth=False)
    check("OPTIONS allowed-origin -> ACAO echoed + PNA", hdr.get("Access-Control-Allow-Origin") == ORIGIN and hdr.get("Access-Control-Allow-Private-Network") == "true", str(hdr))

    # 3. CORS: a foreign origin — WITHOUT ACAO
    st, hdr, _ = cl.req("OPTIONS", "/runs", origin=BAD_ORIGIN, auth=False)
    check("OPTIONS bad-origin -> no ACAO", "Access-Control-Allow-Origin" not in hdr, str(hdr))

    # 4. a protected endpoint without a token -> 401
    st, _, _ = cl.req("GET", "/runs/x/events", auth=False)
    check("events without token -> 401", st == 401, str(st))

    # 5. wrong pairing code -> 403
    st, _, _ = cl.req("POST", "/pair", body=json.dumps({"code": "000000"}), ctype="application/json", auth=False)
    check("pair wrong code -> 403", st == 403, str(st))

    # 6. code request -> 200 (the code is in the console; the factory returns 123456).
    # Without a UI the agent MUST stay on the code channel: dev/macOS/headless.
    st, _, body = cl.req("POST", "/pair/request", auth=False)
    j = json.loads(body)
    check("pair/request -> 200", st == 200, str(st))
    check("without UI the pairing channel = code", j.get("mode") == "code", str(j))
    check("health reports the absence of a UI",
          json.loads(cl.req("GET", "/health", auth=False)[2]).get("ui") is False)

    # 6b. The same exchange, but via the confirmation channel — as on Windows with an icon.
    class _FakeUI:
        def __init__(self): self.woken = 0
        def wake(self): self.woken += 1
    fake_ui = _FakeUI()
    srv.ui = fake_ui
    st, _, body = cl.req("POST", "/pair/request", origin=ORIGIN, auth=False)
    j = json.loads(body)
    rid = j.get("request_id")
    check("with UI the pairing channel = confirmation", st == 200 and j.get("mode") == "approve", str(j))
    check("UI thread woken", fake_ui.woken == 1, str(fake_ui.woken))
    st, _, body = cl.req("GET", f"/pair/status?id={rid}", auth=False)
    check("status before the person's answer — pending",
          st == 200 and json.loads(body).get("state") == "pending", body[:120])
    # The person clicked "Allow" (in real life the UI thread does this).
    pairing.take_pending()
    pairing.resolve(rid, True)
    st, _, body = cl.req("GET", f"/pair/status?id={rid}", auth=False)
    j = json.loads(body)
    check("after confirmation — token", st == 200 and j.get("state") == "approved" and bool(j.get("token")), body[:160])
    st, _, body = cl.req("GET", "/pair/status?id=", auth=False)
    check("status without id -> 400", st == 400, str(st))
    pairing.revoke_all()
    srv.ui = None

    # 7. correct code -> token
    st, _, body = cl.req("POST", "/pair", body=json.dumps({"code": "123456"}), ctype="application/json", auth=False)
    tok = json.loads(body).get("token") if st == 200 else None
    check("pair correct code -> token", st == 200 and bool(tok), f"{st} {body[:120]}")
    cl.token = tok

    # 8. start a run: multipart + correct sha256 -> 202
    bundle, checksum = build_bundle()
    mp, ctype = multipart(bundle, {"checksum_sha256": checksum, "run_id": "r1", "generator": "echo-test", "version": "1", "mode": "echo"})
    st, _, body = cl.req("POST", "/runs", body=mp, ctype=ctype)
    run_id = json.loads(body).get("agent_run_id") if st == 202 else None
    check("start run (good checksum) -> 202", st == 202 and bool(run_id), f"{st} {body[:120]}")

    # 9. NDJSON stream: we see the stages and a terminal done with the echo slot
    st, hdr, body = cl.req("GET", f"/runs/{run_id}/events")
    lines = [json.loads(x) for x in body.decode("utf-8").splitlines() if x.strip()]
    stages = {e.get("stage") for e in lines if e.get("t") == "stage"}
    done = next((e for e in lines if e.get("t") == "done"), None)
    check("events NDJSON -> stages received/unpack/echo",
          {"received", "unpack", "echo"}.issubset(stages), str(stages))
    check("events -> done with slot echo",
          done is not None and done.get("outputs", {}).get("slots", {}).get("echo") == "echo.json", str(done))
    check("events content-type ndjson + chunked",
          "ndjson" in hdr.get("Content-Type", "") and hdr.get("Transfer-Encoding") == "chunked", str(hdr))

    # 10. serving the output: bytes + JSON, bundle_files match, checksum_ok
    st, _, body = cl.req("GET", f"/runs/{run_id}/outputs/echo.json")
    out = json.loads(body) if st == 200 else {}
    check("output echo.json -> 200 + integrity",
          st == 200 and out.get("checksum_ok") is True
          and out.get("bundle_files") == ["data/blob.bin", "manifest.json", "sql/01_sales.sql"],
          f"{st} {out}")

    # 11. checksum-mismatch -> the run starts, but an error checksum_mismatch event
    mp2, ctype2 = multipart(bundle, {"checksum_sha256": "deadbeef", "run_id": "r2", "mode": "echo"})
    st, _, body = cl.req("POST", "/runs", body=mp2, ctype=ctype2)
    rid2 = json.loads(body).get("agent_run_id")
    st, _, body = cl.req("GET", f"/runs/{rid2}/events")
    lines2 = [json.loads(x) for x in body.decode("utf-8").splitlines() if x.strip()]
    err = next((e for e in lines2 if e.get("t") == "error"), None)
    check("checksum mismatch -> error event", err is not None and err.get("kind") == "checksum_mismatch", str(err))

    # 12. cancel of an unknown -> 404; of a known -> 200
    st, _, _ = cl.req("DELETE", "/runs/nope")
    check("cancel unknown run -> 404", st == 404, str(st))
    st, _, _ = cl.req("DELETE", f"/runs/{run_id}")
    check("cancel known run -> 200", st == 200, str(st))

    # 13. single-instance: a repeated bind of the same port fails with OSError
    try:
        AgentServer(("127.0.0.1", port), cfg, pairing)
        check("second bind same port -> OSError", False, "bind succeeded (should not)")
    except OSError:
        check("second bind same port -> OSError", True)

    # ── DNS rebinding: Origin does not help here, only Host saves ───────
    # An attacker serves a page at attacker.com:<agent port> and with a short TTL
    # rebinds the name to 127.0.0.1. For the browser this is same-origin, so
    # Origin is not checked at all and the response is read in full. The only thing
    # that distinguishes such a request from an honest one is the Host header.
    for path, method in (("/health", "GET"), ("/pair/request", "POST"),
                         ("/runs", "POST"), ("/runs/x", "DELETE")):
        st, _, _ = cl.req(method, path, host="attacker.com:1234")
        check(f"rebinding: {method} {path} -> 421", st == 421, f"got {st}")

    st, _, _ = cl.req("GET", "/health", host=f"127.0.0.1:{port}")
    check("an honest Host is accepted", st == 200, f"got {st}")
    st, _, _ = cl.req("GET", "/health", host=f"localhost:{port}")
    check("localhost is accepted too", st == 200, f"got {st}")

    # A body to an unauthorized route is not allocated by its declaration.
    st, _, _ = cl.req("POST", "/pair", body=b"x" * (128 * 1024),
                      ctype="application/json", auth=False)
    check("a huge body on /pair -> 413", st == 413, f"got {st}")

    srv.shutdown()
    srv.server_close()

    # ── rate limit /pair/request: N attempts/minute per client, 429 + Retry-After
    # A fresh server/pairing — the counter is independent of the calls above.
    from agent.server import _PAIR_RL_MAX
    cfg2 = Config(allowed_origins=(ORIGIN,), connections={})
    pairing2 = Pairing(code_factory=lambda: "654321")
    srv2 = AgentServer(("127.0.0.1", 0), cfg2, pairing2)
    srv2.state_root = Path(tempfile.mkdtemp(prefix="op-agent-test-rl-"))
    port2 = srv2.server_address[1]
    t2 = threading.Thread(target=srv2.serve_forever, daemon=True)
    t2.start()
    time.sleep(0.1)
    cl2 = Client(port2)

    for i in range(_PAIR_RL_MAX):
        st, _, body = cl2.req("POST", "/pair/request", auth=False)
        check(f"pair/request #{i + 1} within limit -> 200", st == 200, f"{st} {body}")

    st, hdr, body = cl2.req("POST", "/pair/request", auth=False)
    j = json.loads(body)
    check(f"pair/request #{_PAIR_RL_MAX + 1} over limit -> 429",
          st == 429 and j.get("error") == "rate_limited", f"{st} {j}")
    check("429 carries Retry-After", "Retry-After" in hdr and int(hdr["Retry-After"]) > 0, str(hdr))

    srv2.shutdown()
    srv2.server_close()

    # ── polling our own port goes AROUND the proxy ─────────────────────────
    # This is not hygiene but the post-mortem of the 12.10.2026 failure: the shortcut "OnePage — my reports"
    # on the owner's machine waited 20 seconds for the agent's health and gave up. `urlopen`
    # reads the environment's proxy settings (on Windows — from the registry), and `<local>` in
    # the exclusion list does not cover 127.0.0.1: by the CPython rule it
    # matches only a name WITHOUT dots. On a corporate machine the check of a
    # LIVE agent returned a refusal — and that read as "no agent",
    # also breaking the single-instance mutex (a second instance would take
    # the next port of the ladder). The test sets a dead proxy and requires that the
    # agent is still found.
    import os
    import agent.__main__ as agent_main

    srv3 = AgentServer(("127.0.0.1", 0), Config(allowed_origins=(ORIGIN,), connections={}),
                       Pairing(code_factory=lambda: "111111"))
    port3 = srv3.server_address[1]
    threading.Thread(target=srv3.serve_forever, daemon=True).start()
    saved = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY")}
    os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = "http://127.0.0.1:9/"
    try:
        check("health probe ignores the environment proxy",
              agent_main._is_our_agent(port3) is True)
        # And the control the other way round: without bypassing the proxy the same request would not pass —
        # otherwise the test is green and proves nothing.
        import urllib.error
        import urllib.request
        naive_failed = False
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port3}/health", timeout=1.0).read()
        except (urllib.error.URLError, OSError):
            naive_failed = True
        check("naive urlopen WOULD have failed through that proxy", naive_failed)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        srv3.shutdown()
        srv3.server_close()

    _check_folders_handle()

    print(f"\n{'ALL PASS' if _FAIL == 0 else 'FAILURES: ' + str(_FAIL)}  ({_PASS} passed)")
    return 1 if _FAIL else 0


def _check_folders_handle() -> None:
    """`GET /folders` (contract K1) + the kit command.

    What is pinned here is mainly NOT the shape of the answer but the match with the run:
    the handler and `runs._resolve_folder_files` must name a section the same way,
    refuse the same way and count duplicate names the same way. Were they to diverge —
    the page would show "36 files, all good", while the run refuses, and there
    would be nothing to explain it to the person with.
    """
    import os
    import tempfile
    from pathlib import Path

    from urllib.parse import quote

    from agent import folders
    from agent.runs import Run, RunError, _resolve_folder_files
    from agent.server import folders_payload

    root = Path(tempfile.mkdtemp(prefix="op-folders-test-"))
    a, b, empty = root / "A", root / "B", root / "Empty"
    for d in (a, b, empty):
        d.mkdir()
    (a / "SL_01.xlsx").write_bytes(b"x" * 10)
    (a / "SL_02.xlsx").write_bytes(b"y" * 20)
    (a / "~$SL_01.xlsx").write_bytes(b"junk")       # must be filtered out by exclude
    (a / "notes.txt").write_bytes(b"skip me")       # not under include
    (b / "ref.xlsx").write_bytes(b"z" * 30)

    ini = root / "folders.ini"
    ini.write_text(
        f"[folders:17]\n"
        f"dir = {a}\n"
        f"dir = {b}\n"
        f"file = {a / 'SL_01.xlsx'}\n"
        f"include = *.xlsx\n"
        f"exclude = ~$*\n"
        f"\n"
        f"[folders:17:été-B]\n"
        f"dir = {a}\n"
        f"include = *.xlsx\n"
        f"exclude = ~$*\n"
        f"\n"
        f"[folders:18]\n"
        f"dir = {empty}\n"
        f"dir = {root / 'Absent_é'}\n"
        f"include = *.xlsx\n",
        encoding="utf-8")

    saved_ini = os.environ.get("OP_FOLDERS_INI")
    os.environ["OP_FOLDERS_INI"] = str(ini)

    cfg = Config(allowed_origins=(ORIGIN,), connections={})
    pairing = Pairing(code_factory=lambda: "222222")
    srv = AgentServer(("127.0.0.1", 0), cfg, pairing)
    srv.state_root = root
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.1)
    cl = Client(srv.server_address[1])
    try:
        # Without a token — 401: a list of paths on the person's machine is not for any tab.
        st, _, _ = cl.req("GET", "/folders?dashboard=17", auth=False)
        check("folders without token -> 401", st == 401, str(st))

        cl.req("POST", "/pair/request", auth=False)      # the code from the factory above
        cl.token = json.loads(cl.req("POST", "/pair", body=json.dumps({"code": "222222"}),
                                     ctype="application/json", auth=False)[2])["token"]

        st, _, body = cl.req("GET", "/folders")
        check("folders without dashboard -> 400",
              st == 400 and json.loads(body).get("error") == "missing_dashboard",
              f"{st} {body[:120]}")

        # No section at all / the report exists but no such scenario — DIFFERENT refusals,
        # and the order is the same as in runs.py.
        st, _, body = cl.req("GET", "/folders?dashboard=999")
        check("folders unknown dashboard -> 404 no_section",
              st == 404 and json.loads(body).get("error") == "no_section",
              f"{st} {body[:160]}")
        st, _, body = cl.req("GET", "/folders?dashboard=17&scenario=" + quote("none"))
        j = json.loads(body)
        check("folders unknown scenario -> 404 no_scenario + list",
              st == 404 and j.get("error") == "no_scenario"
              and j.get("scenarios") == ["", "été-B"], f"{st} {j}")

        # Snapshot of the folders BEFORE and AFTER: the handler must be read-only.
        before = sorted((f.name, f.stat().st_size, f.stat().st_mtime_ns)
                        for f in a.iterdir())

        st, _, body = cl.req("GET", "/folders?dashboard=17")
        j = json.loads(body)
        check("folders -> 200 ok", st == 200 and j.get("ok") is True, f"{st} {body[:160]}")
        check("folders: ini — the absolute path from OP_FOLDERS_INI",
              j.get("ini") == str(ini), str(j.get("ini")))
        check("folders: the unnamed scenario and the list of all",
              j.get("scenario") == "" and j.get("scenarios") == ["", "été-B"], str(j))
        check("folders: section entries in order, with masks",
              [(e["index"], e["kind"], e["raw"]) for e in j["entries"]]
              == [(0, "dir", str(a)), (1, "dir", str(b)), (2, "file", str(a / "SL_01.xlsx"))]
              and j["entries"][0]["include"] == ["*.xlsx"]
              and j["entries"][0]["exclude"] == ["~$*"], str(j["entries"]))
        names = [f["name"] for f in j["files"]]
        check("folders: masks applied (~$ and .txt filtered out)",
              names == ["SL_01.xlsx", "SL_02.xlsx", "ref.xlsx", "SL_01.xlsx"], str(names))
        check("folders: dir points to ITS OWN section entry",
              [f["dir"] for f in j["files"]] == [0, 0, 1, 2],
              str([f["dir"] for f in j["files"]]))
        check("folders: size/mtime_ms/path filled in",
              all(f["size"] > 0 and f["mtime_ms"] > 0 and os.path.isabs(f["path"])
                  for f in j["files"]), str(j["files"][:2]))
        check("folders: totals counts the same as files",
              j["totals"]["count"] == 4
              and j["totals"]["bytes"] == sum(f["size"] for f in j["files"]),
              str(j["totals"]))
        roots = {r["dir"]: r for r in j["roots"]}
        check("folders: a root per entry, root_id = folders.identity",
              set(roots) == {0, 1, 2}
              and roots[0]["root_path"] == str(a)
              and roots[0]["root_id"] == folders.identity(a)
              and roots[2]["root_path"] == str(a)
              and roots[1]["root_id"] == folders.identity(b), str(j["roots"]))
        check("folders: the duplicate name is named with BOTH paths",
              j["duplicates"] == [{"name": "SL_01.xlsx",
                                   "paths": [str(a / "SL_01.xlsx")] * 2}],
              str(j["duplicates"]))

        after = sorted((f.name, f.stat().st_size, f.stat().st_mtime_ns)
                       for f in a.iterdir())
        check("folders: the handler changed nothing on disk", before == after)

        # And the same config through the RUN's eyes: a refusal exactly on the duplicate
        # the handler showed (otherwise the page and the run lie in different ways).
        run = Run("t", root / "runs" / "t")
        try:
            _resolve_folder_files(run, {"dashboard_id": "17"})
            check("the run refuses on the same duplicate", False, "there was no refusal")
        except RunError as e:
            check("the run refuses on the same duplicate",
                  e.kind == "folders_duplicate_names" and "SL_01.xlsx" in e.detail,
                  f"{e.kind} {e.detail}")

        # A named scenario — its own set, with no duplicates in it.
        st, _, body = cl.req("GET", "/folders?dashboard=17&scenario=" + quote("été-B"))
        j2 = json.loads(body)
        check("folders: the named scenario gives its own set",
              st == 200 and j2["scenario"] == "été-B"
              and [f["name"] for f in j2["files"]] == ["SL_01.xlsx", "SL_02.xlsx"]
              and j2["duplicates"] == [], f"{st} {body[:200]}")

        # Missing items: a folder empty after masks and a nonexistent path — out loud.
        st, _, body = cl.req("GET", "/folders?dashboard=18")
        j3 = json.loads(body)
        reasons = {(m["raw"], m["reason"]) for m in j3["missing"]}
        check("folders: no_match and not_found are named out loud",
              st == 200 and j3["files"] == []
              and reasons == {(str(empty), "no_match"), (str(root / "Absent_é"), "not_found")},
              f"{st} {body[:220]}")

        # Time limit: a slow share gives a PARTIAL answer with an incompleteness
        # flag, not a hung connection. We substitute `resolve` (`folders.py`
        # itself is not touched) and ask for a zero budget.
        orig_resolve = folders.resolve

        def _slow(src):
            time.sleep(0.3)
            return orig_resolve(src)

        folders.resolve = _slow
        try:
            status, part = folders_payload("17", budget_s=0.05)
        finally:
            folders.resolve = orig_resolve
        check("folders: expired budget -> partial, not emptiness and not a hang",
              status == 200 and part.get("partial") is True
              and [p["index"] for p in part.get("pending", [])] == [0, 1, 2]
              and part["files"] == [], str(part)[:240])

        # The kit command answers the same and with its return code says "the run
        # will not go" (here — because of a duplicate name).
        import agent.__main__ as agent_main
        check("kit: folders <id> -> code 1 on a duplicate",
              agent_main._folders_cmd(["17"]) == 1)
        check("kit: folders <id> <scenario> -> code 0 on a healthy set",
              agent_main._folders_cmd(["17", "été-B"]) == 0)
        check("kit: folders without arguments lists the sections",
              agent_main._folders_cmd([]) == 0)
        check("kit: folders on an unknown report -> code 1",
              agent_main._folders_cmd(["999"]) == 1)
        # The source key: the folder NAME, not the path. The rule lives here because
        # `D:\` and UNC must be parsed by whoever knows their OS. If it
        # quietly drifts, the same folder would create a SECOND source on the platform
        # (the lookup is by key_name), and the history of a report run both in the
        # browser and locally would split — it would be noticed in a month.
        from agent.server import _root_name
        for raw, want in [
            ("D:\\Ventes_été", "Ventes_été"),
            ("D:\\Département\\Ventes_été\\", "Ventes_été"),          # trailing slash
            ("\\\\server\\share\\département\\2026", "2026"),
            ("/home/analyst/Downloads/SL", "SL"),
        ]:
            check(f"root name {raw!r} -> {want!r}", _root_name(raw) == want,
                  repr(_root_name(raw)))
        # A bare UNC share stays whole: `katalog` apart from the server is not
        # recognised either by a person or as a key.
        check("a bare UNC share is not cut",
              _root_name("\\\\share\\katalog") == "\\\\share\\katalog",
              repr(_root_name("\\\\share\\katalog")))
    finally:
        srv.shutdown()
        srv.server_close()
        if saved_ini is None:
            os.environ.pop("OP_FOLDERS_INI", None)
        else:
            os.environ["OP_FOLDERS_INI"] = saved_ini


if __name__ == "__main__":
    sys.exit(main())
