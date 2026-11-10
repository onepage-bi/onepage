#!/usr/bin/env python3
"""One generator under CPython and under Pyodide: are the numbers the same?

    .venv/bin/python tools/engine_parity.py --bundle B.zip --dir FOLDER \
        [--out DIR] [--python EXE] [--repeat 2] [--no-probe] \
        [--decoder module:function]

Why. Today the report generator lives in the browser (wasm32, CPython 3.14,
pandas 3.0.2), and we promise the analyst the same report NATIVELY on their
laptop (CPython 3.13, pandas 3.0.5). The promise is worth exactly as much as the
proof that both sides produce THE SAME NUMBERS. Known landmine: in wasm
`np.array([1]).dtype` is int32, natively int64 — pandas pins int64 explicitly,
so the risky spots are where the generator calls bare numpy without a dtype.

How this tool differs from `diff`: it compares in LAYERS. Byte equality is the
expected outcome and the only PASS; but a gzip header, JSON key order or
reshuffled whitespace in HTML can differ without moving a single number, and
calling that a failure trains the report's reader to scroll past red. So a byte
difference is DECODED and re-compared by numbers, while an opaque binary format
honestly stays a failure (we do not pretend to understand someone else's
layout — that is what `--decoder` is for).

── Contracts with neighbouring parts ───────────────────────────────────────
This file writes NEITHER `agent/r2host.py` NOR `tools/run_bundle_pyodide.mjs`.
It CALLS them, and this is exactly how:

  agent/r2host.py  (native side, protocol = line-delimited JSON)
      python agent/r2host.py --app-dir DIR --module MODULE \
          --out-dir OUT --cache-dir CACHE --schema-version SV
      stdin : {"op":"add_file","name","mtime_ms","size","path"}
              {"op":"finish"} / {"op":"abort"}
      stdout: {"t":"ready"} — before the first command
              {"t":"progress","stage","done","total","msg"}
              {"t":"disposition","kind":"parsed|cached|service","name"}
              {"t":"warning","message"}
              {"t":"result","result":{"html","slots","write_back"}}
              {"t":"error","kind","message","detail"}

  tools/run_bundle_pyodide.mjs  (wasm side)
      node tools/run_bundle_pyodide.mjs --bundle B.zip --files FILES.JSON \
          --out OUT [--pyodide DIR] [--schema SHA]
      writes the outputs to OUT and OUT/result.json.

files.json (written to `<out>/files.json` and fed to BOTH sides — it is what
guarantees that identical inputs are compared in identical order) is a FLAT
ARRAY, which is what the runner expects:
      [{"name","path","mtime_ms","size","matched_input"}, ...]
Everything else about the run (bundle, entrypoint, schema_version, masks) sits
next to it in `<out>/run-spec.json` — for the record, not for the runner.

result.json is read leniently, because the two sides write it with different code:
      {"result": {"html": name|null, "slots": {...}, "write_back": {...}},
       "dispositions": [{"name","kind"}, ...] | [[name, kind], ...],
       "progress_count" | "progress_frames": int,
       "versions": {"python","pandas","numpy","numpy_default_int", ...}}
No `versions` is fine — the probe covers them; no `result` is a failure,
because then there is nothing to compare.

── Verdict ─────────────────────────────────────────────────────────────────
  PASS — every declared output is byte-identical.
  WARN — bytes differ, but every integer matches, every float differs by no
         more than 1e-12 relative difference, and dispositions match.
  FAIL — at least one number differs, an output is missing, an opaque output
         is not identical, or a side disagrees with itself in NUMBERS
         (`--repeat`; byte instability without moving numbers is only a
         note: a generator that stamps wall-clock time need not be
         byte-deterministic, and there is no point measuring its clock).
Exit code 0 on PASS/WARN, 1 on FAIL.

The asymmetry is deliberate: a side's self-check decides whether the comparison
MAKES SENSE at all, and forgives a difference that moves no numbers; the
comparison between sides forgives nothing and names every difference
individually — including that very clock.

This is a developer tool and does not ship in the agent's offline kit, so pandas
and numpy may be used here (only the probe calls them).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib
import io
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
# globmatch is the ONLY allowed mask matcher: the same `globToRe` port lives
# in the browser engine, on the server and in the agent, while fnmatch differs
# from it on every point (character classes, "/", case).
sys.path.insert(0, str(ROOT))
from app.services.globmatch import first_match  # noqa: E402

REL_TOL = 1e-12          # part of the verdict contract, not a tunable knob
MAX_SHOWN = 10           # "the first 10 places that differ"

# ──────────────────────────────────────────────────────────────────────────
# Bit-width probe
# ──────────────────────────────────────────────────────────────────────────

# The same source runs on both sides — otherwise the probe would compare not
# the environments but two editions of itself.
PROBE_SRC = r"""
import sys, json

def probe():
    import numpy as np
    import pandas as pd
    # 8 x 2**30 = 2**33: in int32 that overflows with a sign flip, in int64 not.
    # Bare numpy without an explicit dtype is exactly the case the whole check
    # exists for, so the vector is built from a list of Python ints.
    overflow = np.array([2 ** 30] * 8)
    return {
        "python": sys.version.split()[0],
        "maxsize": sys.maxsize,
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "numpy_default_int": int(np.dtype(int).itemsize),
        "array_int_dtype": str(overflow.dtype),
        "int32_overflow_sum": int(overflow.sum()),
        "float_reduction": repr(float(np.linspace(0.0, 1.0, 1000000).sum())),
    }
