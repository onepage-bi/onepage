"""Generator code bundle storage — manages uploads/_generators/{id}/v{n}/.

Mirrors the immutable-version layout of storage.py, but for generator code
bundles rather than dashboard content. The "_generators" prefix can never
collide with a dashboard directory (those are named by uuid4).
"""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import zipfile
from pathlib import Path

from app.config import settings


class BundleError(ValueError):
    """Human-readable bundle problem — the API maps it to HTTP 400."""


MANIFEST_MAX_BYTES = 64 * 1024

_MANIFEST_NAME = "manifest.json"
_BUNDLE_NAME = "bundle.zip"


def _generators_dir() -> Path:
    return settings.abs_uploads_dir / "_generators"


def _generator_dir(generator_id: int) -> Path:
    return _generators_dir() / str(generator_id)


def _version_dir(generator_id: int, version_number: int) -> Path:
    return _generator_dir(generator_id) / f"v{version_number}"


# ---------------------------------------------------------------------------
# Zip-slip / manifest extraction
# ---------------------------------------------------------------------------

def _is_unsafe_member(name: str) -> bool:
    """True if a zip entry name could escape its extraction directory. The
    server itself never extracts a bundle, but the agent engine will, on
    users' machines — some of those tools treat a backslash as a path
    separator even though the zip spec doesn't, so both conventions are
    checked."""
    if not name:
        return False
    if name.startswith("/") or name.startswith("\\"):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", name):  # Windows drive-letter absolute path
        return True
    if ".." in name.split("/"):
        return True
    if "\\" in name and ".." in name.split("\\"):
        return True
    return False


def read_manifest_from_zip(zip_bytes: bytes) -> bytes:
    """Validate a bundle zip and return its root manifest.json bytes.

    Raises BundleError if: the payload isn't a zip; there is no manifest.json
    at the archive root (exact name, not nested); the manifest exceeds
    MANIFEST_MAX_BYTES; or any member has an unsafe path (zip-slip).
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise BundleError("not a valid zip archive") from e

    with zf:
        for info in zf.infolist():
            if _is_unsafe_member(info.filename):
                raise BundleError(f"unsafe path in bundle: {info.filename!r}")

        if _MANIFEST_NAME not in zf.namelist():
            raise BundleError("bundle is missing manifest.json at its root")

        # NOT zf.getinfo().file_size: that is a number the archive declares
        # about itself, and the writer is free to lie. A bundle well under the
        # 20 MB cap can declare 1000 bytes and expand to hundreds of MB —
        # measured 815 KB in, 839 MB allocated — because zf.read() inflates the
        # whole stream before the CRC mismatch is noticed. Read through a bounded
        # stream instead, so the ceiling is enforced on bytes actually produced.
        with zf.open(_MANIFEST_NAME) as fh:
            data = fh.read(MANIFEST_MAX_BYTES + 1)
        if len(data) > MANIFEST_MAX_BYTES:
            raise BundleError(
                f"manifest.json exceeds the {MANIFEST_MAX_BYTES}-byte limit"
            )
        return data


_SEMANTICS_NAME = "semantics.json"
SEMANTICS_MAX_BYTES = 512 * 1024


def read_semantics_from_zip(zip_bytes: bytes) -> bytes | None:
    """Return the root semantics.json bytes, or None when the bundle has none
    (the file is optional). Same bounded read as the manifest: the size cap is
    enforced on inflated bytes, never on what the archive claims."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        if _SEMANTICS_NAME not in zf.namelist():
            return None
        with zf.open(_SEMANTICS_NAME) as fh:
            data = fh.read(SEMANTICS_MAX_BYTES + 1)
        if len(data) > SEMANTICS_MAX_BYTES:
            raise BundleError(f"semantics.json exceeds the {SEMANTICS_MAX_BYTES}-byte limit")
        return data


# ---------------------------------------------------------------------------
# Version storage
# ---------------------------------------------------------------------------

def save_bundle(
    generator_id: int, version_number: int, zip_bytes: bytes, manifest_bytes: bytes
) -> tuple[str, str]:
    """Write a version directory: bundle.zip (as uploaded) + manifest.json
    (extracted copy, for fast reads). Returns (version dir path relative to
    settings.abs_uploads_dir, sha256 hex of zip_bytes). Versions are
    immutable — raises BundleError if this version already has a bundle on
    disk (protects against silent overwrite)."""
    vdir = _version_dir(generator_id, version_number)
    if (vdir / _BUNDLE_NAME).exists():
        raise BundleError(
            f"generator {generator_id} version {version_number} already has "
            f"a bundle — versions are immutable"
        )

    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / _BUNDLE_NAME).write_bytes(zip_bytes)
    (vdir / _MANIFEST_NAME).write_bytes(manifest_bytes)

    checksum = hashlib.sha256(zip_bytes).hexdigest()
    return str(vdir.relative_to(settings.abs_uploads_dir)), checksum


