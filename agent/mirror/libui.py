"""Library shell: the `/library/...` pages on the agent's loopback.

Without this module the mirror is unreachable: `serve.py` can serve a REPORT
at `/mirror/<id>/`, but a person has no way to get to that address — the list
of copies, the sync state and key intake live here. The split is exactly
this because the trust boundaries of the two halves differ: report delivery
knows nothing about the device key and does not go to the network
(`serve.py`, docstring), while the shell accepts the key and triggers sync.
Code that holds a secret is best kept in one file — then "the key does not
reach the log" is checked by reading one module, not the whole package.

Addresses (the already written `web/*` pages expect them, so this is a
CONTRACT, not a choice of this module):

    GET  /library/                 web/library.html
    GET  /library/view/            web/viewer.html   (?id=&space=&n= read by JS)
    GET  /library/common.js | library.js | viewer.js | mirror.css
    GET  /library/strings.json     texts (assets.strings_path)
    GET  /library/data.json        the mirror state in one response
    POST /library/sync             sync now; the answer is
                                   {"started":true} or {"started":false,
                                   "reason":"already_running"|"no_schedule"}
    POST /library/key   {"key":…}  accept the device key
    POST /library/local-run        build a report from folders WITHOUT the platform;
                                   {"started":true} or {"started":false,
                                   "reason":<precheck code>, "why":…}

Decisions, each with its reason:

  * **A name from the address is matched against a WHITE LIST, not turned
    into a path.** The file is served by `assets.res_path`, which itself
    checks the name against a white alphabet, but the list here is still
    explicit: otherwise `/library/<any file of the web/ directory>` would
    become a public address, and sooner or later `web/` acquires a file that
    was not meant to be served. That is why `library.html` and `viewer.html`
    are NOT served under their own names — only at the addresses `/library/`
    and `/library/view/`. There is no percent-decoding here ON PURPOSE: all
    names in the contract are plain ASCII, and `%2e%2e%2f` never occurs in
    the address of an honest page, so it is an attempt, and the answer to it
    is 404, not normalization (the same rule as in `layout.parse_local_path`).

  * **The `web/` directory is not listed** and has no subdirectories: an
    address that did not match the list exactly is a 404.

  * **MIME comes from `serve.mime_for`.** A second type table would drift
    from the first, and the price of drift is known: a `.js` with the type
    `application/octet-stream` is silently blocked by ORB (see pitfall 2 in `serve.py`).

  * **POST is protected no weaker than the agent's existing routes.** Today
    the agent checks two things on POST: `Host` names loopback (`_host_ok`
    against DNS rebinding — done by `AgentHandler` itself before us) and, for
    `/runs`, the pairing Bearer token. The library page has no token and
    cannot have one (it is OUR page, not a platform page), so the second
    check here is ORIGIN: it must be our own loopback origin with our port.
    A foreign site that reaches 127.0.0.1 (CORS does not block sending a
    simple request, only reading the response) gets 403 and does not save a
    key. A missing `Origin` is allowed by the same logic as in the platform's
    `middleware.OriginGuard`: a browser always sends it on a state-changing
    request, so the client is not a browser (curl, a smoke test) and is not
    a CSRF vector.

  * **No CORS headers.** Echoing `Access-Control-Allow-Origin` for the
    platform's origin would let a platform page read the report list and the
    state from someone else's computer. No scenario needs that.

  * **A refusal names its reason.** `POST /library/sync` answers not with a
    bare `started` but with a reason, and "no schedule" goes out as code 503.
    An empty `{"started": false}` the page could draw only as the previous
    state: the person pressed the button a second and third time while the
    mirror stayed silent. The same reason is behind `enabled` in `data.json`
    — without that field nobody can say "updating does not work", and the
    page would show "done" for a program that does not sync.

  * **A field we do not know is ABSENT from the response.** `fetched_at` and
    `last_sync_at` do not go out as `null`: `new Date(null)` is a valid date,
    and the page printed "31.12.1969 16:00". The `common.js` input now
    distrusts such a value too, but the check stands on both sides ON PURPOSE:
    a false date on the mirror is the answer to the one question people come
    here with ("how fresh are my numbers"), and one safeguard is not enough.

  * **The address prefix is one per program.** The string "/library" is
    declared in `schedule.library_url` (the icon menu calls it), and here it
    is taken from that same address rather than repeated as a literal: a
    reverse import would be circular, and two literals diverge one day — and
    the icon menu opens a 404.

  * **We enumerate the mirror OURSELVES, in one top-level walk.** See
    `_mirror_rows`: `store.list_mirrored` computes the on-disk size of every
    report with a recursive walk, while the response has no size at all.

  * **An incomplete kit names itself, in the HTML.** If there is no `mirror/`
    directory next to `op_agent.py` (and `setup.bat` does not check for it),
    the page stayed empty and silent: no texts, `fmt` on a missing key gave
    '', and the page swallows the 404 on `strings.json` on purpose. JS cannot
    say it — `common.js` disappears together with the texts. So `_send_page`
    inserts a bar listing the missing files straight into the HTML (the only
    place where we edit the served markup, and the only reason it is
    justified), and when the page itself is missing too, it answers with its
    own, built in code, with no links to `mirror.css`.

  * **The device key is not returned, not logged and does not get into
    data.json** — neither whole nor in part. The response to `POST /library/key`
    holds only a success flag: any text assembled from the input would one
    day turn out to be a string with the key.

Standard library only (agent/ — Python 3.12, stdlib).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import assets, layout, localrun, schedule, serve, store

# Prefix of the shell's addresses. Taken from `schedule.library_url` — from
# the very string the icon menu opens — and not written here as a second
# literal: two entries have no more way to diverge (decision in the docstring).
# The port in this address does not matter, only the path is used.
LIBRARY_PREFIX = urlsplit(schedule.library_url(0)).path.rstrip("/")

# Address → page file in `web/`. Addresses with a trailing slash: the browser
# resolves `/library/common.js` and the pages' other relative links from them.
_PAGES = {
    LIBRARY_PREFIX + "/": "library.html",
    LIBRARY_PREFIX + "/view/": "viewer.html",
}

# Addresses that arrived without a trailing slash get a redirect, not a 404: otherwise
# the page's relative links would go one level up and it would open bare.
_REDIRECTS = {
    LIBRARY_PREFIX: LIBRARY_PREFIX + "/",
    LIBRARY_PREFIX + "/view": LIBRARY_PREFIX + "/view/",
}

# Page resources. A white list, not "any file of web/" — see the docstring.
_ASSETS = frozenset({"common.js", "library.js", "viewer.js", "mirror.css",
                     "icon.svg"})

# Files without which the shell is incomplete. The list is explicit, not "what
# was found": a loss must be NAMED, and for that we must know what was expected.
# Names are as in the kit (next to `op_agent.py`), because that is where the kit gets lost.
_KIT_FILES = ((assets.STRINGS_NAME, False),) + tuple(
    (n, True) for n in ("library.html", "viewer.html", "common.js",
                        "library.js", "viewer.js", "mirror.css", "icon.svg"))

# Text of the incomplete-kit bar. Not in `strings.json` ON PURPOSE: it is about
# `strings.json` not having been read. The tone matches the other strings
# (strings_README): first what is with the reports, then what to do.
_KIT_NOTICE = (
    "The program was not copied completely: {files} not found. Copy the mirror/ "
    "directory from the kit in full, next to op_agent.py. The labels on this "
    "page and the program's texts come from there, so string names are shown "
    "instead of them. Report copies already downloaded are not affected: they "
    "are on disk and open.")

# The bar is drawn without `mirror.css` too: it may be missing from an
# incomplete kit as well, and that is exactly when the bar is needed.
_KIT_STYLE = ("margin:0;padding:12px 16px;font:14px/1.5 system-ui,sans-serif;"
              "background:#fde8e4;color:#5b1a10;border-bottom:1px solid #e0a79c")

# Ceiling on the POST body. The key is one string (the controller cuts it at 512 chars),
# `{"key": …}` cannot exceed a kilobyte; we do not allocate on a promised
# Content-Length.
_MAX_BODY = 8 * 1024

# Sync state kind → the state string understood by
# `web/library.js::renderStatus`. `M_FRESH` is given as `idle` deliberately:
# the page has no separate branch for "fresh", it shows "Copies updated
# …" from `last_sync_at`, and the "Update now" button stays the main one on `idle`.
_STATUS_BY_KIND = {
    schedule.M_IDLE: "idle",
    schedule.M_FRESH: "idle",
    schedule.M_SYNCING: "running",
    schedule.M_UNAVAILABLE: "unavailable",
    schedule.M_NO_KEY: "no_key",
    schedule.M_KEY_EXPIRED: "key_expired",
    schedule.M_KEY_REJECTED: "key_rejected",
    schedule.M_NO_DISK: "no_disk_space",
    schedule.M_QUOTA: "mirror_full",
}


# --------------------------------------------------------------------------- #
# Time                                                                        #
# --------------------------------------------------------------------------- #

def epoch_ms(value: object) -> int | None:
    """ISO string of the state or unix seconds → milliseconds for `new Date()`.

    The page formats time ITSELF (`common.js::dateTime`), i.e. in the browser's
    local zone. It cannot be given an ISO string: mirror time is UTC without a zone
    (`sync._now_iso`), and `new Date("2026-10-11T09:00:00")` in the browser would read
    these digits as LOCAL, i.e. show the copy three hours younger than it
    is. So here UTC is honestly turned into a number.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value * 1000) if value > 0 else None
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        from datetime import UTC
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


