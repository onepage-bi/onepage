from __future__ import annotations

import queue
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from time import perf_counter
from typing import NamedTuple, Optional

_SESSION_TTL = 8 * 3600
COOKIE_NAME = "op_session"


@dataclass
class UserSession:
    session_id: str
    user_id: int
    person_id: str
    role: str
    created_at: float = field(default_factory=time.time)
    _last_seen: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        return (time.time() - self._last_seen) > _SESSION_TTL

    def touch(self) -> None:
        self._last_seen = time.time()


class AuthRow(NamedTuple):
    """Attribute-named row for the auth JOIN (order must match _AUTH_SQL)."""
    user_id: int
    s_created: float
    last_seen: float
    id: int
    person_id: str
    display_name: Optional[str]
    role: str
    is_active: int
    u_created: Optional[str]
    last_login: Optional[str]
    lang: Optional[str]


class SqliteSessionStore:
    """SQLite-backed sessions: shared across uvicorn workers (OP_WORKERS > 1 is
    impossible with an in-memory dict — login on worker A would 401 on worker B)
    and surviving restarts.

    Event-loop safety (get_with_user is called from async deps, in-loop):
    - reads go through a DEDICATED read-only sqlite3 connection — never the
      shared SQLAlchemy QueuePool, whose exhaustion by threadpool routes would
      otherwise block the loop for up to pool_timeout;
    - the sliding-TTL last_seen UPDATE (throttled to 1/min per session) and the
      expired-session DELETE are queued to a background writer thread — a busy
      writer lock can therefore never stall the loop for busy_timeout. TTL is
      enforced by the read path regardless of when the DELETE lands.

    No DB work happens at import time: schema + connections are created lazily
    on first use (the lifespan purge_expired() call triggers it at startup)."""

    _TOUCH_EVERY = 60.0

    _AUTH_SQL = (
        "SELECT s.user_id, s.created_at, s.last_seen,"
        "       u.id, u.person_id, u.display_name, u.role, u.is_active,"
        "       u.created_at, u.last_login, u.lang"
        " FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.session_id = ? AND u.is_active = 1"
    )

    def __init__(self) -> None:
        self._ready = False
        self._init_lock = threading.Lock()
        self._read_conn: Optional[sqlite3.Connection] = None
        self._read_lock = threading.Lock()
        self._writes: "queue.SimpleQueue[tuple]" = queue.SimpleQueue()

    def _ensure(self) -> None:
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            from sqlalchemy import text
            from app.config import settings
            from app.database import engine
            self._engine = engine
            self._text = text
            with engine.connect() as conn:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS sessions ("
                    " session_id TEXT PRIMARY KEY,"
                    " user_id INTEGER NOT NULL,"
                    " person_id TEXT NOT NULL,"
                    " role TEXT NOT NULL,"
                    " created_at REAL NOT NULL,"
                    " last_seen REAL NOT NULL)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_sessions_last_seen ON sessions (last_seen)"
                ))
                conn.commit()
            rconn = sqlite3.connect(settings.abs_db_path, check_same_thread=False)
            rconn.execute("PRAGMA busy_timeout=250")  # read-only conn must fail fast, never hold event loop
            rconn.execute("PRAGMA query_only=ON")  # hard guarantee: never writes
            self._read_conn = rconn
            threading.Thread(target=self._writer_loop, daemon=True,
                             name="op-session-writer").start()
            self._ready = True

    def _writer_loop(self) -> None:
        while True:
            op = self._writes.get()
            try:
                with self._engine.connect() as conn:
                    if op[0] == "touch":
                        conn.execute(self._text(
                            "UPDATE sessions SET last_seen = :now WHERE session_id = :sid"
                        ), {"now": op[2], "sid": op[1]})
                    else:  # "delete"
                        conn.execute(self._text(
                            "DELETE FROM sessions WHERE session_id = :sid"
                        ), {"sid": op[1]})
                    conn.commit()
            except Exception:
                pass  # best-effort: the read path enforces TTL regardless

    @staticmethod
    def _perf(t0: float) -> None:
        # The dedicated connection bypasses the engine's SQL timing listeners —
        # account for it manually so db_ms/db_n stay honest.
        from app.services import perflog
        rec = perflog.request_record.get()
        if rec is not None:
            rec["db_n"] = rec.get("db_n", 0) + 1
            rec["db_ms"] = rec.get("db_ms", 0.0) + (perf_counter() - t0) * 1000

    def create(self, user_id: int, person_id: str, role: str) -> str:
        self._ensure()
        sid = secrets.token_hex(32)
        now = time.time()
        with self._engine.connect() as conn:
            conn.execute(self._text(
                "INSERT INTO sessions VALUES (:sid, :uid, :ovid, :role, :now, :now)"
            ), {"sid": sid, "uid": user_id, "ovid": person_id, "role": role, "now": now})
            conn.commit()
        return sid

    def get_with_user(self, session_id: str) -> Optional[tuple]:
        """Session + active user in ONE read (the auth hot path).
        Returns (UserSession, AuthRow) or None."""
        self._ensure()
        now = time.time()
        t0 = perf_counter()
        with self._read_lock:
            try:
                raw = self._read_conn.execute(self._AUTH_SQL, (session_id,)).fetchone()
            except sqlite3.OperationalError:
                # checkpoint restart busy condition clears in microseconds; retry once
                raw = self._read_conn.execute(self._AUTH_SQL, (session_id,)).fetchone()
        self._perf(t0)
        if raw is None:
            return None
        row = AuthRow(*raw)
        if now - row.last_seen > _SESSION_TTL:
            self._writes.put(("delete", session_id))
            return None
        if now - row.last_seen > self._TOUCH_EVERY:
            self._writes.put(("touch", session_id, now))
        sess = UserSession(session_id=session_id, user_id=row.user_id,
                           person_id=row.person_id, role=row.role,
                           created_at=row.s_created)
        sess._last_seen = row.last_seen
        return sess, row

    def delete(self, session_id: str) -> None:
        self._ensure()
        with self._engine.connect() as conn:
            conn.execute(self._text("DELETE FROM sessions WHERE session_id = :sid"),
                         {"sid": session_id})
            conn.commit()

    def delete_for_user(self, user_id: int) -> int:
        self._ensure()
        with self._engine.connect() as conn:
            res = conn.execute(self._text("DELETE FROM sessions WHERE user_id = :uid"),
                               {"uid": user_id})
            conn.commit()
            return res.rowcount or 0

    def purge_expired(self) -> int:
        self._ensure()
        cutoff = time.time() - _SESSION_TTL
        with self._engine.connect() as conn:
            res = conn.execute(self._text("DELETE FROM sessions WHERE last_seen < :cutoff"),
                               {"cutoff": cutoff})
            conn.commit()
            return res.rowcount or 0


session_store = SqliteSessionStore()


def get_session_id_from_cookie(cookie_header: str) -> Optional[str]:
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{COOKIE_NAME}="):
            return part[len(f"{COOKIE_NAME}="):].strip()
    return None
