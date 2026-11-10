"""Agent log — a replacement for the console that does not exist under `pythonw.exe`.

By removing the black window we must put the diagnostics somewhere: under
`pythonw.exe` `sys.stdout` and `sys.stderr` are None, and any `print()` in the
code would crash the process with AttributeError. Here both streams are replaced
by a file, and the "Show log" menu item opens it in Notepad.

Invariants:
  * Writing NEVER raises. A log failure must not be allowed to kill a run —
    it is diagnostics, not work.
  * The password does not get here: that is the caller's job (`gpsource._safe_msg`
    strips `password=` BEFORE the string appears anywhere). The log filters
    nothing and must not create the illusion that it does.
  * Size-based rotation, exactly one old copy: the agent lives for months in
    autostart, an unbounded file is a disk leak.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024      # 2 MB per file
LOG_NAME = "op-agent.log"


class _LogStream:
    """File stream with timestamps. Writes line by line and thread-safely:
    HTTP threads, the run thread and the UI thread print concurrently."""

    def __init__(self, path: Path, mirror=None):
        self.path = path
        self._lock = threading.Lock()
        self._mirror = mirror            # dev: duplicate to the real console
        self._at_line_start = True
        self._fh = None
        self._open()

    def _open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8", errors="replace")
        except OSError:
            self._fh = None              # disk unavailable — silently work "into the void"

    def _rotate_if_needed(self) -> None:
        if self._fh is None:
            return
        try:
            if self._fh.tell() < MAX_BYTES:
                return
            self._fh.close()
            self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
        except OSError:
            pass
        self._open()

    def write(self, text) -> int:
        if not text:
            return 0
        s = text if isinstance(text, str) else str(text)
        try:
            with self._lock:
                if self._mirror is not None:
                    try:
                        self._mirror.write(s)
                    except Exception:
                        pass
                if self._fh is None:
                    return len(s)
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                # The timestamp goes at the start of EVERY line, so
                # multi-line diagnostics stay readable.
                out = []
                for i, part in enumerate(s.split("\n")):
                    if i:
                        out.append("\n")
                        self._at_line_start = True
                    if part and self._at_line_start:
                        out.append(f"{stamp}  ")
                        self._at_line_start = False
                    out.append(part)
                if s.endswith("\n"):
                    self._at_line_start = True
                self._fh.write("".join(out))
                self._fh.flush()
                self._rotate_if_needed()
        except Exception:
            pass                          # the log must not be allowed to break work
        return len(s)

    def flush(self) -> None:
        try:
            with self._lock:
                if self._fh is not None:
                    self._fh.flush()
        except Exception:
            pass

    # http.server and parts of stdlib check these attributes.
    def isatty(self) -> bool:
        return False

    def fileno(self):
        raise OSError("log stream has no fileno")


_path: Path | None = None


def setup(state_dir: Path, mirror_console: bool = False) -> Path:
    """Redirect stdout/stderr to a file. Returns the path to it.

    mirror_console=True keeps output in the console too — development mode, where
    a window exists and it is convenient to watch it.
    """
    global _path
    path = Path(state_dir) / LOG_NAME
    # sys.__stdout__ is None under pythonw — nowhere to duplicate to, and that is normal.
    mirror = sys.__stdout__ if (mirror_console and sys.__stdout__ is not None) else None
    stream = _LogStream(path, mirror=mirror)
    sys.stdout = stream
    sys.stderr = stream
    _path = path
    return path


def log_path() -> Path | None:
    """Path to the log if it is already set up (for the "Show log" menu item)."""
    return _path
