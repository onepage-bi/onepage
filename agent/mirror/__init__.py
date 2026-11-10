"""Desktop report mirror: on-disk layout, state, local addresses.

This package holds the part of the mirror that does NOT touch the network:
where things live, what a slot file is called, what goes into state.json, and
at which addresses the local HTTP serves the document, snapshot, slots and
relative assets. Decisions, boundaries and lifted invariants are in
`docs/RFC_DESKTOP_MIRROR.md`; the server side is `app/api/sync.py`.

Why the contract is a separate module rather than "emerging" in place:
the synchronizer, the local server and the viewer must all compute the SAME
file name and the same address. A mismatch between them yields an empty report
on the person's machine at the very moment the server is down and there is
nobody to ask -- that is, in exactly the one case this program exists for.

Two invariants that live here and nowhere else:

  * **A name from the network never becomes a path.** The original slot name
    is kept in state.json (`name`) and is used to resolve assets; on disk the
    file is written as `file` = `safe_slot_filename(name)`. The path is always
    taken from the inventory that we wrote ourselves, so `../`, an NTFS
    stream, `NUL.js` and a 400-character name cannot escape the directory even
    in theory. Details and the list of pitfalls are in the docstring of
    `layout`.
  * **The document is served at an address with a trailing slash.** Otherwise
    relative `<script src="x.js">` and `snapshot.js?v=` resolve one level up
    and the report opens empty. A request without the slash gets a 307, and
    `parse_local_path` returns a separate kind `KIND_REDIRECT` for it, so the
    server cannot forget about it.

Reports with `mirrorable: false` (RLS, ADR-18) never end up here: the ban is
enforced by the synchronizer, and there is simply no function here that would
build a path for such a report.

Standard library only (agent/ is Python 3.12, stdlib).
"""
from __future__ import annotations

from .layout import (  # noqa: F401
    # layout
    MIRROR_DIRNAME, STATE_NAME, STATE_SCHEMA, DEFAULT_RIGHTS_TTL_S,
    SPACE_SERVER, SPACE_LOCAL, DOCUMENT_NAME, SNAPSHOT_NAME,
    mirror_root, dashboard_dir, server_version_dir, local_run_dir,
    state_file, dir_for,
    # file names and resolution
    MAX_NAME_CHARS, is_valid_dashboard_id, safe_slot_filename, plan_slot_files,
    resolve_asset, slot_by_number, slot_disk_path,
    # local addresses
    URL_PREFIX, KIND_DOCUMENT, KIND_SNAPSHOT, KIND_SLOT, KIND_ASSET,
    KIND_REDIRECT, MirrorRequest, parse_local_path,
    document_url, local_document_url, base_url, snapshot_url, slot_url,
    asset_url,
    # state
    new_state, migrate_state, read_state, write_state, atomic_write, sweep_tmp,
    rights_expired, can_update_locally, next_local_n, add_local_run,
)

__all__ = [
    "MIRROR_DIRNAME", "STATE_NAME", "STATE_SCHEMA", "DEFAULT_RIGHTS_TTL_S",
    "SPACE_SERVER", "SPACE_LOCAL", "DOCUMENT_NAME", "SNAPSHOT_NAME",
    "mirror_root", "dashboard_dir", "server_version_dir", "local_run_dir",
    "state_file", "dir_for",
    "MAX_NAME_CHARS", "is_valid_dashboard_id", "safe_slot_filename",
    "plan_slot_files", "resolve_asset", "slot_by_number", "slot_disk_path",
    "URL_PREFIX", "KIND_DOCUMENT", "KIND_SNAPSHOT", "KIND_SLOT", "KIND_ASSET",
    "KIND_REDIRECT", "MirrorRequest", "parse_local_path",
    "document_url", "local_document_url", "base_url", "snapshot_url",
    "slot_url", "asset_url",
    "new_state", "migrate_state", "read_state", "write_state", "atomic_write",
    "sweep_tmp", "rights_expired", "can_update_locally", "next_local_n",
    "add_local_run",
]
