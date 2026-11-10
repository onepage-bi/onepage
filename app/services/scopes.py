"""Sales-territory scopes (ADR-18) — parsing, person resolution and the
`user_scopes` recompute, behind /admin/scopes (app/api/scopes.py).

Two source formats land here (`parse_csv`):
- **wide** — the raw export of `analytics_dw.v_positions`
  (`v_positions.csv`): one row per TMR position, columns for all
  four hierarchy levels (HOU → RM → CM → TMR) at once. Detected by the
  presence of a `tmr_position_name` column.
- **long** — the manual/test format: one row per (person, role, node,
  parent). This is also what the wide format is normalized *into* before
  `apply_import` ever touches the database, so there is exactly one write
  path regardless of which file was uploaded.

`SalesScope` rows come in two flavours:
- person rows (`person_key` = a real identifier, `person_id` resolved or
  NULL) — these are what `recompute_user_scopes` groups by person_id.
- edge rows (`person_key="-"`, `role="NODE"`, `person_id=None`) — these carry
  the hierarchy (parent → child) even for a vacant position, because the
  subtree BFS needs every edge, not just the ones with a person attached.
  They never count toward `persons`/`resolved`/`unresolved`.

Recompute (`recompute_user_scopes`) lives here, not in `app/services/rls.py`
per ADR-18: rls.py only ever *reads* `UserScope` on the serve hot path.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.i18n import _
from app.models import Employee, SalesScope, SalesScopeSync, User, UserScope

# Key normalisation is one for the whole project (ADR-18). A copy here would mean
# two different answers to the same question: a local duplicate that sat behind
# an ImportError lost the `None -> ""` branch and turned norm(None) into "none".
from app.services.rls import norm

MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_ROWS = 300_000

# Header aliases (casefold, trim) for the long/manual format.
PERSON_ALIASES = {"person", "person_id", "login", "employeeid", "upn", "email", "user"}
ROLE_ALIASES = {"role"}
NODE_ALIASES = {"node", "position", "position_name"}
PARENT_ALIASES = {"parent", "parent_node", "parent_position"}

# Wide-format hierarchy, top to bottom (level key, node column, upn column, login column).
_WIDE_LEVELS = [
    ("hou", "hou_position_name", "hou_userprincipalname", "hou_login"),
    ("rm", "rm_position_name", "rm_userprincipalname", "rm_login"),
    ("cm", "cm_position_name", "cm_userprincipalname", "cm_login"),
    ("tmr", "tmr_position_name", "tmr_userprincipalname", "tmr_login"),
]
_WIDE_MARKER = "tmr_position_name"

NODE_PERSON_KEY = "-"
NODE_ROLE = "NODE"


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(_("The file cannot be read as UTF-8 or CP1251"))


def _header_key(h: str | None) -> str:
    return (h or "").strip().casefold()


def _sniff_delimiter(first_line: str) -> str:
    return ";" if first_line.count(";") >= first_line.count(",") else ","


def _row_get(rec: dict, keys: set[str]) -> str:
    for k, v in rec.items():
        if k and _header_key(k) in keys:
            return (v or "").strip() if isinstance(v, str) else (str(v) if v is not None else "")
    return ""


def parse_csv(raw: bytes) -> dict:
    """Parse an import file into normalized long rows.

    Returns {"format": "wide"|"long", "rows": [{"person_key","role","node",
    "parent"}...], "stats": {"total_lines": int, "skipped": int}}.
    `rows` includes NODE edge rows for the wide format (person_key="-").
    Raises ValueError on anything that should surface as a 415/422.
    """
    text = _decode(raw)
    first_line = text.split("\n", 1)[0]
    delim = _sniff_delimiter(first_line)
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    headers = [_header_key(h) for h in (reader.fieldnames or [])]
    if not headers:
        raise ValueError(_("The file has no data rows"))

    if _WIDE_MARKER in headers:
        return _parse_wide(reader)
    return _parse_long(reader, headers)


def _parse_long(reader: csv.DictReader, headers: list[str]) -> dict:
    if not (PERSON_ALIASES & set(headers)):
        raise ValueError(
            _("The file has no person/person_id/login/employeeid/upn/email/user column")
        )
    rows: list[dict] = []
    total = 0
    skipped = 0
    for rec in reader:
        if total >= MAX_IMPORT_ROWS:
            raise ValueError(_("More than {n} rows", n=MAX_IMPORT_ROWS))
        total += 1
        person = _row_get(rec, PERSON_ALIASES)
        if not person:
            skipped += 1
            continue
        role = _row_get(rec, ROLE_ALIASES).upper() or "UNKNOWN"
        node = _row_get(rec, NODE_ALIASES) or None
        parent = _row_get(rec, PARENT_ALIASES) or None
        rows.append({
            "person_key": person.casefold(),
            "role": role,
            "node": node,
            "parent": parent,
        })
    return {"format": "long", "rows": rows, "stats": {"total_lines": total, "skipped": skipped}}


def _parse_wide(reader: csv.DictReader) -> dict:
    rows: list[dict] = []
    total = 0
    skipped = 0
    seen_tmr: set[str] = set()
    for rec in reader:
        if total >= MAX_IMPORT_ROWS:
            raise ValueError(_("More than {n} rows", n=MAX_IMPORT_ROWS))
        total += 1
        by_key = {_header_key(k): (v.strip() if isinstance(v, str) else (v or ""))
                  for k, v in rec.items() if k}
        tmr_name = by_key.get(_WIDE_MARKER, "")
        tmr_norm = norm(tmr_name)
        if not tmr_norm:
            # vacant/service TMR position with no real node name — skip & count.
            skipped += 1
            continue
        if tmr_norm in seen_tmr:
            # same position repeats once per id_posterr/customer-cycle-hist row.
            skipped += 1
            continue
        seen_tmr.add(tmr_norm)

        parent_name: str | None = None
        for _level, node_col, upn_col, login_col in _WIDE_LEVELS:
            node_name = by_key.get(node_col, "")
            if not node_name:
                continue
            rows.append({
                "person_key": NODE_PERSON_KEY, "role": NODE_ROLE,
                "node": node_name, "parent": parent_name,
            })
            person = by_key.get(upn_col, "") or by_key.get(login_col, "")
            if person:
                rows.append({
                    "person_key": person.casefold(),
                    "role": _level_role(node_col),
                    "node": node_name, "parent": parent_name,
                })
            parent_name = node_name
    return {"format": "wide", "rows": rows, "stats": {"total_lines": total, "skipped": skipped}}


def _level_role(node_col: str) -> str:
    return node_col.split("_", 1)[0].upper()


def resolve_persons(db: Session, keys: set[str]) -> dict[str, str | None]:
    """Resolve casefolded `person_key`s to `User.person_id`, in order:
    User.upn → User.person_id → Employee.email → Employee.person_id →
    Employee.employeeid → (local part before "@") against User.person_id.
    First match wins; unresolved keys map to None. All comparisons casefold."""
    keys = {k for k in keys if k and k != NODE_PERSON_KEY}
    result: dict[str, str | None] = {k: None for k in keys}
    if not keys:
        return result

    users_by_upn: dict[str, str] = {}
    users_by_person_id: dict[str, str] = {}
    for u in db.query(User.upn, User.person_id).all():
        if u.upn:
            users_by_upn.setdefault(u.upn.casefold(), u.person_id)
        if u.person_id:
            users_by_person_id.setdefault(u.person_id.casefold(), u.person_id)

    employees_by_email: dict[str, str] = {}
    employees_by_person_id: dict[str, str] = {}
    employees_by_employeeid: dict[str, str] = {}
    for e in db.query(Employee.email, Employee.person_id, Employee.employeeid).all():
        if not e.person_id:
            continue
        if e.email:
            employees_by_email.setdefault(e.email.casefold(), e.person_id)
        employees_by_person_id.setdefault(e.person_id.casefold(), e.person_id)
        if e.employeeid:
            employees_by_employeeid.setdefault(e.employeeid.casefold(), e.person_id)

    for key in keys:
        hit = (
            users_by_upn.get(key)
            or users_by_person_id.get(key)
            or employees_by_email.get(key)
            or employees_by_person_id.get(key)
            or employees_by_employeeid.get(key)
        )
        if not hit and "@" in key:
            local = key.split("@", 1)[0]
            hit = users_by_person_id.get(local)
        result[key] = hit
    return result


def apply_import(db: Session, parsed: dict, actor: User, dry_run: bool) -> dict:
    """Replace all `source='import'` SalesScope rows with `parsed["rows"]`,
    resolve persons, recompute user_scopes for everyone, and record a
    SalesScopeSync. `dry_run=True` computes and returns the same counters
    without writing anything (rolled back, never committed)."""
    rows = parsed["rows"]
    person_rows = [r for r in rows if r["person_key"] != NODE_PERSON_KEY]
    person_keys = {r["person_key"] for r in person_rows}
    node_keys = {norm(r["node"]) for r in rows if r.get("node")}

    resolved_map = resolve_persons(db, person_keys)
    resolved = sum(1 for v in resolved_map.values() if v)
    unresolved_keys = sorted(k for k, v in resolved_map.items() if not v)

    counts = {
        "total": parsed["stats"]["total_lines"] - parsed["stats"]["skipped"],
        "persons": len(person_keys),
        "resolved": resolved,
        "unresolved": len(person_keys) - resolved,
        "nodes": len(node_keys),
        "dry_run": dry_run,
    }
    if dry_run:
        counts["unresolved_sample"] = unresolved_keys[:10]
        return counts

    db.query(SalesScope).filter(SalesScope.source == "import").delete(synchronize_session=False)
    now = datetime.utcnow()
    for r in rows:
        node = r.get("node")
        parent = r.get("parent")
        person_id = None if r["person_key"] == NODE_PERSON_KEY else resolved_map.get(r["person_key"])
        db.add(SalesScope(
            person_key=r["person_key"], person_id=person_id, role=r["role"],
            node=node, node_norm=norm(node) if node else None,
            parent_norm=norm(parent) if parent else None,
            source="import", is_active=True, synced_at=now,
        ))
    db.flush()  # autoflush=False on this Session — recompute queries SalesScope
    recompute_user_scopes(db, only=None)
    db.add(SalesScopeSync(
        actor_id=actor.id, total=counts["total"], persons=counts["persons"],
        resolved=counts["resolved"], unresolved=counts["unresolved"], nodes=counts["nodes"],
    ))
    db.commit()
    counts["unresolved_sample"] = unresolved_keys[:10]
    counts["at"] = now.isoformat()
    return counts


def attach_on_login(db: Session, user: User) -> int:
    """Bind import rows that were unresolved at import time to `user`'s
    account, then recompute that one person's UserScope. Called on every
    login: someone whose first SSO login happens after the last import
    would otherwise stay in default deny until the next import. Matching
    mirrors `resolve_persons` (User.upn / User.person_id / local part
    before "@" against person_id). Returns the number of rows attached."""
    cands = {v.casefold() for v in (user.upn, user.person_id) if v}
    if not cands or not user.person_id:
        return 0
    conds = [SalesScope.person_key.in_(sorted(cands))]
    ov = user.person_id.casefold()
    if "@" not in ov:
        conds.append(SalesScope.person_key.like(ov.replace("%", "\\%").replace("_", "\\_") + "@%", escape="\\"))
    rows = db.query(SalesScope).filter(
        SalesScope.person_id.is_(None), SalesScope.is_active.is_(True),
        SalesScope.person_key != NODE_PERSON_KEY, or_(*conds),
    ).all()
    if not rows:
        return 0
    for s in rows:
        s.person_id = user.person_id
    db.flush()
    recompute_user_scopes(db, only={user.person_id})
    db.commit()
    return len(rows)


def _subtree(children: dict[str, set[str]], start: str) -> set[str]:
    seen = {start}
    stack = [start]
    while stack:
        cur = stack.pop()
        for nxt in children.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def recompute_user_scopes(db: Session, only: set[str] | None = None) -> None:
    """Flatten every active SalesScope row into UserScope.keys_json. `only`
    limits the recompute to those person_ids (e.g. one manual grant); None
    recomputes everybody, which is also what deletes stale rows for people
    who lost every SalesScope row. children/see_all/roles all come from
    ALL active rows regardless of `only` — a manual grant must be able to
    reference nodes brought in by the last import."""
    all_active = db.query(SalesScope).filter(SalesScope.is_active.is_(True)).all()

    children: dict[str, set[str]] = {}
    by_person_id: dict[str, list[SalesScope]] = {}
    for s in all_active:
        if s.node_norm and s.parent_norm:
            children.setdefault(s.parent_norm, set()).add(s.node_norm)
        if s.person_id:
            by_person_id.setdefault(s.person_id, []).append(s)

    existing = {u.person_id: u for u in db.query(UserScope).all()}
    target_ids = set(only) if only is not None else (set(by_person_id) | set(existing))

    for oid in target_ids:
        scope_rows = by_person_id.get(oid, [])
        if not scope_rows:
            row = existing.get(oid)
            if row is not None:
                db.delete(row)
            continue
        see_all = any(r.role == "TMD" and not r.node_norm for r in scope_rows)
        keys: set[str] = set()
        for r in scope_rows:
            if r.node_norm:
                keys |= _subtree(children, r.node_norm)
        roles = sorted({r.role for r in scope_rows})
        us = existing.get(oid) or UserScope(person_id=oid)
        us.see_all = see_all
        us.roles = ",".join(roles)
        us.keys_json = json.dumps(sorted(keys), ensure_ascii=False)
        us.key_count = len(keys)
        us.computed_at = datetime.utcnow()
        db.add(us)


def apply_manual_grant(db: Session, person_id: str, body: dict) -> dict:
    """POST /api/admin/scopes/manual — see spec for the three shapes:
    {"see_all": true|false}, {"node": str, "role": str}, {"remove": true}."""
    # The account's canonical person_id: a grant for "Ivan_Petrov@…" and for
    # "ivan_petrov@…" is one and the same person, while UserScope is looked up by exact
    # PK. No such account → 404 upstream, not a silent row pointing nowhere.
    canon = db.query(User.person_id).filter(
        func.lower(User.person_id) == person_id.strip().lower()).first()
    if canon is None or not canon[0]:
        raise LookupError(_("User {person_id} not found", person_id=repr(person_id)))
    person_id = canon[0]
    key = person_id.casefold()
    now = datetime.utcnow()

    if body.get("remove"):
        db.query(SalesScope).filter(
            SalesScope.person_id == person_id, SalesScope.source == "manual",
        ).delete(synchronize_session=False)
        db.flush()
        recompute_user_scopes(db, only={person_id})
        db.commit()
        return {"person_id": person_id, "action": "remove"}

    if "see_all" in body:
        existing = db.query(SalesScope).filter(
            SalesScope.person_id == person_id, SalesScope.source == "manual",
            SalesScope.role == "TMD", SalesScope.node_norm.is_(None),
        ).first()
        if body["see_all"]:
            if existing is None:
                db.add(SalesScope(
                    person_key=key, person_id=person_id, role="TMD", node=None,
                    node_norm=None, parent_norm=None, source="manual",
                    is_active=True, synced_at=now,
                ))
        elif existing is not None:
            db.delete(existing)
        db.flush()
        recompute_user_scopes(db, only={person_id})
        db.commit()
        return {"person_id": person_id, "see_all": bool(body["see_all"])}

    if body.get("node"):
        node = str(body["node"])
        role = str(body.get("role") or "GRANT").upper()
        node_norm = norm(node)
        existing = db.query(SalesScope).filter(
            SalesScope.person_id == person_id, SalesScope.source == "manual",
            SalesScope.node_norm == node_norm,
        ).first()
        if existing is None:
            db.add(SalesScope(
                person_key=key, person_id=person_id, role=role, node=node,
                node_norm=node_norm, parent_norm=None, source="manual",
                is_active=True, synced_at=now,
            ))
        else:
            existing.role = role
            existing.synced_at = now
        db.flush()
        recompute_user_scopes(db, only={person_id})
        db.commit()
        return {"person_id": person_id, "node": node, "role": role}

    raise ValueError(_("One of these is required: see_all, node+role, remove"))


def lookup(db: Session, person_id: str) -> dict:
    us = db.get(UserScope, person_id)
    if us is None:
        return {"found": False, "see_all": False, "roles": [], "key_count": 0, "keys_sample": []}
    roles = [r for r in (us.roles or "").split(",") if r]
    keys = us.get_keys()
    return {
        "found": True, "see_all": us.see_all, "roles": roles,
        "key_count": us.key_count, "keys_sample": keys[:20],
    }