# --------------------------------------------------------------------------- #
# data.json                                                                   #
# --------------------------------------------------------------------------- #

def _platform_base(server) -> str | None:
    """Platform address for the "Open on the platform" link. No address — no
    link: a button to nowhere is worse than a missing button (`library.js` is
    written exactly so)."""
    try:
        return schedule.default_server_url(getattr(server, "config", None))
    except Exception:                                     # noqa: BLE001
        return None


def _platform_url(base: str | None, dashboard_id: str) -> str | None:
    if not base:
        return None
    return f"{base}/d/{dashboard_id}"


def _document_on_disk(dashboard_id: str, state: dict) -> bool:
    """Whether the server version's document is on disk.

    The FILE is checked, not a state field: `state.json` is written last, but
    a break between versions leaves a described version without a document, and
    the honest answer to the person is then "no copy yet", not an "Open" button
    that would give a 404 from `serve.py`.
    """
    n = state.get("server_version")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        return False
    try:
        vdir = layout.server_version_dir(dashboard_id, n)
    except ValueError:
        return False
    return (vdir / layout.DOCUMENT_NAME).is_file()


def _slots_public(state: dict) -> list[dict]:
    """The slot inventory in the form `web/viewer.js` reads it: number (also the
    position of `input[type=file]` in the report), original name and `v` for the address.
    The on-disk name, size and ETag are not needed by the page and do not go out.

    It takes both a report state and a local run record: the key is the same in
    both (`slots`), and a second parse of the same inventory would mean two answers to
    the question "which slots does this version have"."""
    out = []
    for rec in state.get("slots") or []:
        if not isinstance(rec, dict):
            continue
        no = rec.get("slot")
        if isinstance(no, bool) or not isinstance(no, int):
            continue
        out.append({
            "slot": no,
            "name": rec.get("name") if isinstance(rec.get("name"), str) else "",
            "v": rec.get("v") if isinstance(rec.get("v"), str) else None,
        })
    return out


