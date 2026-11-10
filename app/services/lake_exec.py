"""Server-side /explore execution over the published lake (ADR-14b).

The browser engine (`static/py/_explore.py`) is plain Python: on the server it
runs NATIVELY (real duckdb + pandas) over the same Parquet partitions the
generator run published to `data/uploads/_lake/{dashboard_id}/`. One question
therefore costs an HTTP round-trip instead of a wasm boot + partition
download — the whole reason the chat widget can answer instantly.

Boundaries this deliberately keeps:
  * ADR-14 read reach: executing a query = reading the lake, so the gate is
    `require_dashboard_access` — enforced by the API layer, not here.
  * «Cells never reach the MODEL»: nothing here talks to any LLM. Rows go
    server -> browser, exactly like `GET …/lake/parts` already does.
  * The ENGINE decides what SQL is allowed: `guard_sql`/`intent_sql` and the
    `enable_external_access=false` connection are the same trust boundary as
    in the browser — this file adds no query surface of its own.

Mechanics:
  * One fresh module instance of `_explore.py` per dashboard
    (`importlib.util` — module-level state is the engine's design, so
    instances must not share it), holding its own DuckDB connection and
    registered frames.
  * `CACHE_DIR` points at a per-instance scratch dir of SYMLINKS to the lake
    parts: the engine's own cache writes (materialised semantic views) and
    its LRU eviction touch the scratch, never the published partitions.
  * Registry: at most `OP_LAKE_EXEC_INSTANCES` instances (LRU), an idle
    instance is dropped after `_IDLE_S`; a manifest republish (run_id /
    created_at changed) rebuilds on the next request.
  * All engine calls for one instance are serialised by its lock — the
    engine is single-connection by design; parallelism across dashboards is
    parallelism across instances.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.i18n import _
from app.models import Dashboard, User
from app.services import storage

_ENGINE_PATH = Path(__file__).resolve().parents[2] / "static" / "py" / "_explore.py"

_IDLE_S = 30 * 60          # an untouched instance's frames are not worth the RAM
_MAX_SQL = 20_000          # mirror of the engine's MAX_SQL_CHARS, checked pre-import


class LakeExecError(Exception):
    """code: 'no_manifest' | 'parser' | 'semantics' | 'index' | 'disabled' | 'timeout'"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_SEQ = 0


