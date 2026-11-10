"""Runtime-editable "Quests" configuration (ADR-17) — season dates and the
holiday calendar, without a redeploy.

This is a SEPARATE overlay file from `data/llm-runtime.json`
(`app/services/runtime_config.py`), not a section bolted onto it, for two
reasons: (1) the holiday calendar is LIST-valued (a set of ISO dates), a
shape `runtime_config.OVERRIDE_FIELDS`'s flat scalar coercion does not
support and should not be stretched to support just for this one feature;
(2) blast radius — a bad write to the LLM overlay can take down `/explore`
model calls, a bad write here can only shift streak/season arithmetic, and
keeping them in different files means a mistake in one can never corrupt
the other's JSON.

Same read-path discipline as `runtime_config.py` and the telemetry
`telemetry.off` marker: env (`OP_QUESTS_*`) is the DEFAULT layer, captured
once at import; what an admin saves becomes an overlay persisted to
`data/quests-runtime.json` and applied by mutating the live `settings`
object; every worker re-reads the file when its mtime changes (≤1 s TTL),
so `OP_WORKERS > 1` converges without any cross-process signalling and a
restart keeps the admin's choice.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path

from app.config import settings
from app.i18n import _

# Scalar fields the admin may override, with a type coercion for each.
OVERRIDE_FIELDS: dict[str, type] = {
    "quests_enabled": bool,
    "quests_season_start": str,
    "quests_season_length": int,
    "quests_companion_enabled": bool,
    # Active card season (1 — foxes, 2 — toads). The overlay wins over env, so
    # the season is switched by editing this file from the admin page, no redeploy.
    "quests_season": int,
}
# Holidays are list-valued, so they live in the overlay file under this key
# rather than in OVERRIDE_FIELDS (which only knows scalar coercions).
HOLIDAYS_KEY = "holidays"

_TTL_S = 1.0

# Env-layer defaults, captured once at import — an override REMOVED from the
# file restores these, not whatever value happened to be applied last.
_env_defaults = {name: getattr(settings, name) for name in OVERRIDE_FIELDS}

_holidays_parsed: set = set()
_checked_at = 0.0
_applied_mtime: float | None = None
_holidays: list[str] = []


def _path() -> Path:
    return settings.abs_db_path.parent / "quests-runtime.json"


_corrupt = False  # last read hit an unparseable file — refuse to overwrite it


def _read() -> dict:
    """Parse the overlay. A missing file is the normal "no overrides" state;
    an unparseable one is an incident: we log it, treat it as empty for
    reading, and `set_overrides` refuses to write over it (the admin has
    to look at the file) — otherwise the next save would silently wipe the
    season calendar."""
    global _corrupt
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _corrupt = False
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        _corrupt = False
        return {}
    except Exception as exc:
        _corrupt = True
        logging.getLogger("onepage.quests").error("quests-runtime.json unreadable: %s", exc)
        return {}


def _coerce(typ, value):
    """JSON/HTTP-safe coercion: `bool("false")` is True, so bools get a
    word table; ints go through `int()`; everything else `str()`."""
    if typ is bool:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(f"not a boolean: {value!r}")
    return typ(value)


def _apply(overrides: dict) -> None:
    global _holidays
    for name, typ in OVERRIDE_FIELDS.items():
        if name in overrides:
            try:
                value = _coerce(typ, overrides[name])
            except Exception:
                value = _env_defaults[name]  # malformed override == removed override
            setattr(settings, name, value)
        else:
            setattr(settings, name, _env_defaults[name])
    global _holidays_parsed
    raw_holidays = overrides.get(HOLIDAYS_KEY)
    _holidays = sorted({str(h) for h in raw_holidays}) if isinstance(raw_holidays, list) else []
    parsed = set()
    for h in _holidays:
        try:
            parsed.add(date.fromisoformat(h))
        except (ValueError, TypeError):
            pass
    _holidays_parsed = parsed  # parsed once per file version, not per call


def ensure_fresh(force: bool = False) -> None:
    """Re-apply the overlay if the file changed. Two `perf_counter` calls
    and one `stat` at most per second — nothing here is on a hot path."""
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


def get() -> dict:
    """Current effective config plus which fields carry an admin override —
    the shape the admin page and the season/streak math both read."""
    ensure_fresh()
    data = _read()
    return {
        "enabled": settings.quests_enabled,
        "season_start": settings.quests_season_start,
        "season_length": settings.quests_season_length,
        "companion_enabled": settings.quests_companion_enabled,
        "season": settings.quests_season,
        "holidays": sorted(_holidays),
        "overrides": [name for name in OVERRIDE_FIELDS if name in data],
    }


def holidays() -> set:
    """Parsed holiday dates. Invalid strings are ignored rather than
    raising — a single bad row in the calendar must not take down every
    streak computation that reads it."""
    ensure_fresh()
    return set(_holidays_parsed)


def set_overrides(changes: dict, holidays: list | None = None, reset: list = ()) -> list:
    """Validate and merge `changes` (+ optionally replace the holiday list)
    into the stored overlay, write it atomically, apply immediately, and
    return the field names that changed (for the audit — names, never
    values). `reset` names fields to drop back to the env default.

    Raises `ValueError` with a readable message on bad input.
    """
    data = _read()
    if _corrupt:
        raise ValueError(
            _("The file quests-runtime.json is corrupt and will not be overwritten: "
              "fix or delete it on the server")
        )
    touched: list[str] = []

    for name in changes or {}:
        if name not in OVERRIDE_FIELDS:
            raise ValueError(_("Unknown quests parameter: {name}", name=name))

    if "quests_season_start" in (changes or {}):
        raw = changes["quests_season_start"]
        try:
            date.fromisoformat(str(raw))
        except (ValueError, TypeError):
            raise ValueError(
                _("The season start date must be in ISO format (YYYY-MM-DD): {value}", value=repr(raw))
            )

    if "quests_season" in (changes or {}):
        raw = changes["quests_season"]
        # Validate against the season registry, not a range: only a declared
        # season may be active, otherwise the draw would silently fall back to the default.
        from app.services.quests import SEASON_BY_KEY

        try:
            key = int(raw)
        except (ValueError, TypeError):
            raise ValueError(_("The season number must be an integer: {value}", value=repr(raw)))
        if key not in SEASON_BY_KEY:
            known = ", ".join(str(k) for k in sorted(SEASON_BY_KEY))
            raise ValueError(_("Unknown card season: {season} (available: {known})", season=key, known=known))

    if "quests_season_length" in (changes or {}):
        raw = changes["quests_season_length"]
        try:
            length = int(raw)
        except (ValueError, TypeError):
            raise ValueError(_("The season length must be an integer: {value}", value=repr(raw)))
        if not (1 <= length <= 400):
            raise ValueError(_("The season length must be from 1 to 400 working days"))

    for name, typ in OVERRIDE_FIELDS.items():
        if name not in (changes or {}):
            continue
        try:
            value = _coerce(typ, changes[name])
        except (ValueError, TypeError):
            raise ValueError(_("Invalid value for parameter {name}: {value}", name=name, value=repr(changes[name])))
        if data.get(name) != value:
            data[name] = value
            touched.append(name)

    for name in reset or ():
        if name not in OVERRIDE_FIELDS and name != HOLIDAYS_KEY:
            raise ValueError(_("Unknown quests parameter: {name}", name=name))
        if name in data:
            del data[name]
            touched.append(name)

    if holidays is not None:
        parsed: list[str] = []
        for h in holidays:
            try:
                d = date.fromisoformat(str(h))
            except (ValueError, TypeError):
                raise ValueError(_("Invalid holiday date (expected YYYY-MM-DD): {value}", value=repr(h)))
            parsed.append(d.isoformat())
        new_holidays = sorted(set(parsed))
        if len(new_holidays) > 400:
            raise ValueError(_("Too many holidays: at most 400 dates"))
        if data.get(HOLIDAYS_KEY) != new_holidays:
            data[HOLIDAYS_KEY] = new_holidays
            touched.append(HOLIDAYS_KEY)

    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # per-worker temp: no interleaving
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    ensure_fresh(force=True)
    return touched
