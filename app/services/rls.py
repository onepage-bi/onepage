"""Row-level security on slot files (ADR-18).

A report's HTML holds no data — all data lives in slot files, and the server
filters EVERY slot file when serving it to a specific viewer. Everything
needed for that lives here:

    norm()             — the single key normalisation (one for the whole project)
    validate_policy()  — validation of Dashboard.rls_json
    subject()          — what this viewer sees (one indexed SELECT)
    filter_bytes()     — filtering of one slot file (OPC1/CSV/JSON/parquet)
    serve_filtered()   — HTTP serving with ETag, 304, gzip and audit on a miss

Default deny: no row in user_scopes → an empty (but valid) file, not everything.
Any ambiguity is an error (RlsError), never "serve it as is":
silently serving someone else's rows is worse than a broken report.

This module does NOT import app.api (otherwise a cycle: files.py imports us).
Recomputing user_scopes lives in app/services/scopes.py — here UserScope is only
read.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.config import settings
from app.i18n import _
from app.models import AuditLog, Dashboard, ROLE_ADMINISTRATOR, User, UserScope
from app.services import encoding, opc1, storage

_MAX_NAME = 128


class RlsError(HTTPException):
    """Filter refusal. An HTTPException, so the route simply propagates it up;
    status/detail is what the client sees (no data values)."""

    def __init__(self, status: int, detail: str):
        super().__init__(status_code=status, detail=detail)
        self.status = status


# ---------------------------------------------------------------------------
# Key normalisation
# ---------------------------------------------------------------------------

def norm(s) -> str:
    """The single normalisation for COMPARING a column value with the viewer's keys.

    NBSP (DWH exports drop them in regularly) → space, whitespace collapsing,
    casefold. Applied on BOTH sides — otherwise "Rep  North-1" from the file
    would never match "Rep North-1" from the positions directory.
    """
    if s is None:
        return ""
    return " ".join(str(s).replace("\xa0", " ").split()).casefold()


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

_SLOT_KEYS = {"index", "column", "key", "via"}
_VIA_KEYS = {"slot", "key", "column"}


def _name_error(where: str, value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return _("{where}: a non-empty string", where=where)
    if len(value) > _MAX_NAME:
        return _("{where}: longer than {max} characters", where=where, max=_MAX_NAME)
    return None


def validate_policy(obj) -> list[str]:
    """List of policy errors (empty = valid). Never raises.

    Stricter than "as long as it parses": an unknown field is an error, because
    a typo like `colum` would otherwise give a slot with no filter, i.e. a leak.
    """
    errors: list[str] = []
    if not isinstance(obj, dict):
        return [_("the policy must be an object")]
    unknown = sorted(set(obj) - {"slots", "passthrough"})
    if unknown:
        errors.append(_("unknown fields: {fields}", fields=", ".join(unknown)))

    pass_raw = obj.get("passthrough", [])
    passthrough: set[int] = set()
    if not isinstance(pass_raw, list):
        errors.append(_("passthrough: a list of indices"))
    else:
        for i, v in enumerate(pass_raw):
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                errors.append(_("passthrough[{i}]: integer ≥ 0", i=i))
            elif v in passthrough:
                errors.append(_("passthrough[{i}]: index {index} is repeated", i=i, index=v))
            else:
                passthrough.add(v)

    slots = obj.get("slots")
    if not isinstance(slots, list) or not slots:
        errors.append(_("slots: a non-empty list"))
        return errors

    seen: dict[int, dict] = {}
    for i, s in enumerate(slots):
        if not isinstance(s, dict):
            errors.append(_("slots[{i}]: must be an object", i=i))
            continue
        unknown = sorted(set(s) - _SLOT_KEYS)
        if unknown:
            errors.append(_("slots[{i}]: unknown fields: {fields}",
                            i=i, fields=", ".join(unknown)))
        idx = s.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
            errors.append(_("slots[{i}]: index must be an integer ≥ 0", i=i))
            idx = None
        elif idx in seen:
            errors.append(_("slots[{i}]: index {index} is repeated", i=i, index=idx))
        elif idx in passthrough:
            errors.append(_("slots[{i}]: index {index} is also in passthrough", i=i, index=idx))
        else:
            seen[idx] = s

        has_col, has_via = "column" in s, "via" in s
        if has_col == has_via:
            errors.append(_("slots[{i}]: exactly one of column or via", i=i))
            continue
        if has_col:
            e = _name_error(f"slots[{i}].column", s.get("column"))
            if e:
                errors.append(e)
            if "key" in s:
                errors.append(_("slots[{i}]: key is only allowed together with via", i=i))
            continue

        e = _name_error(f"slots[{i}].key", s.get("key"))
        if e:
            errors.append(e)
        via = s.get("via")
        if not isinstance(via, dict):
            errors.append(_("slots[{i}].via: must be an object", i=i))
            continue
        unknown = sorted(set(via) - _VIA_KEYS)
        if unknown:
            errors.append(_("slots[{i}].via: unknown fields: {fields}",
                                i=i, fields=", ".join(unknown)))
        for f in ("key", "column"):
            e = _name_error(f"slots[{i}].via.{f}", via.get(f))
            if e:
                errors.append(e)
        vslot = via.get("slot")
        if not isinstance(vslot, int) or isinstance(vslot, bool) or vslot < 0:
            errors.append(_("slots[{i}].via.slot: integer ≥ 0", i=i))
        elif vslot == idx:
            errors.append(_("slots[{i}].via.slot: a slot cannot reference itself", i=i))

    # via is allowed only on a slot with column: depth exactly 1. Otherwise a
    # via→via→… chain would turn one serve into a walk over all report slots.
    for idx, s in seen.items():
        via = s.get("via")
        if not isinstance(via, dict):
            continue
        vslot = via.get("slot")
        if not isinstance(vslot, int) or isinstance(vslot, bool):
            continue
        target = seen.get(vslot)
        if target is None:
            errors.append(_("slots[index={index}].via.slot: slot {slot} is not declared in slots",
                            index=idx, slot=vslot))
        elif "column" not in target:
            errors.append(_("slots[index={index}].via.slot: slot {slot} is itself a via — via depth is 1",
                            index=idx, slot=vslot))
    return errors


_RE_INLINE_SNAPSHOT = re.compile(
    r'<script[^>]*id=["\']bootstrap-snapshot["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE)


def _json_is_empty(raw) -> bool:
    """An "empty" value = nothing, whitespace, {}, [] or null. Everything else is data."""
    if raw is None:
        return True
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    text = text.strip()
    if not text:
        return True
    try:
        v = json.loads(text)
    except (ValueError, TypeError):
        return False
    return v is None or v == {} or v == []


def eligibility_error(dashboard: Dashboard, html: str | None = None) -> str | None:
    """Why a policy CANNOT be set on this report (None = it can).

    An interactive report always qualifies: its data is in slot files. Static —
    never (rows inside the HTML). "Snapshot" — only an empty one: agent reports
    ("Delta") carry an EMPTY <script id="bootstrap-snapshot">{}</script> as a
    placeholder and get their data via op_file, while the type detector
    calls them snapshot. Data in the tag or in a saved snapshot.json means
    refusal: it would be injected identically for every viewer."""
    dtype = dashboard.dash_type
    if dtype == "interactive":
        return None
    if dtype != "snapshot":
        return _("RLS: the {kind} report's data lives in the HTML, there's nothing to filter", kind=dtype)
    if html is None:
        try:
            html = storage.read_html(dashboard.id, dashboard.current_version).decode(
                "utf-8", errors="replace")
        except FileNotFoundError:
            html = ""
    for m in _RE_INLINE_SNAPSHOT.finditer(html):
        if not _json_is_empty(m.group(1)):
            return _("RLS: the HTML contains a non-empty bootstrap snapshot — the data would go out to everyone")
    if not _json_is_empty(storage.read_snapshot_json(dashboard.id, dashboard.current_version)):
        return _("RLS: the report has a saved snapshot.json with data — remove it first")
    return None


def policy_hash(dashboard: Dashboard) -> str:
    """Policy fingerprint — part of the cache key and the ETag.

    Without it a policy change would rely on `cache.clear()`, which is per-process:
    with OP_WORKERS > 1, workers that did not handle PUT /rls would keep serving
    bytes filtered by the OLD policy. With the fingerprint in the key a policy
    change = a new key, invalidation by rotation and no inter-process signalling.
    """
    return hashlib.sha256((dashboard.rls_json or "").encode("utf-8")).hexdigest()[:12]


def policy_of(dashboard: Dashboard) -> dict:
    """The dashboard's policy, or RlsError. Broken JSON = fail closed: a report
    whose rls_json cannot be read serves no data at all."""
    policy = dashboard.get_rls()
    if policy is None:
        raise RlsError(403, "RLS: policy is not readable")
    return policy


def _slot_rule(policy: dict, slot_index: int) -> Optional[dict]:
    """The slot's rule, None for passthrough. A slot described NOWHERE is 403:
    a file added to the version with the policy forgotten = a leak (fail closed)."""
    for s in policy.get("slots") or []:
        if isinstance(s, dict) and s.get("index") == slot_index:
            return s
    pt = policy.get("passthrough") or []
    if isinstance(pt, list) and slot_index in pt:
        return None
    raise RlsError(403, f"RLS: slot {slot_index} is not covered by the policy")


def is_passthrough(policy: dict, slot_index: int) -> bool:
    """True if the slot is served as is (JS libraries and other non-data)."""
    pt = policy.get("passthrough") or []
    return isinstance(pt, list) and slot_index in pt


# ---------------------------------------------------------------------------
# Subject
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Subject:
    """What the viewer sees. keys_hash is part of the ETag: without it a browser
    or proxy cache would serve one viewer's file to another."""

    see_all: bool
    keys: frozenset[str] = field(default_factory=frozenset)
    keys_hash: str = "all"
    user_id: Optional[int] = None      # for the rls_serve audit
    person_id: str = ""


