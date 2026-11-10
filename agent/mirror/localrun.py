"""Generator run WITHOUT the platform: folders on disk -> a local report version.

Why the module exists. Before it, a run was possible exactly when the OnePage
page was alive: it takes the run lock, holds the heartbeat, hands the agent the
bundle, and publishes the outputs. With the server down there is no page at
all — so a person with the right to update data could not update it on
precisely the day it matters most. The owner's decision (11 Oct, not up for
review): "a local run must work without the server too", and "when the server
comes back — ASK the person, do not publish silently".

What happens here and what is DELIBERATELY absent
-------------------------------------------------

Exactly the same happens as in a network run, with the same functions: the
bundle is unpacked by `runs.verify_and_unpack`, folders are resolved by
`runs._resolve_folder_files` (with all its loud refusals), and the generator is
executed by `generator.run_r2_subprocess` under the R2 contract. There is no
second implementation of the run here and there must not be: if it diverged
from the network one, "updated locally, then published" would give two
different reports from one folder, and silently.

Three things are absent:

  * **The network.** Not a single call to the platform. The bundle is taken
    from the mirror cache (put there by `sync._cache_bundle` on a successful
    sync), the rights from `state.json`, the folders from `folders.ini`. The
    module must work on a machine without a single byte of connectivity.
  * **Publication.** The output is placed in `local/L<k>` and stays there
    until a PERSON agrees to publish it. There is no automatic catch-up, by
    the owner's decision; our job is to save the run and its input
    (`input_manifest`) so that catch-up is possible later.
  * **RLS reports.** They are not here in any form: they never occur in the
    mirror (ADR-18), `layout.can_update_locally` answers "no" for
    `rls_enabled`, and a separate check stands here too. Three lines of
    defence for one ban — because the price of a mistake here is not "the
    button doesn't work" but somebody else's rows without a filter, lying as a
    file on disk.

The right to update locally is limited in time
----------------------------------------------

`rights.can_update_data` is fetched by the sync, and there is nobody to revoke
it offline: the revocation arrives only with the next successful connection.
The only thing that protects us is the term (`rights.expires_at`, a day by
default). Expired — the run does NOT start, and the refusal names the date, not
"no rights": the person needs to understand that it is a matter of time, not of
them. READING what is already downloaded is never blocked in the meantime
(RFC §5) — these are different questions, and they must not be confused.

The local run document and the data feed bridge
-----------------------------------------------

The generator returns the BARE html of the report — the very one that is
uploaded as a new version on the platform. An interactive report does not hold
its data inside: it is fed by the `op_ready` -> `op_file` bridge, and the
bridge is inserted by the SERVER when serving (`detector.inject_bootstrap`,
only for `dash_type == "interactive"`). Offline there is no server, and the
naive answer "put the html as is" would give an empty report — exactly the
silent failure the whole undertaking was meant to avoid.

So the bridge is TAKEN FROM THE ALREADY DOWNLOADED document of the same version
(`server/v<n>/dashboard.html`): it is not our copy of somebody else's code but
the very script the platform inserted into THIS report — with all the fixes
made to it there. The agent deliberately keeps no copy of the bridge of its
own: a copy that diverged from the original will one day feed data into the
wrong `input[type=file]`, and this can be noticed only by wrong numbers.

Three outcomes, and each is named in the run record by the `bridge` field:
  `own`      — the generator's html already has the bridge (a snapshot report
               with data inside, or a report written to be self-contained);
  `borrowed` — taken from the mirror of the server version;
  `none`     — nowhere to take it from (no server copy, or the platform
               renamed the marker). The run is NOT cancelled in that case: the
               outputs are valuable in themselves, they still have to be
               published. But the library must say that locally the report
               will show empty sections.

Standard library only (agent/ is Python 3.12, stdlib; see agent/README).
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .. import generator, runs
from ..config import state_dir
from . import layout, store

# The bridge marker in a document served by the platform. A contract string:
# `app/services/detector.py::inject_bootstrap` starts the inserted block with
# it. If they diverge, `bridge` becomes `none`, and this will be visible in the
# library rather than silent. Checking the marker against `detector.py` itself
# lives in the smoke: there is no platform code here and there cannot be (the
# agent ships without `app/`).
BOOTSTRAP_MARKER = "/* OnePage platform bootstrap — injected */"

# File names next to the outputs of a local run. Both are needed for CATCH-UP:
# the page, publishing a run after the fact, must send the server the very
# input the run was computed from, and the very lineage observations —
# otherwise a version without provenance would appear on the platform.
INPUT_MANIFEST_NAME = "input_manifest.json"
OBSERVE_NAME = "observe.json"


class LocalRunRefused(Exception):
    """The run was not started or not completed, and the refusal has a CODE.

    A code, not just text: the library picks a label by it (labels get
    translated), and the test pins the refusal itself. The texts here are
    human-readable anyway — the module is also called from the command line.
    """

    def __init__(self, kind: str, message: str, detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail


def _now_iso() -> str:
    """UTC without a zone — exactly in the form in which the synchronizer
    writes time (`sync._now_iso`). Two time formats in one state.json would
    mean a string comparison that will one day turn out wrong."""
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Whether it is possible at all                                               #
# --------------------------------------------------------------------------- #

def precheck(dashboard_id: str, state: dict | None = None) -> tuple[str, str]:
    """`("ok", "")` or `(code, human-readable reason)`. Touches the disk
    only to read the state and check that the bundle exists.

    This same function answers the library's question "should the button be
    drawn": a button that answers with a refusal is worse than a missing button.
    """
    st = store.load_state(dashboard_id) if state is None else state
    if not st:
        return "no_mirror", "this report is not in the mirror — sync first"
    rights = st.get("rights") if isinstance(st.get("rights"), dict) else {}
    if rights.get("rls_enabled"):
        # ADR-18. Such a report cannot be in the mirror at all, and a state
        # with this flag means corruption — but the answer is the same: no.
        return "rls", "the report shows each person only their own rows — it is not updated locally"
    if not rights.get("can_update_data"):
        return "no_right", "the platform has not given you the right to update this report's data"
    if layout.rights_expired(st):
        when = rights.get("expires_at") or "an unknown time"
        return ("rights_expired",
                f"rights have not been confirmed since {when} — updating is "
                f"suspended until the next contact with the platform; the "
                f"report can still be read")
    gen = st.get("generator") if isinstance(st.get("generator"), dict) else None
    if not gen:
        return "no_generator", "the report has no generator — nothing to update it with"
    try:
        path = layout.bundle_path(dashboard_id, gen.get("checksum_sha256"))
    except ValueError:
        return "no_bundle", "the generator bundle is not in the cache — a sync is needed"
    if not path.is_file():
        return "no_bundle", "the generator bundle is not in the cache — a sync is needed"
    return "ok", ""


def can_run(dashboard_id: str, state: dict | None = None) -> bool:
    return precheck(dashboard_id, state)[0] == "ok"


# --------------------------------------------------------------------------- #
# Run progress: one state per program                                         #
# --------------------------------------------------------------------------- #

class _Progress:
    """Progress of the current run for the library. One record per program.

    A run takes minutes, and the page polls `data.json` every second and a
    half — so the progress has to live somewhere. There is no thread of its own
    here: `start()` starts the thread in the caller (the server), and this
    record only keeps a snapshot under a lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict = {}

    def begin(self, dashboard_id: str) -> None:
        with self._lock:
            self._state = {"dashboard_id": dashboard_id, "running": True,
                           "started_at": time.time(), "stage": "", "msg": "",
                           "done": 0, "total": 0}

    def event(self, ev: dict) -> None:
        kind = ev.get("t")
        with self._lock:
            if not self._state.get("running"):
                return
            if kind == "stage":
                self._state["stage"] = str(ev.get("stage") or "")
            elif kind == "progress":
                self._state["stage"] = str(ev.get("stage") or self._state.get("stage") or "")
                self._state["msg"] = str(ev.get("msg") or "")
                for key in ("done", "total"):
                    value = ev.get(key)
                    if isinstance(value, int) and not isinstance(value, bool):
                        self._state[key] = value

    def finish(self, *, ok: bool, kind: str = "", message: str = "",
               k: int | None = None) -> None:
        with self._lock:
            self._state.update({"running": False, "ok": ok, "error_kind": kind,
                                "error": message, "k": k,
                                "finished_at": time.time()})

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)


