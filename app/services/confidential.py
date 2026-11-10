""""Highly confidential" — a second lock on a report.

What it is. The report owner sets a password; a person on the access list, when opening
the report, enters it once per session. The access list is NOT replaced by the password but
complemented by it (owner's decision 2026-10-20): revoking one person
still costs one row in `dashboard_permissions`, while the password closes
what the list does not — an accidentally granted right and someone else's unlocked
laptop.

**What the password does not do, and this is the key point.** It decides who gets in, and decides
nothing about what the person who got in takes away: a report is a file and slots, and whoever passed
the lock has the bytes. So the mode is not just a door but a set of doors closed
at once, and their list MUST BE KEPT IN ONE PLACE, otherwise the next export endpoint
will appear bypassing it:

* share links — 403 for everyone, including the owner and the administrator (as with RLS:
  a link is anonymous, there is no one to present the password to);
* "report as a single file" (`/standalone`) and version downloads — otherwise a copy
  goes to disk and lives on by itself;
* the source (`/source`);
* the desktop mirror — `mirrorable: false`, reason `confidential`: a device
  key syncs a copy at night, with no person and no password;
* the "Explore" chat and server-side query execution over this report — they
  read the same rows by another path (owner's decision: close for everyone).

What the mode cannot and never can do: a screenshot, a photo, retelling.
This is said out loud in the interface, otherwise the mode promises more than it does.

Why the lock is a database row and not an in-memory dict: `OP_WORKERS` can be
more than one. Details are in the `DashboardUnlock` docstring.
"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.auth import local as pw
from app.config import settings
from app.models import Dashboard, DashboardUnlock, User

# Minimum password length. Not a "complexity policy" (that drives people to
# "Password1!" and a sticky note on the monitor), but a lower bound below which
# even the attempt counter would not stop brute force.
MIN_PASSWORD_LEN = 8

# Refusal codes. A code, not text: the text is translated and lives in the string catalogue,
# while the test and the client look at the code.
LOCKED = "confidential_locked"
BLOCKED = "confidential_mode"
TOO_MANY = "confidential_too_many"


def enabled(dashboard: Dashboard) -> bool:
    return bool(dashboard.conf_hash)


def fingerprint(conf_hash: str | None) -> str:
    """Password fingerprint for the unlock row.

    A fingerprint is stored, not a hash: there is nothing for brute force to work with
    in the unlocks table. Changing the password changes the fingerprint and thereby voids ALL
    granted unlocks, without having to find and delete them.
    """
    if not conf_hash:
        return ""
    return hashlib.sha256(conf_hash.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Attempt counter
# --------------------------------------------------------------------------
# In process memory on purpose: this is not a right or a state that must
# survive a restart, but a brake against guessing. The pair (user, report) —
# not IP: inside the perimeter a whole floor shares an address, and keying on it would block
# the floor rather than the one guessing.
_attempts: dict[tuple[int, str], list[float]] = {}
_attempts_lock = threading.Lock()


def _too_many(user_id: int, dashboard_id: str) -> bool:
    now = time.monotonic()
    window = settings.confidential_attempt_window_s
    with _attempts_lock:
        hits = [t for t in _attempts.get((user_id, dashboard_id), ()) if now - t < window]
        _attempts[(user_id, dashboard_id)] = hits
        return len(hits) >= settings.confidential_attempts


def _note_attempt(user_id: int, dashboard_id: str) -> None:
    now = time.monotonic()
    with _attempts_lock:
        _attempts.setdefault((user_id, dashboard_id), []).append(now)
        # The dict is cleaned as we go: there are few pairs (confidential reports
        # are a handful), but it must not grow without bound.
        if len(_attempts) > 4096:
            window = settings.confidential_attempt_window_s
            for key, hits in list(_attempts.items()):
                if not [t for t in hits if now - t < window]:
                    _attempts.pop(key, None)


def _clear_attempts(user_id: int, dashboard_id: str) -> None:
    with _attempts_lock:
        _attempts.pop((user_id, dashboard_id), None)


# --------------------------------------------------------------------------
# Lock
# --------------------------------------------------------------------------

def is_unlocked(db: Session, session_id: str | None, dashboard: Dashboard) -> bool:
    """Is the lock removed in THIS session and is the unlock still valid."""
    if not enabled(dashboard):
        return True
    if not session_id:
        return False
    row = (db.query(DashboardUnlock)
           .filter(DashboardUnlock.session_id == session_id,
                   DashboardUnlock.dashboard_id == dashboard.id)
           .first())
    if row is None:
        return False
    if row.expires_at <= datetime.utcnow():
        return False
    # The password was changed after the lock was removed — the unlock is invalid.
    return row.conf_fp == fingerprint(dashboard.conf_hash)


def assert_unlocked(db: Session, session_id: str | None, dashboard: Dashboard) -> None:
    """403 with a code by which the page knows: ask for the password."""
    if not is_unlocked(db, session_id, dashboard):
        raise HTTPException(status_code=403, detail=LOCKED)


def assert_unlocked_request(db: Session, request, dashboard: Dashboard) -> None:
    """Same as `assert_unlocked`, but gets the session from the request itself.

    For endpoints that do not go through `require_dashboard_access` (they have their own,
    narrower right — e.g. `/source` requires "refresh data").
    The second lock must stand there too: a maintainer's right is not a password.
    """
    if not enabled(dashboard):
        return
    from app.auth.session import get_session_id_from_cookie
    sid = get_session_id_from_cookie(request.headers.get("cookie", ""))
    assert_unlocked(db, sid, dashboard)


def assert_not_confidential(dashboard: Dashboard) -> None:
    """409 for exports that nobody gets in this mode.

    Precisely 409, not 403: it is not about the requester's rights (the owner gets the same
    answer) but about the report's state — exactly the same logic by which an RLS report
    refuses a single-file export.
    """
    if enabled(dashboard):
        raise HTTPException(status_code=409, detail=BLOCKED)


def unlock(db: Session, session_id: str, user: User, dashboard: Dashboard, password: str) -> bool:
    """Check the password and, if correct, remove the lock for a while in this session.

    Committing is the caller's job, as with `AuditLog`.
    """
    if not enabled(dashboard):
        return True
    if not session_id:
        raise HTTPException(status_code=403, detail=LOCKED)
    if _too_many(user.id, dashboard.id):
        raise HTTPException(status_code=429, detail=TOO_MANY)
    if not pw.verify(password or "", dashboard.conf_hash or ""):
        _note_attempt(user.id, dashboard.id)
        return False

    _clear_attempts(user.id, dashboard.id)
    now = datetime.utcnow()
    expires = now + timedelta(seconds=settings.confidential_unlock_ttl_s)
    row = (db.query(DashboardUnlock)
           .filter(DashboardUnlock.session_id == session_id,
                   DashboardUnlock.dashboard_id == dashboard.id)
           .first())
    if row is None:
        row = DashboardUnlock(session_id=session_id, dashboard_id=dashboard.id)
        db.add(row)
    row.user_id = user.id
    row.conf_fp = fingerprint(dashboard.conf_hash)
    row.created_at = now
    row.expires_at = expires
    # Expired unlocks of this same report are cleaned up here, not by a background
    # sweeper: the platform has no background sweepers by design, and the place
    # for cleanup is a rare path that writes to the database anyway.
    (db.query(DashboardUnlock)
       .filter(DashboardUnlock.dashboard_id == dashboard.id,
               DashboardUnlock.expires_at <= now)
       .delete(synchronize_session=False))
    return True


def set_password(db: Session, dashboard: Dashboard, password: str) -> None:
    """Enable the mode or change the password. Committing is the caller's job."""
    if len(password or "") < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=422, detail="confidential_password_short")
    dashboard.conf_hash = pw.make_hash(password)
    # Unlocks need not be deleted: they stopped working by themselves because the password
    # fingerprint changed. Still worth deleting so the table does not accumulate garbage
    # that no longer means anything.
    _drop_unlocks(db, dashboard)


def clear(db: Session, dashboard: Dashboard) -> None:
    """Disable the mode. Committing is the caller's job."""
    dashboard.conf_hash = None
    _drop_unlocks(db, dashboard)


def _drop_unlocks(db: Session, dashboard: Dashboard) -> None:
    (db.query(DashboardUnlock)
       .filter(DashboardUnlock.dashboard_id == dashboard.id)
       .delete(synchronize_session=False))
