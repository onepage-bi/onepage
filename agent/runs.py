"""Run lifecycle on the agent.

M1 - echo mode: the agent accepts a bundle + meta, verifies sha256, safely
unpacks it (zipguard) and, instead of a real generator/GP, runs an echo task
that sends a few NDJSON progress events and hands back a tiny slot with a
confirmation (the list of bundle files). This exercises EXACTLY the transport
(integrity check + NDJSON stream + byte delivery) that the owner measures on
a work laptop - without psycopg and without a real credential.

M2 replaces `_echo_task` with: gpsource (COPY->gzip) for `sources` + a
subprocess `python -m <module>` for the native generator, forwarding its
stdout progress into the same events. The event and output-delivery contract
does not change.

Events (one JSON per line in /runs/{id}/events):
    {"t":"stage","stage":"received|unpack|s3|resolve|generate|echo|done"}
    {"t":"progress","stage":"echo","done":i,"total":n,"msg":"..."}
    {"t":"warning","kind":"...","msg":"..."}
    {"t":"observe","files":[{dir,name,kind,path,schema,consumed,checks,profiles}],
     "stages":[...]}   - lineage of a folder run; the page forwards the
                         content to POST …/runs/{id}/observations

    {"t":"done","outputs":{"html":..|None,"slots":{..},"write_back":{..}},"gauges":{..}}
    {"t":"error","kind":"...","message":"...","detail":"..."}
"""
from __future__ import annotations

import configparser
import hashlib
import json
import threading
import time
import zipfile
from pathlib import Path

from . import VERSION, folders, generator, gpsource, s3sync, s3ui, zipguard
from .creds import CredStore


