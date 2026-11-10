#!/usr/bin/env python3
"""Single entry point for every OnePage test layer.

    python3 tests/run_all.py [--only LAYERS] [--jobs N] [-k SUBSTR]
                             [--list] [--junit PATH] [--fail-fast] [--chrome]

Design notes that are load-bearing, not taste:

* The python smokes are standalone scripts, not pytest. Each one mutates
  os.environ before importing app.main, and settings is a module-level
  singleton, so two smokes in one interpreter would trample each other.
  Every test therefore gets its own subprocess.
* Parallelism is safe anyway: each smoke makes its own tempfile.mkdtemp and
  only ever talks to the app through TestClient, so no ports are bound.
* agent/tests/* need PYTHONPATH=. or they raise ModuleNotFoundError.
* A skipped layer is always printed with the reason. A silent skip reads as
  success, which is the worst outcome of all.

Only the standard library is used.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parent.parent

LAYERS = [
    "tools",
    "python",
    "node",
    "engine2",
    "engine2-wasm",
    "engine2-parser",
    "chrome",
]

DEFAULT_TIMEOUT = 600

# SAML is off for tests: dev SAML settings in .env would make preflight refuse
# to start, and python3-saml lives in requirements-saml.txt, not the base set.
SAML_OFF = {
    "OP_SAML_IDP_ENTITY_ID": "",
    "OP_SAML_IDP_SSO_URL": "",
    "OP_SAML_IDP_CERT": "",
}


def interpreter() -> str:
    """Python used for the test subprocesses: $PY, repo .venv, or ours."""
    env = os.environ.get("PY")
    if env:
        return env
    venv = ROOT / ".venv" / "bin" / "python"
    if venv.is_file():
        return str(venv)
    return sys.executable


@dataclass
class Task:
    layer: str
    name: str
    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # Tasks that are a loop over many files (node --check) run a callable
    # instead of a single subprocess, so the table keeps one row per concern.
    func: Callable[[int], tuple[bool, str]] | None = None


@dataclass
class Result:
    task: Task
    ok: bool
    seconds: float
    output: str
    reason: str = ""  # non-empty for a timeout or a missing runner


@dataclass
class Skip:
    layer: str
    reason: str


def js_sources() -> list[Path]:
    files = [
        p
        for p in sorted((ROOT / "static" / "js").rglob("*.js"))
        if "vendor" not in p.relative_to(ROOT / "static" / "js").parts
    ]
    return files


def node_check_all(timeout: int) -> tuple[bool, str]:
    """node --check over static/js, one file per call.

    Two traps live here, both of which report a clean lint over broken
    JavaScript: find's -exec with a trailing semicolon discards node's
    exit code, and `node
    --check` with several paths checks only the first and ignores the rest.
    Hence the explicit loop.
    """
    broken: list[str] = []
    lines: list[str] = []
    files = js_sources()
    for path in files:
        rel = path.relative_to(ROOT)
        try:
            proc = subprocess.run(
                ["node", "--check", str(rel)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            broken.append(str(rel))
            lines.append(f"{rel}: node --check timed out after {timeout}s")
            continue
        if proc.returncode != 0:
            broken.append(str(rel))
            lines.append(f"{rel}:\n{proc.stdout}{proc.stderr}".rstrip())
    if broken:
        head = f"{len(broken)} of {len(files)} files failed node --check:"
        return False, "\n".join([head, *lines])
    return True, f"{len(files)} files checked"


def build_tasks(only: Iterable[str], pattern: str | None, want_chrome: bool):
    """Return (tasks, skips). Skips carry the reason they were skipped."""
    py = interpreter()
    tasks: list[Task] = []
    skips: list[Skip] = []
    wanted = list(only)

    def enabled(layer: str) -> bool:
        return layer in wanted

    # ── tools ──────────────────────────────────────────────────────────────
    if enabled("tools"):
        tool_cmds = [
            ("i18n_check --stats", [py, "tools/i18n_check.py", "--stats"]),
            ("learn_balance", [py, "tools/learn_balance.py"]),
            ("learn_selfcontained", [py, "tools/learn_selfcontained.py"]),
            ("learn_fixture_delta --check",
             [py, "tools/learn_fixture_delta.py", "--check"]),
            ("learn_fixture_anon --check",
             [py, "tools/learn_fixture_anon.py", "--check"]),
            ("learn_fixture_report --check",
             [py, "tools/learn_fixture_report.py", "--check"]),
            ("public_sanitize --check .",
             [py, "tools/public_sanitize.py", "--check", "."]),
            # The engineering guide lives in two files, and the html is built
            # from the markdown. Without this check the html fell three and a
            # half months behind and described features that had since
            # changed — silently, because nothing asked.
            ("report_guide_html --check",
             [py, "tools/report_guide_html.py", "--check"]),
        ]
        for name, argv in tool_cmds:
            tasks.append(Task("tools", name, argv=argv))
        tasks.append(
            Task(
                "tools",
                f"node --check static/js ({len(js_sources())} files)",
                func=node_check_all,
            )
        )

    # ── python ─────────────────────────────────────────────────────────────
    if enabled("python"):
        for path in sorted((ROOT / "tests").glob("smoke_*.py")):
            rel = path.relative_to(ROOT)
            tasks.append(Task("python", str(rel), argv=[py, str(rel)]))
        for path in sorted((ROOT / "agent" / "tests").glob("smoke_*.py")):
            rel = path.relative_to(ROOT)
            # PYTHONPATH=. or these raise ModuleNotFoundError.
            tasks.append(
                Task("python", str(rel), argv=[py, str(rel)],
                     env={"PYTHONPATH": "."})
            )

    # ── node ───────────────────────────────────────────────────────────────
    if enabled("node"):
        for path in sorted((ROOT / "tests").glob("*.js")):
            if path.name.startswith("_"):
                continue  # tests/_i18n_stub.js is a helper, not a test
            rel = path.relative_to(ROOT)
            tasks.append(Task("node", str(rel), argv=["node", str(rel)]))

    # ── engine2 ────────────────────────────────────────────────────────────
    if enabled("engine2"):
        tasks.append(
            Task(
                "engine2",
                "tests/engine2/guard.test.mjs",
                argv=["node", "--test", "tests/engine2/guard.test.mjs"],
            )
        )

    wasm_dir = ROOT / "static" / "duckdb-wasm"
    arrow_dir = ROOT / "node_modules" / "apache-arrow"
    if enabled("engine2-wasm"):
        missing = []
        if not wasm_dir.is_dir():
            missing.append("static/duckdb-wasm — make assets")
        if not arrow_dir.is_dir():
            missing.append("node_modules/apache-arrow — npm install")
        if missing:
            skips.append(Skip("engine2-wasm", "missing " + "; ".join(missing)))
        else:
            for path in sorted((ROOT / "tests" / "engine2").glob("*.test.mjs")):
                if path.name in ("guard.test.mjs", "parser.test.mjs"):
                    continue  # own layers
                rel = path.relative_to(ROOT)
                tasks.append(
                    Task("engine2-wasm", str(rel),
                         argv=["node", "--test", str(rel)])
                )

    if enabled("engine2-parser"):
        xlsx = ROOT / "static" / "xlsx-arrow" / "xlsx_arrow_bg.wasm"
        if not xlsx.is_file():
            skips.append(
                Skip("engine2-parser",
                     "missing static/xlsx-arrow/xlsx_arrow_bg.wasm — "
                     "make assets-wasm")
            )
        else:
            tasks.append(
                Task("engine2-parser", "tests/engine2/parser.test.mjs",
                     argv=["node", "--test", "tests/engine2/parser.test.mjs"])
            )

    # ── chrome ─────────────────────────────────────────────────────────────
    if enabled("chrome"):
        chrome_bin = os.environ.get("CHROME_BIN")
        if not (want_chrome or chrome_bin):
            skips.append(
                Skip("chrome",
                     "needs a real browser — pass --chrome or set CHROME_BIN")
            )
        else:
            for path in sorted((ROOT / "tests" / "chrome").glob("smoke_*_chrome.py")):
                rel = path.relative_to(ROOT)
                tasks.append(Task("chrome", str(rel), argv=[py, str(rel)]))

    if pattern:
        before = len(tasks)
        tasks = [t for t in tasks if pattern in t.name]
        if before and not tasks:
            skips.append(Skip("-k", f"no test name contains {pattern!r}"))

    return tasks, skips


def run_task(task: Task, timeout: int) -> Result:
    started = time.monotonic()
    if task.func is not None:
        try:
            ok, output = task.func(timeout)
        except Exception as exc:  # a broken helper must not read as a pass
            ok, output = False, f"{type(exc).__name__}: {exc}"
        return Result(task, ok, time.monotonic() - started, output)

    env = dict(os.environ)
    env.update(SAML_OFF)
    env.update(task.env)
    runner = task.argv[0]
    if shutil.which(runner) is None and not Path(runner).is_file():
        return Result(
            task, False, 0.0,
            f"runner {runner!r} not found on PATH",
            reason=f"{runner} is not installed",
        )
    try:
        proc = subprocess.run(
            task.argv,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return Result(
            task, False, time.monotonic() - started,
            out + f"\n[timeout] killed after {timeout}s",
            reason=f"timed out after {timeout}s",
        )
    output = proc.stdout + proc.stderr
    if proc.returncode != 0 and not output.strip():
        output = f"[exit {proc.returncode} with no output]"
    return Result(task, proc.returncode == 0, time.monotonic() - started, output)


def _xml_safe(text: str) -> str:
    """Drop characters XML cannot carry.

    ElementTree writes them out happily and the file then fails to parse —
    an artefact no JUnit consumer can read, produced exactly when something
    already went wrong. ANSI colour (\x1b) is the common case.
    """
    return "".join(ch for ch in text
                   if ch in "\t\n\r" or 0x20 <= ord(ch) < 0xD800 or 0xE000 <= ord(ch))


def write_junit(path: str, results: list[Result], skips: list[Skip]) -> None:
    suites = ET.Element("testsuites")
    by_layer: dict[str, list[Result]] = {}
    for res in results:
        by_layer.setdefault(res.task.layer, []).append(res)
    for layer, rows in by_layer.items():
        failures = sum(1 for r in rows if not r.ok)
        suite = ET.SubElement(
            suites, "testsuite",
            name=layer,
            tests=str(len(rows)),
            failures=str(failures),
            errors="0",
            skipped="0",
            time=f"{sum(r.seconds for r in rows):.3f}",
        )
        for res in rows:
            case = ET.SubElement(
                suite, "testcase",
                classname=layer,
                name=res.task.name,
                time=f"{res.seconds:.3f}",
            )
            if not res.ok:
                failure = ET.SubElement(
                    case, "failure",
                    message=res.reason or "failed",
                    type="failure",
                )
                failure.text = _xml_safe(res.output[-20000:])
    for skip in skips:
        suite = ET.SubElement(
            suites, "testsuite",
            name=skip.layer,
            tests="1", failures="0", errors="0", skipped="1", time="0.000",
        )
        case = ET.SubElement(
            suite, "testcase", classname=skip.layer, name=f"{skip.layer} (layer)",
            time="0.000",
        )
        ET.SubElement(case, "skipped", message=skip.reason)
    ET.ElementTree(suites).write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tests/run_all.py",
        description="Run every OnePage test layer.",
    )
    parser.add_argument(
        "--only",
        default=",".join(LAYERS),
        help="comma-separated layers: " + ", ".join(LAYERS),
    )
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4,
                        help="parallel subprocesses (default: CPU count)")
    parser.add_argument("-k", dest="pattern", default=None,
                        help="only tests whose name contains SUBSTR")
    parser.add_argument("--list", action="store_true",
                        help="print what would run, then exit")
    parser.add_argument("--junit", metavar="PATH", help="write JUnit XML here")
    parser.add_argument("--fail-fast", action="store_true",
                        help="stop after the first failure")
    parser.add_argument("--chrome", action="store_true",
                        help="include the chrome layer (needs a browser)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"per-test timeout in seconds (default {DEFAULT_TIMEOUT})")
    args = parser.parse_args()

    only = [x.strip() for x in args.only.split(",") if x.strip()]
    unknown = [x for x in only if x not in LAYERS]
    if unknown:
        parser.error(
            f"unknown layer(s): {', '.join(unknown)}. Known: {', '.join(LAYERS)}"
        )

    tasks, skips = build_tasks(only, args.pattern, args.chrome)

    if args.list:
        print(f"interpreter: {interpreter()}")
        for layer in LAYERS:
            rows = [t for t in tasks if t.layer == layer]
            if rows:
                print(f"\n{layer} ({len(rows)}):")
                for task in rows:
                    print(f"  {task.name}")
        if skips:
            print("\nskipped:")
            for skip in skips:
                print(f"  {skip.layer}: {skip.reason}")
        print(f"\n{len(tasks)} tests would run with --jobs {args.jobs}.")
        return 0

    if not tasks:
        # Zero tests run is ALWAYS a failure, even when everything was formally
        # "skipped for a reason". Otherwise a typo in -k, or a layer that
        # collected nothing but skips, gives a green run that checked nothing —
        # exactly the silent success this runner was written against.
        print("Nothing ran.")
        for skip in skips:
            print(f"  {skip.layer}: {skip.reason}")
        print("A run that executes no test is a failure, not a pass.")
        return 1

    print(f"Running {len(tasks)} tests across "
          f"{len({t.layer for t in tasks})} layer(s), --jobs {args.jobs}, "
          f"interpreter {interpreter()}")

    results: list[Result] = []
    stopped = False
    wall = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [(pool.submit(run_task, t, args.timeout), t) for t in tasks]
        # Collected in submission order so the table is stable. A fail-fast
        # cancel can only stop tests that have not started yet.
        for future, _task in futures:
            if stopped:
                future.cancel()
                continue
            res = future.result()
            results.append(res)
            sys.stdout.write("." if res.ok else "F")
            sys.stdout.flush()
            if args.fail_fast and not res.ok:
                stopped = True
    elapsed = time.monotonic() - wall
    print()

    failed = [r for r in results if not r.ok]

    # Only failures print their stdout: 95 passing logs would bury the one
    # that matters.
    for res in failed:
        print("\n" + "=" * 72)
        head = f"FAIL  [{res.task.layer}] {res.task.name}"
        if res.reason:
            head += f"  ({res.reason})"
        print(head)
        print("=" * 72)
        text = res.output.rstrip()
        print(text if text else "(no output)")

    print("\n" + "-" * 72)
    print(f"{'LAYER':<15} {'NAME':<44} {'STATUS':<7} {'SEC':>7}")
    print("-" * 72)
    for res in results:
        name = res.task.name
        if len(name) > 44:
            name = "…" + name[-43:]
        print(f"{res.task.layer:<15} {name:<44} "
              f"{'ok' if res.ok else 'FAIL':<7} {res.seconds:>7.1f}")
    print("-" * 72)

    if skips:
        print("\nSkipped layers (each with its reason):")
        for skip in skips:
            print(f"  {skip.layer}: skipped, {skip.reason}")

    not_run = len(tasks) - len(results)
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed"
          + (f", {not_run} not run (--fail-fast)" if not_run else "")
          + (f", {len(skips)} layer(s) skipped" if skips else "")
          + f" in {elapsed:.1f}s wall")
    if failed:
        print("Failed:")
        for res in failed:
            print(f"  [{res.task.layer}] {res.task.name}"
                  + (f" — {res.reason}" if res.reason else ""))

    if args.junit:
        write_junit(args.junit, results, skips)
        print(f"JUnit XML: {args.junit}")

    return 1 if failed or not_run else 0


if __name__ == "__main__":
    sys.exit(main())
