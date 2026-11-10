"""DeepSeek access control (owner decision 2026-08-23: "only for a limited
number of users, assigned only by me").

Two independent gates:

- `allowed(db, user)` — may this user USE the paid provider? True for
  everyone while the provider is Ollama (local, free); for DeepSeek it
  requires an `llm_access` row. Checked wherever a model call or the
  browser-key handout would happen (app/api/explore.py).
- `can_assign(user)` — may this user GRANT/REVOKE access? Administrator
  role, further narrowed by `OP_LLM_ACCESS_ADMINS` (comma-separated
  person_ids). The narrowing lives in env on purpose: it can only be
  changed by someone with server access, so other administrators cannot
  widen it from the UI.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import settings
from app.models import ROLE_ADMINISTRATOR, LlmAccess, User


def assign_admins() -> list[str]:
    return [s.strip() for s in (settings.llm_access_admins or "").split(",") if s.strip()]


def can_assign(user: User) -> bool:
    if user.role != ROLE_ADMINISTRATOR:
        return False
    admins = assign_admins()
    if not admins:
        return True
    # The env may name the person by the numeric person_id OR by the UPN
    # the account merged from (users.upn) — both are the same person.
    mine = {user.person_id.lower(), (getattr(user, "upn", None) or "").lower()} - {""}
    return any(a.lower() in mine for a in admins)


def has_row(db: Session, user_id: int) -> bool:
    return db.query(LlmAccess.id).filter(LlmAccess.user_id == user_id).first() is not None


def allowed(db: Session, user: User) -> bool:
    """May `user` reach the CURRENT provider's model? Import nl2sql lazily —
    provider() re-reads the runtime overlay, and nl2sql imports must not
    become circular through this module."""
    from app.services import nl2sql
    if nl2sql.provider() != "deepseek":
        return True
    return has_row(db, user.id)
