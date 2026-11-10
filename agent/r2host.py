"""Native host of the R2 contract: the generator in a SEPARATE process under plain CPython.

In the browser the engine (`static/js/gen-worker.js`, steps 5-7) keeps the generator
session right in Pyodide. On the analyst's laptop this file plays Pyodide's role: it
imports the generator module, calls `begin()`, feeds files to `add_file()` and
hands back `finish()`. The semantics are repeated from the worker LITERALLY - the two
environments must give the same numbers, and the easiest way to breed a divergence is
to "improve" one of the sides.

Launch (the caller sets cwd=app_dir):

    <python> -I agent/r2host.py --app-dir DIR --out-dir DIR --cache-dir DIR \
             --schema-version SHA --module slreport.engine

Protocol - line-delimited JSON in both directions, one line = one frame.

    in:    {"op":"add_file","name":..,"mtime_ms":..,"size":..,"path":..,
            "dir":..,"matched_input":..}
           {"op":"finish"}  {"op":"abort"}  {"op":"ping"}
    out:   {"t":"ready"}
           {"t":"progress","stage":..,"done":..,"total":..,"msg":..}
           {"t":"disposition","kind":"parsed"|"cached"|"service","name":..}
           {"t":"observe","files":[{dir,name,kind,path,schema,consumed,
                                    checks,profiles}],"stages":[..],"budget":..}
           {"t":"warning","kind":..,"msg":..}
           {"t":"result","result":{"html":..,"slots":{..},"write_back":{..}}}
           {"t":"error","kind":..,"message":..,"detail":..}

`begin()` is called BEFORE reading the first command: a crash on importing the generator
must arrive as an `error` frame, not as a dead pipe into which the caller writes add_file.

Six decisions explain why the file is written this way - they are in the comments at
`_Frames`, `_add_file`, `_stat_file`, `_arm_cache`, `_check_deps` and in the
"Lineage" section. In short: stdout is taken by the generator's own printing; `path` is
the analyst's REAL file; the input set is not frozen; an unfinished cache silently
poisons the next run; the kit contents are pinned by the Pyodide lock, and a missing
package must be named at start, before the run touches the cache; provenance is
taken by THE SAME probe `static/py/_lineage.py` and in the same order as the browser.

Standard library only and top-level imports only: the file travels in the
offline kit, and the `deploy/build_agent_single.py` builder strips relative
imports only at the top level.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

# The "run in progress" marker inside cache_dir. The name starts with a dot so it is not
# confused with an entry of the generator's own cache, should that ever start putting
# files flat.
RUNNING_MARKER = ".r2host-running"

# Length of the traceback tail in the error frame: the caller puts it in the log rather
# than showing it to the person in full.
MAX_DETAIL_CHARS = 8000


class R2Error(Exception):
    """A failure with a classification: kind goes into the error frame as is."""

    def __init__(self, kind: str, message: str, detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail


class _Frames:
    """The only writer of protocol frames.

    DECISION 1 - stdout is taken. The generators' contract historically allows printing
    to stdout, and pandas with python-calamine pour warnings there on their own,
    unasked. If frames were written to the same fd 1, the first such line would break
    the caller's parsing. So at start fd 1 is DUPLICATED (`os.dup`), frames
    go only to this duplicate, and the real fd 1 is redirected to stderr:
      * `os.dup2(2, 1)` also covers the C level (pandas calls printf from C code,
        replacing `sys.stdout` does not catch it);
      * `sys.stdout = sys.stderr` - so references to the object already captured by
        someone also lead to stderr.
    Passing a separate fd via `pass_fds` is impossible: Windows has no such thing, and the kit
    goes exactly there.
    """

    def __init__(self) -> None:
        fd = os.dup(1)
        try:
            os.dup2(2, 1)
        except OSError:
            # No stderr (a service without a console) - then at least swap the object;
            # frames go to the duplicate anyway and will not suffer.
            pass
        sys.stdout = sys.stderr
        self._out = os.fdopen(fd, "w", encoding="utf-8", newline="\n")

    def emit(self, frame: dict) -> None:
        # flush after every line: the caller reads synchronously and waits for a reply,
        # an 8 KB buffer would turn progress into silence until the end of the run.
        self._out.write(json.dumps(frame, ensure_ascii=False) + "\n")
        self._out.flush()

    def warn(self, kind: str, msg: str) -> None:
        self.emit({"t": "warning", "kind": kind, "msg": msg})


def _short(text: str) -> str:
    return text if len(text) <= MAX_DETAIL_CHARS else text[-MAX_DETAIL_CHARS:]


def _stat_file(path: str) -> tuple[int, int]:
    """Fresh (mtime_ms, size) of the real file.

    DECISION 3 - the input set is not frozen. The files lie in the analyst's folder: OneDrive
    re-syncs them, the person saves a workbook in the middle of a run. What
    the caller sent it measured while walking the folder - on a long run that
    was minutes ago. The generator's cache key is computed from mtime+size, so
    a stale pair = a cache entry with someone else's content.
    """
    st = os.stat(path)
    return int(st.st_mtime * 1000), int(st.st_size)


def _arm_cache(cache_dir: Path, frames: _Frames) -> Path:
    """Set the RUNNING marker, wiping the poisoned cache of the previous run.

    DECISION 4 - the cache can be poisoned. A run killed halfway (Ctrl-C, OOM,
    a pulled-out laptop) leaves an UNFINISHED entry. Its key (schema|name|
    mtime|size) will match on the next run, the generator will read it as
    `cached` and SILENTLY hand out a wrong report - the worst possible outcome,
    because there is nothing to notice. So the marker is set before the work and removed
    ONLY after a successful finish(): a found marker means "last time we did not
    reach the end", and the whole directory is wiped. Repairing selectively
    is pointless - which entry is unfinished is not visible from outside.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    marker = cache_dir / RUNNING_MARKER
    if marker.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        frames.warn(
            "cache_reset",
            "The previous run did not finish - the cache in "
            f"{cache_dir} was cleared entirely so that an unfinished entry is not "
            "read as a finished one.",
        )
    marker.write_text("running\n", encoding="utf-8")
    return marker


