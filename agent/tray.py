"""Tray icon — the agent's face for the analyst (M4).

The goal of this step: remove the command line from everyday use. The icon by
the clock shows the state by colour, the question "OnePage wants to connect —
allow?" arrives as a modal window, the password is entered in a dedicated
window, diagnostics open from a menu item.

The module has two layers, and that is the main decision:

  * `TrayState` — a PURE state machine: states, labels, availability of menu
    items. Not a single ctypes call, so it is tested on macOS along with
    everything else (`agent/tests/smoke_tray.py`);
  * `_Win32Tray` — a thin shell: window, message loop, `Shell_NotifyIcon`,
    `MessageBoxW`, the password input window. Verified on Windows only, so it
    has no logic at all — only drawing.

There is no policy here either: what "test the connection" means and how to save
the password is known by the controller that the entry point passes in. The module knows
nothing about GP or DPAPI.

Icons are drawn in memory (`CreateIcon`) — no .ico files, no Pillow: the agent
stays a single .py file, and the state colours live in one place.

Standard library only.
"""
from __future__ import annotations

import sys
import threading

# [mirror] texts and kinds of the mirror state. A top-level import: in the
# single-file build a relative import inside a function does not survive the merge.
from .mirror import schedule as _mirror

# ── States ──────────────────────────────────────────────────────────────────
ST_NO_PASSWORD = "no_password"
ST_READY = "ready"
ST_BUSY = "busy"
ST_ERROR = "error"
# [mirror] States of the background sync of report copies. Colours come from the
# palette above, no new ones: blue "work in progress", green "fresh", grey "no
# connection" (like "not configured" — nothing for the person to do, just wait), red
# "your action is needed" (key died, no disk space).
ST_SYNC = "mirror_sync"
ST_MIRROR_FRESH = "mirror_fresh"
ST_OFFLINE = "mirror_offline"
ST_KEY_DEAD = "mirror_key_dead"
ST_MIRROR_DISK = "mirror_no_disk"

# BGR (Windows order), not RGB.
_COLORS = {
    ST_NO_PASSWORD: (0x9E, 0x9E, 0x9E),   # grey  — password not set
    ST_READY:       (0x4C, 0xAF, 0x50),   # green — ready
    ST_BUSY:        (0xF5, 0x9E, 0x21),   # blue  — export in progress
    ST_ERROR:       (0x43, 0x43, 0xE5),   # red — the last attempt failed
    ST_SYNC:        (0xF5, 0x9E, 0x21),   # [mirror] blue  — updating copies
    ST_MIRROR_FRESH: (0x4C, 0xAF, 0x50),  # [mirror] green — copies are fresh
    ST_OFFLINE:     (0x9E, 0x9E, 0x9E),   # [mirror] grey  — platform unavailable
    ST_KEY_DEAD:    (0x43, 0x43, 0xE5),   # [mirror] red — key expired/not accepted
    ST_MIRROR_DISK: (0x43, 0x43, 0xE5),   # [mirror] red — no disk space
}

# Menu item identifiers (also the WM_COMMAND commands).
CMD_PASSWORD = 1001
CMD_TEST = 1002
CMD_UNPAIR = 1003
CMD_LOG = 1004
CMD_QUIT = 1005
# [mirror]
CMD_SYNC = 1006
CMD_LIBRARY = 1007
CMD_STATE = 1008
CMD_KEY = 1009
# Report folders: this item is NOT part of the mirror block — a local run works without
# syncing copies, so it is always available.
CMD_FOLDERS = 1010


