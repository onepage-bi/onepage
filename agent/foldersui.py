"""The "Report folders" screen in the agent window: pages and `/folders-ui/...` handlers.

Why it exists. Today a person edits `folders.ini` in Notepad. The owner
(12.10): "we need a proper app on the computer, not a sucker-style agent",
"let the paths go to the platform so the girls at least do not get confused;
otherwise it starts every time, one department in one excel, another in
another". Notepad cannot do the main thing: say that the path a person wrote
DOES NOT EXIST, and say it at the moment they write it, not on a run a week
later.

The structure is copied from the mirror shell (`agent/mirror/libui.py` +
`agent/mirror/web/`) ON PURPOSE: the same trust boundary, the same rules for
serving files, the same Origin check, the same trick "pages lie as files
alongside, the bundling carries only code". A second way of doing the same
thing would be a second way of making a mistake.

Addresses (the `agent/web/*` pages expect them, so this is a CONTRACT):

    GET  /folders-ui/              web/folders.html
    GET  /folders-ui/folders.js | folders.css | strings.json
    GET  /folders-ui/state.json    the whole screen in one response
    POST /folders-ui/probe         {path}            what lies at the path RIGHT NOW
    POST /folders-ui/check         {key}             recompute resolve for one section
    POST /folders-ui/pick          {kind}            system dialog for choosing a path
    POST /folders-ui/entry/add     {key, kind, raw}
    POST /folders-ui/entry/remove  {key, index}
    POST /folders-ui/masks         {key, include?, exclude?}
    POST /folders-ui/section       {dashboard_id, scenario?}
    POST /folders-ui/scenario      {key, scenario}   copy of a section under a new name
    POST /folders-ui/s3/preview    {name, host, port, tls, remote, …}
    POST /folders-ui/s3/save       {…the same + access_key, target, secret?}
    POST /folders-ui/s3/secret     {name, secret}    DPAPI only, not in the ini
    POST /folders-ui/s3/forget     {name}            forget the secret
    POST /folders-ui/s3/remove     {name}            remove the connection from the ini
    POST /folders-ui/s3/test       {name}            a real listing, without downloading
    POST /folders-ui/s3/pull       {name}            download now (the same road as a run)
    GET  /folders-ui/s3/progress   progress and result of downloads (polled while running)

Decisions, each with a reason
--------------------------

* **Choosing a folder is by TEXT, not by a system dialog.** The kit has no
  third-party packages, and there is nothing to get the standard dialog from:
  `tkinter` is absent in the portable CPython build the kit carries (the
  embeddable package ships without Tcl/Tk), `pywin32` is a wheel, and the kit
  is offline. What remains is the browser `<input webkitdirectory>`, but it
  gives FILE NAMES, not the folder path (the browser fundamentally does not
  reveal the path), i.e. exactly what the app is moving away from. So the path
  is typed as text, and honesty is restored by `POST /folders-ui/probe`: it
  checks existence ON EVERY CHANGE of the field and, on a button, shows what
  was found in that folder. This is worse than a mouse and better than
  silence.

* **Writing goes through `foldersio`, not here.** This module accepts the
  request, checks the boundary and calls one function. The writing rules
  (atomicity, comments, refusals) live in one file and are checked by one test
  without HTTP.

* **`folders.py` is not touched AT ALL.** We read with the same code the run
  reads with: the display and the run must not diverge
  (`server.folders_payload`, docstring).

* **The response to any edit is the FULL screen state.** The page does not
  stitch its view together from pieces: an edit returned the state, the page
  redrew. Otherwise the first edit that went through halfway (the ini written,
  the disk already different) would leave on screen a picture that exists
  nowhere.

* **The content of `folders.ini` is not written to the log.** A path is a
  personal thing: it names a department, a project and a surname in the name
  of a network share. There is not a single `print` or `applog` in this
  module, and that is checked by reading the file.

* **POST is protected as in `libui`: Host is loopback (done by the agent's
  `_host_ok`), Origin is our own loopback origin with our port.** This page
  has no pairing Bearer token and cannot have one: the token is issued to the
  PLATFORM PAGE at pairing, while this is our own page opened by the agent
  window. A missing `Origin` is allowed by the same logic as in the
  platform's `middleware.OriginGuard`: a browser always sends it on a
  state-changing request, so the client is not a browser (curl, a smoke), and
  it is not a CSRF vector. There are no CORS headers: a foreign page must not
  read either the paths or the response.

* **A time budget for walking the disk.** A network share answers in seconds,
  and there may be a dozen sections: without a limit the screen would simply
  not open. Sections are computed in turn up to `BUDGET_S`, the uncomputed
  ones arrive with `checked: false`, and the page says so out loud instead of
  drawing "0 files".

Standard library only (`agent/README.md`).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import appwindow, folders, foldersio, s3ui

# Prefix of the screen's addresses. One literal per program: the window opens
# the address via `appwindow.FOLDERS_PATH`, and the cross-check stands RIGHT
# HERE, at import. The two entries cannot drift apart: a mismatch stops the
# agent's start with a ready explanation instead of turning into a window that
# opens a 404 for the person.
PREFIX = "/folders-ui"
assert appwindow.FOLDERS_PATH == PREFIX + "/", (
    "appwindow.FOLDERS_PATH and foldersui.PREFIX diverged: "
    f"{appwindow.FOLDERS_PATH!r} versus {PREFIX + '/'!r}")

# The resource directory next to `op_agent.py` in the kit and `agent/web/` in
# the repository. The names are deliberately NOT `RES_DIRNAME`/`WEB_DIRNAME`
# as in the mirror: the single-file bundling puts all modules into one flat
# namespace, and two identical names there are resolved by renaming, i.e. the
# bundle tail (`_kit_problems`) would stop seeing the one it needs. Two
# parties know the name, this module and `deploy/build_agent_single.py`; it
# must be changed in both places at once.
FOLDERS_RES_DIRNAME = "appwin"
FOLDERS_WEB_DIRNAME = "web"

# Pages served at an address with a slash: the browser resolves from it
# `/folders-ui/folders.js`.
_PAGES = {PREFIX + "/": "folders.html"}
_REDIRECTS = {PREFIX: PREFIX + "/"}

# Whitelist of resources. Not "any file in web/": sooner or later the directory
# acquires a file that was not meant to be served.
_ASSETS = frozenset({"folders.js", "folders.css", "strings.json"})

# Resource name: the same white alphabet as the mirror's. Repeated here rather
# than imported from `agent/mirror/assets.py`, because the mirror is another
# half of the program with its own resource directory, and tying them together
# means fixing one while breaking the other.
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")

# Ceiling on the POST body. The biggest body here is the list of masks: a
# hundred masks of 128 characters. Kilobytes, not megabytes.
MAX_BODY = 64 * 1024

# How many seconds the screen may walk the disk for one response. More and the
# window looks hung; less and no network share manages in time.
BUDGET_S = 3.0

# How many names to show in the `probe` response. The person needs to be sure
# it is the right folder, not to read it whole.
PROBE_NAMES = 20

_MIME = {".html": "text/html; charset=utf-8",
         ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8",
         ".json": "application/json; charset=utf-8"}


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #

def _roots() -> tuple[Path, ...]:
    """Candidate directories: the package (development) and the `op_agent.py` directory (kit).

    The single-file bundling carries only CODE, so in the kit `__file__`
    points to `op_agent.py` itself, and the pages lie alongside in
    `appwin/web/`. Searching a list rather than computing is the only way that
    works in both cases (the same trick as in `mirror/assets.py`).
    """
    import sys
    here = Path(__file__).resolve().parent
    out: list[Path] = [here, here / FOLDERS_RES_DIRNAME]
    try:
        out.append(Path(sys.argv[0]).resolve().parent / FOLDERS_RES_DIRNAME)
    except (OSError, IndexError, ValueError):   # pragma: no cover
        pass
    return tuple(out)


def win_res_path(name: str) -> Path | None:
    """Path to the resource `web/<name>` or None.

    The name comes from the ADDRESS, so it is checked against a white alphabet
    rather than normalised: one missed `..` here is a read of any file on the
    person's machine over loopback.
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return None
    for root in _roots():
        p = root / FOLDERS_WEB_DIRNAME / name
        try:
            if p.is_file():
                return p
        except OSError:                         # pragma: no cover
            continue
    return None


