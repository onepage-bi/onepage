#!/usr/bin/env python3
"""companion_sprites.py -- chroma-key MP4 clips into sprite-strip PNGs.

Usage:
  python tools/companion_sprites.py --in <folder of .mp4 clips on a green background> \
      --out static/img/quests/companion/sheets [--fps 12] [--size 256] [--pad 0.06]
  Clip name = file name without extension (run_in.mp4 -> run_in.png in --out).
  For every clip a <clip>_contact.jpg is also written (a sheet of numbered
  frames, 6 columns) -- so a person can choose frame ranges.
  The sheets.json manifest in --out is merged with the existing entries.
  Self-test: python tools/companion_sprites.py --selftest

Requires: ffmpeg + ffprobe on PATH (or under /opt/homebrew/bin). No Pillow, no
numpy -- frames are handled as raw RGBA byte buffers and scanned with plain
bytes.translate()/find()/rfind(), which is implemented in C and fast enough
for clips of a few seconds at a few dozen frames per second.

Chroma key filter chain: `colorkey=0x00FF00:0.30:0.12,despill=type=green:
mix=0.5:expand=0`. `colorkey` does the actual keying (similarity 0.30 catches
JPEG/H.264 compression noise around the green without eating into the
character; blend 0.12 gives a soft edge instead of a jagged cutout).
`despill` afterwards removes the green fringe/spill that chroma keying alone
leaves on hair/edges, which is the visible "green halo" artifact. This chain
is more robust than `chromakey` (HSV-based) for a flat, evenly lit green
background, and cheaper than rotoscoping with cropdetect (which does not
understand alpha and only detects black borders).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

CHROMA_FILTER = "colorkey=0x00FF00:0.30:0.12,despill=type=green:mix=0.5:expand=0"
ALPHA_THRESHOLD = 32  # 0..255, pixels with alpha >= this are "opaque subject"
MAX_FRAMES = 96
CONTACT_COLUMNS = 6
CONTACT_THUMB_W = 96


class SpriteError(RuntimeError):
    """Raised for user-facing, actionable failures (missing ffmpeg, no green, ...)."""


# --------------------------------------------------------------------------
# ffmpeg / ffprobe plumbing
# --------------------------------------------------------------------------

def _find_tool(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    fallback = f"/opt/homebrew/bin/{name}"
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    return None


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


def require_ffmpeg() -> None:
    missing = [n for n, p in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)) if not p]
    if missing:
        raise SpriteError(
            "Missing required tool(s): " + ", ".join(missing) + ". "
            "Install ffmpeg (e.g. `brew install ffmpeg`) or make sure it is on PATH."
        )


def run_capture(cmd: Sequence[str], input_bytes: Optional[bytes] = None) -> bytes:
    """Run a subprocess with no shell, return stdout, raise SpriteError on failure."""
    try:
        proc = subprocess.run(
            list(cmd),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SpriteError(f"Failed to launch {cmd[0]}: {exc}") from exc
    if proc.returncode != 0:
        stderr_tail = proc.stderr[-4000:].decode("utf-8", "replace")
        raise SpriteError(f"Command failed ({cmd[0]}): {' '.join(cmd)}\n{stderr_tail}")
    return proc.stdout


def probe_video(path: str) -> Tuple[int, int, float]:
    """Return (width, height, duration_seconds) for a video file."""
    require_ffmpeg()
    if not os.path.isfile(path):
        raise SpriteError(f"Clip not found: {path}")
    out = run_capture([
        FFPROBE, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-show_entries", "format=duration",
        "-of", "json", path,
    ])
    data = json.loads(out.decode("utf-8"))
    streams = data.get("streams") or []
    if not streams:
        raise SpriteError(f"No video stream found in {path}")
    width = int(streams[0]["width"])
    height = int(streams[0]["height"])
    duration = float(data.get("format", {}).get("duration") or 0.0)
    if duration <= 0:
        raise SpriteError(f"Could not determine duration of {path}")
    return width, height, duration


# --------------------------------------------------------------------------
# raw RGBA extraction + alpha bbox scanning (pure python, no numpy/Pillow)
# --------------------------------------------------------------------------

def extract_rgba_frames(path: str, fps: float, width: int, height: int) -> bytes:
    """Extract chroma-keyed frames as one big raw RGBA buffer via ffmpeg."""
    vf = f"fps={fps},{CHROMA_FILTER},format=rgba"
    return run_capture([
        FFMPEG, "-v", "error", "-i", path, "-vf", vf,
        "-f", "rawvideo", "-pix_fmt", "rgba", "-",
    ])


_ALPHA_TABLE = bytes(0 if i < ALPHA_THRESHOLD else 1 for i in range(256))
_ONE = b"\x01"


def frame_alpha_bbox(frame: bytes, width: int, height: int) -> Optional[Tuple[int, int, int, int]]:
    """Return (x0, y0, x1, y1) exclusive bbox of "opaque enough" pixels, or None."""
    alpha = frame[3::4]
    marked = alpha.translate(_ALPHA_TABLE)
    x0 = y0 = x1 = y1 = None
    row_bytes = width
    for y in range(height):
        row = marked[y * row_bytes:(y + 1) * row_bytes]
        i = row.find(_ONE)
        if i == -1:
            continue
        j = row.rfind(_ONE)
        if y0 is None:
            y0 = y
        y1 = y
        if x0 is None or i < x0:
            x0 = i
        if x1 is None or j > x1:
            x1 = j
    if x0 is None:
        return None
    return (x0, y0, x1 + 1, y1 + 1)


def opaque_fraction(buf: bytes, width: int, height: int, frame_size: int) -> float:
    """Fraction of pixels across the whole buffer that are "opaque enough"."""
    total_pixels = 0
    opaque_pixels = 0
    n = len(buf) // frame_size
    for k in range(n):
        frame = buf[k * frame_size:(k + 1) * frame_size]
        alpha = frame[3::4]
        marked = alpha.translate(_ALPHA_TABLE)
        opaque_pixels += marked.count(1)
        total_pixels += width * height
    if total_pixels == 0:
        return 0.0
    return opaque_pixels / total_pixels


def union_bbox(
    frames: Sequence[bytes], width: int, height: int
) -> Tuple[int, int, int, int]:
    x0 = y0 = None
    x1 = y1 = None
    for frame in frames:
        bbox = frame_alpha_bbox(frame, width, height)
        if bbox is None:
            continue
        bx0, by0, bx1, by1 = bbox
        x0 = bx0 if x0 is None else min(x0, bx0)
        y0 = by0 if y0 is None else min(y0, by0)
        x1 = bx1 if x1 is None else max(x1, bx1)
        y1 = by1 if y1 is None else max(y1, by1)
    if x0 is None:
        raise SpriteError(
            "No opaque subject pixels survived the chroma key in any frame "
            "-- check the green background and colorkey thresholds."
        )
    return x0, y0, x1, y1


def pad_and_square(
    bbox: Tuple[int, int, int, int], width: int, height: int, pad_frac: float
) -> Tuple[int, int, int, int]:
    """Pad bbox by pad_frac of its size, then grow to a square, clamped to frame."""
    x0, y0, x1, y1 = (float(v) for v in bbox)
    bw, bh = x1 - x0, y1 - y0
    pad_x, pad_y = bw * pad_frac, bh * pad_frac
    x0 -= pad_x
    x1 += pad_x
    y0 -= pad_y
    y1 += pad_y
    bw, bh = x1 - x0, y1 - y0
    side = min(max(bw, bh), float(min(width, height)))
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    nx0, ny0 = cx - side / 2.0, cy - side / 2.0
    nx1, ny1 = nx0 + side, ny0 + side
    if nx0 < 0:
        nx1 -= nx0
        nx0 = 0.0
    if ny0 < 0:
        ny1 -= ny0
        ny0 = 0.0
    if nx1 > width:
        shift = nx1 - width
        nx0 = max(0.0, nx0 - shift)
        nx1 = float(width)
    if ny1 > height:
        shift = ny1 - height
        ny0 = max(0.0, ny0 - shift)
        ny1 = float(height)
    cx0, cy0 = int(round(nx0)), int(round(ny0))
    cw = min(int(round(nx1 - nx0)), width - cx0)
    ch = min(int(round(ny1 - ny0)), height - cy0)
    cw = max(cw, 1)
    ch = max(ch, 1)
    return cx0, cy0, cw, ch


def scale_dims(crop_w: int, crop_h: int, size: int) -> Tuple[int, int]:
    if crop_w >= crop_h:
        out_w = size
        out_h = max(1, round(crop_h * size / crop_w))
    else:
        out_h = size
        out_w = max(1, round(crop_w * size / crop_h))
    return out_w, out_h


def pick_subsample_indices(n: int, keep: int) -> List[int]:
    if n <= keep:
        return list(range(n))
    if keep <= 1:
        return [0]
    return [round(i * (n - 1) / (keep - 1)) for i in range(keep)]


# --------------------------------------------------------------------------
# assembly: strip PNG + contact sheet JPG via ffmpeg fed with raw RGBA stdin
# --------------------------------------------------------------------------

# WebP refuses a side over 16383 px and browsers cap a texture at 16384, so a
# long clip is laid out as a GRID rather than one row; the manifest carries
# `cols` and the player reads row/column from the frame index.
MAX_SHEET_PX = 16000


def grid_cols(n: int, cell_w: int) -> int:
    per_row = max(1, MAX_SHEET_PX // max(1, cell_w))
    if n <= per_row:
        return max(1, n)
    # Square-ish, but never wider than the limit.
    import math as _m
    cols = min(per_row, max(1, int(_m.ceil(_m.sqrt(n)))))
    return cols


def build_strip_png(
    frames: Sequence[bytes], width: int, height: int,
    crop: Tuple[int, int, int, int], out_w: int, out_h: int, out_path: str,
    cols: Optional[int] = None, gain: Optional[Tuple[float, float, float]] = None,
) -> None:
    cx, cy, cw, ch = crop
    n = len(frames)
    c = cols or grid_cols(n, out_w)
    rows = (n + c - 1) // c
    tone = ""
    if gain:
        # Match this shoot's tone to the reference one. A per-channel gain, not
        # a hue shift: the difference between generations is exposure and white
        # balance, and multiplying each channel is exactly what a colourist
        # does to match two takes.
        tone = (f"colorchannelmixer=rr={gain[0]:.4f}:gg={gain[1]:.4f}:bb={gain[2]:.4f},")
    vf = (f"crop={cw}:{ch}:{cx}:{cy},{tone}scale={out_w}:{out_h}:flags=lanczos,"
          f"tile={c}x{rows}:color=0x00000000")
    buf = b"".join(frames)
    run_capture([
        FFMPEG, "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}",
        "-r", "1", "-i", "-",
        "-vf", vf, "-frames:v", "1", out_path,
    ], input_bytes=buf)


_DRAWTEXT_AVAILABLE: Optional[bool] = None


def drawtext_available() -> bool:
    """Whether this ffmpeg build has the drawtext filter (needs libfreetype)."""
    global _DRAWTEXT_AVAILABLE
    if _DRAWTEXT_AVAILABLE is None:
        try:
            out = run_capture([FFMPEG, "-hide_banner", "-filters"])
        except SpriteError:
            _DRAWTEXT_AVAILABLE = False
        else:
            _DRAWTEXT_AVAILABLE = b" drawtext " in out or b" drawtext\n" in out
    return _DRAWTEXT_AVAILABLE


def find_font() -> Optional[str]:
    """Return a usable font file for drawtext, or None if numbering is unavailable."""
    if not drawtext_available():
        return None
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def build_contact_sheet(
    frames: Sequence[bytes], width: int, height: int,
    crop: Tuple[int, int, int, int], out_path: str, font: Optional[str],
) -> None:
    cx, cy, cw, ch = crop
    thumb_w = CONTACT_THUMB_W
    thumb_h = max(1, round(ch * thumb_w / cw))
    n = len(frames)
    cols = CONTACT_COLUMNS
    rows = math.ceil(n / cols)
    pad_needed = rows * cols - n
    padded_frames = list(frames) + [frames[-1]] * pad_needed

    fg = f"crop={cw}:{ch}:{cx}:{cy},scale={thumb_w}:{thumb_h}:flags=lanczos,format=rgba"
    if font:
        safe_font = font.replace(":", r"\:")
        fg += (
            f",drawtext=fontfile='{safe_font}':text='%{{n}}':x=3:y=3:"
            "fontsize=13:fontcolor=white:box=1:boxcolor=black@0.55"
        )
    filter_complex = (
        f"[0:v]{fg}[fg];"
        f"color=c=0x2b2b2b:s={thumb_w}x{thumb_h}[bg];"
        f"[bg][fg]overlay=shortest=1,format=yuv420p,tile={cols}x{rows}[outv]"
    )
    buf = b"".join(padded_frames)
    run_capture([
        FFMPEG, "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}",
        "-r", "1", "-i", "-",
        "-filter_complex", filter_complex, "-map", "[outv]",
        "-frames:v", "1", "-q:v", "3", out_path,
    ], input_bytes=buf)


# --------------------------------------------------------------------------
# per-clip pipeline
# --------------------------------------------------------------------------

@dataclass
class ClipResult:
    name: str
    frames: int
    fps: float
    w: int
    h: int
    png_bytes: int
    cols: int = 0
    cx: float = 0.5
    by: float = 1.0
    by_end: float = 1.0
    by_max: float = 1.0
    ey: float = 0.43
    gaze: Optional[List[List[float]]] = None




# Dark enough to be an eye, a nose or an ear rim on this character.
_DARK = (95, 80, 75)


def eye_row(frame: bytes, width: int, height: int,
            crop: Tuple[int, int, int, int]) -> float:
    """The eye line of one frame, as a fraction of the CROPPED cell.

    Two eyes in one row make it the darkest row of the upper body; the search
    is limited to the top 45 % of the character so the bandana, which is navy,
    cannot win it.
    """
    bb = frame_alpha_bbox(frame, width, height)
    if bb is None:
        return 0.43
    top, bot = bb[1], bb[3]
    limit = top + int((bot - top) * 0.45)
    best_y, best_n = top, -1
    for y in range(top, max(top + 1, limit)):
        row = frame[(y * width) * 4:((y + 1) * width) * 4]
        n = 0
        for x in range(bb[0], bb[2]):
            i = x * 4
            if row[i + 3] > 200 and row[i] < _DARK[0] and row[i + 1] < _DARK[1] and row[i + 2] < _DARK[2]:
                n += 1
        if n > best_n:
            best_n, best_y = n, y
    return round((best_y - crop[1]) / float(crop[3]), 4)


def first_frame(clip_path: str, width: int, height: int) -> Optional[bytes]:
    """The clip's first chroma-keyed frame."""
    buf = extract_rgba_frames(clip_path, 2.0, width, height)
    fsz = width * height * 4
    return buf[:fsz] if len(buf) >= fsz else None


