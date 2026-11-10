"""The "S3 bucket" panel on the folders screen: WinSCP-style fields -> a `[s3:<name>]` section.

Reading from a bucket is already written (`agent/s3sync.py`): listing, the
SigV4 signature computed by hand, downloading a prefix into a local folder,
the state of what was downloaded, the kit commands `s3-list` / `s3-sync`.
Exactly one thing was missing -- the place where a person SETS it. The owner
showed two WinSCP screenshots and said plainly: let the bucket be configured
in the same place as GreenPlum, not by editing the ini in Notepad.

So the fields here are exactly the ones he sees in WinSCP, and not one extra:

    File protocol: Amazon S3        <- the [s3:...] section itself is "Amazon S3"
    Encryption: TLS/SSL Implicit    <- the "TLS" checkbox  -+
    Host name: storage.yandexcloud.net                      +-> endpoint
    Port number: 443                                       -+
    Access key ID                   -> access_key   (the LOGIN, not a secret)
    Secret access key               -> DPAPI, entry `s3-<name>`
    Remote directory: prod-s3-nwd   -> bucket + prefix (see below)

Three decisions, each with its reason
-------------------------------------

**1. `Remote directory` is parsed OUT LOUD.** In WinSCP the first segment of
this field is the BUCKET, not a folder inside it: buckets are shown as the
root list. But a person who was told "enter the folder" will write `prod-s3-nwd`,
`prod-s3-nwd/weekly` and `/prod-s3-nwd/weekly/` alike. Guessing silently is
not allowed: a mistake here is invisible -- the listing simply returns zero
objects, and the person goes off to look for a non-existent problem in the
key. So there is one parse (`split_remote`), it is also shown on screen as a
"bucket: ..., prefix: ..." line BEFORE anything is saved, and it is also what
is applied on write. One function, not one on the server and one on the page:
a second parse of the same field would mean that what is shown is not what
gets saved.

**2. The target folder is chosen with a button, not typed as a string.** The
dialog already exists (`POST /folders-ui/pick` -> `appwindow.pick_path`) and is
already used on this same screen for the report folders. A second way to name
a folder on one screen is a second way to make a mistake.

**3. The connection check does a REAL listing.** The same road as the
`s3-list` command: signature, network, proxy, region, permissions. A "check"
that only verifies that the fields are filled in deceives exactly when it is
useful. There is no download: the first question on someone else's machine is
"do we reach the storage and is it the right key", and answering it with
gigabytes is wrong.

Boundaries held here
--------------------

* **The secret only in DPAPI** (`creds.CredStore`, entry `s3-<name>` -- the
  name comes from `S3Source.cred_name` and is not assembled again here). It is
  not in `agent.ini`, not in endpoint responses, not in the log. Only one flag,
  `secret_set`, goes out. This is the same write-only discipline as the git
  token and the model keys on the platform: the field accepts a value and never
  returns it.
* **No `print` and no `applog` in this module** -- for the same reason as in
  `foldersui`: what arrives here is a folder path on the person's machine and
  bucket object names, that is, information about his work, not about our
  program. The names are seen by the PERSON on his own screen and nobody else.
* **We write line by line**, like `foldersio`: `agent.ini` is also edited by
  whoever configures GreenPlum, and his comments are the only place that
  records why the parameters are what they are.

Standard library only.
"""
from __future__ import annotations

import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import foldersio, s3sync
from .creds import CredError, CredStore

INI_NAME = "agent.ini"

# The connection name becomes the section name, the DPAPI entry name and the
# state file name (`s3sync.state_path`). So the alphabet is narrow: whatever is
# fine as a file name on Windows without reservations.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

# The storage address. The scheme and port are set by their own fields, so they
# must not be in the host name: `storage.yandexcloud.net:443` in the "Host name"
# field would give `https://storage.yandexcloud.net:443:443`, and the failure
# would look like a network problem. We reject out loud instead of fixing by
# guesswork.
_HOST_BAD = set(" \t/\\?#@[]")

# What is not allowed in a bucket name. A deliberately short list: the AWS
# limits on a bucket name (3-63 characters, lowercase) differ for MinIO and
# Ceph, and rejecting by them would mean not accepting the person's real
# bucket. We reject only what would break the request ADDRESS.
_BUCKET_BAD = set(" \t/\\?#[]")

