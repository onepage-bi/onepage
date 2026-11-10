"""Daemon entry point: single-instance locking via the port ladder + startup.

A successful bind on 127.0.0.1:<port> IS the single-instance mutex
(allow_reuse_address=False). If the port is busy we check /health: if it is our
agent, we exit (already running); otherwise we try the next port of the ladder.

Two modes of operation, and the choice between them is not a setting but a property of the environment:
  * with an icon (Windows): the HTTP server goes to a background thread, the MAIN thread
    runs the Windows message loop. It cannot be otherwise: windows belong to the thread that created
    them, and a modal question from an HTTP thread would freeze the whole agent;
  * without an icon (dev/macOS/headless): as before — `serve_forever` in the main
    thread, pairing by a code in the console.

Run:      python -m agent            (dev/test — state in ~/.op-agent)
         python -m agent folders [id] [scenario]   what the agent will read
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request

from . import APP, VERSION, applog, tray
from .config import PORT_LADDER, load_config, state_dir
from .controller import AgentController
from .creds import CredStore
from .pairing import TOKEN_FILE, Pairing
from .server import AgentServer


def _set_cred(argv: list[str]) -> int:
    """`python -m agent set-cred [connection]` — write the GP password to DPAPI.
    A path for IT/development: the analyst has a tray menu item for this."""
    import getpass
    conn = argv[0] if argv else "analytics_dw"
    store = CredStore()
    if store.has_password(conn):
        print(f"The password for connection {conn!r} is already saved — overwrite? [y/N]", flush=True)
        if (input().strip().lower() or "n") not in ("y", "yes", "\u0434", "\u0434\u0430"):
            print("Cancelled.")
            return 0
    pw = getpass.getpass(f"GreenPlum password for connection {conn!r} (input hidden): ")
    if not pw:
        print("Empty password — cancelled.", file=sys.stderr)
        return 1
    store.set_password(conn, pw)
    print(f"OK: password for {conn!r} saved to DPAPI ({store.dir}).", flush=True)
    return 0


def _del_cred(argv: list[str]) -> int:
    conn = argv[0] if argv else "analytics_dw"
    CredStore().delete_password(conn)
    print(f"OK: password for {conn!r} deleted (if there was one).", flush=True)
    return 0


def _folders_cmd(argv: list[str]) -> int:
    """`python -m agent folders [dashboard_id] [scenario]`.

    The printing itself lives in `agent/server.py::folders_cli` — next to the
    `GET /folders` handler whose reply it repeats. The reason is not stylistic:
    the kit has a SECOND entry point (`deploy/build_agent_single.py`, see its
    comment about "they cannot be merged into one"), and two copies of this command
    would diverge on exactly the day the page could not reach the agent and
    the person needs the exact answer.
    """
    from .server import folders_cli
    return folders_cli(argv)


# Polling OUR OWN port bypasses the proxy, and this is not overcaution.
# `urlopen` by default reads the system proxy settings (on Windows — from
# the registry), and `<local>` in the exclusion list does not cover 127.0.0.1: by
# the CPython rule it matches only a name WITHOUT dots. On a corporate
# machine with a proxy set, a request to our own loopback would go to the proxy
# and return a refusal — and for us that is "no agent": a shortcut would wait 20 seconds for
# the health of a live agent, and a second instance would take the next port
# of the ladder instead of quietly exiting. Checking our own port is never
# a network request outward, so an empty ProxyHandler is mandatory here.
_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_our_agent(port: int) -> bool:
    """Is the busy port our agent? (then the second instance quietly exits.)"""
    try:
        with _LOOPBACK.open(f"http://127.0.0.1:{port}/health", timeout=1.0) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data.get("app") == APP
    except (urllib.error.URLError, OSError, ValueError):
        return False


def main() -> int:
    # Credential-store subcommands (do not start the daemon).
    if len(sys.argv) > 1 and sys.argv[1] == "set-cred":
        return _set_cred(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "del-cred":
        return _del_cred(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "folders":
        return _folders_cmd(sys.argv[2:])

    want_tray = "--no-tray" not in sys.argv and tray.available()
    config = load_config()
    root = state_dir()
    # With an icon there is no console (pythonw): redirect output to a file BEFORE the first
    # print, otherwise sys.stdout is None will crash the agent out of the blue.
    if want_tray:
        applog.setup(root)
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
            continue   # the port is taken by someone else — next
        server.state_root = root
        print("=" * 56, flush=True)
        print(f"  {APP} {VERSION} listening on http://127.0.0.1:{port}", flush=True)
        print(f"  allowlist origin: {', '.join(config.allowed_origins)}", flush=True)
        print(f"  state: {root}", flush=True)
        print("  Stop: Ctrl+C." if not want_tray else "  Stop: the \"Exit\" item at the icon.",
              flush=True)
        print("=" * 56, flush=True)
        return _serve(server, config, pairing, want_tray)

    print(f"Could not take any port of {PORT_LADDER}: {last_err}", file=sys.stderr)
    return 1


def _serve(server, config, pairing, want_tray: bool) -> int:
    """Start the controller and the mirror, then the icon, if it asks for one.

    The order is exactly this, and it is not style. Previously the controller was created ONLY
    in the icon branch, and the mirror was started by the tray after its own start. So
    every path without an icon — `--no-tray`, no support, a failed
    window creation — left a person with a working library in which
    copies are NEVER updated: `MirrorWorker` was simply never born. On the
    person's machine this would look like "the program started, the list is
    empty", without a single error. Now the mirror does not depend on the icon: the icon is
    a surface, not a condition for working.
    """
    # We take the connection name as the first one from agent.ini: the analyst has one, and
    # inventing a choice where there is none is an extra screen.
    conn = next(iter(config.connections), "analytics_dw")
    controller = AgentController(server, pairing, server.cred_store, config, conn)
    server.controller = controller
    # A repeated start is safe: `MirrorWorker.start()` on a live thread is a
    # no-op, and the tray calls it after its own rise.
    controller.start_mirror()

    ui = tray.start(controller, pairing) if want_tray else None
    if ui is None:
        # No icon (not requested, not supported or did not come up) — we serve from the
        # main thread. The mirror is already running.
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n  Stopped.", flush=True)
        finally:
            controller.mirror.stop(timeout=3.0)
            server.shutdown()
            server.server_close()
        return 0

    server.ui = ui
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    try:
        ui.run()                      # holds the main thread until "Exit"
    finally:
        controller.mirror.stop(timeout=3.0)
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