def _skipped_item(dashboard_id: str, title: str, reason: str,
                  base: str | None) -> dict:
    """A report that is not in the mirror — with the reason.

    It does not disappear from the list (RFC §4): a report that vanished without
    explanation is thought lost by the person, who goes looking for it with us, not on the platform.
    """
    return {
        "dashboard_id": dashboard_id,
        "title": title,
        "mirrorable": False,
        "skip_reason": reason,
        "platform_url": _platform_url(base, dashboard_id),
    }


def _local_runs_public(state: dict) -> list[dict]:
    """Local runs for the page: number, when built, whether published, and
    the SLOT INVENTORY of this run.

    The inventory must travel its own: `web/viewer.js`, opening `?space=local&n=`,
    fed the report the slots of the SERVER version — that is, old data
    into a fresh document, silently. The fields are the same as in the server inventory
    (`_slots_public`), so the viewer reads them with one code.
    """
    out = []
    for rec in layout.local_runs(state):
        item = {
            "k": rec["k"],
            "published": bool(rec.get("published")),
            "bridge": rec.get("bridge") or "",
            "slots": _slots_public(rec),
        }
        at = epoch_ms(rec.get("created_at"))
        if at is not None:
            item["created_at"] = at
        out.append(item)
    return out


def _mirrored_item(row: dict, base: str | None) -> dict:
    dashboard_id = row.get("dashboard_id") or ""
    state = row.get("state") if isinstance(row.get("state"), dict) else {}
    title = state.get("title") if isinstance(state.get("title"), str) else ""
    rights = state.get("rights") if isinstance(state.get("rights"), dict) else {}

    # `rls_enabled` in the mirror means a corrupted state: such a report
    # cannot be here (ADR-18), and `serve.py` closes its delivery entirely.
    # The shell must answer the same way, otherwise it would draw an "Open" button for
    # a report that will return 404 — and the person would decide the program broke.
    if rights.get("rls_enabled"):
        return _skipped_item(dashboard_id, title, "rls", base)

    folder = state.get("folder")
    item = {
        "dashboard_id": dashboard_id,
        "title": title,
        # An empty string is "No folder", we introduce no separate flag: on the
        # platform that is not a folder either, but a section for everything unassigned.
        "folder": folder if isinstance(folder, str) else "",
        # Weight for tiles. 0 means "we do not know": the manifest has not been seen yet, and
        # there is nothing to put a placeholder instead of a frame for.
        "size": (state.get("size") if isinstance(state.get("size"), int)
                 and not isinstance(state.get("size"), bool) else 0),
        "mirrorable": True,
        "slots": _slots_public(state),
    }
    # "Copy from …": the moment of the last SUCCESSFUL check against the platform, not
    # the server `updated_at` (that answers a different question — when the report was
    # edited, not when we last knew the copy to be correct).
    # No date — no field: the page printed `null` as "31.12.1969".
    fetched_at = epoch_ms(state.get("last_sync_at"))
    if fetched_at is not None:
        item["fetched_at"] = fetched_at

    # Local runs and the catch-up queue. An unpublished run is the main
    # thing that can be said about this report: there are numbers on disk that
    # the platform lacks, and the person must be ASKED about it (owner's decision
    # of 11.10), not left in silence or published by ourselves.
    runs_pub = _local_runs_public(state)
    if runs_pub:
        item["local_runs"] = runs_pub
    pending = [r for r in runs_pub if not r["published"]]
    newest_local = max((r.get("created_at") or 0) for r in runs_pub) if runs_pub else 0
    if newest_local:
        item["local_at"] = newest_local

    # Copy state. Three captions instead of the single "Copy from …", and all three
    # are statements we can prove: `local_only` from the run inventory,
    # `fresh`/`outdated` by comparing what the manifest NAMED in the last
    # pass (`server_seen`) with what actually lies on disk. This is still
    # not "on the platform right now" but "as of the last contact", which is why both
    # captions carry a date.
    if not _document_on_disk(dashboard_id, state):
        item["state"] = "not_downloaded"
    elif pending:
        item["state"] = "local_only"
    else:
        status = layout.platform_status(state)
        if status in ("fresh", "outdated"):
            item["state"] = status
        # `unknown` sets no field: the manifest has not been seen yet, and we have
        # nothing to say except "Copy from <date>".

    # Whether it can be updated from folders right now. The button is drawn only on
    # `can`: a button that answers with a refusal is worse than a missing one. The price of
    # the question is ONE `stat` per report (is the bundle in the cache) on top of what
    # `_mirror_rows` computes; `precheck` does no directory walks.
    kind, why = localrun.precheck(dashboard_id, state)
    update = {"can": kind == "ok", "reason": "" if kind == "ok" else kind}
    if kind != "ok":
        # The agent's text is a fallback: the label for a known code lies in
        # strings.json, and for rare codes (folders.ini refusals) inventing
        # a second wording would diverge from what the run will show.
        update["why"] = why
    item["update"] = update
    return item


