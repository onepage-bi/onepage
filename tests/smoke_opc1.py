"""OPC1 (ADR-18): decode/encode of the columnar slot file.

Pins what the module exists for: encode() bytes match the generator's
reference encoder (otherwise a filtered file would read differently on the
client than the published one), a dict column's dictionary is rebuilt ONLY
from the surviving rows, i32 null and alignment survive a round trip.

The data is synthetic (one non-ASCII value keeps multi-byte UTF-8 covered).
"""
import gzip
import importlib.util
import json
import math
import os
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
from app.services import opc1  # noqa: E402

FAILURES = []

# Optional reference encoder to compare against; the check is skipped without it.
REF_PATH = Path(os.environ.get("OP_OPC1_REF", "opc1_reference.py"))


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  {extra}"))
    if not cond:
        FAILURES.append(label)


SCHEMA = [("territory", "dict"), ("customer", "dict"), ("qty", "i32"),
          ("amount", "f64"), ("share", "f32")]
ROWS = [
    ["Rep North-1", "C-001", "5", "1200.5", "0.25"],
    ["Rep South-2", "C-002", "", "0.0", "1.5"],
    ["Rep North-1", "C-003", "-7", "3.25", "0.75"],
    ["Rep Zürich-3", "C-004", "42", "", "2.0"],
]
HEADER = [n for n, _ in SCHEMA]


def cols_from_rows(rows):
    return {n: [r[i] for r in rows] for i, n in enumerate(HEADER)}


def main():
    body = opc1.encode(SCHEMA, cols_from_rows(ROWS))

    # --- format -----------------------------------------------------------
    check("MAGIC OPC1", body[:4] == b"OPC1", body[:8])
    (desc_len,) = struct.unpack_from("<I", body, 4)
    check("descriptor length is a multiple of 8", desc_len % 8 == 0, desc_len)
    desc_raw = body[8:8 + desc_len]
    check("descriptor is padded with spaces", desc_raw.rstrip() == desc_raw.rstrip(b" ")
          and desc_raw.endswith(b" ") or desc_raw[-1:] != b"\0", desc_raw[-8:])
    desc = json.loads(desc_raw.decode("utf-8"))
    check("rows in the descriptor", desc["rows"] == len(ROWS), desc["rows"])
    check("columns are 8-byte aligned", all(c["offset"] % 8 == 0 for c in desc["cols"]),
          [c["offset"] for c in desc["cols"]])
    check("i32 declares null", desc["cols"][2]["null"] == opc1.I32_NULL)
    check("dict carries values", desc["cols"][0]["values"] == ["Rep North-1", "Rep South-2", "Rep Zürich-3"],
          desc["cols"][0].get("values"))
    check("f64/f32 have no values/null", not ({"values", "null"} & set(desc["cols"][3]))
          and not ({"values", "null"} & set(desc["cols"][4])))

    # --- round-trip -------------------------------------------------------
    d2, cols = opc1.decode(body)
    check("decode: dict materialised into strings",
          cols["territory"] == [r[0] for r in ROWS], cols["territory"])
    check("decode: i32", cols["qty"] == [5, opc1.I32_NULL, -7, 42], cols["qty"])
    check("decode: empty i32 -> I32_NULL", cols["qty"][1] == opc1.I32_NULL)
    check("decode: f64", cols["amount"][0] == 1200.5 and math.isnan(cols["amount"][3]),
          cols["amount"])
    check("decode: f32", abs(cols["share"][0] - 0.25) < 1e-9, cols["share"])
    check("decode: schema_of", opc1.schema_of(d2) == SCHEMA, opc1.schema_of(d2))

    again = opc1.encode(opc1.schema_of(d2), cols)
    check("round-trip byte for byte", again == body, f"{len(again)} vs {len(body)}")

    # --- dict rebuild -----------------------------------------------------
    keep = [ROWS[1]]                                   # only "Rep South-2"
    filtered = opc1.encode(SCHEMA, cols_from_rows(keep))
    fdesc, fcols = opc1.decode(filtered)
    check("filter: 1 row", fdesc["rows"] == 1, fdesc["rows"])
    check("filter: dictionary rebuilt from the survivors",
          fdesc["cols"][0]["values"] == ["Rep South-2"], fdesc["cols"][0]["values"])
    check("filter: no other territory in the file's BYTES",
          b"North" not in filtered and "Zürich".encode() not in filtered)
    check("filter: the row's values are intact", fcols["customer"] == ["C-002"], fcols["customer"])

    empty = opc1.encode(SCHEMA, {n: [] for n in HEADER})
    edesc, ecols = opc1.decode(empty)
    check("empty file is valid", edesc["rows"] == 0 and ecols["territory"] == [])
    check("empty file: dictionary is empty", edesc["cols"][0]["values"] == [])

    # --- negatives ---------------------------------------------------------
    for label, fn in (
        ("not OPC1", lambda: opc1.decode(b"NOPE" + body[4:])),
        ("truncated file", lambda: opc1.decode(body[:12])),
        ("columns of different length", lambda: opc1.encode(
            SCHEMA, {**cols_from_rows(ROWS), "qty": ["1"]})),
        ("unknown type", lambda: opc1.encode([("a", "i64")], {"a": [1]})),
        ("missing column values", lambda: opc1.encode(SCHEMA, {"territory": ["x"]})),
    ):
        try:
            fn()
            check(f"negative: {label}", False, "no exception raised")
        except opc1.Opc1Error:
            check(f"negative: {label}", True)

    # --- byte for byte with the generator's reference encoder ----------------------
    if REF_PATH.exists():
        spec = importlib.util.spec_from_file_location("ref_opc1", REF_PATH)
        ref = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ref)
        with tempfile.TemporaryDirectory(prefix="op-opc1-") as tmp:
            dst = Path(tmp) / "slot.bin.gz"
            ref.pack_rows("synthetic", HEADER, iter([list(r) for r in ROWS]), dst, SCHEMA)
            ref_body = gzip.decompress(dst.read_bytes())
        check("byte for byte with the reference encoder", ref_body == body,
              f"ref {len(ref_body)} != {len(body)}")
        rdesc, rcols = opc1.decode(ref_body)
        check("decode reads the reference output", rcols["territory"] == [r[0] for r in ROWS])
    else:
        print("SKIP reference encoder not found: " + str(REF_PATH))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
