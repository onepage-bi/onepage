"""The report library window: our own `msedge.exe --app`, not a browser tab.

Why. The owner called the program a "sucker agent" because of one line: the
tray menu opened the library through `webbrowser.open`, that is, as a tab among
forty others. Here the same content opens as a window without an address bar
and tabs, with its own icon in the taskbar. The server, the pages and the
synchronizer do not change at all -- only WHAT opens the address changes.

Contract (docs/RFC_DESKTOP_MIRROR.md, "Window and folders"):
    find_edge()                         -> Path | None
    open(url, *, state_root, title)     -> "app" | "browser"
`open` NEVER raises: the tray icon calls it from a menu handler, and an
unhandled exception there would quietly kill the shell thread. Any failure
(no Edge, it did not start, not Windows) ends with a plain `webbrowser.open`
and the answer "browser".

Testability without Windows. We have none, so the Edge search, the process
launch and the fallback are THREE hooks: optional keyword parameters
`find`/`launch`/`fallback` (the test passes them directly). Module-level names
(`_launch`, `_fallback`) are read at call time, not at import, so patching
through monkeypatch works too. Default parameters equal to the functions
themselves would not give this: a default value is fixed at definition time.

Standard library only: the kit is installed offline, there will be no new wheels.
Note: the name `open` in this module shadows the builtin, so files are not
opened here at all -- otherwise the breakage would surface far from the cause.
"""
from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

WINDOW_SIZE = "1280,860"
PROFILE_DIR = "window"

# The address of the "Report folders" screen on the agent's loopback. Declared HERE,
# not by whoever opens the window, for the same reason `mirror.libui`
# takes its prefix from `schedule.library_url`: two literals diverge one
# day, and then the menu item opens a 404. The screen itself lives in
# `agent/foldersui.py` (`foldersui.PREFIX`), and it does the cross-check: the import goes
# that way (`foldersui` -> `appwindow`), because this module must remain
# a leaf -- it is called by `controller`, which is bundled into the kit earlier.
FOLDERS_PATH = "/folders-ui/"

_EXE = "msedge.exe"
_REL = Path("Microsoft") / "Edge" / "Application" / _EXE
_APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"

# CreateProcess flags are given as numbers: subprocess has no such constants
# on non-Windows, and the module must import there too (tests, dev on macOS).
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _registry_paths() -> list[str]:
    """App Paths\\msedge.exe values from HKCU and HKLM, including the 32-bit view.

    The registry is needed because Edge is installed not only in Program Files:
    corporate builds put it on another drive, and then the only honest trace is
    App Paths, which the installer itself writes. Both views (64 and 32 bit) are
    tried explicitly: without KEY_WOW64_* a 32-bit process would see only "its
    own" half. Any read error is simply "not here": the search has no right to fail.
    """
    try:
        import winreg                                    # Windows only
    except ImportError:
        return []
    found: list[str] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, _APP_PATHS, 0,
                                    winreg.KEY_READ | view) as k:
                    val, _ = winreg.QueryValueEx(k, "")   # the default value
                if val:
                    found.append(os.path.expandvars(str(val)).strip('"'))
            except OSError:
                continue
    return found


def find_edge(*, env=None, registry=None) -> Path | None:
    """The path to msedge.exe, or None.

    Directories first (cheap and covers 99%), then the registry. `env` and
    `registry` are test hooks: an environment dict and a function returning a
    list of paths. Existence is checked with is_file(), not by trusting the entry:
    the registry keeps leftovers of uninstalled installations.
    """
    env = os.environ if env is None else env
    candidates: list[Path] = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = env.get(var)
        if base:
            candidates.append(Path(base) / _REL)
    reg = _registry_paths if registry is None else registry
    try:
        candidates.extend(Path(p) for p in reg() if p)
    except Exception:                                    # noqa: BLE001
        pass                                             # the registry is no reason to fail
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def build_args(edge: Path, url: str, state_root) -> list[str]:
    """Launch arguments. A list, not a string, and the url is passed as is.

    If the command line is assembled by hand and then given to Popen as a
    string, the quotes pass through list2cmdline a second time, and
    `--app="http://..."` turns into an address with quotes inside: Edge opens
    an empty error page. A list escapes exactly once and exactly where needed.

    --user-data-dir INSIDE the program folder: the person's profile (his
    bookmarks, passwords, account sign-in) is neither seen nor touched by the
    window. A side benefit: a separate profile gives a separate Edge process,
    and --window-size takes effect instead of being lost in an already open window.
    """
    profile = Path(state_root) / PROFILE_DIR
    return [str(edge), f"--app={url}", f"--user-data-dir={profile}",
            f"--window-size={WINDOW_SIZE}", "--no-first-run"]