def _mirror_rows() -> list[dict]:
    """The mirror inventory for ONE response: `{dashboard_id, state}` per report.

    Not `store.list_mirrored()`, and the reason is the price. It computes for every report the
    actual on-disk size with a recursive walk (`os.walk` + `stat` per
    file), while `data.json` has no size at all: measured 41 ms on 4,560
    files and 122 ms on 13,560, linearly. It is paid on the same disk the
    synchronizer is writing to right now, and on every page poll — and
    during an update it polls `data.json` every 1.5 s. On top of that
    `list_mirrored` was called TWICE: for the list and for the rights banner.

    There must be no walk of our own here, and this one is temporary: when `store`
    gets an enumeration without size, this function goes away entirely, and the call
    becomes a single line. For now the walk is top-level only — `iterdir` plus
    reading `state.json` — and not a single `stat` beyond that.
    """
    try:
        entries = sorted(layout.mirror_root().iterdir(), key=lambda p: p.name)
    except OSError:
        # The mirror directory does not exist yet (no sync at all) — not an error.
        return []
    out: list[dict] = []
    for p in entries:
        try:
            if not p.is_dir():
                continue
        except OSError:                                   # pragma: no cover
            continue
        if not layout.is_valid_dashboard_id(p.name):
            continue
        # A report without a readable state.json is in the list too, with `{}` — so "the directory
        # exists, the inventory is lost" is visible rather than masked as "no such report".
        out.append({"dashboard_id": p.name, "state": store.load_state(p.name)})
    return out


