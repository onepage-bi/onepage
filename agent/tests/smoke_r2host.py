"""Smoke test for agent/r2host.py: the protocol of the native R2 engine host.

Run:  python -m agent.tests.smoke_r2host   (from the OnePage repo root)

The host is checked by a REAL subprocess launch, not by calling functions: exactly
half of its work is process behaviour (fd 1 is taken from the generator, frames are
line-delimited, the cache marker survives a crash), and that does not fit in one process.

The generator for the test is a dummy, written into a temporary directory. It
deliberately PRINTS GARBAGE TO stdout on every add_file: historically the generator
contract allows this, pandas and calamine do it on their own, and if protocol frames
went to the same fd, parsing would break on the very first file. So "the protocol
was parsed" here is at the same time a check of the stdout redirection.

What is checked:
  1. ready arrives BEFORE the first command, dispositions - one per add_file,
     result declares files that really lie in out_dir;
  2. a module without begin() gives a gen_no_r2 refusal and exit code 1 (not a quiet
     fallback to some other way of launching);
  3. a poisoned cache (the marker of a previous run) is wiped entirely with a
     cache_reset warning;
  4. a file that changed between the folder walk and add_file gives an
     input_changed warning, and the generator gets FRESH mtime/size;
  5. a manifest that declares a package the kit does not have gives a
     gen_missing_dep refusal with the package NAME - and the refusal comes BEFORE the run
     touched the cache (otherwise a typo in the manifest would cost a full recompute);
     a version mismatch with the installed one is a dep_version warning, not a
     refusal: the browser behaves exactly the same.
  6. LINEAGE: the `static/py/_lineage.py` hooks are installed BEFORE importing the generator,
     an observe frame travels for each file with the schema, the COLUMNS read,
     quality checks (with the same `check_id` as the server) and profiles,
     the `onepage.stage()` stages arrive as the last frame, and the observations
     manage to go out BEFORE the terminal frame - including when the run fails.
     Without `_lineage.py` next to it - a warning and an honest run without
     observations, not invented empty ones.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from importlib.metadata import distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
R2HOST = ROOT / "agent" / "r2host.py"

_PASS = 0
_FAIL = 0


def check(label, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"PASS {label}")
    else:
        _FAIL += 1
        print(f"FAIL {label}  {extra}")


# A dummy generator. The cache uses the same key as the real engine
# (schema|name|mtime|size), so that the "poisoned" entry in the test is built
# the same way as in real life.
FAKEGEN = '''
import hashlib, json, os, sys


def begin(out_dir, cache_dir, schema_version, progress):
    progress("begin", 0, 0, "opening the session")
    return Session(out_dir, cache_dir, schema_version, progress)


class Session:
    def __init__(self, out_dir, cache_dir, schema_version, progress):
        self.out_dir = out_dir
        self.cache_dir = cache_dir
        self.sv = schema_version
        self.progress = progress
        self.seen = []

    def add_file(self, name, mtime_ms, size, path):
        # Garbage on stdout is the main point of the dummy generator.
        print("FAKEGEN is noisy on stdout:", name, mtime_ms, size)
        self.progress("files", len(self.seen) + 1, 0, name)
        if name.startswith("SL_NWD"):
            return "service"
        key = hashlib.sha1(
            f"{self.sv}|{name}|{mtime_ms}|{size}".encode()).hexdigest()
        slot = os.path.join(self.cache_dir, key)
        if os.path.exists(slot):
            self.seen.append((name, "cached"))
            return "cached"
        with open(slot, "w", encoding="utf-8") as fh:
            fh.write(str(size))
        self.seen.append((name, mtime_ms, size))
        return "parsed"

    def finish(self):
        html = os.path.join(self.out_dir, "report.html")
        with open(html, "w", encoding="utf-8") as fh:
            fh.write("<html>%d</html>" % len(self.seen))
        data = os.path.join(self.out_dir, "data.json")
        with open(data, "w", encoding="utf-8") as fh:
            json.dump({"seen": self.seen}, fh, ensure_ascii=False)
        return {"html": "report.html", "slots": {"0": "data.json"},
                "write_back": {}}
'''

NOGEN = '''
def run(argv):
    """The old CLI convention: there is no begin() here and never will be."""
    return 0
'''


def _build_apps(tmp: Path) -> Path:
    """Two "bundle" folders: a real R2 generator and a module without begin()."""
    app = tmp / "app"
    (app / "fakegen").mkdir(parents=True)
    (app / "fakegen" / "__init__.py").write_text("", encoding="utf-8")
    (app / "fakegen" / "engine.py").write_text(FAKEGEN, encoding="utf-8")
    (app / "nogen").mkdir(parents=True)
    (app / "nogen" / "__init__.py").write_text("", encoding="utf-8")
    (app / "nogen" / "engine.py").write_text(NOGEN, encoding="utf-8")
    return app


def run_host(app_dir: Path, module: str, out_dir: Path, cache_dir: Path,
             commands: list[dict], schema: str = "sv1") -> dict:
    """Runs the host to the end and returns the parsed frames.

    Commands are handed over WHOLE and at once, not one by one waiting for a reply: the test
    checks the content of the protocol, not the control flow, so it cannot
    hang if the host goes silent.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        # `-I` is mandatory: the host itself is built for it (it puts app_dir on
        # sys.path by hand precisely because with `-I` neither
        # PYTHONPATH nor the script directory gets there).
        sys.executable, "-I", str(R2HOST),
        "--app-dir", str(app_dir), "--module", module,
        "--out-dir", str(out_dir), "--cache-dir", str(cache_dir),
        "--schema-version", schema,
    ]
    stdin = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in commands)
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                          timeout=120)
    frames = []
    junk = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            frames.append(json.loads(line))
        except ValueError:
            junk.append(line)
    return {"frames": frames, "junk": junk, "code": proc.returncode,
            "stderr": proc.stderr}


