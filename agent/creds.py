"""Storage of the analyst's GP password — Windows DPAPI via ctypes, NO dependencies.

Decision of gate G1 (docs/RFC_AGENT_GREENPLUM.md, probe g1_cred_probe.py passed on
a real machine): the password is stored as a DPAPI blob bound to user+machine. The
encryption key is derived from the Windows login and never leaves it — a copied/
backed-up folder carries a useless blob (unlike "the DBeaver way", where the
AES key sits next to it). The `keyring` package is NOT needed — only stdlib `ctypes`.

Invariants:
  * The plaintext password is NEVER written to disk or logged.
  * The blob lives in state_dir()/creds/<connection>.bin (inside %LOCALAPPDATA%,
    outside the copyable portable agent folder).
  * The binding entropy includes the connection name -> a blob of one connection
    cannot be slipped under another, and a foreign app will not decrypt our blob with its DPAPI.

Outside Windows (dev/mac/linux) there is no DPAPI. So the module can be tested
and the agent fails meaningfully instead of silently writing plaintext, the
non-Windows backend is enabled ONLY by an explicit `OP_AGENT_DEV_CRED_STORE=1` and keeps a reversibly
obfuscated blob with a loud marker "NOT secure, dev only". By default
outside Windows any access to the password raises CredError.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from .config import state_dir

_ENTROPY_PREFIX = b"op-agent/greenplum/v1:"
# Explicit marker at the start of the dev blob: if such a file ever catches an eye,
# it is immediately clear that it is NOT Windows DPAPI and NOT for production.
_DEV_MAGIC = b"OPAGENT-DEV-INSECURE-CRED\x00"
_DEV_XOR_KEY = b"op-agent-dev-not-a-secret"


class CredError(Exception):
    """Credential store unavailable/broken. The message NEVER carries the password."""


# --------------------------------------------------------------------------- #
# Windows DPAPI (real backend)                                                #
# --------------------------------------------------------------------------- #
def _dpapi(protect: bool, data: bytes, entropy: bytes) -> bytes:
    """CryptProtectData / CryptUnprotectData via ctypes. Windows only."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    def _blob(b: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    b_in, b_ent, b_out = _blob(data), _blob(entropy), DATA_BLOB()
    # protect(pIn, descr, pEntropy, reserved, prompt, flags, pOut)
    # unprotect(pIn, ppDescr, pEntropy, reserved, prompt, flags, pOut)
    args = ([ctypes.byref(b_in), None, ctypes.byref(b_ent), None, None,
             CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(b_out)])
    if not fn(*args):
        raise CredError("DPAPI operation failed (WinError %d)"
                        % ctypes.get_last_error())
    try:
        return ctypes.string_at(b_out.pbData, int(b_out.cbData))
    finally:
        kernel32.LocalFree(ctypes.cast(b_out.pbData, ctypes.c_void_p))


# --------------------------------------------------------------------------- #
# Dev backend (non-Windows, only under an explicit flag)                      #
# --------------------------------------------------------------------------- #
def _dev_enabled() -> bool:
    return os.environ.get("OP_AGENT_DEV_CRED_STORE") == "1"


def _dev_transform(data: bytes) -> bytes:
    key = _DEV_XOR_KEY
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _protect(data: bytes, entropy: bytes) -> bytes:
    if sys.platform == "win32":
        return _dpapi(True, data, entropy)
    if _dev_enabled():
        return _DEV_MAGIC + base64.b64encode(_dev_transform(data))
    raise CredError("credential store unavailable: DPAPI is Windows-only "
                    "(set OP_AGENT_DEV_CRED_STORE=1 for an INSECURE dev store)")


def _unprotect(blob: bytes, entropy: bytes) -> bytes:
    if sys.platform == "win32":
        return _dpapi(False, blob, entropy)
    if _dev_enabled():
        if not blob.startswith(_DEV_MAGIC):
            raise CredError("dev credential blob has an unexpected header")
        return _dev_transform(base64.b64decode(blob[len(_DEV_MAGIC):]))
    raise CredError("credential store unavailable: DPAPI is Windows-only")


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #
def _entropy(connection: str) -> bytes:
    return _ENTROPY_PREFIX + connection.encode("utf-8")


class CredStore:
    """Per-connection store of GP passwords. The password lives in memory only for the duration of
    the call — get_password returns a str, the caller must not log it."""

    def __init__(self, root: Path | None = None):
        self.dir = (root or state_dir()) / "creds"

    def _path(self, connection: str) -> Path:
        # connection is validated on the server (_NAME_RE) and frozen in the manifest;
        # here we additionally rule out any separator trickery.
        if not connection or "/" in connection or "\\" in connection or ".." in connection:
            raise CredError("invalid connection name")
        return self.dir / f"{connection}.bin"

    def set_password(self, connection: str, password: str) -> None:
        blob = _protect(password.encode("utf-8"), _entropy(connection))
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(connection)
        tmp = path.with_suffix(".bin.tmp")
        tmp.write_bytes(blob)
        try:
            os.chmod(tmp, 0o600)  # no-op on Windows, a safety net on POSIX dev
        except OSError:
            pass
        os.replace(tmp, path)  # atomic swap

    def get_password(self, connection: str) -> str | None:
        path = self._path(connection)
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return None
        return _unprotect(blob, _entropy(connection)).decode("utf-8")

    def has_password(self, connection: str) -> bool:
        return self._path(connection).is_file()

    def list_connections(self) -> list[str]:
        """Connection names for which a password is saved. The passwords themselves are not
        revealed — only the labels they are stored under (needed to
        catch a password saved under a former connection name)."""
        try:
            return sorted(p.stem for p in self.dir.glob("*.bin"))
        except OSError:
            return []

    def delete_password(self, connection: str) -> None:
        try:
            self._path(connection).unlink()
        except FileNotFoundError:
            pass