def _check_deps(app_dir: Path, frames: _Frames) -> None:
    """Refuse at START if the kit does not carry a package declared by the manifest.

    DECISION 5 - the kit contents are not fitted to the manifest. The offline kit's wheels
    are pinned by the Pyodide lock (`deploy/agent_tools/lock_specs.py`), not by the bundle's
    `deps`: for distribution packages the browser ignores the manifest pin and installs
    the distribution version, so building "by the manifest" would plant a divergence
    of numbers between the browser and the native run. Consequence: a package that is not
    in the lock is not in the kit either.

    The check stands BEFORE `_arm_cache`, and this is not cosmetics: `_arm_cache` sets
    the run marker and at the same time wipes the previous cache. A refusal after it would
    cost the person a full recompute over a typo in the manifest. So the order
    is: first say what is missing and exit without touching anything.

    A version mismatch is NOT a refusal but a warning: the browser behaves exactly the same
    (`genmanifest.resolve_deps` -> `wasm32_note: pyodide dist ships X`),
    and a ban here would mean the engines diverge in strictness.
    """
    manifest_path = app_dir / "manifest.json"
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError:
        # No manifest next to it - nothing to check. Silence is not allowed: a run without a
        # manifest happens only in development, and the frames should say so.
        frames.warn("no_manifest",
                    f"{manifest_path} was not read - the dependency set was not checked.")
        return
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise R2Error("gen_bad_result", f"manifest.json cannot be parsed: {exc}") from exc

    deps = manifest.get("deps") or []
    if not isinstance(deps, list):
        raise R2Error("gen_bad_result", "manifest.json: deps is not a list.")

    missing: list[str] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name") or "").strip()
        if not name:
            continue
        want = str(dep.get("version") or "").strip()
        try:
            have = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(f"{name}=={want}" if want else name)
            continue
        if want and have != want:
            frames.warn(
                "dep_version",
                f"{name}: the manifest pins {want}, {have} is installed - the version "
                "is taken from the Pyodide lock so that the native run computes on the same "
                "thing as the browser.")

    if missing:
        raise R2Error(
            "gen_missing_dep",
            "the offline kit does not carry packages declared by the manifest: "
            + ", ".join(missing),
            "The kit contents are pinned by the Pyodide lock "
            "(app/services/data/pyodide_lock_314.json, see "
            "deploy/agent_tools/lock_specs.py). A package that is not in the lock is "
            "not in the kit and cannot be: otherwise the native run would compute not on "
            "the same thing the browser computes on.")