def _of(frames: list[dict], kind: str) -> list[dict]:
    return [f for f in frames if f.get("t") == kind]


# A generator that REALLY reads workbooks - otherwise there is nothing to observe.
# The two read roads are deliberately different: pandas with an explicit usecols (so the probe sees
# COLUMNS) and python_calamine IMPORTED AT MODULE TOP LEVEL (this
# checks that the hooks were installed BEFORE the generator import: otherwise an
# unwrapped class would be bound here and the reference read would not get into lineage).
# `onepage.stage()` is called without an import - install_hooks() puts it into builtins.
LINGEN = '''
import os

import pandas as pd
from python_calamine import CalamineWorkbook


def begin(out_dir, cache_dir, schema_version, progress):
    return Session(out_dir)


class Session:
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.rows = 0

    def add_file(self, name, mtime_ms, size, path):
        if name.startswith("SL_NWD"):
            wb = CalamineWorkbook.from_path(path)
            wb.get_sheet_by_name(wb.sheet_names[0]).to_python()
            return "service"
        df = pd.read_excel(path, usecols=["city", "qty"])
        onepage.stage("read " + name, df)      # noqa: F821
        self.rows += len(df)
        return "parsed"

    def finish(self):
        p = os.path.join(self.out_dir, "report.html")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("<html>%d</html>" % self.rows)
        return {"html": "report.html", "slots": {}, "write_back": {}}
'''

# A manifest with `inputs` and `checks`: without them the probe takes only the schema, and
# a file has no quality checks and no declared columns.
LIN_MANIFEST = {
    "name": "lingen", "version": "1",
    "entrypoint": {"module": "lingen.engine"},
    "deps": [],
    "inputs": [{"match": "SL_*.xlsx",
                "sheets": [{"name": "Données", "columns": ["city", "qty"]}]}],
    "checks": [{"match": "SL_*.xlsx", "sheet": "Données", "column": "qty",
                "type": "range", "min": 0},
               {"match": "SL_*.xlsx", "column": "city", "type": "not_null"}],
}


def _xlsx(path: Path, rows: list) -> None:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Données"
    ws.append(["city", "qty"])
    for r in rows:
        ws.append(r)
    wb.save(path)


def _entries(frames: list) -> dict:
    """All observation records of all observe frames, by file name."""
    out = {}
    for fr in _of(frames, "observe"):
        for f in fr.get("files") or []:
            out[f.get("name")] = f
    return out


