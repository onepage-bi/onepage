"""Read keys for the desktop program: issue, verify, revoke.

Why this is a separate module and not a couple of functions in `deps.py`: key
verification must live in exactly one place, otherwise "read-only" will sooner or later
stop being true. This is also the only place where a key exists in plaintext —
at the moment of issue, after which only its sha256 remains.

Format `opdt_<43 base64url characters>`. The prefix protects against nothing; it
is there so that a leaked string can be recognised by eye in a log or a ticket and
it is clear what to revoke — the same idea as `OPAGENT-DEV-INSECURE-CRED` in
`agent/creds.py`.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import DeviceToken, User

PREFIX = "opdt_"
# 32 bytes of entropy. Brute-forcing that is pointless, so the hash is plain
# sha256 without stretching — rationale in the `DeviceToken` model docstring.
_NBYTES = 32
DEFAULT_TTL_DAYS = 90
MAX_PER_USER = 5
# `last_used_at` is updated no more than once an hour: this is not a hot path, but
# there is no reason to write on every GET.
_TOUCH_EVERY_S = 3600


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue(db: Session, user: User, label: str = "", ttl_days: int = DEFAULT_TTL_DAYS) -> tuple[str, DeviceToken]:
    """Create a key and return (plaintext string, DB row).

    The caller must hand the plaintext to the person ONCE and store it nowhere
    else: it cannot be recovered from the database by construction.
    """
    raw = PREFIX + secrets.token_urlsafe(_NBYTES)
    row = DeviceToken(
        user_id=user.id,
        token_hash=hash_token(raw),
        label=(label or "")[:64],
        expires_at=datetime.utcnow() + timedelta(days=max(1, int(ttl_days))),
    )
    db.add(row)
    return raw, row


def resolve(db: Session, raw: str) -> DeviceToken | None:
    """Find a live key by the presented string. No reason for refusal
    is disclosed — to the client it is simply "not authenticated"."""
    if not raw or not raw.startswith(PREFIX):
        return None
    row = db.query(DeviceToken).filter(DeviceToken.token_hash == hash_token(raw)).first()
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at is not None and row.expires_at <= datetime.utcnow():
        return None
    return row


def touch(db: Session, row: DeviceToken) -> bool:
    """Record usage, but no more than once an hour. Returns whether it wrote —
    so the caller knows whether a commit is needed."""
    now = datetime.utcnow()
    if row.last_used_at is not None and (now - row.last_used_at).total_seconds() < _TOUCH_EVERY_S:
        return False
    row.last_used_at = now
    return True


def active_for(db: Session, user: User) -> list[DeviceToken]:
    return (
        db.query(DeviceToken)
          .filter(DeviceToken.user_id == user.id, DeviceToken.revoked_at.is_(None))
          .order_by(DeviceToken.created_at.desc())
          .all()
    )


def revoke(db: Session, user: User, token_id: int) -> bool:
    """Revoke only one's own key. Someone else's id is a 404 for the caller, not a 403:
    it is not our business to disclose that other people's keys exist."""
    row = (
        db.query(DeviceToken)
          .filter(DeviceToken.id == token_id, DeviceToken.user_id == user.id,
                  DeviceToken.revoked_at.is_(None))
          .first()
    )
    if row is None:
        return False
    row.revoked_at = datetime.utcnow()
    return True