MAX_HOST = 255
MAX_ACCESS_KEY = 256
MAX_SECRET = 1024
MAX_REMOTE = 1024
MAX_REGION = 64

# The connection check waits less than a pass: the person is looking at the screen.
TEST_TIMEOUT_S = 20.0
# The listing ceiling for the check. A bucket may have hundreds of thousands of
# objects, and the answer to "do we reach it and do we see the right thing" does not get better from that.
TEST_MAX_LIST = 2000
# How many names to show the person. He checks whether it is the right folder, he does not read it.
TEST_SHOW = 8


# --------------------------------------------------------------------------- #
# File
# --------------------------------------------------------------------------- #

def agent_ini_path() -> str:
    """Absolute path to `agent.ini` -- the same as for the `s3-list`/`s3-sync` commands.

    The order repeats `folders.default_ini_path` and the kit entry point's
    `_ini_path`: first an explicit `OP_AGENT_INI`, then the directory of the
    launched file (in the kit, the folder with `op_agent.py`), then the current
    directory -- the last only for development. The screen and the command must
    not diverge: the person would configure one and the run would read another.
    """
    env = os.environ.get("OP_AGENT_INI")
    if env:
        return os.path.abspath(env)
    try:
        return os.path.abspath(str(Path(sys.argv[0]).resolve().parent / INI_NAME))
    except (OSError, IndexError, ValueError):            # pragma: no cover
        return os.path.abspath(INI_NAME)


# A write refusal is the same object as in the `folders.ini` editor: the codes go to
# the same page and become a human phrase through the same `errText`. Two exception
# classes for one screen would mean two handling branches in the endpoint.
IniError = foldersio.IniError


def _assert_writable(text: str) -> None:
    """Whether it is OK to write to THIS `agent.ini`.

    The check is our own, not `foldersio.assert_writable`: that one requires a
    `[folders:*]` section, which `agent.ini` does not have and should not have.
    The general rule is the same -- non-empty text without a single section
    header is not an ini, and appending lines to it would entrench the breakage.
    """
    if not text.strip():
        return
    if not foldersio.raw_sections(text):
        raise IniError("unparsable", "no [section] headers recognised")
    names = [n for n, _, _ in foldersio.raw_sections(text)
             if n.startswith(s3sync.SECTION_PREFIX)]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        raise IniError("duplicate_section", ", ".join(dups))


def _apply(path, mutate) -> str:
    """Read -> check fitness -> fix -> write atomically.

    A door of its own instead of `foldersio.apply` for exactly one reason -- a
    different fitness check (see `_assert_writable`); reading and writing are
    the very same, and that matters more than symmetry of names: atomicity
    must not be written twice.
    """
    text = foldersio.read_text(path)
    _assert_writable(text)
    new = mutate(text)
    if new != text:
        foldersio.write_text(path, new)
    return new


def _section_bounds(text: str, name: str) -> tuple[int, int] | None:
    """Lines of `[s3:<name>]`: (header, line past the end). No section -- None."""
    want = s3sync.SECTION_PREFIX + name
    found = [(h, e) for n, h, e in foldersio.raw_sections(text) if n == want]
    if not found:
        return None
    if len(found) > 1:                                   # pragma: no cover
        # Not reached: `_assert_writable` refuses earlier. Kept because the
        # order of checks is something that gets edited.
        raise IniError("duplicate_section", want)
    return found[0]


def write_section(text: str, name: str, values: dict[str, str]) -> str:
    """Write values into `[s3:<name>]` without touching anything around it.

    A key with an empty value is DELETED, not written empty: `prefix =` and a
    missing `prefix` are read identically by `s3sync.load_s3`, but an empty line
    in the file looks like "something was here and got lost". Keys that are not
    yet in the section are appended at its end in the declared order -- the
    person finds them where he would have written them himself.
    """
    lines = foldersio.split_lines(text)
    bounds = _section_bounds(text, name)
    if bounds is None:
        block = [""] if lines and lines[-1].strip() else []
        block.append(f"[{s3sync.SECTION_PREFIX}{name}]")
        block += [f"{k} = {v}" for k, v in values.items() if v != ""]
        return "\n".join(lines + block) + "\n"

    header, end = bounds
    drop: set[int] = set()
    replace: dict[int, str] = {}
    seen: set[str] = set()
    tail = header                 # the last meaningful line of the section
    for i in range(header + 1, end):
        key = foldersio.option_key(lines[i])
        if key is None:
            if lines[i].strip():
                tail = i
            continue
        tail = i
        if key not in values:
            continue
        if key in seen or values[key] == "":
            # A repeated key in one section is collapsed by `configparser` to
            # the last one, so the extra lines already mean nothing today.
            drop.add(i)
            continue
        seen.add(key)
        replace[i] = f"{key} = {values[key]}"
    add = [f"{k} = {v}" for k, v in values.items()
           if v != "" and k not in seen]
    insert = {tail + 1: add} if add else {}
    return foldersio.rebuild(lines, drop, replace, insert)


