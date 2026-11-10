"""Smoke test for the comparison layer of tools/engine_parity.py.

Run:  python tests/smoke_engine_parity.py   (from the OnePage repo root)

The parity tool answers one question — "did the generator produce the same
numbers natively and in wasm?" — and answers it with THREE different words.
The cost of a mistake is asymmetric: a false PASS silently lets a divergence
into a report, a false FAIL trains the reader to scroll past red. So the
boundary between verdicts is pinned here, on synthetic pairs whose right
answer is known in advance, not on a real run that takes a minute and depends
on package versions.

Four mandatory outcomes (the task requires exactly these):
    equal bytes                          → PASS
    different gzip header, same numbers  → WARN
    a shifted integer                    → FAIL
    an opaque format                     → FAIL
The other checks are the boundaries where those four are easy to confuse:
float tolerance, the wall-clock mask, text drift apart from numbers, and the
PASS/WARN/FAIL fork itself in `verdict()` at the level of whole outputs.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The module lives in tools/ and is not a package — load it by path, not by
# import: scattering __init__.py over the tools directory for one test would
# be wrong, it is not a library.
_spec = importlib.util.spec_from_file_location(
    "engine_parity", ROOT / "tools" / "engine_parity.py")
ep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ep)

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


def outcome(name: str, a: bytes, b: bytes, decoders=()) -> tuple[str, object]:
    """The verdict word for ONE output + the Diff itself to explain a failure."""
    d = ep.compare_output(name, a, b, list(decoders))
    if d.bytes_equal:
        return "PASS", d
    if d.fatal:
        return "FAIL", d
    return "WARN", d


def _why(d) -> str:
    return "; ".join(d.places()[:2]) or (d.opaque or "") or "—"


def main() -> int:
    # ------------------------------------------------------------------ #
    # Four mandatory outcomes.
    # ------------------------------------------------------------------ #
    got, d = outcome("slot.json", b'{"v":1,"n":[1,2,3]}', b'{"v":1,"n":[1,2,3]}')
    check("equal bytes → PASS", got == "PASS", got)

    # The same gzip with another mtime in the header: byte-identical inside.
    got, d = outcome("slot.json.gz",
                     gzip.compress(b'{"v":1,"n":[1,2,3]}', mtime=1),
                     gzip.compress(b'{"v":1,"n":[1,2,3]}', mtime=99999))
    check("different gzip header with the same numbers → WARN", got == "WARN",
          f"{got}: {_why(d)}")
    check("and it says the content matched after decompression",
          any("identical after decompression" in n for n in d.notes), d.notes)

    got, d = outcome("slot.json", b'{"rows":1000}', b'{"rows":1001}')
    check("shifted integer → FAIL", got == "FAIL", got)
    check("and the place and both values are named",
          d.int_bad == [("$.rows", 1000, 1001)], d.int_bad)

    got, d = outcome("slot.bin", b"\x00\x01\x02\x03", b"\x00\x01\x02\x04")
    check("opaque format → FAIL", got == "FAIL", got)
    check("and it is reported as opaque, not as an invented number divergence",
          bool(d.opaque) and not d.int_bad and not d.float_bad, _why(d))

    # ------------------------------------------------------------------ #
    # Boundaries where those four are easy to confuse.
    # ------------------------------------------------------------------ #
    got, _ = outcome("slot.json", b'{"v":1.0}', b'{"v":1.0000000000000002}')
    check("float within 1e-12 → WARN, not FAIL", got == "WARN", got)
    got, _ = outcome("slot.json", b'{"v":1.0}', b'{"v":1.0000001}')
    check("float outside tolerance → FAIL", got == "FAIL", got)

    got, d = outcome("slot.json", b'{"a":1,"b":2}', b'{"a":1}')
    check("missing key → FAIL (structure, not a number)",
          got == "FAIL" and d.structural and not d.int_bad, _why(d))

    got, d = outcome("slot.csv", b"point,n\nA,5\n", b"point,n\nA,5\n")
    check("csv: equal bytes → PASS", got == "PASS", got)
    got, d = outcome("slot.csv", b"point,n\nA,5\n", b"point,n\nA,6\n")
    check("csv: shifted integer cell → FAIL", got == "FAIL", _why(d))

    # Wall clock: not a computed result, so it is masked — otherwise no run of
    # a generator that stamps it would ever reach a verdict.
    got, d = outcome(
        "report.html",
        b'<p>{"generated_at":"2026-10-19 13:55","rows":208220}</p>',
        b'<p>{"generated_at":"2026-10-19 14:02","rows":208220}</p>')
    check("difference only in a timestamp → WARN, not FAIL", got == "WARN",
          f"{got}: {_why(d)}")
    check("and the mask is stated out loud, not applied silently",
          any("timestamps masked" in n for n in d.notes), d.notes)
    got, d = outcome(
        "report.html",
        b'<p>{"generated_at":"2026-10-19 13:55","rows":208220}</p>',
        b'<p>{"generated_at":"2026-10-19 14:02","rows":208221}</p>')
    check("but a real number next to the timestamp still gives FAIL",
          got == "FAIL", _why(d))
    check("the mask is narrow: a year as a standalone number is not masked",
          outcome("report.html", b"<i>2026</i>", b"<i>2027</i>")[0] == "FAIL")

    # Text apart from numbers: this is exactly how the input folder path in the
    # slreport snapshot surfaced ("/inbox" vs the analyst's directory) — there
    # are no numbers in it.
    got, d = outcome("report.html", b'<p>path="/inbox" n=5</p>',
                     b'<p>path="/Users/an/SL" n=5</p>')
    check("text divergence apart from numbers → WARN", got == "WARN", _why(d))
    check("and it gets a note of its own",
          any("text apart from numbers" in n for n in d.notes), d.notes)

    got, d = outcome("slot.bin.gz", gzip.compress(bytes([1, 2, 3])),
                     gzip.compress(bytes([1, 2, 4])),
                     [lambda name, data: {"cells": list(data)}])
    check("an opaque format with --decoder stops being opaque",
          got == "FAIL" and not d.opaque and d.int_bad, _why(d))

    # ------------------------------------------------------------------ #
    # The whole verdict fork: verdict() over two "runs".
    # ------------------------------------------------------------------ #
    tmp = Path(tempfile.mkdtemp(prefix="op-parity-test-"))
    try:
        nat, wasm = tmp / "native", tmp / "wasm"
        nat.mkdir()
        wasm.mkdir()
        res = {"result": {"html": "r.html", "slots": {"0": "s.json.gz"},
                          "write_back": {}},
               "dispositions": [["SL_01.xlsx", "parsed"]],
               "progress_frames": 3}

        def write(side: Path, html: bytes, slot: bytes) -> None:
            (side / "r.html").write_bytes(html)
            (side / "s.json.gz").write_bytes(slot)

        same_html = b"<b>42</b>"
        write(nat, same_html, gzip.compress(b'{"v":1}', mtime=1))
        write(wasm, same_html, gzip.compress(b'{"v":1}', mtime=1))
        state, diffs, notes = ep.verdict(nat, res, wasm, res, [])
        check("both outputs byte-identical → PASS", state == "PASS",
              f"{state} {notes}")

        write(wasm, same_html, gzip.compress(b'{"v":1}', mtime=7))
        state, diffs, notes = ep.verdict(nat, res, wasm, res, [])
        check("slot differs only in its wrapper → WARN", state == "WARN",
              f"{state} {notes}")

        write(wasm, same_html, gzip.compress(b'{"v":2}', mtime=1))
        state, _, notes = ep.verdict(nat, res, wasm, res, [])
        check("a number inside the slot shifted → FAIL", state == "FAIL",
              f"{state} {notes}")

        # A missing output is a separate path: nothing to compare, but no silence.
        write(wasm, same_html, gzip.compress(b'{"v":1}', mtime=1))
        (wasm / "s.json.gz").unlink()
        state, _, notes = ep.verdict(nat, res, wasm, res, [])
        check("a declared output is missing on one side → FAIL",
              state == "FAIL" and any("the file is missing" in n for n in notes), notes)

        # Dispositions are part of the answer: "cached" where the other side
        # read the file means DIFFERENT computations were compared.
        write(wasm, same_html, gzip.compress(b'{"v":1}', mtime=1))
        other = dict(res, dispositions=[["SL_01.xlsx", "cached"]])
        state, _, notes = ep.verdict(nat, res, wasm, other, [])
        check("diverging dispositions → FAIL",
              state == "FAIL" and any("dispositions differ" in n for n in notes), notes)

        # A side's self-check: the clock is forgiven, a number is not.
        a_dir, b_dir = tmp / "run1", tmp / "run2"
        a_dir.mkdir()
        b_dir.mkdir()
        clock = {"result": {"html": "r.html", "slots": {}, "write_back": {}},
                 "dispositions": [], "progress_frames": 1}
        (a_dir / "r.html").write_bytes(b'<i>2026-10-19 13:55</i><b>42</b>')
        (b_dir / "r.html").write_bytes(b'<i>2026-10-19 14:02</i><b>42</b>')
        bad, soft = ep.selfcheck("native", a_dir, clock, b_dir, clock, [])
        check("a side that stamps the clock is NOT declared non-reproducible",
              bad == [] and len(soft) == 1, (bad, soft))
        (b_dir / "r.html").write_bytes(b'<i>2026-10-19 13:55</i><b>43</b>')
        bad, soft = ep.selfcheck("native", a_dir, clock, b_dir, clock, [])
        check("but a shifted number on the same side is", len(bad) == 1, bad)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # File selection: both sides must get ONE list in ONE order.
    # ------------------------------------------------------------------ #
    tmp = Path(tempfile.mkdtemp(prefix="op-parity-sel-"))
    try:
        for n in ["SL_10.xlsx", "SL_02.xlsx", "~$SL_02.xlsx", "notes.txt"]:
            (tmp / n).write_text("x", encoding="utf-8")
        (tmp / "sub").mkdir()
        files = ep.select_files(tmp, {"inputs": [{"match": "SL_*.xlsx"}]})
        check("selection by mask and casefold order",
              [f["name"] for f in files] == ["SL_02.xlsx", "SL_10.xlsx"],
              [f["name"] for f in files])
        check("every entry carries what the engine keys its cache by",
              all({"name", "path", "mtime_ms", "size"} <= set(f) for f in files))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if _FAIL == 0 else 'FAILURES: ' + str(_FAIL)}  ({_PASS} passed)")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