PROGRESS = _Progress()
_RUNNING = threading.Lock()


def running_for() -> str | None:
    """Which report is being computed right now (or None)."""
    snap = PROGRESS.snapshot()
    return snap.get("dashboard_id") if snap.get("running") else None


class _Run(runs.Run):
    """`runs.Run` plus two things needed only by a local run: the `observe`
    frames (they will have to be handed to the platform on catch-up) and
    relaying progress into `PROGRESS`."""

    def __init__(self, run_id: str, work_dir: Path) -> None:
        super().__init__(run_id, work_dir)
        self.observed: list[dict] = []

    def emit(self, event: dict) -> None:
        super().emit(event)
        if event.get("t") == "observe":
            self.observed.append(event)
        try:
            PROGRESS.event(event)
        except Exception:                                     # noqa: BLE001
            pass            # progress is decoration; it must not bring a run down


# --------------------------------------------------------------------------- #
# Data feed bridge                                                            #
# --------------------------------------------------------------------------- #

_HEAD_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_SLOTS_RE = re.compile(r"^(\s*window\.__OP_SLOTS__\s*=\s*).*?;\s*$", re.MULTILINE)


def extract_bootstrap(server_doc: str) -> str | None:
    """Cut the bridge out of a document downloaded from the platform, or None.

    The boundaries are the marker and the FIRST `</script>` after it: the
    inserted block contains no literal `</script>` (otherwise it would have
    closed itself already on the server), so the search is unambiguous and
    needs no html parsing.
    """
    if not isinstance(server_doc, str):
        return None
    at = server_doc.find(BOOTSTRAP_MARKER)
    if at < 0:
        return None
    start = server_doc.rfind("<script", 0, at)
    end = server_doc.find("</script>", at)
    if start < 0 or end < 0:
        return None
    return server_doc[start:end + len("</script>")]