def items(base: str | None, rows: list[dict] | None = None) -> list[dict]:
    """The report list for the library: what lies in the mirror plus what is
    not in it and why (`store.blocked`).

    `rows` is the already read inventory (`_mirror_rows`): `data()` reads it once
    for the whole response and passes it here, so the disk walk is not repeated.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for row in (_mirror_rows() if rows is None else rows):
        item = _mirrored_item(row, base)
        if item["dashboard_id"]:
            seen.add(item["dashboard_id"])
            out.append(item)
    for dashboard_id, rec in store.blocked().items():
        if dashboard_id in seen or not layout.is_valid_dashboard_id(dashboard_id):
            continue
        title = rec.get("title") if isinstance(rec.get("title"), str) else ""
        reason = rec.get("reason") if isinstance(rec.get("reason"), str) else ""
        out.append(_skipped_item(dashboard_id, title, reason, base))
    # Sorted by title: a person reads the list, and id order is
    # random to them. The id is the second key — so the order does not jump among equal titles.
    out.sort(key=lambda i: ((i.get("title") or "").lower(), i["dashboard_id"]))
    return out


def _worker(server):
    """The snapshot of the sync state is taken from MirrorWorker through the controller:
    the shell has no schedule of its own and must not have one."""
    ctl = getattr(server, "controller", None)
    return getattr(ctl, "mirror", None) if ctl is not None else None


def _has_key(server) -> bool:
    worker = _worker(server)
    if worker is not None:
        return bool(worker.has_key())
    creds = getattr(server, "cred_store", None)
    try:
        return bool(creds is not None and creds.has_password(schedule.KEY_NAME))
    except Exception:                                     # noqa: BLE001
        return False


def sync_block(server) -> dict:
    """The `sync` block for data.json — exactly the fields that
    `web/library.js::renderStatus`."""
    worker = _worker(server)
    snap = worker.snapshot() if worker is not None else None
    if not snap:
        # No schedule at all (the icon did not come up — dev/headless, and then
        # `__main__` does not set a controller). Lying "updating" is not allowed; honestly:
        # there is a key or there is not, and we know nothing else.
        has_key = _has_key(server)
        snap = {"enabled": False, "has_key": has_key,
                "kind": schedule.M_IDLE if has_key else schedule.M_NO_KEY}
    status = _STATUS_BY_KIND.get(snap.get("kind"), "idle")
    out = {
        # Whether the schedule works. The field was in the snapshot and was dropped here — and
        # without it the page showed "idle" and an "Update now" button
        # that silently does nothing: there is no worker (headless) or it has
        # no synchronizer (`MirrorWorker.enabled` is false). "Updating does not
        # work" is the only honest word in this state.
        "enabled": bool(snap.get("enabled")),
        "status": status,
        "has_key": bool(snap.get("has_key")),
        "done": int(snap.get("done") or 0),
        "total": int(snap.get("total") or 0),
    }
    last_sync_at = epoch_ms(snap.get("last_ok"))
    if last_sync_at is not None:
        out["last_sync_at"] = last_sync_at
    if status == "running" and snap.get("bytes"):
        # A ready human-readable size, like `missing`: a second byte formatter
        # in JS would diverge from this one at the first gigabyte.
        out["downloaded"] = schedule.fmt_size(snap.get("bytes"))
    if status == "unavailable":
        out["next_retry_min"] = schedule.retry_minutes(snap.get("retry_in_s"))
    if status in ("no_disk_space", "mirror_full"):
        # A ready human-readable size: the page substitutes it into the text as is,
        # and a second byte formatter in JS would diverge from this.
        out["missing"] = schedule.fmt_size(snap.get("needed_bytes"))
    return out


def _rights_blocked(rows: list[dict]) -> bool:
    """The "rights not confirmed since …" banner: the right was brought, but it has expired —
    local updating is blocked, READING is not (RFC §5). We ask
    `layout` rather than count dates here: the rule is one and lives there."""
    for row in rows:
        state = row.get("state") if isinstance(row.get("state"), dict) else {}
        rights = state.get("rights") if isinstance(state.get("rights"), dict) else {}
        if rights.get("can_update_data") and layout.rights_expired(state):
            return True
    return False


def data(server) -> dict:
    """The mirror state in one response."""
    base = _platform_base(server)
    rows = _mirror_rows()                 # one walk for the whole response
    return {
        "items": items(base, rows),
        "sync": sync_block(server),
        "rights_blocked": _rights_blocked(rows),
        "key_ttl_days": schedule.DEFAULT_TTL_DAYS,
        "local_run": local_run_block(),
    }


def local_run_block() -> dict:
    """Offline run progress for the page.

    A run goes for minutes, while `POST /library/local-run` answers at once, otherwise
    the connection would be held for the whole run and "not responding" would be
    indistinguishable from "calculating". So the progress must arrive with the next poll
    of `data.json` — and it arrives from here, from one record per program (`localrun.PROGRESS`).
    """
    snap = localrun.PROGRESS.snapshot()
    if not snap:
        return {"running": False}
    out = {
        "running": bool(snap.get("running")),
        "dashboard_id": snap.get("dashboard_id") or "",
        "stage": snap.get("stage") or "",
        "msg": snap.get("msg") or "",
        "done": int(snap.get("done") or 0),
        "total": int(snap.get("total") or 0),
    }
    if not out["running"]:
        out["ok"] = bool(snap.get("ok"))
        out["k"] = snap.get("k")
        if not out["ok"]:
            out["reason"] = snap.get("error_kind") or ""
            out["why"] = snap.get("error") or ""
        at = snap.get("finished_at")
        if isinstance(at, (int, float)) and at > 0:
            out["finished_at"] = int(at * 1000)
    return out


# --------------------------------------------------------------------------- #
# Key intake                                                                  #
# --------------------------------------------------------------------------- #

def key_looks_usable(key: object) -> bool:
    """Whether this looks like a device key.

    The same rules as in `controller.set_device_key`: one string of printable
    ASCII without spaces, no longer than 512. The check is repeated here and not
    entrusted to the controller, because there may be no controller (the icon did not
    come up), and saving a key with a trailing newline means getting
    "the platform did not accept the key" an hour later and looking for the cause elsewhere. Format and
    prefix are not imposed: they are not ours.
    """
    return (isinstance(key, str) and bool(key) and len(key) <= 512
            and key.isascii() and key.isprintable()
            and not any(c.isspace() for c in key))


def accept_key(server, key: str) -> bool:
    """Save the key and nudge the sync. True — saved.

    Through the controller if there is one: `key_changed()` lives there too, without which
    the previous "key not accepted" verdict would stay in force until the next hour. Without
    a controller we write straight to the store — otherwise the library in headless mode
    would accept the key into nothing.
    """
    ctl = getattr(server, "controller", None)
    if ctl is not None and hasattr(ctl, "set_device_key"):
        # Compare with the success text, not parse the message: the controller
        # returns a HUMAN string, and retelling it to the page is not allowed
        # (nothing assembled from the input may go into the response).
        return ctl.set_device_key(key) == schedule.text("key_added")
    creds = getattr(server, "cred_store", None)
    if creds is None:
        return False
    try:
        creds.set_password(schedule.KEY_NAME, key)
    except Exception:                                     # noqa: BLE001
        # The caller already knows the exception type; we do not pull the text here — it might
        # contain the key itself, from the store.
        return False
    return True


# --------------------------------------------------------------------------- #
# Responses                                                                   #
# --------------------------------------------------------------------------- #

def _headers(handler, extra: dict | None = None) -> None:
    # All on loopback and everything changes under one's hands: a cache here saves microseconds
    # and muddies the picture. nosniff — so that the table's type has the last word.
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    for key, value in (extra or {}).items():
        handler.send_header(key, value)


def _reply(handler, status: int, obj: dict, extra: dict | None = None) -> None:
    """The shell's JSON response.

    Not named `_json`, although `serve.py` next door has exactly such a function: the
    merge into the single-file `op_agent.py` puts all modules in one namespace and
    renames the namesake itself (`_json__libui`). It works, but the build becomes harder
    to read and riskier to edit (`agent/README.md`, discrepancy 17), and
    cheapest of all is not to create the collision.
    """
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    _headers(handler, extra)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def _not_found(handler) -> None:
    _reply(handler, 404, {"error": "not_found"})


def _send_bytes(handler, body: bytes, name: str) -> None:
    """Send the resource's ready bytes, the type by name (`serve.mime_for`)."""
    handler.send_response(200)
    handler.send_header("Content-Type", serve.mime_for(name))
    handler.send_header("Content-Length", str(len(body)))
    _headers(handler)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def _send_file(handler, path: Path, name: str) -> None:
    """Send a shell resource whole. The files here are a page and a script, tens of
    kilobytes; streaming in chunks (as in `serve.py`) saves nothing here."""
    try:
        body = path.read_bytes()
    except OSError:
        _not_found(handler)
        return
    _send_bytes(handler, body, name)


