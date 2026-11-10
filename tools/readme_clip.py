#!/usr/bin/env python3
"""Record the README clip: the loop a report goes through, as a GIF.

    OP_STAND=http://127.0.0.1:8810 tools/readme_clip.py
    tools/readme_clip.py --out static/img/docs/loop.gif --fps 12

WHY A CLIP AND WHY THIS ONE. A still screenshot can show that the platform
exists; it cannot show the one claim the project actually makes — that a
report is a single HTML file that keeps working when the platform is not
there. That claim is only believable in motion: the same report, first served
by OnePage, then opened from the disk with no server running at all.

So the clip is three scenes and no more:
  1. the list a person lands on, and a report opened from it,
  2. the report itself, scrolling, with its real data,
  3. THE SAME report downloaded as one file and opened over file:// —
     no platform, no network, same numbers.

Nothing is staged: every frame is a real page driven over CDP against a real
stand, the file in scene 3 is what `GET …/standalone` actually returns, and
the captions name what is on screen rather than selling it.

GIF, not mp4: GitHub autoplays a GIF inside a README and renders an mp4 as a
link, and a link is not a demonstration. The palette is built from the clip's
own frames (ffmpeg palettegen) — the default 256-colour web palette turns the
product's flat greys into bands.

Needs ffmpeg on PATH and a stand with demo data (`tools/seed_demo.py`).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "chrome"))

REPO = Path(__file__).resolve().parent.parent
STAND = os.environ.get("OP_STAND", "http://127.0.0.1:8810")
USER = os.environ.get("OP_STAND_USER", "admin")
OUT = REPO / "static" / "img" / "docs" / "loop.gif"
# 2:1 keeps the clip short on a README page; captured at 2x and written at
# OUT_W so text stays sharp on a high-density screen.
W, H = 1280, 640
OUT_W = 1280

# The caption is part of the frame, not the edit: README GIFs are watched without sound and
# without explanation, and the scene must explain itself. A pill at the bottom left, in the same
# typography as the product, so it does not look like a sticker on top of someone else's
# screenshot.
CAPTION = """
(function(text){
  var id='op-clip-cap', n=document.getElementById(id);
  if(!n){ n=document.createElement('div'); n.id=id; document.body.appendChild(n);
    n.style.cssText='position:fixed;left:24px;bottom:24px;z-index:2147483647;'+
      'padding:10px 16px;border-radius:999px;font:500 15px/1.2 "Inter Tight",'+
      'system-ui,sans-serif;color:#fff;background:rgba(14,17,22,.92);'+
      'box-shadow:0 8px 24px -8px rgba(0,0,0,.5);letter-spacing:-.01em;'+
      'pointer-events:none;opacity:0;transition:opacity .25s ease';
  }
  if(text){ n.textContent=text; n.style.opacity='1'; } else { n.style.opacity='0'; }
})(%s);
"""


def fetch_standalone(dash_id: str) -> bytes:
    """Export the report as a single file under our own session."""
    jar = urllib.request.HTTPCookieProcessor()
    opener = urllib.request.build_opener(jar)
    opener.open(urllib.request.Request(
        STAND + "/dev-login",
        data=urllib.parse.urlencode({"person_id": USER}).encode(),
        headers={"Origin": STAND,
                 "Content-Type": "application/x-www-form-urlencoded"}), timeout=30)
    return opener.open(
        STAND + f"/api/dashboards/{dash_id}/standalone", timeout=120).read()


async def hold(s, frames: list, tmp: Path, seconds: float, fps: int):
    """Capture N frames in a row. We hold by frame count, not by sleep: otherwise the
    scene length depends on how fast the machine takes a screenshot."""
    for _ in range(max(1, int(seconds * fps))):
        p = tmp / ("f%05d.png" % len(frames))
        await s.screenshot(str(p))
        frames.append(p)


async def record(fps: int, out: Path) -> int:
    from cdp import ChromeSession
    import json

    tmp = Path(tempfile.mkdtemp(prefix="op-clip-"))
    frames: list[Path] = []
    s = ChromeSession()
    s.start()
    await s.connect()
    try:
        await s.send("Emulation.setDeviceMetricsOverride",
                     {"width": W, "height": H, "deviceScaleFactor": 2, "mobile": False})
        await s.navigate(STAND + "/login", wait_ms=700)
        await s.eval(
            "fetch('/dev-login',{method:'POST',headers:"
            "{'Content-Type':'application/x-www-form-urlencoded'},"
            "body:'person_id=%s'}).then(function(r){return r.status})" % USER,
            await_promise=True)
        await s.eval("document.cookie='op_lang=en;path=/;max-age=86400'")

        # ── scene 1: the list and opening a report ──────────────────────
        await s.navigate(STAND + "/", wait_ms=1400)
        await s.eval(CAPTION % json.dumps("Every report is one HTML file"))
        await hold(s, frames, tmp, 2.2, fps)

        rid = await s.eval(
            "(function(){var a=document.querySelector('a[href^=\"/d/\"]');"
            "return a?a.getAttribute('href'):null})()")
        if not rid:
            print("the stand has no reports — seed it with tools/seed_demo.py")
            return 1

        # ── scene 2: the report live ────────────────────────────────────
        await s.navigate(STAND + rid, wait_ms=2200)
        # The owner's dock is open by default and eats a third of the frame. In this
        # scene the report itself is the star, so the panel is closed; the caption
        # tells that it exists, not a cluttered frame.
        await s.eval("(function(){var b=document.getElementById('dock-toggle');"
                     "var v=document.getElementById('viewer-root');"
                     "if(v&&v.getAttribute('data-dock')==='open'&&b)b.click()})()")
        await s.eval(CAPTION % json.dumps(
            "Served by the platform: access control, versions, audit"))
        await hold(s, frames, tmp, 2.2, fps)
        for _ in range(34):   # slow scroll inside the report frame
            await s.eval(
                "(function(){var f=document.getElementById('dash-frame');"
                "if(f&&f.contentWindow){try{f.contentWindow.scrollBy(0,26)}catch(e){}}"
                "else window.scrollBy(0,26)})()")
            await hold(s, frames, tmp, 1 / fps, fps)
        await hold(s, frames, tmp, 0.8, fps)

        # ── scene 3: the same report as a file on disk ──────────────────
        dash_id = rid.rsplit("/", 1)[-1]
        local = tmp / "report-standalone.html"
        try:
            # Our own session, not the browser's: the `op_session` cookie is
            # HttpOnly, and document.cookie does not expose it — not a browser
            # quirk, but exactly the protection the flag is there for.
            local.write_bytes(fetch_standalone(dash_id))
        except Exception as exc:                       # noqa: BLE001
            print(f"could not export the report as a single file: {exc}")
            return 1

        await s.navigate("file://" + str(local), wait_ms=2400)
        await s.eval(CAPTION % json.dumps(
            "The same file on disk — no server, no network, same numbers"))
        await hold(s, frames, tmp, 3.4, fps)

        print("frames captured: %d (%.1f s at %d fps)" % (len(frames), len(frames) / fps, fps))
    finally:
        await s.close()

    # ── assembly ────────────────────────────────────────────────────────
    ff = shutil.which("ffmpeg")
    if not ff:
        print("ffmpeg not found — frames left in " + str(tmp))
        return 1
    pal = tmp / "palette.png"
    common = ["-framerate", str(fps), "-i", str(tmp / "f%05d.png")]
    # The palette is built from the clip itself: the standard web palette bands
    # the product's flat greys.
    subprocess.run([ff, "-y", "-loglevel", "error", *common,
                    "-vf", "scale=%d:-1:flags=lanczos,palettegen=max_colors=256:stats_mode=full" % OUT_W,
                    str(pal)], check=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ff, "-y", "-loglevel", "error", *common, "-i", str(pal),
                    # No dithering: the interface is flat colour, and a dither pattern over
                    # it reads as noise.
                    "-lavfi", ("scale=%d:-1:flags=lanczos[x];[x][1:v]" % OUT_W)
                              + "paletteuse=dither=none:diff_mode=rectangle",
                    "-loop", "0", str(out)], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    print("built %s (%.1f MB)" % (out, out.stat().st_size / 1e6))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--fps", type=int, default=12)
    a = ap.parse_args(argv)
    return asyncio.run(record(a.fps, Path(a.out)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
