#!/usr/bin/env python3
"""Layer check and a close look at components — what you cannot see in a
screenshot of the whole page.

The bug this was written for: the path line in "Practicum" was drawn ON TOP of
the lesson circles. In a page screenshot at 1× it looked like a thin
dash, at 2× like a crossed-out tick. The design skill does not catch this:
it is about direction, not defects. One mechanical rule catches it:

    the centre of an element must belong to the element itself

If `document.elementFromPoint` at the centre of a button returns a foreign node,
something is lying on top. A pseudo-element (::before/::after) returns its
owner, so exactly this case is visible. Two legitimate cases are excluded:
an element with `pointer-events: none` (a decorative caption over a graphic) and
a `disabled` control (Chrome does not deliver events to it at all).

    tools/ui_check.py                      # default pages
    tools/ui_check.py /learn /docs         # your own
    OP_STAND=http://127.0.0.1:8766 tools/ui_check.py

Writes 2× crops of components to a scratch folder and prints the list of overlaps.
Needs a running stand with `OP_DEV_BYPASS_AUTH=true`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "tests", "chrome"))

from cdp import ChromeSession  # noqa: E402

STAND = os.environ.get("OP_STAND", "http://127.0.0.1:8766")
USER = os.environ.get("OP_STAND_USER", "admin1")
OUT = os.environ.get("OP_UI_OUT", os.path.join(REPO, "scratch-ui"))

# Page -> (selectors checked for overlap; components to crop)
PAGES = {
    "/learn": (".lr-dot, .lr-unit-art, .lr-opt, .lr-opt-key, .btn, .lr-node-label",
               [".lr-hero", ".lr-unit", ".lr-lesson"]),
    "/docs": (".dg-node, .dg-track, .dg-copy, .dg-art figure, .btn",
              [".dg-hero", ".dg-step", ".dg-ref details"]),
    "/": (".dash-table, .btn, .chip", [".content"]),
}

PROBE = """
(function (sel) {
  var bad = [];
  document.querySelectorAll(sel).forEach(function (e) {
    if (e.disabled) return;                       // Chrome sends them no events
    var cs = getComputedStyle(e);
    if (cs.pointerEvents === 'none' || cs.visibility === 'hidden') return;
    var r = e.getBoundingClientRect();
    if (!r.width || !r.height) return;
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cy < 0 || cy > innerHeight || cx < 0 || cx > innerWidth) return;
    var hit = document.elementFromPoint(cx, cy);
    if (hit && (e === hit || e.contains(hit) || hit.contains(e))) return;
    bad.push({ el: (e.className || e.tagName).toString().slice(0, 60),
               over: hit ? (hit.className || hit.tagName).toString().slice(0, 60) : 'null' });
  });
  return JSON.stringify(bad);
})(%s)
"""


async def crop(s: ChromeSession, sel: str, name: str, pad: int = 10) -> None:
    box = await s.eval(
        "(function(){var e=document.querySelector(%s);if(!e)return '';"
        "var r=e.getBoundingClientRect();"
        "return JSON.stringify({x:r.left,y:r.top,w:r.width,h:r.height});})()" % json.dumps(sel))
    if not box:
        return
    b = json.loads(box)
    if b["h"] < 8 or b["w"] < 8:
        return
    r = await s.send("Page.captureScreenshot", {"format": "png", "clip": {
        "x": max(0, b["x"] - pad), "y": max(0, b["y"] - pad),
        "width": b["w"] + 2 * pad, "height": min(b["h"] + 2 * pad, 1400), "scale": 2}})
    path = os.path.join(OUT, name + ".png")
    with open(path, "wb") as f:
        f.write(base64.b64decode((r.get("result") or r)["data"]))
    print("   crop %s" % os.path.relpath(path, REPO))


async def main(paths: list[str]) -> int:
    os.makedirs(OUT, exist_ok=True)
    problems = 0
    s = ChromeSession()
    s.start()
    await s.connect()
    try:
        await s.navigate(STAND + "/login")
        await s.eval(
            "fetch('/dev-login',{method:'POST',headers:"
            "{'Content-Type':'application/x-www-form-urlencoded'},"
            "body:'person_id=%s'}).then(function(r){return r.status})" % USER,
            await_promise=True)
        await s.send("Emulation.setDeviceMetricsOverride", {
            "width": 1280, "height": 950, "deviceScaleFactor": 2, "mobile": False})
        for path in paths:
            sel, crops = PAGES.get(path, (".btn", [".content"]))
            await s.navigate(STAND + path)
            await asyncio.sleep(1.8)
            bad = json.loads(await s.eval(PROBE % json.dumps(sel)) or "[]")
            print("%-10s overlapped: %d" % (path, len(bad)))
            for b in bad:
                print("   %s  <-  %s" % (b["el"], b["over"]))
            problems += len(bad)
            for i, c in enumerate(crops):
                base = path.strip("/").replace("/", "_") or "index"
                await crop(s, c, "%s_%d" % (base, i))
    finally:
        await s.close()
        s.stop()
    return 1 if problems else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a.startswith("/")] or list(PAGES)
    raise SystemExit(asyncio.run(main(args)))
