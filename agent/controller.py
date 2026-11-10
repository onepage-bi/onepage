"""Icon controller — all the policy the icon itself must not know.

`agent/tray.py` knows how to draw and knows nothing about GreenPlum or DPAPI.
Here it is the other way round: zero Windows calls, only "what it means to save
a password" and "what it means to test the connection". Thanks to this split the
controller is fully testable on macOS, and only the drawing stays untestable.

The same object serves both the single-file build and `python -m agent`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from . import appwindow, applog
# A top-level import is mandatory: a relative import INSIDE a function
# would survive the merge into the single-file build and break it. psycopg is not
# pulled in here — gpsource loads the driver lazily.
from .config import state_dir
from .gpsource import _classify, probe
# [mirror] the background sync schedule; texts and logic live in it.
from .mirror import schedule as mirror_schedule


class AgentController:
    """Answers to the person's actions at the icon."""

    def __init__(self, server, pairing, cred_store, config, connection: str):
        self.server = server
        self.pairing = pairing
        self.cred_store = cred_store
        self.config = config
        self.connection = connection
        self._error: str | None = None
        self._lock = threading.Lock()
        # [mirror] The platform address is taken at the moment of the attempt (lambda), not
        # at creation: it can be corrected via an environment variable without recreating.
        self.mirror = mirror_schedule.MirrorWorker(
            cred_store, lambda: mirror_schedule.default_server_url(self.config))

    # ── what the icon shows ─────────────────────────────────────────────
    def connection_name(self) -> str:
        return self.connection

    def status(self) -> dict:
        return {
            "has_password": self.cred_store.has_password(self.connection),
            "busy": self.server.busy.is_set(),
            "paired": self.pairing.paired,
            "error": self.last_error(),
            "mirror": self.mirror.snapshot(),             # [mirror]
        }

    def last_error(self) -> str | None:
        with self._lock:
            return self._error

    def set_error(self, msg: str | None) -> None:
        """Called by a run: a failure turns the icon red, a success clears the red."""
        with self._lock:
            self._error = msg

    # ── menu actions ────────────────────────────────────────────────────
    def set_password(self, password: str) -> str:
        """Save the password and test it with a connection RIGHT AWAY.

        The test is mandatory: a silently saved wrong password would surface
        only on a run, as a refusal without explanation. We already lost
        an evening to exactly this on 22.07.2026 — the cause was a stale password
        in DPAPI, while the diagnostics showed only "connection failure".
        """
        try:
            self.cred_store.set_password(self.connection, password)
        except Exception as e:                                   # noqa: BLE001
            return f"Could not save: {e}"
        ok, msg = self.probe_connection()
        self.set_error(None if ok else msg)
        if ok:
            return "Password saved, connection verified:\n\n" + msg
        return ("Password saved, but the connection failed:\n\n" + msg +
                "\n\nIf the password is wrong, enter it again.")

    def test_connection(self) -> str:
        ok, msg = self.probe_connection()
        self.set_error(None if ok else msg)
        return msg

    def unpair(self) -> None:
        self.pairing.revoke_all()

    def open_log(self) -> None:
        path = applog.log_path()
        if path is None or not Path(path).exists():
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(path))                          # noqa: S606
            else:
                subprocess.Popen(["open", str(path)])            # noqa: S603,S607
        except Exception as e:                                   # noqa: BLE001
            print(f"[tray] the log did not open: {e!r}", flush=True)

    # ── report mirror ───────────────────────────────────────────────────
    def start_mirror(self) -> bool:
        """Start the background sync. Called by the tray after its own start."""
        return self.mirror.start()

    def sync_now(self) -> bool:
        return self.mirror.sync_now()

    def open_library(self) -> None:
        """Open the copies library in its OWN window at the agent's loopback address.

        Which path was chosen (a window or the fallback tab) and why is written by
        appwindow.open_window itself: "opened as a tab" is diagnosed from the log.
        """
        try:
            port = self.server.server_address[1]
            # state_root is filled by `__main__` before the service starts, but if the field
            # is still empty (initialization order is guaranteed by no one), the window
            # would fall back to a tab over a trifle. We take the state directory
            # the same way the agent itself takes it.
            root = self.server.state_root or state_dir()
            appwindow.open_window(mirror_schedule.library_url(port),
                                  state_root=root, title="OnePage")
        except Exception as e:                                   # noqa: BLE001
            print(f"[tray] the library did not open: {e!r}", flush=True)

    def open_folders(self) -> None:
        """Open the "Report folders" screen in its OWN window at the agent's loopback address.

        Not under `_mirror_on()`: folders and the local run live without the mirror —
        a person can compute a report on their machine without ever turning on
        copy sync. Tying them together would hide the folder setup
        behind somebody else's feature.
        """
        try:
            port = self.server.server_address[1]
            root = self.server.state_root or state_dir()
            appwindow.open_folders(port, state_root=root)
        except Exception as e:                                   # noqa: BLE001
            print(f"[tray] the folders screen did not open: {e!r}", flush=True)

    def mirror_state_text(self) -> str:
        """Text of the "Status" item: the sync line and the time of the last
        successful one, if the current one is not it."""
        snap = self.mirror.snapshot()
        lines = [mirror_schedule.status_text(snap)]
        if snap.get("last_ok") and snap.get("kind") != mirror_schedule.M_FRESH:
            lines.append(mirror_schedule.text(
                "sync_done", p1=mirror_schedule.fmt_when(snap["last_ok"])))
        return "\n\n".join(l for l in lines if l) or mirror_schedule.text("sync_no_key")

    def set_device_key(self, key: str) -> str:
        """Save the device key and start an update. Returns text
        for the person. The key itself reaches neither the text nor the log."""
        key = (key or "").strip()
        # A key is one line of printable ASCII without spaces; anything else is a
        # copy error (line break, trailing quotes), and saving such a thing means
        # getting "not accepted" an hour later. We do not impose a format/prefix: it is not ours.
        if not key or len(key) > 512 or not key.isascii() or not key.isprintable() \
                or any(c.isspace() for c in key):
            return mirror_schedule.text("sync_key_rejected")
        try:
            self.cred_store.set_password(mirror_schedule.KEY_NAME, key)
        except Exception as e:                                   # noqa: BLE001
            return f"Could not save: {type(e).__name__}"
        finally:
            del key
        self.mirror.key_changed()
        return mirror_schedule.text("key_added")

    def quit(self) -> None:
        self.mirror.stop(timeout=0)       # [mirror] a signal; nothing to wait for here
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    # ── connection test ─────────────────────────────────────────────────
    def probe_connection(self) -> tuple[bool, str]:
        """(success, human-readable text). The password does not get into the text.

        The implementation lives in gpsource — where all the GP work is; here is
        only the translation of the result into the human's language.
        """
        topo = (self.config.connections or {}).get(self.connection)
        if not topo or not topo.get("host"):
            return False, (f"agent.ini has no [connection:{self.connection}] section "
                           f"with a server address.")
        pw = self.cred_store.get_password(self.connection)
        if pw is None:
            return False, "Password not set. Click \"Enter GreenPlum password\"."
        try:
            info = probe(topo, pw)
        except ModuleNotFoundError as e:                         # no psycopg
            return False, (f"The GreenPlum driver is not installed ({e.name}). "
                           f"Reinstall the agent folder: setup.bat.")
        except Exception as e:                                   # noqa: BLE001
            _, msg = _classify(e)
            return False, msg
        finally:
            del pw
        return True, ("Connection works.\n\n"
                      f"db login  : {info.get('user', '')}\n"
                      f"database  : {info.get('db', '')}\n"
                      f"server    : {info.get('version', '')[:120]}")