def missing_kit() -> tuple[str, ...]:
    """Kit files that are under no root (`assets.res_path`).

    An empty tuple — the kit is intact. Checked on every page request, not
    once at start: a person copies the directory NEXT TO an already running
    program, and the "missing" answer must stop appearing by itself.
    """
    out = []
    for name, web in _KIT_FILES:
        if assets.res_path(name, web=web) is None:
            out.append(f"mirror/web/{name}" if web else f"mirror/{name}")
    return tuple(out)


def _notice_html(missing: tuple[str, ...]) -> bytes:
    """The "incomplete kit" bar. Names are taken from `_KIT_FILES`, i.e. from
    code, not from the request — there is nothing to escape here."""
    text = _KIT_NOTICE.format(files=", ".join(missing))
    return (f'<div id="kit-missing" class="notice bad" role="alert" '
            f'style="{_KIT_STYLE}">{text}</div>').encode("utf-8")


def _inject_notice(body: bytes, missing: tuple[str, ...]) -> bytes:
    """Insert the bar right after `<body…>` — that is, BEFORE any script: it
    must be visible even when no scripts turned out to be on disk."""
    note = _notice_html(missing)
    start = body.lower().find(b"<body")
    end = body.find(b">", start) if start >= 0 else -1
    if end < 0:
        return note + body            # not our markup — the bar is first anyway
    return body[:end + 1] + b"\n" + note + body[end + 1:]