def _launch(args: list[str]) -> None:
    """Launch a detached process and do not wait for it.

    The window must outlive the call (the tray menu has returned and the window
    lives), and the agent must not depend on its closing -- otherwise the menu
    thread would stand until the person closed the window. Hence no wait() and
    no pipes: the streams go to DEVNULL, otherwise the child process holds the
    agent's log descriptors.
    """
    kw: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True)
    if sys.platform == "win32":
        kw["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)                         # noqa: S603


def _fallback(url: str) -> None:
    webbrowser.open(url)


def open_window(url: str, *, state_root, title: str = "", find=None, launch=None,
                fallback=None) -> str:
    """Open the address as a window ("app") or, if that is impossible, as a tab ("browser").

    The name is NOT `open`, although the RFC contract said exactly that: the
    single-file bundle puts all modules into ONE flat namespace, so a top-level
    `def open(...)` shadows the builtin `open` for everything bundled after it
    -- and the kit failed on `open(path, encoding="utf-8")` inside the mirror
    store. The builder now refuses such a name (`_assert_no_shadowed_builtins`),
    and the contract in the RFC has been corrected.

    Edge does not accept `title` in --app: the window title comes from the
    page's <title>. The parameter is kept per the contract and goes to the log,
    so that the "opened as a tab" line says at once which window it is about.

    The log (print goes to applog under pythonw) names the chosen path and the
    reason: "opened as a tab" is the first question in any complaint.
    """
    find = find or find_edge
    launch = launch or _launch
    fallback = fallback or _fallback
    tag = f"[window] {title or url}:"
    reason = ""
    try:
        edge = find()
        if edge is None:
            reason = "Edge not found"
        else:
            args = build_args(Path(edge), url, state_root)
            launch(args)
            print(f"{tag} app window, {edge}", flush=True)
            return "app"
    except Exception as e:                               # noqa: BLE001
        reason = f"launch failed: {e!r}"
    print(f"{tag} opening in a browser tab ({reason})", flush=True)
    try:
        fallback(url)
    except Exception as e:                               # noqa: BLE001
        print(f"{tag} and the tab did not open either: {e!r}", flush=True)
    return "browser"


def folders_url(port: int) -> str:
    """The address of the "Report folders" screen for the window: `http://127.0.0.1:<port>/folders-ui/`.

    Only `127.0.0.1`, not `localhost`: on a corporate machine `localhost`
    resolves to `::1` and -- through the system proxy -- to anything else
    (exactly the pitfall that made polling our own port go through the proxy,
    see `agent/README.md`). There is only one name that is guaranteed to mean
    this machine.
    """
    return f"http://127.0.0.1:{int(port)}{FOLDERS_PATH}"


def open_folders(port: int, *, state_root, **kw) -> str:
    """Open the "Report folders" screen as a window. Returns "app" or "browser".

    A separate function, rather than `open_window(folders_url(port), ...)` at
    each caller: the log title and the address must be the same at all entry
    points (the tray menu, `op-agent.bat folders`), otherwise the "opened as a
    tab" line stops answering which window it is about.
    """
    return open_window(folders_url(port), state_root=state_root,
                       title="OnePage Portable — folders", **kw)


# --------------------------------------------------------------------------- #
# System folder and file picker                                               #
# --------------------------------------------------------------------------- #
#
# Why it exists at all. The "Report folders" screen can do everything except one thing: a path
# has to be PASTED into it as text. A person who sees "no section ... in
# folders.ini" reasonably asks why the folder cannot simply be picked with the
# mouse -- and he is right. The dialog below closes exactly this gap; line-by-line
# config writing (`foldersio`) and path checking (`folders.resolve`) are already written and do not change.
#
# Why ctypes and not tkinter. The kit has no tkinter: the portable CPython build
# from nuget does not carry it, and dragging Tcl/Tk in for one dialog is megabytes
# in the offline bundle. Shell COM exists on every Windows and costs nothing.
#
# Why IFileOpenDialog and not SHBrowseForFolder. The old tree can neither paste
# a path as a string nor open a UNC share (`\\server\exchange`), and the exports
# live exactly there. The new dialog has an address bar, and it is the same choice
# the person makes in Explorer.
_CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
_IID_IFileOpenDialog = "{D57C7288-D4AD-4768-BE02-9D969532D960}"

_FOS_PICKFOLDERS = 0x00000020
_FOS_FORCEFILESYSTEM = 0x00000040        # no "Libraries" or other virtual places
_FOS_PATHMUSTEXIST = 0x00000800
_FOS_NOCHANGEDIR = 0x00000008            # the dialog does not change the process CWD
_SIGDN_FILESYSPATH = 0x80058000
_CLSCTX_INPROC_SERVER = 1
_RPC_E_CHANGED_MODE = -2147417850
_ERROR_CANCELLED = -2147023673           # the person pressed "Cancel" -- this is NOT a failure


def _guid(ctypes, text: str):
    """A string GUID -> a CLSID structure through CLSIDFromString (we have no parser of our own)."""
    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16),
                    ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]
    g = GUID()
    hr = ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    if hr != 0:
        raise OSError(f"CLSIDFromString({text}) = 0x{hr & 0xFFFFFFFF:08X}")
    return g