def window_missing_kit() -> tuple[str, ...]:
    """Which screen files were not found. An empty tuple: the set is intact."""
    want = ("folders.html",) + tuple(sorted(_ASSETS))
    return tuple(n for n in want if win_res_path(n) is None)


# --------------------------------------------------------------------------- #
# Screen state
# --------------------------------------------------------------------------- #

def ini_path() -> str:
    """Absolute path to `folders.ini`: the same as the run's and `GET /folders`."""
    return os.path.abspath(str(folders.default_ini_path()))


def _entry_state(raw: str) -> str:
    """What lies at the entry path RIGHT NOW: `dir` | `file` | `missing` | `denied`.

    Separate from `resolve`, because it answers a different question: resolve
    says "nothing fell under the masks" (`no_match`) where the folder exists
    and is readable, while a person editing an entry first needs to know
    whether the folder itself exists.
    """
    norm = folders._norm(raw)
    try:
        os.stat(norm)
    except PermissionError:
        return "denied"
    except OSError:
        return "missing"
    return "dir" if os.path.isdir(norm) else "file"


def _section_payload(src, deadline: float, masks: dict) -> dict:
    """One section for the screen. The disk is walked only while there is budget.

    The masks come as a separate argument and are not taken from
    `src.entries[0]`: `folders.load` hangs them on EVERY entry, and for a
    section without entries they would vanish from the screen altogether, so
    the person would edit a mask that "does not exist" and not understand why
    the folder added next is not read in full.
    """
    include, exclude = masks.get("include", []), masks.get("exclude", [])
    out = {
        "key": src.key,
        "dashboard_id": src.dashboard_id,
        "scenario": src.scenario,
        "include": include,
        "exclude": exclude,
        "entries": [{"index": i, "kind": e.kind, "raw": e.raw}
                    for i, e in enumerate(src.entries)],
        "checked": False,
        "files": 0,
        "bytes": 0,
        "missing": [],
        "duplicates": [],
    }
    if time.monotonic() >= deadline:
        # Not "zero files" but "not counted". The difference is whether the
        # person goes looking for a problem that does not exist.
        return out
    for item in out["entries"]:
        item["state"] = _entry_state(item["raw"])
    res = folders.resolve(src)
    by_name: dict[str, list[str]] = {}
    for f in res.files:
        by_name.setdefault(f.name, []).append(f.path)
    out.update({
        "checked": True,
        "files": len(res.files),
        "bytes": sum(f.size for f in res.files),
        "missing": [{"raw": m.raw, "reason": m.reason} for m in res.missing],
        "duplicates": [{"name": n, "paths": p}
                       for n, p in by_name.items() if len(p) > 1],
    })
    return out


