from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from time import monotonic
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.i18n import _
from app.auth.deps import (
    get_current_user, get_current_user_optional, require_dashboard_access,
    can_update_dashboard_data, get_dashboard_or_404,
)
from app.config import settings
from app.database import get_db
from app.models import (AuditLog, Dashboard, DashboardFile, DashboardPermission,
                        ShareToken, User, DASH_PAGE, DASH_SNAPSHOT,
                        ROLE_ADMINISTRATOR)
# warm's top level only pulls settings — no import cycle with this module
# (its files.py references are deferred inside functions).
from app.services import detector, encoding, perflog, rls, standalone, storage, warm

router = APIRouter()


def _authorize_render(
    dashboard_id: str,
    share: Optional[str],
    user: Optional[User],
    db: Session,
    request: Optional[Request] = None,
) -> Dashboard:
    """Return dashboard if access is granted via session or share token; raise otherwise.

    This is the ONLY access check for the whole `/render/{id}/…` family — the
    document, snapshot.js, slot files and assets; `require_dashboard_access` is
    not involved here. So the second lock ("strictly confidential") is checked
    right here: a new /render route inherits it at once, as it already inherited
    the ban on share links for RLS.
    """
    dash = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dash:
        raise HTTPException(status_code=404, detail="Dashboard not found")

    if share:
        tok = db.query(ShareToken).filter(
            ShareToken.token == share,
            ShareToken.dashboard_id == dashboard_id,
        ).first()
        if not tok:
            raise HTTPException(status_code=403, detail="Invalid share link")
        if tok.expires_at and tok.expires_at < datetime.utcnow():
            raise HTTPException(status_code=410, detail="Share link has expired")
        # ADR-18: a share link does not know WHO is viewing — there is no one to filter for.
        # The check sits HERE, not in every route: /render/{id}/,
        # snapshot.js, slot files and assets inherit it at once, and so does any new
        # /render route (a token issued before the policy was enabled stops
        # working entirely, not halfway).
        if dash.rls_enabled:
            raise HTTPException(status_code=403,
                                detail="RLS: share links are disabled for this report")
        # An anonymous link has no one to present a password — a "strictly
        # confidential" report has no links at all.
        if dash.conf_hash:
            raise HTTPException(status_code=403,
                                detail="confidential: share links are disabled for this report")
        return dash

    if user is None:
        raise HTTPException(status_code=401)

    def _conf_ok(d: Dashboard) -> Dashboard:
        # The second lock is checked AFTER the right to view and the same for everyone,
        # owner and administrator included: otherwise "strictly confidential"
        # would mean "for everyone except the bosses".
        if d.conf_hash:
            from app.services import confidential
            from app.auth.session import get_session_id_from_cookie
            sid = (get_session_id_from_cookie(request.headers.get("cookie", ""))
                   if request is not None else None)
            confidential.assert_unlocked(db, sid, d)
        return d

    if user.role == ROLE_ADMINISTRATOR or dash.owner_id == user.id or dash.is_public:
        return _conf_ok(dash)

    perm = db.query(DashboardPermission).filter(
        DashboardPermission.dashboard_id == dashboard_id,
        DashboardPermission.user_id == user.id,
    ).first()
    if perm and dash.is_published:
        return _conf_ok(dash)

    raise HTTPException(status_code=403, detail="Access denied")


_MIME = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".json": "application/json",
    # Asset types a dashboard may reference relatively (render_asset). Correct
    # script MIME matters: Chromium's ORB can block octet-stream scripts for
    # the opaque-origin iframe.
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".txt": "text/plain; charset=utf-8",
}


def _mime(filename: str) -> str:
    for ext, ct in _MIME.items():
        if filename.lower().endswith(ext):
            return ct
    return "application/octet-stream"


# ---------------------------------------------------------------------------
# ADR-18 helpers. Both are no-ops for a dashboard without a policy: rls_json is
# already on the loaded row, so an ordinary report pays one attribute read and
# ZERO queries.
# ---------------------------------------------------------------------------

def _assert_rls_full_access(db: Session, dashboard: Dashboard, user: User) -> None:
    """403 for everything that bypasses the filter (source, slot write/delete).
    The "update data" right alone does not grant full access: a colleague with
    their own territory must not read other rows by uploading a file."""
    if not dashboard.rls_enabled:
        return
    if not rls.subject(db, dashboard, user).see_all:
        raise HTTPException(status_code=403,
                            detail="RLS: this report is filtered per viewer")


def _asset_slot_index(dashboard_id: str, version: int, asset_name: str) -> Optional[int]:
    """Index of the slot a relative asset name resolves to (as in
    serve_slot_asset — with the same .gz/.br variants), or None."""
    if ".." in asset_name or asset_name.startswith("."):
        return None
    for candidate in (asset_name, asset_name + ".gz", asset_name + ".br"):
        path = storage.slot_asset_path(dashboard_id, version, candidate)
        if path is not None:
            m = re.match(r"^slot_(\d+)_", path.name)
            if m:
                return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# List files for a dashboard (used by viewer.html JS)
# ---------------------------------------------------------------------------

@router.get("/api/dashboards/{dashboard_id}/files")
def list_files(
    dashboard: Dashboard = Depends(require_dashboard_access),
):
    slots = storage.list_slot_files(dashboard.id, dashboard.current_version)
    return slots


# ---------------------------------------------------------------------------
# Serve slot file (called by viewer.html to fetch default data)
# ---------------------------------------------------------------------------