def _fresh_engine():
    """A private module instance of the browser engine, never in sys.modules."""
    global _SEQ
    _SEQ += 1
    spec = importlib.util.spec_from_file_location(f"_op_lake_exec_{_SEQ}", _ENGINE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest_sig(man: dict) -> str:
    return f"{man.get('run_id')}|{man.get('created_at')}"


class _Instance:
    def __init__(self, dashboard_id: str, sig: str):
        self.dashboard_id = dashboard_id
        self.sig = sig
        self.lock = threading.Lock()
        self.built = False
        self.last_used = time.monotonic()
        self.mod = None
        self.scratch = None
        self.index_errors: list[dict] = []

    # -- lifecycle (caller holds self.lock) ---------------------------------
    def build(self, db: Session, dashboard: Dashboard, man: dict) -> None:
        mod = _fresh_engine()
        # Two writers, two version spaces: the engine's own parser (Excel
        # coercions, PARSER_VERSION) and the slot conversion (VARCHAR columns
        # typed by _sem_cast, lake_slots.PARSER_VERSION_SLOT). Anything else
        # (e.g. an engine-2 manifest) is refused honestly.
        from app.services.lake_slots import PARSER_VERSION_SLOT
        if str(man.get("parser_version") or "") not in (str(mod.PARSER_VERSION), PARSER_VERSION_SLOT):
            raise LakeExecError(
                "parser",
                _("The index on the server was built by a different engine version — recalculate the report."))

        snapshot = _snapshot_for(db, dashboard, man)
        scratch = tempfile.mkdtemp(prefix=f"op-lake-exec-{dashboard.id[:8]}-")
        try:
            for f in man.get("files") or []:
                key = str(f.get("key") or "")
                src = storage.lake_part_path(dashboard.id, key)
                if key and src.exists():
                    os.symlink(src, os.path.join(scratch, key + ".parquet"))
            mod.CACHE_DIR = scratch
            mod.set_catalog(snapshot)

            indexed, errors = 0, []
            for f in man.get("files") or []:
                try:
                    mod.index_cached(
                        str(f.get("key") or ""), f.get("table"),
                        "" if f.get("partition") is None else str(f.get("partition")),
                        str(f.get("name") or ""), f.get("sheet") or "")
                    indexed += 1
                except Exception as e:  # one bad partition must not kill the report
                    errors.append({"name": f.get("name"), "error": str(e)[:200]})
            if not indexed:
                raise LakeExecError("index", _("No index partition could be read — recalculate the report."))
            mod.semantics_status()      # materialise sem views once, at build
        except LakeExecError:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        except Exception:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        self.mod = mod
        self.scratch = scratch
        self.index_errors = errors
        self.built = True

    def close(self) -> None:
        for attr in ("_CON", "_IO_CON"):
            con = getattr(self.mod, attr, None) if self.mod else None
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
        if self.scratch:
            shutil.rmtree(self.scratch, ignore_errors=True)
        self.mod = None
        self.built = False


def _snapshot_for(db: Session, dashboard: Dashboard, man: dict) -> dict:
    """The same catalog snapshot the browser worker gets, built under the
    reach of whoever PUBLISHED the manifest (they could run the generator by
    construction; the dashboard owner is the fallback). Semantics are cut to
    this dashboard's generator: rows here can only come from ITS partitions,
    so other generators' KPIs would be dead weight to materialise."""
    from app.api.explore import _catalog_snapshot   # late: api imports services

    pub = None
    by = str(man.get("by") or "")
    if by:
        pub = db.query(User).filter(User.person_id == by).first()
    if pub is None and dashboard.owner_id:
        pub = db.query(User).filter(User.id == dashboard.owner_id).first()
    if pub is None:
        raise LakeExecError("semantics", _("The index author was not found — recalculate the report."))

    snapshot = _catalog_snapshot(db, pub, None)
    gid = dashboard.generator_id
    sems = [e for e in (snapshot.get("semantics") or []) if e.get("generator_id") == gid]
    if not sems:
        raise LakeExecError("semantics", _("The generator semantics are unavailable — republish the bundle."))
    snapshot = dict(snapshot)
    snapshot["semantics"] = sems
    return snapshot


# ── Registry ─────────────────────────────────────────────────────────────
_REG: dict[str, _Instance] = {}
_REG_LOCK = threading.Lock()


def _evict_locked(keep: str | None) -> None:
    """Drop idle/over-cap instances (registry lock held). A busy instance
    (lock taken) is skipped — it is evicted on a later pass."""
    now = time.monotonic()
    cap = max(1, int(settings.lake_exec_instances))
    items = sorted(_REG.items(), key=lambda kv: kv[1].last_used)
    alive = len(_REG)
    for did, inst in items:
        if did == keep:
            continue
        idle = now - inst.last_used
        if idle < _IDLE_S and alive <= cap:
            continue
        if inst.lock.acquire(blocking=False):
            try:
                _REG.pop(did, None)
                alive -= 1
                inst.close()
            finally:
                inst.lock.release()


def _get_instance(db: Session, dashboard: Dashboard) -> tuple[_Instance, dict]:
    raw = storage.read_lake_manifest(dashboard.id)
    if not raw:
        raise LakeExecError("no_manifest", _("There is no index for this report on the server."))
    man = json.loads(raw)
    sig = _manifest_sig(man)

    with _REG_LOCK:
        inst = _REG.get(dashboard.id)
        if inst is not None and inst.sig != sig and inst.lock.acquire(blocking=False):
            # Republished: the old frames answer for data that no longer exists.
            try:
                _REG.pop(dashboard.id, None)
                inst.close()
            finally:
                inst.lock.release()
            inst = None
        if inst is None:
            inst = _Instance(dashboard.id, sig)
            _REG[dashboard.id] = inst
        inst.last_used = time.monotonic()
        _evict_locked(keep=dashboard.id)
    return inst, man


def _timeout_s() -> float:
    try:
        v = float(settings.lake_exec_timeout_s)
    except (TypeError, ValueError):
        v = 60.0
    return v if v > 0 else 60.0


def _mark_unhealthy_if_broken(inst: "_Instance") -> None:
    """After an interrupt DuckDB's connection is USUALLY still fine to keep
    using — that's the entire point of `interrupt()` over `close()` — but a
    one-query probe turns the rare broken connection into a clean rebuild
    on the NEXT call instead of every future question on this dashboard
    failing. Caller holds inst.lock."""
    mod = inst.mod
    con = getattr(mod, "_CON", None) if mod is not None else None
    try:
        if con is not None:
            con.execute("SELECT 1").fetchone()
            return  # still alive: keep serving from this instance
    except Exception:
        pass
    inst.close()


def _run_watched(mod, timeout_s: float, fn):
    """Run one op against `mod`'s DuckDB connection(s) under a best-effort
    watchdog: `threading.Timer` fires `conn.interrupt()` at `timeout_s`, the
    same mechanism `static/py/_explore.py` uses for its own (unconfigurable,
    30 s) deadline inside `run_sql`/`export_xlsx` — but this is the ONLY
    guard `intent` gets (semantics materialisation runs raw `CREATE TABLE
    AS` with no timeout of its own), and it also catches `export_xlsx`,
    which does not catch its own `ExploreTimeout`/interrupt and would
    otherwise 500 the request instead of answering with a clear error.

    Returns `(result, fired)` — `fired` tells the caller our OWN timer is
    the one that cut the query, so a graceful in-body error (like
    `run_sql`'s) can be reworded to name OUR ceiling instead of the raw
    DuckDB message (its own `_INTERRUPTED` flag never saw our interrupt).

    Two watchdogs run side by side here: ours and `_arm_interrupt` inside
    `_execute`, armed a few hundred microseconds later at the SAME
    `timeout_s`. Whichever fires first wins the race; when the engine's own
    timer wins, `export_xlsx` lets the raw `InterruptException` escape and
    our `fired` flag is still False — so a cut query is recognised by the
    escaped exception itself too, never by our timer alone (otherwise the
    SAME slow query 500s once in a few runs)."""
    fired = {"v": False}

    def _fire():
        fired["v"] = True
        for attr in ("_CON", "_IO_CON"):
            con = getattr(mod, attr, None)
            if con is not None:
                try:
                    con.interrupt()
                except Exception:
                    pass

    timer = threading.Timer(timeout_s, _fire)
    timer.daemon = True
    timer.start()
    try:
        result = fn()
    except LakeExecError:
        raise
    except Exception as e:
        if fired["v"] or _is_interrupt(e):
            raise LakeExecError(
                "timeout",
                _("The query ran longer than {seconds} s and was stopped.", seconds=int(timeout_s)))
        raise
    finally:
        timer.cancel()
    return result, fired["v"]


def _is_interrupt(e: BaseException) -> bool:
    """DuckDB's `InterruptException` without importing duckdb here — this
    module is imported by app.main on every start, the engine (and duckdb)
    only when a lake question actually arrives."""
    return type(e).__name__ == "InterruptException" or "INTERRUPT Error" in str(e)


def reset() -> None:
    """Tests only: drop every instance."""
    with _REG_LOCK:
        for inst in list(_REG.values()):
            with inst.lock:
                inst.close()
        _REG.clear()


# ── Public entry ─────────────────────────────────────────────────────────
def run_op(db: Session, dashboard: Dashboard, op: str, payload: dict) -> dict:
    """Execute one worker-shaped op over the dashboard's published lake.

    Returns the SAME dict the browser worker would post for that op, so the
    page-side transport shim can hand it to untouched UI code. Raises
    LakeExecError for states the client shows as an honest status line."""
    if not settings.lake_exec:
        raise LakeExecError("disabled", _("Server-side query execution is turned off."))
    inst, man = _get_instance(db, dashboard)
    with inst.lock:
        inst.last_used = time.monotonic()
        if not inst.built:
            inst.build(db, dashboard, man)
        mod = inst.mod
        if op == "status":
            return {
                "tables": mod.tables(), "stats": mod.stats(),
                "semantics": mod.semantics_status(),
                "manifest": man, "index_errors": inst.index_errors,
            }
        if op not in ("intent", "sql", "export"):
            raise LakeExecError("index", _("Unknown operation: {op}", op=repr(op)))

        timeout_s = _timeout_s()
        try:
            if op == "intent":
                result, _fired = _run_watched(
                    mod, timeout_s, lambda: mod.intent_sql(payload.get("intent") or {}))
                return result
            if op == "sql":
                sql = str(payload.get("sql") or "")[:_MAX_SQL]
                limit = payload.get("limit") or 1000
                result, fired = _run_watched(
                    mod, timeout_s, lambda: mod.run_sql(sql, limit, timeout_s=timeout_s))
                if fired and result.get("error"):
                    # Our timer — not the engine's own (_INTERRUPTED never saw
                    # it) — cut this one: reword the raw DuckDB message into
                    # the same clear line `run_sql` itself would have used.
                    result = dict(result)
                    result["error"] = (
                        _("Query stopped: over {seconds} s. "
                          "Add filter or restrict partitions.", seconds=int(timeout_s)))
                return result
            sql = str(payload.get("sql") or "")[:_MAX_SQL]
            data, _fired = _run_watched(
                mod, timeout_s, lambda: mod.export_xlsx(sql, timeout_s=timeout_s))
            return {"bytes": data}
        except LakeExecError as e:
            if e.code == "timeout":
                # The interrupted connection is usually fine, but confirm it
                # before the next question on this dashboard relies on it —
                # either alive, or `build()` will recreate it on the next call.
                _mark_unhealthy_if_broken(inst)
            raise