def prune_version_dirs(generator_id: int, keep: int = 2) -> list[int]:
    """Delete bundle DIRS of all but the newest `keep` versions (current +
    previous by default). DB rows stay — version history, checksums and audit
    remain intact; only the code bytes of versions nobody can roll back to
    anymore leave the disk. Only `v{n}` dirs are touched — `runs/` (analyst
    aux uploads) lives beside them and is never eligible. Best-effort: a dir
    that refuses to delete is skipped, not fatal — this runs after the
    version commit and must never fail the upload that triggered it."""
    gdir = _generator_dir(generator_id)
    if not gdir.is_dir():
        return []
    numbered = []
    for child in gdir.iterdir():
        m = re.fullmatch(r"v(\d+)", child.name)
        if m and child.is_dir():
            numbered.append((int(m.group(1)), child))
    numbered.sort(reverse=True)
    pruned = []
    for number, path in numbered[max(keep, 1):]:
        try:
            shutil.rmtree(path)
            pruned.append(number)
        except OSError:
            pass
    return pruned


def bundle_zip_path(generator_id: int, version_number: int):
    """Path to bundle.zip, or None if this version has no bundle on disk."""
    p = _version_dir(generator_id, version_number) / _BUNDLE_NAME
    return p if p.exists() else None


def read_version_manifest(generator_id: int, version_number: int) -> bytes | None:
    """Extracted manifest.json bytes from disk, or None."""
    p = _version_dir(generator_id, version_number) / _MANIFEST_NAME
    return p.read_bytes() if p.exists() else None


# ---------------------------------------------------------------------------
# Run auxiliary input storage — platform-generic "aux_inputs" mechanism
# (manifest-declared, e.g. an analyst's filled reasons workbook consumed by a
# special second run). Lives under the same generator id, but keyed by RUN id
# rather than version number — "runs" as a sibling of "v{n}" can never
# collide, version dirs are always "v" + digits.
# ---------------------------------------------------------------------------

_AUX_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")


def _runs_dir(generator_id: int) -> Path:
    return _generator_dir(generator_id) / "runs"


def _run_aux_dir(generator_id: int, run_id: int) -> Path:
    return _runs_dir(generator_id) / str(run_id)


def _check_aux_filename(filename: str) -> None:
    """Same safe-name rule as genmanifest's aux_inputs regex (kept in sync by
    hand, two small modules). The regex charset already excludes '/' and '\\',
    so no path separator can pass; '..' is rejected explicitly as defense in
    depth, same as the manifest-side check."""
    if not filename or not _AUX_NAME_RE.match(filename) or ".." in filename:
        raise BundleError(f"unsafe aux file name: {filename!r}")


def save_run_aux(generator_id: int, run_id: int, filename: str, content: bytes) -> str:
    """Write a run's auxiliary input file to
    uploads/_generators/{id}/runs/{run_id}/{filename}. Unlike code bundles,
    overwrite IS allowed here — the same run re-uploading after a network
    retry is a legitimate, expected case, not a corruption risk."""
    _check_aux_filename(filename)
    rdir = _run_aux_dir(generator_id, run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    path = rdir / filename
    path.write_bytes(content)
    return str(path.relative_to(settings.abs_uploads_dir))


def run_aux_path(generator_id: int, run_id: int, filename: str) -> Path:
    """Path to a run's auxiliary input file. No existence check — callers
    decide what a missing file means (404 vs. "not uploaded yet")."""
    _check_aux_filename(filename)
    return _run_aux_dir(generator_id, run_id) / filename


# ---------------------------------------------------------------------------
# Git sync helper
# ---------------------------------------------------------------------------

def make_zip_from_dir(src_dir) -> bytes:
    """Deterministic zip of a directory tree, for git-sync: sorted traversal,
    fixed date_time (1980-01-01), ZIP_DEFLATED — the same tree always
    produces the same bytes. Excludes the .git directory (and everything
    under it) entirely. Archive member paths are relative POSIX paths."""
    src_dir = Path(src_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(src_dir)
            if ".git" in rel.parts:
                continue
            info = zipfile.ZipInfo(rel.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def delete_version_dir(generator_id: int, version_number: int) -> bool:
    """Remove ONE version's directory. `save_bundle` refuses to overwrite an
    existing `v{n}` (versions are immutable), so a registration that writes
    the bundle and then fails — the DB rolls back, the directory does not —
    would wedge the generator: every later upload picks the same `v{n}` and
    is refused as immutable. Callers use this as the undo half of
    `save_bundle` on any post-write failure. Best-effort: returns False if
    there was nothing to remove or it could not be removed."""
    vdir = _version_dir(generator_id, version_number)
    if not vdir.is_dir():
        return False
    try:
        shutil.rmtree(vdir)
        return True
    except OSError:
        return False


def delete_generator_files(generator_id: int) -> None:
    gdir = _generator_dir(generator_id)
    if gdir.exists():
        shutil.rmtree(gdir)
