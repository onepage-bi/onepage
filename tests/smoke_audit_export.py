"""Smoke test for audit_log export: CLI (app/tools/audit_export.py) + route
GET /api/admin/audit-log/export (app/api/admin.py).

Covers: permission gate (visitor denied, admin allowed), JSONL row format
(actor/action/resource/detail/created_at/ip), inclusive date-range boundaries
(a row exactly on `to` day is IN, one the day after is OUT), the >366-day
range rejection, and that the export itself lands in audit_log with
action="audit_export" and the requested range in detail — via BOTH the CLI
and the route, so the two paths cannot silently drift apart.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="op-audit-export-smoke-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
})

sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import AuditLog, User  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


_ctx = TestClient(app)
client = _ctx.__enter__()

client.post("/dev-login", data={"person_id": "admin1"})
r = client.post("/api/admin/users", json={"person_id": "plainuser", "role": "visitor"})
assert r.status_code == 200, r.text

# ── seed a handful of audit_log rows spanning several days, directly via
# the ORM (this test is not about how rows get INTO audit_log, only about
# reading them back out) ──────────────────────────────────────────────
db = SessionLocal()
admin_user = db.query(User).filter(User.person_id == "admin1").first()
admin_id = admin_user.id
DAY = timedelta(days=1)
TODAY = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)

seed_dates = {
    "in_range_old": TODAY - 6 * DAY,     # start of a 7-day [today-6, today] window
    "in_range_mid": TODAY - 3 * DAY,
    "in_range_new": TODAY,               # last day of the window, still inclusive
    "just_before": TODAY - 7 * DAY,      # one day before the window -> excluded
    "just_after": TODAY + 1 * DAY,       # one day after -> excluded
}
for tag, ts in seed_dates.items():
    log = AuditLog(actor_id=admin_user.id, action=f"seed_{tag}", resource_type="widget",
                    resource_id="42", ip_address="192.0.2.9", created_at=ts)
    log.set_detail({"tag": tag, "n": 1})
    db.add(log)
db.commit()
db.close()

date_from = (TODAY - 6 * DAY).strftime("%Y-%m-%d")
date_to = TODAY.strftime("%Y-%m-%d")

# ── permission gate ─────────────────────────────────────────────────────
client.post("/dev-login", data={"person_id": "plainuser"}, follow_redirects=False)
r = client.get(f"/api/admin/audit-log/export?from={date_from}&to={date_to}")
check("visitor: export denied", r.status_code in (401, 403), str(r.status_code))

client.post("/dev-login", data={"person_id": "admin1"})
r = client.get(f"/api/admin/audit-log/export?from={date_from}&to={date_to}")
check("admin: export 200", r.status_code == 200, str(r.status_code))
check("admin: ndjson content-type", r.headers.get("content-type", "").startswith("application/x-ndjson"),
      r.headers.get("content-type"))
check("admin: attachment header", "attachment" in r.headers.get("content-disposition", ""),
      r.headers.get("content-disposition"))

lines = [ln for ln in r.text.splitlines() if ln.strip()]
rows = [json.loads(ln) for ln in lines]
tags = [row["detail"].get("tag") for row in rows if row["action"].startswith("seed_")]

# ── range boundaries: both edge days included, the day before/after excluded
check("in_range_old included", "in_range_old" in tags, tags)
check("in_range_mid included", "in_range_mid" in tags, tags)
check("in_range_new included (inclusive 'to')", "in_range_new" in tags, tags)
check("just_before excluded", "just_before" not in tags, tags)
check("just_after excluded", "just_after" not in tags, tags)

# ── row shape ────────────────────────────────────────────────────────────
sample = next(row for row in rows if row["detail"].get("tag") == "in_range_mid")
check("row has actor=person_id", sample["actor"] == "admin1", sample)
check("row has action", sample["action"] == "seed_in_range_mid", sample)
check("row has resource=type/id", sample["resource"] == "widget/42", sample)
check("row has detail dict", sample["detail"] == {"tag": "in_range_mid", "n": 1}, sample)
check("row has created_at (ISO)", "T" in sample["created_at"], sample)
check("row has ip", sample["ip"] == "192.0.2.9", sample)
check("no query-result-shaped fields leak (sql/rows/columns)",
      not any(k in sample for k in ("sql", "rows", "columns", "result")), sample)

# ── the export itself is audited, with the requested range ──────────────
db = SessionLocal()
export_logs = db.query(AuditLog).filter(AuditLog.action == "audit_export").all()
check("export action logged exactly once (route call)", len(export_logs) == 1, len(export_logs))
if export_logs:
    detail = export_logs[0].get_detail()
    check("export audit carries the range", detail == {"from": date_from, "to": date_to}, detail)
    check("export audit actor is the caller", export_logs[0].actor_id == admin_id)
db.close()

# ── >366 day range rejected, before touching the table ───────────────────
far_from = (TODAY - timedelta(days=400)).strftime("%Y-%m-%d")
r = client.get(f"/api/admin/audit-log/export?from={far_from}&to={date_to}")
check("range >366 days rejected", r.status_code == 400, str(r.status_code))
db = SessionLocal()
check("rejected range did not add another export audit row",
      db.query(AuditLog).filter(AuditLog.action == "audit_export").count() == 1)
db.close()

# ── reversed range rejected ───────────────────────────────────────────────
r = client.get(f"/api/admin/audit-log/export?from={date_to}&to={date_from}")
check("reversed range rejected (from > to)", r.status_code == 400, str(r.status_code))

# ── bad date format rejected ──────────────────────────────────────────────
r = client.get(f"/api/admin/audit-log/export?from=2026-13-99&to={date_to}")
check("bad date format rejected", r.status_code == 400, str(r.status_code))

_ctx.__exit__(None, None, None)

# ── CLI path: python -m app.tools.audit_export ───────────────────────────
out_path = os.path.join(TMP, "cli-out.jsonl")
proc = subprocess.run(
    [sys.executable, "-m", "app.tools.audit_export", "--from", date_from, "--to", date_to, "--out", out_path],
    cwd=os.getcwd(), env=os.environ, capture_output=True, text=True,
)
check("CLI exits 0", proc.returncode == 0, proc.stderr)
check("CLI wrote the output file", os.path.exists(out_path))
cli_rows = [json.loads(ln) for ln in open(out_path, encoding="utf-8") if ln.strip()]
cli_tags = [row["detail"].get("tag") for row in cli_rows if row["action"].startswith("seed_")]
check("CLI: same 3 in-range rows as the route",
      sorted(t for t in cli_tags if t) == sorted(["in_range_old", "in_range_mid", "in_range_new"]), cli_tags)

# CLI itself does not write to audit_log (no HTTP actor, no permission
# context) — it is a bare export, distinct from the audited route above.
db = SessionLocal()
check("CLI export did not add a second audit_export row",
      db.query(AuditLog).filter(AuditLog.action == "audit_export").count() == 1)
db.close()

# ── CLI: bad range exits non-zero without writing the file ────────────────
bad_out = os.path.join(TMP, "cli-bad.jsonl")
proc = subprocess.run(
    [sys.executable, "-m", "app.tools.audit_export", "--from", date_to, "--to", date_from, "--out", bad_out],
    cwd=os.getcwd(), env=os.environ, capture_output=True, text=True,
)
check("CLI reversed range exits non-zero", proc.returncode != 0, proc.returncode)
check("CLI reversed range did not write output", not os.path.exists(bad_out))

# ── regression: streaming must not leak pooled connections ────────────────
# The route used to hand the generator the request-scoped `db` from
# Depends(get_db). Since FastAPI 0.106, yield-dependency exit runs BEFORE
# the response body is sent, so that `db` was already closed by the time
# the generator ran its SELECT — a closed-but-reused Session silently
# checks a *new* connection out of the pool and never returns it. Every
# export leaked one connection; run several and make sure none stick.
from app.database import engine as _engine  # noqa: E402

client.post("/dev-login", data={"person_id": "admin1"})
_before = _engine.pool.checkedout()
for _ in range(8):
    r = client.get(f"/api/admin/audit-log/export?from={date_from}&to={date_to}")
    check("regression: repeated export still 200", r.status_code == 200, r.status_code)
_after = _engine.pool.checkedout()
check("regression: no connections leaked by repeated export",
      _after <= _before, f"before={_before} after={_after}")

# ── regression: CLI creates a missing --out parent directory ──────────────
nested_out = os.path.join(TMP, "nested", "dir", "audit.jsonl")
proc = subprocess.run(
    [sys.executable, "-m", "app.tools.audit_export", "--from", date_from, "--to", date_to, "--out", nested_out],
    cwd=os.getcwd(), env=os.environ, capture_output=True, text=True,
)
check("regression: CLI creates missing parent dir and exits 0", proc.returncode == 0, proc.stderr)
check("regression: CLI wrote file under new nested dir", os.path.exists(nested_out))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("All checks passed.")