def _test_lineage(tmp: Path) -> None:
    """Lineage of a native run: the same probe and the same order as in the browser.

    What is checked is not "a frame arrived" but what it exists for: the sheet schema,
    the COLUMNS the generator really read, quality check results
    with the same `check_id` as the browser, profiles, the `onepage.stage()` stages
    - and that all of this goes out BEFORE the terminal frame.
    """
    try:
        import openpyxl                                   # noqa: F401
    except ImportError as e:                              # pragma: no cover
        check("lineage: able to write a workbook for the test (openpyxl)",
              False, f"{e}; install openpyxl in this environment")
        return

    app = tmp / "lin-app"
    (app / "lingen").mkdir(parents=True)
    (app / "lingen" / "__init__.py").write_text("", encoding="utf-8")
    (app / "lingen" / "engine.py").write_text(LINGEN, encoding="utf-8")
    (app / "manifest.json").write_text(json.dumps(LIN_MANIFEST, ensure_ascii=False),
                                       encoding="utf-8")

    data = tmp / "lin-data"
    data.mkdir()
    _xlsx(data / "SL_01.xlsx", [["Málaga", 5], ["Zürich", 7]])
    # The second workbook violates BOTH checks: an empty city and a negative number.
    _xlsx(data / "SL_02.xlsx", [["Málaga", 5], [None, -3]])
    _xlsx(data / "SL_NWD.xlsx", [["Málaga", 1]])     # a reference, read by calamine
    # A file that no `inputs` template recognised: it has no checks, and the
    # checks/profiles keys must be absent from its record altogether.
    _xlsx(data / "other.xlsx", [["Köln", 2]])

    def cmd(name: str, dir_index: int, matched: str | None) -> dict:
        p = data / name
        st = p.stat()
        out = {"op": "add_file", "name": name, "mtime_ms": int(st.st_mtime * 1000),
               "size": st.st_size, "path": str(p), "dir": dir_index}
        if matched:
            out["matched_input"] = matched
        return out

    r = run_host(app, "lingen.engine", tmp / "lin-out", tmp / "lin-cache",
                 [cmd("SL_01.xlsx", 0, "SL_*.xlsx"),
                  cmd("SL_02.xlsx", 1, "SL_*.xlsx"),
                  cmd("SL_NWD.xlsx", 0, "SL_*.xlsx"),
                  cmd("other.xlsx", 1, None),
                  {"op": "finish"}])
    check("lineage: a run with the probe reaches result",
          r["code"] == 0 and len(_of(r["frames"], "result")) == 1,
          r["stderr"][-400:])
    obs = _of(r["frames"], "observe")
    check("lineage: observe frames arrived", bool(obs),
          [f.get("t") for f in r["frames"]])
    ents = _entries(r["frames"])
    check("lineage: an observation for every file handed out",
          sorted(ents) == ["SL_01.xlsx", "SL_02.xlsx", "SL_NWD.xlsx", "other.xlsx"],
          sorted(ents))

    one = ents.get("SL_01.xlsx") or {}
    check("lineage: the record carries dir, path and disposition",
          one.get("dir") == 0 and one.get("kind") == "parsed"
          and one.get("path") == str(data / "SL_01.xlsx"),
          {k: one.get(k) for k in ("dir", "kind", "path")})
    sheets = (one.get("schema") or {}).get("sheets") or []
    check("lineage: the workbook schema was read (sheet and header)",
          len(sheets) == 1 and sheets[0].get("name") == "Données"
          and sheets[0].get("header") == ["city", "qty"],
          one.get("schema"))
    consumed = ((one.get("consumed") or {}).get("sheets") or {}).get("Données") or {}
    check("lineage: the COLUMNS the generator read are visible",
          sorted(consumed.get("columns") or []) == ["city", "qty"]
          and consumed.get("all") is False, one.get("consumed"))

    # The reference is read by calamine imported at the top level of
    # the generator module. A record about it exists ONLY if the hooks were installed before the import.
    nwd = ents.get("SL_NWD.xlsx") or {}
    check("lineage: hooks were installed BEFORE the generator import "
          "(the calamine read is recorded)",
          nwd.get("kind") == "service"
          and bool((nwd.get("consumed") or {}).get("sheets")),
          nwd.get("consumed"))

    ids = [c.get("check_id") for c in (one.get("checks") or [])]
    check("lineage: quality checks arrived with canonical ids",
          ids == ["range:SL_*.xlsx:Données:qty", "not_null:SL_*.xlsx:*:city"], ids)
    check("lineage: column profiles arrived",
          sorted(p.get("column") for p in (one.get("profiles") or []))
          == ["city", "qty"], one.get("profiles"))
    bad = ents.get("SL_02.xlsx") or {}
    states = {c.get("check_id"): (c.get("status"), c.get("failed"))
              for c in (bad.get("checks") or [])}
    check("lineage: a broken workbook yields fail, not silence",
          states.get("range:SL_*.xlsx:Données:qty") == ("fail", 1)
          and states.get("not_null:SL_*.xlsx:*:city") == ("fail", 1), states)

    # An unmarked file: it has NO checks, and no keys either - "there was nothing to
    # do" and "tried, failed" (an explicit null) are read differently by the server.
    other = ents.get("other.xlsx") or {}
    check("lineage: a file without matched_input goes without checks/profiles keys",
          "checks" not in other and "profiles" not in other,
          {k: other.get(k) for k in ("checks", "profiles")})
    check("lineage: its schema is still taken",
          bool((other.get("schema") or {}).get("sheets")), other.get("schema"))

    # Stages: only in the last frame, in call order, with the names of files
    # read before the stage (note_file sets them BEFORE add_file).
    with_stages = [i for i, fr in enumerate(obs) if fr.get("stages")]
    check("lineage: stages travel in exactly one, the last observe frame",
          with_stages == [len(obs) - 1], with_stages)
    stages = obs[-1].get("stages") if obs else []
    names = [s.get("name") for s in (stages or [])]
    check("lineage: onepage.stage() stages in call order",
          names == ["read SL_01.xlsx", "read SL_02.xlsx",
                    "read other.xlsx"], names)
    check("lineage: a stage knows which files were read before it",
          bool(stages) and stages[0].get("read_files") == ["SL_01.xlsx"],
          stages[0].get("read_files") if stages else None)
    check("lineage: a stage has a frame shape (rows and columns)",
          bool(stages) and stages[0].get("rows") == 2
          and stages[0].get("columns") == ["city", "qty"],
          stages[0] if stages else None)

    # Frame order: observations must go out BEFORE the terminal one. The caller
    # (`agent/generator.py::_read_frames`) stops reading on `result`/`error`.
    kinds = [f.get("t") for f in r["frames"]]
    last_obs = len(kinds) - 1 - kinds[::-1].index("observe") if "observe" in kinds else -1
    check("lineage: the last observe frame comes BEFORE result",
          last_obs >= 0 and kinds.index("result") > last_obs, kinds)

    # ── reverse run 1: the run fails - observations still went out ─────────
    broken = data / "SL_03.xlsx"
    broken.write_text("this is not a workbook", encoding="utf-8")
    r2 = run_host(app, "lingen.engine", tmp / "lin-out2", tmp / "lin-cache2",
                  [cmd("SL_01.xlsx", 0, "SL_*.xlsx"),
                   cmd("SL_03.xlsx", 0, "SL_*.xlsx"),
                   {"op": "finish"}])
    kinds2 = [f.get("t") for f in r2["frames"]]
    check("lineage: a failed run is an error", r2["code"] == 1
          and "error" in kinds2, kinds2)
    check("lineage: observations managed to go out BEFORE the error frame",
          "observe" in kinds2 and kinds2.index("observe") < kinds2.index("error"),
          kinds2)
    ents2 = _entries(r2["frames"])
    check("lineage: what was read before the failure is not lost",
          "SL_01.xlsx" in ents2, sorted(ents2))

    # ── reverse run 2: no probe next to it - a warning, not a refusal ──────
    # The host is put into a clean directory: no `_lineage.py` next to it, no ../static/py.
    #
    # The generator here is deliberately DIFFERENT - one that does not call onepage.stage().
    # A generator that calls it fails with NameError without the probe, and this is not
    # unfinished work: the `onepage` module is published by the probe itself, and in the browser,
    # when `_lineage.py` fails to start, such a generator fails in exactly the same way. Fixing
    # it with a stub would diverge the environments - on the laptop the report
    # would build, in the browser it would not.
    lone = tmp / "lin-lonely"
    lone.mkdir()
    shutil.copyfile(R2HOST, lone / "r2host.py")
    proc = subprocess.run(
        [sys.executable, "-I", str(lone / "r2host.py"),
         "--app-dir", str(tmp / "app"), "--module", "fakegen.engine",
         "--out-dir", str(tmp / "lin-out3"), "--cache-dir", str(tmp / "lin-cache3"),
         "--schema-version", "sv1"],
        input=json.dumps(cmd("SL_01.xlsx", 0, "SL_*.xlsx")) + "\n"
              + json.dumps({"op": "finish"}) + "\n",
        capture_output=True, text=True, timeout=120)
    frames3 = []
    for line in proc.stdout.splitlines():
        try:
            frames3.append(json.loads(line))
        except ValueError:
            pass
    warn_kinds = [w.get("kind") for w in _of(frames3, "warning")]
    check("lineage: without _lineage.py next to it - a lineage_unavailable warning",
          "lineage_unavailable" in warn_kinds, warn_kinds)
    check("and the run still REACHES result (the probe is not worth the report)",
          proc.returncode == 0 and len(_of(frames3, "result")) == 1,
          proc.stderr[-300:])
    check("and not a single observe frame is invented",
          _of(frames3, "observe") == [], _of(frames3, "observe"))
    msg = next((w.get("msg", "") for w in _of(frames3, "warning")
                if w.get("kind") == "lineage_unavailable"), "")
    check("and the text names the places that were searched",
          "_lineage.py" in msg and str(lone) in msg, msg[:200])

    # ── checks canonicalisation matches the server one ────────────────────
    # `check_id` is the key by which the server joins Data Quality findings across
    # runs. If it diverged from the browser's, one report would have two
    # independent histories of one check.
    try:
        sys.path.insert(0, str(ROOT))
        from app.services import genmanifest     # noqa: PLC0415
        from agent import r2host as _host        # noqa: PLC0415
        probe = {"checks": [
            {"match": "A*.xlsx", "column": " qty ", "type": "range", "min": 0},
            {"match": "B*.xlsx", "sheet": " Feuille \u00e91 ", "column": "c",
             "type": "ref_lookup", "table": "t"},
            {"match": "C*.xlsx", "column": "d", "type": "no-such-type"},
            "junk",
        ]}
        check("checks canonicalisation in the host matches genmanifest.checks_for",
              _host._canon_checks(probe) == genmanifest.checks_for(probe),
              _host._canon_checks(probe))
    except ImportError as e:
        check("checks canonicalisation was compared with genmanifest.checks_for", False,
              f"the repository is at hand, but the import failed: {e}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="op-r2host-test-"))
    try:
        app = _build_apps(tmp)
        data = tmp / "data"
        data.mkdir()
        names = ["SL_01.xlsx", "SL_02.xlsx", "SL_NWD.xlsx"]
        for i, n in enumerate(names):
            (data / n).write_text("x" * (i + 1), encoding="utf-8")

        def spec(name: str, **over) -> dict:
            p = data / name
            st = p.stat()
            cmd = {"op": "add_file", "name": name,
                   "mtime_ms": int(st.st_mtime * 1000), "size": st.st_size,
                   "path": str(p)}
            cmd.update(over)
            return cmd

        # ---------------------------------------------------------------- #
        # 1. The happy path.
        # ---------------------------------------------------------------- #
        out1 = tmp / "out1"
        cache1 = tmp / "cache1"
        r = run_host(app, "fakegen.engine", out1, cache1,
                     [spec(n) for n in names] + [{"op": "finish"}])
        check("exit code 0 on a clean run", r["code"] == 0, r["stderr"][-400:])
        check("nothing on stdout but frames (the generator's printing is diverted)",
              r["junk"] == [], r["junk"][:3])
        ready = _of(r["frames"], "ready")
        disp = _of(r["frames"], "disposition")
        res = _of(r["frames"], "result")
        check("ready arrived exactly once", len(ready) == 1, len(ready))
        first_disp = r["frames"].index(disp[0]) if disp else -1
        check("ready arrived BEFORE the first disposition",
              ready and first_disp > r["frames"].index(ready[0]))
        check("a disposition for every add_file, in the same order",
              [(d["name"], d["kind"]) for d in disp]
              == [("SL_01.xlsx", "parsed"), ("SL_02.xlsx", "parsed"),
                  ("SL_NWD.xlsx", "service")],
              [(d["name"], d["kind"]) for d in disp])
        check("progress arrives as frames", len(_of(r["frames"], "progress")) >= 4,
              len(_of(r["frames"], "progress")))
        check("result declared html and slot 0",
              len(res) == 1 and res[0]["result"]["html"] == "report.html"
              and res[0]["result"]["slots"] == {"0": "data.json"},
              res)
        check("the declared files really lie in out_dir",
              (out1 / "report.html").is_file() and (out1 / "data.json").is_file())
        check("the analyst's source files are in place - the host deleted nothing",
              all((data / n).exists() for n in names))
        check("the marker was removed after a successful finish()",
              not (cache1 / ".r2host-running").exists())

        # ---------------------------------------------------------------- #
        # 2. A module without begin(): a refusal, not a fallback to another way of launching.
        # ---------------------------------------------------------------- #
        r = run_host(app, "nogen.engine", tmp / "out2", tmp / "cache2",
                     [{"op": "finish"}])
        err = _of(r["frames"], "error")
        check("a module without begin() gives exit code 1", r["code"] == 1)
        check("and an error frame with kind=gen_no_r2",
              len(err) == 1 and err[0]["kind"] == "gen_no_r2", err)
        check("ready was NOT announced", _of(r["frames"], "ready") == [])

        # ---------------------------------------------------------------- #
        # 3. Poisoned cache: the previous run's marker -> wipe everything.
        # ---------------------------------------------------------------- #
        cache3 = tmp / "cache3"
        cache3.mkdir()
        (cache3 / ".r2host-running").write_text("running\n", encoding="utf-8")
        poison = cache3 / "deadbeef"
        poison.write_text("unfinished entry", encoding="utf-8")
        r = run_host(app, "fakegen.engine", tmp / "out3", cache3,
                     [spec(n) for n in names] + [{"op": "finish"}])
        kinds = [w.get("kind") for w in _of(r["frames"], "warning")]
        check("a poisoned cache gives a cache_reset warning",
              "cache_reset" in kinds, kinds)
        check("and the unfinished entry is physically wiped", not poison.exists())
        check("a run after the cache wipe reaches result",
              r["code"] == 0 and len(_of(r["frames"], "result")) == 1,
              r["stderr"][-300:])

        # The same folder a second time, the marker is gone: cache entries are alive, and
        # the generator must answer "cached" - otherwise the previous check
        # proves nothing (one can wipe emptiness too).
        r = run_host(app, "fakegen.engine", tmp / "out3b", cache3,
                     [spec(n) for n in names] + [{"op": "finish"}])
        kinds = [w.get("kind") for w in _of(r["frames"], "warning")]
        disp = [(d["name"], d["kind"]) for d in _of(r["frames"], "disposition")]
        check("without a marker the cache is NOT wiped", "cache_reset" not in kinds, kinds)
        check("and the same set is read from the cache",
              disp == [("SL_01.xlsx", "cached"), ("SL_02.xlsx", "cached"),
                       ("SL_NWD.xlsx", "service")], disp)

        # ---------------------------------------------------------------- #
        # 4. The file changed between the folder walk and add_file.
        # ---------------------------------------------------------------- #
        out4 = tmp / "out4"
        stale = spec("SL_01.xlsx")
        stale["mtime_ms"] = int(stale["mtime_ms"]) - 5000     # "walked" 5 s ago
        stale["size"] = int(stale["size"]) + 7                 # and the size was different
        r = run_host(app, "fakegen.engine", out4, tmp / "cache4",
                     [stale, {"op": "finish"}])
        warns = _of(r["frames"], "warning")
        changed = [w for w in warns if w.get("kind") == "input_changed"]
        check("stale mtime/size give an input_changed warning",
              len(changed) == 1, [w.get("kind") for w in warns])
        check("the warning text shows both the old and the new value",
              changed and str(stale["mtime_ms"]) in changed[0].get("msg", "")
              and str(stale["size"]) in changed[0].get("msg", ""),
              changed)
        # The generator must get FRESH values, not the ones sent: it is
        # they that key the cache, and a stale pair = an entry with someone else's content.
        seen = json.loads((out4 / "data.json").read_text(encoding="utf-8"))["seen"]
        real = (data / "SL_01.xlsx").stat()
        check("the generator got fresh mtime/size, not the ones sent",
              seen and seen[0][1] == int(real.st_mtime * 1000)
              and seen[0][2] == real.st_size, seen)

        # ---------------------------------------------------------------- #
        # 6. The manifest declares a package the kit does not have.
        # ---------------------------------------------------------------- #
        # Put the manifest into THE SAME bundle: the host reads the dependency set
        # from app_dir, not from arguments.
        manifest = app / "manifest.json"
        manifest.write_text(json.dumps({
            "name": "fakegen", "version": "1",
            "entrypoint": {"module": "fakegen.engine"},
            "deps": [{"name": "nonexistent-package-op", "version": "1.2.3"}],
        }), encoding="utf-8")
        cache6 = tmp / "cache6"
        cache6.mkdir()
        keep = cache6 / "cafebabe"
        keep.write_text("entry of the previous run", encoding="utf-8")
        (cache6 / ".r2host-running").write_text("running\n", encoding="utf-8")
        r = run_host(app, "fakegen.engine", tmp / "out6", cache6,
                     [spec("SL_01.xlsx"), {"op": "finish"}])
        err = _of(r["frames"], "error")
        check("a package not from the lock -> exit code 1 and kind=gen_missing_dep",
              r["code"] == 1 and [e["kind"] for e in err] == ["gen_missing_dep"],
              [e.get("kind") for e in err])
        check("the message names the package NAME, not just \"something is missing\"",
              err and "nonexistent-package-op" in err[0].get("message", ""), err)
        check("the refusal came BEFORE the run lock - the cache is untouched",
              keep.exists() and (cache6 / ".r2host-running").exists())
        check("and the session was not opened (ready was not announced)",
              _of(r["frames"], "ready") == [])

        # The package is there but the version differs: a warning, not a refusal -
        # kit versions are pinned by the Pyodide lock, and the browser behaves the same.
        # The name is taken from a REALLY installed package of this same interpreter:
        # writing "pip" here would tie the test to the contents of someone else's venv (the
        # project venv has no pip).
        installed = sorted(d.metadata["Name"] for d in distributions())[0]
        manifest.write_text(json.dumps({
            "name": "fakegen", "version": "1",
            "entrypoint": {"module": "fakegen.engine"},
            "deps": [{"name": installed, "version": "0.0.1-different"}],
        }), encoding="utf-8")
        r = run_host(app, "fakegen.engine", tmp / "out6b", tmp / "cache6b",
                     [spec("SL_01.xlsx"), {"op": "finish"}])
        kinds = [w.get("kind") for w in _of(r["frames"], "warning")]
        check("a different version of an installed package -> a dep_version warning",
              "dep_version" in kinds, kinds)
        check("and the run still reaches result",
              r["code"] == 0 and len(_of(r["frames"], "result")) == 1,
              r["stderr"][-300:])
        manifest.unlink()

        # ---------------------------------------------------------------- #
        # 5. A break before finish(): the marker stays, the next run cleans.
        # ---------------------------------------------------------------- #
        cache5 = tmp / "cache5"
        r = run_host(app, "fakegen.engine", tmp / "out5", cache5,
                     [spec("SL_01.xlsx")])      # we do not send finish, EOF
        check("channel break before finish() - exit code 1 and kind=canceled",
              r["code"] == 1
              and [e["kind"] for e in _of(r["frames"], "error")] == ["canceled"],
              [e.get("kind") for e in _of(r["frames"], "error")])
        check("after the break the marker REMAINED - the cache is marked as poisoned",
              (cache5 / ".r2host-running").exists())

        # ---------------------------------------------------------------- #
        # 6. Lineage: the same probe and the same order as in the browser.
        # ---------------------------------------------------------------- #
        _test_lineage(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if _FAIL == 0 else 'FAILURES: ' + str(_FAIL)}  ({_PASS} passed)")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
