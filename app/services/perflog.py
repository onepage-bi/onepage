"""
Performance log service — near-zero-overhead timing collection.

Hot path: emit() only appends a dict to an in-memory deque (thread-safe,
~microseconds). A daemon writer thread flushes batches to daily JSONL files
(data/perflog/perf-YYYYMMDD.jsonl). JSONL instead of the main SQLite DB so
perf writes never contend with the application's single-writer lock — the
instrumentation must not skew what it measures.

Per-request state lives in `request_record`: the middleware stores a plain
dict there at request start; SQL listeners and span() mutate that same dict.
Sync routes run in a threadpool with a *copied* context, so ContextVar.set()
from inside them would be invisible outside — but the dict object identity is
preserved across the copy, which is why everything mutates the dict in place.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import re
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

from app.config import settings

request_record: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "op_perf_record", default=None
)

_queue: deque[dict] = deque()
_wakeup = threading.Event()
_started = False
_start_lock = threading.Lock()

_FLUSH_INTERVAL = 2.0   # seconds between writer wake-ups
_FLUSH_BATCH = 500      # queue length that triggers an immediate flush
_QUEUE_CAP = 100_000    # hard cap — drop records instead of eating memory
# Records serialized per GIL-release slice in the writer. Sized SMALLER than a
# realistic batch on purpose: the stand measured this the hard way. At 1000 the
# slicing never engaged, because a batch is capped by _FLUSH_BATCH (500) or by
# TARGET_RPS x the 2 s cycle (~430) and never reaches 1000 — so every flush was
# one monolithic dumps holding the GIL ~1.7 ms. The signature was unmistakable:
# with adaptive sampling ON, p50 rose only 22 us over telemetry-off (per-request
# cost is nil) while p999 hit 8.8 ms — and that equalled the p99 of the
# unsampled arm. Sampling had cut how OFTEN the writer stalls the loop, not how
# LONG. This value bounds the stall instead: ~64 records ~= 0.25 ms per slice,
# with the loop free to run in between.
_WRITE_CHUNK = 64


# ---------------------------------------------------------------------------
# Adaptive sampling. Benchmark campaign C1 measured the failure mode this prevents:
# at OP_PERF_SAMPLE=1.0 and ~2k rps the writer's json.dumps batches hold the
# GIL long enough to stall the event loop (bare RTT p99 9.27 ms, vs 0.73 ms
# with telemetry off). The writer thread runs a control loop over its own
# batch sizes and publishes the sampling probability the middleware should
# use; the fixed point is emit rate == OP_PERF_TARGET_RPS. Each sampled
# record is stamped with the probability it survived ("sample"), so
# /admin/perf can weight it by 1/sample back into unbiased counts and
# percentiles.
#
# Lock-free by the same convention as _runtime_on: only the writer thread
# assigns _eff_sample, loop threads only read it (a float rebind is atomic
# under the GIL). With OP_PERF_ADAPTIVE off the value never moves from
# OP_PERF_SAMPLE — exactly the legacy fixed-probability behaviour.
# ---------------------------------------------------------------------------

_eff_sample: float = min(1.0, max(0.0, settings.perf_sample))
_EWMA_ALPHA = 0.3
_ema_rps = 0.0
_ctl_t: float | None = None
_ctl_pending = 0  # records drained during sub-0.1 s wakeups, not yet counted


def effective_sample() -> float:
    """Probability the middleware must use for the gate AND stamp into the
    record — one read, so the weight always matches the decision."""
    return _eff_sample


def _control_tick(batch_len: int) -> None:
    """One control step (writer thread only): estimate the true request rate
    from this cycle's emit rate and the probability those records carried,
    then steer the published probability toward OP_PERF_TARGET_RPS.

    batch_len includes client-beacon, GC and force-emitted error records,
    which don't pass the request sample gate — the estimate overshoots
    slightly and the sampler undershoots, i.e. errs on the conservative
    side."""
    global _eff_sample, _ema_rps, _ctl_t, _ctl_pending
    now = perf_counter()
    _ctl_pending += batch_len
    if _ctl_t is None:
        _ctl_t = now
        _ctl_pending = 0  # no measured interval to attribute these to
        return
    dt = now - _ctl_t
    if dt < 0.1:
        # Batch-triggered wakeups can fire back-to-back; the count stays in
        # _ctl_pending so a burst of short cycles isn't dropped from the
        # estimate — only the interval waits until it's measurable.
        return
    _ctl_t = now
    ceiling = min(1.0, max(0.0, settings.perf_sample))
    floor = min(ceiling, max(0.0, settings.perf_sample_floor))
    target = max(0.0, settings.perf_target_rps)  # negative env must not 1/0
    eff_used = max(_eff_sample, floor, 1e-6)
    inst_rps = (_ctl_pending / dt) / eff_used
    _ctl_pending = 0
    _ema_rps += _EWMA_ALPHA * (inst_rps - _ema_rps)
    if _ema_rps <= target:
        _eff_sample = ceiling  # idle / low traffic: record (up to) everything
    else:
        # _ema_rps > target >= 0 here, so the division is safe. Rounded
        # BEFORE publishing: the stamped "sample" must equal the gate
        # probability exactly, and 6 digits keeps the JSONL field short.
        _eff_sample = round(min(ceiling, max(floor, target / _ema_rps)), 6)


def perf_dir() -> Path:
    p = settings.perf_dir
    return p if p.is_absolute() else settings.base_dir / p


# ---------------------------------------------------------------------------
# Runtime kill switch. OP_PERF_ENABLED is the master (off = no writer thread
# at all); this flag pauses/resumes collection WITHOUT a redeploy — the admin
# toggle on /admin/perf. Persisted as a marker file in the perf dir, so it
# survives restarts and reaches every worker: each worker re-reads the marker
# on its writer wake-up cycle (≤2 s of cross-worker skew).
# ---------------------------------------------------------------------------

_OFF_MARKER = "telemetry.off"
_runtime_on = True
_reopen = False


def runtime_on() -> bool:
    return _runtime_on


def set_runtime(on: bool) -> None:
    global _runtime_on
    _runtime_on = on
    try:
        d = perf_dir()
        d.mkdir(parents=True, exist_ok=True)
        marker = d / _OFF_MARKER
        if on:
            marker.unlink(missing_ok=True)
        else:
            marker.touch()
    except OSError:
        pass


def _refresh_runtime() -> None:
    global _runtime_on
    try:
        _runtime_on = not (perf_dir() / _OFF_MARKER).exists()
    except OSError:
        _runtime_on = True


def request_reopen() -> None:
    """After external log deletion: close + reopen today's file (an unlinked
    open fd would otherwise keep collecting invisible bytes)."""
    global _reopen
    _reopen = True
    _wakeup.set()


def is_perf_file(name: str) -> bool:
    """True for our own JSONL files only — safe for bulk deletion."""
    return _FILE_RE.match(name) is not None


def emit(record: dict) -> None:
    """Hot path: enqueue a record. Never blocks, never raises."""
    if not settings.perf_enabled or not _runtime_on or len(_queue) >= _QUEUE_CAP:
        return
    _queue.append(record)
    if len(_queue) >= _FLUSH_BATCH:
        _wakeup.set()


@contextlib.contextmanager
def span(name: str):
    """Time a stage of the current request; accumulates into rec['stages']."""
    rec = request_record.get()
    if rec is None:
        yield
        return
    t0 = perf_counter()
    try:
        yield
    finally:
        stages = rec.setdefault("stages", {})
        stages[name] = round(stages.get(name, 0.0) + (perf_counter() - t0) * 1000, 3)


# ---------------------------------------------------------------------------
# Writer thread
# ---------------------------------------------------------------------------

def _drain() -> list[dict]:
    out = []
    while True:
        try:
            out.append(_queue.popleft())
        except IndexError:
            return out


# Exactly perf-YYYYMMDD.jsonl (legacy) or perf-YYYYMMDD-{pid}.jsonl — never
# match other JSONL dropped into the dir (e.g. a re-uploaded export named
# perf-YYYYMMDDHHMM-YYYYMMDDHHMM.jsonl), which would double-count summaries
# and be eaten by retention.
_FILE_RE = re.compile(r"^perf-(\d{8})(?:-\d+)?\.jsonl$")


def _apply_retention(directory: Path) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.perf_retention_days)).strftime("%Y%m%d")
    for f in directory.glob("perf-*.jsonl"):
        m = _FILE_RE.match(f.name)
        if m and m.group(1) < cutoff:
            try:
                f.unlink()
            except OSError:
                pass


# A share token is a bearer capability: it grants anonymous read of the
# dashboard and its data, it outlives the grantee's account, and its use leaves
# no per-user audit trail. It travels in the PATH (/s/{t}, /share/{t}/...), so
# without this the log — retained for days, exportable, rendered on /admin/perf
# — hands a working token to anyone who can read either. Redacting happens here,
# in the writer thread, so the request path pays nothing for it.
_TOKEN_PREFIXES = ("/s/", "/share/")


def _redact_path(path: str) -> str:
    if not path.startswith(_TOKEN_PREFIXES):
        return path
    parts = path.split("/")
    # ["", "s", "{token}", ...] / ["", "share", "{token}", ...]
    if len(parts) > 2 and parts[2]:
        parts[2] = "*"
    return "/".join(parts)


def _redact(record: dict) -> dict:
    """Copy-on-need: the overwhelming majority of records carry no token."""
    path, page, url = record.get("path"), record.get("page"), record.get("url")
    if not ((isinstance(path, str) and path.startswith(_TOKEN_PREFIXES))
            or (isinstance(page, str) and _has_token(page))
            or (isinstance(url, str) and _has_token(url))):
        return record
    out = dict(record)
    if isinstance(path, str):
        out["path"] = _redact_path(path)
    # Client beacons carry the page/url the browser was on — a share visitor's
    # beacon would otherwise reintroduce the token the server side just dropped.
    if isinstance(page, str):
        out["page"] = _redact_url(page)
    if isinstance(url, str):
        out["url"] = _redact_url(url)
    return out


def _has_token(value: str) -> bool:
    return any(prefix in value for prefix in _TOKEN_PREFIXES)


def _redact_url(value: str) -> str:
    for prefix in _TOKEN_PREFIXES:
        idx = value.find(prefix)
        if idx == -1:
            continue
        head, tail = value[:idx], value[idx:]
        return head + _redact_path(tail)
    return value


def _writer_loop() -> None:
    global _reopen
    current_day: str | None = None
    fh = None
    while True:
        _wakeup.wait(timeout=_FLUSH_INTERVAL)
        _wakeup.clear()
        _refresh_runtime()  # cross-worker toggle pickup (marker file)
        if _reopen:
            _reopen = False
            if fh:
                fh.close()
            fh = None
            current_day = None  # forces a fresh file on the next batch
        batch = _drain()
        if settings.perf_adaptive:
            # Runs on empty batches too: idle traffic decays the EWMA and the
            # published probability climbs back to the ceiling.
            try:
                _control_tick(len(batch))
            except Exception:
                pass  # the sampler must never take the writer thread down
        if not batch:
            continue
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        try:
            if day != current_day:
                if fh:
                    fh.close()
                d = perf_dir()
                d.mkdir(parents=True, exist_ok=True)
                # Per-process file: each uvicorn worker is the single writer of
                # its own file, so multi-worker deploys can't interleave lines.
                # 0600: the file carries uid, client ip and request paths —
                # readable by the app's own user, nobody else on the host.
                fh = open(
                    os.open(str(d / f"perf-{day}-{os.getpid()}.jsonl"),
                            os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600),
                    "a", encoding="utf-8",
                )
                current_day = day
                _apply_retention(d)
            # Sub-batches with a brief GIL release between them: defense in
            # depth for a queue that piled up anyway (adaptive off, floor too
            # high) — one monolithic dumps of thousands of records is exactly
            # the loop-stalling hotspot C1 measured.
            for i in range(0, len(batch), _WRITE_CHUNK):
                fh.write("".join(
                    json.dumps(_redact(r), ensure_ascii=False,
                               separators=(",", ":"), default=str) + "\n"
                    for r in batch[i:i + _WRITE_CHUNK]
                ))
                if i + _WRITE_CHUNK < len(batch):
                    time.sleep(0.001)
            fh.flush()
        except OSError:
            # Disk trouble must never take the app down; drop what's left of
            # the batch (sub-batches already written stay on disk — a torn
            # trailing line is skipped by iter_records' JSONDecodeError pass).
            current_day = None
            fh = None


def start() -> None:
    """Launch the writer thread once (called from app lifespan)."""
    global _started
    if not settings.perf_enabled:
        return
    _refresh_runtime()  # honor a persisted admin toggle across restarts
    with _start_lock:
        if _started:
            return
        threading.Thread(target=_writer_loop, daemon=True, name="op-perflog").start()
        _started = True


# ---------------------------------------------------------------------------
# Reading (export / summary)
# ---------------------------------------------------------------------------

def iter_records(dt_from: datetime, dt_to: datetime):
    """Yield parsed records with dt_from <= ts < dt_to (ts = epoch seconds, UTC)."""
    ts_from, ts_to = dt_from.timestamp(), dt_to.timestamp()
    d = perf_dir()
    if not d.exists():
        return
    day = dt_from.astimezone(timezone.utc).date()
    last_day = dt_to.astimezone(timezone.utc).date()
    while day <= last_day:
        # Both legacy perf-YYYYMMDD.jsonl and per-pid perf-YYYYMMDD-{pid}.jsonl,
        # validated by _FILE_RE so foreign JSONL in the dir is never counted.
        for f in sorted(f for f in d.glob(f"perf-{day.strftime('%Y%m%d')}*.jsonl")
                        if _FILE_RE.match(f.name)):
            with open(f, encoding="utf-8") as fh:
                n = 0
                for line in fh:
                    # Callers run in the threadpool, but a tight json.loads loop
                    # still starves the event loop via the GIL (measured 300 ms
                    # spikes while parsing 100+ MB). Briefly park the thread
                    # every few thousand lines — costs ~1 ms per 8k records.
                    n += 1
                    if n % 8192 == 0:
                        time.sleep(0.001)
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = rec.get("ts")
                    if isinstance(ts, (int, float)) and ts_from <= ts < ts_to:
                        yield rec
        day += timedelta(days=1)
