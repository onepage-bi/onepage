"""S3 bucket as a network folder: download into a local directory, like WinSCP.

Why this lives here, and not in the browser or on the server
------------------------------------------------------------
A generator reads a FOLDER. The browser engine gets it through a File System
Access handle, the native run through a path. Neither can, or should be able
to, reach object storage: the bucket access key must not be handed to a page
(it lives in a foreign tab and the key would leak into the browser session),
and the OnePage server does not reach the internet at all and, by
construction, does not keep the analyst's credentials.

So the role is exactly one and as boring as possible: **the agent downloads
the objects under a bucket prefix into a local folder, and from there
everything works as before** -- the same folder pick in the engine, the same
masks, the same lineage. That is what "like WinSCP" means: a download tool,
not a second data source that bypasses everything else.

What is deliberately done this way
----------------------------------

**1. The signature is computed by hand, no boto3.** The kit is installed
offline and consists of a relocatable CPython plus four wheels
(`deploy/make-agent-kit.sh`); `boto3`+`botocore` would add tens of megabytes
and an update policy of their own for two requests, `ListObjectsV2` and
`GetObject`. The AWS SigV4 signature is just hmac and sha256, both in the
standard library. The price is honest: our signer has to match the reference
one, so `smoke_s3sync` compares the `Authorization` header with what botocore
computes from the SAME input when it is available on a developer machine (it
is not in the kit and never will be).

**2. The secret lives in DPAPI, not in the ini.** The same principle as the
GreenPlum password and the device key: `agent.ini` holds only the address,
bucket, prefix and LOGIN (`access_key`); the secret is stored with the
command `set-cred s3-<name>`. The credential entry name uses a hyphen, not a
colon: the name becomes a file name, and a colon in a file name is not valid
on Windows (it is the NTFS alternate-data-stream separator).

**3. We delete only what we downloaded ourselves.** A stale file in the
generator's folder is more dangerous than a missing one: the engine would read
both it and the new one, and the report would silently double the data (this
exact trap is worked through in the course, on the doubled week). But the
program has no right to clean someone else's folder either -- the person may
have pointed at their own working directory. The compromise is strict: the
state of a pass (`state_dir()/s3/<name>.json`) remembers which files WE
placed; when an object disappears from the bucket, only such a file is
deleted. Everything else is never touched and goes into the report on a
separate "foreign files" line, so there is no silence here either.

**4. Masks are the same as for folders.** `folders.matches` (a line-by-line
port of `app/services/globmatch.py`, itself a port of the engine's
`globToRe`): one and the same question, "what gets read", is already asked in
four places, and a fifth answer here would mean a lineage that disagrees with
what was computed.

**5. An object key does not become a path carelessly.** `..`, an absolute
path, a backslash and a colon in the key mean refusal by object name, not
"we'll write it somehow": this is the same zip-slip that `agent/zipguard.py`
closes, only the source differs.

**6. The bucket is a SOURCE of the report, not a separate command.** Until
20.10 downloading lived only in a hand-typed `op-agent.bat s3-sync <name>`,
and the run did not know about the bucket at all: nobody refreshed the folder
and the report was silently recomputed on yesterday's workbooks. A silently
wrong report is worse than a refusal, so the link is declared here, in one
place: `connections_for` finds the connections whose `target` matches the
folder declared for the report or lies on the same tree as it, and
`runs._resolve_folder_files` refreshes them BEFORE reading. The comparison is
by normalized absolute path (`folders._norm`, case-insensitive on Windows),
not by string: `D:\\Exports`, `d:/exports/` and `D:\\Exports\\.` are one and
the same folder, and a person will write any of the three. The
`refresh_before_run` flag (ON by default -- that is what the whole thing is
for) lets this be turned off when the network is slow and the person
deliberately wants to compute on what is already downloaded.

Standard library only.
"""
from __future__ import annotations

import configparser
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import folders
from .config import state_dir

# Section name in agent.ini and the credential-store entry name.
SECTION_PREFIX = "s3:"
CRED_PREFIX = "s3-"

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
# sha256 of the empty body. We only do GET and LIST, no request has a body,
# so UNSIGNED-PAYLOAD is not needed: an honest hash is simpler and stricter.
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_KEYS = 1000
CHUNK = 1 << 20
DEFAULT_TIMEOUT = 60.0
# S3 has no object names longer than this (the key limit is 1024 bytes).
MAX_KEY_LEN = 1024