def _vcall(ctypes, ptr, index: int, restype, argtypes, *args):
    """Call a COM method by its vtable index.

    The indexes are the interface's contract, and a mistake in them is caught
    neither by the compiler nor by a test on macOS: a miss calls a NEIGHBOURING
    method with foreign arguments, that is, a process crash. So each index
    below is labelled with the method name, and the order is taken from `shobjidl_core.h`.
    """
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p * 64)).contents
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(ptr, *args)


def pick_path(kind: str = "dir", *, title: str | None = None) -> str | None:
    """Show the system picker dialog and return the path (or None).

    None means "nothing chosen" and deliberately covers THREE different cases:
    not Windows (development on macOS), the person pressed "Cancel", the dialog
    did not come up. For the caller they are the same -- there is nothing to
    write to the config -- and the reason goes to the log: there is no point in
    telling them apart on screen, but behaviour must not change silently.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as e:                                   # noqa: BLE001
        print(f"[window] the picker dialog is unavailable: {e!r}", flush=True)
        return None

    ole32 = ctypes.windll.ole32
    # COM may already be up in this thread under another model -- then
    # CoInitializeEx returns RPC_E_CHANGED_MODE, and we have NO right to shut
    # COM down afterwards (we would break the thread's other owner).
    hr = ole32.CoInitializeEx(None, 0x2)                     # APARTMENTTHREADED
    owns_com = hr in (0, 1)                                  # S_OK | S_FALSE
    if hr not in (0, 1, _RPC_E_CHANGED_MODE):
        print(f"[window] CoInitializeEx = 0x{hr & 0xFFFFFFFF:08X}", flush=True)
        return None

    dlg = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(_guid(ctypes, _CLSID_FileOpenDialog)), None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_guid(ctypes, _IID_IFileOpenDialog)), ctypes.byref(dlg))
        if hr != 0 or not dlg:
            print(f"[window] the picker dialog was not created: 0x{hr & 0xFFFFFFFF:08X}", flush=True)
            return None

        opts = _FOS_FORCEFILESYSTEM | _FOS_PATHMUSTEXIST | _FOS_NOCHANGEDIR
        if kind != "file":
            opts |= _FOS_PICKFOLDERS
        _vcall(ctypes, dlg, 9, ctypes.HRESULT, [wintypes.DWORD], opts)      # SetOptions
        if title:
            _vcall(ctypes, dlg, 17, ctypes.HRESULT, [ctypes.c_wchar_p],     # SetTitle
                   ctypes.c_wchar_p(title))

        hr = _vcall(ctypes, dlg, 3, ctypes.c_int32, [wintypes.HWND], None)  # Show
        if hr != 0:
            if hr != _ERROR_CANCELLED:
                print(f"[window] the picker dialog closed with 0x{hr & 0xFFFFFFFF:08X}", flush=True)
            return None

        item = ctypes.c_void_p()
        hr = _vcall(ctypes, dlg, 20, ctypes.c_int32,                        # GetResult
                    [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(item))
        if hr != 0 or not item:
            return None
        try:
            buf = ctypes.c_wchar_p()
            hr = _vcall(ctypes, item, 5, ctypes.c_int32,                    # GetDisplayName
                        [wintypes.DWORD, ctypes.POINTER(ctypes.c_wchar_p)],
                        _SIGDN_FILESYSPATH, ctypes.byref(buf))
            if hr != 0 or not buf.value:
                return None
            path = str(buf.value)
            # The shell allocated the string memory -- we free it with the same allocator.
            ole32.CoTaskMemFree(buf)
            return path
        finally:
            _vcall(ctypes, item, 2, ctypes.c_uint32, [])                    # Release
    except Exception as e:                                   # noqa: BLE001
        # The dialog is a convenience: its failure must not bring down either the handler
        # thread or the folders screen. The path can still be pasted as text.
        print(f"[window] the picker dialog did not work: {e!r}", flush=True)
        return None
    finally:
        if dlg:
            _vcall(ctypes, dlg, 2, ctypes.c_uint32, [])                     # Release
        if owns_com:
            ole32.CoUninitialize()
