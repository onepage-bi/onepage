"""OPC1 — the columnar binary format of slot files (ADR-18). Stdlib only.

Format (reference: GREENPLUM_DASHBOARD_AUTHORING_RU §5.4, the generator's encoder
liga-bundle/ligafmc/opc1.py):

    MAGIC "OPC1" | uint32 LE len(desc) | JSON descriptor (padded with SPACES to 8)
    | columns, each aligned to 8 bytes. Little-endian.

Descriptor: {"rows": N, "cols": [{"name","kind","offset"[,"values"|"null"]}]}.
kind: i32 | f32 | f64 | dict. i32 carries "null": -2147483648 (int32 min).
dict carries "values" — a dictionary of strings, code = index into that list.

BOTH sides live here: decode() for filtering on serve and encode() for
building the filtered file. The byte output of encode() matches the reference
encoder (see tests/smoke_opc1.py) — otherwise the RLS output would differ from
what the generator published, and the client reads both with the same code.

KEY for RLS: encode() takes ALREADY materialised strings of dict columns
and builds the dictionary anew — only from the strings passed in. Reusing the dictionary
of the source file is not allowed: values of other territories would leak through the descriptor
even if not a single row with them survived.
"""
from __future__ import annotations

import array
import json
import struct
import sys

MAGIC = b"OPC1"
ALIGN = 8                      # TypedArray throws RangeError on an unaligned offset
I32_NULL = -2147483648         # int32 min == "no value"; declared EXPLICITLY in the descriptor

if sys.byteorder != "little":
    raise RuntimeError("OPC1 is a little-endian format")

_TYPECODE = {"i32": "i", "f32": "f", "f64": "d", "dict": "I"}
_HEAD = len(MAGIC) + 4


class Opc1Error(ValueError):
    """A broken or foreign file. Always loud: silently serving an "empty" report instead of
    data is the same lie as serving someone else's rows."""


def decode(raw: bytes) -> tuple[dict, dict[str, list]]:
    """raw (already decompressed) -> (desc, cols).

    cols[name] is a list of Python values: dict columns are MATERIALISED into strings
    (code replaced by the dictionary value), i32 is returned as is (I32_NULL stays
    a number — the caller knows about desc["cols"][i]["null"]), f32/f64 are float
    (NaN where the source was empty).
    """
    if len(raw) < _HEAD or raw[:4] != MAGIC:
        raise Opc1Error("not OPC1: no signature")
    (desc_len,) = struct.unpack_from("<I", raw, 4)
    if desc_len > len(raw) - _HEAD:
        raise Opc1Error("descriptor length exceeds the file")
    try:
        desc = json.loads(raw[_HEAD:_HEAD + desc_len].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise Opc1Error(f"descriptor cannot be parsed: {e}")
    if not isinstance(desc, dict) or not isinstance(desc.get("cols"), list):
        raise Opc1Error("descriptor without cols")
    rows = desc.get("rows")
    if not isinstance(rows, int) or rows < 0:
        raise Opc1Error("descriptor without rows")

    base = _HEAD + desc_len
    cols: dict[str, list] = {}
    for col in desc["cols"]:
        if not isinstance(col, dict):
            raise Opc1Error("descriptor column is not an object")
        name, kind = col.get("name"), col.get("kind")
        tc = _TYPECODE.get(kind)
        if not isinstance(name, str) or tc is None:
            raise Opc1Error(f"column {name!r}: unknown type {kind!r}")
        off = col.get("offset")
        if not isinstance(off, int) or off < 0:
            raise Opc1Error(f"column {name!r}: no offset")
        a = array.array(tc)
        need = rows * a.itemsize
        start = base + off
        if start + need > len(raw):
            raise Opc1Error(f"column {name!r}: data truncated")
        a.frombytes(raw[start:start + need])
        if kind == "dict":
            values = col.get("values")
            if not isinstance(values, list):
                raise Opc1Error(f"column {name!r}: dict without values")
            n = len(values)
            out = []
            for code in a:
                if code >= n:
                    raise Opc1Error(f"column {name!r}: code {code} outside the dictionary")
                out.append(values[code])
            cols[name] = out
        else:
            cols[name] = list(a)
    return desc, cols


def schema_of(desc: dict) -> list[tuple[str, str]]:
    """[(name, kind)] in descriptor order — the input for encode()."""
    return [(c["name"], c["kind"]) for c in desc["cols"]]


def encode(schema: list[tuple[str, str]], rows_cols: dict[str, list],
           null_i32: int = I32_NULL) -> bytes:
    """schema is [(column, type)] in file order; rows_cols are the column values
    (dict columns as Python strings). Returns the OPC1 body WITHOUT gzip.

    Dictionaries of dict columns are built anew, codes are assigned in order of a string's first
    appearance — exactly as in the reference encoder.
    """
    names = [n for n, _ in schema]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise Opc1Error(f"duplicate column in schema: {dup}")
    bad = [k for _, k in schema if k not in _TYPECODE]
    if bad:
        raise Opc1Error(f"unknown column type: {sorted(set(bad))}")
    missing = [n for n in names if n not in rows_cols]
    if missing:
        raise Opc1Error(f"no values for columns {missing}")

    rows = len(rows_cols[names[0]]) if names else 0
    ragged = [n for n in names if len(rows_cols[n]) != rows]
    if ragged:
        # Different column lengths would give a file where row N in one column
        # is row M in another: a plausible wrong report.
        raise Opc1Error(f"columns of different length: {ragged}")

    arrays: dict[str, array.array] = {}
    dicts: dict[str, list[str]] = {}
    for n, k in schema:
        a = array.array(_TYPECODE[k])
        src = rows_cols[n]
        if k == "dict":
            index: dict[str, int] = {}
            values: list[str] = []
            for v in src:
                s = "" if v is None else str(v)
                c = index.get(s)
                if c is None:
                    c = len(values)
                    index[s] = c
                    values.append(s)
                a.append(c)
            dicts[n] = values
        elif k == "i32":
            for v in src:
                if v is None or v == "":
                    a.append(null_i32)
                else:
                    try:
                        a.append(int(v))
                    except (ValueError, OverflowError) as e:
                        raise Opc1Error(f"column {n!r}: {v!r} is not int32 ({e})")
        else:  # f32 / f64
            for v in src:
                if v is None or v == "":
                    a.append(float("nan"))
                else:
                    try:
                        a.append(float(v))
                    except (ValueError, OverflowError):
                        a.append(float("nan"))
        arrays[n] = a

    payload = bytearray()
    desc_cols = []
    for n, k in schema:
        payload += b"\0" * (-len(payload) % ALIGN)      # column alignment
        col = {"name": n, "kind": k, "offset": len(payload)}
        if k == "dict":
            col["values"] = dicts[n]
        elif k == "i32":
            col["null"] = null_i32                      # the client MUST know it
        desc_cols.append(col)
        payload += arrays[n].tobytes()

    desc = json.dumps({"rows": rows, "cols": desc_cols},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # Pad with SPACES, not zeros: JSON.parse tolerates trailing whitespace and
    # fails on \0. This way the data start is aligned without a second formula in JS.
    desc += b" " * (-len(desc) % ALIGN)
    return MAGIC + struct.pack("<I", len(desc)) + bytes(desc) + bytes(payload)