class S3Error(Exception):
    """A storage call failure, already put into human language."""


class S3Stopped(Exception):
    """The pass was stopped by the person ("Exit", Ctrl-C)."""


# --------------------------------------------------------------------------- #
# AWS Signature Version 4 signature
# --------------------------------------------------------------------------- #

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, datestamp: str, region: str,
                service: str = SERVICE) -> bytes:
    """Signing key: four nested HMACs over date, region and service."""
    k = _hmac(("AWS4" + secret).encode("utf-8"), datestamp)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def uri_encode(value: str, keep_slash: bool = False) -> str:
    """Percent-encoding by AWS rules.

    The difference from the default `quote` is significant: exactly
    `A-Za-z0-9-_.~` are unreserved, everything else is encoded, and in a path
    a slash stays a slash. A mistake here does not visibly break the request --
    it breaks the SIGNATURE, and the server answers 403 with no explanation.
    """
    safe = "-_.~/" if keep_slash else "-_.~"
    return urllib.parse.quote(value, safe=safe)


def canonical_query(params: dict[str, str]) -> str:
    """Query parameters in canonical form: encoded and sorted."""
    items = sorted((uri_encode(k), uri_encode("" if v is None else str(v)))
                   for k, v in params.items())
    return "&".join(f"{k}={v}" for k, v in items)


