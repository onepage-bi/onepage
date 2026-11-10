"""
Render-cache warmer: users should never pay a cold miss.

- warm_startup(): background daemon thread launched from the app lifespan.
  Renders + br-compresses the OP_CACHE_WARM most recently updated published
  dashboards into the render LRU (giants included — their multi-second first
  build happens here, off any request path). Warming runs OLDEST-FIRST so the
  most recent dashboards end up most-recently-used — if the byte budget forces
  evictions mid-warm, the least relevant cells go first.
- schedule(dashboard_id): debounced single-dashboard re-warm, called after
  content mutations (replace_html, snapshot/theme/text/slot/localstorage
  writes). Rapid successive saves collapse into ONE build ~2 s after the last
  one — an editing session can't spawn a build per keystroke. The fingerprint
  key has already rotated by then, so the fresh cell simply appears next to
  the dead one.

Build/write-behind logic lives in files.ensure_cell (single-flight with live
requests). Failures are swallowed: warming is best-effort and must never take
the app down or delay startup.
"""
from __future__ import annotations

import threading

from app.config import settings

_DEBOUNCE_S = 2.0
_PENDING: dict[str, threading.Timer] = {}
_PENDING_LOCK = threading.Lock()

# SWR revalidation in-flight set: at most one immediate rebuild thread per
# dashboard, so N stale-serving requests coalesce into ONE build.
_INFLIGHT: set = set()
_INFLIGHT_LOCK = threading.Lock()


def rebuild_now(dashboard_id: str) -> None:
    """Immediate (non-debounced) background rebuild for the render route's
    stale-while-revalidate path. Deduped: at most one rebuild thread per
    dashboard — N stale-serving requests spawn ONE build. NOT gated on
    settings.cache_warm: this is revalidation, not warming; without it a
    stale body would be served until LRU eviction."""
    with _INFLIGHT_LOCK:
        if dashboard_id in _INFLIGHT:
            return
        _INFLIGHT.add(dashboard_id)

    # A rebuild NOW obsoletes any pending debounce timer for this id; any
    # later mutation re-schedules its own anyway.
    with _PENDING_LOCK:
        prev = _PENDING.pop(dashboard_id, None)
        if prev is not None:
            prev.cancel()

    def _run():
        try:
            _warm_one(dashboard_id)
        except Exception:
            pass
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(dashboard_id)

    t = threading.Thread(target=_run, daemon=True, name="op-swr-rebuild")
    t.start()


def _warm_one(dashboard_id: str) -> bool:
    from app.api import files
    from app.database import SessionLocal
    from app.models import Dashboard
    from app.services import encoding, storage

    db = SessionLocal()
    try:
        dash = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
        if dash is None:
            return False
        key = (dash.id, dash.current_version,
               storage.version_fingerprint(dash.id, dash.current_version))
        cell = files.ensure_cell(dash, key)
        if cell is None:
            return False
        # Pre-encode the dominant browser encoding. Background CPU is free, so
        # warm variants use br-9 (a few % smaller wire than the request-path
        # br-5; requests just serve the ready bytes either way).
        if settings.cache_encoded and cell.get("_c", True) and "br" not in cell:
            files.render_cache.put_variant(
                key, "br", encoding.encode_bytes(cell["raw"], "br", level=9))
        return True
    finally:
        db.close()


def _startup_loop() -> None:
    from app.database import SessionLocal
    from app.models import Dashboard

    db = SessionLocal()
    try:
        recent = [row.id for row in (
            db.query(Dashboard.id)
            .filter(Dashboard.is_published == True)  # noqa: E712
            .order_by(Dashboard.updated_at.desc())
            .limit(settings.cache_warm)
            .all()
        )]
        # Cost-aware: the heaviest dashboards are the catastrophic misses
        # (multi-second build+encode), so they stay warm even when they haven't
        # been updated recently enough to make the recency list.
        biggest = [row.id for row in (
            db.query(Dashboard.id)
            .filter(Dashboard.is_published == True)  # noqa: E712
            .order_by(Dashboard.render_size.desc().nullslast())
            .limit(5)
            .all()
        )]
        ids = recent + [i for i in biggest if i not in set(recent)]
    finally:
        db.close()
    warmed = 0
    for did in reversed(ids):  # oldest first -> newest end up hottest in the LRU
        try:
            warmed += 1 if _warm_one(did) else 0
        except Exception:
            continue
    print(f"[warm] pre-rendered {warmed}/{len(ids)} dashboards into the render cache")


def warm_startup() -> None:
    if settings.cache_warm <= 0:
        return
    threading.Thread(target=_startup_loop, daemon=True, name="op-cache-warm").start()


def schedule(dashboard_id: str) -> None:
    """Debounced background re-warm of a dashboard after a content mutation."""
    if settings.cache_warm <= 0:
        return

    def _run():
        with _PENDING_LOCK:
            _PENDING.pop(dashboard_id, None)
        try:
            _warm_one(dashboard_id)
        except Exception:
            pass

    with _PENDING_LOCK:
        prev = _PENDING.pop(dashboard_id, None)
        if prev is not None:
            prev.cancel()
        timer = threading.Timer(_DEBOUNCE_S, _run)
        timer.daemon = True
        _PENDING[dashboard_id] = timer
        timer.start()