def ui_state(*, only: str = "", budget_s: float = BUDGET_S) -> dict:
    """Everything the screen draws, in one dict.

    `only` recomputes the disk ONLY for this section (the "Check" button): the
    others arrive with `checked: false`, and the whole budget goes to the one
    that was asked about.
    """
    path = ini_path()
    out: dict = {"ini": path, "exists": os.path.isfile(path),
                 "problem": None, "writable": True,
                 "comments_preserved": True, "sections": [], "partial": False}
    try:
        text = foldersio.read_text(path)
    except foldersio.IniError as e:
        # The file exists but cannot be read (too large, not UTF-8). Writing
        # into such a file is not allowed, and the person must be told before
        # they press "Add".
        out.update({"problem": e.code, "writable": False,
                    "comments_preserved": False})
        return out
    if text.strip():
        try:
            foldersio.assert_writable(text)
        except foldersio.IniError as e:
            out.update({"problem": e.code, "writable": False,
                        "comments_preserved": False})
    # A duplicate header is read by `folders.load` as one section but edited as
    # two, and writing into such a file is refused (`foldersio.section_of`).
    # We say it in advance, not with a refusal on the button.
    parsed = foldersio.parse(text)
    keys = [s.key for s in parsed]
    dups = sorted({k for k in keys if keys.count(k) > 1})
    if dups:
        out.update({"problem": "duplicate_section", "writable": False,
                    "duplicate_keys": dups})
    out["comments_preserved"] = foldersio.will_preserve_comments(text)

    # Masks come from the line-by-line parse, not from `folders.load` (see
    # `_section_payload`). The first section with this key: with a duplicate
    # header writing is impossible anyway, and showing the first is enough.
    masks: dict[str, dict] = {}
    for sec in parsed:
        if sec.key in masks:
            continue
        masks[sec.key] = {
            k: [m.strip() for v in (sec.option(k).values if sec.option(k) else ())
                for m in v.text.split(",") if m.strip()]
            for k in foldersio.MASK_KEYS}

    sources = folders.load(path)
    deadline = time.monotonic() + max(0.0, float(budget_s))
    for key in sorted(sources, key=lambda k: (k.split(":")[0], k)):
        src = sources[key]
        mk = masks.get(key, {})
        if only and key != only:
            out["sections"].append(_section_payload(src, 0.0, mk))
            continue
        out["sections"].append(_section_payload(src, deadline, mk))
    out["partial"] = any(not s["checked"] for s in out["sections"])
    # Buckets live in ANOTHER file (`agent.ini`) and arrive under a separate
    # key: one response per edit means one screen state, otherwise the page
    # would stitch a picture from two requests and show a half-stale one.
    out["s3"] = s3ui.sources_state()
    return out


