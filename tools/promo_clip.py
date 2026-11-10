#!/usr/bin/env python3
"""Render the OnePage promo film: 16:9, 1920x1080, H.264, ~58 s, no voice.

    OP_STAND=http://127.0.0.1:8810 tools/promo_clip.py --work /tmp/op-promo
    tools/promo_clip.py --work … --only render --scene disk   # one scene again
    tools/promo_clip.py --work … --only check                 # OCR + ffprobe

WHAT THE FILM IS FOR. One measurable job: whoever watches it should want to
clone the repository and run it. So it proves the README's first sentence —
a report is one HTML file, and the same file keeps working opened from disk,
with no server and no network — and then gets out of the way with the two
commands that start a stand. Everything else (access list, versions, share
links, presentations) is shown only as far as it reads in three seconds.

HOW IT IS MADE. Not a screen recording. Four stages, each re-runnable:

  prepare  puts the demo stand into the state the film shows: version 3 of
           the synthetic report, two people on its access list, a share link,
           a presentation. Idempotent; needs `tools/seed_demo.py` first.
  capture  drives headless Chrome over the real stand and saves STILLS of real
           screens (panels/*.png, DPR 2) plus the rectangles of the elements
           the camera and the cursor aim at (panels/meta.json). A still is
           taken only once two screenshots in a row are identical. The report
           is also downloaded as one file (`GET …/standalone`) and opened over
           file:// with the network switched off in DevTools — the «offline»
           in the film is a fact of the capture, not a label.
  render   builds one composition page (scene.html: the stills, the address
           strip, captions, a drawn cursor, typography) and renders every frame
           as a pure function of time: frame k of a scene is seek(t = k/fps).
           The camera is the `clip` of Page.captureScreenshot, so a zoom is
           re-rasterised by Chrome at the zoomed scale instead of stretching
           pixels. A frame whose state did not change is copied, not captured.
  build    ffmpeg: scenes joined by short crossfades, yuv420p (Safari and the
           social players show black on anything else), +faststart, 30 fps,
           and a silent AAC track (some uploaders break the preview without
           audio).

The timeline (SCENES below) is the only place the film is described: a scene
is a duration plus actions bound to seconds. Change a caption or a duration and
re-run `--only render,build`; change the interface and re-run everything.

BOUNDARIES. Only synthetic content reaches the frame: the report is
tools/docs_fixtures/demo_report.html (invented numbers), the people are the
demo accounts. The Practicum report the seed also creates is Russian and is
never opened, and the report list is not in the film at all. `check` OCRs every distinct frame with
the system's Vision framework (it reads Cyrillic; the installed tesseract has
English only) and fails on a single Cyrillic letter. No generated art: only
real screens and the product's own fonts.

Needs: a demo stand (OP_DEV_BYPASS_AUTH=true, tools/seed_demo.py), Chrome,
ffmpeg; `check` needs macOS (swift + Vision).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import hashlib
import http.cookiejar
import http.server
import json
import math
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "chrome"))

STAND = os.environ.get("OP_STAND", "http://127.0.0.1:8810").rstrip("/")
USER = os.environ.get("OP_STAND_USER", "admin")
REPO_URL = "github.com/onepage-bi/onepage"

W, H, FPS = 1920, 1080, 30
XFADE = 0.4                      # crossfade duration between scenes, s
BAR = 56                         # height of the address strip in the frame, px
CSS_W = 1440                     # width of the window the screens are captured in
S = W / CSS_W                    # screen css pixel -> frame pixel (4/3)
CSS_H = round((H - BAR) / S)     # 768: the screen fills the frame below the strip
DPR = 2                          # stills at double density: a zoom up to 1.5x stays sharp

REPORT_TITLE = "Demo: shelf availability"     # created by tools/seed_demo.py
NAV_TITLE = "Demo: navigator"
PRES_TITLE = "Demo: weekly review"
SHARE_LABEL = "Partner review"
FIXTURE = REPO / "tools" / "docs_fixtures" / "demo_report.html"

FONTS = ["inter-tight-400-latin.woff2", "inter-tight-500-latin.woff2",
         "inter-tight-600-latin.woff2", "jetbrains-mono-400-latin.woff2",
         "jetbrains-mono-500-latin.woff2"]

CYR = re.compile(r"[\u0400-\u04FF]")


# ════════════════════════════════════════════════════════════════════════
# Stand: our own session over urllib
# ════════════════════════════════════════════════════════════════════════

class Stand:
    """HTTP to the stand under our own session.

    The `op_session` cookie is HttpOnly — the browser cannot read it, and that is
    right; so everything done outside the page (data preparation, downloading the
    report as one file) goes through our own session, as in readme_clip.py.
    """

    def __init__(self, base: str, user: str):
        self.base = base
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.req("POST", "/dev-login", form={"person_id": user})

    def req(self, method: str, path: str, *, form=None, js=None,
            files=None, ok=(200, 201, 302, 303)) -> bytes:
        headers = {"Origin": self.base}   # OriginGuard allows a missing Origin, but be explicit
        data = None
        if js is not None:
            data = json.dumps(js).encode()
            headers["Content-Type"] = "application/json"
        elif files is not None:
            bnd = uuid.uuid4().hex
            parts = []
            for k, v in (form or {}).items():
                parts.append(f'--{bnd}\r\nContent-Disposition: form-data; '
                             f'name="{k}"\r\n\r\n{v}\r\n'.encode())
            for k, (name, blob) in files.items():
                parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"; '
                             f'filename="{name}"\r\nContent-Type: text/html\r\n\r\n'
                             .encode() + blob + b"\r\n")
            parts.append(f"--{bnd}--\r\n".encode())
            data = b"".join(parts)
            headers["Content-Type"] = f"multipart/form-data; boundary={bnd}"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers=headers)
        try:
            with self.op.open(r, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in ok:
                return e.read()
            raise SystemExit(f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}")

    def home(self) -> dict:
        t = self.req("GET", "/").decode()
        m = re.search(r'<script id="dash-data" type="application/json">(.*?)</script>',
                      t, re.S)
        if not m:
            raise SystemExit("no #dash-data on / — is this really OnePage?")
        return json.loads(m.group(1))

    def find(self, title: str) -> dict | None:
        return next((d for d in self.home()["items"] if d["title"] == title), None)


def prepare(st: Stand) -> dict:
    """Bring the stand to the state the film shows. Idempotent."""
    rep = st.find(REPORT_TITLE)
    if not rep:
        raise SystemExit(f"the stand has no \"{REPORT_TITLE}\" — run "
                         f"tools/seed_demo.py --url {STAND}")
    rid = rep["id"]
    # Three versions: the "Versions" panel must show a history, not a single row.
    # The same synthetic file is uploaded — the numbers in the frame do not change.
    for _ in range(max(0, 3 - int(rep.get("ver") or 1))):
        st.req("POST", f"/api/dashboards/{rid}/version",
               files={"html_file": ("shelf-availability.html", FIXTURE.read_bytes())})
    for pid, upd in (("analyst", "true"), ("viewer", "false")):
        st.req("POST", f"/api/dashboards/{rid}/permissions",
               form={"person_id": pid, "can_update_data": upd})
    links = json.loads(st.req("GET", f"/api/dashboards/{rid}/share-links") or b"[]")
    if isinstance(links, dict):
        links = links.get("items") or links.get("links") or []
    if not any((l.get("label") or "") == SHARE_LABEL for l in links):
        st.req("POST", f"/api/dashboards/{rid}/share-links",
               js={"label": SHARE_LABEL, "expires_in_days": 30})
    pres = json.loads(st.req("GET", "/api/presentations"))
    if isinstance(pres, dict):
        pres = pres.get("items") or pres.get("presentations") or []
    p = next((x for x in pres if x.get("title") == PRES_TITLE), None)
    pid = p["id"] if p else json.loads(
        st.req("POST", "/api/presentations", js={"title": PRES_TITLE}))["id"]
    have = {i.get("dashboard_id") for i in
            json.loads(st.req("GET", f"/api/presentations/{pid}")).get("items", [])}
    # Two slides: the report and a navigator (also from seed_demo). Navigators do
    # not appear on the home page; their id is taken from the /navigators page.
    nav = re.search(r'<a class="nav-card" href="/d/([0-9a-f-]{36})">\s*'
                    r'<div class="nav-card-name">' + re.escape(NAV_TITLE),
                    st.req("GET", "/navigators", ok=(200, 404)).decode())
    for did in [rid] + ([nav.group(1)] if nav else []):
        if did not in have:
            st.req("POST", f"/api/presentations/{pid}/items",
                   js={"kind": "dashboard", "dashboard_id": did})
    print(f"  stand ready: report {rid}, presentation {pid}")
    return {"report": rid, "presentation": pid}


# ════════════════════════════════════════════════════════════════════════
# Capturing real screens
# ════════════════════════════════════════════════════════════════════════

# The page cannot look inside the report's sandboxed iframe (no
# allow-same-origin), but DevTools can: Chrome keeps such an iframe as a separate
# target (OOPIF) with its own debugging socket. Rectangles inside the report and
# its text for the Cyrillic check are taken through it.
RECT_JS = """(function(sel){var e=document.querySelector(sel);if(!e)return null;
var r=e.getBoundingClientRect();return [r.x,r.y,r.width,r.height]})(%s)"""


async def iframe_eval(s, url_part: str, exprs: list[str]) -> list | None:
    """Evaluate expressions in the iframe target whose URL contains url_part."""
    import websockets
    with urllib.request.urlopen(f"http://127.0.0.1:{s.port}/json", timeout=5) as r:
        targets = json.loads(r.read())
    t = next((x for x in targets if x.get("type") == "iframe"
              and url_part in x.get("url", "") and x.get("webSocketDebuggerUrl")), None)
    if not t:
        return None
    out = []
    async with websockets.connect(t["webSocketDebuggerUrl"], max_size=None) as ws:
        for i, e in enumerate(exprs, start=1):
            await ws.send(json.dumps({"id": i, "method": "Runtime.evaluate",
                                      "params": {"expression": e, "returnByValue": True}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == i:
                    out.append(m.get("result", {}).get("result", {}).get("value"))
                    break
    return out


CLOSE_DOCK = ("(function(){var v=document.getElementById('viewer-root');"
              "if(v&&v.getAttribute('data-dock')!=='closed'){"
              "var b=document.getElementById('dock-toggle');if(b)b.click();}})()")
OPEN_DOCK = ("(function(){var v=document.getElementById('viewer-root');"
             "if(v&&v.getAttribute('data-dock')==='closed'){"
             "var b=document.getElementById('dock-toggle');if(b)b.click();}})()")


def tab(name: str) -> str:
    return (OPEN_DOCK + ";(function(){var b=document.querySelector("
            "'.dock-tab[data-tab=\"%s\"]');if(b)b.click()})()" % name)


async def stable_shot(s, path: Path, tries: int = 14) -> None:
    """Take a still only once two in a row match byte for byte: otherwise a
    half-drawn chart or a semi-transparent panel animation ends up in the frame."""
    prev = None
    for _ in range(tries):
        r = await s.send("Page.captureScreenshot", {"format": "png"})
        if r["data"] == prev:
            break
        prev = r["data"]
        await asyncio.sleep(0.35)
    else:
        print(f"  WARNING {path.name}: the screen never settled, taking the last one")
    path.write_bytes(base64.b64decode(prev))


async def cyr_on_page(s, frame_part: str | None = None) -> list[str]:
    """Cyrillic in the page's visible text (and the report inside it)."""
    txt = await s.eval("document.body ? document.body.innerText : ''") or ""
    if frame_part:
        got = await iframe_eval(s, frame_part, ["document.body?document.body.innerText:''"])
        if got is None:
            print(f"  WARNING: iframe {frame_part} not found — report text not checked")
        else:
            txt += got[0] or ""
    return sorted(set(m.group(0) for m in re.finditer(r"\S*[\u0400-\u04FF]+\S*", txt)))[:12]


