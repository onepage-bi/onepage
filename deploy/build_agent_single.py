#!/usr/bin/env python3
"""Build a single-file op_agent.py from the agent/ package — the file to carry to a machine.

    python deploy/build_agent_single.py [out.py]
    python deploy/build_agent_single.py --install-resources <kit directory>
    python deploy/build_agent_single.py --check-kit <kit directory>

Why: on an analyst's locked-down machine one file is handier than a package of
eight modules. But copying code by hand is not an option — it would drift from
what was tested. So the file is GENERATED from the same modules the smoke tests
run: the logic is identical by construction.

How it works:
  * modules are concatenated in dependency order;
  * `from __future__ import annotations` is stripped from all of them and put
    once as the first statement of the file (a Python requirement);
  * relative imports (`from . import ...`) are stripped — everything is local now;
  * ordinary imports stay in place (a repeated `import os` is harmless);
  * each module's docstring becomes a section banner comment;
  * cross-module references such as `gpsource.run_sources(...)` in runs.py are
    NOT rewritten — instead the module name becomes a VIEW onto the same flat
    namespace (`_FlatModule`), so names resolve at runtime exactly as before.
    Fewer edits = less risk;
  * a flat namespace means that same-named globals of two modules are one
    name. The build resolves such collisions itself: the name stays with the
    module concatenated first, the second gets a module suffix
    (`URL_PREFIX__libui`) — and whatever cannot be fixed that way makes the
    build refuse. Modules are still written as modules: demanding kit-wide
    uniqueness would push a concatenation constraint onto normal code. A
    dropped symbol import (`from .layout import atomic_write`) goes the same
    way: if the source renamed the name, references in the importing module are
    rewritten to the new one (`_plan_import_renames`), and if they cannot be
    rewritten the build fails. It can never silently bind to a namesake.
Then the single-file main is appended: set-cred/del-cred subcommands, automatic
creation of an agent.ini template next to the script, a psycopg presence check
and the port ladder.

The report mirror (`agent/mirror/`) is concatenated like everything else, but
the CODE is only half of the mirror: the concatenation cannot carry
`strings.json` and the `web/` pages. `--install-resources` puts them into the
kit (the `mirror/` subdirectory next to `op_agent.py` — exactly where
`assets.res_path` looks for them), and `--check-kit` refuses to accept a kit
that lacks them or lacks the mirror code. The kit used to build silently
WITHOUT the mirror, and that surfaced on the user's machine.

`agent/r2host.py` — the R2 contract host for folder runs — travels the same way.
It is not a resource but CODE, yet code that is LAUNCHED as a separate process
(`python -I r2host.py …`) rather than imported, so by construction it never
enters the concatenation: it is not and cannot be in `MODULES`. In the kit it
lies as a SEPARATE file next to `op_agent.py` (`generator._r2_host_path()` looks
for it there), and `--check-kit` compares it with the repository byte for byte.
Without that the kit would build silently, `--check-kit` would stay quiet, and
the very first folder run on the analyst's machine would fail with
`gen_no_host` — exactly the class of defect already seen with `ENTRY_VIEWS`.
"""
from __future__ import annotations

import ast
import builtins
import datetime
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "agent"

# Order = dependency order (config → … → server).
# Order = concatenation order. applog comes before controller/tray: they call
# it by module name through the shim, but top-to-bottom readability wins.
# The mirror subpackage goes in whole and strictly by dependency
# (assets → layout → store → sync → serve → schedule) and MUST precede
# controller/tray: they call `mirror.schedule` by module name. While these six
# names were missing here the kit built without the mirror — silently, because
# the concatenation takes exactly what is listed.
# `folders` and `appwindow` are leaves: both are stdlib-only and reference
# nothing else inside the agent, so they come before everyone who calls them
# (`appwindow` — `controller.open_library()`, `folders` — the schedule and
# runs). Both are mandatory here: the concatenation takes exactly what is
# listed, and `controller.py`, which already imports `appwindow`, breaks the
# build on a name mismatch without this line — so an omission shows up at
# once, not in the kit.
# `foldersio` (writes folders.ini), `s3ui` (bucket panel, writes agent.ini) and
# `foldersui` (the "Report folders" screen)
# come AFTER the mirror and strictly in this order. The order among them is a
# dependency: `foldersui` imports `foldersio`, `folders` and `appwindow`
# (it checks the URL prefix against the latter right at import time). And
# AFTER the mirror because of the flat namespace: the window screen and the
# mirror shell solve the same problem and so name their pieces the same
# (`_roots`, `_get`, `_post`, `_reply`). `_plan_renames` resolves the
# collisions, the name stays with whoever is concatenated EARLIER, and the
# build tail (`_kit_problems`) and the mirror markers call exactly the MIRROR
# version. Put the screen higher and `res_path` in the kit becomes the
# window's, with a different signature: the kit builds, passes --check-kit and
# crashes on launch (verified — that is what happened).
MODULES = ["config", "zipguard", "pairing", "creds", "gpsource", "generator",
           "runs", "applog", "folders", "s3sync",
           "appwindow",
           "mirror.assets", "mirror.layout", "mirror.store", "mirror.sync",
           "mirror.serve", "mirror.localrun", "mirror.schedule", "mirror.libui",
           "foldersio", "s3ui", "foldersui",
           "server", "controller", "tray"]

# Module views called ONLY by the tail (`_MAIN`) and by none of the modules.
# The regular list of views is collected from the MODULES' relative imports,
# and the kit's entry point is not part of that walk — so `s3sync.load_s3(...)`
# in the tail built, compiled and failed with `NameError` on the very first run
# of the command. `_assert_entry_views` guards the list: a forgotten name now
# stops the build.
ENTRY_VIEWS = ["s3sync"]

# Files the kit carries SEPARATELY from the concatenation, next to `op_agent.py`:
# {name in the kit: (file in the repository, what its absence leads to)}.
#
# `r2host.py` — the R2 contract host: it is LAUNCHED (`python -I r2host.py …`),
# not imported, so it cannot be concatenated in principle, yet it can be
# forgotten silently.
#
# `_lineage.py` — the lineage probe, and its source is NOT in agent/ but in
# `static/py/`: it is THE SAME file the browser worker loads from
# `/static/py/_lineage.py`. It deliberately has no copy of its own in the
# repository — a copy that drifts from the original is the classic way for one
# report to get two different lineages (different `check_id`s, different
# schemas). So the original itself goes into the kit, and `--check-kit`
# compares the file in the kit with it BYTE FOR BYTE and fails on a mismatch.
#
# From here both end up in `--install-resources`, in `--check-kit` and in the
# `_KIT_RESOURCES` list the agent checks itself against at startup.
HOST_FILES = {
    "r2host.py": (PKG / "r2host.py",
                  "the concatenation does not carry it — --install-resources "
                  "puts it in; without it a folder run fails with gen_no_host"),
    "_lineage.py": (ROOT / "static" / "py" / "_lineage.py",
                    "--install-resources copies it from static/py/; without it "
                    "the run goes ahead, but the report's provenance stays empty"),
}

# Packages that exist in the concatenation only as a name (`from .mirror import
# schedule as mirror_schedule` — the single-file build has no package, but the
# name exists).
PACKAGES = ["mirror"]

_FUTURE_RE = re.compile(r"^from\s+__future__\s+import\s+annotations\s*$")

# Scopes the module-name analysis does not descend into: they hold local names,
# not the module namespace.
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

# Rename opt-out: a name that MUST stay shared across the whole concatenation
# even if two modules declare it. The empty dict is not unfinished work but the
# outcome: `_plan_renames` resolves collisions (the name stays with whoever is
# concatenated first, the second gets a module suffix), and no exceptions have
# been needed so far.
_SHADOW_OK: dict[str, str] = {}

# Names that show the mirror really made it into the build. Taken from
# DIFFERENT mirror modules: a module missing from MODULES is visible this way,
# whereas a mere "does it compile" check would let it through.
_MIRROR_MARKERS = {
    "res_path": "mirror/assets.py",
    "strings_path": "mirror/assets.py",
    "URL_PREFIX": "mirror/layout.py",
    "parse_local_path": "mirror/layout.py",
    "mirror_root": "mirror/layout.py",
    "handles": "mirror/serve.py",
    "serve": "mirror/serve.py",
    "MirrorWorker": "mirror/schedule.py",
}


def _mod_path(key: str) -> Path:
    return PKG / (key.replace(".", "/") + ".py")


def _view_var(key: str) -> str:
    """Name of the view variable: `mirror.schedule` → `_mod_mirror_schedule`."""
    return "_mod_" + key.replace(".", "_")


def _build_stamp() -> str:
    """Build stamp: short git sha + date. Needed so the machine shows which
    build exactly is installed (otherwise "the command doesn't work" = an old file)."""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        sha = ""
    day = datetime.date.today().isoformat()
    return f"{day}{(' ' + sha) if sha else ''}"


def _read_constants() -> dict:
    """APP / VERSION / PROTOCOL from agent/__init__.py (not duplicated by hand)."""
    tree = ast.parse((PKG / "__init__.py").read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name) and t.id in ("APP", "VERSION", "PROTOCOL"):
                out[t.id] = ast.literal_eval(node.value)
    missing = {"APP", "VERSION", "PROTOCOL"} - set(out)
    if missing:
        raise SystemExit(f"constants not found in agent/__init__.py: {missing}")
    return out