def keys_hash(keys) -> str:
    h = hashlib.sha256("\n".join(sorted(keys)).encode("utf-8")).hexdigest()
    return h[:16]


def subject(db: Session, dashboard: Dashboard, user: User) -> Subject:
    """One query to user_scopes by PK. Not called at all for NON-RLS
    dashboards — an ordinary report's hot path pays not a single query."""
    row = None
    ov = (user.person_id or "").strip()
    if ov:
        row = db.query(UserScope).filter(UserScope.person_id == ov).first()
    if user.role == ROLE_ADMINISTRATOR or dashboard.owner_id == user.id or (row and row.see_all):
        return Subject(True, frozenset(), "all", user.id, ov)
    keys = frozenset(norm(k) for k in (row.get_keys() if row else []) if norm(k))
    return Subject(False, keys, keys_hash(keys), user.id, ov)


# ---------------------------------------------------------------------------
# Slot-file tables
# ---------------------------------------------------------------------------

def _to_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v != v:      # NaN is "empty" in OPC1
        return ""
    if isinstance(v, str):
        return v
    return str(v)


class _Table:
    """Common format interface: columns by name + rebuilding bytes by a mask.
    The mask is built once and applied to all columns at once — otherwise
    rows would drift apart between columns."""

    rows: int = 0

    def has(self, name: str) -> bool:
        raise NotImplementedError

    def values(self, name: str) -> list[str]:
        raise NotImplementedError

    def dump(self, mask: list[bool]) -> bytes:
        raise NotImplementedError