class TrayState:
    """What to show. Pure logic — no Windows.

    The source of truth is the status from the controller; `apply()` returns True if the
    visible state changed (then the shell redraws the icon). This
    avoids redrawing the icon on a timer every half second.
    """

    def __init__(self, connection: str = ""):
        self.connection = connection
        self.has_password = False
        self.busy = False
        self.paired = False
        self.error: str | None = None
        self.mirror: dict | None = None          # [mirror] MirrorWorker snapshot
        self._state = ST_NO_PASSWORD

    def apply(self, status: dict) -> bool:
        before = (self._state, self.tooltip)
        self.has_password = bool(status.get("has_password"))
        self.busy = bool(status.get("busy"))
        self.paired = bool(status.get("paired"))
        self.error = status.get("error") or None
        self.mirror = status.get("mirror") or None
        mk = self._mirror_kind()
        # Order of branches = display priority. "Busy" matters more than a past error:
        # the icon must show what is happening now, not history.
        if self.busy:
            self._state = ST_BUSY
        elif mk == _mirror.M_SYNCING:
            self._state = ST_SYNC                      # [mirror]
        elif not self.has_password and not self._mirror_has_key():
            self._state = ST_NO_PASSWORD
        elif self.error:
            self._state = ST_ERROR
        elif mk in _mirror.KEY_DEAD:                   # [mirror] key — a human action
            self._state = ST_KEY_DEAD
        elif mk in (_mirror.M_NO_DISK, _mirror.M_QUOTA):
            # One colour for two kinds on purpose: for the icon it is one urgency —
            # "copies are not updating, a human action is needed". WHAT exactly to
            # do is distinguished by the texts (disk space vs. own quota);
            # a separate colour the person would not read anyway.
            self._state = ST_MIRROR_DISK
        elif mk == _mirror.M_UNAVAILABLE:
            self._state = ST_OFFLINE
        elif self.has_password:
            self._state = ST_READY
        elif mk == _mirror.M_FRESH:
            self._state = ST_MIRROR_FRESH
        else:
            # A key exists, no first sync yet, no GP password:
            # grey, not green — "fresh" without a single sync would be a lie.
            self._state = ST_OFFLINE
        return (self._state, self.tooltip) != before

    @property
    def state(self) -> str:
        return self._state

    @property
    def color(self) -> tuple[int, int, int]:
        return _COLORS[self._state]

    # [mirror] ─ helpers for the mirror state ──────────────────────────
    def _mirror_on(self) -> bool:
        return bool(self.mirror and self.mirror.get("enabled"))

    def _mirror_has_key(self) -> bool:
        return self._mirror_on() and bool(self.mirror.get("has_key"))

    def _mirror_kind(self) -> str | None:
        return self.mirror.get("kind") if self._mirror_on() else None

    def _mirror_text(self) -> str:
        """First sentence of the status line: the icon tooltip is 127 chars,
        while the full text (with "will retry in N min") is read by the person in the
        "Status" item. Cutting by length is not allowed — it chopped off the number of minutes."""
        full = _mirror.status_text(self.mirror) if self._mirror_on() else ""
        return full.split(". ", 1)[0].rstrip(".") if full else ""

    @staticmethod
    def _clip(s: str, n: int = 120) -> str:
        """Windows tooltip — up to 127 characters. Cut on a word, with an ellipsis."""
        if len(s) <= n:
            return s
        cut = s[:n].rsplit(" ", 1)[0]
        return (cut or s[:n]).rstrip(" ,.;—-") + "…"

    @property
    def tooltip(self) -> str:
        head = "op-agent"
        # [mirror] its own caption for mirror states; for the others a line
        # about copies is appended to the previous text, if there is one.
        if self._state in (ST_SYNC, ST_MIRROR_FRESH, ST_OFFLINE, ST_KEY_DEAD,
                           ST_MIRROR_DISK):
            t = self._mirror_text()
            return self._clip(f"{head} — {t}") if t else head
        base = self._base_tooltip(head)
        t = self._mirror_text() if self._mirror_kind() not in (None, _mirror.M_IDLE) else ""
        return self._clip(f"{base} · {t}", 127) if t else base

    def _base_tooltip(self, head: str) -> str:
        if self._state == ST_BUSY:
            return f"{head} — export in progress"
        if self._state == ST_NO_PASSWORD:
            return f"{head} — GreenPlum password not set"
        if self._state == ST_ERROR:
            # The Windows tooltip is cut at 127 characters — we cut ourselves so that
            # the cut does not fall in the middle of a word.
            return f"{head} — error: {(self.error or '')[:80]}"
        return f"{head} — ready"

    def menu(self) -> list[tuple[int, str, bool]]:
        """[(command_id, label, enabled)] — top to bottom."""
        conn = f" ({self.connection})" if self.connection else ""
        items = [
            (CMD_PASSWORD, f"Enter GreenPlum password{conn}…", True),
            (CMD_TEST, "Test connection", self.has_password),
            (CMD_UNPAIR, "Unlink OnePage", self.paired),
            (CMD_FOLDERS, "Report folders…", True),
        ]
        if self._mirror_on():                          # [mirror]
            m = self.mirror
            idle = m.get("kind") != _mirror.M_SYNCING and bool(m.get("has_key"))
            items += [
                (CMD_SYNC, _mirror.text("action_sync_now"), idle),
                (CMD_LIBRARY, _mirror.text("action_open_library"), True),
                (CMD_STATE, _mirror.text("action_show_state"), True),
                (CMD_KEY, _mirror.text("action_replace_key" if m.get("has_key")
                                       else "action_add_key") + "…", True),
            ]
        items += [
            (CMD_LOG, "Show log", True),
            (CMD_QUIT, "Exit", True),
        ]
        return items