def serve_slot_download(request: Request, dashboard_id: str, version: int,
                        slot_index: int) -> Response:
    """Serve a slot file by index (viewer/share data fetch). Shared by
    get_slot_file and share_get_slot.

    - Content-addressed by `?v=` (storage.slot_fp, handed to the page in
      slots_json). On a match the body is immutable — a repeat view costs ZERO
      requests. A bare or stale url is `no-store`: it must never be reused,
      because the url has no version in it and a publish reuses it verbatim.
      This replaced `max-age=300, must-revalidate`, which does NOT revalidate
      while the response is still fresh (RFC 9111) — it served a five-minute-
      old report after a generator run published new data.
    - Strong ETag from stat (mtime_ns + size + representation): a 304
      revalidation costs one stat, never a multi-MB read + hash.
    - Pre-compressed *.br/*.gz slots pass through with their Content-Encoding
      (decompressed to identity for a client that can't decode it).
    - Raw slots go through negotiated compression OFF the event loop —
      GZipMiddleware would otherwise gzip multi-MB bodies in-loop, freezing
      every concurrent request."""
    path = storage.slot_file_path(dashboard_id, version, slot_index)
    if path is None:
        raise HTTPException(status_code=404, detail="No file for this slot")
    filename = path.name[len(f"slot_{slot_index}_"):]
    st = path.stat()
    lower = filename.lower()
    pre = "br" if lower.endswith(".br") else ("gzip" if lower.endswith(".gz") else None)
    accept_encoding = request.headers.get("accept-encoding", "")

    if pre is not None:
        rep = pre if pre in encoding.accepted_encodings(accept_encoding) else "dec"
    else:
        # Deterministic for (file, Accept-Encoding): the probe verdict is a pure
        # function of content, so the etag uniquely identifies the response.
        rep = encoding.negotiate(accept_encoding)
    fp = storage.slot_fp(st)
    etag = f'"{fp}-{rep}"'
    cache_headers = {
        "ETag": etag,
        "Cache-Control": ("private, max-age=31536000, immutable"
                          if request.query_params.get("v") == fp
                          else "no-store, must-revalidate"),
        "Vary": "Accept-Encoding",
    }
    inm = request.headers.get("if-none-match")
    if inm and etag in [t.strip() for t in inm.split(",")]:
        return Response(status_code=304, headers=cache_headers)

    headers = dict(cache_headers)
    headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"

    # Encoded slot variants live in the render LRU, keyed by the SAME stat the
    # ETag uses (in-place replacement = new key = rotation invalidation, like
    # render cells). A repeat download of a big raw slot then costs zero encode
    # CPU and zero disk read — before this, every open of an interactive
    # dashboard re-compressed its multi-MB data file (~255 ms measured).
    slot_key = (dashboard_id, "slot", slot_index, st.st_mtime_ns, st.st_size)
    if pre is None and settings.cache_encoded and rep != "identity":
        cell = render_cache.get(slot_key)
        if cell is not None:
            payload = cell.get(rep)
            if payload is not None:
                headers["Content-Encoding"] = rep
                return Response(content=payload, media_type=_mime(filename), headers=headers)

    content = path.read_bytes()

    if pre is not None:
        media_filename = filename[:-3]
        if rep == "dec":
            try:
                content = encoding.decode_bytes(content, pre)
            except encoding.DecodedTooLarge:
                raise HTTPException(status_code=413,
                                    detail="Stored file expands beyond the serving limit")
        else:
            headers["Content-Encoding"] = pre
        return Response(content=content, media_type=_mime(media_filename), headers=headers)

    on_complete = (
        (lambda e, payload: render_cache.put_standalone_variant(slot_key, e, payload))
        if settings.cache_encoded else None
    )
    return encoding.encoded_response(
        content, accept_encoding,
        media_type=_mime(filename), headers=headers, enc=rep,
        on_complete=on_complete,
    )