class _Opc1Table(_Table):
    def __init__(self, raw: bytes):
        try:
            self.desc, self.cols = opc1.decode(raw)
        except opc1.Opc1Error as e:
            raise RlsError(415, f"RLS: broken OPC1 slot ({e})")
        self.schema = opc1.schema_of(self.desc)
        self.rows = int(self.desc.get("rows") or 0)
        self._cache: dict[str, list[str]] = {}

    def has(self, name: str) -> bool:
        return name in self.cols

    def values(self, name: str) -> list[str]:
        got = self._cache.get(name)
        if got is None:
            col = self.cols[name]
            # a dict column is already strings — the most common case for a scope key,
            # so no copy is made.
            got = col if col and isinstance(col[0], str) else [_to_str(v) for v in col]
            self._cache[name] = got
        return got

    def dump(self, mask: list[bool]) -> bytes:
        cols = {n: [v for v, keep in zip(self.cols[n], mask) if keep]
                for n, _t in self.schema}
        # Dictionaries of dict columns are rebuilt from the surviving rows — see opc1.encode.
        return opc1.encode(self.schema, cols)


class _CsvTable(_Table):
    def __init__(self, raw: bytes, slot_index: int):
        import csv
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Guessing between utf-8-sig and cp1251 is not allowed: a wrong guess silently
            # renames a territory and the row "disappears" for its own owner.
            raise RlsError(415, f"RLS: slot {slot_index} is not valid UTF-8")
        rd = csv.reader(io.StringIO(text, newline=""))
        self.header = next(rd, None) or []
        self.rows_data = [r for r in rd if r]
        self.rows = len(self.rows_data)

    def has(self, name: str) -> bool:
        return name in self.header

    def values(self, name: str) -> list[str]:
        i = self.header.index(name)
        return [(r[i] if i < len(r) else "") for r in self.rows_data]

    def dump(self, mask: list[bool]) -> bytes:
        import csv
        buf = io.StringIO(newline="")
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(self.header)                    # the header always stays
        for r, keep in zip(self.rows_data, mask):
            if keep:
                w.writerow(r)
        return buf.getvalue().encode("utf-8")


