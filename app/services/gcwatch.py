"""
GC-pause telemetry — measure-first, per the audit's finding F11.

Python's cyclic garbage collector runs on whatever thread trips a collection
threshold, which on this app is usually the event-loop thread. A gen2 sweep
walks every tracked container, so a big heap can stall the loop for
milliseconds — a plausible-but-unmeasured tail-latency source. Rather than
guess, this hook records the actual pause distribution into the perf log so the
decision is data-driven.

- start(): registers a gc.callbacks hook (idempotent). No import-time side
  effects — importing this module never touches gc.
- The callback times each collection with two perf_counter() reads and emits a
  perf record ONLY for gen1/gen2 sweeps or any collection ≥ 1 ms. gen0
  collections fire constantly (~every 700 allocations) and are almost always
  sub-millisecond, so the hot "start"/"stop" pair costs two perf_counter()
  calls and one comparison — nothing is allocated on the common gen0 pass.

Threshold for action: if the observed gen2 pause p99 exceeds 5 ms, move GC off
the loop thread (e.g. freeze the long-lived heap / gc.freeze at startup, or
tune thresholds). Until then this is pure measurement.

Records land in data/perflog as {"t": "gc", "gen", "dur_ms", "collected",
"uncollectable"}; perflog.emit already gates on settings.perf_enabled and the
runtime on/off marker, so a disabled telemetry run drops these for free.
"""
from __future__ import annotations

import gc
import time
from time import perf_counter

from app.services import perflog

# Collections don't nest in CPython, so a single slot for the in-flight sweep's
# start time + generation is sufficient. Set on "start", read on "stop".
_t0: float = 0.0
_gen: int = 0
_registered = False


def _callback(phase: str, info: dict) -> None:
    global _t0, _gen
    if phase == "start":
        _gen = info.get("generation", 0)
        _t0 = perf_counter()
        return
    # phase == "stop"
    dur_ms = (perf_counter() - _t0) * 1000.0
    # gen0 fires constantly and is almost always sub-ms — skip it unless it
    # actually stalled. gen1/gen2 are always worth a record.
    if _gen >= 1 or dur_ms >= 1.0:
        perflog.emit({
            "ts": time.time(),
            "src": "server",
            "t": "gc",
            "gen": _gen,
            "dur_ms": round(dur_ms, 3),
            "collected": info.get("collected"),
            "uncollectable": info.get("uncollectable"),
        })


def start() -> None:
    """Register the GC-pause hook. Idempotent — safe to call more than once."""
    global _registered
    if _registered or _callback in gc.callbacks:
        return
    gc.callbacks.append(_callback)
    _registered = True