def probe_path(raw: object) -> dict:
    """What lies at the path RIGHT NOW: the answer to the "Show what was found" button.

    Replaces the system folder-picker dialog that the kit does not have (see
    the module header): the person types a path and the program immediately
    shows whether it is the right folder. Masks are deliberately NOT applied
    here: the question "what lies in the folder at all" and the question "what
    of it will the generator read" are different, and the second is answered
    by the "Check" button via `folders.resolve`.
    """
    try:
        value = foldersio.check_raw(raw)
    except foldersio.IniError as e:
        return {"ok": False, "error": e.code, "detail": e.detail}
    norm = folders._norm(value)
    out: dict = {"ok": True, "raw": value, "path": norm}
    try:
        st = os.stat(norm)
    except PermissionError:
        return dict(out, state="denied")
    except OSError:
        return dict(out, state="missing")
    if not os.path.isdir(norm):
        return dict(out, state="file", size=int(st.st_size),
                    mtime_ms=int(st.st_mtime * 1000))
    names: list[str] = []
    total = 0
    try:
        with os.scandir(norm) as it:
            for de in it:
                try:
                    if de.is_dir():
                        continue
                except OSError:                          # pragma: no cover
                    continue
                total += 1
                if len(names) < PROBE_NAMES:
                    names.append(de.name)
    except OSError:
        return dict(out, state="denied")
    names.sort(key=str.casefold)
    return dict(out, state="dir", count=total, names=names,
                truncated=total > len(names),
                root_id=folders.identity(norm))


# --------------------------------------------------------------------------- #
# Edits
# --------------------------------------------------------------------------- #

def _key(body: dict) -> str:
    key = body.get("key")
    if not isinstance(key, str) or not key.strip():
        raise foldersio.IniError("missing_key")
    return key.strip()


def apply_action(action: str, body: dict) -> dict:
    """Perform an edit and return the FRESH screen state.

    One door for all edits: the file-fitness check and the atomic write live
    in `foldersio.apply`, and here there is only the parsing of the body.
    `IniError` exceptions are caught by the caller and turned into a code for
    the page.
    """
    path = ini_path()
    if action == "entry/add":
        key, kind, raw = _key(body), body.get("kind"), body.get("raw")
        foldersio.apply(path, lambda t: foldersio.add_entry(t, key, kind, raw))
    elif action == "entry/remove":
        key, index = _key(body), body.get("index")
        foldersio.apply(path, lambda t: foldersio.remove_entry(t, key, index))
    elif action == "masks":
        key = _key(body)
        inc, exc = body.get("include"), body.get("exclude")
        foldersio.apply(path, lambda t: foldersio.set_masks(t, key, inc, exc))
    elif action == "scenario":
        key, name = _key(body), body.get("scenario")
        foldersio.apply(path, lambda t: foldersio.copy_section(t, key, name))
    elif action == "section":
        dash, scen = body.get("dashboard_id"), body.get("scenario") or ""
        if not Path(path).exists():
            # The file itself does not exist yet: the first section creates it
            # too. `assert_writable` lets empty text pass, `rebuild` will
            # assemble a file from a single header line.
            foldersio.write_text(path, "")
        foldersio.apply(path, lambda t: foldersio.create_section(t, dash, scen))
    else:                                                # pragma: no cover
        raise foldersio.IniError("unknown_action", action)
    return ui_state()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _headers(handler, status: int, ctype: str, length: int) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(length))
    # The screen shows the state of the disk: caching it would mean showing
    # yesterday's folders one day. There are no CORS headers on purpose (see
    # the header).
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()


def _reply(handler, status: int, obj: dict) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    _headers(handler, status, _MIME[".json"], len(body))
    if handler.command != "HEAD":
        handler.wfile.write(body)


