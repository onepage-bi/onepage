"""Offline run and the catch-up queue: `agent/mirror/localrun.py` and its seams.

What is checked here and why exactly this.

The GAP is checked, not the halves. `smoke_r2folders` already pins the folder
run, `smoke_mirror_sync` the synchronization, and both were green at exactly the
moment when a run WITHOUT the server was impossible in principle: the bundle was
cached nowhere, the `local/L<k>` space existed in `layout.py` and was used by not
a single line of code, and `libui._mirrored_item` answered `not_downloaded` for
any report. Hence here: a real bundle under the R2 contract, real folders from
`folders.ini`, a real record in `state.json`, a real `AgentServer` on loopback
— and exactly two substitutions, both named (the platform network in the
bundle-cache scenario, and the system time nowhere).

Separately pinned is what would break SILENTLY:
  * the bridge marker is checked against `app/services/detector.py` — if it is
    renamed there, the local report opens empty rather than with an error;
  * the right is limited in time: an expired term must STOP the run and NOT
    interfere with reading (RFC §5);
  * RLS does not pass by any path (ADR-18);
  * `output_path` returns nothing that is not in the run inventory (`..` over
    loopback means reading other people's files);
  * a run does not leave the catch-up queue by itself: only `mark_published`.

Run:  python -m agent.tests.smoke_localrun   (from the OnePage repo root)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from http.client import HTTPConnection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("OP_AGENT_DEV_CRED_STORE", "1")
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="smoke_localrun_"))
os.environ["LOCALAPPDATA"] = str(_TMP_ROOT / "state")

from agent.config import Config                                    # noqa: E402
from agent.mirror import layout, libui, localrun, store, sync      # noqa: E402
from agent.pairing import Pairing                                  # noqa: E402
from agent.server import AgentServer                               # noqa: E402

_PASS = 0
_FAIL = 0


def check(label: str, ok: bool, got: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("PASS " + label)
    else:
        _FAIL += 1
        print("FAIL " + label + (f"  -> {got}" if got else ""))


# --------------------------------------------------------------------------- #
# Dummy bundle under the R2 contract                                          #
# --------------------------------------------------------------------------- #

GEN = '''
import os, json
def begin(out_dir, cache_dir, schema_version, progress):
    return S(out_dir)
class S:
    def __init__(self, out):
        self.out, self.rows = out, []
    def add_file(self, name, mtime_ms, size, path):
        self.rows.append((name, size))
        return "parsed"
    def finish(self):
        names = [r[0] for r in self.rows]
        open(os.path.join(self.out, "report.html"), "w", encoding="utf-8").write(
            "<html><head><title>t</title></head><body>offline</body></html>")
        open(os.path.join(self.out, "data0.bin"), "w", encoding="utf-8").write(
            json.dumps({"files": names}, ensure_ascii=False))
        return {"html": "report.html", "slots": {"0": "data0.bin"}, "write_back": {}}
'''

MANIFEST = {"name": "fakelocal", "version": "1.0",
            "entrypoint": {"module": "fakelocal.main"},
            "outputs": {"html": True, "slots": ["data"]},
            "inputs": [{"match": "SL_*.xlsx", "role": "data", "required": True}],
            "engine": "browser"}


def make_bundle(manifest: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest or MANIFEST, ensure_ascii=False))
        z.writestr("fakelocal/__init__.py", "")
        z.writestr("fakelocal/main.py", GEN)
    return buf.getvalue()


BUNDLE = make_bundle()
SHA = hashlib.sha256(BUNDLE).hexdigest()

# A document "as the platform serves it": the bridge is inserted with exactly the block
# that `detector.inject_bootstrap` writes, with a foreign slot inventory inside.
SERVER_DOC = (
    '<html><head>\n<script>\n' + localrun.BOOTSTRAP_MARKER + '\n(function(){\n'
    '  window.__OP_SLOTS__ = [{"slot": 0, "name": "old_\u00e9t\u00e9.bin"}];\n'
    '})();\n</script>\n</head><body>server</body></html>')


def seed(dashboard_id: str, *, can_update=True, expired=False, rls=False,
         bundle=True, generator=True, doc=True, version=3,
         seen_version=3) -> dict:
    """Mirror of one report in the form the synchronization leaves it."""
    state = store.fresh_state(dashboard_id, title="Report " + dashboard_id,
                              dash_type="interactive")
    state["server_version"] = version
    state["fingerprint"] = "ff"
    state["server_seen"] = {"version": seen_version, "fingerprint": "ff",
                            "at": "2026-10-19T09:00:00"}
    state["last_sync_at"] = "2026-10-19T09:00:00"
    state["rights"] = {
        "can_update_data": can_update, "rls_enabled": rls,
        "fetched_at": "2026-10-19T09:00:00",
        # The term is deliberately in the past/future — we do not substitute time,
        # the comparison is a string one (`layout.rights_expired`).
        "expires_at": "2000-01-01T00:00:00" if expired else "2999-01-01T00:00:00",
        "ttl_s": 86400,
    }
    if generator:
        state["generator"] = {"id": 7, "name": "fakelocal", "version": 4,
                              "checksum_sha256": SHA, "file": SHA + ".zip"}
    if doc:
        vdir = store.ensure_version_dir(dashboard_id, version)
        (vdir / layout.DOCUMENT_NAME).write_text(SERVER_DOC, encoding="utf-8")
    if bundle:
        layout.bundle_path(dashboard_id, SHA, create=True).write_bytes(BUNDLE)
    store.save_state(dashboard_id, state)
    return state


def make_folders(dashboard_id: str, tag: str) -> Path:
    data = _TMP_ROOT / ("data-" + tag)
    data.mkdir(parents=True, exist_ok=True)
    (data / "SL_01.xlsx").write_bytes(b"a" * 100)
    (data / "SL_02.xlsx").write_bytes(b"b" * 250)
    ini = _TMP_ROOT / f"folders-{tag}.ini"
    ini.write_text(f"[folders:{dashboard_id}]\ndir = {data}\ninclude = SL_*.xlsx\n",
                   encoding="utf-8")
    os.environ["OP_FOLDERS_INI"] = str(ini)
    return data


# --------------------------------------------------------------------------- #
# 1. Who is entitled                                                          #
# --------------------------------------------------------------------------- #

def test_precheck() -> None:
    check("report not in the mirror -> no_mirror",
          localrun.precheck("unknown_\u00e9t\u00e9")[0] == "no_mirror",
          localrun.precheck("unknown_\u00e9t\u00e9")[0])

    seed("rep-rls", rls=True)
    check("an RLS report is not updated locally (ADR-18)",
          localrun.precheck("rep-rls")[0] == "rls", localrun.precheck("rep-rls")[0])

    seed("rep-noright", can_update=False)
    check("without the right -> no_right",
          localrun.precheck("rep-noright")[0] == "no_right")

    seed("rep-expired", expired=True)
    kind, why = localrun.precheck("rep-expired")
    check("an expired right stops the run", kind == "rights_expired", kind)
    check("the refusal for the term names the date, not \u201cno rights\u201d",
          "2000-01-01" in why, why)
    # ...and does NOT interfere with reading: serving knows only one thing about rights — RLS.
    state = store.load_state("rep-expired")
    check("an expired right does not interfere with reading the copy",
          libui._document_on_disk("rep-expired", state))

    seed("rep-nobundle", bundle=False)
    check("no bundle in the cache -> no_bundle",
          localrun.precheck("rep-nobundle")[0] == "no_bundle")

    seed("rep-nogen", generator=False)
    check("the report has no generator -> no_generator",
          localrun.precheck("rep-nogen")[0] == "no_generator")

    seed("rep-ok")
    check("right present, bundle present -> ok", localrun.precheck("rep-ok")[0] == "ok",
          str(localrun.precheck("rep-ok")))


# --------------------------------------------------------------------------- #
# 2. Data feed bridge                                                        #
# --------------------------------------------------------------------------- #

def test_bridge() -> None:
    # The marker is a contract with the platform. If it is renamed there, the local report
    # opens EMPTY, and there is nowhere to learn about it except here: the agent ships
    # without `app/`, and the kit will not have this check.
    detector = Path(__file__).resolve().parents[2] / "app" / "services" / "detector.py"
    if detector.is_file():
        check("bridge marker matches app/services/detector.py",
              localrun.BOOTSTRAP_MARKER in detector.read_text(encoding="utf-8"))
    else:
        check("bridge marker matches app/services/detector.py", True,
              "detector.py is not next to it — check skipped")

    html = "<html><head></head><body>gen</body></html>"
    out, kind = localrun.with_bridge(html, SERVER_DOC, '[{"slot": 0}]')
    check("the bridge is taken from the mirror of the server version", kind == "borrowed", kind)
    check("the bridge is inserted right after <head>",
          out.index(localrun.BOOTSTRAP_MARKER) < out.index("<body>"))
    check("the slot inventory is replaced with its OWN",
          '[{"slot": 0}]' in out and "old_\u00e9t\u00e9.bin" not in out, out[:300])

    out2, kind2 = localrun.with_bridge(html, None, "[]")
    check("no server copy -> bridge=none and the document is not spoiled",
          kind2 == "none" and out2 == html, kind2)

    own = "<html><head></head><body>" + localrun.BOOTSTRAP_MARKER + "</body></html>"
    out3, kind3 = localrun.with_bridge(own, SERVER_DOC, "[]")
    check("the own bridge is not doubled", kind3 == "own" and out3 == own, kind3)


# --------------------------------------------------------------------------- #
# 3. A real run without the server                                            #
# --------------------------------------------------------------------------- #

def test_run() -> dict:
    dash = "rep-run"
    seed(dash)
    make_folders(dash, "run")
    info = {"roots": [{"dir": 0, "root_path": "/data/Exports_\u00e9t\u00e9",
                       "root_id": "abc123", "name": "Exports_\u00e9t\u00e9"}]}
    rec = localrun.run_local(dash, folders_info=info)

    check("the run returned a record numbered L1", rec.get("k") == 1, str(rec)[:200])
    vdir = layout.local_run_dir(dash, 1)
    check("the document lies in local/L1", (vdir / layout.DOCUMENT_NAME).is_file())
    check("the bridge in the local document is taken from the mirror",
          rec.get("bridge") == "borrowed", str(rec.get("bridge")))
    doc = (vdir / layout.DOCUMENT_NAME).read_text(encoding="utf-8")
    check("the local document is the GENERATOR output, not a copy of the server one",
          "offline" in doc and "server" not in doc)
    check("the output slot is copied under its own name",
          (vdir / "data0.bin").is_file())
    payload = json.loads((vdir / "data0.bin").read_text(encoding="utf-8"))
    check("the slot is built from the folder files, not from nothing",
          payload.get("files") == ["SL_01.xlsx", "SL_02.xlsx"], str(payload))

    manifest_in = json.loads((vdir / localrun.INPUT_MANIFEST_NAME).read_text("utf-8"))
    check("input_manifest lies next to it for catch-up",
          manifest_in.get("source") == "folders" and manifest_in.get("count") == 2,
          str(manifest_in)[:200])
    check("input_manifest carries the file PATHS",
          all(f.get("path") for f in manifest_in["files"]), str(manifest_in["files"])[:200])
    check("roots are taken from the /folders response, not invented",
          manifest_in["roots"] == [{"dir": 0, "root_path": "/data/Exports_\u00e9t\u00e9",
                                    "root_id": "abc123"}]
          and manifest_in["dirs"] == ["Exports_\u00e9t\u00e9"], str(manifest_in["roots"]))
    check("the inputs mask reached the input manifest",
          manifest_in["files"][0].get("matched_input") == "SL_*.xlsx",
          str(manifest_in["files"][0]))
    observed = json.loads((vdir / localrun.OBSERVE_NAME).read_text("utf-8"))
    check("observe frames are saved alongside (even if empty)",
          isinstance(observed, list), str(observed)[:120])

    state = store.load_state(dash)
    saved = layout.local_run(state, 1)
    check("the run is recorded in state.json and NOT published",
          saved is not None and saved.get("published") is False, str(saved)[:200])
    check("the run remembers which server version it was counted from",
          saved.get("based_on_server_version") == 3, str(saved.get("based_on_server_version")))
    check("the run slot inventory is its own, by numbers from the R2 contract",
          [s["slot"] for s in saved["slots"]] == [0]
          and saved["slots"][0]["name"] == "data0.bin", str(saved.get("slots")))
    check("the server slot inventory is untouched", state.get("slots") == [])

    # The second run must get ITS OWN number, not overwrite the first.
    rec2 = localrun.run_local(dash, folders_info=info)
    check("second run -> L2", rec2.get("k") == 2, str(rec2.get("k")))
    check("the first run directory is intact",
          (layout.local_run_dir(dash, 1) / layout.DOCUMENT_NAME).is_file())
    check("sweeping server versions does not touch local runs",
          store.prune_versions(dash, 3) >= 0
          and layout.local_run_dir(dash, 2).is_dir())
    return {"dash": dash}


def test_run_refusals() -> None:
    dash = "rep-empty"
    seed(dash)
    ini = _TMP_ROOT / "folders-empty.ini"
    ini.write_text(f"[folders:{dash}]\ndir = {_TMP_ROOT / 'no-such-\u00e9t\u00e9'}\n",
                   encoding="utf-8")
    os.environ["OP_FOLDERS_INI"] = str(ini)
    try:
        localrun.run_local(dash)
        check("an empty folder is a loud refusal, not an empty report", False,
              "the run finished successfully")
    except localrun.LocalRunRefused as exc:
        check("an empty folder is a loud refusal, not an empty report",
              exc.kind == "folders_empty", exc.kind)
    check("after the refusal no local version appeared",
          not layout.local_run_dir(dash, 1).exists())

    # A GreenPlum bundle: there is nothing to compute it with offline, and the data
    # source must not be swapped silently.
    dash2 = "rep-gp"
    gp_manifest = dict(MANIFEST, sources={"type": "greenplum",
                                          "connection": "analytics_dw",
                                          "queries": []})
    gp_bundle = make_bundle(gp_manifest)
    gp_sha = hashlib.sha256(gp_bundle).hexdigest()
    state = seed(dash2)
    state["generator"] = dict(state["generator"], checksum_sha256=gp_sha)
    store.save_state(dash2, state)
    layout.bundle_path(dash2, gp_sha, create=True).write_bytes(gp_bundle)
    make_folders(dash2, "gp")
    try:
        localrun.run_local(dash2)
        check("a warehouse bundle does not run offline", False, "success")
    except localrun.LocalRunRefused as exc:
        check("a warehouse bundle does not run offline",
              exc.kind == "gp_bundle", exc.kind)


# --------------------------------------------------------------------------- #
# 4. Catch-up queue                                                          #
# --------------------------------------------------------------------------- #

def test_queue() -> None:
    dash = "rep-run"
    pending = localrun.list_unpublished(dash, with_input=True)
    check("both runs stand in the catch-up queue", len(pending) == 2, str(len(pending)))
    first = pending[0]
    check("the SAVED input_manifest travels in the queue",
          (first.get("input_manifest") or {}).get("source") == "folders",
          str(first.get("input_manifest"))[:120])
    check("the slot->name map for publication travels in the queue",
          first["outputs"]["slots"] == {"0": "data0.bin"}, str(first["outputs"]))
    # Lineage travels TOGETHER with the input: there is nothing to recompute it with on
    # catch-up — the files in the folders may have moved away by then, and the page must
    # tell the platform what exactly was read.
    check("the run observe frames travel in the queue",
          isinstance(first.get("observe"), list), str(type(first.get("observe"))))
    check("without with_input observations are not sent (the library queue does not read them)",
          "observe" not in localrun.list_unpublished(dash)[0])

    vdir = layout.local_run_dir(dash, 1)
    with layout.atomic_write(vdir / localrun.OBSERVE_NAME, "w") as fh:
        fh.write(json.dumps([{"t": "observe", "files": [{"name": "SL_01.xlsx"}]},
                             {"t": "progress", "stage": "files"}]))
    frames = localrun.read_observe(dash, 1)
    check("read_observe returns only observe frames",
          [f["t"] for f in frames] == ["observe"], str(frames))
    check("an observation file that is absent reads as \u201cnothing to say\u201d",
          localrun.read_observe(dash, 99) == [])
    check("the version note names BOTH dates",
          first["note"].startswith("built offline ") and ", published " in first["note"],
          first["note"])

    path = localrun.output_path(dash, 1, "data0.bin")
    check("the run output is served by a name from the inventory", path is not None and path.is_file())
    check("a name not from the inventory is not served",
          localrun.output_path(dash, 1, "../../state.json") is None)
    check("a foreign name is not served", localrun.output_path(dash, 1, "secret.bin") is None)
    check("the run document is served by its own name",
          localrun.output_path(dash, 1, layout.DOCUMENT_NAME) is not None)

    check("the mark is set only by whoever learned of the publication",
          localrun.mark_published(dash, 1) is True)
    left = localrun.list_unpublished(dash)
    check("the published run left the queue, the unpublished one remained",
          [r["k"] for r in left] == [2], str([r["k"] for r in left]))
    check("the published run directory is NOT deleted",
          (layout.local_run_dir(dash, 1) / layout.DOCUMENT_NAME).is_file())
    check("a mark on a nonexistent run -> False",
          localrun.mark_published(dash, 99) is False)
    check("the queue across the whole mirror sees the same run",
          any(r["dashboard_id"] == dash and r["k"] == 2
              for r in localrun.list_unpublished()))


# --------------------------------------------------------------------------- #
# 5. Library                                                                  #
# --------------------------------------------------------------------------- #

def test_library() -> None:
    server = type("S", (), {"config": None, "controller": None, "cred_store": None})()
    data = libui.data(server)
    by_id = {i["dashboard_id"]: i for i in data["items"]}

    it = by_id["rep-run"]
    check("a report with an unpublished run is labelled local_only",
          it.get("state") == "local_only", str(it.get("state")))
    check("the library knows the date of the local version", it.get("local_at"), str(it))
    check("data.json carries the run OWN slot inventory",
          it["local_runs"][0]["slots"][0]["name"] == "data0.bin",
          str(it.get("local_runs"))[:200])
    check("the \u201cUpdate from folders\u201d button is allowed to be drawn",
          it["update"]["can"] is True, str(it["update"]))

    check("a fresh copy is labelled fresh",
          by_id["rep-ok"].get("state") == "fresh", str(by_id["rep-ok"].get("state")))

    # In the last pass the manifest named version 5, while version 3 lies on disk —
    # exactly the case `outdated` was introduced for.
    seed("rep-old", seen_version=5)
    data = libui.data(server)
    by_id = {i["dashboard_id"]: i for i in data["items"]}
    check("a copy that was not finished downloading is labelled outdated",
          by_id["rep-old"].get("state") == "outdated",
          str(by_id["rep-old"].get("state")))

    check("an RLS report has no update button at all",
          "update" not in by_id.get("rep-rls", {}), str(by_id.get("rep-rls")))
    check("expired right: no button, the reason is named by a code",
          by_id["rep-expired"]["update"] == {"can": False, "reason": "rights_expired",
                                             "why": by_id["rep-expired"]["update"]["why"]},
          str(by_id["rep-expired"]["update"]))
    check("run progress arrives as a separate block",
          isinstance(data.get("local_run"), dict), str(data.get("local_run"))[:120])

    # The viewer header ALWAYS draws the button (unlike the list) and must say why on a
    # ban. It has nothing to say if in `data.json` the ban has neither a code nor words,
    # — hence the pin is not on one line but on all bans at once: each has something
    # to show.
    mute = [i for i in data["items"]
            if isinstance(i.get("update"), dict) and not i["update"]["can"]
            and not (i["update"].get("reason") and i["update"].get("why"))]
    check("every update ban has a code AND words", not mute, str(mute)[:200])
    check("\u201cno generator\u201d is an honest state, not an empty button",
          by_id["rep-nogen"]["update"]["reason"] == "no_generator",
          str(by_id.get("rep-nogen", {}).get("update")))


# --------------------------------------------------------------------------- #
# 6. Bundle cache in the synchronization                                     #
# --------------------------------------------------------------------------- #

class _FakeResp:
    """The platform response. The only network substitution in this file."""

    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))}
        self.code = 200

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def close(self) -> None:
        pass


def test_sync_caches_bundle() -> None:
    dash = "rep-sync"
    state = store.fresh_state(dash, title="S", dash_type="interactive")
    syncer = sync.Syncer(base_url="http://127.0.0.1:1", token="k" * 10)
    asked: list[str] = []

    def fake_open(url, accept_encoding, etag=None):
        asked.append(url)
        return _FakeResp(BUNDLE)

    syncer._open = fake_open                     # noqa: SLF001 — network substitution
    gen = {"id": 7, "name": "fakelocal", "version": 4, "checksum_sha256": SHA}
    syncer._cache_bundle(dash, gen, state)       # noqa: SLF001
    check("the bundle was requested at its own version address",
          asked and asked[0].endswith("/generator/bundle?v=4"), str(asked))
    check("the bundle lies in the cache under its own sum name",
          layout.bundle_path(dash, SHA).is_file())
    check("the state remembered the generator",
          (state.get("generator") or {}).get("checksum_sha256") == SHA,
          str(state.get("generator")))

    syncer._cache_bundle(dash, gen, state)       # noqa: SLF001
    check("the second pass does not download what is already downloaded", len(asked) == 1, str(asked))

    # Substituted bytes: the file name is the sum, and a cache in which something
    # else lies under it lies to everyone who looks into it.
    other = hashlib.sha256(b"nope").hexdigest()
    syncer._open = lambda url, ae, etag=None: _FakeResp(b"nope-but-wrong")  # noqa: SLF001
    try:
        syncer._cache_bundle(dash, dict(gen, checksum_sha256=other), state)  # noqa: SLF001
        check("a bundle with a foreign sum is rejected", False, "accepted")
    except sync.SyncError:
        check("a bundle with a foreign sum is rejected", True)
    check("the corrupted bundle did not stay on disk",
          not layout.bundle_path(dash, other).exists())

    # A new generator version displaces the previous one: there is nothing to roll a run back
    # with offline anyway, and the person's disk space is not ours.
    newer = make_bundle(dict(MANIFEST, version="2.0"))
    newer_sha = hashlib.sha256(newer).hexdigest()
    syncer._open = lambda url, ae, etag=None: _FakeResp(newer)   # noqa: SLF001
    syncer._cache_bundle(dash, dict(gen, version=5,              # noqa: SLF001
                                    checksum_sha256=newer_sha), state)
    check("the previous-version bundle is swept",
          layout.bundle_path(dash, newer_sha).is_file()
          and not layout.bundle_path(dash, SHA).is_file())

    state2 = store.fresh_state("rep-nogen2", title="B")
    syncer._cache_bundle("rep-nogen2", None, state2)             # noqa: SLF001
    check("a report without a generator does not start a cache", state2.get("generator") is None)


# --------------------------------------------------------------------------- #
# 7. Agent endpoints                                                          #
# --------------------------------------------------------------------------- #

def test_handles() -> None:
    cfg = Config(allowed_origins=("https://onepage.example",), connections={})
    pairing = Pairing(code_factory=lambda: "123456")
    srv = AgentServer(("127.0.0.1", 0), cfg, pairing)
    srv.state_root = _TMP_ROOT / "runs"
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.1)
    try:
        def req(method, path, body=None, token=None):
            c = HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {}
            if token:
                headers["Authorization"] = "Bearer " + token
            if body is not None:
                headers["Content-Type"] = "application/json"
            c.request(method, path, body=body, headers=headers)
            r = c.getresponse()
            return r.status, r.read()

        st, _ = req("GET", "/local-runs")
        check("the local runs list is closed without a pairing key", st == 401, str(st))

        code, _ttl = pairing.request_code()
        token = pairing.redeem(code)

        st, body = req("GET", "/local-runs?dashboard=rep-run", token=token)
        payload = json.loads(body)
        check("the endpoint lists unpublished runs",
              st == 200 and [r["k"] for r in payload["runs"]] == [2],
              f"{st} {body[:200]}")
        check("the run input travels in the response",
              (payload["runs"][0].get("input_manifest") or {}).get("source") == "folders")

        st, body = req("GET", "/local-runs/output?dashboard=rep-run&k=2&name=data0.bin",
                       token=token)
        check("the run output is served as bytes", st == 200 and b"SL_01.xlsx" in body,
              f"{st} {body[:120]}")
        st, _ = req("GET", "/local-runs/output?dashboard=rep-run&k=2&name=../state.json",
                    token=token)
        check("an output under a foreign name -> 404", st == 404, str(st))

        # Mirror serving: a local run must OPEN. This is exactly the gap the file was
        # written for — `local/L<k>` existed in the layout and was not connected to
        # anything.
        st, body = req("GET", "/mirror/rep-run/local/L2/")
        check("the local run document is served by the server",
              st == 200 and b"offline" in body, f"{st} {body[:120]}")
        check("the served document has the data feed bridge",
              localrun.BOOTSTRAP_MARKER.encode("utf-8") in body)
        st, body = req("GET", "/mirror/rep-run/local/L2/files/0")
        check("the local run slot is served by number",
              st == 200 and b"SL_02.xlsx" in body, f"{st} {body[:120]}")
        st, _ = req("GET", "/mirror/rep-run/local/L9/")
        check("a nonexistent run -> 404", st == 404, str(st))

        st, _ = req("POST", "/local-runs/published",
                    body=json.dumps({"dashboard": "rep-run", "k": 2}), token=token)
        check("the page marks the run as published", st == 200, str(st))
        check("after the mark the queue is empty",
              localrun.list_unpublished("rep-run") == [])
        st, _ = req("POST", "/local-runs/published",
                    body=json.dumps({"dashboard": "rep-run", "k": 77}), token=token)
        check("a mark on a nonexistent run -> 404", st == 404, str(st))
    finally:
        srv.shutdown()
        srv.server_close()


def main() -> int:
    try:
        for fn in (test_precheck, test_bridge, test_run, test_run_refusals,
                   test_queue, test_library, test_sync_caches_bundle,
                   test_handles):
            print(f"--- {fn.__name__}")
            try:
                fn()
            except Exception:                                  # noqa: BLE001
                check(f"{fn.__name__} did not raise an exception", False,
                      "\n" + traceback.format_exc())
    finally:
        os.environ.pop("LOCALAPPDATA", None)
        os.environ.pop("OP_FOLDERS_INI", None)
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    total = _PASS + _FAIL
    print(f"\n{_PASS}/{total}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