def frame_tone(frame: bytes, width: int, height: int,
               crop: Tuple[int, int, int, int]) -> Optional[Tuple[float, float, float, bytes]]:
    """Mean colour of ONE frame, plus its coarse alpha mask.

    Tone is only comparable between the SAME pose: a curled-up sleeping fox
    shows a different mix of fur to a sitting one, so matching whole-clip means
    corrects for the pose and wrecks the colour (measured: it moved the sleep
    clip from 2.3 to 12.3 units away from the reference). The mask is what
    decides whether two frames show the same pose at all.
    """
    x0, y0 = crop[0], crop[1]
    x1, y1 = min(crop[0] + crop[2], width), min(crop[1] + crop[3], height)
    sr = sg = sb = 0
    count = 0
    mask = bytearray()
    for y in range(y0, y1, 4):
        row = y * width * 4
        for x in range(x0, x1, 4):
            i = row + x * 4
            on = 1 if frame[i + 3] > 200 else 0
            mask.append(on)
            if on:
                sr += frame[i]; sg += frame[i + 1]; sb += frame[i + 2]; count += 1
    if count < 200:
        return None
    return (sr / count, sg / count, sb / count, bytes(mask))


def fur_mean(path: str) -> Optional[Tuple[float, float, float]]:
    """Mean colour of the ORANGE FUR in a still image.

    The sprites come through an h264 encode with subsampled chroma, so their
    tone drifts a few units from the art the character is drawn from — enough
    to notice next to the cards and the intro picture (owner, 2026-09-12: "it's
    a slightly different shade there, too"). This is the tone to land on. Only
    the fur is measured: the bandana and the cream chest would drag a plain
    average around as the pose changes.
    """
    raw = run_capture([FFMPEG, "-v", "error", "-i", path, "-vf", "format=rgba",
                       "-f", "rawvideo", "-pix_fmt", "rgba", "-"])
    sr = sg = sb = 0
    n = 0
    for i in range(0, len(raw), 4):
        if raw[i + 3] > 200 and raw[i] > 120 and (raw[i] - raw[i + 2]) > 40:
            sr += raw[i]; sg += raw[i + 1]; sb += raw[i + 2]; n += 1
    if n < 500:
        return None
    return (sr / n, sg / n, sb / n)