def _kit_page(handler, missing: tuple[str, ...]) -> None:
    """The answer when even the page itself is missing: a kit without the `mirror/` directory.

    503, not 404: the address is right, the program is missing — and a 404 with a faceless
    JSON was read by the person as "there is no such page". The markup is assembled entirely here
    and without links to `mirror.css`: it is missing too.
    """
    body = (b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            b'<title>OnePage</title></head><body>'
            + _notice_html(missing) + b'</body></html>')
    handler.send_response(503)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    _headers(handler)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def _send_page(handler, name: str) -> None:
    """Send a shell page, and for an incomplete kit — say so."""
    missing = missing_kit()
    path = assets.res_path(name, web=True)
    body = None
    if path is not None:
        try:
            body = path.read_bytes()
        except OSError:
            body = None
    if body is None:
        _kit_page(handler, missing or (f"mirror/web/{name}",))
        return
    if missing:
        body = _inject_notice(body, missing)
    _send_bytes(handler, body, name)


def _resource(handler, name: str, *, web: bool) -> None:
    """Name → file through `assets.res_path`. There is no path joining of our own here
    ON PURPOSE: a single `join` on a network string is reading any file on
    the person's machine over loopback."""
    path = assets.res_path(name, web=web)
    if path is None:
        _not_found(handler)
        return
    _send_file(handler, path, name)


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #

def _same_origin(handler) -> bool:
    """The request came from our own page (or from outside a browser) — see the docstring."""
    origin = (handler.headers.get("Origin") or "").strip()
    if not origin:
        return True
    try:
        port = int(handler.server.server_address[1])
    except (AttributeError, IndexError, TypeError, ValueError):
        return False
    return origin.lower() in (f"http://127.0.0.1:{port}",
                              f"http://localhost:{port}",
                              f"http://[::1]:{port}")


class _BodyRefused(Exception):
    """The POST body was not read, and each reason has its own response code.

    `Content-Length: abc` and `-5` are request syntax (400), while a declared
    size above the ceiling is 413. A shared 413 for both cases would send debugging
    the wrong way: people would look for a big body that does not exist at all.
    """

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def _read_body(handler) -> bytes:
    """The POST body. An unusable or too large `Content-Length` is
    `_BodyRefused`, and the body is NOT read then (we do not allocate on a promise).

    `isascii()` next to `isdigit()` is not for show: `isdigit()` is true for
    "²" too (which is printable latin-1, so a header with such a sign gets through), and
    `int("²")` fails — and a broken header would become a 500 error of the shell.
    """
    raw = (handler.headers.get("Content-Length") or "0").strip()
    if not (raw.isascii() and raw.isdigit()):
        raise _BodyRefused(400, "bad_content_length")
    n = int(raw)
    if n > _MAX_BODY:
        raise _BodyRefused(413, "body_too_large")
    return handler.rfile.read(n) if n else b""


def _sync_now(handler) -> None:
    """Start an update by the button — or say WHY it cannot.

    `{"started": false}` without a reason was a silent refusal: the page
    redrew the same state, and the person pressed the button again. Now a
    refusal has a reason, and "no schedule" also has code 503: a 200 answer to
    an action that did not happen lies twice.

    `enabled` is asked BEFORE `sync_now()` ON PURPOSE: that one sets an event and
    returns True even when the thread never came up (there is no synchronizer), that
    is, it answers "started" about something it did not start.
    """
    worker = _worker(handler.server)
    snap = worker.snapshot() if worker is not None else None
    if worker is None or not (snap or {}).get("enabled"):
        _reply(handler, 503, {"started": False, "reason": "no_schedule"})
        return
    if not worker.sync_now():
        # Already running: a second parallel one would write to the same `.tmp` and show
        # a jumbled counter. It is not a program failure, and the page will say
        # "updating copies…", not "cannot".
        _reply(handler, 200, {"started": False, "reason": "already_running"})
        return
    _reply(handler, 200, {"started": True})