def _not_found(handler) -> None:
    _reply(handler, 404, {"ok": False, "error": "not_found"})


def _send_file(handler, path: Path, name: str) -> None:
    try:
        body = path.read_bytes()
    except OSError:                                      # pragma: no cover
        _reply(handler, 500, {"ok": False, "error": "read_failed"})
        return
    _headers(handler, 200, _MIME.get(Path(name).suffix, "application/octet-stream"),
             len(body))
    if handler.command != "HEAD":
        handler.wfile.write(body)


# The banner about an incomplete kit. NOT in `strings.json` ON PURPOSE: it is
# about `strings.json` not having been read, and saying that from JS is
# impossible, since `folders.js` itself disappears together with the texts.
_KIT_NOTICE = (
    "The program was not copied completely: {files} not found. Copy the "
    "appwin/ directory from the kit as a whole, next to op_agent.py. Until it "
    "is there, this screen will not work; already configured folders are not "
    "affected, folders.ini is in place and runs go on.")

_KIT_STYLE = ("margin:0;padding:12px 16px;font:14px/1.5 system-ui,sans-serif;"
              "background:#fde8e4;color:#5b1a10;border-bottom:1px solid #e0a79c")


def _kit_page(handler, missing: tuple[str, ...]) -> None:
    """Its own page, for when even `folders.html` is missing. No links to css."""
    text = _KIT_NOTICE.format(files=", ".join(missing))
    body = ("<!doctype html><html lang=en><meta charset=utf-8>"
            "<title>OnePage</title><p style=\"" + _KIT_STYLE + "\">"
            + text.replace("&", "&amp;").replace("<", "&lt;")
            + "</p></html>").encode("utf-8")
    _headers(handler, 200, _MIME[".html"], len(body))
    if handler.command != "HEAD":
        handler.wfile.write(body)


def _send_page(handler, name: str) -> None:
    missing = window_missing_kit()
    path = win_res_path(name)
    if path is None:
        _kit_page(handler, missing or (name,))
        return
    try:
        body = path.read_bytes()
    except OSError:                                      # pragma: no cover
        _reply(handler, 500, {"ok": False, "error": "read_failed"})
        return
    if missing:
        notice = ("<p style=\"" + _KIT_STYLE + "\">"
                  + _KIT_NOTICE.format(files=", ".join(missing))
                  + "</p>").encode("utf-8")
        marker = b"<body>"
        at = body.find(marker)
        body = (body[:at + len(marker)] + notice + body[at + len(marker):]
                if at >= 0 else notice + body)
    _headers(handler, 200, _MIME[".html"], len(body))
    if handler.command != "HEAD":
        handler.wfile.write(body)