class _JsonTable(_Table):
    def __init__(self, raw: bytes, slot_index: int):
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise RlsError(415, f"RLS: slot {slot_index} is not valid JSON")
        self.wrapper: Optional[dict] = None
        if isinstance(doc, dict) and isinstance(doc.get("rows"), list):
            self.wrapper, rows = doc, doc["rows"]
        elif isinstance(doc, list):
            rows = doc
        else:
            raise RlsError(415, f"RLS: slot {slot_index} is not an array of objects")
        if any(not isinstance(r, dict) for r in rows):
            raise RlsError(415, f"RLS: slot {slot_index} is not an array of objects")
        self.rows_data = rows
        self.rows = len(rows)

    def has(self, name: str) -> bool:
        return any(name in r for r in self.rows_data)

    def values(self, name: str) -> list[str]:
        return [_to_str(r.get(name)) for r in self.rows_data]

    def dump(self, mask: list[bool]) -> bytes:
        kept = [r for r, keep in zip(self.rows_data, mask) if keep]
        if self.wrapper is not None:
            doc = dict(self.wrapper)               # the wrapper is kept as is
            doc["rows"] = kept
        else:
            doc = kept
        return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class _ParquetTable(_Table):
    def __init__(self, raw: bytes, slot_index: int):
        try:
            import pyarrow.parquet as pq          # optional (see filter_bytes)
        except ImportError:
            raise RlsError(415, "RLS: unsupported slot format")
        self._pq = pq
        try:
            self.table = pq.read_table(io.BytesIO(raw))
        except Exception as e:                     # noqa: BLE001 — any broken file
            raise RlsError(415, f"RLS: broken parquet slot ({type(e).__name__})")
        self.rows = self.table.num_rows

    def has(self, name: str) -> bool:
        return name in self.table.column_names

    def values(self, name: str) -> list[str]:
        return [_to_str(v) for v in self.table.column(name).to_pylist()]

    def dump(self, mask: list[bool]) -> bytes:
        import pyarrow as pa
        out = io.BytesIO()
        # type=bool_ explicitly: an empty mask (slot with no rows) would otherwise become
        # an array of type null, and filter() on it fails.
        self._pq.write_table(self.table.filter(pa.array(mask, type=pa.bool_())), out)
        return out.getvalue()


