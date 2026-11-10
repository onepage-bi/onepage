#!/usr/bin/env python3
"""App icon for "OnePage — My reports" (.ico with PNGs inside).

    tools/make_app_icon.py [--check] [--out PATH]

Writes `deploy/agent_tools/onepage.ico`, from where make-agent-kit.sh copies it.
Pure stdlib (zlib + struct): Pillow will not go into the kit's offline wheels, and
it is not worth pulling in for one icon. Sizes 16/32/48/256; the ICO holds PNGs
(Windows reads that since Vista; 256 as BMP would be 256 KB instead of a couple of KB).

Design: a blue rounded square and a white sheet with a folded corner and three
lines. Nothing national and no lettering: the audience is international,
and text at 16 pixels is unreadable anyway.

Deterministic: no time, no randomness, a fixed zlib level, so
`--check` regenerates and compares byte for byte with the file in the repository.
Drawing uses 4x4 supersampling and the alpha channel: edges are smooth on any
taskbar background, light or dark.
"""
from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "deploy" / "agent_tools" / "onepage.ico"
SIZES = (16, 32, 48, 256)
SS = 4                                   # supersampling: SS x SS subsamples per pixel

BLUE = (0x1F, 0x5F, 0xBF)
PAPER = (0xFF, 0xFF, 0xFF)
FOLD = (0xBF, 0xD4, 0xF2)
LINE = (0x1F, 0x5F, 0xBF)


def _shape(u: float, v: float):
    """RGBA colour at a point of the unit square, or None (transparent)."""
    # rounded background square
    r = 0.2
    cx = min(max(u, r), 1 - r)
    cy = min(max(v, r), 1 - r)
    if (u - cx) ** 2 + (v - cy) ** 2 > r * r:
        return None
    # sheet: x 0.26..0.74, y 0.16..0.84, top-right corner cut off
    px0, px1, py0, py1, cut = 0.26, 0.74, 0.16, 0.84, 0.16
    if px0 <= u <= px1 and py0 <= v <= py1:
        if u > px1 - cut and v < py0 + cut:
            # corner square: above the sheet's diagonal there is no sheet (background), below it - the fold
            return (*BLUE, 255) if (u - (px1 - cut)) > (v - py0) else (*FOLD, 255)
        # three lines of text
        for y0 in (0.40, 0.54, 0.68):
            if y0 <= v <= y0 + 0.055 and px0 + 0.07 <= u <= px1 - 0.07:
                return (*LINE, 255)
        return (*PAPER, 255)
    return (*BLUE, 255)


def _render(n: int) -> bytes:
    """Raw RGBA rows (with the filter byte) for an n x n PNG."""
    rows = []
    for y in range(n):
        row = bytearray([0])
        for x in range(n):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    c = _shape((x + (sx + .5) / SS) / n, (y + (sy + .5) / SS) / n)
                    if c:
                        a = c[3]
                        acc[0] += c[0] * a
                        acc[1] += c[1] * a
                        acc[2] += c[2] * a
                        acc[3] += a
            if acc[3]:
                row += bytes((acc[0] // acc[3], acc[1] // acc[3], acc[2] // acc[3],
                              acc[3] // (SS * SS)))
            else:
                row += b"\0\0\0\0"
            # transparent background: colour at alpha=0 is zeroed on purpose, otherwise the bytes
            # would depend on traversal order and break determinism
        rows.append(bytes(row))
    return b"".join(rows)


def _png(n: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    ihdr = struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0)       # 8 bits, RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(_render(n), 9)) + chunk(b"IEND", b""))


def build() -> bytes:
    pngs = [_png(n) for n in SIZES]
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries = b""
    for n, data in zip(SIZES, pngs):
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32,
                               len(data), offset)               # 256 is written as 0
        offset += len(data)
    return head + entries + b"".join(pngs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="compare with the file in the repository, write nothing")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    data = build()
    if a.check:
        if not a.out.is_file() or a.out.read_bytes() != data:
            print(f"STALE: {a.out} - run tools/make_app_icon.py", file=sys.stderr)
            return 1
        print(f"OK: {a.out} ({len(data)} bytes)")
        return 0
    a.out.write_bytes(data)
    print(f"wrote {a.out} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