def _import_generator(module: str, app_dir: Path):
    """Import the generator module and make sure it speaks R2."""
    # We start with `-I`: it implies `-E`, i.e. PYTHONPATH is IGNORED, and the
    # script directory does not get into sys.path either. So we put the bundle on the path
    # ourselves - that is why `--app-dir` exists as a separate argument. At the same time
    # isolation stays honest: apart from app_dir, nothing is imported from the
    # analyst's folders.
    app = str(app_dir)
    if app not in sys.path:
        sys.path.insert(0, app)
    try:
        mod = importlib.import_module(module)
    except Exception as exc:
        raise R2Error("gen_crash", f"Generator module {module!r} cannot be imported: {exc}",
                      _short(traceback.format_exc())) from exc
    if not hasattr(mod, "begin"):
        # NO fallback to the old CLI convention: silently running the generator
        # another way means getting another cache, other warnings and, in the
        # worst case, other numbers - exactly what this work proves
        # impossible.
        raise R2Error(
            "gen_no_r2",
            f"Module {module!r} does not implement the R2 engine contract: it has no "
            "begin(out_dir, cache_dir, schema_version, progress). The generator "
            "must be updated to R2 (docs/GENERATOR_AUTHORING.md section 5) - "
            "this host will not run it any other way.",
        )
    return mod


def _make_progress(frames: _Frames):
    """progress(stage, done, total, msg) -> a progress frame.

    The default values are not there out of politeness: in the browser this is a JS function, and
    a generator calling it with three arguments passes there silently. Native must not
    fail on this - the environments' divergence matters more than signature strictness.
    """

    def progress(stage: str = "", done: int = 0, total: int = 0, msg: str = "") -> None:
        frames.emit({"t": "progress", "stage": str(stage),
                     "done": int(done or 0), "total": int(total or 0),
                     "msg": "" if msg is None else str(msg)})

    return progress


# ==========================================================================
#  Lineage: the same probe as in the browser (static/py/_lineage.py)
# ==========================================================================
#
# DECISION 6 - provenance is collected by THE SAME probe and in THE SAME order as in the
# browser. The worker (`static/js/gen-worker.js`, step "4b. Lineage probe" and
# further along the file loop) does exactly this for each file:
#
#     sniff(path) → run_checks(...) → note_file(name) → add_file() → drain()
#
# and no step may be reordered:
#   * `install_hooks()` must run BEFORE importing the generator module -
#     otherwise its `from python_calamine import CalamineWorkbook` binds to the
#     UNwrapped class, and the run will not see a single read;
#   * `sniff`/`run_checks` must stand BEFORE `add_file` - after it the file has already been
#     read (and in the browser also deleted), and reading it a second time means
#     measuring a different state;
#   * `note_file` - before `add_file`, otherwise the file will not get into the nearest frame
#     `onepage.stage()`;
#   * `drain` - right after `add_file`, otherwise what was read is recorded for the NEXT
#     file: `_consumed` in the probe is shared, records are separated only by the moment of taking.
#
# The `onepage` module (the one whose `stage()` the generator calls) need not be
# supplied separately, and MUST NOT be: `install_hooks()` itself publishes it
# (`_lineage._install_onepage` - into `sys.modules` and `builtins`). A second
# `onepage` built here would diverge from the browser's in its composition.
#
# The probe never costs a run: any failure of it is a warning and an empty
# schema WITH A REASON, but not a refusal. An empty schema WITHOUT a reason is exactly what
# the browser separately moved away from: "not observed" and "observed and failed"
# must be distinguishable on the lineage screen, otherwise seven files remain without
# explanation and without a schema.
LINEAGE_NAME = "_lineage.py"

# The numbers are the same as in gen-worker.js (OBSERVE_BATCH_SIZE, OBSERVE_BUDGET_BYTES,
# CHECK_BUDGET_MS, MAX_STAGES, MAX_REASON_CHARS). They must match: the batch is
# accepted by one and the same server (`_MAX_OBS_BODY` in app/api/generators.py), and
# an engine that counts the budget its own way will one day send a body that is thrown away
# whole - together with `final:true`.
OBSERVE_BATCH = 25
OBSERVE_BUDGET_BYTES = 2 * 1024 * 1024
CHECK_BUDGET_MS = 20_000.0
MAX_STAGES = 32
MAX_REASON_CHARS = 200

# A mirror of genmanifest.CHECK_TYPES. The list is closed: a type that is not here
# will not reach run_checks, and that is more correct than handing the probe a spec
# the server does not recognise.
CHECK_TYPES = ("not_null", "unique", "range", "regex", "enum", "known_values",
               "ref_lookup")