class RunError(Exception):
    def __init__(self, kind: str, message: str, detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail


class Run:
    """One run: an event buffer + a condition for replay-and-tail, outputs, cancel."""

    def __init__(self, run_id: str, work_dir: Path):
        self.id = run_id
        self.work_dir = work_dir
        self._events: list[dict] = []
        self._cond = threading.Condition()
        self._done = False
        self._cancel = threading.Event()
        self.outputs: dict[str, bytes] = {}       # name -> bytes (small: echo)
        self.output_paths: dict[str, Path] = {}   # name -> file on disk (large: slots)

    # ── Publishing events (from the task side) ─────────────────────────
    def emit(self, event: dict) -> None:
        with self._cond:
            self._events.append(event)
            if event.get("t") in ("done", "error"):
                self._done = True
            self._cond.notify_all()
        self._log(event)

    def _log(self, event: dict) -> None:
        """Mirror the run's story into op-agent.log (stdout is redirected there
        by applog). Until 2026-09-09 the events went only to the browser's Run
        window, so an analyst opening the log after a failed run found HTTP
        noise and nothing about the run itself — not even which folders the
        generator searched for its xlsx files. One short line per event, the
        generator's own text; no data rows pass through here."""
        try:
            t = event.get("t")
            rid = str(self.id)[:8]
            if t == "progress":
                stage = event.get("stage", "")
                if event.get("done") is not None and event.get("total"):
                    line = f"[{stage}] {event.get('done')}/{event.get('total')} {event.get('msg', '')}"
                else:
                    line = f"[{stage}] {event.get('output', '')} {event.get('phase', '')} " \
                           f"{event.get('rows', '')}".rstrip()
            elif t == "stage":
                line = f"stage: {event.get('stage', '')}"
            elif t == "error":
                line = (f"ERROR {event.get('kind', '')}: {event.get('message', '')}"
                        + (f" | {event.get('detail', '')}" if event.get("detail") else ""))
            elif t == "observe":
                # Only the COUNT: the frame carries sheet and column names, while the
                # log is a file on the analyst's machine and ends up in support
                # requests. "How many observations went out" answers the question
                # "why is the lineage empty", and nothing more is needed.
                line = (f"observe: files={len(event.get('files') or [])}"
                        f" stages={len(event.get('stages') or [])}")
            elif t == "done":
                line = "done"
            else:
                return
            print(f"[run {rid}] {line}", flush=True)
        except Exception:
            pass

    # ── Consuming events (from the /events side; replay + tail) ────────
    def stream(self, timeout_per_wait: float = 1.0):
        """Generator of dict events: first those already accumulated, then new ones, up to
        the terminal one. Supports attaching a consumer AFTER the start."""
        idx = 0
        while True:
            with self._cond:
                while idx >= len(self._events) and not self._done:
                    self._cond.wait(timeout_per_wait)
                batch = self._events[idx:]
                idx = len(self._events)
                done = self._done
            for ev in batch:
                yield ev
            if done and idx >= len(self._events):
                return

    @property
    def done(self) -> bool:
        with self._cond:
            return self._done

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def canceled(self) -> bool:
        return self._cancel.is_set()

    def terminal(self) -> dict | None:
        """The run's final event (`done`/`error`) or None if it is still
        running. The tray icon is coloured and shows a notification from it."""
        with self._cond:
            if not self._events:
                return None
            last = self._events[-1]
            return last if last.get("t") in ("done", "error") else None


def verify_and_unpack(bundle_bytes: bytes, checksum_sha256: str, dest: Path) -> list[str]:
    """Verify sha256(bundle)==checksum and safely unpack. Return the
    list of bundle file names (for the echo confirmation). RunError on mismatch."""
    got = hashlib.sha256(bundle_bytes).hexdigest()
    if not checksum_sha256 or got != checksum_sha256:
        raise RunError("checksum_mismatch",
                       "Bundle checksum does not match the version manifest",
                       f"expected={checksum_sha256} got={got}")
    dest.mkdir(parents=True, exist_ok=True)
    zpath = dest / "_bundle.zip"
    zpath.write_bytes(bundle_bytes)
    app_dir = dest / "app"
    zipguard.safe_extract(zpath, app_dir)
    try:
        with zipfile.ZipFile(zpath) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
    finally:
        try:
            zpath.unlink()
        except OSError:
            pass
    return sorted(names)



def _match_input(name: str, inputs: list) -> str | None:
    """The first `inputs` template that recognises this file name - as in the browser.

    The browser sets `matched_input` while walking a folder (`gen-engine.js`): the
    first `inputs[].match` that fits, and quality checks and declared columns are
    later selected by it. Masks are compared by `folders.matches` - one port of
    `app/services/globmatch.py` for the whole application: a second answer to
    the question "what was read" would mean lineage that contradicts what was
    computed.

    What is DELIBERATELY absent here: with declared `inputs` the browser also does
    NOT READ a file that no template recognised. Here the set was chosen by the
    person in `folders.ini` - dropping a file they named would silently compute
    over half a folder, which is the worst possible outcome (the same reason
    missing files are always reported). An unmarked file simply goes through
    without checks and without declared columns.
    """
    for inp in inputs:
        if not isinstance(inp, dict):
            continue
        pat = inp.get("match")
        if isinstance(pat, str) and pat and folders.matches(pat, name):
            return pat
    return None


def _s3_stale_ok(meta: dict) -> bool:
    """Whether the person asked to compute on what is already downloaded THIS time.

    Separate from the connection's checkbox: the checkbox is a setting ("do not go
    over the network here any more"), while this is a one-off decision at the button
    ("the bucket is there, but I need the report now"). Neither is ever a default:
    the field must arrive in `meta` explicitly.
    """
    return bool(meta.get("s3_stale_ok"))


def _pull_buckets(run: "Run", src, *, stale_ok: bool = False,
                  cred_store=None, ini_path: str | None = None) -> None:
    """Fetch fresh data from the buckets that this report's folders point at.

    This is what "the bucket is the report's source" means: the link is expressed
    by a FOLDER and computed in `s3sync.connections_for` (which also explains why
    it is done this way), while here it is what the run does with it.

    The failure policy is NAMED, not accidental. If the bucket is unavailable (no
    network, 403, foreign region) the run stops with `RunError`, just as it stops
    for `folders_no_section`, and the refusal names the way out: what to switch
    off or what to press in order to compute anyway. Silently computing on
    yesterday's files is not allowed: a quietly wrong report is worse than a
    refusal, and it is discovered a week later.

    Computing on what is already downloaded is possible through two deliberate
    actions, and both are visible on screen: untick "refresh before run" on the
    connection (a setting) or ask for it at launch (`meta.s3_stale_ok`).
    """
    # The "report folder <-> bucket" link lives in s3sync: it is expressed by a
    # FOLDER, and there must be no second implementation of it here.
    path = ini_path or s3ui.agent_ini_path()
    try:
        known = s3sync.load_s3(path)
    except (OSError, ValueError, configparser.Error):
        # A broken or unreadable `agent.ini` is a diagnosis for the "Report
        # folders" screen, not for the run: it holds no connections for us right
        # now, and there is nothing to invent them from. The folders are declared
        # in ANOTHER file and are read as usual.
        return
    store = cred_store if cred_store is not None else CredStore()
    for conn in s3sync.connections_for(src, known):
        if not conn.refresh_before_run:
            # The person unticked the box deliberately: compute on what is already
            # downloaded and say so out loud - there must be no silence here.
            run.emit({"t": "warning", "kind": "s3_not_refreshed",
                      "msg": f"{conn.name}: refresh before run is off, "
                             f"computing on what is already downloaded"})
            continue
        run.emit({"t": "stage", "stage": "s3"})
        run.emit({"t": "progress", "stage": "s3", "done": 0, "total": 0,
                  "msg": f"{conn.name}: fetching data from the bucket"})
        try:
            # The same access to the secret as the screen and the `s3-sync` command:
            # we do not open a second road to DPAPI.
            secret = store.get_password(conn.cred_name)
        except Exception as exc:                        # noqa: BLE001
            # We do not hand the credential store exception text outward in full:
            # there is no secret in it, but no use either - the type is enough.
            secret = None
            if not _bucket_may_be_stale(run, conn, stale_ok):
                raise RunError(
                    "s3_no_secret",
                    f"the Windows protected store did not return the secret "
                    f"of connection “{conn.name}”",
                    type(exc).__name__) from None
            continue
        if not secret:
            if _bucket_may_be_stale(run, conn, stale_ok):
                continue
            raise RunError(
                "s3_no_secret",
                f"connection “{conn.name}” has no secret key set, "
                f"and there is no way to fetch fresh files",
                f"open “Report folders”, the “S3 bucket” section, connection "
                f"“{conn.name}”, enter the Secret access key and save")
        try:
            rep = s3sync.sync(conn, secret, progress=lambda d, t, b:
                              run.emit({"t": "progress", "stage": "s3",
                                        "done": d, "total": t, "bytes": b}))
        except s3sync.S3Error as exc:
            # `str(exc)` is already a human sentence with advice: `_explain`
            # translated the HTTP code and the S3 code (a foreign region and a
            # foreign path_style give refusals that look identical).
            reason = str(exc)[:400]
        else:
            reason = ("; ".join(f"{k or 'bucket'}: {why}"
                                for k, why in rep.errors[:3])[:400]
                      if rep.errors else "")
            if not reason:
                run.emit({"t": "progress", "stage": "s3",
                          "done": rep.matched, "total": rep.matched,
                          "msg": f"{conn.name}: downloaded {rep.downloaded}, "
                                 f"unchanged {rep.skipped}, "
                                 f"removed {rep.deleted}"})
                if rep.foreign:
                    # Foreign files in the destination folder are not a refusal (we
                    # did not bring them and do not touch them), but not silence
                    # either: they are what doubles the data if left over from a
                    # manual export.
                    run.emit({"t": "warning", "kind": "s3_foreign_files",
                              "msg": f"{conn.name}: the folder has files that the "
                                     f"program did not bring: "
                                     f"{', '.join(rep.foreign[:5])}"})
                continue
        if _bucket_may_be_stale(run, conn, stale_ok, reason):
            continue
        raise RunError(
            "s3_unavailable",
            f"bucket “{conn.name}” is unavailable, and the report reads a folder "
            f"that it alone fills - I will not compute on yesterday's files",
            f"{reason} | you can compute on what is already downloaded "
            f"deliberately: untick “refresh before run” on connection "
            f"“{conn.name}” in the “Report folders” screen (the program icon "
            f"op-agent.bat folders)")


def _bucket_may_be_stale(run: "Run", conn, stale_ok: bool,
                         reason: str = "") -> bool:
    """Whether it is allowed to continue on what is already downloaded this time.

    The permission is always LOUD: the warning goes both to the run window and to
    the log. The person who allowed it must see in the report that the numbers
    were not computed from today's bucket.
    """
    if not stale_ok:
        return False
    run.emit({"t": "warning", "kind": "s3_stale",
              "msg": f"{conn.name}: the bucket did not answer, computing on what is already downloaded"
                     + (f" ({reason})" if reason else "")})
    return True


def _resolve_folder_files(run: "Run", meta: dict, inputs: list | None = None) -> list[dict]:
    """Lay out the report's folders into a file list for the R2 contract.

    The source is `folders.ini` next to the program: the person declares folders
    by PATHS, not by a browser descriptor, so it is visible which exact file was
    read. `scenario` selects a named variant of the set - the field is laid down
    now, the selection UI will be drawn later (owner's decision of 19.10).

    Three refusals BEFORE the generator starts, and all three are loud. A silent
    empty run is the worst possible outcome: the report is built, published and
    lies, and the person finds out a week later.
    """
    dash = str(meta.get("dashboard_id") or "").strip()
    scen = str(meta.get("scenario") or "").strip()
    if not dash:
        raise RunError("folders_no_dashboard",
                       "the page did not name the report - I do not know which "
                       "folders.ini section to read")

    ini = folders.default_ini_path()
    sources = folders.load(ini)
    key = f"{dash}:{scen}" if scen else dash
    src = sources.get(key)
    if src is None:
        known = folders.scenarios_for(sources, dash)
        if known:
            raise RunError("folders_no_scenario",
                           f"{ini} has no scenario {scen!r} for this report",
                           "known: " + ", ".join(n or "(unnamed)" for n in known))
        # The refusal must SAY where to go. The earlier text named the file and
        # demanded to "declare folders" - and the person reasonably asked why they
        # cannot simply pick a folder with the mouse. They can, in the "Report
        # folders" screen, and now it is written here, not only in the README,
        # which nobody will reach at this moment.
        raise RunError("folders_no_section",
                       f"no data folders have been chosen for this report yet",
                       f"open “Report folders” (the program icon near the clock "
                       f"or op-agent.bat folders), add the report {dash} and "
                       f"pick the folder with the “Browse…” button. Settings file: {ini}")

    # The bucket as a SOURCE, not as a setting. If the report folder is the one
    # that the S3 connection downloads into, the fresh files must be fetched BEFORE
    # reading: otherwise the generator will honestly recompute the report on
    # yesterday's exports and suspect nothing, and a quietly wrong report is worse
    # than a refusal.
    _pull_buckets(run, src, stale_ok=_s3_stale_ok(meta))

    res = folders.resolve(src)

    # Missing files are ALWAYS reported, even when files were found: half a folder
    # read silently is the same lying report, only less noticeable.
    for m in res.missing:
        run.emit({"t": "warning", "kind": "folder_" + m.reason,
                  "msg": f"{m.raw}: {m.reason}"})

    if not res.files:
        raise RunError("folders_empty",
                       "no files were found in the declared folders",
                       "; ".join(f"{m.raw}: {m.reason}" for m in res.missing))

    # The same names from different folders - a refusal with BOTH paths. The
    # generator tells files apart by name, not by path, and this is exactly the
    # confusion about which the owner said "one department in one excel, another in another".
    seen: dict[str, str] = {}
    dupes: list[str] = []
    for f in res.files:
        prev = seen.get(f.name)
        if prev is not None:
            dupes.append(f"{f.name}: {prev} and {f.path}")
        else:
            seen[f.name] = f.path
    if dupes:
        raise RunError("folders_duplicate_names",
                       "the same file name in different folders", "; ".join(dupes[:10]))

    run.emit({"t": "stage", "stage": "resolve"})
    run.emit({"t": "progress", "stage": "resolve", "done": len(res.files),
              "total": len(res.files),
              "msg": f"files: {len(res.files)}" + (f", scenario “{scen}”" if scen else "")})
    # `dir` is the section entry number, exactly the one `GET /folders` returns and
    # that arrives in `roots[].dir` of the start manifest: the page and the
    # server use it to tie a file observation to its root.
    decl = [i for i in (inputs or []) if isinstance(i, dict)]
    out = []
    for f in res.files:
        item = {"name": f.name, "mtime_ms": f.mtime_ms, "size": f.size,
                "path": f.path, "dir": f.entry_index}
        matched = _match_input(f.name, decl)
        if matched:
            item["matched_input"] = matched
        out.append(item)
    return out


def run_echo(run: Run, bundle_bytes: bytes, meta: dict) -> None:
    """M1 echo task. Runs in a separate thread; sends events via run.emit."""
    try:
        run.emit({"t": "stage", "stage": "received"})
        run.emit({"t": "stage", "stage": "unpack"})
        names = verify_and_unpack(bundle_bytes, meta.get("checksum_sha256", ""), run.work_dir)

        run.emit({"t": "stage", "stage": "echo"})
        total = max(1, len(names))
        for i, nm in enumerate(names):
            if run.canceled:
                run.emit({"t": "error", "kind": "canceled", "message": "Canceled by user"})
                return
            run.emit({"t": "progress", "stage": "echo", "done": i, "total": total, "msg": nm})
            time.sleep(0.05)   # a visible event stream for measuring NDJSON latency

        # A tiny confirmation output: proves integrity + unpacking + delivery.
        payload = json.dumps({
            "echo": True,
            "run_id": meta.get("run_id"),
            "generator": meta.get("generator"),
            "version": meta.get("version"),
            "bundle_files": names,
            "checksum_ok": True,
        }, ensure_ascii=False).encode("utf-8")
        run.outputs["echo.json"] = payload

        run.emit({
            "t": "done",
            "outputs": {"html": None, "slots": {"echo": "echo.json"}, "write_back": {}},
            "gauges": {"bundle_files": len(names)},
        })
    except RunError as e:
        run.emit({"t": "error", "kind": e.kind, "message": e.message, "detail": e.detail})
    except Exception as e:  # noqa: BLE001 - any failure becomes an event, not a daemon crash
        run.emit({"t": "error", "kind": "internal", "message": str(e)[:300], "detail": ""})


def run_generator(run: Run, bundle_bytes: bytes, meta: dict, *, config,
                  cred_store, python_exe: str | None = None,
                  connect=None) -> None:
    """M2 run: unpack the bundle -> (if there is `sources`) run GP under the personal
    credential into inbox/ -> start the native generator as a subprocess -> register
    the outputs for delivery to the page. Any failure becomes an NDJSON `error` event,
    the daemon does not crash. The password stays inside gpsource and never reaches events."""
    try:
        run.emit({"t": "stage", "stage": "received"})
        run.emit({"t": "stage", "stage": "unpack"})
        verify_and_unpack(bundle_bytes, meta.get("checksum_sha256", ""), run.work_dir)
        app_dir = run.work_dir / "app"

        try:
            manifest = json.loads((app_dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RunError("bad_bundle", "cannot read the bundle manifest", str(e)[:200])

        inbox = run.work_dir / "inbox"
        out_dir = run.work_dir / "out"
        cache_dir = run.work_dir / "cache"
        inbox.mkdir(parents=True, exist_ok=True)

        # ── GreenPlum sources (if declared) ───────────────────────────────
        sources = manifest.get("sources")

        # K3: the page declares `mode` separately from what is written in the
        # manifest, and these two answers must agree. They diverge not
        # theoretically: between the folder probe at page load and the button press
        # the generator could have been re-registered with a `sources` block -
        # and then a person who just saw on screen a list of files
        # from their folder would silently get a GreenPlum report. The server closes this
        # case (K2), but the agent is a separate entry point with its own
        # bundle, and there is nothing to rely on someone else's check here. The refusal comes
        # BEFORE the password prompt: asking for a credential for a run we will
        # not perform anyway is the worst possible order.
        if meta.get("mode") == "folders" and sources:
            raise RunError(
                "mode_mismatch",
                "the page asks for a folder run, but the bundle manifest declares "
                "`sources` (GreenPlum) - refresh the page: the generator "
                "was re-registered, and a folder run is no longer provided for it")

        if sources:
            conn_name = sources.get("connection", "")
            topology = (config.connections or {}).get(conn_name)
            if not topology or not topology.get("host"):
                raise RunError("gp_config",
                               f"no connection topology {conn_name!r} in agent.ini")
            password = cred_store.get_password(conn_name)
            if password is None:
                raise RunError("cred_missing",
                               f"no saved password for {conn_name!r} - "
                               f"enter the credentials in the agent")
            app_name = (f"op-agent/{VERSION} "
                        f"{meta.get('generator', '?')}@{meta.get('version', '?')}")
            try:
                gpsource.run_sources(
                    sources, topology, password, app_name, app_dir, inbox,
                    run.emit, cancel=lambda: run.canceled, connect=connect)
            except (gpsource.GpError, gpsource.RunCanceled):
                return  # the error/canceled event has already been sent inside run_sources
            finally:
                password = None  # do not keep the password in the frame longer than needed

        # ── native generator ──────────────────────────────────────────────
        module = (manifest.get("entrypoint") or {}).get("module")
        if not module:
            raise RunError("bad_bundle", "the manifest has no entrypoint.module")
        dispositions: list[dict] = []
        try:
            if sources:
                # GreenPlum: the files are already in inbox, the generator finds them itself.
                # The legacy CLI stays exactly here and only here.
                result = generator.run_generator_subprocess(
                    module, app_dir, inbox, out_dir,
                    meta.get("checksum_sha256", ""), run.emit,
                    cancel=lambda: run.canceled, python_exe=python_exe,
                    cache_dir=cache_dir)
            else:
                # Folders: the R2 contract, the same one the browser executes. Before
                # this branch did not exist at all - a bundle without `sources` got an EMPTY
                # inbox and built a report from nothing, silently.
                files = _resolve_folder_files(run, meta, manifest.get("inputs"))
                run.emit({"t": "stage", "stage": "generate"})
                out = generator.run_r2_subprocess(
                    module, app_dir, out_dir, files,
                    meta.get("checksum_sha256", ""), run.emit,
                    cancel=lambda: run.canceled, python_exe=python_exe,
                    cache_dir=cache_dir)
                result, dispositions = out["result"], out["dispositions"]
        except generator.GeneratorError as e:
            run.emit({"t": "error", "kind": e.kind, "message": e.message, "detail": e.detail})
            return

        # ── registering outputs for delivery to the page ──────────────────
        html = result.get("html")
        slots = result.get("slots") or {}
        write_back = result.get("write_back") or {}
        for name in [html, *slots.values(), *write_back.values()]:
            if not name:
                continue
            p = out_dir / name
            if p.is_file():
                run.output_paths[name] = p
            else:
                raise RunError("gen_bad_result",
                               f"the generator declared output {name!r}, but there is no file")

        run.emit({
            "t": "done",
            "outputs": {"html": html, "slots": slots, "write_back": write_back},
            "dispositions": dispositions,
            "gauges": {"outputs": len(run.output_paths)},
        })
    except RunError as e:
        run.emit({"t": "error", "kind": e.kind, "message": e.message, "detail": e.detail})
    except Exception as e:  # noqa: BLE001 - any failure becomes an event, not a daemon crash
        run.emit({"t": "error", "kind": "internal", "message": str(e)[:300], "detail": ""})
