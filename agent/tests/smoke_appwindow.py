"""Smoke test of the library window: app when Edge is found, browser on any trouble.

We have no Windows, so the real msedge launch is checked by nothing except a
live machine. What is checked is what does not depend on it: the CHOICE of path
and the COMPOSITION of the arguments. The search, the launch and the fallback
tab are replaced by appwindow hooks.

Run:  python -m agent.tests.smoke_appwindow   (from the OnePage repo root)
"""
from __future__ import annotations

import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from agent import appwindow

_PASS = _FAIL = 0


def check(label, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"PASS {label}")
    else:
        _FAIL += 1
        print(f"FAIL {label}  {extra}")


URL = "http://127.0.0.1:47113/library/?a=1&b=two words"


def run(find, launch=None, fallback=None, root=None):
    """open() with substitutions; returns (result, launch calls, fallback calls, log)."""
    launched, opened = [], []
    buf = io.StringIO()
    with redirect_stdout(buf):
        res = appwindow.open_window(
            URL, state_root=root, title="OnePage", find=find,
            launch=launch or launched.append,
            fallback=fallback or opened.append)
    return res, launched, opened, buf.getvalue()


with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "state"
    fake = Path(td) / "msedge.exe"
    fake.write_bytes(b"")

    # 1. Edge found -> app window, no tab is opened.
    res, launched, opened, log = run(lambda: fake, root=root)
    check("Edge found -> app", res == "app", res)
    check("exactly one process launched", len(launched) == 1, launched)
    check("no tab was opened", opened == [], opened)
    args = launched[0] if launched else []
    check("the first argument is Edge itself", args[:1] == [str(fake)], args)
    check("has --app=<url>", f"--app={URL}" in args, args)
    # The address goes as one list element and without quotes: list2cmdline
    # escapes by itself, manual quotes would turn into double escaping.
    check("url is not escaped and has no quotes",
          not any('"' in a or "\\" + "&" in a for a in args)
          and sum(URL in a for a in args) == 1, args)
    prof = [a for a in args if a.startswith("--user-data-dir=")]
    check("its own --user-data-dir", len(prof) == 1, args)
    if prof:
        p = Path(prof[0].split("=", 1)[1])
        check("the profile is INSIDE state_root", root in p.parents, p)
        check("the profile is not equal to state_root", p != root, p)
    check("window size and no first-run wizard",
          "--window-size=1280,860" in args and "--no-first-run" in args, args)
    check("the log names the chosen path", "app window" in log, log)

    # 2. Edge not found -> tab.
    res, launched, opened, log = run(lambda: None, root=root)
    check("Edge not found -> browser", res == "browser", res)
    check("no process was launched", launched == [], launched)
    check("the tab is opened with the same url", opened == [URL], opened)
    check("the log names the reason", "Edge not found" in log, log)

    # 3. Launch failed -> does not propagate, the tab is opened.
    def boom(_args):
        raise OSError("access denied")
    try:
        res, _, opened, log = run(lambda: fake, launch=boom, root=root)
        raised = False
    except Exception:                                    # noqa: BLE001
        raised = True
    check("the launch exception did not propagate", not raised)
    check("launch failed -> browser + tab",
          not raised and res == "browser" and opened == [URL], (res, opened))
    check("the log names the failure reason", "launch failed" in log, log)

    # 4. Both the search and the fallback tab fail: the "never raises" contract.
    def bad_find():
        raise RuntimeError("find")

    def bad_fb(_u):
        raise RuntimeError("fb")
    try:
        res, *_ = run(bad_find, fallback=bad_fb, root=root)
        raised = False
    except Exception:                                    # noqa: BLE001
        raised = True
    check("both the search and the tab fail -> still browser",
          not raised and res == "browser")

    # 5. state_root=None (the server has not filled it yet) does not raise.
    res, launched, opened, _ = run(lambda: fake, root=None)
    check("state_root=None -> browser, without an exception",
          res == "browser" and launched == [] and opened == [URL], res)

    # 6. Module-level hooks are read at call time.
    saved = appwindow._launch, appwindow._fallback
    got = []
    appwindow._launch = got.append
    appwindow._fallback = got.append
    try:
        with redirect_stdout(io.StringIO()):
            r = appwindow.open_window(URL, state_root=root, title="t", find=lambda: fake)
    finally:
        appwindow._launch, appwindow._fallback = saved
    check("substituting the module-level _launch works", r == "app" and len(got) == 1, (r, got))

    # 7. find_edge: directory order and the registry.
    pf = Path(td) / "pf"
    pf86 = Path(td) / "pf86"
    la = Path(td) / "la"
    reg_exe = Path(td) / "elsewhere" / "msedge.exe"
    for base in (pf, pf86, la):
        (base / "Microsoft/Edge/Application").mkdir(parents=True)
    env = {"ProgramFiles": str(pf), "ProgramFiles(x86)": str(pf86),
           "LOCALAPPDATA": str(la)}
    check("nothing there -> None", appwindow.find_edge(env=env, registry=lambda: []) is None)
    (pf86 / "Microsoft/Edge/Application/msedge.exe").write_bytes(b"")
    check("finds it in ProgramFiles(x86)",
          appwindow.find_edge(env=env, registry=lambda: []) == pf86 / appwindow._REL)
    (la / "Microsoft/Edge/Application/msedge.exe").write_bytes(b"")
    check("ProgramFiles(x86) comes before LOCALAPPDATA",
          appwindow.find_edge(env=env, registry=lambda: []) == pf86 / appwindow._REL)
    (pf86 / "Microsoft/Edge/Application/msedge.exe").unlink()
    (la / "Microsoft/Edge/Application/msedge.exe").unlink()
    reg_exe.parent.mkdir()
    reg_exe.write_bytes(b"")
    check("finds it through the registry",
          appwindow.find_edge(env=env, registry=lambda: [str(reg_exe)]) == reg_exe)
    check("a dead registry entry is ignored",
          appwindow.find_edge(env=env, registry=lambda: [str(Path(td) / "gone.exe")]) is None)

    def bad_reg():
        raise OSError("reg")
    check("a registry failure does not bring the search down",
          appwindow.find_edge(env=env, registry=bad_reg) is None)

# 8. Wiring: open_library must go through appwindow.open_window, not around it.
# This very line was once the "sucker" (webbrowser.open right in the
# controller); the test goes red if it is brought back.
from agent import controller as _ctl                            # noqa: E402


class _Srv:
    server_address = ("127.0.0.1", 47114)
    state_root = Path("/tmp/op-state-stub")


c = _ctl.AgentController.__new__(_ctl.AgentController)
c.server = _Srv()
seen = {}
_saved_open = appwindow.open_window
appwindow.open_window = lambda url, **kw: seen.update(url=url, **kw) or "app"
try:
    with redirect_stdout(io.StringIO()):
        c.open_library()
finally:
    appwindow.open_window = _saved_open
check("open_library calls appwindow.open_window with the agent address",
      seen.get("url") == "http://127.0.0.1:47114/library/", seen)
check("open_library passes the server state_root",
      seen.get("state_root") == _Srv.state_root, seen)
check("there is no direct webbrowser in the controller",
      "webbrowser" not in Path(_ctl.__file__).read_text(encoding="utf-8"))

print(f"\n{_PASS} passed, {_FAIL} failed")
sys.exit(1 if _FAIL else 0)