def _json_bytes(obj) -> int:
    """Size in UTF-8 BYTES, not in characters.

    The worker counts the same way (`byteLength`, not `String.length`) and for the same
    reason: the server budget is in real bytes, and a non-ASCII (e.g. Cyrillic) sheet name
    costs twice as much there as it seems.
    """
    try:
        return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _reason(exc: BaseException) -> str:
    """A short reason for the schema: the last line of the text, like `shortMsg`."""
    text = str(exc) or exc.__class__.__name__
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return (lines[-1] if lines else text)[:MAX_REASON_CHARS]


def _canon_checks(manifest: dict) -> list:
    """The manifest's `checks` in canonical form - a port of genmanifest.checks_for.

    A port, not a free-form parse, because of one field: `id` =
    "type:match:sheet:column" is the KEY by which the server joins Data Quality
    findings across runs. If it diverged from the browser's, one report
    computed there and here would start two independent histories of one check.
    The comparison with the original lives in agent/tests/smoke_r2host.py: when the
    repository is at hand, the output is compared with genmanifest.checks_for itself.

    What is not here and will not be: `keys` for `ref_lookup`. They are put in by the
    PAGE from the current version of the platform reference, the agent has nowhere to
    get them, and run_checks will honestly mark such a check skipped "no_keys" -
    that is better than computing it over an empty dictionary and declaring everything
    unmatched.
    """
    out = []
    for chk in (manifest.get("checks") or []):
        if not isinstance(chk, dict):
            continue
        match, column, ctype = chk.get("match"), chk.get("column"), chk.get("type")
        if not (isinstance(match, str) and isinstance(column, str)
                and ctype in CHECK_TYPES):
            continue
        sheet = chk.get("sheet").strip() if isinstance(chk.get("sheet"), str) else "*"
        sheet = sheet or "*"
        column = column.strip()
        out.append({
            "id": f"{ctype}:{match}:{sheet}:{column}",
            "type": ctype,
            "match": match,
            "sheet": sheet,
            "column": column,
            "min": chk.get("min"),
            "max": chk.get("max"),
            "pattern": chk.get("pattern"),
            "values": list(chk["values"]) if isinstance(chk.get("values"), list) else None,
            "on_fail": chk.get("on_fail") or "warn",
            "label": chk.get("label"),
            "table": chk["table"] if ctype == "ref_lookup" and isinstance(chk.get("table"), str) else None,
        })
    return out


def _checks_for(checks: list, matched_input) -> list:
    """Checks declared for exactly this file (gen-worker.js::checksFor).

    A file that no `inputs` template recognised gets no checks -
    exactly as in the browser, where `matched_input` is not set for such a file.
    """
    if not matched_input:
        return []
    return [c for c in checks if c.get("match") == matched_input]


def _declared_for(inputs: list, matched_input) -> dict:
    """{sheet mask: [column, ...]} for this file (gen-worker.js::declaredFor)."""
    declared: dict = {}
    if not matched_input:
        return declared
    inp = None
    for i in inputs:
        if isinstance(i, dict) and i.get("match") == matched_input:
            inp = i
            break
    if inp is None:
        return declared
    for sh in (inp.get("sheets") or []):
        if not isinstance(sh, dict):
            continue
        cols = sh.get("columns")
        if not isinstance(cols, list) or not cols:
            continue
        declared[sh.get("name") or "*"] = list(cols)
    return declared