def sign_headers(method: str, url: str, params: dict[str, str], *,
                 access_key: str, secret: str, region: str,
                 payload_sha: str = EMPTY_SHA256,
                 now: time.struct_time | None = None) -> dict[str, str]:
    """Headers of one signed request.

    Returns `Authorization`, `x-amz-date`, `x-amz-content-sha256` and `Host`.
    The time is taken from UTC; a machine clock that is off by more than 15
    minutes is a diagnosis of its own, and the server answers it with a clear
    text, which we show as is.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    t = now or time.gmtime()
    amzdate = time.strftime("%Y%m%dT%H%M%SZ", t)
    datestamp = time.strftime("%Y%m%d", t)

    # The path in the canonical request is taken AS IS, with no normalization
    # and no re-encoding: S3 is the only AWS service that does not normalize
    # the path (`botocore.auth.S3SigV4Auth._normalize_url_path` simply returns
    # it), and any "fix" here breaks the signature. So exactly one place
    # encodes the address -- `S3Client._url` -- and `%7E` stays `%7E` instead
    # of turning back into `~`.
    canonical_uri = (parts.path or "/") or "/"
    canonical_headers = (f"host:{host}\n"
                         f"x-amz-content-sha256:{payload_sha}\n"
                         f"x-amz-date:{amzdate}\n")
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join([
        method,
        canonical_uri,
        canonical_query(params),
        canonical_headers,
        signed_headers,
        payload_sha,
    ])
    scope = f"{datestamp}/{region}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join([
        ALGORITHM, amzdate, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(signing_key(secret, datestamp, region),
                         string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return {
        "Host": host,
        "x-amz-date": amzdate,
        "x-amz-content-sha256": payload_sha,
        "Authorization": (f"{ALGORITHM} Credential={access_key}/{scope}, "
                          f"SignedHeaders={signed_headers}, "
                          f"Signature={signature}"),
    }


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class S3Source:
    """One bucket-plus-folder from `agent.ini`. There is no secret here and there cannot be."""
    name: str
    endpoint: str                   # https://s3.example.com (the scheme is required)
    bucket: str
    access_key: str
    target: str                     # where to put files, an absolute path
    region: str = "us-east-1"
    prefix: str = ""                # the "folder" inside the bucket
    path_style: bool = True
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    # Refresh the folder before the generator runs. YES by default: that is
    # exactly why the connection is created -- the report must be computed on
    # what is in the bucket right now. It is turned off deliberately (slow
    # network, a business trip), and then the run uses what is already
    # downloaded and says so out loud.
    refresh_before_run: bool = True

    @property
    def cred_name(self) -> str:
        return CRED_PREFIX + self.name


def _split_masks(raw: str) -> tuple[str, ...]:
    out = []
    for chunk in (raw or "").replace("\n", ",").split(","):
        m = chunk.strip()
        if m:
            out.append(m[:folders.MAX_PATTERN_LEN])
    return tuple(out)


def _as_bool(raw: str, default: bool) -> bool:
    v = (raw or "").strip().lower()
    if v in ("1", "true", "yes", "on", "\u0434\u0430"):
        return True
    if v in ("0", "false", "no", "off", "\u043d\u0435\u0442"):
        return False
    return default


def source_from_values(name: str, values: dict) -> S3Source:
    """`S3Source` from section values NOT yet written to the file.

    Needed by the connection check: the person presses "Check" before saving,
    and the same code that will later read the connection from the file has to
    build it from what is on the screen. Otherwise "the check passed" and "the
    run works" would one day diverge -- in exactly the detail nobody looked for.
    """
    get = lambda k, d="": str(values.get(k, d) or "").strip()  # noqa: E731
    endpoint = get("endpoint").rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        raise S3Error("the storage address must start with http:// or https://")
    missing = [k for k in ("bucket", "access_key", "target") if not get(k)]
    if missing:
        raise S3Error("not filled in: " + ", ".join(missing))
    return S3Source(
        name=name, endpoint=endpoint, bucket=get("bucket"),
        access_key=get("access_key"), target=get("target"),
        region=get("region", "us-east-1") or "us-east-1",
        prefix=get("prefix").lstrip("/"),
        path_style=_as_bool(get("path_style"), True),
        include=_split_masks(get("include")),
        exclude=_split_masks(get("exclude")),
        refresh_before_run=_as_bool(get("refresh_before_run"), True),
    )


def load_s3(ini_path) -> dict[str, S3Source]:
    """`[s3:<name>]` sections from agent.ini. No file -- an empty dict, not an error.

    A section without a required field is SKIPPED with a clear message in the
    output, rather than completed by guesswork: a half-specified bucket is
    better named out loud than silently visited at the wrong place.
    """
    out: dict[str, S3Source] = {}
    path = Path(ini_path) if ini_path else None
    if not path or not path.is_file():
        return out
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    for section in cp.sections():
        if not section.startswith(SECTION_PREFIX):
            continue
        name = section[len(SECTION_PREFIX):].strip()
        if not name:
            continue
        get = lambda key, d="": cp.get(section, key, fallback=d).strip()  # noqa: E731
        endpoint = get("endpoint").rstrip("/")
        bucket = get("bucket")
        access_key = get("access_key")
        target = get("target")
        missing = [k for k, v in (("endpoint", endpoint), ("bucket", bucket),
                                  ("access_key", access_key), ("target", target))
                   if not v]
        if missing:
            print(f"[s3] section [{section}] skipped: missing "
                  f"{', '.join(missing)}", flush=True)
            continue
        if not endpoint.startswith(("http://", "https://")):
            print(f"[s3] section [{section}] skipped: endpoint must "
                  f"start with http:// or https://", flush=True)
            continue
        out[name] = S3Source(
            name=name, endpoint=endpoint, bucket=bucket, access_key=access_key,
            target=target, region=get("region", "us-east-1") or "us-east-1",
            prefix=get("prefix").lstrip("/"),
            path_style=_as_bool(get("path_style"), True),
            include=_split_masks(get("include")),
            exclude=_split_masks(get("exclude")),
            # No key means YES. A default of "do not refresh" would mean that a
            # connection created by an older version of the program silently
            # computes on yesterday's files; a default of "refresh" at worst
            # costs an extra trip to the network, which is visible on screen.
            refresh_before_run=_as_bool(get("refresh_before_run"), True),
        )
    return out


# --------------------------------------------------------------------------- #
# Link "bucket connection <-> report folder"
# --------------------------------------------------------------------------- #

def _cmp_path(raw: str) -> str:
    """A path in a form suitable for COMPARING two paths with each other.

    Normalization is the same as `folders.identity` (one `_norm` function per
    program), and case folding happens where it does there: on Windows,
    because NTFS and SMB do not distinguish case, and only there.
    """
    norm = folders._norm(raw)
    if os.name == "nt":
        return norm.replace("/", "\\").casefold()
    return norm


def _same_or_inside(child: str, parent: str) -> bool:
    """`child` is `parent` or lies INSIDE it (both already comparable)."""
    if child == parent:
        return True
    sep = "\\" if parent.startswith("\\\\") or os.name == "nt" else os.sep
    head = parent if parent.endswith(sep) else parent + sep
    return child.startswith(head)


def connections_for(source, s3: dict[str, "S3Source"]) -> list["S3Source"]:
    """Connections that fill the folders of THIS report.

    There is no "report <-> bucket" link and none is needed: it is already
    expressed by the FOLDER. The person points to a folder in `folders.ini`,
    and in `agent.ini` to where the bucket is put, and if it is one and the
    same folder, then the bucket is the report's source. Linking them with a
    third field would create one more place where two answers can diverge.

    What counts as "the same folder" (and why both directions):

      * the report's `dir` entry and the connection's `target` -- one tree in
        EITHER direction. `target` inside the report folder is the ordinary
        case ("download into the weekly subfolder, read the whole folder");
        the report folder inside `target` is real too ("download the whole
        prefix, read one directory from it"), and skipping it would mean
        exactly the silent computation on stale data that all this was
        written to prevent;
      * the report's `file` entry -- if the file itself lies inside `target`:
        that is exactly the file the connection brings.

    The answer order is `sorted` by name: a run lists the connections for the
    person, and the order must not jump from pass to pass.
    """
    entries = tuple(getattr(source, "entries", ()) or ())
    out: list[S3Source] = []
    for name in sorted(s3):
        src = s3[name]
        try:
            target = _cmp_path(src.target)
        except (TypeError, ValueError):               # pragma: no cover
            continue
        for entry in entries:
            try:
                raw = _cmp_path(entry.raw)
            except (TypeError, ValueError):           # pragma: no cover
                continue
            if entry.kind == "file":
                hit = _same_or_inside(raw, target)
            else:
                hit = _same_or_inside(target, raw) or _same_or_inside(raw, target)
            if hit:
                out.append(src)
                break
    return out


# --------------------------------------------------------------------------- #
# Names: object key -> path on disk
# --------------------------------------------------------------------------- #

_BAD_PARTS = {"", ".", ".."}


def relative_name(key: str, prefix: str) -> str | None:
    """The part of the key after the prefix, or None if the key is unsafe.

    Unsafe: going up, an absolute path, a backslash (a separator on Windows,
    and `a\\..\\..\\b` would leave the folder), a colon (drive and NTFS
    streams), control characters. We refuse BY NAME -- before a single byte
    is written.
    """
    if not key or len(key) > MAX_KEY_LEN or key.endswith("/"):
        return None
    rel = key[len(prefix):] if prefix and key.startswith(prefix) else key
    rel = rel.lstrip("/")
    if not rel or "\\" in rel or ":" in rel:
        return None
    if any(ord(c) < 32 for c in rel):
        return None
    parts = rel.split("/")
    if any(p in _BAD_PARTS for p in parts):
        return None
    return rel


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

@dataclass
class S3Object:
    key: str
    size: int
    etag: str
    modified: str = ""


class S3Client:
    """The two requests we need: list a prefix and download an object."""

    def __init__(self, src: S3Source, secret: str, *, opener=None,
                 timeout: float = DEFAULT_TIMEOUT):
        self._src = src
        self._secret = secret
        # We RESPECT the proxy here (unlike when polling our own port): the
        # storage is a real way out, and on a corporate machine it goes through
        # the proxy from the system settings.
        self._opener = opener or urllib.request.build_opener()
        self._timeout = timeout

    # -- addresses -------------------------------------------------------
    def _url(self, key: str = "") -> str:
        parts = urllib.parse.urlsplit(self._src.endpoint)
        bucket = self._src.bucket
        path = "/" + uri_encode(key, keep_slash=True) if key else "/"
        if self._src.path_style:
            path = "/" + uri_encode(bucket, keep_slash=True) + path
            netloc = parts.netloc
        else:
            netloc = f"{bucket}.{parts.netloc}"
        return urllib.parse.urlunsplit((parts.scheme, netloc, path, "", ""))

    def _open(self, key: str, params: dict[str, str]):
        url = self._url(key)
        headers = sign_headers("GET", url, params,
                               access_key=self._src.access_key,
                               secret=self._secret, region=self._src.region)
        full = url + ("?" + canonical_query(params) if params else "")
        req = urllib.request.Request(full, headers=headers, method="GET")
        try:
            return self._opener.open(req, timeout=self._timeout)
        except urllib.error.HTTPError as e:
            raise S3Error(self._explain(e, key)) from None
        except urllib.error.URLError as e:
            raise S3Error(f"no connection to the storage ({self._src.endpoint}): "
                          f"{self._scrub(str(getattr(e, 'reason', e)))}") from None
        except OSError as e:
            raise S3Error(f"no connection to the storage ({self._src.endpoint}): "
                          f"{type(e).__name__}") from None

    # -- diagnostics -----------------------------------------------------
    def _scrub(self, text: str) -> str:
        """The secret cannot get into an error text (it is only in the
        signature), but the signature in a text is superfluous: its length and
        shape hint at more than necessary. We cut it out too."""
        out = text.replace(self._secret, "***") if self._secret else text
        return out.replace(self._src.access_key, "***")

    def _explain(self, e: urllib.error.HTTPError, key: str) -> str:
        code = getattr(e, "code", 0)
        body = ""
        try:
            body = e.read(4096).decode("utf-8", "replace")
        except Exception:                                    # noqa: BLE001
            pass
        s3code = ""
        try:
            s3code = (ET.fromstring(body).findtext("Code") or "").strip()
        except Exception:                                    # noqa: BLE001
            s3code = ""
        what = f"object {key}" if key else f"bucket {self._src.bucket}"
        if code == 403:
            return (f"the storage did not accept the key ({what}): check access_key, "
                    f"the secret (set-cred {self._src.cred_name}) and region -- the "
                    f"signature is computed with the region, and a wrong region "
                    f"gives exactly this refusal"
                    + (f" [{s3code}]" if s3code else ""))
        if code == 404:
            if s3code == "NoSuchBucket":
                return f"bucket {self._src.bucket} does not exist at {self._src.endpoint}"
            return f"no such object: {key}"
        if code == 400 and s3code == "AuthorizationHeaderMalformed":
            return (f"the storage expects a different region (currently {self._src.region!r}): "
                    f"{self._scrub(body[:200])}")
        if code in (301, 307) or s3code == "PermanentRedirect":
            return ("the storage asks for a different bucket address: try "
                    "path_style = false (or true, if it is false now)")
        return f"the storage answered {code} ({what}): {self._scrub(body[:200])}"

    # -- listing ---------------------------------------------------------
    def list_objects(self, stop=None):
        """All objects under the prefix, in pages of MAX_KEYS."""
        token = ""
        seen = 0
        while True:
            if stop is not None and stop.is_set():
                raise S3Stopped("listing stopped")
            params = {"list-type": "2", "max-keys": str(MAX_KEYS)}
            if self._src.prefix:
                params["prefix"] = self._src.prefix
            if token:
                params["continuation-token"] = token
            resp = self._open("", params)
            try:
                raw = resp.read()
            finally:
                resp.close()
            try:
                root = ET.fromstring(raw)
            except ET.ParseError as e:
                raise S3Error(f"the storage response could not be parsed as XML: {e}") from None
            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag[: root.tag.index("}") + 1]
            for node in root.findall(f"{ns}Contents"):
                key = (node.findtext(f"{ns}Key") or "").strip()
                try:
                    size = int(node.findtext(f"{ns}Size") or 0)
                except ValueError:
                    size = 0
                etag = (node.findtext(f"{ns}ETag") or "").strip().strip('"')
                mod = (node.findtext(f"{ns}LastModified") or "").strip()
                if key:
                    seen += 1
                    yield S3Object(key=key, size=size, etag=etag, modified=mod)
            truncated = (root.findtext(f"{ns}IsTruncated") or "").strip().lower()
            token = (root.findtext(f"{ns}NextContinuationToken") or "").strip()
            if truncated != "true" or not token:
                break

    # -- download --------------------------------------------------------
    def download(self, key: str, dest: Path, *, stop=None, on_chunk=None) -> int:
        """An object to disk. We write to a temporary file and rename: an
        interrupted download must not leave a truncated .xlsx under the right
        name -- the generator would read it as a whole one."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".op-s3-tmp")
        written = 0
        resp = self._open(key, {})
        try:
            promised = resp.headers.get("Content-Length")
            try:
                expected = int(promised) if promised is not None else None
            except (TypeError, ValueError):
                expected = None
            with open(tmp, "wb") as fh:
                while True:
                    if stop is not None and stop.is_set():
                        raise S3Stopped(f"download stopped ({key})")
                    buf = resp.read(CHUNK)
                    if not buf:
                        break
                    fh.write(buf)
                    written += len(buf)
                    if on_chunk is not None:
                        on_chunk(len(buf))
                fh.flush()
                os.fsync(fh.fileno())
            if expected is not None and written != expected:
                # `read(n)` on a cut-off response does NOT raise (http.client
                # quietly closes the connection) -- without this check a
                # truncated file would land as a whole one. The same lesson as
                # in the report mirror.
                raise S3Error(f"{key}: received {written} B of the promised {expected} B")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            resp.close()
        os.replace(tmp, dest)
        return written


# --------------------------------------------------------------------------- #
# A pass
# --------------------------------------------------------------------------- #

@dataclass
class SyncReport:
    listed: int = 0
    matched: int = 0
    downloaded: int = 0
    skipped: int = 0
    deleted: int = 0
    bytes_downloaded: int = 0
    foreign: tuple[str, ...] = ()
    errors: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and not self.stopped


def state_path(name: str, root: Path | None = None) -> Path:
    base = (root or state_dir()) / "s3"
    base.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in name if c.isalnum() or c in "-_") or "default"
    return base / f"{safe}.json"


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_state(path: Path) -> dict:
    files = _read_state(path).get("files")
    return files if isinstance(files, dict) else {}


