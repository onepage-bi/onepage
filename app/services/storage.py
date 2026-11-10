"""
File storage service — manages uploads/{dashboard_id}/v{n}/ layout.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from app.config import settings


def _dash_dir(dashboard_id: str) -> Path:
    return settings.abs_uploads_dir / dashboard_id


def _version_dir(dashboard_id: str, version: int) -> Path:
    return _dash_dir(dashboard_id) / f"v{version}"


def _files_dir(dashboard_id: str, version: int) -> Path:
    return _version_dir(dashboard_id, version) / "files"


def _current_file(dashboard_id: str) -> Path:
    return _dash_dir(dashboard_id) / "current"


# ---------------------------------------------------------------------------
# Version management
# ---------------------------------------------------------------------------

def init_dashboard(dashboard_id: str) -> None:
    _dash_dir(dashboard_id).mkdir(parents=True, exist_ok=True)


def save_html(dashboard_id: str, version: int, content: bytes) -> str:
    """Save HTML bytes to v{version}/dashboard.html. Returns relative path."""
    vdir = _version_dir(dashboard_id, version)
    vdir.mkdir(parents=True, exist_ok=True)
    _files_dir(dashboard_id, version).mkdir(parents=True, exist_ok=True)
    path = vdir / "dashboard.html"
    path.write_bytes(content)
    _current_file(dashboard_id).write_text(str(version), encoding="utf-8")
    return str(path.relative_to(settings.abs_uploads_dir))


def read_html(dashboard_id: str, version: int | None = None) -> bytes:
    v = version or _get_current_version(dashboard_id)
    return (_version_dir(dashboard_id, v) / "dashboard.html").read_bytes()


def _get_current_version(dashboard_id: str) -> int:
    cf = _current_file(dashboard_id)
    if cf.exists():
        return int(cf.read_text(encoding="utf-8").strip())
    return 1


def estimate_render_size(dashboard_id: str, version: int) -> int | None:
    """Cheap estimate of the injected /render body: HTML + inlined snapshot.
    Undershoots by a few KB of injected controllers — fine for a preview
    threshold. Returns None when the HTML itself is missing."""
    vdir = _version_dir(dashboard_id, version)
    try:
        total = (vdir / "dashboard.html").stat().st_size
    except OSError:
        return None
    try:
        total += (vdir / "snapshot.json").stat().st_size
    except OSError:
        pass
    return total


def version_fingerprint(dashboard_id: str, version: int) -> tuple:
    """mtime_ns of every render input for this version — a cheap cache key that
    changes whenever the rendered output could change. The files/ dir mtime
    covers slot add/remove/replace (save_slot_file always unlinks + rewrites)."""
    vdir = _version_dir(dashboard_id, version)

    def m(p: Path) -> int:
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0

    return (
        m(vdir / "dashboard.html"),
        m(vdir / "snapshot.json"),
        m(vdir / "theme.json"),
        m(vdir / "text.json"),
        m(vdir / "localstorage.json"),
        m(vdir / "files"),
    )


# ---------------------------------------------------------------------------
# Slot files (interactive dashboards)
# ---------------------------------------------------------------------------

def _slot_filename(slot_index: int, original_name: str) -> str:
    safe = re.sub(r'[^\w.\-]', '_', original_name)
    return f"slot_{slot_index}_{safe}"


def save_slot_file(
    dashboard_id: str,
    version: int,
    slot_index: int,
    original_name: str,
    content: bytes,
) -> str:
    """Save file for a given slot. Returns stored path relative to uploads_dir."""
    fdir = _files_dir(dashboard_id, version)
    fdir.mkdir(parents=True, exist_ok=True)

    # Remove any existing file for this slot
    for existing in fdir.glob(f"slot_{slot_index}_*"):
        existing.unlink()

    filename = _slot_filename(slot_index, original_name)
    path = fdir / filename
    path.write_bytes(content)
    return str(path.relative_to(settings.abs_uploads_dir))


def slot_file_path(dashboard_id: str, version: int, slot_index: int):
    """Path of the slot file for this index, or None. No content read — the
    serving routes stat it for the ETag and only read on a cache miss."""
    fdir = _files_dir(dashboard_id, version)
    if not fdir.exists():
        return None
    matches = list(fdir.glob(f"slot_{slot_index}_*"))
    return matches[0] if matches else None


def read_slot_file(dashboard_id: str, version: int, slot_index: int) -> tuple[bytes, str] | None:
    """Returns (content, filename) or None if no file for this slot."""
    f = slot_file_path(dashboard_id, version, slot_index)
    if f is None:
        return None
    return f.read_bytes(), f.name[len(f"slot_{slot_index}_"):]


def delete_slot_file(dashboard_id: str, version: int, slot_index: int) -> bool:
    """Delete slot file from filesystem. Returns True if a file was deleted."""
    fdir = _files_dir(dashboard_id, version)
    if not fdir.exists():
        return False
    deleted = False
    for f in fdir.glob(f"slot_{slot_index}_*"):
        f.unlink()
        deleted = True
    return deleted


def slot_fp(st: os.stat_result) -> str:
    """Content identity of a slot file, derived from a stat we already hold.

    Slot URLs carry this as `?v=` so that a changed slot mints a NEW url —
    invalidation by key rotation, the same discipline as the render cache and
    `?v=` on static assets. It is what makes an `immutable` slot response safe:
    without it the url is version-less and any freshness window silently serves
    the previous report's data (a publish is a new version = a new file, but
    the same url).
    """
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


def list_slot_files(dashboard_id: str, version: int) -> list[dict]:
    """Returns [{slot, name, size, v}] for all slots in this version.

    `v` costs nothing extra: the size we already report needs the same stat."""
    fdir = _files_dir(dashboard_id, version)
    if not fdir.exists():
        return []
    result = []
    for f in sorted(fdir.iterdir()):
        m = re.match(r'^slot_(\d+)_(.+)$', f.name)
        if m:
            st = f.stat()
            result.append({
                "slot": int(m.group(1)),
                "name": m.group(2),
                "size": st.st_size,
                "v": slot_fp(st),
            })
    return result


def slot_asset_path(dashboard_id: str, version: int, original_name: str):
    """Path of the slot file whose ORIGINAL name matches, or None. One
    directory scan, no content read — the asset routes stat it for the ETag
    and only read on a cache miss."""
    fdir = _files_dir(dashboard_id, version)
    if not fdir.exists():
        return None
    for f in fdir.iterdir():
        m = re.match(r'^slot_(\d+)_(.+)$', f.name)
        if m and m.group(2) == original_name:
            return f
    return None


# ---------------------------------------------------------------------------
# Presentation slide files (images / PDF)
# ---------------------------------------------------------------------------

_PRESENTATIONS_ROOT = "_presentations"
PRESENTATION_EXTS = ("png", "jpg", "pdf", "html")


def presentation_dir(presentation_id: str) -> Path:
    return settings.abs_uploads_dir / _PRESENTATIONS_ROOT / presentation_id


def save_presentation_file(presentation_id: str, item_id: str, ext: str, data: bytes) -> str:
    """Store one slide file. Returns the path relative to uploads_dir.

    The name is built ONLY from ids the server minted plus a whitelisted
    extension — a client filename never reaches the filesystem, so `../` in an
    upload is not something to strip, it is something that never arrives."""
    ext = ext.lower().lstrip(".")
    if ext not in PRESENTATION_EXTS:
        raise ValueError(f"unsupported presentation file extension: {ext!r}")
    pdir = presentation_dir(presentation_id)
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{item_id}.{ext}"
    path.write_bytes(data)
    return str(path.relative_to(settings.abs_uploads_dir))


def presentation_file_path(rel_path: str) -> Path:
    return settings.abs_uploads_dir / rel_path


def delete_presentation_file(rel_path: str | None) -> bool:
    """Unlink a stored slide file. A missing file is not an error — deletion
    must succeed even if the disk state drifted."""
    if not rel_path:
        return False
    try:
        presentation_file_path(rel_path).unlink()
        return True
    except OSError:
        return False


def delete_presentation_dir(presentation_id: str) -> None:
    pdir = presentation_dir(presentation_id)
    if pdir.exists():
        shutil.rmtree(pdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Server lake cache (Phase 9.1) — row-level Parquet partitions of a report's
# source folder, stored ON THE SERVER, written by the report's generator run,
# readable by anyone with dashboard access. Only the LATEST run is kept.
# ---------------------------------------------------------------------------

_LAKE_ROOT = "_lake"
_LAKE_MANIFEST_NAME = "manifest.json"


def lake_dir(dashboard_id: str) -> Path:
    """Path is built ONLY from the validated dashboard id — never from
    anything a client names."""
    return settings.abs_uploads_dir / _LAKE_ROOT / dashboard_id


def lake_part_path(dashboard_id: str, key: str) -> Path:
    """`key` must already be validated by the caller (`^[a-f0-9]{16,64}$`) —
    this function does not re-check it."""
    return lake_dir(dashboard_id) / f"{key}.parquet"


def lake_manifest_path(dashboard_id: str) -> Path:
    return lake_dir(dashboard_id) / _LAKE_MANIFEST_NAME


def save_lake_part(dashboard_id: str, key: str, data: bytes) -> Path:
    """Write one partition atomically: a tmp file in the SAME directory, then
    `os.replace` — a concurrent reader never observes a half-written
    `.parquet`, and a crash mid-write leaves only an orphaned `.tmp`, never a
    corrupt part under the real name."""
    d = lake_dir(dashboard_id)
    d.mkdir(parents=True, exist_ok=True)
    final = lake_part_path(dashboard_id, key)
    tmp = d / f".{key}.{os.getpid()}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, final)
    return final


def lake_dir_bytes(dashboard_id: str) -> int:
    """Total size of the KEPT partitions (`.parquet` only — an in-flight
    `.tmp` is not published data yet, so it is not counted against the cap)."""
    d = lake_dir(dashboard_id)
    if not d.exists():
        return 0
    return sum(f.stat().st_size for f in d.glob("*.parquet") if f.is_file())


def save_lake_manifest(dashboard_id: str, content: bytes) -> None:
    """Same atomic tmp+replace discipline as a part — a reader mid-publish
    sees either the old manifest or the new one, never a half-written one."""
    d = lake_dir(dashboard_id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".manifest.{os.getpid()}.tmp"
    tmp.write_bytes(content)
    os.replace(tmp, lake_manifest_path(dashboard_id))


def read_lake_manifest(dashboard_id: str) -> bytes | None:
    p = lake_manifest_path(dashboard_id)
    return p.read_bytes() if p.exists() else None


def list_lake_part_keys(dashboard_id: str) -> set[str]:
    d = lake_dir(dashboard_id)
    if not d.exists():
        return set()
    return {f.stem for f in d.glob("*.parquet") if f.is_file()}


def delete_lake_part(dashboard_id: str, key: str) -> bool:
    try:
        lake_part_path(dashboard_id, key).unlink()
        return True
    except OSError:
        return False


def delete_lake_dir(dashboard_id: str) -> None:
    d = lake_dir(dashboard_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Snapshot JSON (snapshot dashboards)
# ---------------------------------------------------------------------------

def save_snapshot_json(dashboard_id: str, version: int, content: bytes) -> str:
    vdir = _version_dir(dashboard_id, version)
    vdir.mkdir(parents=True, exist_ok=True)
    path = vdir / "snapshot.json"
    path.write_bytes(content)
    return str(path.relative_to(settings.abs_uploads_dir))


def read_snapshot_json(dashboard_id: str, version: int) -> bytes | None:
    path = _version_dir(dashboard_id, version) / "snapshot.json"
    return path.read_bytes() if path.exists() else None


def save_ls_json(dashboard_id: str, version: int, content: bytes) -> None:
    vdir = _version_dir(dashboard_id, version)
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "localstorage.json").write_bytes(content)


def read_ls_json(dashboard_id: str, version: int) -> bytes | None:
    path = _version_dir(dashboard_id, version) / "localstorage.json"
    return path.read_bytes() if path.exists() else None


# ---------------------------------------------------------------------------
# Theme JSON (Colors feature) — per-version colour overrides
# ---------------------------------------------------------------------------

def save_theme_json(dashboard_id: str, version: int, content: bytes) -> None:
    vdir = _version_dir(dashboard_id, version)
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "theme.json").write_bytes(content)


def read_theme_json(dashboard_id: str, version: int) -> bytes | None:
    path = _version_dir(dashboard_id, version) / "theme.json"
    return path.read_bytes() if path.exists() else None


def delete_theme_json(dashboard_id: str, version: int) -> bool:
    path = _version_dir(dashboard_id, version) / "theme.json"
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Text JSON (Edit text feature) — per-version static-text overrides
# ---------------------------------------------------------------------------

def save_text_json(dashboard_id: str, version: int, content: bytes) -> None:
    vdir = _version_dir(dashboard_id, version)
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "text.json").write_bytes(content)


def read_text_json(dashboard_id: str, version: int) -> bytes | None:
    path = _version_dir(dashboard_id, version) / "text.json"
    return path.read_bytes() if path.exists() else None


def delete_text_json(dashboard_id: str, version: int) -> bool:
    path = _version_dir(dashboard_id, version) / "text.json"
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Version copy
# ---------------------------------------------------------------------------

def copy_slot_files(dashboard_id: str, from_version: int, to_version: int) -> None:
    """Copy all slot files from one version to another."""
    src = _files_dir(dashboard_id, from_version)
    dst = _files_dir(dashboard_id, to_version)
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if re.match(r'^slot_\d+_.+$', f.name):
            shutil.copy2(f, dst / f.name)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def delete_dashboard_files(dashboard_id: str) -> None:
    ddir = _dash_dir(dashboard_id)
    if ddir.exists():
        shutil.rmtree(ddir)