def frame_fur_mean(frame: bytes, width: int, height: int,
                   crop: Tuple[int, int, int, int]) -> Optional[Tuple[float, float, float]]:
    """The same measurement on one keyed frame."""
    x0, y0 = crop[0], crop[1]
    x1, y1 = min(crop[0] + crop[2], width), min(crop[1] + crop[3], height)
    sr = sg = sb = 0
    n = 0
    for y in range(y0, y1):
        row = y * width * 4
        for x in range(x0, x1):
            i = row + x * 4
            if frame[i + 3] > 200 and frame[i] > 120 and (frame[i] - frame[i + 2]) > 40:
                sr += frame[i]; sg += frame[i + 1]; sb += frame[i + 2]; n += 1
    if n < 500:
        return None
    return (sr / n, sg / n, sb / n)


def mask_iou(a: bytes, b: bytes) -> float:
    """How much two silhouettes agree, 0..1."""
    if not a or not b or len(a) != len(b):
        return 0.0
    inter = union = 0
    for i in range(len(a)):
        if a[i] or b[i]:
            union += 1
            if a[i] and b[i]:
                inter += 1
    return (inter / union) if union else 0.0



def gaze_vectors(frames: Sequence[bytes], width: int, height: int,
                 crop: Tuple[int, int, int, int]) -> List[List[float]]:
    """Where each frame is LOOKING, measured, as a fraction of the cell.

    The eyes and the nose are the dark cluster of the upper body; its centroid
    moves with the head, left as the fox turns left and up as it looks up. Each
    frame's vector is that centroid minus the first frame's, so frame 0 is
    «straight at you» and every other frame is a direction with a reach.

    This replaces marking «level left» and «straight up» by eye: the arc in the
    art never actually reaches level left — its most horizontal frame is 14
    degrees above the horizon — and calling a frame level made the creature
    look above a pointer that was level with it (owner, 2026-09-12).
    """
    out: List[List[float]] = []
    base = None
    for fr in frames:
        bb = frame_alpha_bbox(fr, width, height)
        if bb is None:
            out.append([0.0, 0.0])
            continue
        top, bot = bb[1], bb[3]
        lo = top + int((bot - top) * 0.12)     # below the ear tips
        hi = top + int((bot - top) * 0.48)     # above the bandana
        sx = sy = k = 0
        for y in range(lo, hi):
            row = y * width * 4
            for x in range(bb[0], bb[2]):
                i = row + x * 4
                if (fr[i + 3] > 200 and fr[i] < _DARK[0]
                        and fr[i + 1] < _DARK[1] and fr[i + 2] < _DARK[2]):
                    sx += x; sy += y; k += 1
        if k < 20:
            out.append(out[-1] if out else [0.0, 0.0])
            continue
        cx, cy = sx / k, sy / k
        if base is None:
            base = (cx, cy)
        out.append([round((cx - base[0]) / crop[2], 5),
                    round((cy - base[1]) / crop[3], 5)])
    return out