_GZ_SUFFIX = ".gz"


def _strip_gz(filename: str) -> tuple[str, bool]:
    low = filename.lower()
    if low.endswith(_GZ_SUFFIX):
        return filename[:-len(_GZ_SUFFIX)], True
    return filename, False


def _table(filename: str, raw: bytes, slot_index: int) -> _Table:
    """Parse a slot file by its EXTENSION (we do not guess the content): an unknown
    extension = 415, not "serve it as is"."""
    bare, gz = _strip_gz(filename)
    low = bare.lower()
    if gz:
        try:
            raw = encoding.decode_bytes(raw, "gzip")
        except Exception:  # noqa: BLE001 — broken gzip of any kind
            raise RlsError(415, f"RLS: slot {slot_index} is not valid gzip")
    if low.endswith(".bin"):
        return _Opc1Table(raw)
    if low.endswith(".csv"):
        return _CsvTable(raw, slot_index)
    if low.endswith(".json"):
        return _JsonTable(raw, slot_index)
    if low.endswith(".parquet"):
        return _ParquetTable(raw, slot_index)
    raise RlsError(415, "RLS: unsupported slot format")


def _column(table: _Table, name: str, slot_index: int) -> list[str]:
    if not table.has(name):
        # Fail closed: the scope column is missing — nothing to filter by, so
        # nothing may be served (the column was renamed in the generator).
        raise RlsError(403, f"RLS: column {name} missing in slot {slot_index}")
    return table.values(name)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

LoadSlot = Callable[[int], tuple[str, bytes]]


def filter_bytes(policy: dict, slot_index: int, filename: str, raw: bytes,
                 keys: frozenset[str], load_slot: LoadSlot) -> tuple[bytes, int, int]:
    """Filtered slot file -> (bytes, rows_in, rows_out).

    Returns the file in THE SAME representation as the input: *.gz in — *.gz
    out (gzip mtime=0, filename="" — the bytes do not depend on time or path).
    passthrough slots never get here, they are served as is.
    """
    rule = _slot_rule(policy, slot_index)
    if rule is None:
        return raw, 0, 0

    _base, gz = _strip_gz(filename)
    table = _table(filename, raw, slot_index)
    rows_in = table.rows

    if rows_in == 0:
        # The slot honestly published zero rows in this cycle. There is nobody
        # to ask about the column (an empty JSON array shows no columns), and
        # nothing to filter — we serve an empty but valid table rather than a 403
        # "no column": the report should show an empty table, not break.
        out = table.dump([])
        return (_gzip(out) if gz else out), 0, 0

    if "column" in rule:
        values = _column(table, rule["column"], slot_index)
        mask = [norm(v) in keys for v in values]
    else:
        allowed = _via_values(policy, rule["via"], keys, load_slot)
        values = _column(table, rule["key"], slot_index)
        mask = [norm(v) in allowed for v in values]

    rows_out = sum(mask)
    out = table.dump(mask)
    if gz:
        out = _gzip(out)
    return out, rows_in, rows_out


def _via_values(policy: dict, via: dict, keys: frozenset[str],
                load_slot: LoadSlot) -> frozenset[str]:
    """Indirect scope, depth 1: from slot via.slot take the via.key values of
    rows whose via.column is in keys. "Delta" example: customers (customer_number)
    of my territories — the slot with per-customer facts is filtered by them."""
    src_index = via["slot"]
    src_name, src_raw = load_slot(src_index)
    src = _table(src_name, src_raw, src_index)
    scope_values = _column(src, via["column"], src_index)
    key_values = _column(src, via["key"], src_index)
    allowed: set[str] = set()
    for k, s in zip(key_values, scope_values):
        if norm(s) not in keys:
            continue
        nk = norm(k)
        # An empty key (NULL in the export, i32 NULL, empty cell) does NOT go
        # into the set: otherwise one such source row would open all rows of
        # the target slot with an empty key — i.e. other people's customers. The direct
        # path (column) drops empty keys in subject(); same rule here.
        if nk:
            allowed.add(nk)
    return frozenset(allowed)