def drop_section(text: str, name: str) -> str:
    """Remove the whole section together with its lines."""
    bounds = _section_bounds(text, name)
    if bounds is None:
        raise IniError("no_section", name)
    header, end = bounds
    lines = foldersio.split_lines(text)
    # The trailing blank lines of a section belong to it: without this, blank
    # lines pile up in the place of a deleted connection.
    while end > header + 1 and not lines[end - 1].strip():
        end -= 1
    return foldersio.rebuild(lines, set(range(header, end)), {}, {})


# --------------------------------------------------------------------------- #
# WinSCP fields -> section fields
# --------------------------------------------------------------------------- #

def check_name(raw: object) -> str:
    """The connection name: also the section name, the DPAPI entry and the state file."""
    value = (raw or "") if isinstance(raw, str) else ""
    value = value.strip()
    if not _NAME_RE.match(value):
        raise IniError("bad_s3_name", value[:64])
    return value


def make_endpoint(host: object, port: object, tls: object) -> str:
    """`Host name` + `Port number` + `Encryption` -> one `endpoint` with a scheme.

    The default port (443 for TLS, 80 without it) is NOT written into the
    address: it is part of the canonical request's `Host`, and therefore of the
    signature, and a stray `:443` makes our signature differ from the one the
    storage will compute. The failure would look like "wrong key" -- exactly the
    kind of error that cannot be found from the server's response.
    """
    value = (host or "") if isinstance(host, str) else ""
    value = value.strip()
    # A person pastes the scheme together with the address from the browser. We strip it, but
    # we do NOT derive encryption from it: the checkbox is responsible for that, and silently
    # switching it would lie about what is shown on the screen.
    for scheme in ("https://", "http://"):
        if value.lower().startswith(scheme):
            value = value[len(scheme):]
            break
    value = value.rstrip("/")
    if not value or len(value) > MAX_HOST or any(c in _HOST_BAD for c in value):
        raise IniError("bad_s3_host", value[:120])
    if ":" in value:
        raise IniError("bad_s3_host_port", value[:120])
    if any(ord(c) < 32 for c in value):                  # pragma: no cover
        raise IniError("bad_s3_host", value[:120])
    secure = bool(tls)
    default = 443 if secure else 80
    raw_port = port
    if isinstance(raw_port, str):
        raw_port = raw_port.strip()
    if raw_port in ("", None):
        number = default
    else:
        try:
            number = int(raw_port)
        except (TypeError, ValueError):
            raise IniError("bad_s3_port", str(port)[:16]) from None
        if not 1 <= number <= 65535:
            raise IniError("bad_s3_port", str(number))
    scheme = "https" if secure else "http"
    suffix = "" if number == default else f":{number}"
    return f"{scheme}://{value}{suffix}"


def split_endpoint(endpoint: str) -> dict:
    """Reverse parse: the ini's `endpoint` -> the WinSCP fields on the screen.

    Needed because the screen must show what is SAVED in the same fields in
    which the person entered it. Showing him an "endpoint" where he entered
    "Host name" and "Port" would force him to parse a string by eye.
    """
    parts = urlsplit(endpoint or "")
    tls = parts.scheme != "http"
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:                                   # pragma: no cover
        port = None
    return {"host": host, "port": port or (443 if tls else 80), "tls": tls}