def with_bridge(html: str, server_doc: str | None, slots_json: str) -> tuple[str, str]:
    """`(document, bridge kind)` — see the section of the module docstring.

    `slots_json` is substituted in one line: `window.__OP_SLOTS__` in the
    borrowed bridge describes the slots of the PREVIOUS version, and the report
    reads it on `op_ready`. Not substituting it would hand the report an
    inventory that matches nothing on disk.
    """
    if BOOTSTRAP_MARKER in html:
        return html, "own"
    block = extract_bootstrap(server_doc or "")
    if not block:
        return html, "none"
    block = _SLOTS_RE.sub(lambda m: m.group(1) + slots_json + ";", block, count=1)
    m = _HEAD_RE.search(html)
    if m:
        return html[:m.end()] + "\n" + block + html[m.end():], "borrowed"
    # No `<head>` — insert at the start, exactly as the server does.
    return block + html, "borrowed"


# --------------------------------------------------------------------------- #
# The run                                                                     #
# --------------------------------------------------------------------------- #

def _work_root() -> Path:
    """Run working directory — OUTSIDE the mirror.

    The unpacked bundle, the generator cache, and the outputs before copying
    must not count towards the mirror quota and must not fall under
    `store.sweep`: this is the litter of one run, not a copy of a report.
    """
    root = state_dir() / "local-runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _copy_into(src: Path, dest: Path) -> int:
    """Copy an output file into the run directory atomically. Returns the size.

    Through `layout.atomic_write`, not `shutil.copy`: an interrupted copy would
    leave a file with the right name and the wrong length — exactly the
    "half-file that looks whole" that is forbidden in the mirror.
    """
    written = 0
    with layout.atomic_write(dest, "wb") as out:
        with open(src, "rb") as fh:
            while True:
                buf = fh.read(1 << 20)
                if not buf:
                    break
                out.write(buf)
                written += len(buf)
    return written