def _gzip(data: bytes) -> bytes:
    """mtime=0 + filename="": the same input gives the same bytes, so
    the cache and ETag do not depend on when and where the file was built."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0,
                       filename="") as out:
        out.write(data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Cache of filtered files
# ---------------------------------------------------------------------------

class _FilteredCache:
    """Byte-budgeted LRU (the _RenderCache idiom from app/api/files.py).

    The key carries the file's stat, the POLICY fingerprint and the hash of the viewer's keys:
    replacing a slot, changing the policy or recomputing scopes = a new key = invalidation
    by rotation, not deletion (so it does not depend on the number of workers). Budget
    settings.rls_cache_mb; 0 — cache disabled (every serve filters afresh).
    """

    def __init__(self) -> None:
        self._cells: "OrderedDict[tuple, bytes]" = OrderedDict()
        self._lock = threading.Lock()
        self.bytes = 0

    @property
    def budget(self) -> int:
        return max(0, settings.rls_cache_mb) * 1024 * 1024

    def get(self, key: tuple) -> Optional[bytes]:
        if not self.budget:
            return None
        with self._lock:
            payload = self._cells.get(key)
            if payload is not None:
                self._cells.move_to_end(key)
            return payload

    def put(self, key: tuple, payload: bytes) -> None:
        budget = self.budget
        if not budget or len(payload) > budget:
            return
        with self._lock:
            old = self._cells.pop(key, None)
            if old is not None:
                self.bytes -= len(old)
            self._cells[key] = payload
            self.bytes += len(payload)
            while self._cells and self.bytes > budget:
                _key, evicted = self._cells.popitem(last=False)
                self.bytes -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._cells.clear()
            self.bytes = 0


cache = _FilteredCache()


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------

# Duplicate of the map from app/api/files.py: importing app.api from here is not possible
# (cycle — files.py imports this module). The list is short because only data
# formats plus passthrough assets go through RLS.
_MIME = {
    ".bin": "application/octet-stream",
    ".csv": "text/csv",
    ".json": "application/json",
    ".parquet": "application/octet-stream",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


def _mime(filename: str) -> str:
    low = filename.lower()
    for ext, ct in _MIME.items():
        if low.endswith(ext):
            return ct
    return "application/octet-stream"


def _log(db: Session, actor_id: Optional[int], dashboard_id: str, detail: dict) -> None:
    entry = AuditLog(
        actor_id=actor_id, action="rls_serve",
        resource_type="dashboard", resource_id=dashboard_id,
        ip_address="",
    )
    entry.set_detail(detail)
    db.add(entry)


def serve_filtered(request, db: Session, dashboard: Dashboard, slot_index: int,
                   subject: Subject) -> Response:
    """Slot file filtered for this viewer (sync def — disk and CPU
    go to the threadpool, like serve_slot_download).

    - ETag = "<slot_fp>-<policy_hash>-<keys_hash>-<rep>": keys_hash is MANDATORY,
      otherwise one viewer's immutable response would reach another; policy_hash —
      otherwise tightening the policy would not reset an already served immutable response.
    - Cache-Control: private, no-cache always (immutable is forbidden — shared URL,
      see the comment by the headers)
      (the address is then content-addressed, as in serve_slot_download).
    - The rls_serve audit is written ONLY on a cache miss (real filtering) and
      carries only counters — not a single data value.
    """
    policy = policy_of(dashboard)
    passthrough = is_passthrough(policy, slot_index)
    if not passthrough:
        _slot_rule(policy, slot_index)             # 403 on an uncovered slot

    version = dashboard.current_version
    path = storage.slot_file_path(dashboard.id, version, slot_index)
    if path is None:
        raise HTTPException(status_code=404, detail="No file for this slot")
    filename = path.name[len(f"slot_{slot_index}_"):]
    st = path.stat()
    fp = storage.slot_fp(st)
    # A via slot also depends on the source slot: replacing it in place (PUT
    # of the slot by a subject with see_all) must reset both the cache and the ETag of the target slot,
    # otherwise the viewer gets customers by an OUTDATED territory map.
    via_fp = ""
    if not passthrough:
        via = (_slot_rule(policy, slot_index) or {}).get("via")
        if isinstance(via, dict):
            src = storage.slot_file_path(dashboard.id, version, int(via["slot"]))
            if src is None:
                raise RlsError(403, f"RLS: slot {via['slot']} is missing (via lookup)")
            via_fp = storage.slot_fp(src.stat())

    _base, gz = _strip_gz(filename)
    accept = request.headers.get("accept-encoding", "")
    if gz:
        rep = "gzip" if "gzip" in encoding.accepted_encodings(accept) else "dec"
    else:
        rep = encoding.negotiate(accept)

    phash = policy_hash(dashboard)
    etag = f'"{fp}{"-" + via_fp if via_fp else ""}-{phash}-{subject.keys_hash}-{rep}"'
    headers = {
        "ETag": etag,
        # Never immutable: the URL is shared by all viewers, and the browser serves an
        # immutable response without revalidation — on a shared computer or after
        # an account switch the next user would get someone else's rows
        # (found on the stand 2026-09-05: admin → dev-login tmr1 in the same
        # browser saw the full file). no-cache = revalidation by the ETag with
        # keys_hash on every open: the same viewer gets a 304.
        # Passthrough (libraries, identical for everyone) stays immutable by
        # ?v= — it holds no rows that depend on the viewer.
        "Cache-Control": ("private, max-age=31536000, immutable"
                          if passthrough and request.query_params.get("v") == fp
                          else "private, no-cache"),
        "Vary": "Accept-Encoding, Cookie",
    }
    inm = request.headers.get("if-none-match")
    if inm and etag in [t.strip() for t in inm.split(",")]:
        return Response(status_code=304, headers=headers)

    headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    media_filename = filename[:-3] if gz else filename

    if passthrough:
        payload = path.read_bytes()                # not data: libraries, assets
    else:
        key = (dashboard.id, version, slot_index, st.st_mtime_ns, st.st_size,
               via_fp, phash, subject.keys_hash)
        payload = cache.get(key)
        if payload is None:
            raw = path.read_bytes()
            payload, rows_in, rows_out = filter_bytes(
                policy, slot_index, filename, raw, subject.keys,
                lambda i: _load_slot(dashboard.id, version, i),
            )
            cache.put(key, payload)
            _log(db, subject.user_id, dashboard.id, {
                "slot": slot_index, "rows_in": rows_in, "rows_out": rows_out,
                "keys": len(subject.keys),
            })
            db.commit()

    if gz:
        if rep == "gzip":
            headers["Content-Encoding"] = "gzip"
            return Response(content=payload, media_type=_mime(media_filename),
                            headers=headers)
        try:
            payload = encoding.decode_bytes(payload, "gzip")
        except encoding.DecodedTooLarge:
            raise HTTPException(status_code=413,
                                detail="Stored file expands beyond the serving limit")
        headers["Content-Encoding"] = "identity"
        return Response(content=payload, media_type=_mime(media_filename),
                        headers=headers)

    resp = encoding.encoded_response(payload, accept, media_type=_mime(filename),
                                     headers=headers, enc=rep)
    # enc_headers() sets Vary: Accept-Encoding — for private serving that is
    # not enough: without Cookie a shared proxy would serve one viewer's file to another.
    resp.headers["Vary"] = "Accept-Encoding, Cookie"
    return resp


def _load_slot(dashboard_id: str, version: int, slot_index: int) -> tuple[str, bytes]:
    """Source for the via lookup. A separate function so that filter_bytes stays
    pure (tests substitute their own)."""
    path = storage.slot_file_path(dashboard_id, version, slot_index)
    if path is None:
        raise RlsError(403, f"RLS: slot {slot_index} is missing (via lookup)")
    return path.name[len(f"slot_{slot_index}_"):], path.read_bytes()