def fetch_standalone(rid: str) -> bytes:
    return Stand(STAND, USER).req("GET", f"/api/dashboards/{rid}/standalone")


async def capture(ids: dict, work: Path) -> dict:
    from cdp import ChromeSession

    panels = work / "panels"
    panels.mkdir(parents=True, exist_ok=True)
    rid, pid = ids["report"], ids["presentation"]
    meta: dict = {"ids": ids, "stand": STAND}
    s = ChromeSession(port=9341)
    s.start()
    await s.connect()
    try:
        await s.send("Emulation.setDeviceMetricsOverride", {
            "width": CSS_W, "height": CSS_H, "deviceScaleFactor": DPR, "mobile": False})
        await s.navigate(STAND + "/login", wait_ms=600)
        await s.eval("fetch('/dev-login',{method:'POST',headers:{'Content-Type':"
                     "'application/x-www-form-urlencoded'},body:'person_id=%s'})"
                     ".then(function(r){return r.status})" % USER, await_promise=True)
        # The language is held by the op_lang cookie, not the browser flag (cdp.py sets ru).
        await s.eval("document.cookie='op_lang=en;path=/;max-age=86400'")
        # The quests companion is not wanted in a promo frame (quests are off on the stand anyway).
        hide_css = "#op-companion{display:none!important}"

        async def page(name: str, url: str, setup: str = "", wait: float = 2.0,
                       rects: dict | None = None, inner: dict | None = None,
                       frame_part: str | None = None):
            await s.navigate(url, wait_ms=int(wait * 1000))
            await s.eval("(function(){var st=document.createElement('style');"
                         "st.textContent=%s;document.head.appendChild(st)})()"
                         % json.dumps(hide_css))
            if setup:
                await s.eval(setup)
                await asyncio.sleep(1.2)
            await stable_shot(s, panels / f"{name}.png")
            m = {"url": url, "rects": {}}
            for k, sel in (rects or {}).items():
                m["rects"][k] = await s.eval(RECT_JS % json.dumps(sel))
            if inner:
                fr = m["rects"].get("frame")
                got = await iframe_eval(s, frame_part or "/render/",
                                        [RECT_JS % json.dumps(sel) for sel in inner.values()])
                for k, r in zip(inner, got or [None] * len(inner)):
                    m["rects"][k] = ([fr[0] + r[0], fr[1] + r[1], r[2], r[3]]
                                     if (r and fr) else None)
            m["cyrillic"] = await cyr_on_page(s, frame_part)
            if m["cyrillic"]:
                print(f"  WARNING {name}: Cyrillic on screen {m['cyrillic']}")
            meta[name] = m
            print(f"  captured {name}")

        d = STAND + f"/d/{rid}"
        await page("report", d, CLOSE_DOCK, wait=2.6,
                   rects={"frame": "#dash-frame"},
                   inner={"kpis": ".kpis", "h1": "h1"}, frame_part="/render/")
        dock_rects = {"dock": "#viewer-root .dock, #viewer-root aside",
                      "tab_access": '.dock-tab[data-tab="access"]',
                      "tab_versions": '.dock-tab[data-tab="versions"]',
                      "tab_share": '.dock-tab[data-tab="share"]',
                      "panel": ".dock-panel.active",
                      "standalone": "#btn-download-standalone",
                      "frame": "#dash-frame"}
        for t in ("settings", "access", "versions", "share"):
            await page(t, d, tab(t), wait=2.6, rects=dock_rects)
        meta["standalone_name"] = await s.eval(
            "(function(){var b=document.getElementById('btn-download-standalone');"
            "return b?b.getAttribute('data-filename'):null})()")
        await page("pres", STAND + f"/presentations/{pid}", wait=2.6,
                   rects={"main": ".content, main"})

        # ── the same report as a file on disk, no network ─────────────────────
        disk = work / "disk"
        disk.mkdir(exist_ok=True)
        fname = meta["standalone_name"] or "shelf-availability.html"
        local = disk / fname
        local.write_bytes(fetch_standalone(rid))
        meta["standalone_bytes"] = local.stat().st_size
        # The network is switched off in the browser itself BEFORE the file opens: the
        # "offline" in the frame is a state of the capture, not a label.
        await s.send("Network.emulateNetworkConditions", {
            "offline": True, "latency": 0, "downloadThroughput": -1,
            "uploadThroughput": -1})
        await page("disk", "file://" + str(local), wait=2.4,
                   rects={"kpis": ".kpis", "h1": "h1"})
        meta["disk_requests_blocked"] = True
        await s.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 0, "downloadThroughput": -1,
            "uploadThroughput": -1})
    finally:
        await s.close()
        s.stop()

    (panels / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    return meta


# ════════════════════════════════════════════════════════════════════════
# Timeline
# ════════════════════════════════════════════════════════════════════════

def fr(rect) -> list[float]:
    """A rectangle in still css pixels -> frame pixels."""
    x, y, w, h = rect
    return [x * S, BAR + y * S, w * S, h * S]


def around(rect, pad: float = 1.25, minw: float = 900) -> list[float]:
    """A 16:9 camera around a frame rectangle, staying inside the frame."""
    x, y, w, h = rect
    cw = max(minw, w * pad, h * pad * 16 / 9)
    cw = min(cw, W)
    ch = cw * 9 / 16
    cx, cy = x + w / 2, y + h / 2
    x0 = min(max(cx - cw / 2, 0), W - cw)
    y0 = min(max(cy - ch / 2, 0), H - ch)
    return [x0, y0, cw]


FULL = [0, 0, W]


def scenes(meta: dict) -> list[dict]:
    """The whole film. Each scene: name, duration, kind, actions by second.

    Pacing rule: every caption stays in the frame for at least 2.5 s (crossfade
    included), otherwise it cannot be read to the end — to the film's author
    everything is "clear" because they know what comes next.
    """
    R = {k: (fr(v) if v else None) for k, v in meta["report"]["rects"].items()}
    D = {k: (fr(v) if v else None) for k, v in meta["disk"]["rects"].items()}
    V = {k: (fr(v) if v else None) for k, v in meta["versions"]["rects"].items()}
    T = {k: (fr(v) if v else None) for k, v in meta["settings"]["rects"].items()}
    ids = meta["ids"]
    host = urllib.parse.urlparse(meta["stand"]).netloc
    fname = meta.get("standalone_name") or "report.html"
    kb = max(1, round(meta.get("standalone_bytes", 0) / 1024))

    def ctr(r):
        return [r[0] + r[2] / 2, r[1] + r[3] / 2]

    kpi_cam = around(R["kpis"], pad=1.12) if R.get("kpis") else FULL
    disk_cam = around(D["kpis"], pad=1.12) if D.get("kpis") else FULL
    # Dock on the right: the camera hugs the right edge and starts right below the
    # address strip — an address cut in the middle ("l0/d/...") reads as an
    # error. The frame keeps the dock tabs and the edge of the report they
    # belong to. Zoom 1.2x — the dock's height (the versions list) allows no more.
    dock_cam = [W - 1600, BAR, 1600]
    # Presentation: the camera crops exactly at the sidebar edge, not through the
    # middle of its labels.
    sb = meta["pres"]["rects"].get("main") or [216, 0, 0, 0]
    pres_cam = [sb[0] * S - 6, BAR, W - (sb[0] * S - 6)]

    return [
        {"name": "title", "dur": 4.8, "kind": "type",
         "html": f"""
          <div class="t-wrap">
            <div class="t-file" data-a="file">{fname}</div>
            <h1 class="t-h" data-a="h">A report is one HTML file.</h1>
            <p class="t-sub" data-a="sub">OnePage gives it an address and a list of people who may open it.</p>
          </div>""",
         "anim": [["file", 0.15, 0.7], ["h", 0.45, 1.1], ["sub", 1.5, 2.1]]},

        {"name": "platform", "dur": 6.4, "kind": "screen",
         "url": f"http://{host}/d/{ids['report']}", "badge": "",
         "shots": [["report", 0]],
         "caps": [[0.3, 6.4, "Served by OnePage: one address, an access list, versions"]],
         "cam": [[0, FULL], [2.4, FULL], [5.6, kpi_cam]]},

        {"name": "download", "dur": 5.2, "kind": "screen",
         "url": f"http://{host}/d/{ids['report']}", "badge": "",
         "shots": [["versions", 0]],
         "caps": [[0.3, 5.2, "Download it as one file — the data travels inside"]],
         "cursor": [[0, 1180, 640], [0.7, 1180, 640],
                    [1.9] + ctr(V["standalone"])] if V.get("standalone") else [],
         "clicks": [2.15],
         "chip": [2.45, f"{fname} · {kb} KB"],
         "cam": [[0, FULL], [0.4, FULL], [1.9, dock_cam]]},

        {"name": "disk", "dur": 6.8, "kind": "screen",
         "url": f"file:///…/Downloads/{fname}", "badge": "network off",
         "shots": [["disk", 0]],
         "caps": [[0.3, 6.8, "The same file, opened from disk. No server, no network."]],
         "cam": [[0, FULL], [2.6, FULL], [5.8, disk_cam]]},

        {"name": "same", "dur": 4.2, "kind": "compare",
         "a": {"shot": "report", "rect": R.get("kpis"), "label": f"http://{host} · served by OnePage"},
         "b": {"shot": "disk", "rect": D.get("kpis"), "label": f"file:// · {fname} · offline"},
         "caps": [[0.4, 4.2, "Same numbers."]]},

        {"name": "dock", "dur": 12.0, "kind": "screen",
         "url": f"http://{host}/d/{ids['report']}", "badge": "",
         "shots": [["settings", 0], ["access", 1.15], ["versions", 5.15], ["share", 9.15]],
         "caps": [[0.2, 4.0, "Who may open it is a list of people"],
                  [4.2, 8.0, "Every upload is a version you can restore"],
                  [8.2, 12.0, "Share links for everyone else — with an expiry"]],
         "cursor": ([[0, 1300, 700], [0.85] + ctr(T["tab_access"]),
                     [4.0] + ctr(T["tab_access"]), [4.85] + ctr(T["tab_versions"]),
                     [8.0] + ctr(T["tab_versions"]), [8.85] + ctr(T["tab_share"])]
                    if T.get("tab_share") else []),
         "clicks": [1.0, 5.0, 9.0],
         "cam": [[0, dock_cam]]},

        {"name": "pres", "dur": 4.0, "kind": "screen",
         "url": f"http://{host}/presentations/{ids['presentation']}", "badge": "",
         "shots": [["pres", 0]],
         "caps": [[0.3, 4.0, "Put reports on slides and present them to a room"]],
         "cam": [[0, FULL], [0.5, FULL], [3.2, pres_cam]]},

        {"name": "facts", "dur": 6.2, "kind": "type",
         "html": """
          <div class="f-wrap">
            <div class="f-row" data-a="a">
              <h2 class="f-h">Your data can stay on your machine.</h2>
              <p class="f-sub">generators run in the browser · Pyodide + DuckDB-WASM · only the result is uploaded</p>
            </div>
            <div class="f-row" data-a="b">
              <h2 class="f-h">Nothing needs the internet.</h2>
              <p class="f-sub">no CDN · no telemetry · built for isolated corporate networks</p>
            </div>
          </div>""",
         "anim": [["a", 0.3, 0.9], ["b", 2.9, 3.5]]},

        {"name": "try", "dur": 7.2, "kind": "type",
         "html": f"""
          <div class="k-wrap">
            <h2 class="k-h" data-a="h">Try it on your laptop</h2>
            <div class="k-term" data-a="term">
              <div class="k-line"><span class="k-p">$</span> <span data-type="l1">git clone https://{REPO_URL}</span></div>
              <div class="k-line"><span class="k-p">$</span> <span data-type="l2">cd onepage &amp;&amp; make demo</span></div>
              <div class="k-line k-out" data-a="out">→ http://localhost:8000/dev-login · sign in as admin</div>
            </div>
            <p class="k-sub" data-a="sub">Docker, or Python 3.12 without it. Demo data included.</p>
          </div>""",
         "anim": [["h", 0.1, 0.6], ["term", 0.3, 0.8], ["out", 3.6, 4.0], ["sub", 4.2, 4.8]],
         "type": [["l1", 0.9, 2.4], ["l2", 2.6, 3.4]]},

        {"name": "end", "dur": 5.0, "kind": "card"},
    ]


# ════════════════════════════════════════════════════════════════════════
# Composition page
# ════════════════════════════════════════════════════════════════════════

SCENE_CSS = """
@font-face{font-family:"Inter Tight";font-weight:400;src:url(fonts/inter-tight-400-latin.woff2)}
@font-face{font-family:"Inter Tight";font-weight:500;src:url(fonts/inter-tight-500-latin.woff2)}
@font-face{font-family:"Inter Tight";font-weight:600;src:url(fonts/inter-tight-600-latin.woff2)}
@font-face{font-family:"JetBrains Mono";font-weight:400;src:url(fonts/jetbrains-mono-400-latin.woff2)}
@font-face{font-family:"JetBrains Mono";font-weight:500;src:url(fonts/jetbrains-mono-500-latin.woff2)}
:root{--ink:#0E1116;--paper:#F6F7F9;--muted:#8A94A3;--line:#D9DEE5;--accent:#2F6BFF}
*{box-sizing:border-box;margin:0}
html,body{width:1920px;height:1080px;overflow:hidden;background:var(--ink)}
body{font-family:"Inter Tight",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.scene{position:absolute;left:0;top:0;width:1920px;height:1080px;display:none;overflow:hidden}
.scene.on{display:block}
/* address strip: the real address of the captured page, not decoration */
.bar{position:absolute;left:0;top:0;width:1920px;height:56px;background:#E9ECF1;
  border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 22px;gap:14px}
.dots{display:flex;gap:9px}.dots i{width:13px;height:13px;border-radius:50%;background:#C5CBD3;display:block}
.addr{flex:1;height:36px;border-radius:9px;background:#fff;border:1px solid var(--line);
  display:flex;align-items:center;padding:0 16px;font:500 18px/1 "JetBrains Mono",monospace;color:#3A4350;
  white-space:nowrap;overflow:hidden}
.badge{height:32px;padding:0 14px;border-radius:999px;background:#0E1116;color:#fff;
  font:500 16px/32px "JetBrains Mono",monospace;letter-spacing:.02em}
.badge:empty{display:none}
.shot{position:absolute;left:0;top:56px;width:1920px;height:1024px;opacity:0}
/* the captions and cursor layer lives in camera coordinates */
.ov{position:absolute;left:0;top:0;width:1920px;height:1080px;transform-origin:0 0;pointer-events:none}
.cap{position:absolute;left:48px;bottom:48px;padding:16px 26px;border-radius:999px;
  background:rgba(14,17,22,.93);color:#fff;font:500 32px/1.15 "Inter Tight";letter-spacing:-.012em;
  box-shadow:0 14px 40px -12px rgba(0,0,0,.55);opacity:0}
.chip{position:absolute;right:48px;top:150px;padding:16px 22px;border-radius:14px;background:#fff;
  border:1px solid var(--line);box-shadow:0 22px 50px -18px rgba(14,17,22,.45);
  font:500 24px/1 "JetBrains Mono";color:#0E1116;opacity:0;display:flex;gap:14px;align-items:center}
.chip b{display:inline-block;width:30px;height:36px;border-radius:4px;background:var(--accent);
  clip-path:polygon(0 0,70% 0,100% 25%,100% 100%,0 100%)}
.cur{position:absolute;left:0;top:0;width:34px;height:34px;transform-origin:0 0;opacity:0}
.cur svg{display:block;filter:drop-shadow(0 2px 3px rgba(0,0,0,.35))}
.ring{position:absolute;width:64px;height:64px;margin:-32px 0 0 -32px;border-radius:50%;
  border:3px solid var(--accent);opacity:0}
/* typographic scenes */
.type{background:var(--ink);color:#fff}
.t-wrap{position:absolute;left:180px;top:300px;right:180px}
.t-file{display:inline-block;font:500 26px/1 "JetBrains Mono";color:#C9D3FF;padding:12px 18px;
  border:1px solid #2B3340;border-radius:10px;background:#151A22;margin-bottom:44px}
.t-h{font:600 112px/1.02 "Inter Tight";letter-spacing:-.035em}
.t-sub{margin-top:36px;font:400 40px/1.3 "Inter Tight";color:#AEB7C4;letter-spacing:-.01em;max-width:1300px}
.f-wrap{position:absolute;left:180px;top:250px;right:180px;display:flex;flex-direction:column;gap:110px}
.f-h{font:600 80px/1.05 "Inter Tight";letter-spacing:-.03em}
.f-sub{margin-top:22px;font:400 28px/1.35 "JetBrains Mono";color:#8FA0B5}
.k-wrap{position:absolute;left:180px;top:210px;right:180px}
.k-h{font:600 76px/1 "Inter Tight";letter-spacing:-.03em;margin-bottom:56px}
.k-term{background:#151A22;border:1px solid #2B3340;border-radius:18px;padding:44px 52px;
  font:500 40px/1.75 "JetBrains Mono";color:#E8EDF4}
.k-p{color:#5C6B80}.k-out{color:#7FD1A8;font-size:30px;margin-top:14px}
.k-sub{margin-top:40px;font:400 30px/1.3 "Inter Tight";color:#8FA0B5}
.caret{display:inline-block;width:.55em;height:1.05em;background:#E8EDF4;vertical-align:-.15em;margin-left:2px}
/* comparison */
.cmp{background:#F6F7F9}
.cmp-row{position:absolute;left:120px;width:1680px;height:260px;border-radius:16px;overflow:hidden;
  border:1px solid var(--line);background:#fff;box-shadow:0 18px 50px -24px rgba(14,17,22,.35)}
.cmp-row .crop{position:absolute;left:0;top:64px;width:1680px;height:196px;overflow:hidden}
.cmp-row img{position:absolute;transform-origin:0 0}
.cmp-row .lab{position:absolute;left:0;top:0;right:0;height:64px;display:flex;align-items:center;
  padding:0 26px;font:500 22px/1 "JetBrains Mono";color:#3A4350;background:#EEF1F5;border-bottom:1px solid var(--line)}
.end img{position:absolute;left:0;top:0;width:1920px;height:1080px}
"""

SCENE_JS = r"""
var CFG = %(cfg)s;
function clamp(x,a,b){return Math.max(a,Math.min(b,x))}
function ease(x){x=clamp(x,0,1);return x<.5?4*x*x*x:1-Math.pow(-2*x+2,3)/2}
function lerp(a,b,u){return a+(b-a)*u}
function fade(t,a,b){return ease((t-a)/Math.max(.001,b-a))}
// Caption visibility window: appear over 0.3 s, leave 0.25 s before the end.
function win(t,a,b){if(t<a||t>b)return 0;return Math.min(fade(t,a,a+.3),1-fade(t,b-.25,b))}
function path(keys,t){ // [[t,x,y],...] → [x,y]
  if(!keys.length)return null; if(t<=keys[0][0])return [keys[0][1],keys[0][2]];
  for(var i=1;i<keys.length;i++){var a=keys[i-1],b=keys[i];
    if(t<=b[0]){var u=ease((t-a[0])/Math.max(.001,b[0]-a[0]));return [lerp(a[1],b[1],u),lerp(a[2],b[2],u)]}}
  var l=keys[keys.length-1];return [l[1],l[2]]}
function r3(x){return Math.round(x*1000)/1000}

window.seek = function(name,t,cx,cy,cw){
  var sc=CFG.scenes[name], el=document.getElementById('s-'+name), sig=[];
  document.querySelectorAll('.scene').forEach(function(n){n.classList.toggle('on',n===el)});
  var z=1920/cw, ov=el.querySelector('.ov');
  if(ov) ov.style.transform='translate('+cx+'px,'+cy+'px) scale('+(1/z)+')';
  (sc.anim||[]).forEach(function(a){
    var n=el.querySelector('[data-a="'+a[0]+'"]'); var u=fade(t,a[1],a[2]);
    n.style.opacity=u; n.style.transform='translateY('+((1-u)*26)+'px)'; sig.push(r3(u));});
  (sc.type||[]).forEach(function(a){
    var n=el.querySelector('[data-type="'+a[0]+'"]'); var full=n.getAttribute('data-full');
    if(full===null){full=n.textContent;n.setAttribute('data-full',full)}
    var k=Math.round(clamp((t-a[1])/(a[2]-a[1]),0,1)*full.length);
    var caret=(t>=a[1]-.4&&(t<a[2]+.6||a[0]===sc.type[sc.type.length-1][0]))&&(Math.floor(t*2.2)%%2===0||t<a[2]);
    n.innerHTML=''; n.appendChild(document.createTextNode(full.slice(0,k)));
    if(caret){var c=document.createElement('span');c.className='caret';n.appendChild(c)}
    sig.push(k,caret?1:0);});
  // stills: the latest whose time has come; switching is a short 0.15 s swap
  var shots=el.querySelectorAll('.shot');
  (sc.shots||[]).forEach(function(s,i){
    var next=(sc.shots[i+1]||[0,1e9])[1];
    var o=(t>=s[1]&&t<next)?1:0;
    if(i>0&&t>=s[1]&&t<s[1]+.15)o=(t-s[1])/.15;
    shots[i].style.opacity=o; sig.push(r3(o));});
  var cap=el.querySelector('.cap');
  if(cap){var shown=null,o=0;(sc.caps||[]).forEach(function(c){var w=win(t,c[0],c[1]);if(w>0){shown=c[2];o=w}});
    if(shown!==null&&cap.textContent!==shown)cap.textContent=shown; cap.style.opacity=o; sig.push(shown,r3(o));}
  var cur=el.querySelector('.cur');
  if(cur){var p=path(sc.cursor||[],t);
    if(p){cur.style.opacity=fade(t,0,.25); cur.style.transform='translate('+p[0]+'px,'+p[1]+'px) scale('+(1/z)+')';
      sig.push(r3(p[0]),r3(p[1]));
      var ring=el.querySelector('.ring'),ro=0,rs=1;
      (sc.clicks||[]).forEach(function(c){if(t>=c&&t<c+.45){var u=(t-c)/.45;ro=1-u;rs=.5+u*.9}});
      ring.style.left=p[0]+'px';ring.style.top=p[1]+'px';ring.style.opacity=ro;
      ring.style.transform='scale('+(rs/z)+')';sig.push(r3(ro));}}
  var chip=el.querySelector('.chip');
  if(chip&&sc.chip){var u=fade(t,sc.chip[0],sc.chip[0]+.35);chip.style.opacity=u;
    chip.style.transform='translateY('+((1-u)*30)+'px)';sig.push(r3(u));}
  return JSON.stringify(sig);
};
"""

CURSOR_SVG = ('<svg width="34" height="34" viewBox="0 0 24 24"><path d="M3 2l7.5 19 2.6-7.9L21 '
              '10.5z" fill="#0E1116" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>')


def scene_html(sc: dict, meta: dict) -> str:
    n, k = sc["name"], sc["kind"]
    if k == "type":
        return f'<section class="scene type" id="s-{n}">{sc["html"]}</section>'
    if k == "card":
        return f'<section class="scene end" id="s-{n}"><img src="panels/endcard.png"></section>'
    if k == "compare":
        rows = []
        for i, side in enumerate((sc["a"], sc["b"])):
            r = side["rect"]                      # rectangle in frame pixels
            # the KPI row is fitted to a width of 1680 px
            z = 1680 / (r[2] + 40)
            x = -(r[0] - 20) * z
            y = -(r[1] - BAR - 14) * z            # the still starts below the address strip
            rows.append(
                f'<div class="cmp-row" style="top:{230 + i * 330}px" data-a="r{i}">'
                f'<div class="lab">{side["label"]}</div><div class="crop">'
                f'<img src="panels/{side["shot"]}.png" style="width:{W * z}px;'
                f'height:{(H - BAR) * z}px;left:{x}px;top:{y}px"></div></div>')
        sc.setdefault("anim", [["r0", 0.0, 0.5], ["r1", 0.5, 1.0]])
        return (f'<section class="scene cmp" id="s-{n}">{"".join(rows)}'
                f'<div class="ov"><div class="cap"></div></div></section>')
    shots = "".join(f'<img class="shot" src="panels/{s}.png">' for s, _ in sc["shots"])
    chip = ('<div class="chip"><b></b><span>%s</span></div>' % sc["chip"][1]) if sc.get("chip") else ""
    cur = (f'<div class="ring"></div><div class="cur">{CURSOR_SVG}</div>'
           if sc.get("cursor") else "")
    return (f'<section class="scene screen" id="s-{n}">'
            f'<div class="bar"><div class="dots"><i></i><i></i><i></i></div>'
            f'<div class="addr">{sc["url"]}</div><div class="badge">{sc.get("badge", "")}</div></div>'
            f'{shots}{cur}<div class="ov"><div class="cap"></div>{chip}</div></section>')


ENDCARD = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>OnePage</title>
<style>%(fonts)s
*{box-sizing:border-box;margin:0}
html,body{width:1920px;height:1080px;background:#0E1116;color:#fff;overflow:hidden;
 font-family:"Inter Tight",sans-serif;-webkit-font-smoothing:antialiased}
.w{position:absolute;left:180px;right:180px;top:320px}
.k{font:500 30px/1 "JetBrains Mono";color:#8FA0B5;letter-spacing:.02em}
h1{margin-top:34px;font:600 132px/1 "Inter Tight";letter-spacing:-.04em}
.u{margin-top:54px;display:inline-block;font:500 50px/1 "JetBrains Mono";color:#fff;
 padding:26px 34px;border-radius:16px;background:#151A22;border:1px solid #2B3340}
.u b{color:#2F6BFF;font-weight:500}
.f{margin-top:56px;font:400 36px/1 "Inter Tight";color:#AEB7C4}
.f i{font-style:normal;color:#4A5566;padding:0 18px}
</style></head><body><div class="w">
<div class="k">open source · one HTML file per report</div>
<h1>OnePage</h1>
<div class="u"><b>github.com/</b>onepage-bi/onepage</div>
<div class="f">Apache-2.0<i>·</i>runs offline<i>·</i>one make demo</div>
</div></body></html>"""


def font_faces(prefix: str) -> str:
    return "".join(
        '@font-face{font-family:"%s";font-weight:%s;src:url(%s%s)}' % (
            "Inter Tight" if f.startswith("inter") else "JetBrains Mono",
            f.split("-")[-2], prefix, f) for f in FONTS)


def build_page(work: Path, meta: dict, scs: list[dict]) -> None:
    fdir = work / "fonts"
    fdir.mkdir(exist_ok=True)
    for f in FONTS:
        shutil.copy2(REPO / "static" / "fonts" / f, fdir / f)
    (work / "endcard.html").write_text(ENDCARD % {"fonts": font_faces("fonts/")})
    body = "".join(scene_html(sc, meta) for sc in scs)
    cfg = {"scenes": {sc["name"]: {k: sc.get(k) for k in
                                   ("anim", "type", "shots", "caps", "cursor", "clicks", "chip")}
                      for sc in scs}}
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>OnePage promo</title>'
            f'<style>{SCENE_CSS}</style></head><body>{body}'
            f'<script>{SCENE_JS % {"cfg": json.dumps(cfg)}}</script></body></html>')
    (work / "scene.html").write_text(html)


def camera(sc: dict, t: float) -> list[float]:
    """Camera [x, y, w] at time t: a smooth transition between keys."""
    keys = sc.get("cam") or [[0, FULL]]
    if t <= keys[0][0]:
        return list(keys[0][1])
    for (ta, a), (tb, b) in zip(keys, keys[1:]):
        if t <= tb:
            u = (t - ta) / max(1e-3, tb - ta)
            u = 4 * u ** 3 if u < .5 else 1 - (-2 * u + 2) ** 3 / 2
            # interpolate the width in log space: the zoom reads as uniform
            w = math.exp(math.log(a[2]) + (math.log(b[2]) - math.log(a[2])) * u)
            return [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u, w]
    return list(keys[-1][1])


def serve(root: Path):
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    h.log_message = lambda *a, **k: None
    httpd = socketserver.TCPServer(("127.0.0.1", 0), h)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


async def render(work: Path, meta: dict, only: set[str] | None) -> None:
    from cdp import ChromeSession

    scs = scenes(meta)
    build_page(work, meta, scs)
    httpd = serve(work)
    port = httpd.server_address[1]
    s = ChromeSession(port=9342)
    s.start()
    await s.connect()
    try:
        await s.send("Emulation.setDeviceMetricsOverride",
                     {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False})
        # the end card is a separate page captured by the same Chrome
        await s.navigate(f"http://127.0.0.1:{port}/endcard.html", wait_ms=900)
        await s.eval("document.fonts.ready.then(function(){return 1})", await_promise=True)
        await stable_shot(s, work / "panels" / "endcard.png")
        await s.navigate(f"http://127.0.0.1:{port}/scene.html", wait_ms=900)
        n_img = await s.eval(
            "(async function(){await document.fonts.ready;await Promise.all([...document.images]"
            ".map(function(i){return i.decode().catch(function(){return 0})}));"
            "return document.images.length})()", await_promise=True)
        if s.console_errors:
            print("  WARNING console:", s.console_errors[:3])
        print(f"  composition ready: {n_img} images, {len(scs)} scenes")
        for sc in scs:
            if only and sc["name"] not in only:
                continue
            out = work / "frames" / sc["name"]
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True)
            n = round(sc["dur"] * FPS)
            prev_sig, prev_path, shot_n = None, None, 0
            for k in range(n):
                t = k / FPS
                cx, cy, cw = camera(sc, t)
                sig = await s.eval(f"seek({json.dumps(sc['name'])},{t:.5f},{cx:.3f},{cy:.3f},{cw:.3f})")
                sig = f"{sig}|{cx:.2f},{cy:.2f},{cw:.2f}"
                p = out / f"f_{k:05d}.png"
                if sig == prev_sig:
                    shutil.copyfile(prev_path, p)      # frame unchanged — do not capture
                    continue
                await s.eval("new Promise(function(r){requestAnimationFrame(function(){"
                             "requestAnimationFrame(function(){r(1)})})})", await_promise=True)
                r = await s.send("Page.captureScreenshot", {
                    "format": "png", "clip": {"x": cx, "y": cy, "width": cw,
                                              "height": cw * 9 / 16, "scale": W / cw}})
                p.write_bytes(base64.b64decode(r["data"]))
                prev_sig, prev_path = sig, p
                shot_n += 1
            print(f"  {sc['name']}: {n} frames ({shot_n} captured, the rest are copies)")
    finally:
        await s.close()
        s.stop()
        httpd.shutdown()


# ════════════════════════════════════════════════════════════════════════
# Build and check
# ════════════════════════════════════════════════════════════════════════

def build(work: Path, meta: dict, out: Path) -> None:
    scs = scenes(meta)
    args = ["ffmpeg", "-y", "-loglevel", "error"]
    for sc in scs:
        args += ["-framerate", str(FPS), "-i", str(work / "frames" / sc["name"] / "f_%05d.png")]
    args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    parts, last, off = [], "v0", 0.0
    for i, sc in enumerate(scs):
        parts.append(f"[{i}:v]scale={W}:{H}:flags=lanczos,fps={FPS},settb=AVTB,format=yuv420p[v{i}]"
                     if i else
                     f"[0:v]scale={W}:{H}:flags=lanczos,fps={FPS},settb=AVTB,format=yuv420p[v0]")
    for i, sc in enumerate(scs[1:], start=1):
        off += scs[i - 1]["dur"] - XFADE
        parts.append(f"[{last}][v{i}]xfade=transition=fade:duration={XFADE}:offset={off:.3f}[x{i}]")
        last = f"x{i}"
    total = sum(sc["dur"] for sc in scs) - XFADE * (len(scs) - 1)
    args += ["-filter_complex", ";".join(parts), "-map", f"[{last}]", "-map", f"{len(scs)}:a",
             "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
             "-profile:v", "high", "-r", str(FPS), "-movflags", "+faststart",
             "-c:a", "aac", "-b:a", "96k", "-t", f"{total:.3f}", str(out)]
    subprocess.run(args, check=True)
    print(f"  built {out} — {total:.1f} s, {out.stat().st_size / 1e6:.1f} MB")


def boundaries(meta: dict) -> list[tuple[str, float]]:
    """Control frame moments: the middle of every scene and every joint."""
    pts, t = [], 0.0
    scs = scenes(meta)
    for i, sc in enumerate(scs):
        pts.append((f"{i:02d}-{sc['name']}-mid", t + sc["dur"] / 2))
        t += sc["dur"] - XFADE
        if i < len(scs) - 1:
            pts.append((f"{i:02d}-{sc['name']}-cut", t + XFADE / 2))
    pts.append((f"{len(scs) - 1:02d}-end-last", t + XFADE - 0.05))
    return pts


OCR_SWIFT = r"""
import Foundation
import Vision
import AppKit
// Recognises the text on every frame and prints lines containing Cyrillic.
var bad = 0
for p in CommandLine.arguments.dropFirst() {
  guard let img = NSImage(contentsOfFile: p),
        let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
  let req = VNRecognizeTextRequest()
  req.recognitionLevel = .accurate
  req.recognitionLanguages = ["ru-RU", "en-US"]
  try? VNImageRequestHandler(cgImage: cg).perform([req])
  for o in req.results ?? [] {
    let s = o.topCandidates(1).first?.string ?? ""
    if s.range(of: "[\\u0400-\\u04FF]", options: .regularExpression) != nil {
      print("CYR\t\(p)\t\(s)"); bad += 1
    }
  }
}
print("OCR_DONE\t\(bad)")
"""


def check(work: Path, meta: dict, out: Path) -> int:
    rc = 0
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                         "stream=codec_name,width,height,pix_fmt,r_frame_rate,codec_type:"
                         "format=duration,size", "-of", "json", str(out)],
                        capture_output=True, text=True, check=True)
    print("  ffprobe:", json.dumps(json.loads(pr.stdout), ensure_ascii=False))
    # control frames
    ctl = work / "control"
    shutil.rmtree(ctl, ignore_errors=True)
    ctl.mkdir()
    for name, t in boundaries(meta):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(out),
                        "-frames:v", "1", str(ctl / f"{name}-{t:05.2f}s.png")], check=True)
    print(f"  control frames: {ctl}")
    # Cyrillic is searched for three times, and each method has its own trust level.
    #  1. DOM: the visible text of every captured screen and the report inside it
    #     (meta.json, capture stage) — exact, it is the page's own text.
    #  2. OCR of the sharp stills panels/*.png (DPR 2): Vision with the Russian model
    #     reads glyphs (⚠, the dock icon) as a single Cyrillic letter — a known
    #     false signal, so only a WORD, two Cyrillic letters in a row, counts as
    #     a failure.
    #  3. OCR of every distinct frame of the film: in zoom frames small English
    #     captions (STORES OUT OF STOCK) blur into homoglyphs (Cyrillic
    #     look-alikes of "STOCK"). Such lines do not fail the check; they are written to
    #     ocr_hits.tsv for eyeballing; if the sharp still of the same screen does
    #     not have them, they are a motion artefact.
    sw = work / "ocr.swift"
    sw.write_text(OCR_SWIFT)

    def ocr(paths: list[str]) -> list[tuple[str, str]]:
        hits = []
        for i in range(0, len(paths), 200):
            r = subprocess.run(["swift", str(sw), *paths[i:i + 200]],
                               capture_output=True, text=True)
            if "OCR_DONE" not in r.stdout:
                raise SystemExit("OCR failed: " + r.stderr[-400:])
            hits += [tuple(l.split("\t")[1:3]) for l in r.stdout.splitlines()
                     if l.startswith("CYR")]
        return hits

    word = re.compile(r"[\u0400-\u04FF]{2,}")
    stills = sorted(str(p) for p in (work / "panels").glob("*.png"))
    s_hits = ocr(stills)
    s_words = [h for h in s_hits if word.search(h[1])]
    print(f"  OCR of stills: {len(stills)} files, Cyrillic words {len(s_words)}, "
          f"single glyph letters {len(s_hits) - len(s_words)}")
    for h in s_words:
        print("    WORD:", h)

    seen, uniq = set(), []
    allf = sorted((work / "frames").glob("*/f_*.png"))
    for p in allf:
        hsh = hashlib.sha1(p.read_bytes()).hexdigest()
        if hsh not in seen:
            seen.add(hsh)
            uniq.append(str(p))
    f_hits = ocr(uniq)
    (work / "ocr_hits.tsv").write_text("".join(f"{a}\t{b}\n" for a, b in f_hits))
    f_words = [h for h in f_hits if word.search(h[1])]
    print(f"  OCR of frames: {len(uniq)} distinct out of {len(allf)}; lines with Cyrillic "
          f"{len(f_hits)}, of them with a word {len(f_words)} — for review, {work / 'ocr_hits.tsv'}")
    dom = {k: v.get("cyrillic") for k, v in meta.items()
           if isinstance(v, dict) and v.get("cyrillic")}
    print(f"  Cyrillic in the DOM of captured screens: {dom or 0}")
    return 1 if (dom or s_words) else rc


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "op-promo"))
    ap.add_argument("--out", default=None, help="mp4; default <work>/onepage-promo.mp4")
    ap.add_argument("--only", default="prepare,capture,render,build,check",
                    help="comma-separated stages")
    ap.add_argument("--scene", default="", help="render only these scenes (comma-separated)")
    a = ap.parse_args(argv)
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    out = Path(a.out) if a.out else work / "onepage-promo.mp4"
    stages = set(a.only.split(","))
    meta_p = work / "panels" / "meta.json"

    if "prepare" in stages or "capture" in stages:
        ids = prepare(Stand(STAND, USER))
        if "capture" in stages:
            asyncio.run(capture(ids, work))
    meta = json.loads(meta_p.read_text())
    if "render" in stages:
        asyncio.run(render(work, meta, set(filter(None, a.scene.split(","))) or None))
    if "build" in stages:
        build(work, meta, out)
    if "check" in stages:
        return check(work, meta, out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