def split_remote(raw: object) -> tuple[str, str]:
    """`Remote directory` -> (bucket, prefix). The first segment is the BUCKET.

    This is how WinSCP itself works: the Amazon S3 protocol shows the list of
    buckets as the root, so `prod-s3-nwd` is a whole bucket, and
    `prod-s3-nwd/weekly` is a bucket plus a folder in it. A person will write
    either, with extra slashes, and with backslashes (copied from Explorer).
    The parse must be the same in all these cases and NAMED on the screen.

    The prefix is returned with a trailing slash: without it `weekly` would also
    match `weekly-2025/`, that is, someone else's workbooks would arrive in the folder.
    """
    value = (raw or "") if isinstance(raw, str) else ""
    value = value.strip().replace("\\", "/")
    if len(value) > MAX_REMOTE or any(ord(c) < 32 for c in value):
        raise IniError("bad_s3_remote", value[:120])
    parts = [p for p in value.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise IniError("bad_s3_remote", value[:120])
    bucket = parts[0]
    if any(c in _BUCKET_BAD for c in bucket):            # pragma: no cover
        raise IniError("bad_s3_remote", bucket[:120])
    prefix = "/".join(parts[1:])
    return bucket, (prefix + "/" if prefix else "")


def join_remote(bucket: str, prefix: str) -> str:
    """(bucket, prefix) -> one `Remote directory` field, as WinSCP sees it."""
    tail = (prefix or "").strip("/")
    return f"{bucket}/{tail}" if tail else bucket


def _check_simple(raw: object, limit: int, code: str, *, required: bool) -> str:
    """A single-line value for the ini: no line break and no section header."""
    value = (raw or "") if isinstance(raw, str) else ""
    value = value.strip()
    if not value:
        if required:
            raise IniError(code, "")
        return ""
    if len(value) > limit or any(ord(c) < 32 for c in value) \
            or value[:1] in ("[", "=", ":"):
        raise IniError(code, value[:120])
    return value


def _masks(raw: object) -> str:
    """Masks from an input field into one ini line. The same masks as for folders."""
    if isinstance(raw, (list, tuple)):
        items = [str(m) for m in raw]
    else:
        items = str(raw or "").split(",")
    out = [foldersio.check_mask(m) for m in (i.strip() for i in items) if m]
    return ", ".join(out)


def form_to_values(body: dict) -> tuple[str, dict[str, str]]:
    """The page's request body -> (name, values of the `[s3:<name>]` section).

    A separate function, not inside the endpoint: this is exactly what the test
    checks and exactly what the "as parsed" button shows BEFORE saving. A parse
    that exists only inside the POST handler can be neither shown nor checked
    except by writing to the file.
    """
    name = check_name(body.get("name"))
    bucket, prefix = split_remote(body.get("remote"))
    values = {
        "endpoint": make_endpoint(body.get("host"), body.get("port"),
                                  body.get("tls", True)),
        "region": _check_simple(body.get("region"), MAX_REGION, "bad_s3_region",
                                required=False),
        "bucket": bucket,
        "prefix": prefix,
        "access_key": _check_simple(body.get("access_key"), MAX_ACCESS_KEY,
                                    "bad_s3_access_key", required=True),
        "target": foldersio.check_raw(body.get("target")),
        "path_style": "true" if body.get("path_style", True) else "false",
        # The default is YES: that is what a bucket is connected for. A cleared
        # flag means "compute on what is already downloaded", and that is the
        # person's deliberate choice, not a state left by oversight.
        "refresh_before_run": "true" if body.get("refresh_before_run", True) else "false",
        "include": _masks(body.get("include")),
        "exclude": _masks(body.get("exclude")),
    }
    return name, values


def preview(body: dict) -> dict:
    """What would come out of the input -- WITHOUT writing anywhere.

    The page calls this while typing and shows "bucket: ..., prefix: ...". The
    same `split_remote` as on saving, so the parse on the screen and the parse
    in the file cannot diverge.
    """
    name, values = form_to_values(body)
    return {"ok": True, "name": name, "endpoint": values["endpoint"],
            "bucket": values["bucket"], "prefix": values["prefix"],
            "region": values["region"] or "us-east-1",
            "cred_name": s3sync.CRED_PREFIX + name}


# --------------------------------------------------------------------------- #
# Screen state
# --------------------------------------------------------------------------- #

def _store(root: Path | None = None) -> CredStore:
    return CredStore(root)


def _last_pull(name: str, *, state_root: Path | None = None) -> dict:
    """When this connection was last downloaded and how it ended.

    The fact is taken FROM THE SAME PLACE as the state of what was downloaded
    (`s3sync.last_sync`, the file `state_dir()/s3/<name>.json`): "what we have"
    and "when we took it" are one record, and we keep no journal of our own
    here -- a second record of the same fact would one day diverge from the first.

    The date is ASSEMBLED HERE, not on the page: the kit has no `Intl` setting
    and no second formatter, and two different kinds of date on one screen are
    the same trouble as two byte formatters in the mirror.
    """
    info = s3sync.last_sync(name, state_root)
    if not info:
        return {}
    out = dict(info)
    try:
        out["at"] = datetime.fromtimestamp(
            info["at_ms"] / 1000).strftime("%d.%m.%Y %H:%M")
    except (OSError, OverflowError, ValueError):     # pragma: no cover
        out["at"] = ""
    return out


def sources_state(*, cred_root: Path | None = None) -> dict:
    """All bucket connections for the screen. There is no secret here in any form.

    `secret_set` is the only thing said outward about the secret: "set" or "not
    set". The git token and the model keys on the platform work exactly the same
    way, for the same reason -- a value that an endpoint is able to return will
    sooner or later end up in the browser history, in a screenshot or in a copy
    of the response.
    """
    path = agent_ini_path()
    out: dict = {"ini": path, "exists": os.path.isfile(path),
                 "problem": None, "writable": True, "sources": []}
    try:
        text = foldersio.read_text(path)
    except IniError as e:
        out.update({"problem": e.code, "writable": False})
        return out
    try:
        _assert_writable(text)
    except IniError as e:
        out.update({"problem": e.code, "writable": False, "detail": e.detail})
    store = _store(cred_root)
    for name, src in sorted(s3sync.load_s3(path).items()):
        parts = split_endpoint(src.endpoint)
        out["sources"].append({
            "name": name,
            "host": parts["host"], "port": parts["port"], "tls": parts["tls"],
            "bucket": src.bucket, "prefix": src.prefix,
            "remote": join_remote(src.bucket, src.prefix),
            "access_key": src.access_key,
            "target": src.target,
            "region": src.region,
            "path_style": src.path_style,
            "refresh_before_run": src.refresh_before_run,
            # When it was last downloaded is not decoration: without this date a
            # person cannot tell "the data is fresh" from "the connection is set
            # up but has never fired".
            "last_pull": _last_pull(name),
            "include": list(src.include),
            "exclude": list(src.exclude),
            "secret_set": store.has_password(src.cred_name),
            "cred_name": src.cred_name,
        })
    return out


# --------------------------------------------------------------------------- #
# Edits
# --------------------------------------------------------------------------- #

def save(body: dict) -> dict:
    """Save a connection: the section into the ini, the secret (if sent) into DPAPI.

    The secret and the other fields arrive in one request because for the
    person it is one form, but they part ways into two stores in the two lines
    below and never meet again.
    """
    name, values = form_to_values(body)
    path = agent_ini_path()
    _apply(path, lambda t: write_section(t, name, values))
    secret = body.get("secret")
    if isinstance(secret, str) and secret != "":
        set_secret(name, secret)
    return {"ok": True, "name": name}


def set_secret(name: object, secret: object, *, cred_root: Path | None = None) -> dict:
    """Put the bucket secret into DPAPI under the name `s3-<name>`.

    An empty string does NOT erase the entry: the form sends an empty field
    every time the person edits, say, the masks, and "empty means delete" would
    lose the secret for no reason. There is a button of its own for deletion.
    """
    key = check_name(name)
    value = secret if isinstance(secret, str) else ""
    if not value or len(value) > MAX_SECRET:
        raise IniError("bad_s3_secret", "")
    try:
        _store(cred_root).set_password(s3sync.CRED_PREFIX + key, value)
    except CredError as e:
        # The secret does not get into the credential-store error text (it is not
        # put there), but we do not hand the whole phrase outward either: the code is enough.
        raise IniError("cred_unavailable", type(e).__name__) from None
    return {"ok": True, "name": key, "secret_set": True}


def forget_secret(name: object, *, cred_root: Path | None = None) -> dict:
    key = check_name(name)
    _store(cred_root).delete_password(s3sync.CRED_PREFIX + key)
    return {"ok": True, "name": key, "secret_set": False}


def remove(body: dict) -> dict:
    """Remove a connection from the ini. The secret and the downloaded files stay.

    Deliberately: deleting a settings line must not delete data on disk -- the
    person disconnects a source, he does not throw away an export. The page
    says out loud that a secret remains and offers a button.
    """
    name = check_name(body.get("name"))
    _apply(agent_ini_path(), lambda t: drop_section(t, name))
    return {"ok": True, "name": name}


# --------------------------------------------------------------------------- #
# "Download now"
# --------------------------------------------------------------------------- #

# The download progress per program: connection name -> snapshot. A dict, not a
# queue: one or two connections download at a time, and the question to them is
# exactly one -- "how is it doing right now". It is written ONLY by the download
# thread and read by the page; so what sits under the lock is the swap of the
# snapshot itself, not the work with the network (otherwise polling the progress
# would block for the duration of the download).
_PULLS: dict[str, dict] = {}
_PULLS_LOCK = threading.Lock()


def _pull_set(name: str, **fields) -> None:
    with _PULLS_LOCK:
        snap = dict(_PULLS.get(name) or {})
        snap.update(fields)
        _PULLS[name] = snap


def pull_state(*, state_root: Path | None = None) -> dict:
    """Progress and result of downloads for the page: one answer for all connections.

    A separate light endpoint, not inside the screen state: the screen walks the
    disks (seconds on a network share), while the download progress is polled by
    the person once a second while he watches the bar.
    """
    with _PULLS_LOCK:
        out = {name: dict(snap) for name, snap in _PULLS.items()}
    for name, snap in out.items():
        snap["last_pull"] = _last_pull(name, state_root=state_root)
    return {"ok": True, "pulls": out}


def _pull_worker(src: s3sync.S3Source, secret: str, *, client=None,
                 state_root: Path | None = None) -> None:
    """One pass in the background. Raises NO exceptions outward AT ALL: this is a thread,
    and an unhandled exception here is not a failure on screen but silence."""
    name = src.name
    try:
        def progress(done: int, total: int, downloaded: int) -> None:
            _pull_set(name, done=done, total=total, bytes=downloaded)

        rep = s3sync.sync(src, secret, state_root=state_root, client=client,
                          progress=progress)
    except s3sync.S3Error as exc:
        # `str(exc)` is a ready human phrase with advice (`_explain`);
        # the key and signature are cut out of it by the same `_scrub`.
        _pull_set(name, running=False, ok=False, error=str(exc)[:400])
        return
    except Exception as exc:                             # noqa: BLE001
        # The unforeseen lands here (disk full, a name that cannot be written). The text
        # may carry a path, so it goes to the page of the same person and
        # nowhere else -- this module writes nothing to the log.
        _pull_set(name, running=False, ok=False,
                  error=f"{type(exc).__name__}: {str(exc)[:200]}")
        return
    finally:
        secret = ""          # do not hold the secret in the frame longer than the pass
    _pull_set(name, running=False, ok=rep.ok,
              done=rep.matched, total=rep.matched,
              downloaded=rep.downloaded, skipped=rep.skipped,
              deleted=rep.deleted, listed=rep.listed,
              bytes=rep.bytes_downloaded,
              foreign=list(rep.foreign)[:10],
              error="; ".join(f"{k or '?'}: {why}" for k, why in rep.errors[:5])[:400])


def pull_now(body: dict, *, cred_root: Path | None = None, client=None,
             state_root: Path | None = None, background: bool = True) -> dict:
    """Download now -- the same as what a run and the `s3-sync` command do.

    As a separate button, because "the connection is configured" and "the files
    are in the folder" are different states, and until now the second was
    reached only from the command line. There is one road, `s3sync.sync`: we
    do not create a second download implementation, otherwise the button and
    the run would one day bring different things.

    The endpoint ANSWERS AT ONCE, and the pass goes in a background thread --
    like "Update now" in the report mirror and for the same reason: a
    40-megabyte export downloads for a minute, and a connection held for that
    minute is indistinguishable from a hung program. The progress arrives with
    the next `pull_state` poll, and the byte counter inside a file lets the bar
    move while the file count stands still.

    `background=False` is for the test only: it must wait for the result, not
    guess whether the thread made it in time.
    """
    name = check_name(body.get("name"))
    src = s3sync.load_s3(agent_ini_path()).get(name)
    if src is None:
        raise IniError("no_section", name)
    with _PULLS_LOCK:
        if (_PULLS.get(name) or {}).get("running"):
            # A second pass would write to the same `.tmp` and the same state
            # file. This is not a program failure -- the page just keeps
            # showing the progress of the one already running.
            return {"ok": True, "started": False, "reason": "already_running",
                    "name": name}
    try:
        secret = _store(cred_root).get_password(src.cred_name)
    except CredError:
        raise IniError("cred_unavailable", "") from None
    if not secret:
        raise IniError("no_s3_secret", name)
    _pull_set(name, running=True, ok=None, error="", done=0, total=0, bytes=0,
              downloaded=0, skipped=0, deleted=0, listed=0, foreign=[],
              target=src.target)
    if not background:
        _pull_worker(src, secret, client=client, state_root=state_root)
        return {"ok": True, "started": True, "name": name,
                "pull": pull_state(state_root=state_root)["pulls"].get(name, {})}
    threading.Thread(target=_pull_worker, args=(src, secret),
                     kwargs={"client": client, "state_root": state_root},
                     name=f"op-s3-pull-{name}", daemon=True).start()
    return {"ok": True, "started": True, "name": name}


# --------------------------------------------------------------------------- #
# Connection check
# --------------------------------------------------------------------------- #

def test_connection(body: dict, *, cred_root: Path | None = None,
                    client=None) -> dict:
    """A real listing of the prefix: what is visible, downloading nothing.

    The same road as the `s3-list` command (`S3Client.list_objects`), the same
    masks (`s3sync.wanted`) and the same name parse (`relative_name`) --
    otherwise "the check passed" would mean less than a run.

    The error is returned as a HUMAN text: `S3Client._explain` already turns
    the HTTP code and the S3 code into a phrase with advice (a wrong region and
    a wrong `path_style` give refusals that look identical, and naming them is
    half the work). There is no traceback here in any case, and the key and
    signature are cut out of the text by the same `_scrub`.
    """
    # We check WHAT IS ON THE SCREEN, not only what is saved. A person presses
    # this button precisely so as not to save blindly (that is how Login works
    # in WinSCP), and the former refusal "save first" turned the button into a
    # trap: it is there, visible, and on a new connection always answers "no".
    # The form arrives whole -- it is parsed by the same `form_to_values` that
    # will later write the section, so exactly what will be saved gets checked.
    name = check_name(body.get("name"))
    if body.get("host") is not None:
        _, values = form_to_values(body)
        src = s3sync.source_from_values(name, values)
    else:
        src = s3sync.load_s3(agent_ini_path()).get(name)
        if src is None:
            raise IniError("no_section", name)

    # The secret: one typed into the field takes priority over the saved one. An empty field on
    # a saved connection means "the same one that is stored" (saving behaves the same
    # way), and on a new one -- that there is nothing to check with.
    secret = body.get("secret") or ""
    if not secret:
        try:
            secret = _store(cred_root).get_password(src.cred_name) or ""
        except CredError:
            raise IniError("cred_unavailable", "") from None
    if not secret:
        raise IniError("no_s3_secret", name)
    cli = client or s3sync.S3Client(src, secret, timeout=TEST_TIMEOUT_S)
    listed = matched = size = 0
    names: list[str] = []
    truncated = False
    try:
        for obj in cli.list_objects():
            listed += 1
            rel = s3sync.relative_name(obj.key, src.prefix)
            if rel is None or not s3sync.wanted(src, rel):
                continue
            matched += 1
            size += obj.size
            if len(names) < TEST_SHOW:
                names.append(rel)
            if listed >= TEST_MAX_LIST:
                truncated = True
                break
    except s3sync.S3Error as e:
        # `str(e)` is already a ready phrase for a human, not an exception.
        return {"ok": False, "error": "s3_failed", "detail": str(e)[:400],
                "bucket": src.bucket, "prefix": src.prefix}
    except (OSError, ValueError) as e:                   # pragma: no cover
        return {"ok": False, "error": "s3_failed",
                "detail": type(e).__name__, "bucket": src.bucket,
                "prefix": src.prefix}
    return {"ok": True, "name": name, "listed": listed, "matched": matched,
            "bytes": size, "names": names, "truncated": truncated,
            "bucket": src.bucket, "prefix": src.prefix,
            "endpoint": src.endpoint, "target": src.target}
