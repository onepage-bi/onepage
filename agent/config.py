"""Agent configuration and state.

Two principles baked into the code:
  1. The agent NEVER stores the GP password in its config. The password lives in
     Windows DPAPI (agent/creds.py, ctypes — no packages); here we keep only
     the logical connection name and the topology (host/port/dbname/user),
     which is not a secret.
  2. The origin allowlist is strict: the agent sends the CORS header
     Access-Control-Allow-Origin ONLY for origins on the list, never '*'.

Standard library only — it ships on a portable python next to the exe.
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path


# Ports are tried in a ladder: a successful bind = the single-instance mutex (server.py).
PORT_LADDER = (47113, 47114, 47115)

# Default allowlist when agent.ini is absent — the OnePage prod origin.
_DEFAULT_ORIGINS = ("https://onepage.example.com",)


def state_dir() -> Path:
    """Agent state directory (agent_id, logs, per-run temp).

    Windows: %LOCALAPPDATA%\\op-agent — the same place as the launcher cache.
    Otherwise (dev/mac/linux): ~/.op-agent — so M1 can be tested off Windows.
    """
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "op-agent" if base else Path.home() / ".op-agent"
    root.mkdir(parents=True, exist_ok=True)
    return root


class Config:
    """Parsed agent.ini. A missing file is not an error: defaults are used."""

    def __init__(self, allowed_origins: tuple[str, ...], connections: dict[str, dict[str, str]]):
        self.allowed_origins = allowed_origins
        # connections: {"analytics_dw": {"host":..., "port":..., "dbname":..., "user":...}, ...}
        # There is NO password here and there must not be — topology only.
        self.connections = connections

    def origin_allowed(self, origin: str | None) -> bool:
        return bool(origin) and origin in self.allowed_origins


def load_config(ini_path: Path | None = None) -> Config:
    """Read agent.ini next to the exe (or a given path). Tolerates absence.

    Expected sections:
        [security]
        allowed_origins = https://onepage.example.com, https://onepage-stage.example.com
        [connection:analytics_dw]     ; LOGICAL name (manifest<->ini label), not the DB name
        host = warehouse.example.com
        port = 6432               ; usually a pool (pgbouncer), not GP itself — see gpsource
        dbname = analytics_dw
        user = u10000009          ; NOT a secret — the analyst's personal GP login
        ; We NEVER store the PASSWORD here — DPAPI only (agent/creds.py)
    """
    if ini_path is None:
        # exe_dir/agent.ini; in dev — CWD/agent.ini, otherwise defaults.
        ini_path = Path(os.environ.get("OP_AGENT_INI", "")) if os.environ.get("OP_AGENT_INI") else None

    origins: tuple[str, ...] = _DEFAULT_ORIGINS
    connections: dict[str, dict[str, str]] = {}

    if ini_path and ini_path.is_file():
        cp = configparser.ConfigParser()
        cp.read(ini_path, encoding="utf-8")
        if cp.has_option("security", "allowed_origins"):
            raw = cp.get("security", "allowed_origins")
            parsed = tuple(o.strip() for o in raw.split(",") if o.strip())
            if parsed:
                origins = parsed
        for section in cp.sections():
            if section.startswith("connection:"):
                name = section.split(":", 1)[1].strip()
                # Topology + non-secret login. We IGNORE `password` DELIBERATELY:
                # the password lives only in DPAPI (agent/creds.py), not in the ini.
                conn = {}
                # sslmode/gssencmode — non-secret libpq parameters. Needed when
                # the entry point is a pool: libpq (unlike JDBC) first tries
                # GSSAPI encryption, and some pools silently drop the connection on it.
                for key in ("host", "port", "dbname", "user", "sslmode", "gssencmode",
                            "application_name"):
                    if cp.has_option(section, key):
                        conn[key] = cp.get(section, key)
                connections[name] = conn

    return Config(allowed_origins=origins, connections=connections)
