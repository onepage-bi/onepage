"""Runtime-editable LLM configuration — the admin's "turn on DeepSeek"
switch, without a redeploy and without touching docker-compose.

Env (`OP_LLM_*`) stays the DEFAULT layer; what the admin saves on /admin
becomes an overlay persisted to `data/llm-runtime.json` and applied by
mutating the live `settings` object. Every worker re-reads the file when
its mtime changes (checked at the entry points below, ≤1 s TTL — the same
read-path discipline as the telemetry `telemetry.off` marker and the
session TTL), so `OP_WORKERS > 1` converges without any cross-process
signalling and a restart keeps the admin's choice.

Entry points that call `ensure_fresh()`: `nl2sql.provider()`,
`nl2sql.call_llm()` and the `/api/explore/llm-config` route — everything
else reads `settings.llm_*` downstream of one of those in the same
request. Keys (`llm_api_key`, `llm_browser_key`) are write-only through
the admin API: stored in the file (0600), never echoed back, never
audited by value — the git-token discipline.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.config import settings

# Fields the admin may override, with a type coercion for each.
OVERRIDE_FIELDS: dict[str, type] = {
    "llm_provider": str,
    "llm_browser": bool,
    "llm_model": str,
    "llm_endpoint": str,
    "llm_api_key": str,
    "llm_browser_key": str,
}
SECRET_FIELDS = ("llm_api_key", "llm_browser_key")

_TTL_S = 1.0

# Env-layer defaults, captured once at import — an override REMOVED from the
# file restores these, not whatever value happened to be applied last.
_env_defaults = {name: getattr(settings, name) for name in OVERRIDE_FIELDS}

_checked_at = 0.0
_applied_mtime: float | None = None


def _path() -> Path:
    return settings.abs_db_path.parent / "llm-runtime.json"


def _read() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _apply(overrides: dict) -> None:
    for name, typ in OVERRIDE_FIELDS.items():
        if name in overrides:
            try:
                value = typ(overrides[name])
            except Exception:
                continue
            setattr(settings, name, value)
        else:
            setattr(settings, name, _env_defaults[name])


def ensure_fresh(force: bool = False) -> None:
    """Re-apply the overlay if the file changed. Two `perf_counter` calls
    and one `stat` at most per second — nothing here is on a hot path
    anyway (explore/admin routes only)."""
    global _checked_at, _applied_mtime
    now = time.monotonic()
    if not force and now - _checked_at < _TTL_S:
        return
    _checked_at = now
    try:
        mtime = _path().stat().st_mtime
    except FileNotFoundError:
        mtime = None
    if mtime == _applied_mtime and not force:
        return
    _applied_mtime = mtime
    _apply(_read() if mtime is not None else {})


def current_overrides() -> dict:
    """The stored overlay with secrets reduced to `*_set` booleans."""
    data = _read()
    out = {k: v for k, v in data.items() if k in OVERRIDE_FIELDS and k not in SECRET_FIELDS}
    for name in SECRET_FIELDS:
        out[name + "_set"] = bool((data.get(name) or "").strip())
    return out


def set_overrides(changes: dict) -> dict:
    """Merge `changes` into the stored overlay and apply immediately.

    Semantics per field: absent = untouched; None = drop the override
    (env default returns); anything else = the new override. Returns the
    field names that changed (for the audit — names, never values).
    """
    data = _read()
    touched: list[str] = []
    for name in OVERRIDE_FIELDS:
        if name not in changes:
            continue
        value = changes[name]
        if value is None:
            if name in data:
                del data[name]
                touched.append(name)
            continue
        value = OVERRIDE_FIELDS[name](value)
        if data.get(name) != value:
            data[name] = value
            touched.append(name)
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    ensure_fresh(force=True)
    return {"changed": touched}