def _get(handler, path: str) -> None:
    target = _REDIRECTS.get(path)
    if target:
        handler.send_response(307)
        handler.send_header("Location", target)
        handler.send_header("Content-Length", "0")
        _headers(handler)
        handler.end_headers()
        return
    page = _PAGES.get(path)
    if page:
        _send_page(handler, page)
        return
    if path == LIBRARY_PREFIX + "/strings.json":
        _resource(handler, assets.STRINGS_NAME, web=False)
        return
    if path == LIBRARY_PREFIX + "/data.json":
        _reply(handler, 200, data(handler.server))
        return
    name = path[len(LIBRARY_PREFIX) + 1:]
    if name in _ASSETS:
        _resource(handler, name, web=True)
        return
    # There was no exact match: the directory is not listed, nothing similar is searched.
    _not_found(handler)


def _local_run(handler, body: bytes) -> None:
    """Start an offline run by the "Update from folders" button.

    It is started by the SERVER (`AgentServer.start_local_run`), not the shell:
    the run lock is one per program — the same one with which the OnePage
    page occupies the agent — and the folder roots for `input_manifest` are computed
    by `server.folders_payload`. The shell here only passes the request on and
    names the refusal.
    """
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        _reply(handler, 400, {"error": "bad_json"})
        return
    if not isinstance(payload, dict):
        _reply(handler, 400, {"error": "bad_json"})
        return
    dashboard_id = str(payload.get("dashboard_id") or "").strip()
    scenario = str(payload.get("scenario") or "").strip()
    if not layout.is_valid_dashboard_id(dashboard_id):
        _reply(handler, 400, {"started": False, "reason": "bad_dashboard"})
        return
    starter = getattr(handler.server, "start_local_run", None)
    if starter is None:
        # The shell was raised without an agent server (that is how tests run it) — saying
        # "started" would be a lie.
        _reply(handler, 503, {"started": False, "reason": "no_agent"})
        return
    kind, why = starter(dashboard_id, scenario)
    if kind != "started":
        # 200, not an error: the refusal is named by a code, and the page draws the reason
        # itself. The codes are the same as in `localrun.precheck`.
        _reply(handler, 200, {"started": False, "reason": kind, "why": why})
        return
    _reply(handler, 200, {"started": True})


def _post(handler, path: str) -> None:
    if path == LIBRARY_PREFIX + "/local-run":
        if not _same_origin(handler):
            _reply(handler, 403, {"error": "bad_origin"})
            return
        try:
            _local_run(handler, _read_body(handler))
        except _BodyRefused as refused:
            handler.close_connection = True
            _reply(handler, refused.status, {"error": refused.code})
        return
    if path not in (LIBRARY_PREFIX + "/sync", LIBRARY_PREFIX + "/key"):
        _not_found(handler)
        return
    if not _same_origin(handler):
        _reply(handler, 403, {"error": "bad_origin"})
        return
    try:
        body = _read_body(handler)
    except _BodyRefused as refused:
        # The declared body was left unread in the socket: the next request on
        # this connection would be parsed from our bytes and get an obscure
        # error instead of an answer. We close the connection ourselves.
        handler.close_connection = True
        _reply(handler, refused.status, {"error": refused.code})
        return

    if path == LIBRARY_PREFIX + "/sync":
        _sync_now(handler)
        return

    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        # The body text goes neither into the response nor into the log: it contains the key.
        _reply(handler, 400, {"error": "bad_json"})
        return
    key = payload.get("key") if isinstance(payload, dict) else None
    try:
        if not key_looks_usable(key):
            _reply(handler, 400, {"error": "key_rejected"})
            return
        ok = accept_key(handler.server, key)
    finally:
        del key, payload, body            # do not hold the key in the frame longer than needed
    if not ok:
        _reply(handler, 500, {"error": "key_not_saved"})
        return
    _reply(handler, 200, {"ok": True})


def serve_library(handler) -> bool:
    """Handle a shell request. True — answered, False — not our address.

    Exceptions are not let out, for the same reason as in `serve.serve`:
    a failure here would leave the connection without an answer, and the library would hang on
    an empty page. The exception text goes into the log without the request body — the body
    may hold the key.
    """
    path = handler.path.split("?", 1)[0].split("#", 1)[0]
    if path != LIBRARY_PREFIX and not path.startswith(LIBRARY_PREFIX + "/"):
        return False
    if handler.command not in ("GET", "HEAD", "POST"):
        _reply(handler, 405, {"error": "method_not_allowed"},
              extra={"Allow": "GET, HEAD, POST"})
        return True
    try:
        if handler.command == "POST":
            _post(handler, path)
        else:
            _get(handler, path)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass                              # the tab was closed — nobody to answer
    except Exception as exc:                                  # noqa: BLE001
        print(f"[library] 500 {type(exc).__name__} on {path}")
        _reply(handler, 500, {"error": "library_failed"})
    return True