"""

PROBE_NATIVE_TAIL = '\nprint("@@PROBE@@" + json.dumps(probe()))\n'


def probe_native(python_exe: str) -> dict:
    out = subprocess.run(
        [python_exe, "-I", "-c", PROBE_SRC + PROBE_NATIVE_TAIL],
        capture_output=True, text=True,
    )
    return _parse_probe(out.stdout, out.stderr, "native")


def probe_wasm(node: str, pyodide_base: Path, work: Path) -> dict:
    """Starts Pyodide under node ONLY for the probe.

    The throwaway script lives in the work directory, not in the repository: it
    is a debugging stub, not a second engine, and has no place in project history.
    """
    src = work / "_probe.mjs"
    base = pyodide_base.resolve()
    src.write_text(
        "const base = new URL('file://%s/');\n"
        "const { loadPyodide } = await import(base + 'pyodide.mjs');\n"
        "const py = await loadPyodide({ indexURL: base.pathname });\n"
        "await py.loadPackage(['pandas']);\n"
        "py.runPython(%s);\n"
        "const out = py.runPython('json.dumps(probe())');\n"
        "console.log('@@PROBE@@' + out);\n" % (base, json.dumps(PROBE_SRC)),
        encoding="utf-8",
    )
    out = subprocess.run([node, str(src)], capture_output=True, text=True)
    return _parse_probe(out.stdout, out.stderr, "wasm")


def _parse_probe(stdout: str, stderr: str, side: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith("@@PROBE@@"):
            return json.loads(line[len("@@PROBE@@"):])
    # An explicit failure instead of an empty dict: "the probe printed nothing"
    # is a fact that must reach the reader, not dissolve into dashes.
    return {"error": (stderr.strip() or stdout.strip() or f"{side} probe is silent")[-600:]}


def print_probe(native: dict, wasm: dict) -> None:
    print("\n── Bit-width probe ─────────────────────────────────────────────")
    rows = [
        ("python", "python"), ("pandas", "pandas"), ("numpy", "numpy"),
        ("sys.maxsize", "maxsize"),
        ("numpy_default_int", "numpy_default_int"),
        ("np.array([...]).dtype", "array_int_dtype"),
        ("sum 8 x 2**30", "int32_overflow_sum"),
        ("sum(linspace 1e6)", "float_reduction"),
    ]
    width = max(len(lbl) for lbl, _ in rows)
    for label, key in rows:
        a = native.get(key, "—")
        b = wasm.get(key, "—")
        flag = "  <-- differs" if a != b else ""
        print(f"  {label:<{width}}  native: {a!s:<26} wasm: {b!s}{flag}")
    for side, d in (("native", native), ("wasm", wasm)):
        if d.get("error"):
            print(f"  !! {side} probe failed: {d['error']}")


# ──────────────────────────────────────────────────────────────────────────
# Bundle and file selection
# ──────────────────────────────────────────────────────────────────────────

def unpack_bundle(zip_path: Path, dest: Path) -> dict:
    """Unpacks the bundle and returns manifest.json."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            # The bundle is a foreign archive; ".." and absolute paths do not get
            # in (same lesson as agent/zipguard.py, only shorter: here we are in temp).
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise SystemExit(f"bundle contains a forbidden path: {info.filename}")
        z.extractall(dest)
    man = dest / "manifest.json"
    if not man.exists():
        raise SystemExit(f"no manifest.json in the bundle: {zip_path}")
    return json.loads(man.read_text(encoding="utf-8"))


def schema_version_of(zip_path: Path) -> str:
    """The engine's schema_version = checksum_sha256 of the bundle version (R2
    contract: changed code invalidates the generator's whole cache)."""
    h = hashlib.sha256()
    with zip_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def select_files(folder: Path, manifest: dict) -> list[dict]:
    """Flat listing of the folder + selection by the manifest.inputs[].match masks.

    Order is by `name.casefold()`: add_file order is visible to the generator
    (cache, accumulators, concatenation order), so both sides must receive the
    same list in the same order.
    """
    patterns = [i.get("match") for i in manifest.get("inputs", []) if i.get("match")]
    if not patterns:
        raise SystemExit("manifest.inputs is empty — nothing to select folder files by")
    out: list[dict] = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        if not entry.is_file():
            continue
        pat = first_match(patterns, entry.name)
        if pat is None:
            continue
        st = entry.stat()
        out.append({
            "name": entry.name,
            "path": str(entry.resolve()),
            # mtime in ms — exactly what the engine keys its cache on (hash(sv|name|mtime|size))
            "mtime_ms": int(st.st_mtime * 1000),
            "size": st.st_size,
            "matched_input": pat,
        })
    if not out:
        raise SystemExit(f"no file in {folder} matched the masks {patterns}")
    return out


# ──────────────────────────────────────────────────────────────────────────
# Native run: r2host protocol driver
# ──────────────────────────────────────────────────────────────────────────

class HostError(RuntimeError):
    pass


