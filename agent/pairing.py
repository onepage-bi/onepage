"""Page<->agent pairing: human confirmation -> a long-lived Bearer token.

The top-priority asset to protect is the GP credential: it must not sneak through the
browser. Pairing adds a human-in-the-loop to the bootstrap.

There are two confirmation channels, and both prove presence at the machine:
  * `approve` — the agent shows a modal question at the tray icon, the person
    clicks "Allow". The main path on Windows;
  * `code`   — the agent prints a 6-digit code to its console, the person enters it
    on the page. The path without UI: dev, macOS, headless tests.

The token SURVIVES a restart (a DPAPI blob next to the credentials), otherwise the agent's
autostart would mean a new confirmation every morning. The trade-off is deliberate: the former
property "revoke = restart the agent" disappears, so revocation became explicit —
`revoke_all()` (the "Unlink OnePage" menu item), and it also erases the file.

The trust threshold here is a foreign BROWSER, not a foreign process on this same machine:
local malware running as the same user would decrypt the password's
DPAPI blob directly anyway, so hardening pairing against it is pointless.

Standard library only.
"""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

# ONLY a top-level import: the single-file build strips relative
# imports only at the top level, while an import inside a function would survive and break the
# built agent (deploy/build_agent_single.py now checks this too).
from .creds import _protect, _unprotect

# Defaults; tests override them via the constructor.
_CODE_TTL_S = 300.0        # the code/request is valid for 5 minutes
_CODE_ATTEMPTS = 5         # this many wrong entries kill the code

TOKEN_FILE = "pairing.bin"
_TOKEN_ENTROPY = b"op-agent/pairing/v1"


def _now() -> float:
    return time.monotonic()


def _eq(a: str, b: str) -> bool:
    """Constant-time comparison, robust against any input.

    `secrets.compare_digest` on STRINGS with non-ASCII raises TypeError — i.e. a
    token or code with Cyrillic would crash the handler instead of a clean refusal.
    We compare bytes: they have no such restriction, and constant time is preserved.
    """
    return secrets.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