def _server_document(dashboard_id: str, state: dict) -> str | None:
    """Text of the already downloaded server-version document — the bridge source."""
    n = state.get("server_version")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        return None
    try:
        path = layout.server_version_dir(dashboard_id, n) / layout.DOCUMENT_NAME
    except ValueError:
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _input_manifest(info: dict | None, files: list[dict], scenario: str) -> dict:
    """The run input in the form in which the page sends it for `engine:"local"`
    (`static/js/gen-engine.js::buildLocalInputManifest`).

    The shape is repeated VERBATIM, because it is what will be sent to the
    server on catch-up: if it diverged, an offline run would end up on the
    platform with a different lineage than the same run online.

    `roots` are taken from the `GET /folders` response (`info`) if there is
    one: the root name is computed by the agent (`server._root_name`), and a
    second implementation of that parsing must not be introduced. No response —
    no roots, and that is more honest than invented ones.
    """
    roots = []
    for r in (info or {}).get("roots") or []:
        if not isinstance(r, dict):
            continue
        roots.append({"dir": r.get("dir") or 0, "root_path": r.get("root_path"),
                      "root_id": r.get("root_id")})
    names = [((r.get("name") or r.get("root_path")) if isinstance(r, dict) else None)
             for r in (info or {}).get("roots") or []]
    out = {
        "source": "folders",
        "scenario": scenario or "",
        "files": [{"name": f["name"], "size": f["size"], "mtime_ms": f["mtime_ms"],
                   "dir": f.get("dir") or 0, "path": f.get("path")}
                  for f in files],
        "dirs": [n for n in names if n],
        "roots": roots,
        "total_bytes": sum(int(f.get("size") or 0) for f in files),
        "count": len(files),
        "skipped": [],
    }
    for item, src in zip(out["files"], files):
        if src.get("matched_input"):
            item["matched_input"] = src["matched_input"]
    return out


def run_local(dashboard_id: str, scenario: str = "", *, folders_info=None,
              python_exe: str | None = None, cancel=None) -> dict:
    """Build the report from local folders and put it into `local/L<k>`.

    Returns the run record (the same one that will go into `state.json`). Any
    refusal is a `LocalRunRefused` with a code: quietly reporting "done" for a
    run that was not done is impossible here.

    `folders_info` is an optional `GET /folders` response (contract K1) from
    which the roots for `input_manifest` are taken. The server passes it; the
    module does not fetch it itself, because otherwise it would have to import
    `agent/server.py`, which itself imports the mirror.
    """
    if not _RUNNING.acquire(blocking=False):
        raise LocalRunRefused("busy", "a run is already in progress — wait for it to finish")
    try:
        return _run_local_locked(dashboard_id, scenario, folders_info,
                                 python_exe, cancel)
    finally:
        _RUNNING.release()


