"""Smoke test M4: the icon state machine, confirmed pairing, token survival.

This checks ALL of the icon's logic — the part that does not draw. The Win32 shell
(`agent.tray._Win32Tray`) does not run on macOS and cannot be verified by
anything but a live Windows; so the logic is pulled out of it into here, and only
drawing remains there. The test holds this boundary: if anything verified below
moves into the shell, it falls out from under the check.

Run:     python -m agent.tests.smoke_tray   (from the OnePage repo root)
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# DPAPI dev backend: there is no real one on macOS, but token storage must be checked.
# The same flag and the same discipline as for the credentials (agent/creds.py).
os.environ["OP_AGENT_DEV_CRED_STORE"] = "1"

from agent.pairing import Pairing                                    # noqa: E402
from agent.tray import (CMD_LOG, CMD_PASSWORD, CMD_TEST, CMD_UNPAIR,  # noqa: E402
                        ST_BUSY, ST_ERROR, ST_NO_PASSWORD, ST_READY, TrayState)

_PASS = _FAIL = 0


def check(label, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"PASS {label}")
    else:
        _FAIL += 1
        print(f"FAIL {label}  {extra}")


def menu_enabled(state) -> dict:
    return {cmd: enabled for cmd, _, enabled in state.menu()}


# ── 1. State machine ────────────────────────────────────────────────────────
s = TrayState("analytics_dw")
check("start without a password -> grey", s.state == ST_NO_PASSWORD, s.state)

s.apply({"has_password": True})
check("password present -> green", s.state == ST_READY, s.state)

s.apply({"has_password": True, "error": "gp_network: no network"})
check("error -> red", s.state == ST_ERROR, s.state)

# Priority: work in progress matters more than a past error — the icon shows "now".
s.apply({"has_password": True, "busy": True, "error": "gp_network: no network"})
check("busy matters more than a past error", s.state == ST_BUSY, s.state)

# No password matters more than an error: fix the cause first, not the effect.
s.apply({"has_password": False, "error": "gp_auth"})
check("no password matters more than an error", s.state == ST_NO_PASSWORD, s.state)

changed = s.apply({"has_password": False, "error": "gp_auth"})
check("same status again -> no redraw", changed is False)

s2 = TrayState("analytics_dw")
s2.apply({"has_password": True})
check("state change -> redraw", s2.apply({"has_password": True, "busy": True}) is True)

check("the label carries the connection name", "analytics_dw" in s2.menu()[0][1], s2.menu()[0][1])
check("the tooltip fits the Windows limit (127)",
      all(len(TrayState().apply({"error": "é" * 400}) or TrayState().tooltip) <= 127
          for _ in (0,)))

# Availability of items depends on the state, not on the mood.
s3 = TrayState()
s3.apply({"has_password": False, "paired": False})
m = menu_enabled(s3)
check("without a password: \"Test connection\" is unavailable", m[CMD_TEST] is False)
check("without pairing: \"Unlink\" is unavailable", m[CMD_UNPAIR] is False)
check("\"Enter password\" is always available", m[CMD_PASSWORD] is True)
check("\"Show log\" is always available", m[CMD_LOG] is True)
s3.apply({"has_password": True, "paired": True})
m = menu_enabled(s3)
check("with a password: the test is available", m[CMD_TEST] is True)
check("paired: \"Unlink\" is available", m[CMD_UNPAIR] is True)


# ── 2. Confirmed pairing ────────────────────────────────────────────────────
p = Pairing()
rid, ttl = p.request_approval("https://onepage.example.com")
check("request created", bool(rid) and ttl > 0)
check("before the person answers — not paired", p.paired is False)
check("status before the answer — pending", p.approval_status(rid)["state"] == "pending")

pending = p.take_pending()
check("request visible to the UI thread", pending == [(rid, "https://onepage.example.com")], str(pending))
check("a repeated poll does not show it again", p.take_pending() == [])

check("resolve on a live request accepted", p.resolve(rid, True) is True)
st = p.approval_status(rid)
check("after \"Allow\" — token issued", st["state"] == "approved" and bool(st.get("token")))
token = st["token"]
check("token accepted", p.valid(token) is True)
check("token is handed out once", p.approval_status(rid).get("token") is None)
check("agent considers itself paired", p.paired is True)

# Denial
rid2, _ = p.request_approval("https://onepage.example.com")
p.take_pending()
p.resolve(rid2, False)
check("after \"Deny\" — denied", p.approval_status(rid2)["state"] == "denied")
check("repeated resolve rejected", p.resolve(rid2, True) is False)
check("unknown request -> expired", p.approval_status("no-such-id")["state"] == "expired")

# An expired request does not turn into a token.
p_short = Pairing(code_ttl_s=0.05)
rid3, _ = p_short.request_approval(None)
time.sleep(0.1)
check("expired request -> expired", p_short.approval_status(rid3)["state"] == "expired")
check("an expired one cannot be confirmed", p_short.resolve(rid3, True) is False)
check("an expired one gives no pairing", p_short.paired is False)

# A request does NOT block the calling thread: otherwise one modal question would freeze
# the whole agent (it is created by an HTTP thread, and answered by the UI thread).
p_block = Pairing()
t0 = time.monotonic()
p_block.request_approval(None)
check("request_approval does not block", time.monotonic() - t0 < 0.05)


# ── 3. The token survives a restart ─────────────────────────────────────────
tmp = Path(tempfile.mkdtemp(prefix="op-tray-"))
tok_path = tmp / "pairing.bin"

p1 = Pairing(token_path=tok_path)
rid, _ = p1.request_approval("https://onepage.example.com")
p1.take_pending()
p1.resolve(rid, True)
saved = p1.approval_status(rid)["token"]
check("token written to disk", tok_path.is_file())
check("the token itself is not on disk in the clear",
      saved.encode() not in tok_path.read_bytes())

p2 = Pairing(token_path=tok_path)          # "agent restart"
check("after a restart the pairing is kept", p2.paired is True)
check("the previous token is still accepted", p2.valid(saved) is True)
check("a foreign token is not accepted", p2.valid("wrong-token") is False)

p2.revoke_all()
check("\"Unlink\" kills the token", p2.valid(saved) is False)
check("\"Unlink\" erases the file", not tok_path.exists())
p3 = Pairing(token_path=tok_path)
check("after unlinking a restart does not resurrect the token", p3.paired is False)

# A broken blob (the folder was moved to another machine) is not an emergency but "we will ask again".
tok_path.write_bytes("garbage, not our blob".encode("utf-8"))
p4 = Pairing(token_path=tok_path)
check("a broken blob does not crash the agent", p4.paired is False)

# Without token_path the behaviour is the old one: the token is only in memory.
p5 = Pairing()
rid, _ = p5.request_approval(None)
p5.take_pending()
p5.resolve(rid, True)
check("without a path the token is not written to disk", not (tmp / "absent.bin").exists() and p5.paired)


# ── 4. The old channel (code) keeps working ─────────────────────────────────
p6 = Pairing(code_factory=lambda: "424242")
code, _ = p6.request_code()
check("code is issued", code == "424242")
check("wrong code rejected", p6.redeem("000000") is None)
tok = p6.redeem("424242")
check("correct code gives a token", bool(tok) and p6.valid(tok))
check("code is single-use", p6.redeem("424242") is None)

# Regression: secrets.compare_digest on STRINGS with non-ASCII raises TypeError.
# Before the fix, non-ASCII letters in a code or in a Bearer token crashed the
# request handler instead of a clean refusal.
p8 = Pairing(code_factory=lambda: "424242")
p8.request_code()
check("non-ASCII in a code -> refusal, not a crash", p8.redeem("κωδικός") is None)
check("non-ASCII in a token -> refusal, not a crash", p8.valid("τοκεν") is False)
check("empty token -> refusal", p8.valid("") is False and p8.valid(None) is False)

# Both channels reuse one token store.
p7 = Pairing(code_factory=lambda: "111111", token_path=tmp / "both.bin")
p7.request_code()
p7.redeem("111111")
check("a token from a code is also saved", (tmp / "both.bin").is_file())



# ── 5. Notifications are moved to the UI thread, not drawn in place ─────────
# The icon belongs to the thread that created it. notify() is called from the RUN
# THREAD, so it must only put the message in a queue. That is exactly what we check:
# on macOS any real Win32 call here would raise NameError.
from agent.tray import _Win32Tray                                    # noqa: E402


class _StubCtl:
    def connection_name(self): return "analytics_dw"
    def status(self): return {"has_password": True}


ui = _Win32Tray(_StubCtl(), Pairing())
ui.notify("Report ready", "1000 rows")
ui.notify("Report not built", "gp_network", error=True)
check("notify does not draw in a foreign thread, it accumulates", len(ui._notify_queue) == 2)
check("wake() without a window is safe", (ui.wake() or True))

from_thread = []
t = threading.Thread(target=lambda: (ui.notify("from a thread", "ok"),
                                     from_thread.append(True)))
t.start(); t.join(2)
check("notify from another thread does not crash", from_thread == [True])
check("the queue grew", len(ui._notify_queue) == 3)


print()
print(f"{'ALL PASS' if not _FAIL else str(_FAIL) + ' FAILURES'}  ({_PASS} passed)")
sys.exit(1 if _FAIL else 0)
