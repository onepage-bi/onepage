"""Export of `audit_log` to JSONL — one line per record.

Shared code for two paths:
  * the CLI, for manual/cron runs:
        python -m app.tools.audit_export --from 2026-08-01 --to 2026-08-31 --out audit.jsonl
  * the route `GET /api/admin/audit-log/export` (app/api/admin.py), which calls
    `resolve_range()`/`iter_export_rows()` directly — it does not spawn a process.

Each line is one JSON object: `actor` (`users.person_id` of the user who recorded
the action, or `null` for system records), `action`,
`resource` (`"{resource_type}/{resource_id}"`, or just `resource_type`
without an id, as on the /admin/audit page), `detail` (an already unpacked dict —
what `AuditLog.get_detail()` would return), `created_at` (ISO 8601),
`ip` (may be `null`). `audit_log.detail` never stores query results
(SQL/data) — only the fact of them and truncated metadata (see
`app/api/explore.py`), so there is nothing beyond that to export.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, User

# One calendar year (with a margin for a leap year) — the same limit
# the CLI and the route apply identically, before touching a single table row.
MAX_RANGE_DAYS = 366


class InvalidRange(ValueError):
    """Bad date format, reversed range or a range wider than MAX_RANGE_DAYS."""


def parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise InvalidRange(f"Invalid date (expected YYYY-MM-DD): {s!r}") from None


def resolve_range(from_str: str, to_str: str) -> tuple[datetime, datetime]:
    """`(from, to)` as `YYYY-MM-DD` strings -> `[start, end)` naive UTC
    datetimes — `to` includes its whole day. Raises `InvalidRange` on
    a bad format, a reversed range or a range wider than `MAX_RANGE_DAYS`
    days — before a single DB query."""
    d_from = parse_date(from_str)
    d_to = parse_date(to_str)
    if d_from > d_to:
        raise InvalidRange("'from' must not be after 'to'")
    span_days = (d_to - d_from).days + 1
    if span_days > MAX_RANGE_DAYS:
        raise InvalidRange(f"Range too wide: {span_days} days (max {MAX_RANGE_DAYS})")
    start = datetime(d_from.year, d_from.month, d_from.day)
    end = datetime(d_to.year, d_to.month, d_to.day) + timedelta(days=1)
    return start, end


def iter_export_rows(db: Session, start: datetime, end: datetime) -> Iterator[dict]:
    """Streams `audit_log` in `[start, end)` by `created_at`, ascending,
    without loading the whole range into memory (`yield_per`). One `outerjoin` to
    `users` — no N+1 on actor per row; an actor without an account (already
    deleted/merged) is returned as `actor: null`, it does not fail."""
    stmt = (
        select(
            AuditLog.action, AuditLog.resource_type, AuditLog.resource_id,
            AuditLog.detail, AuditLog.ip_address, AuditLog.created_at,
            User.person_id,
        )
        .outerjoin(User, AuditLog.actor_id == User.id)
        .where(AuditLog.created_at >= start, AuditLog.created_at < end)
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    for action, rtype, rid, detail, ip, created_at, person_id in db.execute(stmt).yield_per(500):
        resource = f"{rtype}/{rid}" if rid else (rtype or None)
        yield {
            "actor": person_id,
            "action": action,
            "resource": resource,
            "detail": json.loads(detail) if detail else {},
            "created_at": created_at.isoformat(),
            "ip": ip,
        }


def row_to_jsonl(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"


def write_jsonl(rows: Iterable[dict], out) -> int:
    n = 0
    for row in rows:
        out.write(row_to_jsonl(row))
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Export audit_log to JSONL")
    ap.add_argument("--from", dest="date_from", required=True, metavar="YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", required=True, metavar="YYYY-MM-DD")
    ap.add_argument("--out", dest="out_path", required=True, metavar="file.jsonl")
    args = ap.parse_args(argv)

    try:
        start, end = resolve_range(args.date_from, args.date_to)
    except InvalidRange as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    out_dir = os.path.dirname(args.out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    from app.database import SessionLocal
    db = SessionLocal()
    try:
        with open(args.out_path, "w", encoding="utf-8") as f:
            n = write_jsonl(iter_export_rows(db, start, end), f)
    finally:
        db.close()
    print(f"{n} rows -> {args.out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
