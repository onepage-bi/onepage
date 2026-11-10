"""Single-use enforcement for SAML assertions.

A signed assertion stays cryptographically valid until its NotOnOrAfter — a few
minutes with ADFS defaults. Signature checking alone does not make it single-use:
the same bytes replayed inside that window authenticate again, and the SP has no
way to tell the second POST from the first. That matters here because /saml/acs
is unauthenticated by definition and reachable by anyone who can reach the site.

So each accepted assertion ID is recorded, and a repeat is refused. Storage is
SQLite, next to the sessions table and for the same reason: OP_WORKERS may be
raised above 1, and a per-process set would then let a replay land on whichever
worker had not seen it. Rows are pruned past the longest window we honour, so
the table stays a few rows wide even on a busy day.

Deliberately NOT a security boundary on its own: it narrows an already-signed,
already-time-bounded assertion to one use. The binding to a specific login
attempt (InResponseTo) is the other half and lives in saml.py.
"""
from __future__ import annotations

import threading
import time

# Longer than any sane assertion lifetime, so an id is remembered for at least
# as long as the assertion carrying it could still validate.
_RETENTION_S = 3600.0
_PRUNE_EVERY_S = 300.0

_ready = False
_init_lock = threading.Lock()
_last_prune = 0.0


def _ensure():
    global _ready
    if _ready:
        return
    with _init_lock:
        if _ready:
            return
        from sqlalchemy import text
        from app.database import engine
        with engine.connect() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS saml_assertions ("
                " assertion_id TEXT PRIMARY KEY,"
                " seen_at REAL NOT NULL)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_saml_assertions_seen_at"
                " ON saml_assertions (seen_at)"
            ))
            conn.commit()
        _ready = True


def claim(assertion_id: str) -> bool:
    """Record this assertion id. False if it was already used (a replay).

    Called once per successful signature validation, on the login path only —
    a handful of times a day, so the cost of a write here is irrelevant.
    """
    global _last_prune
    if not assertion_id:
        # No id to key on: cannot prove single use, so do not claim to.
        return True
    _ensure()
    from sqlalchemy import text
    from app.database import engine

    now = time.time()
    with engine.connect() as conn:
        result = conn.execute(
            text("INSERT OR IGNORE INTO saml_assertions (assertion_id, seen_at)"
                 " VALUES (:aid, :now)"),
            {"aid": assertion_id, "now": now},
        )
        fresh = result.rowcount == 1
        if now - _last_prune > _PRUNE_EVERY_S:
            conn.execute(text("DELETE FROM saml_assertions WHERE seen_at < :cut"),
                         {"cut": now - _RETENTION_S})
            _last_prune = now
        conn.commit()
    return fresh