@router.get("/render/{dashboard_id}/files/{slot_index}")
def get_slot_file(
    request: Request,
    dashboard_id: str,
    slot_index: int,
    share: Optional[str] = Query(None),
    user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    dashboard = _authorize_render(dashboard_id, share, user, db, request)
    # ADR-18: the data serving point. For an ordinary report (rls_json NULL) nothing
    # changes — not a single extra query and the same serve_slot_download.
    if dashboard.rls_enabled:
        # share is impossible here already — _authorize_render refused above.
        subj = rls.subject(db, dashboard, user)
        if not subj.see_all:
            return rls.serve_filtered(request, db, dashboard, slot_index, subj)
        # A see_all subject's full file must NOT land in the browser cache as
        # immutable: the URL is the same for all viewers, and an immutable response
        # is served without revalidation — the next user of the same browser (shared
        # computer, account switch) would get someone else's rows. no-cache =
        # ETag revalidation on every open (304 for the same file).
        resp = serve_slot_download(request, dashboard.id, dashboard.current_version, slot_index)
        if not rls.is_passthrough(rls.policy_of(dashboard), slot_index):
            resp.headers["Cache-Control"] = "private, no-cache"
            resp.headers["Vary"] = "Accept-Encoding, Cookie"
        return resp
    return serve_slot_download(request, dashboard.id, dashboard.current_version, slot_index)


# ---------------------------------------------------------------------------
# Update slot file (called by viewer.html when visitor uploads their own file)
# ---------------------------------------------------------------------------

@router.put("/api/dashboards/{dashboard_id}/files/{slot_index}")
async def update_slot_file(
    slot_index: int,
    file: UploadFile = File(...),
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    allowed = can_update_dashboard_data(dashboard=dashboard, user=user, db=db)
    if not allowed:
        raise HTTPException(status_code=403, detail="No permission to update data")
    _assert_rls_full_access(db, dashboard, user)

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    stored = storage.save_slot_file(
        dashboard.id, dashboard.current_version,
        slot_index, file.filename, content,
    )

    # Upsert DB record
    existing = db.query(DashboardFile).filter(
        DashboardFile.dashboard_id == dashboard.id,
        DashboardFile.version_number == dashboard.current_version,
        DashboardFile.slot_index == slot_index,
    ).first()
    if existing:
        existing.original_name = file.filename
        existing.stored_path = stored
        existing.size_bytes = len(content)
    else:
        db.add(DashboardFile(
            dashboard_id=dashboard.id,
            version_number=dashboard.current_version,
            slot_index=slot_index,
            original_name=file.filename,
            stored_path=stored,
            size_bytes=len(content),
        ))
    db.commit()
    warm.schedule(dashboard.id)  # slot files are part of the render fingerprint
    return {"ok": True}


# ---------------------------------------------------------------------------
# Update snapshot JSON
# ---------------------------------------------------------------------------

@router.put("/api/dashboards/{dashboard_id}/snapshot")
async def update_snapshot(
    file: UploadFile = File(...),
    dashboard: Dashboard = Depends(get_dashboard_or_404),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if dashboard.dash_type != DASH_SNAPSHOT:
        raise HTTPException(status_code=400, detail="Dashboard is not a snapshot type")

    allowed = can_update_dashboard_data(dashboard=dashboard, user=user, db=db)
    if not allowed:
        raise HTTPException(status_code=403, detail="No permission to update data")
    if dashboard.rls_enabled:
        # The snapshot is shared by all viewers — an RLS report has nowhere to get one
        # (data lives only in slots); see build_render_body.
        raise HTTPException(status_code=409,
                            detail=_("RLS: snapshot not stored for reports with a scope policy"))

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    # Validate JSON
    try:
        json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    storage.save_snapshot_json(dashboard.id, dashboard.current_version, content)
    dashboard.render_size = storage.estimate_render_size(dashboard.id, dashboard.current_version)
    db.commit()
    warm.schedule(dashboard.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Serve the rendered HTML (with injection)
# ---------------------------------------------------------------------------

# The fully-injected render output is identical for every viewer (annotate/
# theme/text controllers are dormant per-user, seeded per-dashboard), so it is
# cached in-process. A cache CELL holds the raw body plus lazily-filled
# compressed variants: {"raw": bytes, "br": bytes, "zstd": ..., "gzip": ...,
# "_c": bool} ("_c" = compressibility probe result). One key = one content
# generation — variants derive from the raw of the SAME cell, so a
# stale-variant desync is structurally impossible; invalidation is key
# rotation (the key embeds version_fingerprint mtimes), not deletion.
#
# Warm hits are served straight from the event loop (no threadpool hop, no
# compression) — the fast path costs auth + dict lookup + send.

# Cell-count cap is a sanity bound; the byte budget is the real guard. It must
# clear warm list (cache_warm + 5 giants) + share cells or warming self-evicts.
_RENDER_CACHE_MAX = max(64, 2 * settings.cache_warm + 16)
_RENDER_CACHE_MAX_BYTES = settings.cache_max_mb * 1024 * 1024  # raw + variants

# Fingerprint TTL micro-cache: the 6 stat() calls behind version_fingerprint
# cost ~180 µs on a Docker bind mount — a 1.0 s TTL keeps them off the hot path,
# trading 6× fewer stat bursts for up to 1.0 s staleness. Staleness no longer
# causes cold build: a fingerprint-lag miss serves the previous cell instantly
# via stale-while-revalidate (SWR branch in render_dashboard) while rebuild
# runs off-path; the 2 s re-warm debounce bounds content staleness.
_FP_TTL = 1.0
_FP_CACHE: dict[tuple, tuple] = {}  # (id, version) -> (monotonic deadline, fp)
_FP_LOCK = threading.Lock()


def fingerprint_cached(dashboard_id: str, version: int) -> tuple:
    key = (dashboard_id, version)
    now = monotonic()
    hit = _FP_CACHE.get(key)  # GIL-atomic read; the lock only guards writers
    if hit is not None and hit[0] > now:
        return hit[1]
    fp = storage.version_fingerprint(dashboard_id, version)
    with _FP_LOCK:
        if len(_FP_CACHE) > 512:  # entries rotate with versions — drop expired
            for stale in [k for k, (dl, _) in _FP_CACHE.items() if dl <= now]:
                del _FP_CACHE[stale]
        _FP_CACHE[key] = (now + _FP_TTL, fp)
    return fp


def _cell_size(cell: dict) -> int:
    return sum(len(v) for v in cell.values() if isinstance(v, bytes))


class _RenderCache:
    """Byte-budgeted LRU of render cells (module-level singleton below).
    Self-contained: cells, lock, and byte accounting live on the instance."""

    def __init__(self) -> None:
        self._cells: "OrderedDict[tuple, dict]" = OrderedDict()
        self._lock = threading.Lock()
        self.bytes = 0

    def get(self, key: tuple) -> Optional[dict]:
        with self._lock:
            cell = self._cells.get(key)
            if cell is not None:
                self._cells.move_to_end(key)
            return cell

    def put_raw(self, key: tuple, body: bytes, compressible: bool) -> None:
        with self._lock:
            old = self._cells.pop(key, None)
            if old is not None:
                self.bytes -= _cell_size(old)
            self._cells[key] = {"raw": body, "_c": compressible}
            self.bytes += len(body)
            self._evict_locked()

    def put_variant(self, key: tuple, enc: str, payload: bytes) -> None:
        with self._lock:
            cell = self._cells.get(key)
            if cell is None or enc in cell:
                return  # cell evicted meanwhile / variant raced in — drop
            cell[enc] = payload
            self._cells.move_to_end(key)
            self.bytes += len(payload)
            self._evict_locked()

    def put_standalone_variant(self, key: tuple, enc: str, payload: bytes) -> None:
        """Variant-only cell (slot downloads): the raw bytes stay on disk, only
        the encoded payload is worth keeping in RAM — get-or-create the cell."""
        with self._lock:
            cell = self._cells.get(key)
            if cell is None:
                cell = self._cells[key] = {"raw": b"", "_c": True}
            if enc in cell:
                return
            cell[enc] = payload
            self._cells.move_to_end(key)
            self.bytes += len(payload)
            self._evict_locked()

    def _evict_locked(self) -> None:
        while self._cells and (
            len(self._cells) > _RENDER_CACHE_MAX
            or self.bytes > _RENDER_CACHE_MAX_BYTES
        ):
            _, evicted = self._cells.popitem(last=False)
            self.bytes -= _cell_size(evicted)


render_cache = _RenderCache()

# render_size write-behind: the exact preview weight is a best-effort UI hint,
# so it must never cost a SQLite COMMIT on the serving path (worse: inside
# build_lock). Dedup: only the LATEST weight per dashboard matters.
_RS_PENDING: dict = {}          # dashboard_id -> latest weight
_RS_LOCK = threading.Lock()
_RS_WAKE = threading.Event()
_RS_STARTED = False


def _queue_render_size(dashboard_id: str, weight: int) -> None:
    """Record the latest exact preview weight for a background writer to persist.
    Lazily starts the daemon writer on first use. Coalescing: repeated queues for
    the same dashboard keep only the newest weight."""
    global _RS_STARTED
    with _RS_LOCK:
        _RS_PENDING[dashboard_id] = weight
        if not _RS_STARTED:
            threading.Thread(target=_render_size_writer, daemon=True,
                             name="op-render-size-writer").start()
            _RS_STARTED = True
    _RS_WAKE.set()


def _render_size_writer() -> None:
    """Daemon: persist queued render_size values off the serving path. Each write
    is its own COMMIT on the shared engine; failures are swallowed (a later build
    re-queues, since ensure_cell's guard compares against the DB-loaded value)."""
    from sqlalchemy import text
    from app.database import engine
    while True:
        _RS_WAKE.wait()
        _RS_WAKE.clear()
        while True:
            with _RS_LOCK:
                if not _RS_PENDING:
                    break
                dashboard_id, weight = _RS_PENDING.popitem()
            try:
                with engine.connect() as conn:
                    conn.execute(
                        text("UPDATE dashboards SET render_size = :w WHERE id = :id"),
                        {"w": weight, "id": dashboard_id},
                    )
                    conn.commit()
            except Exception:
                pass  # best-effort: a later build re-queues via the != guard


# SWR: last successfully built render cell key per dashboard. On a miss for
# the current key, that cell (if still in the LRU) is served immediately while
# warm.rebuild_now() rebuilds the current key in the background. Values are
# key tuples only — never cells — so this map cannot pin body bytes.
_LAST_CELL_KEY: "OrderedDict[str, tuple]" = OrderedDict()
_LAST_CELL_LOCK = threading.Lock()
_LAST_CELL_MAX = 4096


def note_built(dashboard_id: str, key: tuple) -> None:
    """Record the freshly built cell key as the newest good render for this
    dashboard (recency-ordered, FIFO-pruned over the cap). Called from
    ensure_cell, so every request-path build, warm_startup pre-render, and
    debounced re-warm populates the SWR map."""
    with _LAST_CELL_LOCK:
        _LAST_CELL_KEY.pop(dashboard_id, None)
        _LAST_CELL_KEY[dashboard_id] = key
        while len(_LAST_CELL_KEY) > _LAST_CELL_MAX:
            _LAST_CELL_KEY.popitem(last=False)


def last_cell_key(dashboard_id: str) -> Optional[tuple]:
    """The last good render key for this dashboard, or None. Plain dict .get()
    (GIL-atomic) — safe to call in-loop without the lock."""
    return _LAST_CELL_KEY.get(dashboard_id)


# Single-flight: concurrent cold requests for the same key build the body once;
# the rest block on the per-key lock (in the threadpool, never the event loop)
# and pick the cell up from the cache. Entries are refcounted — an entry lives
# exactly as long as it has holders, so the registry stays minimal and a lock
# can never be pruned out from under a thread it was just handed to.
_BUILD_LOCKS: dict[tuple, list] = {}  # key -> [threading.Lock, holder_count]
_BUILD_LOCKS_GUARD = threading.Lock()


@contextmanager
def build_lock(key: tuple):
    with _BUILD_LOCKS_GUARD:
        entry = _BUILD_LOCKS.get(key)
        if entry is None:
            entry = _BUILD_LOCKS[key] = [threading.Lock(), 0]
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _BUILD_LOCKS_GUARD:
            entry[1] -= 1
            if entry[1] == 0:
                _BUILD_LOCKS.pop(key, None)


_RENDER_HEADERS = {"Cache-Control": "no-store, must-revalidate"}
_RENDER_MEDIA = "text/html; charset=utf-8"


def build_render_body(dashboard: Dashboard, *,
                      snapshot_external: bool | None = None) -> tuple[bytes, int]:
    """Read + inject the full render document (blocking; call off-loop).
    Returns (body, extern_extra) where extern_extra is the snapshot weight
    shipped separately via snapshot.js when OP_SNAPSHOT_EXTERNAL is on.

    `snapshot_external=False` forces the snapshot to be embedded in the document,
    whatever the server is configured for: the single-file export asks for it
    (`services/standalone.py`) — an external `snapshot.js` lives at a relative
    address that a file on disk does not have. The parameter changes nothing else,
    and the hot path (None → setting) pays nothing for it."""
    with perflog.span("read_html"):
        html_bytes = storage.read_html(dashboard.id, dashboard.current_version)

    html = html_bytes.decode("utf-8", errors="replace")
    fingerprint = storage.version_fingerprint(dashboard.id, dashboard.current_version)

    extern_extra = 0
    with perflog.span("injects"):
        if dashboard.dash_type == DASH_SNAPSHOT:
            # ADR-18: a snapshot-type RLS report is an empty stub with data in
            # slots; the saved snapshot.json is never injected into it (one
            # file for all viewers = handing out rows past the filter).
            snap = (None if dashboard.rls_enabled
                    else storage.read_snapshot_json(dashboard.id, dashboard.current_version))
            if snap:
                externalized = False
                want_extern = (settings.snapshot_external if snapshot_external is None
                               else snapshot_external)
                if want_extern:
                    # Relative URL: resolves against /render/{id}/ (and the
                    # share variant) — the fingerprint in ?v makes it immutable.
                    html, externalized = detector.externalize_snapshot(
                        html, f"snapshot.js?v={snapshot_fp12(fingerprint)}")
                if externalized:
                    extern_extra = len(snap)
                else:
                    html = detector.inject_snapshot(html, snap.decode("utf-8"))
            html = detector.inject_height_reporter(html)

        elif dashboard.dash_type == "interactive":
            slots = storage.list_slot_files(dashboard.id, dashboard.current_version)
            slots_json = json.dumps(slots)
            html = detector.inject_bootstrap(html, slots_json)
            # ADR-18: the saved localStorage is ONE per dashboard, and under RLS
            # it would have been written by a full-access viewer (selections,
            # filters and drill-downs settle in it). Inlining it for everyone hands out data in
            # the very HTML the ADR promises "contains no data".
            if not dashboard.rls_enabled:
                ls_data = storage.read_ls_json(dashboard.id, dashboard.current_version)
                if ls_data:
                    html = detector.inject_ls_state(html, ls_data.decode("utf-8"))

        elif dashboard.dash_type == DASH_PAGE:
            # Navigator: height as for static PLUS a link interceptor —
            # without it any link on the page opens a sandboxed tab
            # with no cookie, i.e. a blank screen on every entry.
            html = detector.inject_height_reporter(html)
            html = detector.inject_link_bridge(html)

        else:
            html = detector.inject_height_reporter(html)

        # Dormant annotate controller for every type; activated only by owner/admin viewer.
        html = detector.inject_annotate(html)

    with perflog.span("theme_text"):
        # Saved colour theme (Colors feature) — applied for every viewer at render.
        theme_bytes = storage.read_theme_json(dashboard.id, dashboard.current_version)
        theme_json = theme_bytes.decode("utf-8") if theme_bytes else "null"
        html = detector.inject_theme(html, theme_json)

        # Saved static-text overrides (Edit text feature) — applied for every viewer.
        text_bytes = storage.read_text_json(dashboard.id, dashboard.current_version)
        text_map_json = "null"
        if text_bytes:
            try:
                text_map_json = json.dumps(json.loads(text_bytes).get("edits") or {}, ensure_ascii=False)
            except (json.JSONDecodeError, AttributeError):
                text_map_json = "null"
        html = detector.inject_textedit(html, text_map_json)

    return html.encode("utf-8"), extern_extra


class _PreparedResponse:
    """Zero-build ASGI responder for a cached variant: status/headers/body are
    computed ONCE per (cell, encoding); a warm hit costs two send() calls. The
    headers list is copied per send because outer middleware (Server-Timing)
    appends to it in place; the body bytes are shared with the cell."""
    __slots__ = ("headers", "body")

    def __init__(self, headers: list, body: bytes):
        self.headers = headers
        self.body = body

    async def __call__(self, scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": list(self.headers)})
        await send({"type": "http.response.body", "body": self.body})


def prepared_from_cell(cell: dict, enc: str) -> Optional[_PreparedResponse]:
    """Prepared messages for a ready variant (the fast lane's serve path).
    None when the variant isn't encoded yet — caller falls through to the
    full route. Header set mirrors _respond_from_cell exactly."""
    if enc != "identity" and not cell.get("_c", True):
        enc = "identity"
    prep = cell.get("_p")
    if prep is None:
        prep = cell["_p"] = {}  # benign race under the GIL: one builder wins
    p = prep.get(enc)
    if p is not None:
        return p
    body = cell["raw"] if enc == "identity" else cell.get(enc)
    if body is None:
        return None
    headers = [
        (b"content-length", str(len(body)).encode()),
        (b"content-type", b"text/html; charset=utf-8"),
        (b"cache-control", b"no-store, must-revalidate"),
        (b"content-encoding", enc.encode()),
        (b"vary", b"Accept-Encoding"),
    ]
    p = _PreparedResponse(headers, body)
    prep[enc] = p
    return p


def _respond_from_cell(cell: dict, key: tuple, enc: str) -> Optional[Response]:
    """Zero-CPU fast path: serve a ready variant (or raw as identity).
    Returns None when the requested variant still needs to be encoded."""
    if enc != "identity" and not cell.get("_c", True):
        enc = "identity"  # probe (computed once at build) said: don't compress
    if enc == "identity":
        return Response(content=cell["raw"], media_type=_RENDER_MEDIA,
                        headers=encoding.enc_headers("identity", _RENDER_HEADERS))
    payload = cell.get(enc)
    if payload is None:
        return None
    return Response(content=payload, media_type=_RENDER_MEDIA,
                    headers=encoding.enc_headers(enc, _RENDER_HEADERS))


def _encode_and_respond(cell: dict, key: tuple, accept_encoding: str,
                        enc: Optional[str] = None) -> Response:
    """Blocking (threadpool): first-time encode of a cached raw body.
    The variant lands in the cell only after the response fully streams."""
    on_complete = (
        (lambda e, payload: render_cache.put_variant(key, e, payload))
        if settings.cache_encoded else None
    )
    return encoding.encoded_response(
        cell["raw"], accept_encoding,
        media_type=_RENDER_MEDIA, headers=dict(_RENDER_HEADERS),
        on_complete=on_complete,
        enc=enc, compressible=cell.get("_c", True),
    )


def ensure_cell(dashboard: Dashboard, key: tuple) -> Optional[dict]:
    """Blocking (threadpool / warm thread): return the cell for key, building it
    under the per-key single-flight lock if missing. Returns None only when the
    dashboard's HTML file is gone. Shared by the render route and warm.py so the
    build sequence exists exactly once."""
    cell = render_cache.get(key)
    if cell is not None:
        return cell
    with build_lock(key):
        cell = render_cache.get(key)
        if cell is not None:
            # Another request built this key while we waited on the lock.
            rec = perflog.request_record.get()
            if rec is not None:
                rec.setdefault("stages", {})["coalesced"] = 1
            return cell
        try:
            body, extern_extra = build_render_body(dashboard)
        except FileNotFoundError:
            return None

        compressible = encoding.is_compressible(body)
        render_cache.put_raw(key, body, compressible)
        note_built(dashboard.id, key)  # SWR: newest good render for this dashboard

        # Exact preview-weight write-behind: queued to a daemon writer so the
        # serving path (and build_lock holders) never pay a SQLite COMMIT.
        weight = len(body) + extern_extra
        if dashboard.render_size != weight:
            _queue_render_size(dashboard.id, weight)

        return render_cache.get(key) or {"raw": body, "_c": compressible}


def _build_encode_respond(dashboard: Dashboard, key: tuple, accept_encoding: str,
                          enc: Optional[str] = None) -> Response:
    """Blocking (threadpool): full miss — build under single-flight, then encode
    outside the lock so waiters compress their variants in parallel."""
    cell = ensure_cell(dashboard, key)
    if cell is None:
        raise HTTPException(status_code=404, detail="Dashboard file not found")
    return _encode_and_respond(cell, key, accept_encoding, enc)


def respond_from_cache(cell: dict, key: tuple, accept_encoding: str) -> Response:
    """Serve a cell honoring Accept-Encoding: ready variant if cached (zero
    CPU), else encode now (and store the variant). For sync/threadpool callers
    (share_render); the async render route splits these steps itself."""
    enc = encoding.negotiate(accept_encoding)
    if settings.cache_encoded:
        resp = _respond_from_cell(cell, key, enc)
        if resp is not None:
            return resp
    return _encode_and_respond(cell, key, accept_encoding, enc)


# Trailing slash is canonical: the iframe document lives at /render/{id}/, so
# RELATIVE asset URLs inside a dashboard resolve to /render/{id}/{asset} and
# are served by render_asset below from same-named slot files. Old bookmarks
# (/render/{id}) get a 307 from Starlette's redirect_slashes.
#
# Async on purpose: auth is two sub-ms SQLite PK reads (same in-loop pattern
# as the index route), and warm hits return without ever touching the
# threadpool. Only misses hop to the threadpool for build/encode.
@router.get("/render/{dashboard_id}/")
async def render_dashboard(
    request: Request,
    dashboard_id: str,
    share: Optional[str] = Query(None),
    user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    dashboard = _authorize_render(dashboard_id, share, user, db, request)

    accept_encoding = request.headers.get("accept-encoding", "")
    enc = encoding.negotiate(accept_encoding)
    key = (dashboard.id, dashboard.current_version,
           fingerprint_cached(dashboard.id, dashboard.current_version))

    cell = render_cache.get(key)
    if cell is not None:
        rec = perflog.request_record.get()
        if rec is not None:
            rec.setdefault("stages", {})["cache_hit"] = 1
        if settings.cache_encoded:
            resp = _respond_from_cell(cell, key, enc)
            if resp is not None:
                return resp
        # Variant not cached yet (or encoded cache disabled) → encode off-loop.
        return await run_in_threadpool(_encode_and_respond, cell, key, accept_encoding, enc)

    # MISS. Stale-while-revalidate: if the last successfully built cell for
    # this dashboard is still in the LRU (under a rotated fingerprint or a
    # pre-bump version), serve it now and rebuild the current key off-path.
    stale_key = last_cell_key(dashboard.id)
    if stale_key is not None and stale_key != key:
        stale_cell = render_cache.get(stale_key)
        if stale_cell is not None:
            warm.rebuild_now(dashboard.id)          # non-blocking, deduped
            rec = perflog.request_record.get()
            if rec is not None:
                rec.setdefault("stages", {})["swr"] = 1
            if settings.cache_encoded:
                resp = _respond_from_cell(stale_cell, stale_key, enc)
                if resp is not None:
                    return resp                     # in-loop, zero-CPU
            return await run_in_threadpool(
                _encode_and_respond, stale_cell, stale_key, accept_encoding, enc)

    return await run_in_threadpool(_build_encode_respond, dashboard, key, accept_encoding, enc)


# ---------------------------------------------------------------------------
# Raw HTML source (for the Annotate "include full source" prompt) — owner/admin
# ---------------------------------------------------------------------------

@router.get("/api/dashboards/{dashboard_id}/source")
def get_dashboard_source(
    request: Request,
    dashboard_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dashboard = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dashboard:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    if not can_update_dashboard_data(dashboard, user, db):
        raise HTTPException(status_code=403, detail="Only owner, admin or a data updater can read source")
    # The source of an RLS report holds no data, but it is a bypass map
    # (slot-file and asset names) — served only to a subject with see_all.
    _assert_rls_full_access(db, dashboard, user)
    # The "update data" right is not a password: a maintainer also passes
    # the second lock, otherwise they would bypass it through the source.
    from app.services import confidential
    confidential.assert_unlocked_request(db, request, dashboard)
    try:
        html_bytes = storage.read_html(dashboard.id, dashboard.current_version)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Dashboard file not found")
    # Raw, un-injected source — exactly what the user pastes into an AI.
    # Negotiated compression OFF the event loop: a giant source would otherwise
    # be gzipped in-loop by GZipMiddleware (measured 1.3 s freeze on a 26 MB
    # dashboard — every concurrent request stalled behind it).
    return encoding.encoded_response(
        html_bytes, request.headers.get("accept-encoding", ""),
        media_type="text/plain; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# The report as one file with its data — owner/admin/data updater
# ---------------------------------------------------------------------------

@router.get("/api/dashboards/{dashboard_id}/standalone")
def get_dashboard_standalone(
    request: Request,
    dashboard_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The `/render/{id}/` document with slots embedded: opens with a double
    click, with no platform and no network (`services/standalone.py`).

    Nothing goes in here that the report's viewer cannot download anyway:
    they see the document already, the slots live under `/render/{id}/files/{slot}`.
    What is new here is not access but form: one file instead of the platform. Hence
    the right is the same as for the source (`/source`), not wider.

    An RLS report (ADR-18) is NEVER exported, not even to an owner with
    `see_all`. The reason is not this person's rights but what results: an
    artifact, a file with rows WITHOUT the filter that lives on by itself and
    gets copied to anyone. For exactly the same reason such a report does not
    go into the desktop mirror either (`docs/RFC_DESKTOP_MIRROR.md`). A single
    slot can still be downloaded one at a time if needed.
    """
    dashboard = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dashboard:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    if not can_update_dashboard_data(dashboard, user, db):
        raise HTTPException(status_code=403,
                            detail="Only owner, admin or a data updater can export")
    if dashboard.rls_enabled:
        raise HTTPException(status_code=409, detail=_(
            'A report with row-level security cannot be exported as one file: its rows are filtered per viewer at serve time, and a file carries no filter.'))
    if dashboard.confidential:
        raise HTTPException(status_code=409, detail=_(
            'A highly confidential report cannot be exported as a single file: that file opens without the password and then lives on its own.'))

    try:
        body, _extern = build_render_body(dashboard, snapshot_external=False)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Dashboard file not found")

    doc, notes = standalone.build(
        dashboard, body,
        t_no_gzip=_('This browser is too old: no DecompressionStream.'),
        t_failed=_('The data embedded in this file did not load: '),
    )
    if len(doc) > standalone.MAX_BYTES:
        raise HTTPException(status_code=413, detail=_(
            'The report with its data would take {p1} MB, over the {p2} MB limit.',
            p1=round(len(doc) / 1048576), p2=round(standalone.MAX_BYTES / 1048576)))

    entry = AuditLog(actor_id=user.id, action="dashboard_standalone_export",
                     resource_type="dashboard", resource_id=dashboard.id,
                     ip_address=request.client.host if request.client else "")
    # The detail carries only the size and the list of slots (index, name, length) —
    # not a single value from the data, per the audit rule.
    entry.set_detail({"version": dashboard.current_version, "bytes": len(doc),
                      "slots": notes})
    db.add(entry)
    db.commit()

    filename = f"{dashboard.slug}-v{dashboard.current_version}-data.html"
    return encoding.encoded_response(
        doc, request.headers.get("accept-encoding", ""),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


# ---------------------------------------------------------------------------
# Colour theme (Colors feature) — owner/admin. Stored per-version, applied for
# every viewer at render time. All values are validated as colours so the
# injected <style>/seed cannot carry a CSS/script injection.
# ---------------------------------------------------------------------------

_COLOR_RE = re.compile(
    r'^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$'
    r'|^rgba?\([\d.,%\s/]+\)$'
    r'|^hsla?\([\d.,%\s/]+\)$'
)
_VARNAME_RE = re.compile(r'^--[\w-]{1,80}$')
_MAX_REMAP = 400
_MAX_VARS = 200


def _is_color(v) -> bool:
    return isinstance(v, str) and bool(_COLOR_RE.match(v.strip()))


def _sanitize_theme(obj: dict) -> dict:
    """Keep only valid colour overrides; drop anything else (no raw strings reach CSS)."""
    if not isinstance(obj, dict):
        raise HTTPException(status_code=400, detail="Theme must be an object")
    vars_in = obj.get("vars") or {}
    remap_in = obj.get("remap") or {}
    if not isinstance(vars_in, dict) or not isinstance(remap_in, dict):
        raise HTTPException(status_code=400, detail="vars/remap must be objects")

    vars_out = {}
    for name, val in list(vars_in.items())[:_MAX_VARS]:
        if _VARNAME_RE.match(str(name)) and _is_color(val):
            vars_out[str(name)] = val.strip()

    remap_out = {}
    for old, new in list(remap_in.items())[:_MAX_REMAP]:
        if _is_color(old) and _is_color(new):
            remap_out[old.strip()] = new.strip()

    preset = obj.get("preset")
    if preset is not None and not (isinstance(preset, str) and re.match(r'^[\w-]{1,40}$', preset)):
        preset = None

    return {"vars": vars_out, "remap": remap_out, "preset": preset}


def _theme_owner_or_404(dashboard_id: str, user: User, db: Session) -> Dashboard:
    dashboard = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dashboard:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    if not can_update_dashboard_data(dashboard, user, db):
        raise HTTPException(status_code=403, detail="Only owner, admin or a data updater can edit the theme")
    return dashboard


@router.get("/api/dashboards/{dashboard_id}/theme")
async def get_dashboard_theme(
    dashboard_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dashboard = _theme_owner_or_404(dashboard_id, user, db)
    data = storage.read_theme_json(dashboard.id, dashboard.current_version)
    if not data:
        return {"vars": {}, "remap": {}, "preset": None}
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return {"vars": {}, "remap": {}, "preset": None}


@router.put("/api/dashboards/{dashboard_id}/theme")
async def put_dashboard_theme(
    dashboard_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dashboard = _theme_owner_or_404(dashboard_id, user, db)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    clean = _sanitize_theme(body)
    # If nothing valid remains, treat as a reset (remove the file) so render stays untouched.
    if not clean["vars"] and not clean["remap"]:
        storage.delete_theme_json(dashboard.id, dashboard.current_version)
        warm.schedule(dashboard.id)
        return {"ok": True, "empty": True}
    content = json.dumps(clean, ensure_ascii=False).encode("utf-8")
    storage.save_theme_json(dashboard.id, dashboard.current_version, content)
    warm.schedule(dashboard.id)
    return {"ok": True, "vars": len(clean["vars"]), "remap": len(clean["remap"])}


@router.delete("/api/dashboards/{dashboard_id}/theme")
async def delete_dashboard_theme(
    dashboard_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dashboard = _theme_owner_or_404(dashboard_id, user, db)
    storage.delete_theme_json(dashboard.id, dashboard.current_version)
    warm.schedule(dashboard.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Static-text overrides (Edit text feature) — owner/admin. Stored per-version,
# applied for every viewer via textContent (never parsed as HTML).
# ---------------------------------------------------------------------------

_MAX_TEXT_EDITS = 500
_MAX_TEXT_LEN = 5000
_MAX_SELECTOR_LEN = 400


def _sanitize_text(obj: dict) -> dict:
    if not isinstance(obj, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")
    edits_in = obj.get("edits") or {}
    if not isinstance(edits_in, dict):
        raise HTTPException(status_code=400, detail="edits must be an object")
    out = {}
    for sel, txt in list(edits_in.items())[:_MAX_TEXT_EDITS]:
        if not isinstance(sel, str) or not isinstance(txt, str):
            continue
        if not sel.strip() or len(sel) > _MAX_SELECTOR_LEN or len(txt) > _MAX_TEXT_LEN:
            continue
        out[sel] = txt
    return {"edits": out}


@router.get("/api/dashboards/{dashboard_id}/text")
async def get_dashboard_text(
    dashboard_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dashboard = _theme_owner_or_404(dashboard_id, user, db)
    data = storage.read_text_json(dashboard.id, dashboard.current_version)
    if not data:
        return {"edits": {}}
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return {"edits": {}}


@router.put("/api/dashboards/{dashboard_id}/text")
async def put_dashboard_text(
    dashboard_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dashboard = _theme_owner_or_404(dashboard_id, user, db)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    clean = _sanitize_text(body)
    if not clean["edits"]:
        storage.delete_text_json(dashboard.id, dashboard.current_version)
        warm.schedule(dashboard.id)
        return {"ok": True, "empty": True}
    content = json.dumps(clean, ensure_ascii=False).encode("utf-8")
    storage.save_text_json(dashboard.id, dashboard.current_version, content)
    warm.schedule(dashboard.id)
    return {"ok": True, "edits": len(clean["edits"])}


@router.delete("/api/dashboards/{dashboard_id}/text")
async def delete_dashboard_text(
    dashboard_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dashboard = _theme_owner_or_404(dashboard_id, user, db)
    storage.delete_text_json(dashboard.id, dashboard.current_version)
    warm.schedule(dashboard.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Delete slot file
# ---------------------------------------------------------------------------

@router.delete("/api/dashboards/{dashboard_id}/files/{slot_index}")
async def delete_slot_file(
    dashboard_id: str,
    slot_index: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dashboard = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dashboard:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    if not can_update_dashboard_data(dashboard, user, db):
        raise HTTPException(status_code=403, detail="Only owner or admin can delete slot files")
    _assert_rls_full_access(db, dashboard, user)

    storage.delete_slot_file(dashboard.id, dashboard.current_version, slot_index)

    db.query(DashboardFile).filter(
        DashboardFile.dashboard_id == dashboard.id,
        DashboardFile.version_number == dashboard.current_version,
        DashboardFile.slot_index == slot_index,
    ).delete()
    db.commit()
    warm.schedule(dashboard.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Content-addressed snapshot data (OP_SNAPSHOT_EXTERNAL). The render document
# references snapshot.js?v={fp12}; the fingerprint changes whenever the data
# changes, so a matching v is immutable-cacheable with zero staleness risk.
# Registered BEFORE the {asset_name} catch-all below.
# ---------------------------------------------------------------------------

def snapshot_fp12(fingerprint: tuple) -> str:
    return hashlib.sha1(repr(fingerprint).encode()).hexdigest()[:12]


def snapshot_js_body(snap: bytes) -> bytes:
    """JS that assigns the snapshot JSON into the bootstrap-snapshot tag.
    json.dumps of the *string* double-encodes it into a safe JS literal; the
    explicit '<' escape prevents any </script> breakout."""
    text = snap.decode("utf-8", errors="replace")
    literal = json.dumps(text, ensure_ascii=False).replace("<", "\\u003c")
    return ("document.getElementById('bootstrap-snapshot').textContent=" + literal + ";").encode("utf-8")


@router.get("/render/{dashboard_id}/snapshot.js")
def render_snapshot_js(
    request: Request,
    dashboard_id: str,
    v: str = Query(""),
    share: Optional[str] = Query(None),
    user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    dashboard = _authorize_render(dashboard_id, share, user, db, request)
    snap = storage.read_snapshot_json(dashboard.id, dashboard.current_version)
    if snap is None:
        # No snapshot — maybe the dashboard ships a real asset named
        # snapshot.js; fall through to slot-file resolution.
        return render_asset(request, dashboard_id, "snapshot.js", share, user, db)
    current = snapshot_fp12(storage.version_fingerprint(dashboard.id, dashboard.current_version))
    # Matching v = content-addressed: browser-private immutable cache. A stale
    # v (data changed between document render and this fetch) self-heals: the
    # current data is served, uncacheable.
    cache = ("private, max-age=31536000, immutable" if v == current
             else "no-store, must-revalidate")
    return encoding.encoded_response(
        snapshot_js_body(snap), request.headers.get("accept-encoding", ""),
        media_type="text/javascript; charset=utf-8",
        headers={"Cache-Control": cache},
    )


# ---------------------------------------------------------------------------
# Relative-asset fallback for non-self-contained dashboards.
#
# The render document lives at /render/{id}/ (trailing slash), so a dashboard
# referencing <script src="my_data.js"> resolves it here. We serve the slot
# file whose ORIGINAL NAME matches (also trying name.gz / name.br with the
# corresponding Content-Encoding). This lets a broken dashboard be fixed by
# just uploading its missing asset as a slot — no HTML rework.
#
# MUST stay registered after /render/{id}/files/{slot} (registration order =
# match order); any future fixed-name routes (e.g. snapshot.js) go above it.
# ---------------------------------------------------------------------------

def serve_slot_asset(request: Request, dashboard_id: str, version: int,
                     asset_name: str) -> Response:
    """Serve a relative dashboard asset from the same-named slot file (also
    name.gz / name.br). Shared by /render/{id}/{asset} and the share variant.

    - A compressed fallback is sent with its Content-Encoding only when the
      client actually accepts that coding; otherwise it is decompressed and
      served as identity (correctness first: a client without br must still
      get a working asset).
    - Strong ETag from stat (mtime_ns + size + representation) — a 304
      revalidation costs one stat, never a full read + hash.
    - Single path segment matched against slot names — traversal-proof;
      dot-relative names rejected outright anyway."""
    if ".." in asset_name or asset_name.startswith("."):
        raise HTTPException(status_code=404, detail="No such asset")

    accepted = encoding.accepted_encodings(request.headers.get("accept-encoding", ""))
    for candidate, enc in ((asset_name, None), (asset_name + ".gz", "gzip"), (asset_name + ".br", "br")):
        path = storage.slot_asset_path(dashboard_id, version, candidate)
        if path is None:
            continue
        st = path.stat()
        decompress = enc is not None and enc not in accepted
        rep = "dec" if decompress else (enc or "id")
        etag = f'"{storage.slot_fp(st)}-{rep}"'
        headers = {
            "ETag": etag,
            # `no-cache` = store it, but revalidate every time. The url is
            # written by the dashboard author (<script src="x.js">), so unlike
            # a slot url we cannot rotate it — a freshness window here would
            # serve the PREVIOUS version's asset after a replace_html, exactly
            # the staleness that `max-age=300, must-revalidate` used to allow
            # (must-revalidate does not revalidate while still fresh). The
            # price is one conditional request per asset, answered by a 304
            # that costs a single stat.
            "Cache-Control": "private, no-cache",
            "Vary": "Accept-Encoding",
        }
        inm = request.headers.get("if-none-match")
        if inm and etag in [t.strip() for t in inm.split(",")]:
            return Response(status_code=304, headers=headers)
        content = path.read_bytes()
        if decompress:
            try:
                content = encoding.decode_bytes(content, enc)
            except encoding.DecodedTooLarge:
                raise HTTPException(status_code=413,
                                    detail="Stored asset expands beyond the serving limit")
        elif enc:
            headers["Content-Encoding"] = enc
        return Response(content=content, media_type=_mime(asset_name), headers=headers)

    raise HTTPException(status_code=404, detail="No asset with this name")


@router.get("/render/{dashboard_id}/{asset_name}")
def render_asset(
    request: Request,
    dashboard_id: str,
    asset_name: str,
    share: Optional[str] = Query(None),
    user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    dashboard = _authorize_render(dashboard_id, share, user, db, request)
    # ADR-18: filter bypass via the ORIGINAL slot-file name. A slot declared in
    # passthrough (JS libraries and other non-data) is served as is; any
    # data slot only to a subject with see_all (fail closed).
    if dashboard.rls_enabled:
        idx = _asset_slot_index(dashboard.id, dashboard.current_version, asset_name)
        if idx is not None and not rls.is_passthrough(rls.policy_of(dashboard), idx):
            if not rls.subject(db, dashboard, user).see_all:
                raise HTTPException(status_code=403,
                                    detail=f"RLS: slot {idx} is served filtered, not by asset name")
    return serve_slot_asset(request, dashboard.id, dashboard.current_version, asset_name)


# ---------------------------------------------------------------------------
# Save localStorage state (called by viewer.js on op_save_ls)
# ---------------------------------------------------------------------------

@router.put("/api/dashboards/{dashboard_id}/localstorage")
async def update_localstorage(
    dashboard_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dashboard = db.query(Dashboard).filter(Dashboard.id == dashboard_id).first()
    if not dashboard:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    if not can_update_dashboard_data(dashboard=dashboard, user=user, db=db):
        raise HTTPException(status_code=403, detail="No permission to update data")
    # The blob is shared by all viewers: under RLS writing it is a channel through
    # which a viewer with their own territory would swap the state for everyone else.
    _assert_rls_full_access(db, dashboard, user)
    body = await request.json()
    content = json.dumps(body, ensure_ascii=False).encode("utf-8")
    storage.save_ls_json(dashboard.id, dashboard.current_version, content)
    warm.schedule(dashboard.id)  # debounced: frequent op_save_ls collapses to one re-warm
    return {"ok": True}
