"""
Performance log endpoints: browser beacon intake, admin summary, export, UI page.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from jinja2 import Environment

from app.auth.deps import get_current_user, require_permission
from app.config import settings
from app.models import User
from app.services import perflog

router = APIRouter()

# ---------------------------------------------------------------------------
# Browser beacon intake
# ---------------------------------------------------------------------------

_MAX_BODY = 32 * 1024
_MAX_EVENTS = 200
_ALLOWED_KEYS = {"t", "page", "url", "name", "ttfb", "fcp", "dcl", "load", "dur", "size", "n"}


@router.post("/api/perf/client")
async def perf_client(
    request: Request,
    user: User = Depends(get_current_user),
):
    if not (settings.perf_enabled and settings.perf_client and perflog.runtime_on()):
        return {"ok": False}
    body = await request.body()
    if len(body) > _MAX_BODY:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        events = json.loads(body).get("events") or []
    except (json.JSONDecodeError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(events, list):
        raise HTTPException(status_code=400, detail="events must be a list")

    now = round(time.time(), 3)
    accepted = 0
    for ev in events[:_MAX_EVENTS]:
        if not isinstance(ev, dict):
            continue
        clean = {"ts": now, "src": "client", "uid": user.id}
        for k, v in ev.items():
            if k not in _ALLOWED_KEYS:
                continue
            if isinstance(v, str):
                clean[k] = v[:300]
            elif isinstance(v, (int, float)) and abs(v) < 1e12:
                clean[k] = round(v, 3)
        if len(clean) > 3:
            perflog.emit(clean)
            accepted += 1
    return {"ok": True, "accepted": accepted}


# ---------------------------------------------------------------------------
# Summary / export helpers
# ---------------------------------------------------------------------------

def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _weight(rec: dict) -> float:
    """Inverse-probability weight: a record sampled at p stands in for 1/p
    requests. Records without the field (legacy files, client beacon, GC)
    were recorded unconditionally — weight 1."""
    s = rec.get("sample")
    if isinstance(s, (int, float)) and 0.0 < s <= 1.0:
        return 1.0 / s
    return 1.0


def _wpct(pairs: list[tuple[float, float]], q: float, total_w: float) -> float:
    """Weighted percentile over (value, weight) pairs sorted by value: the
    smallest value whose cumulative weight reaches q of the total."""
    if not pairs:
        return 0.0
    threshold = q * total_w
    acc = 0.0
    for val, w in pairs:
        acc += w
        if acc >= threshold:
            return val
    return pairs[-1][0]


def _summarize(dt_from: datetime, dt_to: datetime) -> dict:
    # Server rows aggregate WEIGHTED (see _weight): with adaptive sampling a
    # record stands in for 1/sample requests, so counts and percentiles keep
    # estimating the true traffic, not the sampled subset.
    routes: dict[tuple, dict] = {}
    slowest: list[dict] = []
    client_pages: dict[str, dict] = {}
    fast_us: list[tuple[float, float]] = []
    serve_us: list[tuple[float, float]] = []
    total = 0.0

    for rec in perflog.iter_records(dt_from, dt_to):
        if rec.get("src") == "server":
            w = _weight(rec)
            total += w
            key = (rec.get("method", ""), rec.get("route", "?"))
            agg = routes.setdefault(key, {"durs": [], "hdrs": [], "w": 0.0,
                                          "db_ms": 0.0, "db_n": 0.0, "errors": 0.0})
            agg["w"] += w
            agg["durs"].append((rec.get("dur_ms", 0.0), w))
            if isinstance(rec.get("hdr_ms"), (int, float)):
                agg["hdrs"].append((rec["hdr_ms"], w))
            agg["db_ms"] += rec.get("db_ms", 0.0) * w
            agg["db_n"] += rec.get("db_n", 0) * w
            if (rec.get("status") or 0) >= 500:
                agg["errors"] += w
            st = rec.get("stages") or {}
            if isinstance(st.get("fast_us"), (int, float)):
                fast_us.append((st["fast_us"], w))
                if isinstance(st.get("serve_us"), (int, float)):
                    serve_us.append((st["serve_us"], w))
            slowest.append(rec)  # individual records — weighting doesn't apply
            if len(slowest) > 4000:
                slowest.sort(key=lambda r: r.get("dur_ms", 0.0), reverse=True)
                del slowest[200:]
        elif rec.get("src") == "client" and rec.get("t") == "nav":
            agg = client_pages.setdefault(rec.get("page", "?"), {"loads": [], "ttfbs": []})
            if isinstance(rec.get("load"), (int, float)) and rec["load"] > 0:
                agg["loads"].append(rec["load"])
            if isinstance(rec.get("ttfb"), (int, float)):
                agg["ttfbs"].append(rec["ttfb"])

    route_rows = []
    for (method, route), agg in routes.items():
        durs = sorted(agg["durs"])
        hdrs = sorted(agg["hdrs"])
        sw = agg["w"]
        hw = sum(w for _, w in hdrs)
        route_rows.append({
            "method": method, "route": route,
            "count": round(sw), "n_raw": len(durs),
            "p50": round(_wpct(durs, 0.50, sw), 1),
            "p95": round(_wpct(durs, 0.95, sw), 1),
            "p99": round(_wpct(durs, 0.99, sw), 1),
            "max": round(durs[-1][0], 1),
            "hdr_p50": round(_wpct(hdrs, 0.50, hw), 1) if hdrs else None,
            "hdr_p95": round(_wpct(hdrs, 0.95, hw), 1) if hdrs else None,
            "hdr_p99": round(_wpct(hdrs, 0.99, hw), 1) if hdrs else None,
            "avg_db_ms": round(agg["db_ms"] / sw, 1),
            "avg_db_n": round(agg["db_n"] / sw, 1),
            "errors": round(agg["errors"]),
        })
    route_rows.sort(key=lambda r: r["p95"], reverse=True)

    slowest.sort(key=lambda r: r.get("dur_ms", 0.0), reverse=True)
    slow_rows = [{
        "ts": datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "method": r.get("method", ""), "path": r.get("path", ""),
        "status": r.get("status"), "dur_ms": round(r.get("dur_ms", 0.0), 1),
        "db_ms": round(r.get("db_ms", 0.0), 1), "db_n": r.get("db_n", 0),
        "stages": r.get("stages") or {},
    } for r in slowest[:20]]

    page_rows = []
    for page, agg in client_pages.items():
        loads = sorted(agg["loads"])
        ttfbs = sorted(agg["ttfbs"])
        page_rows.append({
            "page": page, "count": max(len(loads), len(ttfbs)),
            "p50_load": round(_pct(loads, 0.50)), "p95_load": round(_pct(loads, 0.95)),
            "p50_ttfb": round(_pct(ttfbs, 0.50)), "p95_ttfb": round(_pct(ttfbs, 0.95)),
        })
    page_rows.sort(key=lambda r: r["p95_load"], reverse=True)

    # Fast-lane microsecond stats: fast_us = full in-server cost of a warm
    # render hit incl. auth; serve_us = cache-lookup -> response only (the
    # number comparable to "handler-only" latency claims).
    fast_us.sort()
    serve_us.sort()
    fw = sum(w for _, w in fast_us)
    sv_w = sum(w for _, w in serve_us)
    fast = {
        "count": round(fw),
        "fast_us_p50": round(_wpct(fast_us, 0.50, fw)), "fast_us_p95": round(_wpct(fast_us, 0.95, fw)),
        "fast_us_p99": round(_wpct(fast_us, 0.99, fw)),
        "serve_us_p50": round(_wpct(serve_us, 0.50, sv_w)), "serve_us_p95": round(_wpct(serve_us, 0.95, sv_w)),
        "serve_us_p99": round(_wpct(serve_us, 0.99, sv_w)),
    } if fast_us else None

    return {"total": round(total), "routes": route_rows, "slowest": slow_rows,
            "pages": page_rows, "fast": fast}


def _parse_range(dt_from: str | None, dt_to: str | None, hours: int) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    try:
        end = datetime.fromisoformat(dt_to).astimezone(timezone.utc) if dt_to else now
        start = (datetime.fromisoformat(dt_from).astimezone(timezone.utc)
                 if dt_from else end - timedelta(hours=hours))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format (use ISO 8601)")
    if start >= end:
        raise HTTPException(status_code=400, detail="'from' must be before 'to'")
    return start, end


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

@router.get("/api/admin/perf/summary")
async def perf_summary(
    actor: User = Depends(require_permission("view_audit_log")),
    hours: int = Query(24, ge=1, le=24 * 31),
):
    start, end = _parse_range(None, None, hours)
    # Threadpool: _summarize parses every JSONL in range — tens/hundreds of MB
    # after a couple of weeks. In the loop this froze the WHOLE app for the
    # duration of the read every time an admin opened the page.
    return await run_in_threadpool(_summarize, start, end)


# "sample" is appended LAST so legacy positional parsers of old exports keep
# their column indices.
_CSV_COLS = ["ts", "iso", "src", "method", "route", "path", "status", "dur_ms",
             "hdr_ms", "db_ms", "db_n", "bytes", "uid", "pid", "ip", "t", "page",
             "url", "name", "ttfb", "dcl", "load", "dur", "size", "stages", "sql",
             "sample"]

# Leading characters a spreadsheet treats as the start of a formula.
_CSV_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    """Neutralise formula injection without changing what the cell reads as."""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_LEAD):
        return "'" + value
    return value


@router.post("/api/admin/perf/toggle")
async def perf_toggle(
    actor: User = Depends(require_permission("view_audit_log")),
):
    """Pause/resume telemetry collection at runtime (persisted marker file —
    survives restarts, reaches all workers within one writer cycle)."""
    perflog.set_runtime(not perflog.runtime_on())
    return {"enabled": perflog.runtime_on()}


@router.delete("/api/admin/perf/logs")
async def perf_clear_logs(
    actor: User = Depends(require_permission("view_audit_log")),
):
    """Delete all collected JSONL (only our own files — validated by name) and
    make the writer reopen today's file so an unlinked fd doesn't keep growing."""
    def _clear():
        d = perflog.perf_dir()
        deleted = freed = 0
        if d.exists():
            for f in d.glob("perf-*.jsonl"):
                if perflog.is_perf_file(f.name):
                    try:
                        freed += f.stat().st_size
                        f.unlink()
                        deleted += 1
                    except OSError:
                        pass
        return deleted, freed

    deleted, freed = await run_in_threadpool(_clear)
    perflog.request_reopen()
    return {"deleted": deleted, "freed_mb": round(freed / 1048576, 2)}


@router.get("/api/admin/perf/export")
async def perf_export(
    actor: User = Depends(require_permission("view_audit_log")),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    hours: int = Query(24, ge=1, le=24 * 31),
    format: str = Query("jsonl", pattern="^(jsonl|csv)$"),
):
    start, end = _parse_range(date_from, date_to, hours)
    stamp = f"{start.strftime('%Y%m%d%H%M')}-{end.strftime('%Y%m%d%H%M')}"

    if format == "jsonl":
        def gen_jsonl():
            for rec in perflog.iter_records(start, end):
                yield json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        return StreamingResponse(
            gen_jsonl(), media_type="application/x-ndjson",
            # Explicit identity: GZipMiddleware would otherwise compress every
            # chunk IN the event loop (admin-only export over the internal net —
            # a loop stall costs more than the extra bytes).
            headers={"Content-Disposition": f'attachment; filename="perf-{stamp}.jsonl"',
                     "Content-Encoding": "identity"},
        )

    def gen_csv():
        # csv.writer quotes delimiters, but a spreadsheet reads a leading
        # = + - @ as a formula, and page/url/name arrive verbatim from the
        # client beacon (any authenticated user). Prefix so the cell is text.
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_CSV_COLS)
        for rec in perflog.iter_records(start, end):
            row = dict(rec)
            ts = row.get("ts")
            row["iso"] = (datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                          if isinstance(ts, (int, float)) else "")
            for k in ("stages", "sql"):
                if k in row:
                    row[k] = json.dumps(row[k], ensure_ascii=False)
            writer.writerow([_csv_safe(row.get(c, "")) for c in _CSV_COLS])
            if buf.tell() > 64 * 1024:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()

    return StreamingResponse(
        gen_csv(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="perf-{stamp}.csv"',
                 "Content-Encoding": "identity"},
    )


# ---------------------------------------------------------------------------
# Dashboard errors — group 4xx/5xx perf records by dashboard, join owner.
# 401s are excluded (auth redirects, not dashboard problems).
# ---------------------------------------------------------------------------

_DASH_ID_RE = re.compile(r"/(?:render|d|api/dashboards)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def _collect_errors(dt_from: datetime, dt_to: datetime) -> tuple[list[dict], list[dict]]:
    by_dash: dict[str, dict] = {}
    other: dict[tuple, dict] = {}
    for rec in perflog.iter_records(dt_from, dt_to):
        if rec.get("src") != "server":
            continue
        status = rec.get("status") or 0
        if status < 400 or status == 401:
            continue
        path = rec.get("path", "")
        m = _DASH_ID_RE.search(path)
        if m:
            agg = by_dash.setdefault(m.group(1), {"count": 0, "statuses": {}, "paths": {}, "last_ts": 0})
            agg["count"] += 1
            agg["statuses"][str(status)] = agg["statuses"].get(str(status), 0) + 1
            agg["paths"].setdefault(path, 0)
            agg["paths"][path] += 1
            agg["last_ts"] = max(agg["last_ts"], rec.get("ts", 0))
        else:
            key = (status, path)
            o = other.setdefault(key, {"status": status, "path": path, "count": 0, "last_ts": 0})
            o["count"] += 1
            o["last_ts"] = max(o["last_ts"], rec.get("ts", 0))
    other_rows = sorted(other.values(), key=lambda r: -r["count"])[:20]
    for o in other_rows:
        o["last"] = datetime.fromtimestamp(o["last_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    return by_dash, other_rows


@router.get("/admin/errors", response_class=HTMLResponse)
async def errors_page(
    request: Request,
    user: User = Depends(require_permission("view_admin")),
    hours: int = Query(24, ge=1, le=24 * 31),
):
    from sqlalchemy.orm import joinedload
    from app.database import SessionLocal
    from app.models import Dashboard

    start, end = _parse_range(None, None, hours)
    # Threadpool: same class of bug as perf_summary — parsing weeks of JSONL
    # in the event loop froze every in-flight request.
    by_dash, other_rows = await run_in_threadpool(_collect_errors, start, end)

    rows = []
    if by_dash:
        db = SessionLocal()
        try:
            dashes = {d.id: d for d in (
                db.query(Dashboard)
                .options(joinedload(Dashboard.owner))
                .filter(Dashboard.id.in_(by_dash.keys()))
                .all()
            )}
        finally:
            db.close()
        for did, agg in by_dash.items():
            dash = dashes.get(did)
            owner = getattr(dash, "owner", None) if dash else None
            paths = sorted(agg["paths"].items(), key=lambda kv: -kv[1])[:3]
            rows.append({
                "id": did,
                "title": dash.title if dash else "(deleted dashboard)",
                "owner": getattr(owner, "person_id", None) or "—",
                "owner_name": getattr(owner, "display_name", None) or "",
                "count": agg["count"],
                "statuses": ", ".join(f"{s}×{n}" for s, n in sorted(agg["statuses"].items())),
                "has_5xx": any(s.startswith("5") for s in agg["statuses"]),
                "paths": [f"{p}  ({n}×)" for p, n in paths],
                "last": datetime.fromtimestamp(agg["last_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            })
        rows.sort(key=lambda r: (-r["has_5xx"], -r["count"]))

    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(env.get_template("pages/errors.html").render,
        user=user, request=request, rows=rows, other=other_rows, hours=hours,
    ))


# ---------------------------------------------------------------------------
# Admin UI page
# ---------------------------------------------------------------------------

@router.get("/admin/perf", response_class=HTMLResponse)
async def perf_page(
    request: Request,
    user: User = Depends(require_permission("view_admin")),
    hours: int = Query(24, ge=1, le=24 * 31),
):
    start, end = _parse_range(None, None, hours)
    summary = await run_in_threadpool(_summarize, start, end)  # see perf_summary

    def _route_row(method: str, route: str) -> dict | None:
        return next((r for r in summary["routes"]
                     if r["method"] == method and r["route"] == route), None)

    cards = {
        "index": _route_row("GET", "/"),
        "render": _route_row("GET", "/render/{dashboard_id}/"),
        "page_index": next((p for p in summary["pages"] if p["page"] == "/"), None),
        "fast": summary["fast"],
    }
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(env.get_template("pages/perf.html").render,
        user=user, request=request, summary=summary, cards=cards, hours=hours,
        perf_enabled=settings.perf_enabled,
        telemetry_on=perflog.runtime_on(),
        # The sampler is a control loop now: the probability floats between
        # OP_PERF_SAMPLE_FLOOR and OP_PERF_SAMPLE instead of sitting where the
        # config put it. Without this readout an operator cannot tell whether
        # the counts above are exact (sampler at the ceiling) or reweighted
        # estimates, which makes prod telemetry uninterpretable.
        effective_sample=perflog.effective_sample(),
        perf_adaptive=settings.perf_adaptive,
        perf_target_rps=settings.perf_target_rps,
    ))