def _target_names(node) -> list[str]:
    """Names bound by an assignment target (tuples bind names too)."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        out = []
        for el in node.elts:
            out += _target_names(el)
        return out
    return []                        # attribute/subscript targets create no name


def _module_names(tree: ast.Module) -> tuple[set[str], set[str], set[str], dict[str, str]]:
    """A module's namespace: (everything, own, declared here, imports).

    Three different sets serve three different checks.
      * "everything" — what the module view will see in the concatenation;
        this includes relative-import bindings (`from . import layout` puts the
        name `layout` into the module, although in the concatenation it is a
        NEIGHBOUR's name).
      * "own" — without relative imports: a module name must not collide with
        such a name.
      * "declared" — only def/class/assignments: a collision of two such names
        in the concatenation silently loses one value. Ordinary imports do not
        go here — `import os` in ten modules yields one and the same object,
        and that is not a collision but the norm. They are checked separately,
        by SOURCE.
    """
    every: set[str] = set()
    own: set[str] = set()
    defined: set[str] = set()
    imports: dict[str, str] = {}

    def add(names, mine: bool, declared: bool = True) -> None:
        every.update(names)
        if mine:
            own.update(names)
        if declared:
            defined.update(names)

    def walk(body) -> None:
        for node in body:
            if isinstance(node, _SCOPES):
                add([node.name], True)
                continue              # do not descend: local names live there
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    add(_target_names(t), True)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                add(_target_names(node.target), True)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                rel = isinstance(node, ast.ImportFrom) and bool(node.level)
                src = getattr(node, "module", None) or ""
                for a in node.names:
                    bound = a.asname or a.name.split(".")[0]
                    add([bound], not rel, declared=False)
                    if not rel:
                        # `import urllib.error` and `import urllib.request`
                        # bind one name `urllib` to one object — the source
                        # here is the package itself, not the submodule.
                        imports[bound] = (bound if isinstance(node, ast.Import)
                                          and not a.asname
                                          else (f"{src}.{a.name}" if src else a.name))
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                add(_target_names(node.target), True)
                walk(node.body)
                walk(node.orelse)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        add(_target_names(item.optional_vars), True)
                walk(node.body)
            elif isinstance(node, ast.If):
                walk(node.body)
                walk(node.orelse)
            elif isinstance(node, ast.Try):
                walk(node.body)
                for h in node.handlers:
                    walk(h.body)
                walk(node.orelse)
                walk(node.finalbody)

    walk(tree.body)
    # `global X` inside a function also creates a global — for the module view
    # it is a name in its namespace, even though no assignment is visible at
    # module level.
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            add(node.names, True)
    return every, own, defined, imports


# What one relative import binds: the node and a list of (name, module key, or
# None for "this is a symbol, not a module").
_RelImport = tuple[ast.ImportFrom, list[tuple[str, "str | None"]]]


def _rel_base(key: str, node: ast.ImportFrom) -> list[str]:
    """Where a relative import points: the source module path, split into parts.

    `[]` is the package root (`from . import VERSION` in agent/*.py), i.e.
    `agent/__init__.py`, which does not exist as a module in the concatenation.
    """
    pkg = key.split(".")[:-1]
    base = pkg[:max(len(pkg) - (node.level - 1), 0)]
    if node.module:
        base = base + node.module.split(".")
    return base


def _rel_imports(key: str, tree: ast.Module) -> list[_RelImport]:
    """A module's relative imports and what they bind.

    Each pair is `(node, [(name, module key or None)])`. None means "a symbol":
    `from .layout import atomic_write` is not needed in the concatenation at
    all, the name already lives in the shared namespace. A key means "this is
    a MODULE NAME" — it must be replaced with a view, otherwise
    `layout.read_state` fails on the user's machine.

    Derived from the imports themselves rather than listed by hand: a new
    `from .mirror import serve as mirror_serve` is picked up automatically.
    """
    known = set(MODULES) | set(PACKAGES)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        base = _rel_base(key, node)
        binds = []
        for a in node.names:
            target = ".".join(base + [a.name])
            binds.append((a.asname or a.name, target if target in known else None))
        out.append((node, binds))
    return out


def _assert_no_nested_relative_imports(path: Path, tree: ast.Module) -> None:
    """A relative import inside a FUNCTION silently breaks the build.

    The concatenation rewrites such imports in place (module name → view,
    symbol → nothing), working from the parsed file. An import inside a
    function has nowhere to be rewritten: it runs after concatenation, where
    there is no `agent` package — `from .gpsource import probe` would survive
    the build and fail on the analyst's machine, where debugging is most
    expensive. So this is a build error, not a warning. Inside a `try:` AT
    MODULE LEVEL it is fine: such an import is rewritten (that is how
    `from . import sync as _sync` lives in mirror/schedule.py).
    """
    bad = {}
    for scope in ast.walk(tree):
        if not isinstance(scope, _SCOPES):
            continue
        for n in ast.walk(scope):
            if isinstance(n, ast.ImportFrom) and n.level:
                bad[n.lineno] = f"{path.name}:{n.lineno}: {ast.unparse(n)}"
    if bad:
        raise SystemExit("Relative imports inside functions (move them to the top):\n  "
                         + "\n  ".join(bad[k] for k in sorted(bad)))


def _strip_module(key: str, renames: dict[str, str] | None = None) -> str:
    """Strip the docstring (into the banner) and the future import, replace
    relative imports with a module-view binding (or drop them if they import a
    symbol), and rename globals that collide with neighbours (see `_plan_renames`)."""
    path = _mod_path(key)
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    _assert_no_nested_relative_imports(path, tree)
    if renames:
        src = _rename_globals(src, tree, renames)
        tree = ast.parse(src)          # positions moved — parse again
    lines = src.splitlines()

    banner_lines = []
    body_start = 0
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        doc = tree.body[0].value.value.strip("\n")
        banner_lines = ["# " + ln if ln.strip() else "#" for ln in doc.splitlines()]
        body_start = tree.body[0].end_lineno          # 1-based, docstring inclusive

    # Edit plan by line number: an import may span several lines
    # (`from .x import (\n  a,\n  b)`), so we cut by the parse tree, not by regexp.
    replace: dict[int, list[str]] = {}
    drop: set[int] = set()
    for node, binds in _rel_imports(key, tree):
        first = lines[node.lineno - 1]
        indent = first[:len(first) - len(first.lstrip())]
        repl = [f"{indent}{name} = {_view_var(target)}"
                for name, target in binds if target]
        if indent and not repl:
            # The import was the only body of a `try:`/`if:` — an empty body
            # is a SyntaxError, not "nothing".
            repl = [f"{indent}pass"]
        replace[node.lineno] = repl
        drop.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    kept = []
    for num, ln in enumerate(lines[body_start:], start=body_start + 1):
        if num in replace:
            kept.extend(replace[num])
            continue
        if num in drop or _FUTURE_RE.match(ln):
            continue
        kept.append(ln)

    head = [
        "",
        "# " + "=" * 74,
        f"#  {_mod_path(key).relative_to(PKG).as_posix()}",
        "# " + "=" * 74,
    ] + banner_lines
    return "\n".join(head + kept).rstrip() + "\n"


_MAIN = '''

# ==========================================================================
#  Single-file entry point: credential subcommands, agent.ini template, port ladder
# ==========================================================================
import getpass as _getpass
import socket as _socket
import threading as _threading
import urllib.error as _urlerr
import urllib.request as _urlreq

_INI_TEMPLATE = """\\
; op-agent configuration. Lives NEXT TO op_agent.py (or at the path in OP_AGENT_INI).
; NEVER WRITE THE PASSWORD HERE — it is stored in Windows DPAPI:
;     python op_agent.py set-cred analytics_dw

[security]
; Origin of the OnePage page that may talk to the agent. Exact, no '*'.
allowed_origins = https://onepage.example.com

; The section name after "connection:" is a LOGICAL label the generator manifest
; uses to refer to this connection (sources.connection). It need not match the
; database name, but that is clearer. Values below are as in DBeaver ("Connection settings").
[connection:analytics_dw]
host = warehouse.example.com
port = 6432
dbname = analytics_dw
; Your PERSONAL GP login (the "Username" field in DBeaver). Not a secret — unlike the password.
user = CHANGE-ME-your-gp-login
; Optional libpq parameters. Needed when the entry point is a connection pooler:
; libpq 12+ tries GSSAPI encryption by default, and some poolers silently drop
; the connection on that packet ("server closed the connection unexpectedly"),
; whereas JDBC (DBeaver) never sends it. To find a working combination:
;     python op_agent.py test-conn analytics_dw
; gssencmode = disable
; sslmode = require

; An S3 bucket as a network folder (like WinSCP): the agent downloads the objects
; under the prefix into a local folder, and the generator reads it as usual. You
; do not have to edit this by hand: the same is filled in with WinSCP-style fields
; on the "Report folders" screen (tray icon -> "Report folders…", "S3 bucket" block),
; which also has a "Test connection" button. NEVER WRITE THE SECRET HERE, EITHER WAY:
;     python op_agent.py set-cred s3-ldwh
; Check access without downloading:   python op_agent.py s3-list ldwh
; Download:                           python op_agent.py s3-sync  ldwh
; [s3:ldwh]
; endpoint = https://s3.company.local   ; with scheme; on AWS — https://s3.<region>.amazonaws.com
; region = us-east-1                    ; part of the signature: wrong region = 403 with no explanation
; bucket = reports
; prefix = sales/weekly/                ; the "folder" inside the bucket
; access_key = AKIA...                  ; a LOGIN, not a secret
; target = C:\\\\OnePage\\\\data\\\\sales       ; where to put files; select this folder in the report
; path_style = true                     ; MinIO/Ceph — true, AWS — false
; include = *.xlsx, *.xlsb
; exclude = ~$*
"""


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


# What must lie next to op_agent.py besides itself: the mirror resources
# (mirror/strings.json, mirror/web/*) AND r2host.py — the R2 contract host,
# which runs as a separate process and therefore is not concatenated. The list
# is filled in by the BUILDER (the same list --install-resources uses to put
# files into the kit); do not edit it by hand: a page added to web/ lands here
# automatically.
_KIT_RESOURCES = ("__KIT_RESOURCES__",)


def _kit_problems() -> list:
    """Which files are missing next to op_agent.py (empty = the kit is intact).

    The agent does NOT crash without the mirror folder: /health answers 200,
    probes work, but every library page is a 404 and the log says nothing (a
    resource "not found" is a normal None for assets.res_path, not an error).
    Kit integrity used to be checked only at BUILD time (--check-kit) and by a
    single `if not exist` in setup.bat, which knows nothing about the mirror
    folder. So the check lives in the agent itself and looks the files up with
    THE SAME res_path the serving code later uses: "check passed" then means
    "the page will open", not "the file is somewhere".

    There are TWO finders because there are two resource roots: `mirror/` for
    the mirror and `appwin/` for the window (the "Report folders" screen). Each
    half looks up its files with its own function — otherwise the check would
    report "no file" for a file that is right there, and the user would unpack
    the kit again for nothing. The choice is by the FIRST path segment, not by
    file name: the name says nothing about whose resource it is.
    """
    out = []
    for rel in _KIT_RESOURCES:
        parent, _sep, name = rel.rpartition("/")
        root = rel.split("/")[0] if "/" in rel else ""
        web = parent.endswith("/" + WEB_DIRNAME)
        found = (win_res_path(name) if root == FOLDERS_RES_DIRNAME
                 else res_path(name, web=web))
        if found is None:
            out.append(rel.replace("/", os.sep))
    return out


def _kit_report(missing: list) -> str:
    """Refusal text for the person who unpacked the kit. No data paths, no keys."""
    shown = missing[:12]
    more = len(missing) - len(shown)
    return ("The op-agent kit was not unpacked completely.\\n\\n"
            f"Folder: {_script_dir()}\\n"
            "Missing:\\n  " + "\\n  ".join(shown)
            + (f"\\n  ... and {more} more" if more > 0 else "")
            + "\\n\\nUnpack the zip again IN FULL, together with the mirror folder. "
              "Copying op_agent.py alone will not work: the report library pages "
              "and r2host.py (which runs generators over folders) are separate "
              "files next to it.")


def _show_fatal(text: str) -> None:
    """Show the refusal SO THAT it is seen: the console, or a window without one.

    An autostarted agent runs through pythonw.exe: there is no console
    (sys.stderr is None), and a one-line refusal on stderr would vanish — the
    user would only see "no tray icon". MessageBoxW is ctypes, standard library.
    """
    if sys.stderr:
        print("\\n[!] " + text + "\\n", file=sys.stderr, flush=True)
    if sys.platform == "win32" and not sys.stdout:
        try:
            import ctypes
            # 0x10 = error icon, 0x40000 = on top of other windows.
            ctypes.windll.user32.MessageBoxW(None, text, "op-agent", 0x10 | 0x40000)
        except Exception:
            pass                           # no window either — nothing else to show with


def _check_kit(argv) -> int:
    """The check-kit subcommand: is the kit intact. Called by setup.bat."""
    missing = _kit_problems()
    if missing:
        _show_fatal(_kit_report(missing))
        return 1
    print(f"OK: kit intact ({len(_KIT_RESOURCES)} adjacent files in place).")
    return 0


def _ini_path() -> Path:
    env = os.environ.get("OP_AGENT_INI")
    return Path(env) if env else _script_dir() / "agent.ini"


_RUN_KEY = r"Software\\Microsoft\\Windows\\CurrentVersion\\Run"
_RUN_VALUE = "OpAgent"


def _autostart_command() -> str:
    """The autostart command: the pythonw next to us + this file.

    pythonw.exe, not python.exe: it opens no console window — that is the whole
    point of this step. Output then goes to the log (applog); otherwise
    sys.stdout being None would crash the agent on the first print().
    """
    here = _script_dir()
    pyw = here / "python" / "pythonw.exe"
    if not pyw.is_file():
        pyw = Path(sys.executable).with_name("pythonw.exe")
        if not pyw.is_file():
            pyw = Path(sys.executable)
    return f'"{pyw}" "{here / Path(__file__).name}"'


def _install_autostart(argv) -> int:
    """Register autostart in HKCU. Needs no administrator rights.

    HKCU, not HKLM: the key (like the password's DPAPI blob) belongs to a
    SPECIFIC Windows user. Install it under the account of the person who will
    work with it — otherwise the agent will not start for them and the password
    will not decrypt.
    """
    if sys.platform != "win32":
        print("Autostart is only available on Windows.", file=sys.stderr)
        return 1
    import winreg
    cmd = _autostart_command()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, _RUN_VALUE, 0, winreg.REG_SZ, cmd)
    print("OK: autostart enabled for user "
          f"{os.environ.get('USERNAME', '?')}.")
    print(f"  {cmd}")
    print("  To disable: python op_agent.py remove-autostart")
    return 0


def _remove_autostart(argv) -> int:
    if sys.platform != "win32":
        return 1
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, _RUN_VALUE)
        print("OK: autostart disabled.")
    except FileNotFoundError:
        print("Autostart was not enabled anyway.")
    return 0


def _set_cred(argv) -> int:
    """Store the GP password in DPAPI. Input is hidden and never hits disk in clear."""
    conn = argv[0] if argv else "analytics_dw"
    store = CredStore()
    if store.has_password(conn):
        print(f"A password for {conn!r} is already stored — overwrite? [y/N]", flush=True)
        # "\u0434\u0430"/"\u0434" are the Russian "yes"/"y", kept so a Russian keyboard answer still counts.
        if (input().strip().lower() or "n") not in ("y", "yes", "d", "\u0434\u0430", "\u0434"):
            print("Cancelled.")
            return 0
    pw = _getpass.getpass(f"GreenPlum password for {conn!r} (input hidden): ")
    if not pw:
        print("Empty password — cancelled.", file=sys.stderr)
        return 1
    store.set_password(conn, pw)
    print(f"OK: password for {conn!r} stored in DPAPI ({store.dir}).", flush=True)
    print("Checking it with a real connection…", flush=True)
    return _test_conn([conn])


def _list_cred(argv) -> int:
    """Under which connection names passwords are stored (passwords are not printed).

    Catches a typical mistake: the password is stored under an old connection
    label while the manifest/ini refer to a new one — then the agent honestly
    reports cred_missing.
    """
    store = CredStore()
    names = store.list_connections()
    print(f"store: {store.dir}")
    if not names:
        print("  (empty — no password saved yet)")
        return 0
    ini = _ini_path()
    config = load_config(ini) if ini.is_file() else load_config()
    known = set(config.connections or {})
    for n in names:
        mark = "" if n in known else "   <-- no such [connection:...] section in agent.ini"
        print(f"  {n}{mark}")
    return 0


def _del_cred(argv) -> int:
    conn = argv[0] if argv else "analytics_dw"
    CredStore().delete_password(conn)
    print(f"OK: password for {conn!r} deleted (if there was one).", flush=True)
    return 0


# Combinations to try when the basic connect fails. Order = decreasing
# likelihood: libpq 12+ tries GSSAPI encryption by default, and many
# poolers/proxies simply close the socket on that packet ("server closed the
# connection unexpectedly"), whereas JDBC (DBeaver) never sends it.
_PROBES = [
    ("without application_name",                     {}, False),
    ("without application_name + sslmode=require",   {"sslmode": "require"}, False),
    ("without application_name + sslmode=disable",   {"sslmode": "disable"}, False),
    ("without application_name + gssencmode=disable", {"gssencmode": "disable"}, False),
    ("gssencmode=disable",                       {"gssencmode": "disable"}, True),
    ("sslmode=require",                          {"sslmode": "require"}, True),
    ("sslmode=disable",                          {"sslmode": "disable"}, True),
]


def _probe_connect(topo, pw, extra, with_app):
    import psycopg
    kw = dict(host=topo.get("host"), port=topo.get("port"), dbname=topo.get("dbname"),
              user=topo.get("user"), password=pw, autocommit=False,
              connect_timeout=CONNECT_TIMEOUT_S)
    kw.update(extra)
    if with_app:
        kw["application_name"] = f"{APP}/{VERSION} probe"
    return psycopg.connect(**kw)


def _probe_matrix(topo, pw) -> int:
    """Try the combinations and name the working one + ready lines for agent.ini."""
    print("\\nTrying connection parameter combinations…", flush=True)
    for label, extra, with_app in _PROBES:
        try:
            conn = _probe_connect(topo, pw, extra, with_app)
        except Exception as e:
            print(f"  [no]  {label}:\\n        {_safe_msg(e, 1500)}", flush=True)
            continue
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user, current_database()")
                row = cur.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        print(f"\\n  [YES] {label}")
        print(f"        current_user = {row[0]}, database = {row[1]}")
        if extra:
            print("\\nAdd to agent.ini, in this connection's section:")
            for k, v in extra.items():
                print(f"    {k} = {v}")
        if not with_app:
            print("\\nWorked WITHOUT application_name. The agent falls back by itself, but")
            print("better pin it in agent.ini — otherwise every connection pays an extra")
            print("handshake (with N parallel queries — N extra):")
            print("    application_name = off")
        return 0
    print("\\nNo combination worked. Most likely the problem is not the libpq "
          "parameters —\\nshow this output to the GreenPlum/pooler administrator.", file=sys.stderr)
    return 1


def _test_conn(argv) -> int:
    """The equivalent of DBeaver's "Test connection" button: a real connect + SELECT.

    Prints the FULL error text — exactly what is missing when a run from the
    page returns a terse "connection failure".
    """
    conn_name = argv[0] if argv else "analytics_dw"
    ini = _ini_path()
    config = load_config(ini) if ini.is_file() else load_config()
    topo = (config.connections or {}).get(conn_name)
    if not topo:
        print(f"NO [connection:{conn_name}] section in {ini}", file=sys.stderr)
        return 1
    pw = CredStore().get_password(conn_name)
    if pw is None:
        print(f"NO password for {conn_name!r}. First run: set-cred {conn_name}", file=sys.stderr)
        return 1

    print(f"host={topo.get('host')} port={topo.get('port')} "
          f"dbname={topo.get('dbname')} user={topo.get('user')}   (password from DPAPI)",
          flush=True)
    print(f"driver: {_driver_status()}", flush=True)
    conn = None
    try:
        conn = _default_connect(topo, pw, f"{APP}/{VERSION} test-conn",
                                CONNECT_TIMEOUT_S, 30000)
    except Exception as e:
        kind, _ = _classify(e)
        print(f"\\nCONNECTION FAILED   [{kind}]", flush=True)
        print(f"  {_safe_msg(e, 1500)}", flush=True)
        print(f"  exception type: {type(e).__name__}", flush=True)
        if isinstance(e, ImportError):
            return 1                       # no driver — nothing to try
        return _probe_matrix(topo, pw)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, current_database(), version()")
            row = cur.fetchone()
        print("\\nCONNECTED")
        print(f"  current_user     = {row[0]}")
        print(f"  current_database = {row[1]}")
        print(f"  version          = {str(row[2])[:90]}")
        # Does the pooler accept application_name? If not, the agent falls back
        # by itself, but every connection pays an extra handshake (with N
        # parallel queries — N extra). Then the mode is worth pinning in agent.ini.
        try:
            _probe_connect(topo, pw, {}, True).close()
            print("\\n  application_name is accepted — nothing needs pinning.")
        except Exception:
            print("\\n  The pooler does NOT accept application_name. The agent falls back by itself, but")
            print("  to avoid an extra handshake on EVERY connection,")
            print("  add to agent.ini, in this connection's section:")
            print("      application_name = off")
        return 0
    except Exception as e:
        kind, msg = _classify(e)
        print(f"\\nconnected, but the query failed [{kind}]: {msg}", file=sys.stderr)
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _driver_status() -> str:
    try:
        import psycopg
        return "psycopg " + getattr(psycopg, "__version__", "?")
    except Exception:
        return 'MISSING  ->  pip install "psycopg[binary]"  (GreenPlum runs will fail)'


# Our own port is polled BYPASSING the proxy: `<local>` in the system bypass
# list does not cover 127.0.0.1 (it matches only names without dots), and on a
# machine with a corporate proxy the health check of a live agent would fail.
# The cost of that bug: a shortcut that "did not respond within 20 seconds" and
# a second instance taking the next port instead of exiting quietly. The same
# fix is in `agent/__main__.py`.
_LOOPBACK = _urlreq.build_opener(_urlreq.ProxyHandler({}))


def _is_our_agent(port: int) -> bool:
    try:
        with _LOOPBACK.open(f"http://127.0.0.1:{port}/health", timeout=1.0) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == APP
    except (_urlerr.URLError, OSError, ValueError):
        return False


def _s3_source(argv):
    """The [s3:<name>] section and its secret from DPAPI. Prints the reason and returns None."""
    name = argv[0] if argv else ""
    ini = _ini_path()
    sources = s3sync.load_s3(ini)
    if not name:
        print("give a name: s3-list <name>   (sections in agent.ini: "
              + (", ".join(sorted(sources)) or "none") + ")", file=sys.stderr)
        return None, None
    src = sources.get(name)
    if src is None:
        print(f"NO [s3:{name}] section in {ini}", file=sys.stderr)
        return None, None
    secret = CredStore().get_password(src.cred_name)
    if secret is None:
        print(f"NO secret for {name!r}. First run: set-cred {src.cred_name}",
              file=sys.stderr)
        return None, None
    return src, secret


def _s3_list(argv) -> int:
    """Bucket access check: list without downloading anything.

    A separate command rather than an s3-sync flag: the first question on
    someone else's machine is "do we reach the storage and is it the right
    key", and answering it by downloading gigabytes is wrong.
    """
    src, secret = _s3_source(argv)
    if src is None:
        return 1
    print(f"{src.endpoint}  bucket={src.bucket}  prefix={src.prefix or '(all)'}"
          f"  region={src.region}  path_style={'yes' if src.path_style else 'no'}",
          flush=True)
    cli = s3sync.S3Client(src, secret)
    shown = total = matched = 0
    size_sum = 0
    try:
        for obj in cli.list_objects():
            total += 1
            rel = s3sync.relative_name(obj.key, src.prefix)
            if rel is None or not s3sync.wanted(src, rel):
                continue
            matched += 1
            size_sum += obj.size
            if shown < 20:
                shown += 1
                print(f"  {obj.size:>12} B  {rel}")
    except s3sync.S3Error as e:
        print(f"\\nFAILED: {e}", file=sys.stderr)
        return 1
    if matched > shown:
        print(f"  … and {matched - shown} more")
    print(f"\\ntotal objects: {total}, matching the masks: {matched}, "
          f"size: {size_sum / (1 << 20):.1f} MB")
    print(f"will be downloaded to: {src.target}")
    return 0


def _s3_sync(argv) -> int:
    """Download the prefix into the folder. Prints progress and the summary."""
    src, secret = _s3_source(argv)
    if src is None:
        return 1
    last = [-1]

    def progress(done, total, nbytes):
        # Reprint the line only when it CHANGES: otherwise thousands of
        # identical lines pile up in the log (and the .bat keeps it).
        line = f"  {done} of {total}, downloaded {nbytes / (1 << 20):.1f} MB"
        if line != last[0]:
            last[0] = line
            print(line, flush=True)

    print(f"{src.endpoint}  bucket={src.bucket}  prefix={src.prefix or '(all)'}",
          flush=True)
    rep = s3sync.sync(src, secret, progress=progress)
    print(f"\\nfound: {rep.listed}, matching: {rep.matched}, "
          f"downloaded: {rep.downloaded} ({rep.bytes_downloaded / (1 << 20):.1f} MB), "
          f"unchanged: {rep.skipped}, stale deleted: {rep.deleted}")
    if rep.foreign:
        print(f"\\nthe folder has files that are not in the bucket ({len(rep.foreign)}) — "
              f"nobody touched them, but the generator will read them too:")
        for name in rep.foreign[:10]:
            print(f"  {name}")
    for key, msg in rep.errors:
        print(f"  [!] {key or 'storage'}: {msg}", file=sys.stderr)
    if rep.stopped:
        print("pass stopped", file=sys.stderr)
    return 0 if rep.ok else 1


def _diagnose(argv) -> int:
    """What can be seen from outside when "the agent did not respond within 20 seconds".

    Added on 2026-10-12 after a live failure at the owner's: the shortcut gave
    up, and the only advice was "open the log from the tray icon menu" — i.e.
    from the icon that is exactly what is missing after a failed start. The
    port is checked with a PLAIN socket, separately from /health: only that
    tells "the agent did not start" from "it started, but the health poll
    lied" (which is exactly what the system proxy intercepting requests to
    127.0.0.1 was doing).
    """
    print(f"{APP} {VERSION}  build {BUILD}")
    root = state_dir()
    ini = _ini_path()
    print(f"folder    : {_script_dir()}")
    print(f"state     : {root}")
    print(f"config    : {ini}  [{'present' if ini.is_file() else 'MISSING'}]")
    missing = _kit_problems()
    print("kit       : " + ("intact" if not missing
                            else f"INCOMPLETE, {len(missing)} mirror files missing"))
    for n in PORT_LADDER:
        try:
            _socket.create_connection(("127.0.0.1", n), 0.3).close()
        except OSError:
            print(f"port {n} : free — nobody is listening here")
            continue
        ours = _is_our_agent(n)
        print(f"port {n} : in use, our agent — "
              + ("YES" if ours else "NO (another program, or the poll failed)"))
    log = root / LOG_NAME
    print(f"log       : {log}")
    if not log.is_file():
        print("  no file — the agent did not get as far as its first line of output.")
        print("  Run  op-agent.bat --no-tray  and send the whole output.")
        return 0
    lines = log.read_text("utf-8", errors="replace").splitlines()
    print("-" * 64)
    for line in lines[-30:]:
        print(line)
    return 0


def main() -> int:
    # Strip the flags BEFORE dispatching: otherwise "--no-tray" would end up in
    # the unknown-command check and the agent would refuse to start.
    no_tray = "--no-tray" in sys.argv
    argv = [a for a in sys.argv[1:] if a != "--no-tray"]
    if argv and argv[0] == "set-cred":
        return _set_cred(argv[1:])
    if argv and argv[0] == "del-cred":
        return _del_cred(argv[1:])
    if argv and argv[0] == "list-cred":
        return _list_cred(argv[1:])
    if argv and argv[0] == "test-conn":
        return _test_conn(argv[1:])
    if argv and argv[0] == "check-kit":
        return _check_kit(argv[1:])
    if argv and argv[0] == "diagnose":
        return _diagnose(argv[1:])
    if argv and argv[0] == "folders":
        # There is DELIBERATELY no printing code of its own here: `folders_cli`
        # comes from agent/server.py together with the `GET /folders` endpoint
        # whose answer it repeats. They must not drift — the command is called
        # precisely when the endpoint is unavailable and there is nothing to
        # compare against.
        return folders_cli(argv[1:])
    if argv and argv[0] == "s3-list":
        return _s3_list(argv[1:])
    if argv and argv[0] == "s3-sync":
        return _s3_sync(argv[1:])
    if argv and argv[0] == "install-autostart":
        return _install_autostart(argv[1:])
    if argv and argv[0] == "remove-autostart":
        return _remove_autostart(argv[1:])
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv and argv[0] in ("version", "--version", "-V"):
        print(f"{APP} {VERSION}  build {BUILD}")
        return 0
    if argv:
        # An unknown argument used to start the daemon silently — and it looked
        # as if the command had "worked". Now it is an explicit error with the
        # list of commands.
        print(f"Unknown command: {argv[0]!r}\\n", file=sys.stderr)
        print("Available commands:", file=sys.stderr)
        print("  python op_agent.py                     start the daemon", file=sys.stderr)
        print("  python op_agent.py set-cred  [conn]    store a password (+ check)", file=sys.stderr)
        print("  python op_agent.py list-cred           where passwords are stored", file=sys.stderr)
        print("  python op_agent.py del-cred  [conn]    delete a password", file=sys.stderr)
        print("  python op_agent.py test-conn [conn]    test the GP connection", file=sys.stderr)
        print("  python op_agent.py check-kit           is the kit intact (mirror folder)", file=sys.stderr)
        print("  python op_agent.py diagnose            ports, config and log tail", file=sys.stderr)
        print("  python op_agent.py folders   [id]     which files a report will read", file=sys.stderr)
        print("  python op_agent.py s3-list   <name>    what is in the S3 bucket (no download)", file=sys.stderr)
        print("  python op_agent.py s3-sync   <name>    download the S3 bucket into a folder", file=sys.stderr)
        print("  python op_agent.py install-autostart   start at Windows sign-in", file=sys.stderr)
        print("  python op_agent.py remove-autostart    cancel autostart", file=sys.stderr)
        print("  python op_agent.py --no-tray           daemon without tray icon (for debugging)", file=sys.stderr)
        print("  python op_agent.py version             version and build stamp", file=sys.stderr)
        print(f"\\nThis build: {VERSION}  build {BUILD}", file=sys.stderr)
        return 2

    # Do not start the daemon from an incompletely copied kit: otherwise it is
    # "alive" (/health answers) while the report library silently returns 404.
    # Before the ini template — so the user does not fix the config of a kit
    # that has to be unpacked again anyway.
    missing = _kit_problems()
    if missing:
        _show_fatal(_kit_report(missing))
        return 3

    ini = _ini_path()
    if not ini.is_file():
        ini.write_text(_INI_TEMPLATE, encoding="utf-8")
        print("=" * 60)
        print(f"  Configuration template created: {ini}")
        print("  1) Fill in host / dbname / user of your GreenPlum (CHANGE-ME).")
        print(f"  2) Store the password:  python {Path(__file__).name} set-cred analytics_dw")
        print(f"  3) Run again:           python {Path(__file__).name}")
        print("=" * 60)
        return 1

    config = load_config(ini)
    root = state_dir()
    # The tray icon (M4) is the main mode on Windows. `--no-tray` restores the
    # old console behaviour: needed when you want to watch the output live.
    want_tray = not no_tray and available()
    if want_tray and not sys.stdout:
        # Started via pythonw.exe: no console, sys.stdout is None, and the very
        # first print() would crash the agent. Set up the log BEFORE any output.
        setup(root)
    elif want_tray:
        setup(root, mirror_console=True)
    pairing = Pairing(token_path=root / TOKEN_FILE)

    last_err = None
    for port in PORT_LADDER:
        try:
            server = AgentServer(("127.0.0.1", port), config, pairing)
        except OSError as e:
            last_err = e
            if _is_our_agent(port):
                print(f"{APP} is already running on 127.0.0.1:{port} — exiting.", flush=True)
                return 0
            continue
        server.state_root = root
        conns = ", ".join(config.connections) or "no [connection:*] sections in the ini"
        print("=" * 60, flush=True)
        print(f"  {APP} {VERSION}  build {BUILD}  ->  http://127.0.0.1:{port}", flush=True)
        print(f"  origin allowlist : {', '.join(config.allowed_origins)}", flush=True)
        print(f"  GP connections   : {conns}", flush=True)
        print(f"  driver           : {_driver_status()}", flush=True)
        print(f"  state            : {root}", flush=True)
        print(f"  config           : {ini}", flush=True)
        if want_tray:
            print(f"  log              : {log_path()}", flush=True)
            print("  The op-agent icon is next to the clock. To stop — the 'Exit' item.", flush=True)
        else:
            print("  Stop: Ctrl+C.  The pairing code will appear here when a run starts.",
                  flush=True)
        print("=" * 60, flush=True)
        return _serve(server, config, pairing, want_tray)

    print(f"Could not bind any port of {PORT_LADDER}: {last_err}", file=sys.stderr)
    return 1


def _serve(server, config, pairing, want_tray: bool) -> int:
    """With a tray icon the main thread runs the Windows message loop and HTTP
    moves to a background thread: windows belong to the thread that created
    them, and a modal prompt from the HTTP thread would freeze the whole agent.

    The controller and the mirror start BEFORE the icon and independently of
    it. The icon is a surface, not a precondition: when the controller was only
    created in the icon branch, any path without it (no support, window
    creation failed, `--no-tray`) left the user with a working library whose
    copies were NEVER updated, and without a single error on screen. The same
    fix is in `agent/__main__.py`; drift between the two is pinned by a test on
    the built kit (`agent/tests/smoke_mirror_kit.py`), because the SECOND entry
    point lives here — the kit's one is richer (set-cred, autostart,
    check-kit), and the two cannot be merged."""
    conn = next(iter(config.connections), "analytics_dw")
    controller = AgentController(server, pairing, server.cred_store, config, conn)
    server.controller = controller
    # A repeated start is safe: on a live thread it is a no-op, and the icon
    # calls the same `start_mirror` once it is up.
    controller.start_mirror()

    ui = start(controller, pairing) if want_tray else None
    if ui is None:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\\n  Stopped.", flush=True)
        finally:
            controller.mirror.stop(timeout=3.0)
            server.shutdown()
            server.server_close()
        return 0

    server.ui = ui
    _threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    try:
        ui.run()
    finally:
        controller.mirror.stop(timeout=3.0)
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


# ==========================================================================
#  Composition checks: breakage must happen here, not on the user's machine
# ==========================================================================

def _analyze() -> tuple[dict, dict, dict, dict, dict]:
    """Parse every concatenated module — one set of names per module."""
    trees, every, own, defined, imports = {}, {}, {}, {}, {}
    for key in MODULES:
        trees[key] = ast.parse(_mod_path(key).read_text(encoding="utf-8"))
        (every[key], own[key],
         defined[key], imports[key]) = _module_names(trees[key])
    return trees, every, own, defined, imports


def _assert_no_shadowed_globals(defined: dict, imports: dict) -> None:
    """Whatever renaming cannot resolve is a build error.

    Renaming (`_plan_renames`) fixes a collision of two DECLARED names. Two
    cases remain, and both are a reason to stop.
      * One name imported in two modules from DIFFERENT sources: `import os`
        in ten modules yields one and the same object and that is the norm,
        but two different `from … import run` under one name are not.
      * A name declared before a neighbour imports it: the build cannot rename
        an import (its module calls it by that name).
    """
    def where(key: str) -> str:
        return _mod_path(key).relative_to(PKG).as_posix()

    bad = []
    declared: dict[str, str] = {}
    origin: dict[str, tuple[str, str]] = {}
    for key in MODULES:
        for name, src in sorted(imports[key].items()):
            if name in declared and name not in _SHADOW_OK:
                bad.append(f"{name}: declared in {where(declared[name])}, while "
                           f"{where(key)} imports it as a module/symbol name")
            was = origin.get(name)
            if was and was[1] != src and name not in _SHADOW_OK:
                bad.append(f"{name}: {where(was[0])} imports {was[1]}, "
                           f"{where(key)} — {src}")
            origin.setdefault(name, (key, src))
        for name in sorted(defined[key]):
            declared.setdefault(name, key)
    if bad:
        raise SystemExit("One name in two modules — the concatenation will keep "
                         "only one value:\n  " + "\n  ".join(bad)
                         + "\n\nRename one of them in the source.")


def _assert_no_shadowed_builtins(defined: dict) -> None:
    """A top-level name that matches a builtin is a build error.

    Found live (2026-10-12): `appwindow.open`, per the RFC contract, became a
    top-level `open` in the concatenation, and EVERYTHING concatenated after it
    got the window-opening function instead of the builtin `open` —
    `open(path, encoding="utf-8")` in the mirror store failed with TypeError.
    The kit still built and compiled: the error waited for launch.

    Renaming cannot fix this: a call to `open(...)` in another module refers to
    the BUILTIN name, and replacing it with a builder's guess is worse than a
    crash. So the name is fixed in the source, and the build stops here with a
    ready explanation.
    """
    reserved = set(dir(builtins))
    bad = []
    for key in MODULES:
        for name in sorted(defined[key]):
            if name in reserved and not name.startswith("_"):
                where = _mod_path(key).relative_to(PKG).as_posix()
                bad.append(f"{name}: declared in {where}, would shadow the builtin "
                           f"{name}() for every module concatenated below")
    if bad:
        raise SystemExit(
            "A builtin function name at module top level:\n  "
            + "\n  ".join(bad)
            + "\n\nRename it in the source (e.g. open -> open_window): "
              "in a flat concatenation there is no other way out.")


def _assert_module_refs(trees: dict, every: dict) -> None:
    """After concatenation, `layout.read_state` goes through the module view.
    Check that the neighbour DOES have such a name: otherwise a typo survives
    until runtime (the view reads the flat namespace and stays silent)."""
    subs = {p: {k.split(".")[-1] for k in MODULES if k.startswith(p + ".")}
            for p in PACKAGES}
    bad = []
    for key in MODULES:
        aliases = {name: target
                   for _node, binds in _rel_imports(key, trees[key])
                   for name, target in binds if target}
        if not aliases:
            continue
        for node in ast.walk(trees[key]):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            target = aliases.get(node.value.id)
            if target is None:
                continue
            known = subs[target] if target in PACKAGES else every[target]
            if node.attr not in known:
                bad.append(f"{_mod_path(key).relative_to(PKG).as_posix()}:"
                           f"{node.lineno}: {node.value.id}.{node.attr} — "
                           f"{target} has no such name")
    if bad:
        raise SystemExit("References to names of neighbouring modules do not match:\n  "
                         + "\n  ".join(bad))


def _local_bindings(tree: ast.Module) -> set[str]:
    """Names that are local INSIDE the module's functions/classes.

    Renaming needs this: a global can be renamed mechanically only if the name
    never turns out to be something else — a parameter, a local variable, an
    `except … as`, an import inside a function. Otherwise one `Name` would go
    to the wrong place, and that would change meaning, not just concatenate.
    """
    out: set[str] = set()
    for scope in ast.walk(tree):
        if not isinstance(scope, _SCOPES):
            continue
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = scope.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs,
                        *[x for x in (a.vararg, a.kwarg) if x]]:
                out.add(arg.arg)
        for n in ast.walk(scope):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                out.add(n.id)
            elif isinstance(n, _SCOPES) and n is not scope:
                out.add(n.name)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                out.add(n.name)
            elif isinstance(n, ast.Global):
                # `global X` cannot be renamed from the parse tree: names in
                # that statement have no positions. Treat the name as taken —
                # better the build refuses than rewrites half of it.
                out.update(n.names)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for al in n.names:
                    out.add(al.asname or al.name.split(".")[0])
    return out


_DEF_LINE_RE = r"^(\s*(?:async\s+)?(?:def|class)\s+){0}\b"


def _rename_globals(src: str, tree: ast.Module, renames: dict[str, str]) -> str:
    """Rename a module's globals from the parse tree (not as text).

    Text replacement will not do: `"URL_PREFIX"` in a string, `mod.URL_PREFIX`
    in a neighbour and `f(URL_PREFIX=1)` are OTHER things, and only references
    to our own global must change. So the edit goes by `Name` positions plus
    one `def`/`class` line. Parse-tree columns are in utf-8 BYTES, so bytes are
    what we slice: otherwise a non-ASCII comment on the same line shifts the slice.
    """
    lines = [ln.encode("utf-8") for ln in src.splitlines()]
    edits: dict[int, list[tuple[int, int, bytes]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in renames:
            edits.setdefault(node.lineno, []).append(
                (node.col_offset, node.end_col_offset,
                 renames[node.id].encode("utf-8")))
        elif isinstance(node, _SCOPES) and node.name in renames:
            i = node.lineno - 1
            line = lines[i].decode("utf-8")
            lines[i] = re.sub(_DEF_LINE_RE.format(re.escape(node.name)),
                              lambda m: m.group(1) + renames[node.name],
                              line, count=1).encode("utf-8")
    for num, spans in edits.items():
        raw = lines[num - 1]
        for start, end, new_name in sorted(spans, reverse=True):
            raw = raw[:start] + new_name + raw[end:]
        lines[num - 1] = raw
    return "\n".join(ln.decode("utf-8") for ln in lines)


def _plan_renames(trees: dict, defined: dict,
                  imports: dict) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Resolve global collisions: the name stays with whoever is concatenated EARLIER.

    The BUILD does the renaming, not an edit of the modules: modules are
    written as modules, with their own namespaces, and asking the author of
    `libui` for kit-wide uniqueness would spread a concatenation constraint
    onto normal code. In the built file the name becomes `URL_PREFIX__libui` —
    you can see where it comes from and that it is not a manual edit.
    """
    renames: dict[str, dict[str, str]] = {}
    problems = []
    first: dict[str, str] = {}
    for key in MODULES:
        for name in sorted(defined[key]):
            if name not in first:
                continue
            if name in _SHADOW_OK:
                continue
            suffix = key.split(".")[-1]
            if name in _local_bindings(trees[key]):
                problems.append(
                    f"{_mod_path(key).relative_to(PKG).as_posix()}: global "
                    f"{name!r} is already declared in "
                    f"{_mod_path(first[name]).relative_to(PKG).as_posix()}, and "
                    f"the build cannot rename it — the same name is local inside "
                    f"the module. Rename it in the source.")
                continue
            renames.setdefault(key, {})[name] = f"{name}__{suffix}"
        # A name becomes taken AFTER the module is analysed: there can be no
        # collision inside one module, but the next one will run into it.
        for name in defined[key] | set(imports[key]):
            first.setdefault(name, key)
    return renames, problems


# Names the build itself puts at the top of the file (`agent/__init__.py`):
# their "source" for `from . import VERSION` is not a module in MODULES but the header.
_ROOT_NAMES = ("APP", "VERSION", "PROTOCOL")


def _plan_import_renames(trees: dict, defined: dict,
                         renames: dict[str, dict[str, str]]
                         ) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Where a dropped SYMBOL import leads if its source was renamed.

    The concatenation drops `from .layout import atomic_write` entirely,
    relying on the name already being in the shared namespace. That holds as
    long as it has the same name there. But `_plan_renames` takes the name away
    from whoever is concatenated LATER — and if a namesake appears in a module
    concatenated before the source (`def atomic_write` in applog), the name in
    layout gets renamed (`atomic_write__layout`), while the dropped import in
    schedule is bound to nothing: its `atomic_write(...)` silently lands in
    someone else's function. The build still passed with rc=0.

    So every dropped symbol is resolved explicitly:
      * the source is a concatenated module and the name is DECLARED in it
        (def/class/assignment), or it is a file-header name
        (APP/VERSION/PROTOCOL). Otherwise — refuse: a symbol the concatenation
        does not provide would fail on the user's machine with a NameError;
      * if the source renamed that name, references IN THE IMPORTING module
        are rewritten to the new name (the plan is returned to the caller and
        merged into its `renames`), just as for its own globals;
      * if they cannot be rewritten (the name is also local in the module, or
        declared in it) — refuse with a location, not a silent substitution.
    Returns ONLY edits for importing modules: they do not affect module views
    (`pins`), hence a separate dict.
    """
    def where(k: str) -> str:
        return _mod_path(k).relative_to(PKG).as_posix()

    extra: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    local = {k: None for k in MODULES}            # _local_bindings — on demand
    for key in MODULES:
        for node, binds in _rel_imports(key, trees[key]):
            base = _rel_base(key, node)
            src = ".".join(base)
            for a, (_bound, target) in zip(node.names, binds):
                if target:
                    continue                      # a module name — a view, not a symbol
                at = f"{where(key)}:{node.lineno}: {ast.unparse(node)}"
                if a.name == "*":
                    problems.append(f"{at} — the concatenation does not allow `*` imports")
                    continue
                bound = a.asname or a.name
                if not base:
                    if a.name not in _ROOT_NAMES:
                        problems.append(
                            f"{at} — the build header only has "
                            f"{', '.join(_ROOT_NAMES)}")
                    elif bound != a.name:
                        problems.append(f"{at} — `as` is not supported for header names")
                    continue
                if src not in MODULES:
                    problems.append(
                        f"{at} — {src!r} is not in MODULES, the symbol will not "
                        f"make it into the build")
                    continue
                if a.name not in defined[src]:
                    problems.append(
                        f"{at} — {a.name!r} is not DECLARED in {where(src)} "
                        f"(the concatenation does not follow re-exports via imports)")
                    continue
                final = renames.get(src, {}).get(a.name, a.name)
                if final == bound:
                    continue                      # same name in the concatenation — nothing to fix
                if bound in defined[key]:
                    problems.append(
                        f"{at} — the module also declares {bound!r} itself: the "
                        f"name from {where(src)} would overwrite it")
                    continue
                if local[key] is None:
                    local[key] = _local_bindings(trees[key])
                if bound in local[key]:
                    problems.append(
                        f"{at} — {a.name!r} from {where(src)} is named "
                        f"{final!r} in the concatenation, and the build cannot "
                        f"rewrite the references: {bound!r} is also a local name "
                        f"inside the module. Rename one of them in the source.")
                    continue
                extra.setdefault(key, {})[bound] = final
    return extra, problems


def _assert_clear_of_entry(defined: dict) -> None:
    """Modules must not declare the names of the header and the single-file entry.

    The header (APP/VERSION/PROTOCOL/BUILD) comes BEFORE the sections, `_MAIN`
    after them. A same-named module global would either overwrite the header or
    be silently overwritten by the entry itself: the same "two globals — one
    name", only the neighbour is not a module.
    """
    entry = _module_names(ast.parse(_MAIN))[2] | set(_ROOT_NAMES) | {"BUILD"}
    bad = [f"{_mod_path(k).relative_to(PKG).as_posix()}: {n}"
           for k in MODULES for n in sorted(defined[k] & entry)]
    if bad:
        raise SystemExit("A module name collides with the build header or entry:\n  "
                         + "\n  ".join(bad)
                         + "\n\nRename it in the module source.")


def _assert_subpackage_complete() -> None:
    """A new module in agent/mirror/ must be added to MODULES.

    The concatenation takes EXACTLY what is listed, so a forgotten module is
    not an error but silence: the file builds, compiles and reaches the user
    without half of the mirror. That is exactly how the kit lived — without the
    whole subpackage.
    """
    listed = {k for k in MODULES if k.startswith("mirror.")}
    on_disk = {f"mirror.{p.stem}" for p in sorted((PKG / "mirror").glob("*.py"))
               if p.name != "__init__.py"}
    missing = sorted(on_disk - listed)
    if missing:
        raise SystemExit(
            "These mirror modules will not make it into the build: " + ", ".join(missing)
            + "\nAdd them to MODULES — in dependency order and BEFORE "
              "controller/tray/server, which call them.")
    gone = sorted(listed - on_disk)
    if gone:
        raise SystemExit("Listed in MODULES but not on disk: " + ", ".join(gone))


def _alias_bindings(trees: dict) -> dict[str, list[tuple[str, str]]]:
    """For each module — which names it binds to modules: [(name, key)]."""
    return {key: [(name, target)
                  for _n, binds in _rel_imports(key, trees[key])
                  for name, target in binds if target]
            for key in MODULES}


def _pins(trees: dict, defined: dict) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Names that must be "pinned" before a module name overwrites them.

    The concrete problem: `mirror/serve.py` declares a function `serve`, and
    `mirror/libui.py` does `from . import serve` — in the flat namespace that
    is ONE name, and the view binding overwrites the function. So right after
    the module's section its own name is saved under a service name
    (`_pin_…`), and the module view looks there first. Renaming
    (`_plan_renames`) does not fix this: what would need renaming is not the
    global but the BINDING in the other module, i.e. all its references to the
    neighbour.

    What CANNOT be fixed this way: if the module name is bound EARLIER than the
    name itself is declared, the overwrite goes the other way — the module view
    is lost. That is a build error, fixed in the source (`import … as`).
    """
    order = {key: i for i, key in enumerate(MODULES)}
    bindings = _alias_bindings(trees)
    alias_names = {name for binds in bindings.values() for name, _t in binds}
    pins: dict[str, dict[str, str]] = {}
    problems = []
    for key in MODULES:
        for name in sorted(defined[key] & alias_names):
            pins.setdefault(key, {})[name] = f"_pin_{_view_var(key)[5:]}_{name}"
            for other, binds in bindings.items():
                if order[other] < order[key] and any(n == name for n, _t in binds):
                    problems.append(
                        f"{other} binds the module name {name!r} before "
                        f"{key} declares its global {name!r} — the concatenation "
                        f"will overwrite the module view. Rename the import: "
                        f"`from . import {name} as _{name}`.")
    return pins, problems


def _assert_entry_views(targets) -> None:
    """The tail calls neighbours by module name — the view must exist.

    The check was added after a live defect: `s3sync.load_s3()` in the kit's
    entry point built, compiled and failed with `NameError` for the user,
    because module views are built from the imports of the MODULES THEMSELVES,
    and the tail is not part of that walk. The same class of error as builtin
    shadowing, and cured the same way — by refusing at build time with a ready
    explanation.
    """
    flat = {m for m in MODULES if "." not in m}
    tree = ast.parse(_MAIN)
    # Names the tail assigns itself (`config = load_config()`,
    # `server = AgentServer(...)`) are not modules, even though they match
    # module names. Without this subtraction the check would complain about
    # every other line.
    bound = {n.id for n in ast.walk(tree)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    bound |= {a.arg for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              for a in n.args.args + n.args.kwonlyargs}
    used = {node.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id in flat and node.value.id not in bound}
    missing = sorted(used - set(targets))
    if missing:
        raise SystemExit(
            "The entry point calls modules by name, but they have no view: "
            + ", ".join(missing)
            + ".\nAdd them to ENTRY_VIEWS — otherwise the build passes and the "
              "command fails with NameError on its first run.")


def _shim_block(trees: dict, pins: dict) -> str:
    """Code of the module views. The list holds only those really called by name."""
    by_target: dict[str, dict[str, str]] = {}
    for key, named in pins.items():
        by_target[key] = named
    targets = sorted({target
                      for key in MODULES
                      for _n, binds in _rel_imports(key, trees[key])
                      for _name, target in binds if target})
    # The package is called as `mirror.schedule` — so its modules need views too.
    for pkg in [t for t in targets if t in PACKAGES]:
        targets += [k for k in MODULES if k.startswith(pkg + ".")]
    targets += [t for t in ENTRY_VIEWS if t not in targets]
    _assert_entry_views(targets)
    lines = [
        "",
        "# " + "=" * 74,
        "#  Module views: the concatenation puts the code into ONE namespace, but",
        "#  some code calls neighbours by module name (`layout.read_state`,",
        "#  `gpsource.run_sources`, `mirror_schedule.MirrorWorker`). Rewriting",
        "#  those references would mean editing tested code, so the module name",
        "#  becomes a VIEW onto the same flat namespace.",
        "#  The view is live (reads globals() at access time): a module that",
        "#  changes its global from a function would drift from a snapshot.",
        "#  The list is derived by the builder from relative imports — do not edit.",
        "# " + "=" * 74,
        "",
        "",
        "class _FlatModule:",
        '    """A module name in the single-file build: a view onto the flat namespace."""',
        "",
        "    def __init__(self, name, subs=None, pins=None):",
        '        self.__dict__["__name__"] = name',
        '        self.__dict__["_subs"] = dict(subs or {})',
        '        # Names overwritten later (see _pins in the builder): look at',
        '        # the saved copy first, then at the ordinary global.',
        '        self.__dict__["_pins"] = dict(pins or {})',
        "",
        "    def __getattr__(self, attr):",
        "        d = self.__dict__",
        '        sub = d["_subs"].get(attr)',
        "        if sub is not None:",
        "            return sub",
        "        g = globals()",
        '        for name in (d["_pins"].get(attr), attr):',
        "            if name in g:",
        "                return g[name]",
        "        raise AttributeError(f\"{d['__name__']}.{attr}\") from None",
        "",
        "    def __setattr__(self, attr, value):",
        "        globals()[attr] = value",
        "",
        "    def __repr__(self):",
        "        return f\"<module {self.__dict__['__name__']} (concatenated)>\"",
        "",
    ]
    for key in sorted(set(targets)):
        pinned = ", ".join(f'"{n}": "{var}"'
                           for n, var in sorted(by_target.get(key, {}).items()))
        if key in PACKAGES:
            subs = ", ".join(f'"{k.split(".")[-1]}": {_view_var(k)}'
                             for k in MODULES if k.startswith(key + "."))
            lines.append(f'{_view_var(key)} = _FlatModule("{key}", {{{subs}}})')
        else:
            lines.append(f'{_view_var(key)} = _FlatModule("{key}", '
                         f'pins={{{pinned}}})' if pinned
                         else f'{_view_var(key)} = _FlatModule("{key}")')
    # The view for the tail is also available under the ORDINARY module name:
    # other names (`folders = _mod_folders`) are bound by the concatenation
    # where a neighbour imports them, but a module called only by the entry
    # point has no such place — and `s3sync.load_s3()` failed with NameError on
    # a live machine.
    for key in ENTRY_VIEWS:
        lines.append(f"{key} = {_view_var(key)}")
    return "\n".join(lines) + "\n"


def verify_built(path: Path) -> list[str]:
    """Problems with the built FILE (see `verify_text`)."""
    return verify_text(path.read_text(encoding="utf-8"), str(path))


def verify_text(text: str, label: str) -> list[str]:
    """Problems with the build: does it compile and does it contain the mirror.

    An empty list = all good. The check exists precisely because a missing
    mirror is NOT visible by anything: the file compiles, the agent starts, and
    the report library simply does not answer.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [f"does not compile: {e}"]
    try:
        compile(text, label, "exec")
    except SyntaxError as e:                       # pragma: no cover
        return [f"does not compile: {e}"]
    names = _module_names(tree)[0]
    out = [f"no name {name} (from {src}) — the mirror code did not make it into the build"
           for name, src in sorted(_MIRROR_MARKERS.items()) if name not in names]
    # The mirror routes hang on a single prefix; a mismatch between the build
    # and layout.py would mean some other file got concatenated.
    want = _const_value(ast.parse(_mod_path("mirror.layout").read_text(encoding="utf-8")),
                        "URL_PREFIX")
    got = _const_value(tree, "URL_PREFIX")
    if want is not None and got != want:
        out.append(f"URL_PREFIX in the build is {got!r}, in mirror/layout.py {want!r}")
    # The agent's kit self-check (`_kit_problems`) compares against this list:
    # stale or empty — and the check would let a kit without pages through.
    res = tuple(rel for rel, _src in kit_resources())
    if _const_value(tree, "_KIT_RESOURCES") != res:
        out.append("_KIT_RESOURCES in the build does not match the kit contents "
                   "(agent/mirror/ + " + ", ".join(HOST_FILES)
                   + ") — the agent's kit self-check is useless")
    return out


def _const_value(tree: ast.Module, name: str):
    """Value of a top-level constant, or None."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name) and t.id == name:
                try:
                    return ast.literal_eval(node.value)
                except ValueError:
                    return None
    return None


# ==========================================================================
#  Mirror resources: the concatenation carries only CODE
# ==========================================================================

def _assets_module():
    """The agent/mirror/assets.py module, loaded by path.

    By path, not as a package import: the resource naming contract
    (`RES_DIRNAME`, the name allow-list alphabet) belongs to it, and
    duplicating it here would mean drifting apart one day. It has no relative
    imports, so loading it is safe.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_op_mirror_assets",
                                                  PKG / "mirror" / "assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mirror_resources() -> list[tuple[str, Path]]:
    """Mirror resources for the kit: [(path inside the kit, file in the repository)].

    The contents come from the repository, not from a list in the script: a new
    page in `agent/mirror/web/` goes into the kit by itself. Names are checked
    with the same `res_path` the agent will use to find them — a resource whose
    name is outside the allow-list alphabet would reach the kit and be served
    as a 404.
    """
    a = _assets_module()
    src = PKG / "mirror"
    out = [(f"{a.RES_DIRNAME}/{a.STRINGS_NAME}", src / a.STRINGS_NAME)]
    web = src / a.WEB_DIRNAME
    for p in sorted(web.iterdir()) if web.is_dir() else []:
        if p.is_file() and not p.name.startswith("."):
            out.append((f"{a.RES_DIRNAME}/{a.WEB_DIRNAME}/{p.name}", p))
    return out


def _foldersui_module():
    """The agent/foldersui.py module, loaded by path — for the resource names.

    By path, not as a package import: `agent/__init__.py` drags in the whole
    agent, and here we need three constants (`RES_DIRNAME`, `WEB_DIRNAME`,
    `_NAME_RE`). Loading would require the module not to execute relative
    imports at top level... and it does (`from . import appwindow, folders,
    foldersio`), so we read the constants by parsing the source — duplicating
    them here would mean drifting apart one day from what the agent uses to
    find its pages.
    """
    import ast as _ast
    tree = _ast.parse((PKG / "foldersui.py").read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, _ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], _ast.Name) \
                and isinstance(node.value, _ast.Constant) \
                and isinstance(node.value.value, str):
            out[node.targets[0].id] = node.value.value
    missing = {"FOLDERS_RES_DIRNAME", "FOLDERS_WEB_DIRNAME"} - set(out)
    if missing:
        raise SystemExit("constants not found in agent/foldersui.py: "
                         + ", ".join(sorted(missing)))
    return out


def window_resources() -> list[tuple[str, Path]]:
    """Window resources ("Report folders") for the kit: [(path in the kit, file in the repo)].

    As with the mirror, the contents come from the repository, not from a list
    in the script: a new page in `agent/web/` goes into the kit by itself.
    Names are checked against the same allow-list alphabet the agent will use
    to find them (`foldersui._NAME_RE`) — a resource with an unfit name would
    reach the kit and be served as a 404.
    """
    c = _foldersui_module()
    src = PKG / "web"
    out = []
    for p in sorted(src.iterdir()) if src.is_dir() else []:
        if not p.is_file() or p.name.startswith("."):
            continue
        if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$", p.name):
            raise SystemExit(
                f"{p}: the name fails the foldersui._NAME_RE allow-list alphabet — "
                "the agent will answer 404 for it")
        out.append((f"{c['FOLDERS_RES_DIRNAME']}/"
                    f"{c['FOLDERS_WEB_DIRNAME']}/{p.name}", p))
    if not out:
        raise SystemExit("agent/web/ has no files at all: the \"Report "
                         "folders\" screen would reach the user empty")
    return out


def host_resources() -> list[tuple[str, Path]]:
    """Separate kit files (`HOST_FILES`): [(path inside the kit, file in the repo)].

    They lie in the kit ROOT, next to `op_agent.py`: that is exactly where
    `generator._r2_host_path()` looks for them and where `assets.res_path`
    finds them (its first root is the directory of `op_agent.py` itself), so
    the agent's kit self-check handles them with the same code as the mirror
    resources.
    """
    out = []
    for name, (src, why) in HOST_FILES.items():
        if not src.is_file():
            raise SystemExit(f"missing {src}: the kit would build without it, but {why}")
        out.append((name, src))
    return out


def kit_resources() -> list[tuple[str, Path]]:
    """Everything that must lie next to `op_agent.py`: the mirror + separate files.

    One list for installing, for checking and for the agent's self-check — let
    two of them drift and the kit ships without a file nobody misses until it
    reaches the user's machine.
    """
    return mirror_resources() + window_resources() + host_resources()


def install_resources(kit: Path) -> list[str]:
    """Put the mirror resources into the kit. Returns paths inside the kit.

    The copy is BYTE FOR BYTE (`shutil.copyfile`): .html/.js/.css/.json must
    not be converted to CRLF — line breaks inside JS strings and the hashes the
    pages are checked against would shift. Only .bat/.txt in the kit need CRLF,
    and make-agent-kit.sh takes care of that.
    """
    done = []
    for rel, src in kit_resources():
        dst = kit / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        done.append(rel)
    return done


def check_kit(kit: Path) -> int:
    """Check the CONTENTS of the built kit. This is the last line of defence before the user."""
    failures = []

    def check(label, ok, extra=""):
        print(("PASS " if ok else "FAIL ") + label
              + (f"  <-- {extra}" if extra and not ok else ""))
        if not ok:
            failures.append(label)

    print(f"kit: {kit}\n")
    agent = kit / "op_agent.py"
    check("op_agent.py is in place", agent.is_file())
    if agent.is_file():
        for problem in verify_built(agent) or []:
            check("op_agent.py: " + problem, False)
        if not verify_built(agent):
            check("op_agent.py compiles and contains the mirror code", True)

    a = _assets_module()
    res = kit_resources()
    check(f"files next to op_agent.py are listed ({len(res)})", bool(res),
          "agent/mirror/ has neither strings.json nor web/")
    for rel, src in res:
        name = Path(rel).name
        web = Path(rel).parent.name == a.WEB_DIRNAME
        dst = kit / rel
        # What an omission leads to depends on the file, and the user needs to
        # be told: without a mirror page the library is silently empty, without
        # r2host.py the RUN fails (`gen_no_host`), and the cause surfaces at the
        # analyst's.
        why = (HOST_FILES[rel][1] if rel in HOST_FILES
               else "the concatenation carries only code — the resource is put "
                    "in by --install-resources")
        check(f"{rel}: present in the kit", dst.is_file(), why)
        if not dst.is_file():
            continue
        # The name is checked with THE SAME rule the agent uses to find the file
        # next to itself. For mirror resources we call res_path itself (it will
        # also find them in the repository — extra confirmation that the list
        # is not made up); for files in the kit root (HOST_FILES) there is no
        # other directory in the repository, so only the name is checked — its
        # allow-list alphabet.
        # Window resources live under their own root (`appwin/web/`), and they
        # are found not by `mirror.assets` but by `foldersui.res_path`; the two
        # halves share exactly one thing — the name alphabet, so that is what
        # we check.
        ok_name = (a.res_path(name, web=web) is not None
                   if rel != name and rel.startswith(a.RES_DIRNAME + "/")
                   else a._NAME_RE.match(name) is not None)
        check(f"{rel}: name passes the assets._NAME_RE allow-list alphabet", ok_name,
              "unfit name — the agent will answer 404 / not find the file")
        # Byte for byte against the ORIGINAL, not against "some copy": the
        # source of `_lineage.py` lives in static/py/ and is also read by the
        # browser engine. A copy in the kit that drifted from it means two
        # different provenances for one report, and nothing else would notice.
        check(f"{rel}: byte-for-byte identical to {src.relative_to(ROOT)}",
              dst.read_bytes() == src.read_bytes(),
              "size or line endings differ (converted to CRLF? the copy in the "
              "kit edited instead of the original?)")
    # .bat and .txt in the kit need CRLF (cmd.exe and Notepad read line by
    # line). A file written bypassing make-agent-kit.sh's perl loop (that is
    # how BUILD-INFO.txt once came out) passed silently. Count bare LFs:
    # EVERYTHING must be converted to CRLF, not "mostly".
    for f in sorted([*kit.glob("*.bat"), *kit.glob("*.txt")]):
        raw = f.read_bytes()
        bare = raw.count(b"\n") - raw.count(b"\r\n")
        check(f"{f.name}: CRLF line endings", bare == 0,
              f"{bare} lines with a bare LF")

    strings = kit / f"{a.RES_DIRNAME}/{a.STRINGS_NAME}"
    if strings.is_file():
        try:
            data = json.loads(strings.read_text(encoding="utf-8"))
        except ValueError as e:
            data = None
            check(f"{a.STRINGS_NAME}: parses as JSON", False, str(e))
        if data is not None:
            check(f"{a.STRINGS_NAME}: non-empty dictionary of texts",
                  isinstance(data, dict) and bool(data))

    print()
    print("ALL PASS" if not failures else f"{len(failures)} FAILURES: "
          + ", ".join(failures))
    return 1 if failures else 0


def _assert_host_not_glued() -> None:
    """`r2host.py` must not end up in MODULES.

    It is launched as a process, not imported: in the concatenation its
    `main()` and argv parsing would collide with the agent's entry point, and
    in the kit the file has to lie separately anyway. The check is cheap and
    the error would be silent.
    """
    stems = {name.rpartition(".")[2] for name in MODULES}
    bad = sorted(stems & {Path(n).stem for n in HOST_FILES})
    if bad:
        raise SystemExit("MODULES contains a file the kit carries separately: "
                         + ", ".join(bad))


def build(out_path: Path) -> Path:
    c = _read_constants()
    _assert_host_not_glued()
    host_resources()                     # file missing in the repo — do not build the kit
    _assert_subpackage_complete()
    trees, every, _own, defined, imports = _analyze()
    _assert_no_shadowed_globals(defined, imports)
    _assert_no_shadowed_builtins(defined)
    _assert_module_refs(trees, every)
    _assert_clear_of_entry(defined)
    renames, problems = _plan_renames(trees, defined, imports)
    # Edits for IMPORTING modules (the source renamed a name) go only into the
    # module body; they have no place in `pins` (views), see _plan_import_renames.
    import_renames, import_problems = _plan_import_renames(trees, defined, renames)
    problems += import_problems
    pins, order_problems = _pins(trees, defined)
    problems += order_problems
    if problems:
        raise SystemExit("Names in the concatenation do not match:\n  "
                         + "\n  ".join(problems))
    # Neighbours see a renamed name through the module view — let the view know
    # what it is called now.
    for key, named in renames.items():
        pins.setdefault(key, {}).update(named)
    parts = [
        '#!/usr/bin/env python3',
        f'"""op-agent {c["VERSION"]} — single-file build (OnePage engine #2, GreenPlum).',
        "",
        "GENERATED from the agent/ package by deploy/build_agent_single.py —",
        "do not edit by hand; edit the modules and rebuild.",
        "",
        "Quick start on the analyst's machine:",
        "    python op_agent.py                 # the 1st run creates an agent.ini template",
        "    <fill in agent.ini: host/dbname/user>",
        '    pip install "psycopg[binary]"      # GreenPlum driver',
        "    python op_agent.py set-cred analytics_dw   # password -> Windows DPAPI",
        "    python op_agent.py test-conn analytics_dw  # connection check (as in DBeaver)",
        "    python op_agent.py                 # daemon on 127.0.0.1:47113",
        "",
        "Listens on loopback ONLY. The GP password never leaves the process and is",
        "never written to disk in clear text. The agent holds no OnePage secrets and",
        "does not talk to the server.",
        "",
        "The report mirror takes its texts and pages from the mirror/ subdirectory NEXT",
        "TO this file (mirror/strings.json, mirror/web/*) — the concatenation carries",
        "only code. r2host.py lies right there too: a generator run over local folders",
        "launches it as a separate process, so it is not part of this concatenation.",
        '"""',
        "from __future__ import annotations",
        "",
        f'APP = {c["APP"]!r}',
        f'VERSION = {c["VERSION"]!r} + "-single"',
        f'PROTOCOL = {c["PROTOCOL"]!r}',
        f'BUILD = {_build_stamp()!r}',
    ]
    # Module views are declared BEFORE the sections: `from . import sync as _sync`
    # inside a `try:` in mirror/schedule.py becomes an in-place binding, and the
    # view must already exist by then.
    parts.append(_shim_block(trees, pins))
    for name in MODULES:
        parts.append(_strip_module(name, {**renames.get(name, {}),
                                          **import_renames.get(name, {})}))
        # A copy of names that a module name from later sections would overwrite.
        for own_name, var in sorted(pins.get(name, {}).items()):
            if own_name in renames.get(name, {}):
                continue               # a renamed name has nothing to overwrite it
            parts.append(f"{var} = {own_name}            "
                         f"# [concatenation] see _pins\n")
    parts.append(_MAIN.replace(
        '("__KIT_RESOURCES__",)',
        repr(tuple(rel for rel, _src in kit_resources()))))

    text = "\n".join(parts)
    # Check BEFORE writing: a file with incomplete contents must not appear on
    # disk at all — otherwise make-agent-kit.sh would pick it up as the next step.
    problems = verify_text(text, str(out_path))
    if problems:
        raise SystemExit("The build completed, but its contents are wrong:\n  "
                         + "\n  ".join(problems))
    out_path.write_text(text, encoding="utf-8")
    return out_path


def _usage() -> int:
    print(__doc__)
    return 2


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--check-kit":
        if len(argv) != 2:
            raise SystemExit(_usage())
        raise SystemExit(check_kit(Path(argv[1])))
    if argv and argv[0] == "--install-resources":
        if len(argv) != 2:
            raise SystemExit(_usage())
        kit = Path(argv[1])
        for rel in install_resources(kit):
            print(f"    {rel}")
        raise SystemExit(0)
    if argv and argv[0] == "--list-resources":
        for rel, _src in kit_resources():
            print(rel)
        raise SystemExit(0)
    if argv and argv[0].startswith("-"):
        raise SystemExit(_usage())
    out = Path(argv[0]) if argv else ROOT / "docs" / "agent_m2_kit" / "op_agent.py"
    out.parent.mkdir(parents=True, exist_ok=True)
    p = build(out)
    print(f"OK: {p} ({p.stat().st_size} bytes, {len(p.read_text(encoding='utf-8').splitlines())} lines)")
    # There are deliberately NO resources next to the built file: a copy of
    # strings.json and web/ in the repository would silently drift from the
    # original. make-agent-kit.sh puts them into the kit, and without them the
    # mirror starts with no texts and no pages.
    print(f"    more files are needed alongside ({', '.join(rel for rel, _ in kit_resources())}):"
          f" --install-resources <directory next to {p.name}>")