def run_native(files_spec: dict, out_dir: Path, python_exe: str, r2host: Path) -> dict:
    if not r2host.exists():
        raise SystemExit(
            f"{r2host} is missing — a neighbouring part of the work writes it.\n"
            "The expected call and protocol are described in the header of tools/engine_parity.py."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    # The cache is PRIVATE to each run. With a shared cache the second run gets
    # "cached" where the first got "parsed", and the reproducibility check
    # catches not non-determinism but the test bench's own setup (caught on the
    # very first --repeat 2). The wasm side is always cold: its cache lives in
    # the Pyodide FS and dies with the process.
    cache_dir = out_dir.parent / f"cache-{out_dir.name}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    err_log = out_dir / "r2host.stderr.log"

    cmd = [
        # `-I` is not over-caution but part of the r2host contract: it puts
        # app_dir on sys.path itself precisely because under `-I` neither
        # PYTHONPATH nor the script's directory (agent/) gets there. Without `-I`
        # agent/ would come first on the path, and a bundle module could be
        # shadowed by an agent module of the same name — a needless divergence from wasm.
        python_exe, "-I", str(r2host),
        "--app-dir", files_spec["bundle_dir"],
        "--module", files_spec["entrypoint"],
        "--out-dir", str(out_dir),
        "--cache-dir", str(cache_dir),
        "--schema-version", files_spec["schema_version"],
    ]
    t0 = time.time()
    # stderr goes to a file, not a PIPE: reading two channels from one thread is
    # the classic deadlock, and the generator's traceback is needed in full.
    with err_log.open("wb") as errfh:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errfh,
            text=True, bufsize=1,
        )
        state = {"progress": 0, "dispositions": [], "warnings": []}
        try:
            _await(proc, state, {"ready"}, err_log)
            for f in files_spec["files"]:
                _send(proc, {"op": "add_file", "name": f["name"],
                             "mtime_ms": f["mtime_ms"], "size": f["size"],
                             "path": f["path"]})
                # A disposition must arrive for EVERY add_file: it is what says
                # the file was parsed / taken from cache / kept until finish.
                _await(proc, state, {"disposition"}, err_log)
            _send(proc, {"op": "finish"})
            msg = _await(proc, state, {"result"}, err_log)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait(timeout=30)

    res = {
        "engine": "native",
        "versions": {},
        "numpy_default_int": None,
        "dispositions": state["dispositions"],
        "progress_frames": state["progress"],
        "warnings": state["warnings"],
        "elapsed_s": round(time.time() - t0, 3),
        "result": msg.get("result") or {},
    }
    res = normalize_result(res, "native")
    (out_dir / "result.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def _send(proc: subprocess.Popen, obj: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def _await(proc: subprocess.Popen, state: dict, want: set[str], err_log: Path) -> dict:
    """Reads frames until the first of `want`; progress/warning accumulate on the way."""
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            tail = err_log.read_text(encoding="utf-8", errors="replace")[-800:]
            raise HostError(f"r2host closed stdout before sending {sorted(want)}.\n{tail}")
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            raise HostError(f"r2host printed non-JSON: {line[:200]!r}")
        t = msg.get("t")
        if t == "progress":
            state["progress"] += 1
            continue
        if t == "warning":
            # r2host sends the text in "msg", the protocol draft called it "message" —
            # accept both so a warning is not lost to a typo.
            state["warnings"].append(msg.get("msg") or msg.get("message") or "")
            continue
        if t == "disposition":
            state["dispositions"].append([msg.get("name"), msg.get("kind")])
            if "disposition" in want:
                return msg
            continue
        if t == "error":
            raise HostError("r2host: {} / {} / {}".format(
                msg.get("kind"), msg.get("message"), str(msg.get("detail"))[:400]))
        if t in want:
            return msg
        # An unknown frame is no reason to crash: the protocol may grow.


# ──────────────────────────────────────────────────────────────────────────
# Run under wasm
# ──────────────────────────────────────────────────────────────────────────

def run_wasm(files_json: Path, out_dir: Path, node: str, runner: Path,
             bundle_zip: Path, pyodide_base: Path, schema_version: str) -> dict:
    if not runner.exists():
        raise SystemExit(
            f"{runner} is missing — a neighbouring part of the work writes it.\n"
            "The expected call is described in the header of tools/engine_parity.py."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    proc = subprocess.run(
        [node, str(runner), "--bundle", str(bundle_zip), "--files", str(files_json),
         "--out", str(out_dir), "--pyodide", str(pyodide_base),
         "--schema", schema_version],
        capture_output=True, text=True,
    )
    (out_dir / "runner.stdout.log").write_text(proc.stdout, encoding="utf-8")
    (out_dir / "runner.stderr.log").write_text(proc.stderr, encoding="utf-8")
    rj = out_dir / "result.json"
    if proc.returncode != 0 or not rj.exists():
        raise HostError(
            f"run_bundle_pyodide.mjs returned {proc.returncode} and "
            f"{'did not write' if not rj.exists() else 'wrote'} result.json\n"
            + (proc.stderr or proc.stdout)[-800:]
        )
    res = normalize_result(json.loads(rj.read_text(encoding="utf-8")), "wasm")
    res.setdefault("elapsed_s", round(time.time() - t0, 3))
    return res


def normalize_result(res: dict, engine: str) -> dict:
    """Normalises result.json to one shape.

    The two sides write it with different code in different languages, and
    bending them to each other for cosmetics is needless coupling; it is cheaper
    to accept both shapes here, in the one place that reads both.
    """
    res = dict(res)
    res.setdefault("engine", engine)
    disp = []
    for d in res.get("dispositions") or []:
        if isinstance(d, dict):
            disp.append([d.get("name"), d.get("kind")])
        else:
            disp.append([d[0], d[1]])
    res["dispositions"] = disp
    if "progress_frames" not in res:
        res["progress_frames"] = res.get("progress_count")
    if not isinstance(res.get("result"), dict):
        raise HostError(f"{engine}: result.json has no result block — nothing to compare")
    return res


# ──────────────────────────────────────────────────────────────────────────
# Comparison layer
# ──────────────────────────────────────────────────────────────────────────

class Diff:
    """Comparison outcome for ONE output."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.bytes_equal = False
        self.opaque: str | None = None        # format cannot be read — that is a failure
        self.structural: list[str] = []       # shape differs (keys, lengths, types)
        self.notes: list[str] = []            # a difference that moves no numbers
        self.int_bad: list[tuple] = []        # (where, a, b)
        self.float_bad: list[tuple] = []      # (where, a, b, rel) — rel > REL_TOL
        self.float_near: list[tuple] = []     # (where, a, b, rel) — rel <= REL_TOL
        self.numbers = 0                      # how many differences were recorded
        self.seen = 0                         # how many numbers were checked at all
        self.sizes: tuple[int, int] = (0, 0)
        self.worst_abs = 0.0
        self.worst_rel = 0.0
        self.decoded_as = ""

    @property
    def fatal(self) -> bool:
        return bool(self.opaque or self.structural or self.int_bad or self.float_bad)

    @property
    def clean(self) -> bool:
        return self.bytes_equal

    def note(self, where: str, a, b) -> None:
        """Records one numeric difference and updates the worst-case metrics."""
        self.numbers += 1
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            self.structural.append(f"{where}: {a!r} vs {b!r}")
            return
        if math.isnan(fa) and math.isnan(fb):
            return
        ad = abs(fa - fb)
        scale = max(abs(fa), abs(fb))
        rel = ad / scale if scale else (0.0 if ad == 0 else math.inf)
        self.worst_abs = max(self.worst_abs, ad)
        self.worst_rel = max(self.worst_rel, rel if math.isfinite(rel) else self.worst_rel)
        if isinstance(a, int) and isinstance(b, int):
            # An integer is a count, an identifier or a key; "almost equal" makes
            # no sense for it, so the tolerance does not apply.
            self.int_bad.append((where, a, b))
        elif rel <= REL_TOL:
            self.float_near.append((where, a, b, rel))
        else:
            self.float_bad.append((where, a, b, rel))

    def places(self) -> list[str]:
        out = []
        for w, a, b in self.int_bad:
            out.append(f"integer {w}: {a} vs {b}")
        for w, a, b, rel in self.float_bad:
            out.append(f"float {w}: {a!r} vs {b!r} (rel. {rel:.3e})")
        out.extend(self.structural)
        for w, a, b, rel in self.float_near:
            out.append(f"float {w}: {a!r} vs {b!r} (rel. {rel:.3e}, within tolerance)")
        return out


def _decompress(name: str, data: bytes) -> tuple[str, bytes] | None:
    """Strips ONE packing layer; None means there is no layer."""
    low = name.lower()
    if low.endswith(".gz"):
        return name[:-3], gzip.decompress(data)
    if low.endswith(".br"):
        try:
            import brotli  # type: ignore
        except ImportError:
            return None     # more honest to stay opaque than to guess
        return name[:-3], brotli.decompress(data)
    return None


_NUM_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")

# Wall-clock time in the document: "2026-10-19 13:55" / "2026-10-19T13:55:07".
# It is MASKED before numbers are compared — the only normalisation in the whole
# tool, so it is described here in full.
#   Why at all: slreport stamps `generated_at` into the bootstrap snapshot. It is
#   not a computed result but the launch time, and it differs even between two
#   runs of the SAME side if they fall on different minutes. Without the mask the
#   tool declares the side "not reproducing itself" and never gets to comparing
#   the sides at all — i.e. it measures the clock instead of the generator.
#   Why so narrow: the mask catches exactly a date-with-time and nothing else. The
#   year ("current_year":2026), the week number, the file count are ordinary
#   numbers, and a difference in them stays a difference.
#   Why this is not concealment: every masked place is counted, and the counter is
#   printed in the report next to the output. The reader sees "masked 1" and
#   knows what is missing from the comparison.
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?")
_TS_MASK = "<TS>"


def _mask_timestamps(text: str) -> tuple[str, int]:
    masked, n = _TS_RE.subn(_TS_MASK, text)
    return masked, n


def _numbers_in_text(text: str) -> tuple[list[Any], list[str]]:
    """The document's numbers in order + the surroundings of each.

    The surroundings exist purely to make a failure readable: "#1095: 48 vs 49"
    says nothing, while the same line with a piece of "generated_at" instantly
    explains that it was the clock that differed, not the computation. Context
    does not affect the verdict.
    """
    out: list[Any] = []
    ctx: list[str] = []
    for m in _NUM_RE.finditer(text):
        s = m.group(0)
        out.append(float(s) if any(c in s for c in ".eE") else int(s))
        around = text[max(0, m.start() - 44):m.end() + 16]
        ctx.append(re.sub(r"\s+", " ", around).strip())
    return out, ctx


def _note_text_drift(d: Diff, ta: str, tb: str) -> None:
    """Did the DOCUMENT text differ apart from the numbers? A note, not a verdict.

    Numbers are cut out identically on both sides, then the skeletons themselves
    are compared. If they are equal, the byte difference is fully explained by
    numbers (or whitespace, if those are gone too). If not, the reader is shown
    the first place where the texts diverge: usually a path, a machine name or a
    time zone, i.e. environment provenance, not the computation.
    """
    sa, sb = _NUM_RE.sub("#", ta), _NUM_RE.sub("#", tb)
    if sa == sb:
        return
    i = 0
    while i < min(len(sa), len(sb)) and sa[i] == sb[i]:
        i += 1
    d.notes.append(
        "text apart from numbers differs too, first place — "
        f"…{_squeeze(sa[max(0, i - 60):i + 70])}… vs "
        f"…{_squeeze(sb[max(0, i - 60):i + 70])}…")


def _squeeze(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _walk(a: Any, b: Any, where: str, d: Diff) -> None:
    if isinstance(a, bool) or isinstance(b, bool):
        if a != b:
            d.structural.append(f"{where}: {a!r} vs {b!r}")
        return
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        d.seen += 1
        if a != b or type(a) is not type(b):
            if a != b:
                d.note(where, a, b)
            # A different TYPE with an equal value (3 vs 3.0) moves no numbers —
            # exactly the case the comparison is not byte-level for.
        return
    if isinstance(a, dict) and isinstance(b, dict):
        ka, kb = set(a), set(b)
        if ka != kb:
            d.structural.append(
                f"{where}: keys differ (+{sorted(ka - kb)[:5]} / -{sorted(kb - ka)[:5]})")
        for k in sorted(ka & kb, key=str):
            _walk(a[k], b[k], f"{where}.{k}", d)
        return
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            d.structural.append(f"{where}: length {len(a)} vs {len(b)}")
        for i in range(min(len(a), len(b))):
            _walk(a[i], b[i], f"{where}[{i}]", d)
        return
    if a != b:
        d.structural.append(f"{where}: {a!r} vs {b!r}")


def _cells(data: bytes, delim: str) -> list[list[str]]:
    text = data.decode("utf-8", errors="replace")
    return list(csv.reader(io.StringIO(text), delimiter=delim))


def _compare_cells(ra: list[list[str]], rb: list[list[str]], d: Diff) -> None:
    if len(ra) != len(rb):
        d.structural.append(f"rows {len(ra)} vs {len(rb)}")
    for r in range(min(len(ra), len(rb))):
        if len(ra[r]) != len(rb[r]):
            d.structural.append(f"row {r + 1}: columns {len(ra[r])} vs {len(rb[r])}")
        for c in range(min(len(ra[r]), len(rb[r]))):
            x, y = ra[r][c], rb[r][c]
            if x == y:
                if _as_number(x) is not None:
                    d.seen += 1
                continue
            nx, ny = _as_number(x), _as_number(y)
            if nx is not None and ny is not None:
                d.seen += 1
            if nx is None or ny is None:
                d.structural.append(f"cell r{r + 1}c{c + 1}: {x!r} vs {y!r}")
            else:
                d.note(f"r{r + 1}c{c + 1}", nx, ny)


def _as_number(s: str):
    # A comma as decimal separator is NOT guessed: "1,5" may be a number or a
    # separator in a cell's text, and a guessed parse would turn a difference
    # into silence.
    t = s.strip()
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        return None


def compare_output(name: str, a: bytes, b: bytes, decoders: list[Callable]) -> Diff:
    d = Diff(name)
    d.sizes = (len(a), len(b))
    if a == b:
        d.bytes_equal = True
        d.decoded_as = "bytes"
        return d

    # Packing is stripped recursively: a gzip header carries mtime and name, it
    # differs on its own and says nothing about the numbers inside.
    cur_name, ca, cb = name, a, b
    while True:
        ua = _decompress(cur_name, ca)
        ub = _decompress(cur_name, cb)
        if ua is None or ub is None:
            break
        cur_name, ca = ua
        _, cb = ub
    if ca == cb:
        # Inside are identical bytes, only the wrapper differs (gzip writes mtime
        # and name into the header). Not a single number moved, but this does not
        # give PASS: by contract PASS is only ever byte-level.
        d.decoded_as = "decompressed bytes"
        d.notes.append("identical after decompression — only the packing headers differ")
        return d

    low = cur_name.lower()
    try:
        if low.endswith(".json"):
            d.decoded_as = "json"
            _walk(json.loads(ca.decode("utf-8")), json.loads(cb.decode("utf-8")), "$", d)
            return d
        if low.endswith(".csv"):
            d.decoded_as = "csv"
            _compare_cells(_cells(ca, ","), _cells(cb, ","), d)
            return d
        if low.endswith(".tsv"):
            d.decoded_as = "tsv"
            _compare_cells(_cells(ca, "\t"), _cells(cb, "\t"), d)
            return d
        if low.endswith((".html", ".htm", ".txt", ".md", ".svg")):
            # Crude on purpose: the owner cares about exactly one question —
            # "are the numbers in the report the same?". Comparing markup is
            # pointless, its order and whitespace are not part of any engine contract.
            d.decoded_as = "numeric literals of the document"
            ta, nta = _mask_timestamps(ca.decode("utf-8", errors="replace"))
            tb, ntb = _mask_timestamps(cb.decode("utf-8", errors="replace"))
            if nta or ntb:
                d.notes.append(
                    f"timestamps masked: {nta} / {ntb} "
                    "(launch time, not a computed result — see _TS_RE)")
            na, ctx = _numbers_in_text(ta)
            nb, _ = _numbers_in_text(tb)
            if len(na) != len(nb):
                d.structural.append(f"numeric literals {len(na)} vs {len(nb)}")
            d.seen += min(len(na), len(nb))
            for i in range(min(len(na), len(nb))):
                if na[i] != nb[i]:
                    d.note(f"#{i} «…{ctx[i]}…»", na[i], nb[i])
            # The text BETWEEN numbers is checked too — as a separate note, not a
            # verdict. Caught on the first real run: slreport embeds the input
            # folder path into the snapshot ("/inbox" in wasm vs the analyst's real
            # directory natively). There are no digits there, the numeric comparison
            # would pass it by and the report would say "only the clock differed" —
            # which would be untrue. The comparison is cheap: a skeleton with numbers
            # cut out and the first place of divergence, no diff over 230 KB.
            _note_text_drift(d, ta, tb)
            return d
    except Exception as exc:                      # a broken output is a fact too
        d.structural.append(f"could not parse as {d.decoded_as or low}: {exc}")
        return d

    for dec in decoders:
        try:
            _walk(dec(cur_name, ca), dec(cur_name, cb), "$", d)
            d.decoded_as = f"decoder {getattr(dec, '__qualname__', dec)}"
            return d
        except Exception:
            continue

    # A foreign columnar format (slreport's slot is `*.bin.gz`) we do NOT understand.
    # Saying "probably the same" would be a lie, hence a failure.
    d.opaque = (f"{cur_name}: opaque binary format, bytes differ "
                f"({len(ca)} vs {len(cb)} B) — supply --decoder")
    return d


def load_decoder(spec: str) -> Callable:
    mod_name, _, fn_name = spec.partition(":")
    if not fn_name:
        raise SystemExit(f"--decoder expects module:function, got {spec!r}")
    mod = importlib.import_module(mod_name)
    return getattr(mod, fn_name)


# ──────────────────────────────────────────────────────────────────────────
# Output assembly and verdict
# ──────────────────────────────────────────────────────────────────────────

def declared_outputs(res: dict) -> dict[str, str]:
    """{label: file name} from what the SIDE ITSELF declared in finish()."""
    r = res.get("result") or {}
    out: dict[str, str] = {}
    if r.get("html"):
        out["html"] = r["html"]
    for idx, nm in sorted((r.get("slots") or {}).items(), key=lambda kv: str(kv[0])):
        out[f"slot:{idx}"] = nm
    for idx, nm in sorted((r.get("write_back") or {}).items(), key=lambda kv: str(kv[0])):
        out[f"write_back:{idx}"] = nm
    return out


def selfcheck(side: str, a_dir: Path, a: dict, b_dir: Path, b: dict,
              decoders: list[Callable]) -> tuple[list[str], list[str]]:
    """Does a side reproduce itself? This must be checked BEFORE any claim about
    the other side: a side that is not equal to itself cannot be the reference
    for anything.

    The comparison here is THE SAME layered one used between sides, not a
    byte-level one, and that is a fix, not a relaxation. The very first real run
    hit the fact that slreport stamps wall-clock time into `dashboard.html`
    (`"generated_at":"2026-10-19 13:52"`), and two runs a minute apart were
    declared "not reproducible" — the gate never reached a verdict at all,
    although not a single number in the report moved. Byte strictness that the
    generator physically cannot satisfy measures the clock, not the generator.
    So a FAILURE here is only a moved number, a missing output, differing
    dispositions or an opaque format; a byte difference without a numeric one
    goes into a note the reader sees next to the verdict, deciding for
    themselves whether such "reproducibility" is good enough.
    """
    problems: list[str] = []
    notes: list[str] = []
    da, db = declared_outputs(a), declared_outputs(b)
    if da != db:
        problems.append(f"{side}: two runs declared different outputs: {da} vs {db}")
        return problems, notes
    for label, nm in da.items():
        pa, pb = a_dir / nm, b_dir / nm
        if not pa.exists() or not pb.exists():
            problems.append(f"{side}: {label} ({nm}) is missing in one of the runs")
            continue
        d = compare_output(nm, pa.read_bytes(), pb.read_bytes(), decoders)
        if d.bytes_equal:
            continue
        if d.fatal:
            places = "; ".join(d.places()[:3]) or (d.opaque or "")
            problems.append(
                f"{side}: {label} ({nm}) does not reproduce — its own two runs "
                f"differ in numbers: {places}")
        else:
            notes.append(
                f"{side}: {label} ({nm}) is byte-unstable, but all {d.seen} "
                f"checked numbers matched (compared as \"{d.decoded_as}\")")
    if a.get("dispositions") != b.get("dispositions"):
        problems.append(f"{side}: file dispositions do not reproduce")
    return problems, notes


def verdict(nat_dir: Path, nat: dict, wasm_dir: Path, wasm: dict,
            decoders: list[Callable]) -> tuple[str, list[Diff], list[str]]:
    notes: list[str] = []
    diffs: list[Diff] = []
    fatal = False

    dn, dw = declared_outputs(nat), declared_outputs(wasm)
    if set(dn) != set(dw):
        notes.append(f"output sets differ: native {sorted(dn)}, wasm {sorted(dw)}")
        fatal = True
    for label in sorted(set(dn) & set(dw)):
        na, wa = nat_dir / dn[label], wasm_dir / dw[label]
        if dn[label] != dw[label]:
            notes.append(f"{label}: file name differs ({dn[label]} vs {dw[label]})")
        if not na.exists() or not wa.exists():
            notes.append(f"{label}: output declared but the file is missing "
                         f"({'no native' if not na.exists() else ''}"
                         f"{' / ' if not na.exists() and not wa.exists() else ''}"
                         f"{'no wasm' if not wa.exists() else ''})")
            fatal = True
            continue
        d = compare_output(dn[label], na.read_bytes(), wa.read_bytes(), decoders)
        d.name = f"{label} ({dn[label]})"
        diffs.append(d)
        fatal = fatal or d.fatal

    disp_n = [tuple(x) for x in nat.get("dispositions", [])]
    disp_w = [tuple(x) for x in wasm.get("dispositions", [])]
    if disp_n != disp_w:
        notes.append(f"file dispositions differ: {_first_disp_diff(disp_n, disp_w)}")
        fatal = True

    pn, pw = nat.get("progress_frames"), wasm.get("progress_frames")
    if pn != pw:
        # Progress frames are an indicator, not a report number: they do not
        # affect the verdict, but a difference almost always means another path inside.
        notes.append(f"progress frames {pn} vs {pw} (does not affect the verdict)")

    if fatal:
        return "FAIL", diffs, notes
    if all(d.clean for d in diffs) and not notes:
        return "PASS", diffs, notes
    return "WARN", diffs, notes


def _first_disp_diff(a: list, b: list) -> str:
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x != y:
            return f"#{i}: {x} vs {y}"
    return "length"


def print_report(nat: dict, wasm: dict, state: str, diffs: list[Diff],
                 notes: list[str], nat_probe: dict, wasm_probe: dict) -> None:
    def triple(res: dict, probe: dict) -> str:
        v = res.get("versions") or {}
        src = {**probe, **{k: val for k, val in v.items() if val}}
        return "python {} / pandas {} / numpy {}".format(
            src.get("python", "?"), src.get("pandas", "?"), src.get("numpy", "?"))

    print("\n── Environments ────────────────────────────────────────────────")
    print(f"  native  : {triple(nat, nat_probe)}")
    print(f"  wasm    : {triple(wasm, wasm_probe)}")
    def defint(res: dict, probe: dict) -> str:
        # The runner writes the dtype NAME here ("int32"), the probe the size in
        # bytes; print what there is without pretending it is one quantity.
        v = (res.get("versions") or {}).get("numpy_default_int")
        if v is None:
            v = res.get("numpy_default_int")
        if v is None:
            v = probe.get("array_int_dtype")
        size = probe.get("numpy_default_int")
        return f"{v}" + (f" ({size} B)" if size else "")
    ni, wi = defint(nat, nat_probe), defint(wasm, wasm_probe)
    print(f"  numpy_default_int: native {ni}, wasm {wi}"
          + ("   <-- DIFFERENT BIT WIDTH" if ni != wi else ""))
    print(f"  run time: native {nat.get('elapsed_s', '?')} s, "
          f"wasm {wasm.get('elapsed_s', '?')} s")

    print("\n── Outputs ─────────────────────────────────────────────────────")
    worst_abs = max([d.worst_abs for d in diffs] or [0.0])
    worst_rel = max([d.worst_rel for d in diffs] or [0.0])
    for d in diffs:
        if d.bytes_equal:
            print(f"  = {d.name}: byte-identical")
            continue
        print(f"  ! {d.name}: bytes differ ({d.sizes[0]} vs {d.sizes[1]} B), "
              f"compared as \"{d.decoded_as or '?'}\"")
        if d.opaque:
            print(f"      {d.opaque}")
        for n in d.notes:
            print(f"      {n}")
        shown = d.places()
        if not shown and not d.opaque:
            # The most valuable outcome after PASS: bytes differ, numbers do not.
            # Silently showing "!" and explaining nothing is not acceptable.
            print(f"      numbers checked: {d.seen}, all matched — "
                  f"the byte difference lies OUTSIDE the numbers")
        if shown:
            print(f"      differences: integers {len(d.int_bad)}, "
                  f"floats out of tolerance {len(d.float_bad)}, "
                  f"floats within tolerance {len(d.float_near)}, "
                  f"structural {len(d.structural)}")
            for line in shown[:MAX_SHOWN]:
                print(f"        · {line}")
            if len(shown) > MAX_SHOWN:
                print(f"        … {len(shown) - MAX_SHOWN} more")
    print(f"\n  worst absolute difference: {worst_abs!r}")
    print(f"  worst relative difference: {worst_rel!r} (threshold {REL_TOL})")
    for n in notes:
        print(f"  · {n}")

    print("\n── Verdict ─────────────────────────────────────────────────────")
    explain = {
        "PASS": "all outputs are byte-identical",
        "WARN": "bytes differ, but no number moved beyond the tolerance",
        "FAIL": "numbers differ, an output is missing or the format is opaque",
    }[state]
    print(f"  {state} — {explain}")


# ──────────────────────────────────────────────────────────────────────────
# Comparison layer self-test
# ──────────────────────────────────────────────────────────────────────────

def self_test() -> int:
    """Synthetic file pairs: check that the comparison layer answers what it
    promises BEFORE it is used to measure a real generator."""
    cases: list[tuple[str, str, bytes, bytes, str]] = [
        ("json: bytes equal", "a.json", b'{"x":1}', b'{"x":1}', "clean"),
        ("json: key order", "a.json",
         b'{"x":1,"y":2}', b'{"y":2,"x":1}', "equal-numbers"),
        ("json: integer differs", "a.json", b'{"n":10}', b'{"n":11}', "fatal"),
        ("json: float within tolerance", "a.json",
         b'{"v":1.0000000000000002}', b'{"v":1.0}', "equal-numbers"),
        ("json: float out of tolerance", "a.json",
         b'{"v":1.0000001}', b'{"v":1.0}', "fatal"),
        ("json: key missing", "a.json", b'{"a":1,"b":2}', b'{"a":1}', "fatal"),
        ("gz: header only", "a.json.gz",
         gzip.compress(b'{"v":1}', mtime=1), gzip.compress(b'{"v":1}', mtime=2),
         "equal-numbers"),
        ("gz: number inside", "a.json.gz",
         gzip.compress(b'{"v":1}'), gzip.compress(b'{"v":2}'), "fatal"),
        ("csv: float cell within tolerance", "a.csv",
         b"n;x\n1,1.0000000000000002\n".replace(b";", b","),
         b"n,x\n1,1.0\n", "equal-numbers"),
        ("csv: integer cell", "a.csv", b"n\n7\n", b"n\n8\n", "fatal"),
        ("csv: row missing", "a.csv", b"n\n1\n2\n", b"n\n1\n", "fatal"),
        ("html: different markup, same numbers", "r.html",
         b"<b>  42 </b><i>3.5</i>", b"<b>42</b>\n<i>3.5</i>", "equal-numbers"),
        ("html: number moved", "r.html",
         b"<b>42</b>", b"<b>43</b>", "fatal"),
        ("html: more numbers", "r.html",
         b"<b>42</b>", b"<b>42</b><i>1</i>", "fatal"),
        ("bin: opaque format", "slot.bin.gz",
         gzip.compress(b"\x00\x01\x02"), gzip.compress(b"\x00\x01\x03"), "opaque"),
    ]
    ok = True
    print("── Comparison layer self-test ──────────────────────────────────")
    for title, name, a, b, expect in cases:
        d = compare_output(name, a, b, [])
        if expect == "clean":
            got = "clean" if d.clean else "other"
        elif expect == "opaque":
            got = "opaque" if d.opaque else "other"
        elif expect == "fatal":
            got = "fatal" if d.fatal else "other"
        else:  # equal-numbers: bytes differ, numbers do not
            got = "equal-numbers" if (not d.clean and not d.fatal) else "other"
        mark = "ok  " if got == expect else "BAD "
        ok = ok and got == expect
        detail = ""
        if d.fatal or d.float_near:
            detail = "; ".join(d.places()[:2])
        print(f"  {mark} {title:<38} → {got:<14} {detail}")

    # Separately: a decoder for a "foreign" format — the very --decoder switch.
    def _dec(name: str, data: bytes):
        return {"cells": list(data)}
    d = compare_output("slot.bin.gz", gzip.compress(bytes([1, 2, 3])),
                       gzip.compress(bytes([1, 2, 4])), [_dec])
    got = "fatal" if (d.fatal and not d.opaque) else "other"
    ok = ok and got == "fatal"
    print(f"  {'ok  ' if got == 'fatal' else 'BAD '} "
          f"{'bin: same format via --decoder':<38} → {got:<14} "
          + "; ".join(d.places()[:1]))
    print("  RESULT:", "all expectations met" if ok else "THERE ARE MISMATCHES")
    return 0 if ok else 1


# ──────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", help="generator bundle zip")
    ap.add_argument("--dir", help="folder with the input workbooks")
    ap.add_argument("--out", help="where to put the outputs (default: a temporary directory)")
    ap.add_argument("--python", default=sys.executable, help="interpreter for the native side")
    ap.add_argument("--node", default="node")
    ap.add_argument("--repeat", type=int, default=1,
                    help="run each side N times and check reproducibility first")
    ap.add_argument("--probe", dest="probe", action="store_true", default=True)
    ap.add_argument("--no-probe", dest="probe", action="store_false",
                    help="do not print the bit-width probe (printed by default)")
    ap.add_argument("--decoder", action="append", default=[],
                    help="module:function — decoder for an opaque format, may be repeated")
    ap.add_argument("--r2host", default=str(ROOT / "agent" / "r2host.py"))
    ap.add_argument("--runner", default=str(ROOT / "tools" / "run_bundle_pyodide.mjs"))
    ap.add_argument("--pyodide-base", default=str(ROOT / "static" / "pyodide"))
    ap.add_argument("--self-test", action="store_true",
                    help="check the comparison layer on synthetic pairs and exit")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.bundle or not args.dir:
        ap.error("--bundle and --dir are required (or --self-test)")

    out = Path(args.out).resolve() if args.out else Path(tempfile.mkdtemp(prefix="engine-parity-"))
    out.mkdir(parents=True, exist_ok=True)
    print(f"work directory: {out}")

    bundle_zip = Path(args.bundle).resolve()
    folder = Path(args.dir).resolve()
    bundle_dir = out / "bundle"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    manifest = unpack_bundle(bundle_zip, bundle_dir)

    files = select_files(folder, manifest)
    spec = {
        "bundle_dir": str(bundle_dir),
        "entrypoint": (manifest.get("entrypoint") or {}).get("module", ""),
        "schema_version": schema_version_of(bundle_zip),
        "dir": str(folder),
        "outputs": {
            "html": bool((manifest.get("outputs") or {}).get("html")),
            "slots": {str(s.get("index")): s.get("name")
                      for s in (manifest.get("outputs") or {}).get("slots", [])},
        },
        "files": files,
    }
    files_json = out / "files.json"
    files_json.write_text(json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "run-spec.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{manifest.get('name')} {manifest.get('version')}: selected {len(files)} files "
          f"by masks {[i.get('match') for i in manifest.get('inputs', [])]}")

    nat_probe: dict = {}
    wasm_probe: dict = {}
    if args.probe:
        nat_probe = probe_native(args.python)
        wasm_probe = probe_wasm(args.node, Path(args.pyodide_base), out)
        print_probe(nat_probe, wasm_probe)

    decoders = [load_decoder(s) for s in args.decoder]
    reps = max(1, args.repeat)

    # First EACH side against itself: a side that is not equal to itself has no
    # right to confirm or refute the other.
    nat_runs: list[tuple[Path, dict]] = []
    for i in range(reps):
        d = out / ("native" if i == 0 else f"native-{i + 1}")
        nat_runs.append((d, run_native(spec, d, args.python, Path(args.r2host))))
    wasm_runs: list[tuple[Path, dict]] = []
    for i in range(reps):
        d = out / ("wasm" if i == 0 else f"wasm-{i + 1}")
        wasm_runs.append((d, run_wasm(files_json, d, args.node, Path(args.runner),
                                      bundle_zip, Path(args.pyodide_base),
                                      spec["schema_version"])))

    unstable: list[str] = []
    wobbly: list[str] = []
    for i in range(1, reps):
        for side, runs in (("native", nat_runs), ("wasm", wasm_runs)):
            bad, soft = selfcheck(side, *runs[0], *runs[i], decoders)
            unstable += bad
            wobbly += soft
    if unstable:
        print("\n── Reproducibility ─────────────────────────────────────────────")
        for u in unstable:
            print(f"  !! {u}")
        print("\n── Verdict ─────────────────────────────────────────────────────")
        print("  FAIL — a side does not reproduce itself; sides were not compared")
        return 1
    if reps > 1:
        print("\n── Reproducibility ─────────────────────────────────────────────")
        print(f"  {reps} runs per side, numbers matched in every output")
        for w in dict.fromkeys(wobbly):
            print(f"  · {w}")

    state, diffs, notes = verdict(*nat_runs[0], *wasm_runs[0], decoders)
    print_report(nat_runs[0][1], wasm_runs[0][1], state, diffs, notes, nat_probe, wasm_probe)
    return 0 if state in ("PASS", "WARN") else 1


if __name__ == "__main__":
    sys.exit(main())
