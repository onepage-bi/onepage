"""A single response from which the desktop app learns what it has that is stale.

Why the endpoint is needed at all. Until now the list of reports available to a person
was served in exactly one place — the `#dash-data` blob inside the home page
HTML (`app/api/dashboards.py::index`). For a browser that is right, for
a program it isn't, for three separate reasons:

  * parsing HTML as an API is fragile: any edit to `templates/pages/index.html`
    silently breaks sync;
  * `upd` there is formatted as `%Y-%m-%d`, i.e. **with day precision** —
    a report updated three times a day is indistinguishable by that field from an untouched one;
  * slots aren't listed in it, so every report needs a separate request
    to `/api/dashboards/{id}/files` — N+1 over the network on a slow link.

A separate endpoint rather than extending the index: the home page has a budget of six
queries, written in its own comment, and dragging rights and
generators in there would break it for the sake of a page that doesn't need them.

**`fingerprint` closes a hole that is easy to miss.** `theme.json`,
`text.json`, `localstorage.json` and a slot overwrite change the rendered
document but do NOT move `current_version`; and `/render/{id}/` has neither an ETag nor
a cache (`no-store`), so a conditional request can't catch the change. Without a fingerprint
the program would either miss a theme edit or re-download the document every
hour. We return the same thing the server already keys its render cache by.

**Reports with RLS (ADR-18) are not given to the mirror, and that's not mere caution.** The rows
of such a report are filtered for a specific viewer on every request
(`rls.serve_filtered`), share links are already closed for it, `/source` is closed for
non-`see_all`. A copy on disk is someone's filtered rows lying around without
the filter, which is exactly what ADR-18 forbids. So such a report comes
in the response with `mirrorable: false` and a reason: the program must skip it, and
the person must learn why it isn't there instead of assuming something got lost.

Rights go into the same response (`can_update_data`, `see_all`), because offline
there will be no server to ask. And we honestly return `rights_ttl_s`: a cached
right is a right that can't be revoked, so it must have an expiry.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, joinedload, load_only

from app.auth.deps import get_current_user
from app.config import settings
from app.database import get_db
from app.models import (
    DASH_PAGE, ROLE_ADMINISTRATOR, Dashboard, DashboardPermission, Generator,
    GeneratorVersion, User,
)
from app.services import storage

router = APIRouter()

# How long a delivered right lives before the program must re-request it.
# A day is a compromise between "a revocation arrives in reasonable time" and "the server has been down
# for three days and the person still needs to read reports": reading after expiry
# is never blocked, only LOCAL UPDATE is blocked.
RIGHTS_TTL_S = 24 * 3600


def _mirror_block(d: Dashboard) -> tuple[bool, str]:
    """Whether this report may be kept on the person's disk, and if not — why.

    The reason is returned as a code, not text: text is the UI's business, it gets
    translated, while the code is pinned by a test.
    """
    if d.rls_enabled:
        return False, "rls"
    # "Strictly confidential": the device key syncs the copy at night, without
    # the person and without a password — on disk it would end up without the second lock.
    # The report does NOT disappear from the list: the person must see why it's missing.
    if d.conf_hash:
        return False, "confidential"
    # The navigator is a page of outbound links: in a mirror that is read while
    # the server is down, all its links lead nowhere anyway. A copy of such a
    # page would promise work that doesn't exist.
    if d.dash_type == DASH_PAGE:
        return False, "navigator"
    if not d.is_published:
        return False, "draft"
    return True, ""


@router.get("/api/sync/manifest")
async def sync_manifest(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Access — by the same single WHERE with a scalar subquery as on the home page:
    # own, public, or granted and published. Diverging from the index
    # here is not allowed, otherwise the program would see something other than what the person sees in the browser.
    # The folder is loaded with the same `joinedload` as the owner: the program draws
    # the list with the same sections as "Dashboards" in the browser, and fetching
    # the folder name with a separate query per report is not allowed — this endpoint has the same
    # "one query for everything" budget as the home page.
    q = db.query(Dashboard).options(
        joinedload(Dashboard.owner).load_only(User.person_id),
        joinedload(Dashboard.folder),
    )
    is_admin = user.role == ROLE_ADMINISTRATOR
    if not is_admin:
        perm_sq = (
            select(DashboardPermission.dashboard_id)
            .where(DashboardPermission.user_id == user.id)
            .scalar_subquery()
        )
        q = q.filter(or_(
            Dashboard.owner_id == user.id,
            Dashboard.is_public == True,  # noqa: E712 — SQLAlchemy, not a Python bool
            and_(Dashboard.id.in_(perm_sq), Dashboard.is_published == True),  # noqa: E712
        ))
    dashboards = q.order_by(Dashboard.updated_at.desc()).all()

    # Data-update rights — one query for all reports at once, rather than
    # calling can_update_dashboard_data in a loop (that would be a query per report).
    upd_ids: set[str] = set()
    if not is_admin:
        upd_ids = {
            did for (did,) in
            db.query(DashboardPermission.dashboard_id).filter(
                DashboardPermission.user_id == user.id,
                DashboardPermission.can_update_data == True,  # noqa: E712
            ).all()
        }

    # The current generator version — also one query for all, for the same reason.
    gen_ids = {d.generator_id for d in dashboards if d.generator_id}
    gen_meta: dict[int, dict] = {}
    if gen_ids:
        rows = (
            db.query(Generator.id, Generator.name,
                     GeneratorVersion.version_number, GeneratorVersion.checksum_sha256)
              .join(GeneratorVersion, GeneratorVersion.generator_id == Generator.id)
              .filter(Generator.id.in_(gen_ids),
                      GeneratorVersion.is_current == True)  # noqa: E712
              .all()
        )
        gen_meta = {
            gid: {"id": gid, "name": name, "version": vnum, "checksum_sha256": sha}
            for gid, name, vnum, sha in rows
        }

    items = []
    for d in dashboards:
        mirrorable, reason = _mirror_block(d)
        can_update = is_admin or d.owner_id == user.id or d.id in upd_ids
        row = {
            "id": d.id,
            "title": d.title,
            "type": d.dash_type,
            "version": d.current_version,
            # WITH TIME, unlike `upd` on the home page: a day is too coarse
            # for a synchronizer to rely on.
            "updated_at": d.updated_at.isoformat() if d.updated_at else None,
            "size": d.render_size or 0,
            # Folder — the NAME, not just the id: offline there is no server to ask,
            # and the list section has to be labelled with something. A report without a folder
            # comes with `null` and lands in "No folder", as on the platform.
            "folder": ({"id": d.folder.id, "name": d.folder.name}
                       if d.folder is not None else None),
            "rls_enabled": d.rls_enabled,
            "can_update_data": bool(can_update),
            "mirrorable": mirrorable,
            "skip_reason": reason,
            "generator": gen_meta.get(d.generator_id) if d.generator_id else None,
        }
        # Fingerprint and slots cost disk access, so we compute them only
        # for what the program will actually download.
        if mirrorable:
            fp = storage.version_fingerprint(d.id, d.current_version)
            row["fingerprint"] = "-".join(format(x, "x") for x in fp)
            row["slots"] = storage.list_slot_files(d.id, d.current_version)
        items.append(row)

    return {
        "items": items,
        "user": {"person_id": user.person_id, "role": user.role},
        "rights_ttl_s": RIGHTS_TTL_S,
        # The program must know whether the snapshot is split out into a separate file: with
        # the flag on, the document references `snapshot.js?v=…`, and forgetting
        # it means showing an empty report.
        "snapshot_external": bool(settings.snapshot_external),
    }
