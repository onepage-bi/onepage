"""Run a native generator as a subprocess - TWO runners for two conventions.

The agent does NOT import the generator into its own process on either path:
isolation (a pandas crash != the daemon dying), clean cancel (terminate), zero
edits to the generator.

There are two conventions, and the previous edition of this header claimed the
opposite ("one convention for the browser and the agent") - that was untrue and
cost a lot:

  * `run_r2_subprocess` - the **R2 contract**, the very one described in
    `docs/GENERATOR_AUTHORING.md` section 5 and executed by the browser
    (`static/js/gen-worker.js`): `begin/add_file/finish` through the host
    `agent/r2host.py`. This is the MAIN path for generators that read folders, and
    the only one that gives the same numbers as the browser (verified by the gate
    `tools/engine_parity.py`).
  * `run_generator_subprocess` — **legacy-CLI** `<python> -m <module>
    --input-dir IN --output-dir OUT --schema-version SHA`. Described nowhere, not
    checked in `genmanifest.validate()`, implemented only by slreport, and kept
    exactly for the GreenPlum path, where `gpsource` puts the input files into
    inbox. It is not promised to new generators.

stdout contract:
  * progress lines:  `[stage] done/total msg`  (one per line, flush)
  * the LAST line:  a JSON result `{"html","slots","write_back"}`

The runner parses progress into NDJSON events and returns the result dict. The
password never gets here: the GP part has already finished, the generator only
sees csv.gz in inbox.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

_PROGRESS_RE = re.compile(r"^\[(\w+)\]\s+(\d+)/(\d+)\s*(.*)$")
_TERM_GRACE_S = 5.0


class GeneratorError(Exception):
    def __init__(self, kind: str, message: str, detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail


def run_generator_subprocess(
    module: str, app_dir: Path, input_dir: Path, output_dir: Path,
    schema_version: str, emit: Callable[[dict], None], cancel: Callable[[], bool],
    python_exe: str | None = None, *, cache_dir: Path | None = None,
    params_json: str | None = None,
) -> dict:
    """Run the generator and return its result dict. RunCanceled semantics
    are expressed via GeneratorError(kind='canceled'); other failures use the taxonomy
    gen_crash / gen_no_result / gen_bad_result."""
    output_dir.mkdir(parents=True, exist_ok=True)
    python_exe = python_exe or sys.executable

    argv = [python_exe, "-m", module,
            "--input-dir", str(input_dir),
            "--output-dir", str(output_dir),
            "--schema-version", schema_version]
    if cache_dir is not None:
        argv += ["--cache-dir", str(cache_dir)]
    if params_json is not None:
        argv += ["--params-json", params_json]

    # The bundle on PYTHONPATH -> `-m module` finds the unpacked code. Its own process,
    # its own cwd = app_dir (the generator's relative resources are read).
    env = dict(os.environ)
    env["PYTHONPATH"] = str(app_dir) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"

    emit({"t": "stage", "stage": "generate"})
    try:
        proc = subprocess.Popen(
            argv, cwd=str(app_dir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
    except OSError as e:
        raise GeneratorError("gen_crash", f"generator failed to start: {e}")

    # Active cancel: watches cancel() and terminates the process (it will send no more
    # stdout lines, so the main loop will see EOF).
    stop = threading.Event()
    canceled = threading.Event()

    def watch() -> None:
        while not stop.wait(0.1):
            if cancel():
                canceled.set()
                proc.terminate()
                try:
                    proc.wait(timeout=_TERM_GRACE_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return

    watcher = threading.Thread(target=watch, name="gen-cancel", daemon=True)
    watcher.start()

    last_json: dict | None = None
    try:
        for raw in proc.stdout:                      # line by line, as output arrives
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            m = _PROGRESS_RE.match(line)
            if m:
                stage, done, total, msg = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
                emit({"t": "progress", "stage": stage, "done": done,
                      "total": total, "msg": msg})
                continue
            # not progress -> a possible JSON result (the last one wins)
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    last_json = obj
            except ValueError:
                pass                                  # ignore other noise in stdout
    finally:
        stop.set()
        try:
            proc.stdout.close()
        except OSError:
            pass
        stderr = ""
        try:
            stderr = proc.stderr.read() or ""
        except OSError:
            pass
        code = proc.wait()

    if canceled.is_set():
        raise GeneratorError("canceled", "Canceled by user")
    if code != 0:
        raise GeneratorError("gen_crash",
                             f"generator exited with code {code}",
                             stderr[-500:])
    if last_json is None:
        raise GeneratorError("gen_no_result",
                             "generator did not return a JSON result as the last line",
                             stderr[-500:])
    if "slots" not in last_json and "html" not in last_json:
        raise GeneratorError("gen_bad_result",
                             "generator result lacks the expected keys html/slots",
                             json.dumps(last_json)[:300])
    return last_json


# --------------------------------------------------------------------------- #
# R2 contract: the host in a separate process                                  #
# --------------------------------------------------------------------------- #

# The host file name. One for the whole project: the kit builder also knows it
# (`HOST_FILES` in deploy/build_agent_single.py) - if the two diverge, the kit goes
# out with a file nobody looks for.
R2HOST_NAME = "r2host.py"


def _r2_host_candidates() -> list[Path]:
    """Where `r2host.py` may lie, in order of decreasing likelihood.

    Two cases, and neither is derived from the other:

      * **development and tests** - next to THIS module (`agent/r2host.py`);
      * **single-file kit** - there is no module at all, only the `op_agent.py`
        bundle, and the host lies as a SEPARATE file next to it. It is run as a script, not
        imported, so it cannot get into the bundle by construction
        (`deploy/build_agent_single.py` checks this and puts the file into the kit).

    In the kit `__file__` is `op_agent.py` itself, and the first candidate usually
    works. "Usually" is not "always": `op-agent.bat`/a shortcut may call the agent
    through a symlink or a copy, and then `resolve()` leads to another
    directory. So the directory of the running script is also a candidate
    (`sys.argv[0]`) - exactly the same trick `mirror/assets.py::_roots` uses to
    find its files, rather than a second way to answer the same question.
    """
    out: list[Path] = [Path(__file__).resolve().parent / R2HOST_NAME]
    try:
        out.append(Path(sys.argv[0]).resolve().parent / R2HOST_NAME)
    except (OSError, IndexError, ValueError):   # pragma: no cover
        pass
    seen: set[str] = set()
    uniq = []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


def _r2_host_path() -> Path:
    """Path to `r2host.py`. If it is under no root - return the first candidate.

    The first, not None: the caller refuses with `gen_no_host` and PRINTS the path
    it looked at - "the file is not here" is clearer to a person than "it is missing".
    """
    cands = _r2_host_candidates()
    for p in cands:
        try:
            if p.is_file():
                return p
        except OSError:                         # pragma: no cover
            continue
    return cands[0]


def run_r2_subprocess(
    module: str, app_dir: Path, out_dir: Path, files: list,
    schema_version: str, emit: Callable[[dict], None], cancel: Callable[[], bool],
    python_exe: str | None = None, *, cache_dir: Path | None = None,
) -> dict:
    """Run the generator under the R2 contract. Return
    `{"result": {...}, "dispositions": [{"name","kind"}, ...]}`.

    `files` is a list of dicts `{name, mtime_ms, size, path}` (optionally also
    `dir` and `matched_input` - for lineage) IN THE ORDER in which they must be
    given to the generator; `path` points to the analyst's REAL file, and the
    host does not touch it (see the header of `r2host.py`).

    The host's `observe` frames go outward through `emit` as run events - the
    page forwards them to `POST …/runs/{id}/observations` with the same body
    that the browser engine sends.

    Cancel is two-stage: first an `abort` frame and a pause - the generator can
    close the workbook and exit itself; if a single `add_file` hangs longer than the pause (a
    2 GB workbook checks nothing), the process is taken down by `terminate` -> `kill`.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    python_exe = python_exe or sys.executable
    host = _r2_host_path()
    if not host.is_file():
        # List ALL the places we looked: in the kit this usually means the zip
        # was not fully extracted (`r2host.py` next to `op_agent.py` is mandatory).
        where = ", ".join(str(p) for p in _r2_host_candidates())
        raise GeneratorError("gen_no_host",
                             f"no R2 contract host ({R2HOST_NAME}): looked in {where}")
    if cache_dir is None:
        cache_dir = out_dir.parent / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # `-I` is mandatory and not accidental: the host puts app_dir on sys.path itself
    # precisely because in isolated mode neither PYTHONPATH nor the
    # script directory gets there. Without `-I` the agent directory would come first on the path,
    # and a same-named agent module would shadow the bundle module.
    argv = [python_exe, "-I", str(host),
            "--app-dir", str(app_dir),
            "--out-dir", str(out_dir),
            "--cache-dir", str(cache_dir),
            "--schema-version", schema_version,
            "--module", module]

    proc = subprocess.Popen(
        argv, cwd=str(app_dir),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    # stderr is read by a SEPARATE thread, not at the end. The previous runner collected
    # it after the process finished - a chatty generator (and pandas and calamine
    # are chatty) filled the pipe buffer and stalled dead, waiting for it to be
    # read. We keep a tail for diagnostics, not the whole stream.
    err_tail: list[str] = []

    def _drain_err():
        try:
            for line in proc.stderr:
                err_tail.append(line.rstrip("\n"))
                if len(err_tail) > 200:
                    del err_tail[:100]
        except Exception:
            pass

    t_err = threading.Thread(target=_drain_err, name="op-r2-stderr", daemon=True)
    t_err.start()

    dispositions: list[dict] = []
    result: dict | None = None
    err_frame: dict | None = None

    def _send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def _read_frames(stop_on: tuple):
        """Read frames until one of `stop_on` arrives. Return it."""
        nonlocal result, err_frame
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                fr = json.loads(line)
            except ValueError:
                # A stray line in the protocol is a broken host, not a
                # reason to quietly continue: report numbers travel in the frames.
                raise GeneratorError("gen_bad_result",
                                     "R2 host sent a non-JSON line",
                                     line[:300])
            t = fr.get("t")
            if t == "progress":
                emit({"t": "progress", "stage": fr.get("stage", "generate"),
                      "done": fr.get("done"), "total": fr.get("total"),
                      "msg": fr.get("msg", "")})
            elif t == "disposition":
                dispositions.append({"name": fr.get("name", ""),
                                     "kind": fr.get("kind", "")})
            elif t == "observe":
                # Lineage: the frame goes OUT as is. The host took it with the same
                # probe (`static/py/_lineage.py`) as the browser worker, and the
                # page puts the content into the same
                # `POST …/runs/{id}/observations`. There is nothing to parse or "improve"
                # here and no reason to: any transformation on the way
                # is a second form of the same observations.
                ev = {"t": "observe", "files": fr.get("files") or []}
                if fr.get("stages"):
                    ev["stages"] = fr["stages"]
                if fr.get("budget"):
                    ev["budget"] = True
                emit(ev)
            elif t == "warning":
                emit({"t": "warning", "kind": fr.get("kind", "r2"),
                      "msg": fr.get("msg", "")})
            elif t == "result":
                result = fr.get("result") or {}
            elif t == "error":
                err_frame = fr
            if t in stop_on:
                return fr
        return None

    try:
        first = _read_frames(("ready", "error"))
        if first is None or first.get("t") != "ready":
            kind = (err_frame or {}).get("kind", "gen_crash")
            raise GeneratorError(kind,
                                 (err_frame or {}).get("message", "R2 host failed to start"),
                                 (err_frame or {}).get("detail", "\n".join(err_tail[-20:])))

        total = len(files)
        for i, f in enumerate(files):
            if cancel():
                _abort(proc, _send)
                raise GeneratorError("canceled", "Canceled by user")
            cmd = {"op": "add_file", "name": f["name"], "mtime_ms": f["mtime_ms"],
                   "size": f["size"], "path": f["path"]}
            # The folder number and the recognised `inputs` template are computed by whoever
            # listed the folders (`runs._resolve_folder_files` on top of
            # `folders.matches`). We only pass them to the host: it has neither
            # the agent modules (`-I`) nor the right to introduce a second answer
            # to the question "which template recognised this file".
            if f.get("dir") is not None:
                cmd["dir"] = f["dir"]
            if f.get("matched_input"):
                cmd["matched_input"] = f["matched_input"]
            _send(cmd)
            fr = _read_frames(("disposition", "error"))
            if fr is None or fr.get("t") == "error":
                kind = (err_frame or {}).get("kind", "gen_crash")
                raise GeneratorError(kind,
                                     (err_frame or {}).get("message", "R2 host broke off"),
                                     (err_frame or {}).get("detail", "\n".join(err_tail[-20:])))
            emit({"t": "progress", "stage": "files", "done": i + 1, "total": total,
                  "msg": f["name"]})

        _send({"op": "finish"})
        fr = _read_frames(("result", "error"))
        if fr is None or fr.get("t") == "error" or result is None:
            kind = (err_frame or {}).get("kind", "gen_no_result")
            raise GeneratorError(kind,
                                 (err_frame or {}).get("message", "generator did not return a result"),
                                 (err_frame or {}).get("detail", "\n".join(err_tail[-20:])))
        return {"result": result, "dispositions": dispositions}
    finally:
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        _reap(proc)
        t_err.join(timeout=1.0)


def _abort(proc, send) -> None:
    """Ask the host to finish gracefully, without waiting forever."""
    try:
        send({"op": "abort"})
    except Exception:
        pass
    try:
        proc.wait(timeout=_TERM_GRACE_S)
    except Exception:
        pass


def _reap(proc) -> None:
    """Kill the process if it is still alive. Called in finally on every path."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=_TERM_GRACE_S)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