class Pairing:
    """Pairing state. Thread-safe (the daemon is multi-threaded)."""

    def __init__(self, code_ttl_s: float = _CODE_TTL_S, attempts: int = _CODE_ATTEMPTS,
                 code_factory=None, token_path: Path | None = None,
                 protect=None, unprotect=None):
        self._lock = threading.Lock()
        self._tokens: set[str] = set()
        self._code: str | None = None
        self._code_expires: float = 0.0
        self._code_left: int = 0
        self._ttl = code_ttl_s
        self._attempts = attempts
        # Injection of the code generator for determinism in tests (otherwise — secrets).
        self._code_factory = code_factory or (lambda: f"{secrets.randbelow(1_000_000):06d}")
        self.agent_id = secrets.token_hex(16)

        # Confirmation requests: id -> {state, origin, expires, token}
        self._requests: dict[str, dict] = {}

        # Token storage. protect/unprotect are injected (tests run
        # the dev backend of creds.py on macOS); None — the token lives in memory only.
        self._token_path = Path(token_path) if token_path else None
        if self._token_path is not None:
            protect = protect or _protect
            unprotect = unprotect or _unprotect
        self._protect, self._unprotect = protect, unprotect
        self._load_tokens()

    # ── token persistence ───────────────────────────────────────────────
    def _load_tokens(self) -> None:
        if self._token_path is None or not self._token_path.is_file():
            return
        try:
            raw = self._unprotect(self._token_path.read_bytes(), _TOKEN_ENTROPY)
            self._tokens = {t for t in raw.decode("utf-8").split("\n") if t}
        except Exception:
            # A broken/foreign blob (the folder was moved, the user changed) is not
            # an emergency: there is simply nothing to confirm with, pairing will be asked again.
            self._tokens = set()

    def _save_tokens_locked(self) -> None:
        if self._token_path is None:
            return
        try:
            if not self._tokens:
                self._token_path.unlink(missing_ok=True)
                return
            blob = self._protect("\n".join(sorted(self._tokens)).encode("utf-8"),
                                 _TOKEN_ENTROPY)
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._token_path.with_suffix(self._token_path.suffix + ".tmp")
            tmp.write_bytes(blob)
            tmp.replace(self._token_path)       # atomic swap
        except Exception:
            pass    # not saved — we will survive, we will ask for confirmation again

    def _mint_locked(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens.add(token)
        self._save_tokens_locked()
        return token

    # ── Pairing code (channel without UI) ───────────────────────────────
    def request_code(self) -> tuple[str, float]:
        """Generate a new code (kills the previous one). Returns (code, ttl_s).

        Printing the code to the console is the caller's concern (server), not this module's:
        the module knows nothing about stdout and is tested without it.
        """
        with self._lock:
            self._code = self._code_factory()
            self._code_expires = _now() + self._ttl
            self._code_left = self._attempts
            return self._code, self._ttl

    def redeem(self, code: str) -> str | None:
        """Exchange a code for a token. None — the code is wrong/expired/exhausted."""
        with self._lock:
            if self._code is None or _now() > self._code_expires:
                self._code = None
                return None
            ok = _eq(code, self._code)
            if not ok:
                self._code_left -= 1
                if self._code_left <= 0:
                    self._code = None        # attempts exhausted — the code is dead
                return None
            self._code = None                # the code is single-use
            return self._mint_locked()

    # ── Confirmation request (channel with UI) ──────────────────────────
    def request_approval(self, origin: str | None) -> tuple[str, float]:
        """Create a request. Returns (request_id, ttl_s).

        Does NOT block: it is called from an HTTP thread, while the dialog is shown by the UI thread.
        Otherwise one modal question would freeze the whole agent.
        """
        rid = secrets.token_urlsafe(12)
        with self._lock:
            self._expire_locked()
            self._requests[rid] = {"state": "pending", "origin": origin or "",
                                   "expires": _now() + self._ttl, "token": None}
        return rid, self._ttl

    def take_pending(self) -> list[tuple[str, str]]:
        """Requests not yet shown to the person: [(request_id, origin)].

        Marks them as shown, so the UI loop will not open two windows for one
        request, however many times it polls.
        """
        out = []
        with self._lock:
            self._expire_locked()
            for rid, req in self._requests.items():
                if req["state"] == "pending":
                    req["state"] = "asking"
                    out.append((rid, req["origin"]))
        return out

    def resolve(self, request_id: str, approved: bool) -> bool:
        """The person's answer. False — there is no such request or it has already expired."""
        with self._lock:
            self._expire_locked()
            req = self._requests.get(request_id)
            if req is None or req["state"] not in ("pending", "asking"):
                return False
            if approved:
                req["token"] = self._mint_locked()
                req["state"] = "approved"
            else:
                req["state"] = "denied"
            return True

    def approval_status(self, request_id: str) -> dict:
        """Request state for the page's polling. The token is handed out once."""
        with self._lock:
            self._expire_locked()
            req = self._requests.get(request_id)
            if req is None:
                return {"state": "expired"}
            state = req["state"]
            if state == "approved" and req["token"]:
                token = req["token"]
                req["token"] = None      # taken — we do not hand it out a second time
                return {"state": "approved", "token": token}
            # "asking" is indistinguishable from "pending" outside — it is a UI detail.
            return {"state": "pending" if state in ("pending", "asking") else state}

    def _expire_locked(self) -> None:
        now = _now()
        for rid in [r for r, q in self._requests.items() if q["expires"] < now]:
            self._requests.pop(rid, None)

    # ── Tokens ──────────────────────────────────────────────────────────
    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            # A linear constant-time check of each token (there are only a few).
            return any(_eq(token, t) for t in self._tokens)

    def revoke_all(self) -> None:
        """Unlink everything: in-memory tokens, the file on disk and pending requests."""
        with self._lock:
            self._tokens.clear()
            self._requests.clear()
            self._save_tokens_locked()

    @property
    def paired(self) -> bool:
        with self._lock:
            return bool(self._tokens)