def _manifest_dict(app_dir: Path) -> dict:
    """The bundle's manifest.json or {}.

    Silently: `_check_deps` has already reported an unreadable manifest - it runs earlier and
    on the same file. A second warning about one and the same event teaches the
    person not to read warnings.
    """
    try:
        data = json.loads((app_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _lineage_candidates() -> list:
    """Where `_lineage.py` may lie, in order of decreasing likelihood.

    The same trick and the same order as `generator._r2_host_candidates()` and
    `mirror/assets._roots()`: next to this file (in the kit - the kit root, where
    `deploy/build_agent_single.py` puts it), then the original in the repository,
    then the directory of the running script (a shortcut/copy leads `__file__` astray).
    """
    here = Path(__file__).resolve().parent
    out = [here / LINEAGE_NAME, here.parent / "static" / "py" / LINEAGE_NAME]
    try:
        out.append(Path(sys.argv[0]).resolve().parent / LINEAGE_NAME)
    except (OSError, IndexError, ValueError):   # pragma: no cover
        pass
    seen: set = set()
    uniq = []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


def _load_lineage(frames: _Frames):
    """Load the probe and install the hooks. None - a run without observations.

    We load by PATH, not via sys.path: otherwise the kit directory would lie on the generator's
    import path, and a same-named agent module would shadow the bundle module
    - exactly what the host is launched with `-I` to avoid. It is still put into `sys.modules`
    under the name `_lineage`, and before `exec_module`: `_TrackedFrame`
    is declared in the probe at top level precisely so that the generator's frame
    can be pickled into the aggregate cache, and pickle looks up the class by its module name.
    """
    import importlib.util

    cands = _lineage_candidates()
    for p in cands:
        try:
            if not p.is_file():
                continue
        except OSError:                          # pragma: no cover
            continue
        try:
            spec = importlib.util.spec_from_file_location("_lineage", str(p))
            mod = importlib.util.module_from_spec(spec)
            sys.modules["_lineage"] = mod
            spec.loader.exec_module(mod)
            mod.install_hooks()
            return mod
        except Exception as exc:
            sys.modules.pop("_lineage", None)
            frames.warn("lineage_unavailable",
                        f"the lineage probe {p} failed to start ({exc}) - the run "
                        "goes on, but this report's provenance will stay empty.")
            return None
    frames.warn("lineage_unavailable",
                f"no {LINEAGE_NAME} (looked in: "
                + ", ".join(str(p) for p in cands)
                + ") - the run goes on, but this report's provenance will stay "
                  "empty. In the kit the file is put next to op_agent.py.")
    return None


class _Observer:
    """Observations of one run: batches of `observe` frames.

    Lives only when the probe came up - hence the methods have not a single
    "is there lineage at all" check: whoever creates the object does it.
    """

    def __init__(self, frames: _Frames, lineage, manifest: dict):
        self._frames = frames
        self._lin = lineage
        self._inputs = [i for i in (manifest.get("inputs") or []) if isinstance(i, dict)]
        self._checks = _canon_checks(manifest)
        self._batch: list = []
        self._bytes = 0            # observation budget spent, in bytes
        self._budget = False       # schemas are no longer read
        self._batch_budget = False # something in this batch was cut by the budget
        self._check_ms = 0.0
        self._check_budget = False
        self._sniff_warned = False
        self._check_warned = False

    # ── budgets ───────────────────────────────────────────────────────
    def _spend(self, obj) -> None:
        self._bytes += _json_bytes(obj)
        if self._bytes > OBSERVE_BUDGET_BYTES:
            self._budget = True

    def _warn_sniff(self, exc: BaseException) -> None:
        """One warning per run, as in the worker.

        A broken workbook must not turn the log into a wall of identical lines:
        the file count is visible from the frames anyway, and the cause is usually one.
        """
        if not self._sniff_warned:
            self._sniff_warned = True
            self._frames.warn(
                "lineage_sniff",
                "The lineage probe fails on some files - continuing without their "
                f"schemas: {_reason(exc)}")

    def _warn_checks(self, exc: BaseException) -> None:
        if not self._check_warned:
            self._check_warned = True
            self._frames.warn(
                "lineage_checks",
                "Quality checks fail on some files - continuing without their "
                f"results: {_reason(exc)}")

    # ── per file ──────────────────────────────────────────────────────
    def before_add(self, name: str, path: str, matched_input) -> dict:
        """sniff -> run_checks -> note_file. Returns the observation record fields.

        A key in the dict means "we are sending this". The ABSENCE of a key means
        "there was nothing to do" (the file has no declared checks), and an explicit
        `None` means "there was something to do, but it failed or was not done". The server reads
        them differently, and the worker tells them apart in exactly the same way (`undefined`
        drops out of JSON, `null` does not).
        """
        pre: dict = {}

        if self._budget:
            pre["schema"] = {"sheets": [],
                             "error": "observe budget exceeded — schema not read"}
            self._batch_budget = True
        else:
            try:
                schema = self._lin.sniff(path)
            except Exception as exc:
                # sniff itself returns {"sheets":[],"error":...} for a workbook
                # it could not open. What lands here is the case "the probe fell apart
                # AROUND it" - and it must all the more arrive as a schema with a reason,
                # not look like "unchanged".
                schema = {"sheets": [], "error": _reason(exc)}
                self._warn_sniff(exc)
            pre["schema"] = schema
            self._spend(schema)

        specs = _checks_for(self._checks, matched_input)
        declared = _declared_for(self._inputs, matched_input)
        if specs or declared:
            if self._check_budget:
                pre["checks"] = None
                pre["profiles"] = None
                self._batch_budget = True
            else:
                t0 = time.perf_counter()
                try:
                    res = self._lin.run_checks(path, specs, declared)
                    pre["checks"] = res.get("results")
                    pre["profiles"] = res.get("profiles")
                    self._spend({"checks": pre["checks"], "profiles": pre["profiles"]})
                except Exception as exc:
                    pre["checks"] = None
                    pre["profiles"] = None
                    self._warn_checks(exc)
                self._check_ms += (time.perf_counter() - t0) * 1000.0
                if self._check_ms > CHECK_BUDGET_MS and not self._check_budget:
                    self._check_budget = True
                    self._frames.warn(
                        "checks_budget",
                        "Quality checks stopped: the 20 s per-run budget was exceeded - "
                        "the remaining files go without checks.")

        try:
            self._lin.note_file(name)
        except Exception:
            pass    # a trimmed probe without note_file: stages are a bonus, not the contract
        return pre

    def after_add(self, pre: dict, *, dir_index: int, name: str, kind: str,
                  path: str) -> None:
        """drain() and recording into the batch. Called RIGHT after add_file."""
        consumed = None
        try:
            drained = self._lin.drain()
            # The key is the path exactly as the generator opened it,
            # and we gave it exactly the string that came in the command. There is nothing to normalise
            # here: the probe does not invent its own spelling (`_touch` stores
            # `str(path)`), and substituting a "similar" path would mean attributing
            # someone else's reads to the file.
            if isinstance(drained, dict):
                consumed = drained.get(path)
        except Exception:
            consumed = None   # a broken drain() is no reason to scold for it
        entry = {"dir": dir_index, "name": name, "kind": kind, "path": path,
                 "consumed": consumed}
        entry.update(pre)
        self._batch.append(entry)
        if len(self._batch) >= OBSERVE_BATCH:
            self.flush()

    # ── stages and sending ────────────────────────────────────────────
    def _take_stages(self) -> list:
        """Snapshots of `onepage.stage()` into the remaining budget (takeStagesBudgeted)."""
        try:
            incoming = self._lin.drain_stages()
        except Exception:
            return []   # a probe without drain_stages - stages are a bonus, not the contract
        out: list = []
        left = OBSERVE_BUDGET_BYTES - self._bytes
        left = left if left > 0 else 0
        spent = 0
        for st in (incoming or []):
            if len(out) >= MAX_STAGES:
                break
            if not isinstance(st, dict):
                continue
            size = _json_bytes(st)
            if spent + size > left:
                # Profiles are the heaviest field of a stage; the same concession as on
                # the recovery path after a 413. If it did not fit even without them, none
                # will fit: later stages are never smaller.
                stripped = dict(st)
                stripped["profiles"] = []
                size = _json_bytes(stripped)
                if spent + size > left:
                    self._batch_budget = True
                    break
                st = stripped
            out.append(st)
            spent += size
        self._bytes += spent
        return out

    def flush(self, *, final: bool = False) -> None:
        """Send what has accumulated. `final` - also the run's stages.

        Stages travel ONLY in the last frame: they describe the run as a whole, not
        a file, and the server replaces their set entirely on every arrival.
        """
        files = self._batch
        self._batch = []
        stages = self._take_stages() if final else []
        if not files and not stages:
            return
        frame = {"t": "observe", "files": files}
        if stages:
            frame["stages"] = stages
        if self._batch_budget:
            frame["budget"] = True
            self._batch_budget = False
        self._frames.emit(frame)


def _flush_quiet(observer) -> None:
    """Flush observations before the `error` frame.

    The order matters: on an `error` frame the caller (`agent/generator.py::_read_frames`)
    stops reading, and whatever is sent after it is lost.
    A run that failed on the last file is exactly the case where
    the person most needs to know what managed to be read.
    """
    if observer is None:
        return
    try:
        observer.flush(final=True)
    except Exception:
        pass


def _add_file(session, cmd: dict, frames: _Frames, observer=None) -> None:
    """One add_file: fresh stat -> probe -> generator -> check -> frames.

    DECISION 2 - `path` points to the analyst's REAL file, not to a copy in a
    sandbox. In the browser add_file receives a copy inside the wasm FS, and the worker
    deletes it at once for `parsed`/`cached`, while `service` is kept until finish() -
    there it saves memory over a copy that is no pity to lose. HERE deleting,
    renaming or holding open ANYTHING is forbidden: we would destroy the person's working
    file. The disposition is therefore purely informational - we hand it out as a frame and
    forget. If the next reader decides "someone forgot to clean up here, like in the
    worker" and adds unlink - they will erase the analyst's source data.

    `matched_input` and `dir` come FROM THE CALLER and are not computed here:
    masks are compared by `agent/folders.py` (one port of `globmatch.py` per application),
    and the host runs with `-I` and does not see the agent modules. A second answer to the
    question "which template recognised this file" would mean lineage that
    contradicts what was computed.
    """
    name = str(cmd.get("name") or "")
    path = str(cmd.get("path") or "")
    if not name or not path:
        raise R2Error("gen_bad_result", "The add_file command has no name or path.")
    try:
        mtime_ms, size = _stat_file(path)
    except OSError as exc:
        raise R2Error("gen_crash", f"File {name} is unavailable: {exc}") from exc

    said_mtime = cmd.get("mtime_ms")
    said_size = cmd.get("size")
    if (said_mtime is not None and int(said_mtime) != mtime_ms) or \
       (said_size is not None and int(said_size) != size):
        frames.warn(
            "input_changed",
            f"{name}: the file changed after the folder walk "
            f"(was {said_mtime}/{said_size}, now {mtime_ms}/{size}) - "
            "the generator was given the fresh values.",
        )

    # The probe goes BEFORE add_file (see the "Lineage" section): after it the file has already been
    # read, and a schema taken afterwards would describe another moment.
    pre = None
    if observer is not None:
        matched = cmd.get("matched_input")
        matched = matched if isinstance(matched, str) and matched else None
        pre = observer.before_add(name, path, matched)

    try:
        kind = session.add_file(name, mtime_ms, size, path)
    except Exception as exc:
        raise R2Error("gen_crash", f"add_file({name}) failed: {exc}",
                      _short(traceback.format_exc())) from exc

    kind = str(kind or "")
    if kind not in ("parsed", "cached", "service"):
        # The worker treats everything except "service"/"cached" as "parsed"; we repeat
        # exactly that, but out loud - otherwise a typo by the generator author would diverge
        # between the environments unnoticed.
        frames.warn("unknown_disposition",
                    f"{name}: add_file returned {kind!r} - treating as “parsed”.")
        kind = "parsed"

    try:
        after = _stat_file(path)
    except OSError:
        after = None
    if after is not None and after != (mtime_ms, size):
        # The generator must read, not write: the analyst's source is not its
        # workspace. Silence is not allowed, the run goes on.
        frames.warn("input_modified",
                    f"{name}: the file changed DURING add_file "
                    f"({mtime_ms}/{size} → {after[0]}/{after[1]}).")

    if observer is not None and pre is not None:
        try:
            dir_index = int(cmd.get("dir") or 0)
        except (TypeError, ValueError):
            dir_index = 0
        observer.after_add(pre, dir_index=dir_index, name=name, kind=kind,
                           path=path)

    frames.emit({"t": "disposition", "kind": kind, "name": name})


def _out_name(out_dir: Path, value, where: str) -> str:
    """Check that the declared name is a file that really lies in out_dir."""
    name = str(value or "")
    if not name:
        raise R2Error("gen_bad_result", f"finish(): empty file name in {where}.")
    target = (out_dir / name).resolve()
    try:
        target.relative_to(out_dir.resolve())
    except ValueError:
        raise R2Error("gen_bad_result",
                      f"finish(): {where} points outside out_dir: {name!r}.") from None
    if not target.is_file():
        raise R2Error("gen_bad_result",
                      f"finish(): {where} declares file {name!r} that is not in out_dir.")
    return name


def _finish(session, out_dir: Path) -> dict:
    try:
        raw = session.finish()
    except Exception as exc:
        raise R2Error("gen_crash", f"finish() failed: {exc}",
                      _short(traceback.format_exc())) from exc
    if not isinstance(raw, dict):
        raise R2Error("gen_bad_result",
                      f"finish() returned {type(raw).__name__}, but the contract requires a dict "
                      '{"html", "slots", "write_back"}.')

    result: dict = {"html": None, "slots": {}, "write_back": {}}
    if raw.get("html") is not None:
        result["html"] = _out_name(out_dir, raw["html"], "html")
    for key in ("slots", "write_back"):
        block = raw.get(key) or {}
        if not isinstance(block, dict):
            raise R2Error("gen_bad_result", f"finish(): {key} is not a dict.")
        result[key] = {str(idx): _out_name(out_dir, nm, f"{key}[{idx}]")
                       for idx, nm in block.items()}
    return result


def _serve(session, out_dir: Path, frames: _Frames, observer=None) -> dict:
    """Read commands until finish/abort/EOF. Returns the result of finish()."""
    while True:
        line = sys.stdin.readline()
        if not line:
            # The pipe closed without waiting for finish - for the cache that is the same kind of
            # break as Ctrl-C, so the marker stays in place.
            raise R2Error("canceled", "The caller closed the channel before finish().")
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except ValueError as exc:
            raise R2Error("gen_bad_result", f"Unreadable command: {exc}") from exc
        op = str(cmd.get("op") or "")
        if op == "add_file":
            _add_file(session, cmd, frames, observer)
        elif op == "finish":
            return _finish(session, out_dir)
        elif op == "abort":
            raise R2Error("canceled", "The run was canceled by the caller.")
        elif op == "ping":
            # The protocol has no separate "pong" frame, and there is no need to invent one:
            # "ready" already means "the process is alive, the session is open".
            frames.emit({"t": "ready"})
        else:
            frames.warn("unknown_op", f"Unknown command {op!r} - skipped.")


def _parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="r2host", description="Native host of the R2 contract for a report generator.")
    ap.add_argument("--app-dir", required=True, help="the unpacked generator bundle")
    ap.add_argument("--out-dir", required=True, help="where the generator puts its result")
    ap.add_argument("--cache-dir", required=True, help="the generator's aggregate cache")
    ap.add_argument("--schema-version", required=True, help="checksum_sha256 of the bundle version")
    ap.add_argument("--module", required=True, help="entrypoint module, e.g. slreport.engine")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    # Bring up the frames FIRST: from this second on everything the generator prints
    # goes to stderr and cannot corrupt the protocol.
    frames = _Frames()
    marker = None
    observer = None
    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
        app_dir = Path(args.app_dir).resolve()
        out_dir = Path(args.out_dir).resolve()
        cache_dir = Path(args.cache_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        # Dependencies - BEFORE the marker: see the comment in _check_deps, a refusal
        # after _arm_cache would cost the person the cache of the previous run being wiped.
        _check_deps(app_dir, frames)
        marker = _arm_cache(cache_dir, frames)

        # Hooks - BEFORE importing the generator module, otherwise its own
        # `from python_calamine import CalamineWorkbook` binds to the
        # UNwrapped class and we will not see a single read.
        lineage = _load_lineage(frames)
        if lineage is not None:
            observer = _Observer(frames, lineage, _manifest_dict(app_dir))

        mod = _import_generator(args.module, app_dir)
        try:
            session = mod.begin(str(out_dir), str(cache_dir), args.schema_version,
                                _make_progress(frames))
        except Exception as exc:
            raise R2Error("gen_crash", f"begin() failed: {exc}",
                          _short(traceback.format_exc())) from exc
        if session is None or not hasattr(session, "add_file") or not hasattr(session, "finish"):
            raise R2Error("gen_no_r2",
                          "begin() returned an object without add_file/finish - this is not an R2 session.")

        frames.emit({"t": "ready"})
        result = _serve(session, out_dir, frames, observer)
        # The last observation frame - BEFORE `result`: on `result` the caller
        # stops reading frames, and the `onepage.stage()` stages are taken only
        # here, after finish().
        _flush_quiet(observer)
        frames.emit({"t": "result", "result": result})
        # A clean exit - and only here: the marker is removed after
        # finish() has written the cache out to the end.
        if marker is not None:
            marker.unlink(missing_ok=True)
        return 0
    except R2Error as exc:
        _flush_quiet(observer)
        frames.emit({"t": "error", "kind": exc.kind, "message": exc.message,
                     "detail": exc.detail})
        return 1
    except BaseException as exc:            # including KeyboardInterrupt
        _flush_quiet(observer)
        frames.emit({"t": "error", "kind": "gen_crash", "message": str(exc),
                     "detail": _short(traceback.format_exc())})
        return 1


if __name__ == "__main__":
    sys.exit(main())
