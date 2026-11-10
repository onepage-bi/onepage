"""Smoke test of the BUILT KIT: the person's path from key to report, offline.

Why a separate file when there is `smoke_mirror_sync.py`. All the other tests
work with the `agent/` PACKAGE, but what goes to the person is not the package but the single-file
`op_agent.py` plus a `mirror/` directory with resources. Defects were found between them
that the package does not have and cannot have:

  * the kit has ITS OWN entry point — `_MAIN` inside `deploy/build_agent_single.py`, and
    it is richer than the package one (`set-cred`, autostart, `check-kit`). They cannot
    be merged into one, so they diverge: the fix "the mirror does not depend on the tray icon"
    first landed only in `agent/__main__.py`, and the kit kept arriving with
    a dead schedule. In the package everything was green;
  * resources (`strings.json`, `web/*`) are not carried over by the module merge — `--install-resources`
    puts them alongside, and `assets.res_path` looks for them under a different root;
  * `r2host.py` is the same case, but worse: it is CODE that is launched as a separate
    process, so by construction it does not get into the merge. The kit was built
    without it silently, `--check-kit` was silent, and the very first generator run over
    local folders would fail at the analyst's with `gen_no_host`.

So here the kit is BUILT anew, launched as a SUBPROCESS from its own
directory, and the path is walked over HTTP: key -> manifest -> disk -> serving. Then the
fake platform is SHUT DOWN, and the report must still open as before — this is the
one case for which the program is written.

The platform is a stub on `http.server` (127.0.0.1, port 0). The test does not go out to the real network,
there is no real report data: the document and slot bodies are
deterministic noise. The device key is made up; one of the checks is that
it got neither into the log nor into the state files.

Run:     python -m agent.tests.smoke_mirror_kit   (from the OnePage repo root)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILDER = REPO / "deploy" / "build_agent_single.py"
DID = "rep-ok"
RLS_ID = "rep-rls"
DOC = b"<!doctype html><html><body><script>/*report*/</script></body></html>"
SLOT = bytes(range(256)) * 160                     # 40960 B, deterministic
KEY = "opdt_" + "b" * 43                           # a made-up device key

_ok = _fail = 0


def chk(name: str, cond: bool, note: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print("PASS", name)
    else:
        _fail += 1
        print("FAIL", name, note)


# ── fake platform ───────────────────────────────────────────────────────────
class _Platform(BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *a):                     # silence in the test output
        pass

    def _send(self, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                              # noqa: N802
        _Platform.hits.append(self.path)
        if self.path == "/api/sync/manifest":
            # The shape is like app/api/sync.py: the key `items`, not `reports`.
            # A report with RLS is served as NOT mirrorable: it must not end up on
            # disk by a single byte (ADR-18).
            self._send(json.dumps({"rights_ttl_s": 86400, "items": [
                {"id": DID, "title": "Warehouse availability", "type": "interactive",
                 "version": 1, "updated_at": "2026-10-11T09:00:00",
                 "fingerprint": "fp1", "size": len(DOC), "rls_enabled": False,
                 "can_update_data": False, "mirrorable": True, "skip_reason": "",
                 "slots": [{"slot": 0, "name": "data1.bin",
                            "size": len(SLOT), "v": "v1"}]},
                {"id": RLS_ID, "title": "Report with row-level access",
                 "type": "interactive", "version": 3,
                 "updated_at": "2026-10-11T09:00:00", "size": 10,
                 "rls_enabled": True, "can_update_data": False,
                 "mirrorable": False, "skip_reason": "rls"},
            ]}).encode(), "application/json")
        elif self.path.startswith(f"/render/{DID}/files/0"):
            self._send(SLOT, "application/octet-stream", {"ETag": '"s1"'})
        elif self.path.startswith(f"/render/{DID}/"):
            self._send(DOC, "text/html; charset=utf-8", {"ETag": '"d1"'})
        else:
            self.send_error(404)


def _build_kit(dst: Path) -> None:
    """Build the single-file agent and put the mirror resources alongside."""
    subprocess.run([sys.executable, str(BUILDER)], cwd=REPO, check=True,
                   capture_output=True, text=True)
    dst.mkdir(parents=True, exist_ok=True)
    built = REPO / "docs" / "agent_m2_kit" / "op_agent.py"
    dst.joinpath("op_agent.py").write_bytes(built.read_bytes())
    subprocess.run([sys.executable, str(BUILDER), "--install-resources", str(dst)],
                   cwd=REPO, check=True, capture_output=True, text=True)


def _write_ini(kit: Path) -> None:
    """agent.ini with GP filled in: without it the agent does not start at all.

    The mirror does not need GreenPlum, but the entry point demands a configured connection
    before everything else — recorded as an open delivery question in the RFC. The test does not
    bypass this but honestly fills it in, to test the mirror, not the config.
    """
    kit.joinpath("agent.ini").write_text(
        "[security]\nallowed_origins = https://onepage.example.com\n\n"
        "[connection:analytics_dw]\nhost = gp.invalid\nport = 6432\n"
        "dbname = analytics_dw\nuser = probe_user\n", encoding="utf-8")


def _port_from_log(log_path: Path) -> int | None:
    m = re.search(r"http://127\.0\.0\.1:(\d+)", log_path.read_text(errors="replace"))
    return int(m.group(1)) if m else None


def _build_check(kit: Path):
    """Run the builder's `--check-kit` over a directory. Return (rc, output)."""
    r = subprocess.run([sys.executable, str(BUILDER), "--check-kit", str(kit)],
                       cwd=REPO, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def _check_r2host(tmp: Path, kit: Path) -> None:
    """The R2 contract host travels in the kit as a separate file — and this is checked.

    Four claims, and each closes its own way of losing the file: it is not in
    the kit; it diverged from the repository; the build did not notice; the agent
    cannot find it next to `op_agent.py`. The reverse run (remove the file and
    make sure it breaks) is mandatory here: a check that has not been seen
    failing is not a check.
    """
    src = REPO / "agent" / "r2host.py"
    got = kit / "r2host.py"
    chk("r2host.py is in the kit next to op_agent.py", got.is_file())
    chk("r2host.py byte for byte as in the repository",
        got.is_file() and got.read_bytes() == src.read_bytes())
    # The merge does not carry it and cannot: names from r2host.py in op_agent.py
    # would mean someone added the file to MODULES, and then the kit would end up with
    # two entry points and two argv parsings.
    glued = kit.joinpath("op_agent.py").read_text(encoding="utf-8")
    chk("r2host.py is NOT merged into op_agent.py (it is launched as a process)",
        "class R2Error" not in glued and "def _arm_cache(" not in glued)

    rc, out = _build_check(kit)
    chk("--check-kit is satisfied with the whole kit", rc == 0, out[-400:])

    # ── reverse run 1: the file is missing ─────────────────────────────────
    gone = tmp / "kit-no-host"
    shutil.rmtree(gone, ignore_errors=True)
    shutil.copytree(kit, gone)
    gone.joinpath("r2host.py").unlink()
    rc, out = _build_check(gone)
    chk("--check-kit breaks without r2host.py", rc != 0, out[-400:])
    chk("and names the file and the consequence", "r2host.py" in out and "gen_no_host" in out,
        out[-400:])
    rc_self = subprocess.run([sys.executable, "op_agent.py", "check-kit"], cwd=gone,
                             capture_output=True, text=True)
    chk("the agent itself without r2host.py also refuses",
        rc_self.returncode != 0 and "r2host.py" in rc_self.stdout + rc_self.stderr,
        (rc_self.stdout + rc_self.stderr)[-300:])

    # ── reverse run 2: the file diverged from the repository ───────────────
    drift = tmp / "kit-drift"
    shutil.rmtree(drift, ignore_errors=True)
    shutil.copytree(kit, drift)
    with drift.joinpath("r2host.py").open("ab") as f:
        f.write("\n# an edit in the kit bypassing the repository\n".encode("utf-8"))
    rc, out = _build_check(drift)
    chk("--check-kit breaks on a diverged r2host.py", rc != 0, out[-400:])
    chk("and says the bytes did not match",
        "byte-for-byte" in out, out[-400:])

    # ── the kit's agent can find the host ──────────────────────────────────
    r = subprocess.run(
        [sys.executable, "-c",
         "import op_agent; p = op_agent._r2_host_path(); print(p, p.is_file())"],
        cwd=kit, capture_output=True, text=True)
    chk("op_agent finds r2host.py next to itself",
        r.returncode == 0 and str(kit / "r2host.py") in r.stdout and "True" in r.stdout,
        (r.stdout + r.stderr)[-300:])

    # The second candidate (the directory of the launched script): op_agent is called via a LINK
    # from another directory, and the host lies next to the link. `resolve()` leads the
    # first candidate back into the kit, where the file is not — the second must find it.
    link_dir = tmp / "kit-link"
    shutil.rmtree(link_dir, ignore_errors=True)
    link_dir.mkdir()
    try:
        link_dir.joinpath("op_agent.py").symlink_to(gone / "op_agent.py")
        shutil.copyfile(src, link_dir / "r2host.py")
        r = subprocess.run(
            [sys.executable, "-c",
             "import runpy, sys, pathlib;"
             "m = runpy.run_path('op_agent.py', run_name='_probe');"
             "p = m['_r2_host_path']();"
             "print(p, p.is_file())"],
            cwd=link_dir, capture_output=True, text=True)
        chk("the host is also found next to the launched script (link/copy)",
            r.returncode == 0 and str(link_dir / "r2host.py") in r.stdout
            and "True" in r.stdout, (r.stdout + r.stderr)[-300:])
    except OSError as e:                       # pragma: no cover
        chk("the host is also found next to the launched script (link/copy)",
            False, f"the symbolic link was not created: {e}")


def _check_lineage_copy(tmp: Path, kit: Path) -> None:
    """The lineage probe travels in the kit — and must be the ORIGINAL.

    `_lineage.py` is the only kit file whose source is not in
    `agent/` but in `static/py/`: the browser engine loads exactly the same file.
    It has no copy of its own in the repository on purpose. A copy that diverged from
    the original is the classic way to get TWO different lineages for ONE report:
    different `check_id`s of the quality checks, a different schema shape, and
    nobody notices the divergence because both runs "work".

    The loss of this file is the quietest failure possible: the run passes,
    the report is published, only the provenance stays empty. So there
    are two reverse runs: the file is missing and the file diverged.
    """
    src = REPO / "static" / "py" / "_lineage.py"
    got = kit / "_lineage.py"
    chk("_lineage.py is in the kit next to op_agent.py", got.is_file())
    chk("_lineage.py byte for byte as static/py/_lineage.py (the ORIGINAL)",
        got.is_file() and got.read_bytes() == src.read_bytes())
    chk("the repository has no second copy of agent/_lineage.py",
        not (REPO / "agent" / "_lineage.py").exists())

    # ── reverse run 1: the file is missing ─────────────────────────────────
    gone = tmp / "kit-no-lineage"
    shutil.rmtree(gone, ignore_errors=True)
    shutil.copytree(kit, gone)
    gone.joinpath("_lineage.py").unlink()
    rc, out = _build_check(gone)
    chk("--check-kit breaks without _lineage.py", rc != 0, out[-400:])
    chk("and names the file and the consequence",
        "_lineage.py" in out and "provenance" in out, out[-500:])

    # ── reverse run 2: the copy in the kit diverged from the original ──────
    drift = tmp / "kit-lineage-drift"
    shutil.rmtree(drift, ignore_errors=True)
    shutil.copytree(kit, drift)
    with drift.joinpath("_lineage.py").open("ab") as f:
        f.write("\n# an edit of the copy bypassing static/py/_lineage.py\n".encode("utf-8"))
    rc, out = _build_check(drift)
    chk("--check-kit breaks on a diverged copy of _lineage.py", rc != 0,
        out[-400:])
    chk("and names the original it compared against",
        "static/py/_lineage.py" in out, out[-500:])

    # ── the kit's host finds the probe next to itself ──────────────────────
    r = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util, sys, pathlib;"
         "spec = importlib.util.spec_from_file_location('h', 'r2host.py');"
         "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
         "print([str(c) for c in m._lineage_candidates()][0])"],
        cwd=kit, capture_output=True, text=True)
    chk("r2host.py looks for the probe first of all next to itself",
        r.returncode == 0 and str(kit / "_lineage.py") in r.stdout,
        (r.stdout + r.stderr)[-300:])


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Platform)
    _Platform.hits = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    platform_url = f"http://127.0.0.1:{srv.server_address[1]}"

    tmp = Path(tempfile.mkdtemp(prefix="op-kit-"))
    kit, state = tmp / "kit", tmp / "state"
    state.mkdir()
    _build_kit(kit)
    _write_ini(kit)

    # A kit without the mirror/ directory must refuse, not open empty.
    bare = tmp / "bare"
    bare.mkdir()
    bare.joinpath("op_agent.py").write_bytes(kit.joinpath("op_agent.py").read_bytes())
    rc_bare = subprocess.run([sys.executable, "op_agent.py", "check-kit"], cwd=bare,
                             capture_output=True, text=True).returncode
    rc_full = subprocess.run([sys.executable, "op_agent.py", "check-kit"], cwd=kit,
                             capture_output=True, text=True).returncode
    chk("an incomplete kit is not recognised as whole", rc_bare != 0, f"rc={rc_bare}")
    chk("the built kit is recognised as whole", rc_full == 0, f"rc={rc_full}")

    _check_r2host(tmp, kit)
    _check_lineage_copy(tmp, kit)

    env = dict(os.environ, LOCALAPPDATA=str(state), OP_AGENT_STATE=str(state),
               OP_MIRROR_SERVER=platform_url, OP_AGENT_DEV_CRED_STORE="1",
               PYTHONUNBUFFERED="1")
    log_path = tmp / "run.log"
    with open(log_path, "w") as log:
        proc = subprocess.Popen([sys.executable, "op_agent.py", "--no-tray"],
                                cwd=kit, env=env, stdout=log,
                                stderr=subprocess.STDOUT, text=True)
    try:
        port = None
        t0 = time.time()
        while time.time() - t0 < 40 and port is None and proc.poll() is None:
            port = _port_from_log(log_path)
            time.sleep(0.3)
        chk("the kit's agent came up", port is not None,
            log_path.read_text(errors="replace")[-400:])
        if port is None:
            return 1
        base = f"http://127.0.0.1:{port}"

        def get(path: str, host: str | None = None):
            req = urllib.request.Request(base + path)
            if host:
                req.add_header("Host", host)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read(), dict(r.headers)
            except urllib.error.HTTPError as e:
                return e.code, e.read(), dict(e.headers)

        def post(path: str, obj: dict, origin: str | None = None):
            req = urllib.request.Request(base + path,
                                         data=json.dumps(obj).encode(), method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Origin", origin or base)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        st, body, _ = get("/library/")
        chk("the library is served from the kit's resources", st == 200 and b"<main" in body, str(st))
        st, reply = post("/library/key", {"key": KEY})
        chk("key accepted", st == 200, f"{st} {reply}")
        chk("the key did not come back in the reply", KEY not in json.dumps(reply))

        # The schedule must start BY ITSELF, without a tray icon.
        t0 = time.time()
        while (time.time() - t0 < 40
               and not any("files/0" in h for h in _Platform.hits)):
            time.sleep(0.4)
        chk("the platform received the manifest",
            any("manifest" in h for h in _Platform.hits), repr(_Platform.hits))
        chk("document and slot downloaded",
            any(f"/render/{DID}/" in h and "files" not in h for h in _Platform.hits)
            and any("files/0" in h for h in _Platform.hits), repr(_Platform.hits))
        chk("the report with RLS was not requested from the platform",
            not any(RLS_ID in h for h in _Platform.hits), repr(_Platform.hits))

        time.sleep(1.5)
        st, body, _ = get("/library/data.json")
        data = json.loads(body) if st == 200 else {}
        items = data.get("items") or []
        chk("the schedule works without a tray icon",
            bool((data.get("sync") or {}).get("enabled")), repr(data.get("sync")))
        chk("the library knows the report",
            any(i.get("dashboard_id") == DID for i in items), repr(data)[:300])
        chk("no null in the time fields (otherwise the page prints 31.12.1969)",
            not re.search(r'"(fetched_at|last_sync_at|local_at)"\s*:\s*null',
                          body.decode()))

        # ── shutting the platform down ─────────────────────────────────────
        srv.shutdown()
        srv.server_close()
        time.sleep(0.5)
        st, doc, head = get(f"/mirror/{DID}/")
        chk("OFFLINE: the document opens byte for byte", st == 200 and doc == DOC,
            f"{st}, {len(doc)} B")
        chk("OFFLINE: the document with type text/html",
            head.get("Content-Type", "").startswith("text/html"),
            head.get("Content-Type", ""))
        st, slot, head = get(f"/mirror/{DID}/files/0")
        chk("OFFLINE: the slot is served byte for byte", st == 200 and slot == SLOT,
            f"{st}, {len(slot)} B")
        chk("OFFLINE: the slot length is declared correctly",
            head.get("Content-Length") == str(len(SLOT)), head.get("Content-Length", ""))
        st, _, head = get("/library/common.js")
        chk("OFFLINE: the script with type text/javascript",
            st == 200 and head.get("Content-Type", "").startswith("text/javascript"),
            head.get("Content-Type", ""))
        st, _, _ = get(f"/mirror/{RLS_ID}/")
        chk("OFFLINE: the report with RLS is not served", st != 200, str(st))

        st, _, _ = get("/library/", host="evil.example")
        chk("a foreign Host is rejected", st == 421, str(st))
        code, _ = post("/library/sync", {}, origin="https://evil.example")
        chk("a foreign Origin on POST is rejected", code == 403, str(code))
    finally:
        proc.terminate()
        proc.wait(timeout=15)

    text = log_path.read_text(errors="replace")
    chk("the key is not in the program log", KEY not in text and "bbbb" not in text)
    leaked = [p.name for p in state.rglob("*") if p.is_file()
              and (KEY.encode() in p.read_bytes() or b"bbbbbbbb" in p.read_bytes())]
    chk("the key is not in plaintext in the state files", not leaked, repr(leaked))
    rls_leak = [str(p) for p in state.rglob("*") if RLS_ID in p.name]
    chk("the report-with-RLS directory is not in the mirror", not rls_leak, repr(rls_leak))

    print(f"\n{_ok}/{_ok + _fail}")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
