"""A report as a single file: the document together with its data.

Why. "Download current version" hands out `dashboard.html` — the source, and only
that. A report that keeps its data in slots (interactive, and the hybrid
"inline snapshot + slot" too) has no data in that file: the platform feeds it
through the `op_ready` → `op_file` bridge at view time, because the report itself sits
in a sandbox without `allow-same-origin` and cannot request anything. The downloaded
file therefore opens empty, and to a person that looks like
"it didn't export completely".

What this module does. It takes THE SAME document that `/render/{id}/` serves
(same build, same injections — see `files.build_render_body`), embeds the
slot bytes into it and appends a piece that, on page load, sends the
report the very same `op_file`. The protocol does not change by a single byte — only
the sender changes: not the parent window but the page itself. The output is one
html that works on double-click, without the platform, without a server and without a network.

Three decisions made here deliberately:

1. **The snapshot is inline even if `OP_SNAPSHOT_EXTERNAL` is on.** An external
   `snapshot.js` is a relative address that a file on disk does not have;
   this is the only way not to hand out a report that is known to be empty.
2. **A slot the document itself references is an ASSET, not data.**
   The platform serves `<script src="x.js">` from the slot with the same original
   name (`files.serve_slot_asset`); in a single file such a slot must be
   pasted into the document, not sent as a message. All other slots are data.
3. **gzip is decompressed by the browser** (`DecompressionStream`), brotli by us:
   browsers have no brotli decompressor, and a compressed slot would reach the report as
   garbage. This is exactly the bug the mirror serving already got burned by —
   "a file name says nothing about its encoding".

The module does not parse data: it reads bytes, re-encodes them to base64, counts
the length. Not a single value from a slot reaches the log, the audit or the messages.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

from ..models import Dashboard
from . import storage

# Limit for the resulting file. Base64 adds a third, and the browser opens such
# a document entirely in memory: 64 MB is already "slow", beyond that it is more honest
# to refuse with a number in hand than to hand out something that will not open.
MAX_BYTES = 64 * 1024 * 1024

# Assets we know how to paste into the document. Images and fonts are deliberately NOT
# included: data: URIs in markup need careful substitution and that is a separate
# task; for such a slot the function honestly leaves an entry in notes.
_INLINABLE = {".js": "script", ".css": "style"}

_BODY_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def _bare(name: str) -> str:
    """Asset name without the transport suffix: `x.js.gz` → `x.js`."""
    for suffix in (".gz", ".br"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _ext(name: str) -> str:
    bare = _bare(name)
    dot = bare.rfind(".")
    return bare[dot:].lower() if dot > 0 else ""


def _tag_re(asset: str) -> re.Pattern[str]:
    """The tag by which the document references the asset (with and without `./`, any quotes)."""
    n = re.escape(asset)
    return re.compile(
        r"""<script\b[^>]*\bsrc\s*=\s*["']\.?/?%s(?:\?[^"']*)?["'][^>]*>\s*</script\s*>"""
        r"""|<link\b[^>]*\bhref\s*=\s*["']\.?/?%s(?:\?[^"']*)?["'][^>]*>""" % (n, n),
        re.IGNORECASE,
    )


def _plain(data: bytes, name: str) -> bytes:
    """Decompressed slot bytes (for pasting the asset into the document text)."""
    if name.endswith(".gz") or data[:2] == b"\x1f\x8b":
        import gzip
        return gzip.decompress(data)
    if name.endswith(".br"):
        import brotli  # present in the container (encoding.py); just in case — raise it upward
        return brotli.decompress(data)
    return data


def _payload(data: bytes, name: str) -> tuple[str, str]:
    """(base64, attachment encoding) for a DATA slot.

    gzip is left as is — the browser decompresses it, and the file does not swell
    several times over. brotli is decompressed here: the browser has no decompressor for it.
    """
    if name.endswith(".gz") or data[:2] == b"\x1f\x8b":
        return base64.b64encode(data).decode("ascii"), "gzip"
    if name.endswith(".br"):
        data = _plain(data, name)
    return base64.b64encode(data).decode("ascii"), "identity"


def _jsstr(s: str) -> str:
    """A string as a JS literal: no substitution may close the <script>."""
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + out.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") + '"'


# Next to the module, NOT in services/data/: the `data/` rule in .gitignore catches
# any directory with that name at any depth, and a file from there silently did not
# make it into the commit — the patch went to the stand without the bridge, and the button answered 500.
# The three files that live there were once added with `git add -f`;
# that cannot be relied upon.
_BRIDGE_PATH = Path(__file__).resolve().parent / "standalone_bridge.js"


def _bridge() -> str:
    """The bridge lives in a separate file: inside is ordinary JS with comments, and
    its place is where it can be read and edited as code, not inside
    a Python string."""
    return _BRIDGE_PATH.read_text(encoding="utf-8")


def build(dashboard: Dashboard, render_body: bytes, *,
          t_no_gzip: str, t_failed: str) -> tuple[bytes, list[dict]]:
    """Build the single file. Returns (document, audit records).

    Records are structures, not phrases: they go into `audit_log.detail`, where
    they are useful for querying ("what was exported and how it was embedded"), and the log is not
    an interface and is not translated.

    `render_body` is the finished `/render/{id}/` document, built by the caller with
    an inline snapshot (`files.build_render_body(..., snapshot_external=False)`).
    The split is not for looks: all injection logic lives in ONE place, and
    these two builds have no way to diverge.

    `t_no_gzip` / `t_failed` are two strings for the person who opens the file;
    they come from outside because they are translated (`_()`), and the module must not know
    the request's language.
    """
    html = render_body.decode("utf-8", errors="replace")
    notes: list[dict] = []
    version = dashboard.current_version
    data_slots: list[str] = []

    for rec in storage.list_slot_files(dashboard.id, version):
        name = rec["name"]
        path = storage.slot_file_path(dashboard.id, version, rec["slot"])
        if path is None:            # vanished between listing and reading
            continue
        blob = path.read_bytes()

        asset = _bare(name)
        tag = _tag_re(asset)
        if tag.search(html):
            # This is an asset: the document references it itself, and it cannot be
            # fed as a message — the browser will fetch it by its relative address.
            kind = _INLINABLE.get(_ext(name))
            if kind is None:
                notes.append({"slot": rec["slot"], "name": name, "as": "asset-skipped"})
                continue
            body = _plain(blob, name).decode("utf-8", errors="replace")
            # `</script>` inside the code would close our tag too early.
            body = body.replace("</script", "<\\/script")
            html = tag.sub(lambda _m: f"<{kind}>\n{body}\n</{kind}>", html, count=1)
            notes.append({"slot": rec["slot"], "name": name, "as": kind})
            continue

        b64, enc = _payload(blob, name)
        data_slots.append('{"slot":%d,"name":%s,"enc":"%s","b64":"%s"}'
                          % (rec["slot"], _jsstr(name), enc, b64))
        notes.append({"slot": rec["slot"], "name": name, "as": enc, "bytes": len(blob)})

    if data_slots:
        bridge = (_bridge().replace("__OP_SLOTS__", "[" + ",".join(data_slots) + "]")
                  .replace("__OP_T_OLD__", _jsstr(t_no_gzip))
                  .replace("__OP_T_FAIL__", _jsstr(t_failed)))
        if _BODY_RE.search(html):
            html = _BODY_RE.sub(lambda m: bridge + m.group(0), html, count=1)
        else:
            # A document without </body>: append at the end. The browser will cope, while
            # keeping quiet and handing out a file without data is exactly the breakage
            # this module was created to prevent.
            html = html + bridge
            notes.append({"as": "bridge-appended-at-end"})
    else:
        notes.append({"as": "no-data-slots"})

    doc = html.encode("utf-8")
    return doc, notes