def last_sync(name: str, root: Path | None = None) -> dict:
    """When this connection was last downloaded and how it ended.

    The time lives in the SAME place as the state of what was downloaded
    (`state_dir()/s3/<name>.json`), and for the same reason the file list
    lives there: "what we have" and "when we took it" are one fact, and
    splitting them over two files means one day showing the time of a pass
    whose result is no longer on disk.

    No file -- an empty dict, not an invented zero: "never downloaded" and
    "downloaded in the Unix epoch" tell the person different things.
    """
    data = _read_state(state_path(name, root))
    at = data.get("at")
    if not isinstance(at, (int, float)) or at <= 0:
        return {}
    out = {"at_ms": int(at * 1000), "ok": bool(data.get("ok"))}
    for key in ("downloaded", "deleted", "bytes", "files"):
        value = data.get("files") if key == "files" else data.get(key)
        if key == "files":
            out["files"] = len(value) if isinstance(value, dict) else 0
        elif isinstance(value, int):
            out[key] = value
    return out


def _save_state(path: Path, files: dict, extra: dict | None = None) -> None:
    tmp = path.with_name(path.name + ".tmp")
    payload = {"files": files}
    payload.update(extra or {})
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        # Not a catastrophe: the next pass will simply download everything again.
        print(f"[s3] state not saved: {type(e).__name__}", flush=True)
        tmp.unlink(missing_ok=True)