def clip_bbox(clip_path: str, fps: float) -> Tuple[int, int, Tuple[int, int, int, int]]:
    """(source width, source height, union alpha bbox) for one clip — the first
    pass of a --uniform run, so every clip can share one crop rect."""
    width, height, _duration = probe_video(clip_path)
    buf = extract_rgba_frames(clip_path, fps, width, height)
    frame_size = width * height * 4
    if len(buf) % frame_size != 0:
        buf = buf[: (len(buf) // frame_size) * frame_size]
    n = len(buf) // frame_size
    if n == 0:
        raise SpriteError(f"No frames extracted from {clip_path} (unexpected).")
    frames = [buf[i * frame_size:(i + 1) * frame_size] for i in range(n)]
    return width, height, union_bbox(frames, width, height)


def process_clip(clip_path: str, out_dir: str, fps: float, size: int, pad: float,
                 gain: Optional[Tuple[float, float, float]] = None,
                 want_gaze: bool = False,
                 crop_rect: Optional[Tuple[int, int, int, int]] = None) -> ClipResult:
    require_ffmpeg()
    name = os.path.splitext(os.path.basename(clip_path))[0]
    width, height, duration = probe_video(clip_path)

    buf = extract_rgba_frames(clip_path, fps, width, height)
    frame_size = width * height * 4
    if len(buf) % frame_size != 0:
        buf = buf[: (len(buf) // frame_size) * frame_size]
    n_full = len(buf) // frame_size
    if n_full == 0:
        raise SpriteError(f"No frames extracted from {clip_path} (unexpected).")

    green_fraction = 1.0 - opaque_fraction(buf, width, height, frame_size)
    if green_fraction < 0.005:
        raise SpriteError(
            f"Clip '{name}' does not appear to have a green background: "
            "the chroma key removed almost nothing. Check the footage or "
            "the colorkey color/threshold."
        )

    all_frames = [buf[i * frame_size:(i + 1) * frame_size] for i in range(n_full)]
    if crop_rect is not None:
        # --uniform: one rect for every clip of the batch, so the character is
        # the same size in every scene. Without it each clip is cropped to its
        # own motion and the creature visibly changes scale when one clip hands
        # over to the next (run_in -> idle).
        crop = crop_rect
    else:
        bbox = union_bbox(all_frames, width, height)
        crop = pad_and_square(bbox, width, height, pad)
    out_w, out_h = scale_dims(crop[2], crop[3], size)

    keep_idx = pick_subsample_indices(n_full, MAX_FRAMES)
    kept_frames = [all_frames[i] for i in keep_idx]
    n = len(kept_frames)
    out_fps = fps if n == n_full else round(fps * n / n_full, 3)

    os.makedirs(out_dir, exist_ok=True)
    png_path = os.path.join(out_dir, f"{name}.png")
    contact_path = os.path.join(out_dir, f"{name}_contact.jpg")

    cols = grid_cols(n, out_w)
    # Where the character's own centre sits inside the cell, as a fraction of
    # the cell width. Mirroring flips about the CELL centre, so without this
    # the creature jumps sideways every time it turns (owner, stand
    # 2026-09-11). Median over the frames, so one odd pose cannot move it.
    centres = []
    for fr in kept_frames:
        bb = frame_alpha_bbox(fr, width, height)
        if bb is None:
            continue
        # The BODY's centre, not the whole silhouette's: a turning head drags
        # the bbox sideways, and mirroring about that point still moves the
        # body. The bottom 45 % of the character is legs and haunches, which
        # the head never moves.
        y_lo = int(bb[3] - (bb[3] - bb[1]) * 0.45)
        lo = hi = None
        for y in range(max(0, y_lo), bb[3]):
            row = fr[(y * width) * 4:((y + 1) * width) * 4][3::4]
            marked = row.translate(_ALPHA_TABLE)
            a = marked.find(b"\x01")
            if a < 0:
                continue
            b_ = marked.rfind(b"\x01")
            lo = a if lo is None else min(lo, a)
            hi = b_ if hi is None else max(hi, b_)
        cxs = ((lo + hi) / 2.0) if lo is not None else ((bb[0] + bb[2]) / 2.0)
        centres.append((cxs - crop[0]) / float(crop[2]))
    centres.sort()
    cx = round(centres[len(centres) // 2], 4) if centres else 0.5
    # And where the character STANDS: the lowest point it reaches in the clip,
    # as a fraction of the cell. Poses differ in height — a running fox fills
    # more of the cell than a sitting one — so without this the feet sit at a
    # different place in every clip and the creature sinks through the bottom
    # of the window while it runs (owner, stand 2026-09-12: "a gap while running").
    bottoms = []
    for fr in kept_frames:
        bb = frame_alpha_bbox(fr, width, height)
        if bb is None:
            continue
        bottoms.append((bb[3] - crop[1]) / float(crop[3]))
    # The pose the clip STARTS from, not the lowest point it ever reaches: a
    # stretch legitimately leans lower than a sit, and anchoring on that made
    # the creature hop upwards the moment it was clicked (owner, stand
    # 2026-09-12: "it jumped when clicked"). `by_end` is the pose it hands
    # over IN, for clips that bridge two different shoots.
    by = round(bottoms[0], 4) if bottoms else 1.0
    by_end = round(bottoms[-1], 4) if bottoms else by
    # And the lowest the character ever goes — the FLOOR for a clip where it
    # walks or runs, a pose extreme for one where it stays put. Which of the
    # two a clip should stand on is a property of the clip, so the manifest
    # says it (`align`) and the player obeys.
    by_max = round(max(bottoms), 4) if bottoms else by
    # Where the EYES are, as a fraction of the cell: the row with the most dark
    # pixels inside the top 45 % of the body in the first frame. The gaze angle
    # is measured from this point, and a guessed fraction of the box put the
    # origin a good 20 px above the real eyes (colleague's review, 2026-09-12).
    ey = eye_row(kept_frames[0], width, height, crop) if kept_frames else 0.43
    gaze = gaze_vectors(kept_frames, width, height, crop) if want_gaze else None
    build_strip_png(kept_frames, width, height, crop, out_w, out_h, png_path,
                    cols=cols, gain=gain)
    # The contact sheet is a convenience for a human picking frame ranges, not
    # a deliverable: an ffmpeg build without drawtext (or without libfreetype
    # behind an advertised drawtext, as on this machine) must not fail a run
    # whose strip PNG is already on disk.
    try:
        build_contact_sheet(kept_frames, width, height, crop, contact_path, find_font())
    except SpriteError as exc:
        try:
            build_contact_sheet(kept_frames, width, height, crop, contact_path, None)
        except SpriteError:
            print(f"warn: {name}: contact sheet skipped ({exc})")

    png_bytes = os.path.getsize(png_path)
    return ClipResult(name=name, frames=n, fps=out_fps, w=out_w, h=out_h,
                      png_bytes=png_bytes, cols=cols, cx=cx, by=by, by_end=by_end, by_max=by_max, ey=ey, gaze=gaze)


# --------------------------------------------------------------------------
# manifest handling
# --------------------------------------------------------------------------

def load_manifest(out_dir: str) -> dict:
    path = os.path.join(out_dir, "sheets.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(out_dir: str, manifest: dict) -> None:
    path = os.path.join(out_dir, "sheets.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def result_to_dict(r: ClipResult) -> dict:
    out = {"frames": r.frames, "fps": r.fps, "w": r.w, "h": r.h,
            "cols": r.cols or r.frames, "cx": r.cx, "by": r.by, "by_end": r.by_end,
            "by_max": r.by_max, "ey": r.ey, "bytes": r.png_bytes}
    if r.gaze:
        out["gaze"] = r.gaze
    return out


# --------------------------------------------------------------------------
# CLI driving
# --------------------------------------------------------------------------

def iter_clips(in_dir: str) -> List[str]:
    if not os.path.isdir(in_dir):
        raise SpriteError(f"--in directory not found: {in_dir}")
    clips = sorted(
        os.path.join(in_dir, fn)
        for fn in os.listdir(in_dir)
        if fn.lower().endswith(".mp4")
    )
    if not clips:
        raise SpriteError(f"No .mp4 clips found in {in_dir}")
    return clips


def _cwebp() -> Optional[str]:
    return shutil.which("cwebp") or (
        "/opt/homebrew/bin/cwebp" if os.path.isfile("/opt/homebrew/bin/cwebp") else None
    )


def encode_webp(png_path: str, quality: int) -> Tuple[str, int]:
    """PNG strip -> WebP beside it, alpha preserved. Returns (path, bytes).

    Prefers the standalone `cwebp` (Homebrew's ffmpeg ships without libwebp on
    this machine), falls back to ffmpeg's own encoder where it exists."""
    webp_path = os.path.splitext(png_path)[0] + ".webp"
    cw = _cwebp()
    if cw:
        run_capture([cw, "-quiet", "-q", str(int(quality)), "-alpha_q", "100",
                     "-m", "6", "-mt", png_path, "-o", webp_path])
    else:
        run_capture([
            FFMPEG, "-v", "error", "-y", "-i", png_path,
            "-c:v", "libwebp", "-lossless", "0", "-quality", str(int(quality)),
            "-compression_level", "6", "-preset", "picture", webp_path,
        ])
    if not os.path.isfile(webp_path) or os.path.getsize(webp_path) == 0:
        raise SpriteError(f"WebP encode produced nothing for {png_path}")
    return webp_path, os.path.getsize(webp_path)


def parse_clip_fps(spec: Optional[str]) -> dict:
    """`run=20,sitdown=20` — a frame rate for named clips only.

    A gait is the one thing ten frames a second cannot carry: the step between
    frames is plainly visible while the creature runs (owner, stand
    2026-09-12). Everything else stays at the batch rate, so the sheets do not
    grow for nothing.
    """
    out: dict = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SpriteError(f"--clip-fps: expected name=fps, got '{part}'")
        name, value = part.split("=", 1)
        try:
            rate = float(value)
        except ValueError:
            raise SpriteError(f"--clip-fps: '{value}' is not a number") from None
        if not (rate > 0):
            raise SpriteError(f"--clip-fps: {name} must be positive")
        out[name.strip()] = rate
    return out


def run_batch(in_dir: str, out_dir: str, fps: float, size: int, pad: float,
              webp: bool = False, webp_quality: int = 82,
              uniform: bool = False, clip_fps: Optional[dict] = None,
              color_ref: str = "", gaze_clips: Optional[set] = None,
              color_target: str = "") -> dict:
    require_ffmpeg()
    manifest = load_manifest(out_dir)
    clips = iter_clips(in_dir)
    shared: Optional[Tuple[int, int, int, int]] = None
    if uniform:
        boxes = []
        src_w = src_h = 0
        for clip_path in clips:
            w, h, bb = clip_bbox(clip_path, fps)
            if src_w and (w, h) != (src_w, src_h):
                raise SpriteError(
                    "--uniform needs every clip at the same resolution; "
                    f"{os.path.basename(clip_path)} is {w}x{h}, expected {src_w}x{src_h}"
                )
            src_w, src_h = w, h
            boxes.append(bb)
        # union_bbox speaks CORNERS (x0, y0, x1, y1), not (x, y, w, h) —
        # pad_and_square takes the same shape.
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        shared = pad_and_square((x0, y0, x1, y1), src_w, src_h, pad)
        print(f"uniform crop: x={shared[0]} y={shared[1]} w={shared[2]} h={shared[3]} "
              f"(source {src_w}x{src_h})")
    rates = clip_fps or {}
    gaze_clips = gaze_clips or set()
    # Tone matching: every clip is a separate shoot and their exposure drifts,
    # so the creature visibly changes colour when one clip hands over to the
    # next. Measure each shoot against the reference one and correct the rest.
    gains: dict = {}
    if color_ref and shared:
        tones = {}
        for clip_path in clips:
            nm = os.path.splitext(os.path.basename(clip_path))[0]
            w, h, _fps = probe_video(clip_path)
            fr = first_frame(clip_path, w, h)
            t = frame_tone(fr, w, h, shared) if fr else None
            if t:
                tones[nm] = t
        base = tones.get(color_ref)
        if not base:
            raise SpriteError(f"--color-ref: no clip named '{color_ref}' to match against")
        for nm, t in tones.items():
            if nm == color_ref:
                continue
            iou = mask_iou(base[3], t[3])
            if iou < 0.9:
                print(f"tone: {nm} left alone (its first frame is a different pose, "
                      f"silhouettes agree {iou:.2f})")
                continue
            g = tuple(max(0.85, min(1.18, base[i] / t[i])) for i in range(3))
            if max(abs(x - 1.0) for x in g) > 0.004:
                gains[nm] = g
                print(f"tone: {nm} x{g[0]:.3f}/{g[1]:.3f}/{g[2]:.3f} "
                      f"(same pose, silhouettes agree {iou:.2f}; "
                      f"{t[0]:.0f},{t[1]:.0f},{t[2]:.0f} -> {base[0]:.0f},{base[1]:.0f},{base[2]:.0f})")
    # And one more gain on top of all of them: land the whole set on the tone
    # of the art the character was drawn from, which is what it is seen next to.
    if color_target and color_ref and shared:
        want = fur_mean(color_target)
        ref_clip = next((c for c in clips
                         if os.path.splitext(os.path.basename(c))[0] == color_ref), None)
        if want and ref_clip:
            w, h, _f = probe_video(ref_clip)
            fr = first_frame(ref_clip, w, h)
            have = frame_fur_mean(fr, w, h, shared) if fr else None
            if have:
                lift = tuple(max(0.85, min(1.18, want[i] / have[i])) for i in range(3))
                print(f"tone: whole set x{lift[0]:.3f}/{lift[1]:.3f}/{lift[2]:.3f} "
                      f"(fur {have[0]:.0f},{have[1]:.0f},{have[2]:.0f} -> "
                      f"{want[0]:.0f},{want[1]:.0f},{want[2]:.0f}, from "
                      f"{os.path.basename(color_target)})")
                for nm in [os.path.splitext(os.path.basename(c))[0] for c in clips]:
                    g = gains.get(nm, (1.0, 1.0, 1.0))
                    gains[nm] = tuple(g[i] * lift[i] for i in range(3))
    for clip_path in clips:
        name = os.path.splitext(os.path.basename(clip_path))[0]
        result = process_clip(clip_path, out_dir, rates.get(name, fps), size, pad,
                              gain=gains.get(name), want_gaze=(name in gaze_clips),
                              crop_rect=shared)
        entry = result_to_dict(result)
        png_path = os.path.join(out_dir, result.name + ".png")
        out_path = png_path
        if webp:
            try:
                out_path, wbytes = encode_webp(png_path, webp_quality)
                # The manifest names the file the browser loads; the PNG stays
                # on disk as the lossless master to re-encode from.
                entry["file"] = os.path.basename(out_path)
                entry["bytes"] = wbytes
                entry["png_bytes"] = result.png_bytes
            except SpriteError as exc:
                print(f"warn: {result.name}: WebP skipped ({exc})")
        manifest[result.name] = entry
        print(
            f"{result.name}: {result.frames} frames @ {result.fps}fps, "
            f"{result.w}x{result.h} per frame, {entry['bytes']} bytes -> {out_path}"
        )
    save_manifest(out_dir, manifest)
    return manifest


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------

def make_synthetic_clip(path: str, width: int = 160, height: int = 90,
                         duration: float = 2.0, fps: float = 12.0) -> None:
    """Green background with a moving opaque orange square marker.

    Note: drawbox's x/y expressions do not expose a time variable (its "t"
    option means thickness, not time), so the motion is done with `overlay`
    instead, which does expose "t" as the current timestamp.
    """
    require_ffmpeg()
    marker = 40
    bg = f"color=c=0x00FF00:s={width}x{height}:d={duration}:r={fps}"
    fg = f"color=c=0xFF8800:s={marker}x{marker}:d={duration}:r={fps}"
    filter_complex = (
        f"[0][1]overlay=x='(main_w-{marker})*t/{duration}':"
        f"y='(main_h-{marker})/2':shortest=1,format=yuv420p"
    )
    run_capture([
        FFMPEG, "-v", "error", "-y",
        "-f", "lavfi", "-i", bg,
        "-f", "lavfi", "-i", fg,
        "-filter_complex", filter_complex, path,
    ])


def read_png_rgba(path: str, width: int, height: int) -> bytes:
    return run_capture([
        FFMPEG, "-v", "error", "-i", path,
        "-f", "rawvideo", "-pix_fmt", "rgba", "-",
    ])


def selftest() -> None:
    require_ffmpeg()
    tmp = tempfile.mkdtemp(prefix="companion_sprites_selftest_")
    try:
        in_dir = os.path.join(tmp, "in")
        out_dir = os.path.join(tmp, "out")
        os.makedirs(in_dir)
        clip_path = os.path.join(in_dir, "marker_move.mp4")
        make_synthetic_clip(clip_path)

        manifest = run_batch(in_dir, out_dir, fps=12, size=128, pad=0.06)

        entry = manifest.get("marker_move")
        assert entry is not None, "sheets.json missing the synthetic clip entry"
        assert entry["frames"] == 24, f"expected 24 frames, got {entry['frames']}"
        assert entry["frames"] <= MAX_FRAMES

        png_path = os.path.join(out_dir, "marker_move.png")
        assert os.path.isfile(png_path), "strip PNG was not written"

        strip_w = entry["w"] * entry["frames"]
        strip_h = entry["h"]
        raw = read_png_rgba(png_path, strip_w, strip_h)
        expected_len = strip_w * strip_h * 4
        assert len(raw) == expected_len, (
            f"decoded strip size mismatch: got {len(raw)}, expected {expected_len}"
        )

        def alpha_at(x: int, y: int) -> int:
            idx = (y * strip_w + x) * 4 + 3
            return raw[idx]

        corners = [
            (0, 0), (strip_w - 1, 0), (0, strip_h - 1), (strip_w - 1, strip_h - 1),
        ]
        for x, y in corners:
            a = alpha_at(x, y)
            assert a == 0, f"expected transparent corner at ({x},{y}), alpha={a}"

        alpha_plane = raw[3::4]
        opaque_count = alpha_plane.translate(_ALPHA_TABLE).count(1)
        assert opaque_count > 0, "no opaque (subject) pixels found anywhere in the strip"

        contact_path = os.path.join(out_dir, "marker_move_contact.jpg")
        assert os.path.isfile(contact_path), "contact sheet was not written"
        assert os.path.getsize(contact_path) > 0

        print("SELFTEST OK: 24 frames, corners transparent, "
              f"{opaque_count} opaque pixels, strip {strip_w}x{strip_h}, "
              f"contact sheet {os.path.getsize(contact_path)} bytes")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Turn green-screen MP4 clips into sprite-strip PNGs with alpha."
    )
    p.add_argument("--in", dest="in_dir", help="Directory with input .mp4 clips")
    p.add_argument("--out", dest="out_dir", help="Output directory for PNGs + sheets.json")
    p.add_argument("--fps", type=float, default=12.0, help="Sample rate (default 12)")
    p.add_argument("--size", type=int, default=256, help="Taller side in px (default 256)")
    p.add_argument("--pad", type=float, default=0.06, help="Bbox padding fraction (default 0.06)")
    p.add_argument("--webp", action="store_true",
                   help="Also write <clip>.webp beside the PNG and point sheets.json at it "
                        "(a 54-frame 256px strip is ~3 MB as PNG and ~10x smaller as WebP; "
                        "the browser player only reads the manifest, so the format is ours "
                        "to choose)")
    p.add_argument("--webp-quality", type=int, default=82, help="WebP quality (default 82)")
    p.add_argument("--gaze", default="",
                   help="measure a per-frame gaze direction for these clips, "
                        "e.g. --gaze head (the player picks the frame that looks "
                        "nearest the pointer)")
    p.add_argument("--color-target", default="",
                   help="a still of the character to land the whole set's tone on, "
                        "e.g. --color-target static/img/quests/companion/sit.png")
    p.add_argument("--color-ref", default="",
                   help="match every clip's tone to this one, e.g. --color-ref idle "
                        "(separate shoots drift in exposure)")
    p.add_argument("--clip-fps", default="",
                   help="per-clip frame rate, e.g. run=20,sitdown=20 "
                        "(a gait needs more frames than a sitting pose)")
    p.add_argument("--uniform", action="store_true",
                   help="One crop rect (union over all clips) so the character keeps the same size across scenes")
    p.add_argument("--selftest", action="store_true", help="Run the built-in self-test and exit")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.selftest:
            selftest()
            return 0
        if not args.in_dir or not args.out_dir:
            raise SpriteError("--in and --out are required (unless --selftest)")
        run_batch(args.in_dir, args.out_dir, args.fps, args.size, args.pad,
                  webp=args.webp, webp_quality=args.webp_quality,
                  uniform=args.uniform, clip_fps=parse_clip_fps(args.clip_fps),
                  color_ref=args.color_ref, color_target=args.color_target,
                  gaze_clips=set(x.strip() for x in args.gaze.split(",") if x.strip()))
        return 0
    except SpriteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