def available() -> bool:
    """Whether the icon can be raised in this environment."""
    return sys.platform == "win32"


# ── Win32 shell ──────────────────────────────────────────────────────────
def start(controller, pairing) -> "_Win32Tray | None":
    """Raise the icon. None — an environment without UI (dev/macOS/headless): then the agent
    works the old way, and pairing goes by a code in the console."""
    if not available():
        return None
    try:
        tray = _Win32Tray(controller, pairing)
        tray.create()
        _start_mirror(controller)                      # [mirror]
        return tray
    except Exception as e:                     # noqa: BLE001
        # The icon is a convenience, not the work. If Windows did not give one, the agent
        # must continue in console mode, not die.
        print(f"[tray] icon did not come up ({e!r}) — working without it", flush=True)
        return None


def _start_mirror(controller) -> None:
    """[mirror] Start the background sync. Separate from create(): a mirror failure
    must not cost the icon — it is an add-on to the agent, not its foundation."""
    try:
        starter = getattr(controller, "start_mirror", None)
        if starter:
            starter()
    except Exception as e:                     # noqa: BLE001
        print(f"[tray] mirror did not start ({e!r}) — the icon works without it", flush=True)


class _Win32Tray:
    """Window + icon + message loop. Drawing only, no logic."""

    def __init__(self, controller, pairing):
        self.c = controller
        self.pairing = pairing
        self.state = TrayState(getattr(controller, "connection_name", lambda: "")())
        self.hwnd = None
        self._icons: dict[str, int] = {}
        self._keepalive: list = []             # references to WNDPROC: otherwise GC -> crash
        self._quit = threading.Event()
        self._notify_queue: list = []          # notifications from the run thread
        self._notify_lock = threading.Lock()

    # ── public for the rest of the agent ───────────────────────────────
    def wake(self) -> None:
        """Wake the UI thread (called from HTTP threads). PostMessage only —
        no work in a foreign thread: Windows windows belong to their own."""
        if self.hwnd:
            _u32.PostMessageW(self.hwnd, _WM_WAKE, 0, 0)

    def notify(self, title: str, text: str, error: bool = False) -> None:
        """Pop-up notification at the icon. Called from the RUN THREAD.

        The icon belongs to the UI thread, so here we only put a message in the
        queue and wake that thread — drawing from a foreign thread is not allowed.
        """
        with self._notify_lock:
            self._notify_queue.append((title, text, error))
        if self.hwnd:
            _u32.PostMessageW(self.hwnd, _WM_NOTIFY, 0, 0)

    def _drain_notify(self) -> None:
        with self._notify_lock:
            queued, self._notify_queue = self._notify_queue, []
        for title, text, error in queued:
            try:
                self._nid_show(title=title, text=text, error=error)
            except Exception:                  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._quit.set()
        if self.hwnd:
            _u32.PostMessageW(self.hwnd, _WM_CLOSE, 0, 0)

    # ── creation ────────────────────────────────────────────────────────
    def create(self) -> None:
        cls_name = "OpAgentTrayWnd"
        wndproc = _WNDPROC(self._wndproc)
        self._keepalive.append(wndproc)

        wc = _WNDCLASSEXW()
        wc.cbSize = _ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = wndproc
        wc.hInstance = _k32.GetModuleHandleW(None)
        wc.lpszClassName = cls_name
        if not _u32.RegisterClassExW(_ctypes.byref(wc)):
            raise OSError(f"RegisterClassExW: {_k32.GetLastError()}")

        self.hwnd = _u32.CreateWindowExW(0, cls_name, "op-agent", 0, 0, 0, 0, 0,
                                         None, None, wc.hInstance, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowExW: {_k32.GetLastError()}")

        self._refresh(force=True)
        _u32.SetTimer(self.hwnd, 1, 500, None)   # poll status twice a second

    def run(self) -> None:
        """Message loop. Occupies the main thread until exit."""
        msg = _MSG()
        while not self._quit.is_set():
            r = _u32.GetMessageW(_ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break
            _u32.TranslateMessage(_ctypes.byref(msg))
            _u32.DispatchMessageW(_ctypes.byref(msg))
        self._nid_delete()

    # ── message handling ────────────────────────────────────────────────
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == _WM_TRAY:
                ev = lparam & 0xFFFF
                if ev in (_WM_RBUTTONUP, _WM_CONTEXTMENU):
                    self._show_menu()
                elif ev == _WM_LBUTTONUP:
                    # Left click = the most needed action in the current state.
                    self._on_command(CMD_PASSWORD if not self.state.has_password else CMD_TEST)
                return 0
            if msg == _WM_WAKE:
                self._drain_approvals()
                return 0
            if msg == _WM_NOTIFY:
                self._refresh(force=True)
                self._drain_notify()
                return 0
            if msg == _WM_TIMER:
                self._refresh()
                self._drain_approvals()
                self._drain_notify()
                return 0
            if msg == _WM_COMMAND:
                self._on_command(wparam & 0xFFFF)
                return 0
            if msg in (_WM_CLOSE, _WM_DESTROY):
                self._nid_delete()
                _u32.PostQuitMessage(0)
                return 0
        except Exception as e:                 # noqa: BLE001
            # An exception released into Windows through a ctypes callback kills
            # the process without a trace. We catch here and write to the log.
            print(f"[tray] failure in the handler of message {msg}: {e!r}", flush=True)
            return 0
        return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_command(self, cmd: int) -> None:
        if cmd == CMD_PASSWORD:
            self._ask_password()
        elif cmd == CMD_TEST:
            self._info("Connection test", self.c.test_connection())
        elif cmd == CMD_UNPAIR:
            self.c.unpair()
            self._refresh(force=True)
            self._info("OnePage unlinked",
                       "A confirmation will be required the next time a report runs.")
        elif cmd == CMD_LOG:
            self.c.open_log()
        elif cmd == CMD_SYNC:                  # [mirror]
            self.c.sync_now()
            self._refresh(force=True)
        elif cmd == CMD_LIBRARY:
            self.c.open_library()
        elif cmd == CMD_FOLDERS:
            self.c.open_folders()
        elif cmd == CMD_STATE:
            self._info(_mirror.text("action_show_state"), self.c.mirror_state_text())
        elif cmd == CMD_KEY:
            self._ask_key()
        elif cmd == CMD_QUIT:
            self.stop()
            self.c.quit()

    # ── pairing requests ────────────────────────────────────────────────
    def _drain_approvals(self) -> None:
        for rid, origin in self.pairing.take_pending():
            who = origin or "unknown source"
            text = (f"{who}\n\nAllow this site to run reports "
                    f"on your computer under your GreenPlum login?\n\n"
                    f"Answer \"Yes\" only if you have just clicked "
                    f"\"Run\" in OnePage yourself.")
            ok = _u32.MessageBoxW(None, text, "op-agent — confirmation",
                                  _MB_YESNO | _MB_ICONQUESTION | _MB_TOPMOST) == _IDYES
            self.pairing.resolve(rid, ok)
            self._refresh(force=True)

    # ── password entry ──────────────────────────────────────────────────
    def _ask_password(self) -> None:
        conn = self.state.connection or ""
        try:
            pw = _prompt_password(self.hwnd, conn, self._keepalive)
        except Exception as e:                 # noqa: BLE001
            # The own window failed to build — not a dead end: the console path remains.
            print(f"[tray] the password input window did not open: {e!r}", flush=True)
            self._info("Could not open the window",
                       "Enter the password with the command:\n\n"
                       "    op-agent.bat set-cred " + (conn or "analytics_dw"),
                       error=True)
            return
        if pw is None:
            return                              # cancelled
        if not pw:
            self._info("Empty password", "The password was not saved.", error=True)
            return
        msg = self.c.set_password(pw)
        del pw                                  # do not hold it in the frame longer than needed
        self._refresh(force=True)
        self._info("GreenPlum password", msg)

    # ── device key [mirror] ─────────────────────────────────────────────
    def _ask_key(self) -> None:
        """Key input window. The same window as for the password: the key, like the password, must
        not leave the agent process, neither through a socket nor through a foreign pipe."""
        replace = bool((self.state.mirror or {}).get("has_key"))
        title = _mirror.text("action_replace_key" if replace else "action_add_key")
        label = "\n\n".join((
            _mirror.text("key_hint_where"),
            _mirror.text("key_hint_once"),
            _mirror.text("key_hint_scope", p1=_mirror.DEFAULT_TTL_DAYS),
        ))
        try:
            key = _prompt_password(self.hwnd, "", self._keepalive,
                                   title=title, label=label, extra_h=130)
        except Exception as e:                 # noqa: BLE001
            print(f"[tray] the key input window did not open: {e!r}", flush=True)
            return
        if key is None:
            return
        msg = self.c.set_device_key(key)
        del key
        self._refresh(force=True)
        self._info(title, msg)

    # ── icon ────────────────────────────────────────────────────────────
    def _refresh(self, force: bool = False) -> None:
        changed = self.state.apply(self.c.status())
        if changed or force:
            self._nid_show()

    def _icon(self) -> int:
        key = self.state.state
        if key not in self._icons:
            self._icons[key] = _make_icon(self.state.color)
        return self._icons[key]

    def _nid_show(self, title: str = "", text: str = "", error: bool = False) -> None:
        nid = _NOTIFYICONDATAW()
        nid.cbSize = _ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = _NIF_ICON | _NIF_MESSAGE | _NIF_TIP
        nid.uCallbackMessage = _WM_TRAY
        nid.hIcon = self._icon()
        nid.szTip = self.state.tooltip[:127]
        if text:
            nid.uFlags |= _NIF_INFO
            nid.szInfo = text[:255]
            nid.szInfoTitle = (title or "op-agent")[:63]
            nid.dwInfoFlags = _NIIF_ERROR if error else _NIIF_INFO
        # NIM_MODIFY on a nonexistent icon returns 0 — then we add.
        if not _shell.Shell_NotifyIconW(_NIM_MODIFY, _ctypes.byref(nid)):
            _shell.Shell_NotifyIconW(_NIM_ADD, _ctypes.byref(nid))

    def _nid_delete(self) -> None:
        try:
            nid = _NOTIFYICONDATAW()
            nid.cbSize = _ctypes.sizeof(_NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            _shell.Shell_NotifyIconW(_NIM_DELETE, _ctypes.byref(nid))
        except Exception:
            pass

    def _show_menu(self) -> None:
        hmenu = _u32.CreatePopupMenu()
        for cmd, label, enabled in self.state.menu():
            flags = _MF_STRING | (0 if enabled else _MF_GRAYED)
            _u32.AppendMenuW(hmenu, flags, cmd, label)
        pt = _POINT()
        _u32.GetCursorPos(_ctypes.byref(pt))
        # Without SetForegroundWindow the menu does not close on a click outside it —
        # a known quirk of context menus at a tray icon.
        _u32.SetForegroundWindow(self.hwnd)
        _u32.TrackPopupMenu(hmenu, _TPM_RIGHTALIGN | _TPM_BOTTOMALIGN,
                            pt.x, pt.y, 0, self.hwnd, None)
        _u32.PostMessageW(self.hwnd, 0, 0, 0)
        _u32.DestroyMenu(hmenu)

    def _info(self, title: str, text: str, error: bool = False) -> None:
        _u32.MessageBoxW(None, text, f"op-agent — {title}",
                         (_MB_ICONERROR if error else _MB_ICONINFORMATION) | _MB_TOPMOST)


# ── ctypes: declarations are loaded on Windows only ─────────────────────────
if sys.platform == "win32":
    import ctypes as _ctypes
    from ctypes import wintypes as _wt

    _u32 = _ctypes.WinDLL("user32", use_last_error=True)
    _k32 = _ctypes.WinDLL("kernel32", use_last_error=True)
    _shell = _ctypes.WinDLL("shell32", use_last_error=True)
    _gdi = _ctypes.WinDLL("gdi32", use_last_error=True)

    _WNDPROC = _ctypes.WINFUNCTYPE(_ctypes.c_longlong, _wt.HWND, _wt.UINT,
                                   _ctypes.c_ulonglong, _ctypes.c_longlong)

    class _WNDCLASSEXW(_ctypes.Structure):
        _fields_ = [("cbSize", _wt.UINT), ("style", _wt.UINT),
                    ("lpfnWndProc", _WNDPROC), ("cbClsExtra", _ctypes.c_int),
                    ("cbWndExtra", _ctypes.c_int), ("hInstance", _wt.HINSTANCE),
                    ("hIcon", _wt.HICON), ("hCursor", _wt.HANDLE),
                    ("hbrBackground", _wt.HANDLE), ("lpszMenuName", _wt.LPCWSTR),
                    ("lpszClassName", _wt.LPCWSTR), ("hIconSm", _wt.HICON)]

    class _NOTIFYICONDATAW(_ctypes.Structure):
        _fields_ = [("cbSize", _wt.DWORD), ("hWnd", _wt.HWND), ("uID", _wt.UINT),
                    ("uFlags", _wt.UINT), ("uCallbackMessage", _wt.UINT),
                    ("hIcon", _wt.HICON), ("szTip", _wt.WCHAR * 128),
                    ("dwState", _wt.DWORD), ("dwStateMask", _wt.DWORD),
                    ("szInfo", _wt.WCHAR * 256), ("uVersion", _wt.UINT),
                    ("szInfoTitle", _wt.WCHAR * 64), ("dwInfoFlags", _wt.DWORD),
                    ("guidItem", _ctypes.c_byte * 16), ("hBalloonIcon", _wt.HICON)]

    class _POINT(_ctypes.Structure):
        _fields_ = [("x", _ctypes.c_long), ("y", _ctypes.c_long)]

    class _MSG(_ctypes.Structure):
        _fields_ = [("hwnd", _wt.HWND), ("message", _wt.UINT),
                    ("wParam", _ctypes.c_ulonglong), ("lParam", _ctypes.c_longlong),
                    ("time", _wt.DWORD), ("pt", _POINT)]

    # Without argtypes/restype 64-bit handles are truncated to 32 bits — the most common
    # way to crash ctypes code on Windows. We declare everything we call.
    _u32.RegisterClassExW.argtypes = [_ctypes.c_void_p]
    _u32.RegisterClassExW.restype = _wt.ATOM
    _u32.CreateWindowExW.argtypes = [_wt.DWORD, _wt.LPCWSTR, _wt.LPCWSTR, _wt.DWORD,
                                     _ctypes.c_int, _ctypes.c_int, _ctypes.c_int,
                                     _ctypes.c_int, _wt.HWND, _wt.HMENU,
                                     _wt.HINSTANCE, _ctypes.c_void_p]
    _u32.CreateWindowExW.restype = _wt.HWND
    _u32.DefWindowProcW.argtypes = [_wt.HWND, _wt.UINT, _ctypes.c_ulonglong,
                                    _ctypes.c_longlong]
    _u32.DefWindowProcW.restype = _ctypes.c_longlong
    _u32.PostMessageW.argtypes = [_wt.HWND, _wt.UINT, _ctypes.c_ulonglong,
                                  _ctypes.c_longlong]
    _u32.GetMessageW.argtypes = [_ctypes.c_void_p, _wt.HWND, _wt.UINT, _wt.UINT]
    _u32.GetMessageW.restype = _ctypes.c_int
    _u32.MessageBoxW.argtypes = [_wt.HWND, _wt.LPCWSTR, _wt.LPCWSTR, _wt.UINT]
    _u32.MessageBoxW.restype = _ctypes.c_int
    _u32.CreatePopupMenu.restype = _wt.HMENU
    _u32.AppendMenuW.argtypes = [_wt.HMENU, _wt.UINT, _ctypes.c_ulonglong, _wt.LPCWSTR]
    _u32.TrackPopupMenu.argtypes = [_wt.HMENU, _wt.UINT, _ctypes.c_int, _ctypes.c_int,
                                    _ctypes.c_int, _wt.HWND, _ctypes.c_void_p]
    _u32.SetTimer.argtypes = [_wt.HWND, _ctypes.c_ulonglong, _wt.UINT, _ctypes.c_void_p]
    _u32.SetForegroundWindow.argtypes = [_wt.HWND]
    _u32.GetCursorPos.argtypes = [_ctypes.c_void_p]
    _u32.DestroyMenu.argtypes = [_wt.HMENU]
    _u32.CreateIcon.argtypes = [_wt.HINSTANCE, _ctypes.c_int, _ctypes.c_int,
                                _wt.BYTE, _wt.BYTE, _ctypes.c_char_p, _ctypes.c_char_p]
    _u32.CreateIcon.restype = _wt.HICON
    _shell.Shell_NotifyIconW.argtypes = [_wt.DWORD, _ctypes.c_void_p]
    _shell.Shell_NotifyIconW.restype = _wt.BOOL
    _k32.GetModuleHandleW.argtypes = [_wt.LPCWSTR]
    _k32.GetModuleHandleW.restype = _wt.HMODULE

    _WM_DESTROY, _WM_CLOSE, _WM_COMMAND, _WM_TIMER = 0x0002, 0x0010, 0x0111, 0x0113
    _WM_LBUTTONUP, _WM_RBUTTONUP, _WM_CONTEXTMENU = 0x0202, 0x0205, 0x007B
    _WM_APP = 0x8000
    _WM_TRAY, _WM_WAKE, _WM_NOTIFY = _WM_APP + 1, _WM_APP + 2, _WM_APP + 3
    _NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0, 1, 2
    _NIF_MESSAGE, _NIF_ICON, _NIF_TIP, _NIF_INFO = 0x01, 0x02, 0x04, 0x10
    _NIIF_INFO, _NIIF_ERROR = 0x01, 0x03
    _MF_STRING, _MF_GRAYED = 0x0000, 0x0001
    _TPM_RIGHTALIGN, _TPM_BOTTOMALIGN = 0x0008, 0x0020
    _MB_YESNO, _MB_ICONQUESTION, _MB_ICONINFORMATION = 0x04, 0x20, 0x40
    _MB_ICONERROR, _MB_TOPMOST = 0x10, 0x40000
    _IDYES = 6


def _make_icon(bgr: tuple[int, int, int]):
    """16x16 circle of the given colour, assembled in memory.

    Transparency is set by the AND mask (1 = pixel is transparent), the colour by an XOR bitmap
    32 bits per pixel. So neither .ico files nor Pillow are needed, and the agent stays
    a single .py file.
    """
    n, r = 16, 7.2
    cx = cy = 7.5
    and_rows, xor = bytearray(), bytearray()
    b, g, rr = bgr
    for y in range(n):
        bits = 0
        for x in range(n):
            inside = (x - cx) ** 2 + (y - cy) ** 2 <= r * r
            if not inside:
                bits |= 1 << (15 - x)          # outside the circle — transparent
            xor += bytes((b, g, rr, 0)) if inside else b"\x00\x00\x00\x00"
        and_rows += bytes(((bits >> 8) & 0xFF, bits & 0xFF))
    return _u32.CreateIcon(_k32.GetModuleHandleW(None), n, n, 1, 32,
                           bytes(and_rows), bytes(xor))


# The input window state is MODULE-level, not in a closure.
# Reason: a window class is registered in Windows once, and on repeated
# registration the FIRST handler stays. With a closure the second call would write the
# result into the first one's variables and return "cancel" — a defect that
# would show up only on the second password entry. The dialog is modal and lives in
# the UI thread, so one state is enough.
_PWD = {"value": None, "done": False}
_PWD_CLASS = None
_PWD_KEEP: list = []

_ID_EDIT, _ID_OK, _ID_CANCEL = 2001, 1, 2


def _pwd_proc(hwnd, msg, wparam, lparam):
    if msg == _WM_COMMAND:
        cmd = wparam & 0xFFFF
        if cmd in (_ID_OK, _ID_CANCEL):
            if cmd == _ID_OK:
                h = _u32.GetDlgItem(hwnd, _ID_EDIT)
                buf = _ctypes.create_unicode_buffer(512)
                _u32.GetWindowTextW(h, buf, 512)
                _PWD["value"] = buf.value
                _u32.SetWindowTextW(h, "")     # do not leave the password in the control
            _PWD["done"] = True
            _u32.DestroyWindow(hwnd)
            return 0
    if msg == _WM_CLOSE:
        _PWD["done"] = True
        _u32.DestroyWindow(hwnd)
        return 0
    if msg == _WM_DESTROY:
        _PWD["done"] = True
        return 0
    return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _register_pwd_class():
    global _PWD_CLASS
    if _PWD_CLASS is not None:
        return _PWD_CLASS
    proc = _WNDPROC(_pwd_proc)
    _PWD_KEEP.append(proc)                     # otherwise GC removes the callback -> crash
    wc = _WNDCLASSEXW()
    wc.cbSize = _ctypes.sizeof(_WNDCLASSEXW)
    wc.lpfnWndProc = proc
    wc.hInstance = _k32.GetModuleHandleW(None)
    wc.lpszClassName = "OpAgentPwdWnd"
    wc.hbrBackground = _wt.HANDLE(_COLOR_WINDOW + 1)
    # Without a cursor the window keeps whichever was there — looks hung.
    wc.hCursor = _u32.LoadCursorW(None, _ctypes.cast(_IDC_ARROW, _wt.LPCWSTR))
    if not _u32.RegisterClassExW(_ctypes.byref(wc)):
        raise OSError(f"RegisterClassExW(pwd): {_k32.GetLastError()}")
    _PWD_CLASS = wc.lpszClassName
    return _PWD_CLASS


def _prompt_password(owner, connection: str, keepalive: list, *, title: str | None = None,
                     label: str | None = None, extra_h: int = 0) -> str | None:
    """The own password input window. None — cancelled.

    Our own, not a browser page and not PowerShell: the password does not leave the agent
    process — it goes neither through a socket nor through a foreign process's pipe. It is the same
    invariant for which it lives in DPAPI in the first place.
    """
    cls = _register_pwd_class()
    _PWD["value"], _PWD["done"] = None, False

    hinst = _k32.GetModuleHandleW(None)
    w, h = 420, 200 + extra_h        # [mirror] extra_h — room for a long hint
    hwnd = _u32.CreateWindowExW(
        _WS_EX_TOPMOST | _WS_EX_CONTROLPARENT, cls, title or "op-agent — GreenPlum password",
        _WS_CAPTION | _WS_SYSMENU | _WS_VISIBLE, _CW_USEDEFAULT, _CW_USEDEFAULT,
        w, h, owner, None, hinst, None)
    if not hwnd:
        raise OSError(f"CreateWindowExW(pwd): {_k32.GetLastError()}")

    label = label or (f"GreenPlum password for connection “{connection}”.\n"
                      "It is encrypted with Windows facilities and is not written to files.")
    kids = [
        _child("STATIC", label, _WS_CHILD | _WS_VISIBLE, 16, 14, w - 46, 44 + extra_h, hwnd, 0, hinst),
        _child("EDIT", "", _WS_CHILD | _WS_VISIBLE | _WS_TABSTOP | _WS_BORDER | _ES_PASSWORD,
               16, 64 + extra_h, w - 46, 26, hwnd, _ID_EDIT, hinst),
        _child("BUTTON", "Save",
               _WS_CHILD | _WS_VISIBLE | _WS_TABSTOP | _BS_DEFPUSHBUTTON,
               w - 236, 110 + extra_h, 100, 30, hwnd, _ID_OK, hinst),
        _child("BUTTON", "Cancel", _WS_CHILD | _WS_VISIBLE | _WS_TABSTOP,
               w - 126, 110 + extra_h, 100, 30, hwnd, _ID_CANCEL, hinst),
    ]
    # Without an explicit font the controls are drawn with the system bitmap one — the window looks
    # like it was assembled wrong.
    font = _gdi.GetStockObject(_DEFAULT_GUI_FONT)
    for kid in kids:
        _u32.SendMessageW(kid, _WM_SETFONT, font, 1)

    _u32.SetForegroundWindow(hwnd)
    _u32.SetFocus(_u32.GetDlgItem(hwnd, _ID_EDIT))

    # Nested message loop: IsDialogMessageW gives Tab and Enter for free.
    msg = _MSG()
    while not _PWD["done"]:
        r = _u32.GetMessageW(_ctypes.byref(msg), None, 0, 0)
        if r in (0, -1):
            break
        if not _u32.IsDialogMessageW(hwnd, _ctypes.byref(msg)):
            _u32.TranslateMessage(_ctypes.byref(msg))
            _u32.DispatchMessageW(_ctypes.byref(msg))
    value = _PWD["value"]
    _PWD["value"] = None                       # do not keep the password in the module
    return value


if sys.platform == "win32":
    _WS_CHILD, _WS_VISIBLE, _WS_BORDER = 0x40000000, 0x10000000, 0x00800000
    _WS_TABSTOP, _WS_CAPTION, _WS_SYSMENU = 0x00010000, 0x00C00000, 0x00080000
    _WS_EX_TOPMOST, _WS_EX_CONTROLPARENT = 0x00000008, 0x00010000
    _ES_PASSWORD, _BS_DEFPUSHBUTTON = 0x0020, 0x0001
    _COLOR_WINDOW, _CW_USEDEFAULT = 5, -2147483648
    _WM_SETFONT, _DEFAULT_GUI_FONT, _IDC_ARROW = 0x0030, 17, 32512

    _u32.GetDlgItem.argtypes = [_wt.HWND, _ctypes.c_int]
    _u32.GetDlgItem.restype = _wt.HWND
    _u32.GetWindowTextW.argtypes = [_wt.HWND, _wt.LPWSTR, _ctypes.c_int]
    _u32.SetWindowTextW.argtypes = [_wt.HWND, _wt.LPCWSTR]
    _u32.DestroyWindow.argtypes = [_wt.HWND]
    _u32.SetFocus.argtypes = [_wt.HWND]
    _u32.IsDialogMessageW.argtypes = [_wt.HWND, _ctypes.c_void_p]
    _u32.LoadCursorW.argtypes = [_wt.HINSTANCE, _wt.LPCWSTR]
    _u32.LoadCursorW.restype = _wt.HANDLE
    _u32.SendMessageW.argtypes = [_wt.HWND, _wt.UINT, _ctypes.c_ulonglong,
                                  _ctypes.c_longlong]
    _gdi.GetStockObject.argtypes = [_ctypes.c_int]
    _gdi.GetStockObject.restype = _wt.HANDLE

    def _child(cls, text, style, x, y, w, h, parent, cid, hinst):
        # HMENU(cid) — for a child window it is its identifier, not a menu.
        return _u32.CreateWindowExW(0, cls, text, style, x, y, w, h, parent,
                                    _wt.HMENU(cid), hinst, None)