def _run_local_locked(dashboard_id: str, scenario: str, folders_info,
                      python_exe: str | None, cancel) -> dict:
    state = store.load_state(dashboard_id)
    kind, why = precheck(dashboard_id, state)
    if kind != "ok":
        raise LocalRunRefused(kind, why)

    PROGRESS.begin(dashboard_id)
    gen = state.get("generator") or {}
    checksum = gen.get("checksum_sha256") or ""
    bundle_path = layout.bundle_path(dashboard_id, checksum)

    run_id = f"L{layout.next_local_n(state)}-{int(time.time())}"
    work = _work_root() / run_id
    run = _Run(run_id, work)
    try:
        try:
            bundle_bytes = bundle_path.read_bytes()
        except OSError as exc:
            raise LocalRunRefused("no_bundle",
                                  "the generator bundle cannot be read",
                                  str(exc)[:200]) from None

        run.emit({"t": "stage", "stage": "unpack"})
        try:
            runs.verify_and_unpack(bundle_bytes, checksum, work)
        except runs.RunError as exc:
            raise LocalRunRefused(exc.kind, exc.message, exc.detail) from None
        finally:
            bundle_bytes = b""          # do not hold tens of megabytes in the frame

        app_dir = work / "app"
        try:
            manifest = json.loads((app_dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LocalRunRefused("bad_bundle", "the bundle manifest cannot be read",
                                  str(exc)[:200]) from None
        if manifest.get("sources"):
            # GreenPlum: needs the warehouse and a personal password, i.e. the
            # network. Offline has neither, and the data source must not be
            # swapped silently.
            raise LocalRunRefused(
                "gp_bundle",
                "this report is built from the warehouse, not from folders — "
                "it cannot be updated locally without a connection")
        module = (manifest.get("entrypoint") or {}).get("module")
        if not module:
            raise LocalRunRefused("bad_bundle", "the manifest has no entrypoint.module")

        meta = {"dashboard_id": dashboard_id, "scenario": scenario or "",
                "mode": "folders", "checksum_sha256": checksum,
                "run_id": run_id, "generator": gen.get("name") or "",
                "version": gen.get("version")}
        try:
            files = runs._resolve_folder_files(run, meta, manifest.get("inputs"))
        except runs.RunError as exc:
            raise LocalRunRefused(exc.kind, exc.message, exc.detail) from None

        run.emit({"t": "stage", "stage": "generate"})
        out_dir = work / "out"
        try:
            out = generator.run_r2_subprocess(
                module, app_dir, out_dir, files, checksum, run.emit,
                cancel=cancel or (lambda: run.canceled), python_exe=python_exe,
                cache_dir=work / "cache")
        except generator.GeneratorError as exc:
            raise LocalRunRefused(exc.kind, exc.message, exc.detail) from None
        result = out.get("result") or {}

        record = _store_outputs(dashboard_id, state, result, out_dir,
                                run=run, files=files, scenario=scenario,
                                folders_info=folders_info, gen=gen)
        PROGRESS.finish(ok=True, k=record["k"])
        return record
    except LocalRunRefused as exc:
        PROGRESS.finish(ok=False, kind=exc.kind, message=exc.message)
        raise
    except Exception as exc:                                  # noqa: BLE001
        PROGRESS.finish(ok=False, kind="internal", message=str(exc)[:300])
        raise LocalRunRefused("internal", str(exc)[:300]) from None
    finally:
        # The working directory is ALWAYS removed: it holds the unpacked
        # bundle and copies of the outputs, and the person's disk space is not
        # ours.
        shutil.rmtree(work, ignore_errors=True)


def _store_outputs(dashboard_id: str, state: dict, result: dict, out_dir: Path,
                   *, run: _Run, files: list[dict], scenario: str,
                   folders_info, gen: dict) -> dict:
    """Move the generator outputs into `local/L<k>` and write the state."""
    html_name = result.get("html")
    raw_slots = result.get("slots") or {}
    write_back = result.get("write_back") or {}

    # Slot numbers are the KEYS of the R2 contract's `slots` dict: that is
    # exactly how the page reads them when building `slot_<idx>` for
    # publication (`gen-engine.js`).
    planned_src = []
    for idx, name in raw_slots.items():
        try:
            no = int(idx)
        except (TypeError, ValueError):
            raise LocalRunRefused(
                "gen_bad_result",
                f"the generator named a slot {idx!r}, but a slot number is an integer") from None
        src = out_dir / str(name)
        if not src.is_file():
            raise LocalRunRefused("gen_bad_result",
                                  f"the generator declared slot {name!r}, but the file is missing")
        st = src.stat()
        planned_src.append({"slot": no, "name": str(name), "size": st.st_size,
                            "v": f"{int(st.st_mtime)}-{st.st_size}"})
    # We check the document HERE too, before creating the run directory: a
    # refusal after `mkdir` would leave an empty `local/L<k>`, which the server
    # would show as a broken version and version sweeping would not touch.
    html_src = out_dir / str(html_name) if html_name else None
    if html_src is not None and not html_src.is_file():
        raise LocalRunRefused("gen_bad_result",
                              f"the generator declared html {html_name!r}, but the file is missing")

    planned = layout.plan_slot_files(planned_src)

    k = layout.next_local_n(state)
    dest = layout.local_run_dir(dashboard_id, k, create=True)

    # There may be no document at all (a generator that updates only data),
    # and that is not an error: the server will then answer 404 with a reason
    # rather than an empty page posing as a report.
    bridge = "none"
    if html_src is not None:
        src = html_src
        slots_public = [{"slot": p["slot"], "name": p["name"], "size": p["size"]}
                        for p in planned]
        html, bridge = with_bridge(
            src.read_text(encoding="utf-8", errors="replace"),
            _server_document(dashboard_id, state),
            json.dumps(slots_public, ensure_ascii=False))
        with layout.atomic_write(dest / layout.DOCUMENT_NAME, "w") as fh:
            fh.write(html)

    total = 0
    for rec in planned:
        src = out_dir / rec["name"]
        total += _copy_into(src, layout.slot_disk_path(dest, rec))
        # The slot lies on disk UNPACKED exactly as the generator wrote it:
        # there is nothing to compress with and no reason to, and the server
        # must know the truth about the bytes (the decision "the inventory
        # beats the name", `serve._target_from_record`).
        rec["enc"] = None if not str(rec["name"]).endswith((".gz", ".br")) else (
            "gzip" if str(rec["name"]).endswith(".gz") else "br")

    created_at = _now_iso()
    manifest_in = _input_manifest(folders_info, files, scenario)
    with layout.atomic_write(dest / INPUT_MANIFEST_NAME, "w") as fh:
        fh.write(json.dumps(manifest_in, ensure_ascii=False, indent=1))
    with layout.atomic_write(dest / OBSERVE_NAME, "w") as fh:
        fh.write(json.dumps(run.observed, ensure_ascii=False))

    record = layout.add_local_run(
        state, created_at, based_on=state.get("server_version"),
        note=f"built offline {created_at[:10]}")
    record.update({
        "slots": planned,
        "html": layout.DOCUMENT_NAME if html_name else None,
        "bridge": bridge,
        "scenario": scenario or "",
        "file_count": len(files),
        "bytes": total,
        # There is NO write-back to folders offline: the browser engine puts
        # such files into the analyst's folder, while we only NAME them. The
        # program must not write into other people's folders without the
        # owner's decision, and keeping silent about it is not an option —
        # hence the field name.
        "write_back_skipped": sorted(str(v) for v in write_back.values()),
        "generator": {"id": gen.get("id"), "name": gen.get("name"),
                      "version": gen.get("version"),
                      "checksum_sha256": gen.get("checksum_sha256")},
    })
    assert record["k"] == k                     # numbers are counted by one layout
    store.save_state(dashboard_id, state)
    return record


# --------------------------------------------------------------------------- #
# Catch-up queue                                                              #
# --------------------------------------------------------------------------- #

def publish_note(record: dict, today: str | None = None) -> str:
    """Version note for catch-up: WHEN it was built and WHEN it was published.

    We pay for not adding a new server endpoint with the fact that the future
    run's `started_at` will be the catch-up time, not the time of the real run.
    The only honest answer to that is to write both dates in the note rather
    than pretend the report was computed today.
    """
    made = str(record.get("created_at") or "")[:10]
    day = today or _now_iso()[:10]
    return f"built offline {made}, published {day}"


def _public_run(dashboard_id: str, rec: dict, *, with_input: bool) -> dict:
    out = {
        "dashboard_id": dashboard_id,
        "k": rec.get("k"),
        "created_at": rec.get("created_at"),
        "based_on_server_version": rec.get("based_on_server_version"),
        "published": bool(rec.get("published")),
        "bridge": rec.get("bridge"),
        "scenario": rec.get("scenario") or "",
        "note": publish_note(rec),
        "generator": rec.get("generator") or {},
        "outputs": {
            "html": rec.get("html"),
            # Slot number -> ORIGINAL name: the page builds `slot_<idx>` for
            # publication from this, and it does not need the name on disk.
            "slots": {str(s.get("slot")): s.get("name")
                      for s in (rec.get("slots") or []) if isinstance(s, dict)},
        },
    }
    if with_input:
        out["input_manifest"] = read_input_manifest(dashboard_id, rec.get("k"))
        # Lineage travels TOGETHER with the input, not via a separate
        # endpoint. The `observe` frames were taken during the offline run
        # (sheet schemas, columns read) and cannot be obtained anywhere else:
        # there is nothing to recompute them with on catch-up — the files in
        # the folders may have moved away by then. Handing over the outputs
        # without them means publishing a version for which the platform does
        # not know what exactly was read — precisely the hole that lineage
        # closes.
        out["observe"] = read_observe(dashboard_id, rec.get("k"))
    return out


def list_unpublished(dashboard_id: str | None = None, *,
                     with_input: bool = False) -> list[dict]:
    """Local runs that are not on the platform yet.

    Without `dashboard_id` — across the whole mirror: the report page asks
    about its own, while the library shows the whole queue.
    """
    out: list[dict] = []
    ids: list[str]
    if dashboard_id is not None:
        ids = [dashboard_id] if layout.is_valid_dashboard_id(dashboard_id) else []
    else:
        try:
            ids = sorted(p.name for p in layout.mirror_root().iterdir()
                         if p.is_dir() and layout.is_valid_dashboard_id(p.name))
        except OSError:
            ids = []
    for did in ids:
        state = store.load_state(did)
        for rec in layout.unpublished_local_runs(state):
            out.append(_public_run(did, rec, with_input=with_input))
    return out


def read_input_manifest(dashboard_id: str, k: object) -> dict | None:
    """The run input saved next to the outputs. No file — None."""
    try:
        path = layout.local_run_dir(dashboard_id, k) / INPUT_MANIFEST_NAME
    except ValueError:
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def read_observe(dashboard_id: str, k: object) -> list[dict]:
    """The run's `observe` frames: `[{"t":"observe","files":[…]}, …]`.

    An empty list and "no file" are deliberately indistinguishable to the
    caller: a generator run that opened nothing (all from cache) and a run
    whose observation file could not be read equally have nothing to say about
    lineage — and it must not be invented.
    """
    try:
        path = layout.local_run_dir(dashboard_id, k) / OBSERVE_NAME
    except ValueError:
        return []
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(obj, list):
        return []
    return [f for f in obj if isinstance(f, dict) and f.get("t") == "observe"]


def output_path(dashboard_id: str, k: object, name: str) -> Path | None:
    """A run output file by the name the generator gave, or None.

    The name is checked against the run's INVENTORY, not glued onto a
    directory: the string arrives over the network (the page, on catch-up), and
    `..` in it would mean reading other people's files over loopback. So the
    path is built by `layout.slot_disk_path`, and the name must match the
    recorded one.
    """
    state = store.load_state(dashboard_id)
    rec = layout.local_run(state, k)
    if rec is None or not isinstance(name, str) or not name:
        return None
    try:
        vdir = layout.local_run_dir(dashboard_id, k)
    except ValueError:
        return None
    if name == rec.get("html"):
        path = vdir / layout.DOCUMENT_NAME
        return path if path.is_file() else None
    for slot in rec.get("slots") or []:
        if isinstance(slot, dict) and slot.get("name") == name:
            try:
                path = layout.slot_disk_path(vdir, slot)
            except ValueError:
                return None
            return path if path.is_file() else None
    return None


def mark_published(dashboard_id: str, k: object, at: str | None = None) -> bool:
    """Mark a run as published. False — there is no such run.

    This is called by the PAGE, and only after a successful `publish`: marking
    earlier means losing a run that never reached the platform.
    """
    state = store.load_state(dashboard_id)
    rec = layout.mark_local_published(state, k, at or _now_iso())
    if rec is None:
        return False
    store.save_state(dashboard_id, state)
    return True