def _same_origin(handler) -> bool:
    """Origin is absent or names OUR loopback with OUR port.

    A foreign site that got to 127.0.0.1 (a simple POST is not blocked by CORS,
    only reading the response is) gets 403 and writes nothing.
    """
    origin = (handler.headers.get("Origin") or "").strip()
    if not origin:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:                                   # pragma: no cover
        return False
    host = (parts.hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    return port == handler.server.server_address[1]


def _read_body(handler) -> dict:
    n = int(handler.headers.get("Content-Length", "0") or "0")
    if n > MAX_BODY:
        raise foldersio.IniError("body_too_large", str(n))
    raw = handler.rfile.read(n) if n > 0 else b"{}"
    try:
        body = json.loads(raw or b"{}")
    except ValueError as e:
        raise foldersio.IniError("bad_json", str(e)[:120]) from e
    if not isinstance(body, dict):
        raise foldersio.IniError("bad_json", "not an object")
    return body


def _get(handler, path: str) -> None:
    if path in _REDIRECTS:
        handler.send_response(301)
        handler.send_header("Location", _REDIRECTS[path])
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    if path in _PAGES:
        _send_page(handler, _PAGES[path])
        return
    name = path[len(PREFIX) + 1:]
    if name in _ASSETS:
        p = win_res_path(name)
        if p is None:
            _not_found(handler)
            return
        _send_file(handler, p, name)
        return
    if name == "state.json":
        _reply(handler, 200, ui_state())
        return
    if name == "s3/progress" or name == "s3/progress.json":
        # A separate light handler: the screen state walks the disks (seconds
        # on a network share), while the page asks for the download progress
        # once a second as long as the person looks at the row. GET, because it
        # changes nothing; a foreign page will not read the response anyway,
        # since there are no CORS headers on any address here.
        _reply(handler, 200, s3ui.pull_state())
        return
    _not_found(handler)


def _post(handler, path: str) -> None:
    if not _same_origin(handler):
        _reply(handler, 403, {"ok": False, "error": "bad_origin"})
        return
    action = path[len(PREFIX) + 1:]
    try:
        body = _read_body(handler)
        if action == "probe":
            _reply(handler, 200, probe_path(body.get("path")))
            return
        if action == "check":
            _reply(handler, 200, ui_state(only=_key(body)))
            return
        if action == "pick":
            # The system picker dialog. The handler writes nothing: it returns
            # a path, and then the person sees it in the field and presses
            # "Add", i.e. sees WHAT exactly they add. A dialog that wrote to
            # the config at once would deprive them of that look.
            kind = "file" if body.get("kind") == "file" else "dir"
            picked = appwindow.pick_path(kind)
            # `ok: false` without an error means "nothing chosen": cancel,
            # non-Windows or the dialog did not come up. The page simply does
            # nothing about it, and the path input field stays usable in all
            # three cases.
            _reply(handler, 200, {"ok": bool(picked), "path": picked or ""})
            return
        if action in ("entry/add", "entry/remove", "masks", "scenario",
                      "section"):
            _reply(handler, 200, {"ok": True,
                                  "state": apply_action(action, body)})
            return
        if action == "s3/preview":
            # Parsing WITHOUT writing: the page shows "bucket: …, prefix: …"
            # even before the person pressed "Save". The same parse as on
            # save, otherwise one thing would be shown and another saved.
            _reply(handler, 200, s3ui.preview(body))
            return
        if action == "s3/pull":
            # Answer AT ONCE: the pass runs in a background thread, and the
            # progress arrives by polling `s3/progress`. A request hanging for
            # a minute of download is indistinguishable to a person from a
            # hung program: the same lesson as with the "Update now" button in
            # the report mirror.
            _reply(handler, 200, s3ui.pull_now(body))
            return
        if action == "s3/test":
            # It goes to the network, so it answers with ITS OWN result and not
            # the full screen state: walking the disks to answer "12 files
            # visible" would be an extra second on every check.
            _reply(handler, 200, s3ui.test_connection(body))
            return
        if action in ("s3/save", "s3/secret", "s3/forget", "s3/remove"):
            if action == "s3/save":
                res = s3ui.save(body)
            elif action == "s3/secret":
                res = s3ui.set_secret(body.get("name"), body.get("secret"))
            elif action == "s3/forget":
                res = s3ui.forget_secret(body.get("name"))
            else:
                res = s3ui.remove(body)
            # The response carries the FULL state, like the folder edits: the
            # secret never appears in it on any branch, as `sources_state`
            # gives only the `secret_set` flag.
            _reply(handler, 200, dict(res, state=ui_state()))
            return
    except foldersio.IniError as e:
        # A refusal names the reason machine-readably: the human phrase is
        # assembled by the page from `strings.json`; the program's texts live
        # in one place.
        _reply(handler, 400, {"ok": False, "error": e.code,
                              "detail": e.detail, "state": ui_state()})
        return
    except Exception as e:                               # noqa: BLE001
        # The error text may contain a path, so it does not go to the log, only
        # to the page of the same person on whose machine that path lies (the
        # decision "a path is a personal thing", module header).
        _reply(handler, 500, {"ok": False, "error": "internal",
                              "detail": str(e)[:300]})
        return
    _not_found(handler)


def serve_folders_ui(handler) -> bool:
    """Answer if the address is ours. False means "not our address", parsing continues.

    The same contract as `mirror.libui.serve_library`: the screen is plugged
    into `AgentHandler` with one line and knows nothing of its routes.
    """
    path = handler.path.split("?", 1)[0]
    if path != PREFIX and not path.startswith(PREFIX + "/"):
        return False
    cmd = handler.command
    if cmd in ("GET", "HEAD"):
        _get(handler, path)
        return True
    if cmd == "POST":
        _post(handler, path)
        return True
    # OPTIONS and everything else get an honest 405 WITHOUT CORS headers:
    # otherwise a foreign page's preflight would pass and the browser would
    # consider a cross-origin POST here allowed (the same trap as in the
    # mirror).
    _headers(handler, 405, _MIME[".json"], 0)
    return True