def wanted(src: S3Source, name: str) -> bool:
    """Whether the file is needed according to the section masks. The name is
    the last segment of the key: masks work on the file NAME, just like for folders."""
    base = name.rsplit("/", 1)[-1]
    if src.include and not any(folders.matches(p, base) for p in src.include):
        return False
    if any(folders.matches(p, base) for p in src.exclude):
        return False
    return True


def sync(src: S3Source, secret: str, *, state_root: Path | None = None,
         client: S3Client | None = None, progress=None, stop=None) -> SyncReport:
    """One pass: download what changed, remove what disappeared, report.

    `progress(done, total, downloaded_bytes)` is the same contract as in the
    report mirror: the file count stands still while a large workbook is
    downloading, and the only thing that moves is the bytes.
    """
    rep = SyncReport()
    cli = client or S3Client(src, secret)
    target = Path(src.target).expanduser()
    st_path = state_path(src.name, state_root)
    known = _load_state(st_path)

    try:
        objects = []
        for obj in cli.list_objects(stop=stop):
            rep.listed += 1
            rel = relative_name(obj.key, src.prefix)
            if rel is None:
                rep.errors.append((obj.key, "unsafe object name -- skipped"))
                continue
            if not wanted(src, rel):
                continue
            objects.append((obj, rel))
        rep.matched = len(objects)
    except S3Stopped as e:
        rep.stopped = True
        rep.errors.append(("", str(e)))
        # We leave the list of downloaded files alone (it is still correct), but
        # DO record the ATTEMPT: the screen must tell "did not go" from "went
        # and failed", otherwise "last download: yesterday" looks like success.
        _save_state(st_path, known, {"at": time.time(), "ok": False})
        return rep
    except S3Error as e:
        rep.errors.append(("", str(e)))
        _save_state(st_path, known, {"at": time.time(), "ok": False})
        return rep

    target.mkdir(parents=True, exist_ok=True)
    total = len(objects)
    done = 0
    fresh: dict[str, dict] = {}
    if progress is not None:
        progress(done, total, rep.bytes_downloaded)

    for obj, rel in objects:
        dest = target / rel
        prev = known.get(obj.key)
        # The "unchanged" criterion is etag and size, not time: the clocks of
        # the machine and of the storage live independently, and a time
        # comparison would one day miss a substituted file.
        unchanged = (isinstance(prev, dict) and prev.get("etag") == obj.etag
                     and prev.get("size") == obj.size and dest.is_file()
                     and dest.stat().st_size == obj.size)
        if unchanged:
            rep.skipped += 1
            fresh[obj.key] = prev
            done += 1
            if progress is not None:
                progress(done, total, rep.bytes_downloaded)
            continue
        # A byte counter INSIDE the current file: a 40 MB workbook downloads for
        # a minute, and without it "3 of 12" stands motionless -- exactly the
        # complaint that gave rise to this same counter in the report mirror.
        # The chunks are a megabyte, so the callback is called rarely even
        # without throttling.
        live = [0]

        def _chunk(n: int) -> None:
            live[0] += n
            progress(done, total, rep.bytes_downloaded + live[0])

        try:
            got = cli.download(obj.key, dest, stop=stop,
                               on_chunk=_chunk if progress is not None else None)
        except S3Stopped as e:
            rep.stopped = True
            rep.errors.append((obj.key, str(e)))
            break
        except (S3Error, OSError) as e:
            rep.errors.append((obj.key, str(e)[:300]))
            done += 1
            if progress is not None:
                progress(done, total, rep.bytes_downloaded)
            continue
        rep.downloaded += 1
        rep.bytes_downloaded += got
        fresh[obj.key] = {"etag": obj.etag, "size": obj.size, "name": rel}
        done += 1
        if progress is not None:
            progress(done, total, rep.bytes_downloaded)

    # Disappeared from the bucket -- remove, but ONLY if we placed it ourselves (see the docstring).
    if not rep.stopped:
        for key, meta in known.items():
            if key in fresh or not isinstance(meta, dict):
                continue
            name = meta.get("name")
            if not isinstance(name, str) or relative_name(name, "") is None:
                continue
            victim = target / name
            try:
                if victim.is_file():
                    victim.unlink()
                    rep.deleted += 1
            except OSError as e:
                rep.errors.append((key, f"could not remove the stale file: {type(e).__name__}"))
                fresh[key] = meta        # do not forget it -- we will retry next time

    # The pass time is recorded ALWAYS, including after a failure: "last
    # downloaded at 9:40, and it failed then" is an answer, while "never
    # downloaded" on the same screen would be a lie.
    _save_state(st_path, fresh, {
        "at": time.time(), "ok": rep.ok, "downloaded": rep.downloaded,
        "deleted": rep.deleted, "bytes": rep.bytes_downloaded,
    })

    # Foreign files in the folder are not touched but are named: they are what
    # causes silent data doubling when left over from a manual export.
    ours = {meta.get("name") for meta in fresh.values() if isinstance(meta, dict)}
    foreign = []
    try:
        for p in sorted(target.rglob("*")):
            if p.is_file() and str(p.relative_to(target)).replace(os.sep, "/") not in ours:
                foreign.append(str(p.relative_to(target)).replace(os.sep, "/"))
    except OSError:
        pass
    rep.foreign = tuple(foreign[:50])
    return rep
