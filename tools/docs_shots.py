#!/usr/bin/env python3
"""Capture the screenshots the Dev Guide is built from.

The guide shows the REAL product, not drawings of it, so every picture in it is
a screenshot of a running OnePage taken by this script. Re-run it whenever the
interface moves and the guide stops lying:

    OP_STAND=http://127.0.0.1:8766 tools/docs_shots.py

It drives the same headless Chrome the smoke tests use, signs in through the
dev bypass, and writes WebP files into static/img/docs/. Each shot declares its
own viewport, the element it waits for, and an optional crop, so the framing is
reproducible rather than hand-aimed.

Needs the stand to be running with OP_DEV_BYPASS_AUTH=true and some content in
it; a shot whose page or element is missing is reported and skipped, never
faked.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "tests", "chrome"))

from cdp import ChromeSession  # noqa: E402

STAND = os.environ.get("OP_STAND", "http://127.0.0.1:8766")
FIXTURE = os.path.join(HERE, "docs_fixtures", "demo_report.html")
# The report shots must show a REAL viewer around a report whose numbers are
# invented: no commercial data may travel into documentation. The fixture is
# uploaded to the stand on every run and the fresh id is used below.
REPORT_TITLE = "Shelf availability · week 37"

# Real report, generator and file names must not travel into documentation:
# a screenshot of a stand carries whatever that stand happens to hold. Before
# every capture the page's own strings are swapped for these, positionally —
# the first report on the list becomes the first name here — so the guide
# shows a plausible workspace and no real one. Nothing real is written down:
# the mapping is built in the browser at capture time and lives only in this
# process's memory.
DEMO_REPORTS = [
    "Sales by territory · week",
    "Stock and orders · DC",
    "Promo calendar · quarter",
    "Assortment matrix",
    "Distribution · top 100 stores",
    "Plan attainment · month",
    "Logistics · delivery times",
    "Headcount panel · region",
    "Margin · category",
    "Returns and write-offs",
    "Field team activity",
    "Project budgets",
]
DEMO_GENERATORS = ["salesreport", "stockreport", "promoreport", "fieldreport",
                   "marginreport", "logreport"]
DEMO_FILES = ["Sales_2026_w37.xlsx", "Stock_RC_2026.xlsx", "Promo_plan.xlsx",
              "Assortment.xlsx", "Outlets.xlsx", "Plan_fact.xlsx",
              "Deliveries.xlsx", "Returns.xlsx"]
DEMO_FOLDERS = ["Sales 2026", "DC stock", "Promo", "Lookups", "Plans"]
DEMO_SLUGS = ["client_map", "outlet_map", "channel_map", "unit_map",
              "brand_map", "region_map"]
USER = os.environ.get("OP_STAND_USER", "admin1")
OUT = os.path.join(REPO, "static", "img", "docs")
# Two device pixels per CSS pixel: the guide is read on laptop screens, and a
# 1x screenshot of an interface looks like a photocopy next to the real thing.
DPR = 2
QUALITY = "82"


@dataclass
class Shot:
    name: str
    path: str
    wait: str = "body"
    width: int = 1280
    height: int = 820
    settle: float = 1.2
    crop: Optional[tuple] = None        # (x, y, w, h) in CSS px
    before: Optional[str] = None        # JS to run once the page has settled
    note: str = ""
    hide: list = field(default_factory=list)   # selectors to take out of frame
    blur: list = field(default_factory=list)   # selectors to render unreadable


SHOTS = [
    # ?fox=off keeps the quests companion out of a documentation screenshot.
    Shot("workspace", "/?fox=off", wait=".dash-table, .dash-grid", height=820, settle=2.5,
         hide=["#op-companion"], blur=[".card iframe", ".preview iframe"],
         note="the list every person lands on"),
    Shot("report", "{report}", wait="iframe", height=860, settle=4.0,
         before="(function(){var r=document.getElementById('viewer-root');"
                "if(r) r.setAttribute('data-dock','closed');})()",
         note="a published report as a reader sees it"),
    # The chat pane only exists on a generator-backed report (can_discuss), so
    # the guide shows the same conversation on its own page instead of faking
    # a pane the demo fixture cannot have.
    # The report cards carry LIVE previews of whatever this stand holds: the
    # titles are aliased, the pixels are not, so they are blurred.
    Shot("explore", "/explore", wait=".content, #ex-root", height=860, settle=2.5,
         blur=[".ex-report-frame", ".ex-report-card iframe", ".ex-report-card img"],
         note="asking a report a question"),
    Shot("upload", "/upload", wait="#drop, .upload-drop, form", height=760,
         note="where a finished HTML file goes"),
    Shot("generators", "/generators", wait=".gen-list, .content", height=640,
         blur=[".preview-frame", ".preview iframe", ".preview img"],
         note="generators: a report that rebuilds itself"),
    # The whole graph fits the stage at ~26%, which is a picture of a shape
    # rather than of a lineage: zoom in so the cards are readable.
    Shot("lineage", "/data-sources", wait=".content", height=900, settle=2.6,
         before="(function(){var b=document.querySelector('[data-lin-zoom=\"in\"]');"
                "if(!b) return; for (var i = 0; i < 3; i++) b.click();})()",
         note="where every report's data comes from"),
    Shot("quality", "/data-quality", wait=".content", height=820,
         note="what the platform checks after every run"),
    Shot("reference", "/reference", wait=".content", height=640,
         note="shared lookup tables"),
    Shot("presentations", "/presentations", wait=".content", height=600,
         note="reports gathered into a deck"),
]


def webp(png_path: str, out_path: str, quality: str = QUALITY) -> int:
    """PNG -> WebP with cwebp, falling back to the PNG if it is missing."""
    cw = None
    for cand in ("/opt/homebrew/bin/cwebp", "/usr/local/bin/cwebp", "cwebp"):
        try:
            subprocess.run([cand, "-version"], capture_output=True, check=True)
            cw = cand
            break
        except Exception:
            continue
    if not cw:
        os.replace(png_path, out_path.replace(".webp", ".png"))
        return os.path.getsize(out_path.replace(".webp", ".png"))
    subprocess.run([cw, "-quiet", "-q", quality, png_path, "-o", out_path], check=True)
    os.remove(png_path)
    return os.path.getsize(out_path)


async def seed_report(s: ChromeSession) -> str:
    """Upload the synthetic demo report and return its /d/{id} path.

    Earlier copies are deleted first: a re-run must not leave the workspace
    shot showing the same demo report three times."""
    await s.navigate(STAND + "/")
    await s.eval(
        "(async function(){"
        "var el = document.getElementById('dash-data');"
        "if (!el) return 0;"
        "var rows = (JSON.parse(el.textContent || '{}').items) || [];"
        "var n = 0;"
        "for (var i = 0; i < rows.length; i++) {"
        "  if (rows[i].title !== %s) continue;"
        "  await fetch('/api/dashboards/' + rows[i].id, {method:'DELETE'});"
        "  n++;"
        "}"
        "return n;"
        "})()" % json.dumps(REPORT_TITLE), await_promise=True)
    with open(FIXTURE, encoding="utf-8") as f:
        html = f.read()
    js = (
        "(async function(){"
        "var html = %s;"
        "var fd = new FormData();"
        "fd.append('title', %s);"
        "fd.append('description', %s);"
        "fd.append('html_file', new File([html], 'demo_report.html',"
        " {type:'text/html'}));"
        "var r = await fetch('/api/dashboards', {method:'POST', body: fd});"
        "var m = /\\/d\\/([0-9a-f-]{36})/.exec(r.url || '');"
        "if (m) return m[1];"
        "var txt = await r.text();"
        "try { var j = JSON.parse(txt); if (j.id) return j.id; } catch (e) {}"
        "return 'ERR ' + r.status + ' ' + txt.slice(0, 200);"
        "})()" % (
            json.dumps(html),
            json.dumps(REPORT_TITLE),
            json.dumps("Demo report for the Dev Guide — every figure is invented"),
        )
    )
    rid = (await s.eval(js, await_promise=True) or "").strip().strip('"')
    if not rid or rid.startswith("ERR"):
        raise SystemExit("could not create the demo report: %s" % rid)
    # A draft badge would be the first thing a reader sees; publish it.
    await s.eval(
        "fetch('/api/dashboards/' + %s, {method:'PATCH',"
        " headers:{'Content-Type':'application/json'},"
        " body: JSON.stringify({is_published: true})}).then(function(r){return r.status})"
        % json.dumps(rid), await_promise=True)
    return "/d/" + rid


MASK_JS = r"""
(function(map){
  // Documentation must not carry a stand's real vocabulary. Three passes over
  // the text nodes, all deterministic so one name reads the same everywhere:
  //   1. exact names collected from this stand (reports, generators), matched
  //      whole — a short name must not rewrite the middle of a longer word;
  //   2. quoted «…» strings — on the quality and lineage pages those are the
  //      column and sheet names of real workbooks;
  //   3. folder:<name> and *.xls/*.xlsx file names.
  var COLS = ['Customer code','Product name','Region','Category','Volume',
              'Week','Outlet','Brand','Amount','Quantity','Date',
              'Manager','Plan','Actual','Channel'];
  var FOLDERS = ['Sales 2026','DC stock','Promo','Reference','Plans'];
  var FILES = ['Sales_2026_w37.xlsx','Stock_RC_2026.xlsx','Promo_plan.xlsx',
               'Assortment.xlsx','Outlets.xlsx','Plan_fact.xlsx'];
  var pool = {};
  function alias(kind, value, list) {
    var k = kind + '\u0000' + value;
    if (!pool[k]) {
      var n = Object.keys(pool).filter(function(x){ return x.indexOf(kind) === 0; }).length;
      pool[k] = list[n % list.length];
    }
    return pool[k];
  }
  function esc(x) { return x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
  var keys = Object.keys(map).filter(function(k){ return k.length >= 2; })
                   .sort(function(a, b){ return b.length - a.length; });
  var walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  var nodes = [];
  while (walk.nextNode()) nodes.push(walk.currentNode);
  var n = 0;
  nodes.forEach(function(node){
    var v = node.nodeValue;
    if (!v || !v.trim()) return;
    var before = v;
    keys.forEach(function(k){
      v = v.replace(new RegExp('(^|[^\\w\u0410-\u044F\u0401\u0451])' + esc(k) + '(?![\\w\u0410-\u044F\u0401\u0451])', 'g'),
                    function(m, p1){ return p1 + map[k]; });
    });
    v = v.replace(/«([^»]{2,60})»/g, function(m, inner){
      if (/^(выборка|проверка|знаем)/i.test(inner)) return m;
      return '«' + alias('col', inner, COLS) + '»';
    });
    v = v.replace(/[A-Za-z0-9_.\-]+\.(?:bin|csv|json)(?:\.gz|\.br)?\b/g, 'report_data.bin.gz');
    v = v.replace(/folder:\s*([^\s,;]+(?:\s+[^\s,;]+)?)/g, function(m, name){
      return 'folder:' + alias('dir', name, FOLDERS);
    });
    v = v.replace(/[A-Za-z\u0410-\u044F\u0401\u04510-9_.\-* ]{2,40}\.xlsx?/g, function(m){
      return alias('file', m.trim(), FILES);
    });
    if (v !== before) { node.nodeValue = v; n++; }
  });
  return n;
})(__NAMES__)
"""


async def collect_names(s: ChromeSession) -> dict:
    """Build the real -> invented mapping from this stand's own pages."""
    await s.navigate(STAND + "/")
    titles = await s.eval(
        "(function(){var el=document.getElementById('dash-data');"
        "if(!el) return '[]';"
        "var rows=(JSON.parse(el.textContent||'{}').items)||[];"
        "return JSON.stringify(rows.map(function(r){return r.title}));})()")
    await s.navigate(STAND + "/generators")
    await asyncio.sleep(1.2)
    gens = await s.eval(
        "JSON.stringify([].map.call(document.querySelectorAll("
        "'.gen-slug, .gen-card code, [data-generator], .gen-card .mono'),"
        "function(e){return (e.textContent||'').trim()}).filter(Boolean))")
    await s.navigate(STAND + "/data-quality")
    await asyncio.sleep(1.6)
    gens2 = await s.eval(
        "JSON.stringify([].map.call(document.querySelectorAll('#dq-rows td:nth-child(4),"
        " table tbody tr td:nth-child(4)'), function(e){return (e.textContent||'').trim()})"
        ".filter(function(x){return x && x.length > 2 && x.length < 40}))")
    folders = await s.eval(
        "JSON.stringify(Array.from(new Set(((document.body.innerText||'')"
        ".match(/folder:[^\\n]+/g) || []).map(function(x){"
        "return x.replace(/^folder:\\s*/, '').trim()}))))")
    files = await s.eval(
        "JSON.stringify(Array.from(new Set((document.body.innerText||'')"
        ".match(/[A-Za-z0-9_. -]+\\.xlsx?/g) || [])))")

    await s.navigate(STAND + "/reference")
    await asyncio.sleep(1.4)
    slugs = await s.eval(
        "JSON.stringify(Array.from(new Set([].map.call("
        "document.querySelectorAll('td, .ref-slug, code'),"
        "function(e){return (e.textContent||'').trim()})"
        ".filter(function(x){return /^[a-z][a-z0-9_]{3,40}$/.test(x)}))))")

    mapping: dict[str, str] = {}
    for i, name in enumerate(json.loads(titles or "[]")):
        if name and name != REPORT_TITLE:
            mapping[name] = DEMO_REPORTS[i % len(DEMO_REPORTS)]
    for i, name in enumerate(sorted(set(json.loads(gens or "[]"))
                                    | set(json.loads(gens2 or "[]")))):
        if len(name) > 2:
            mapping.setdefault(name, DEMO_GENERATORS[i % len(DEMO_GENERATORS)])
    for i, name in enumerate(sorted(set(json.loads(files or "[]")))):
        mapping.setdefault(name.strip(), DEMO_FILES[i % len(DEMO_FILES)])
    for i, name in enumerate(sorted(set(json.loads(folders or "[]")))):
        if len(name) > 1:
            mapping.setdefault(name, DEMO_FOLDERS[i % len(DEMO_FOLDERS)])
    for i, name in enumerate(sorted(set(json.loads(slugs or "[]")))):
        mapping.setdefault(name, DEMO_SLUGS[i % len(DEMO_SLUGS)])
    return mapping


async def grab(s: ChromeSession, shot: Shot, names: dict, navigate: bool = True) -> str:
    """Take a shot. `navigate=False` — the page is already open and prepared
    by the caller: this is how screens are captured whose state cannot be recreated
    from the page's own JS (a file is put into the input from outside, over CDP)."""
    await s.send("Emulation.setDeviceMetricsOverride", {
        "width": shot.width, "height": shot.height,
        "deviceScaleFactor": DPR, "mobile": False,
    })
    if navigate:
        await s.navigate(STAND + shot.path)
    ok = False
    for _ in range(40):
        await asyncio.sleep(0.15)
        found = await s.eval(
            "String(!!document.querySelector(%r))" % shot.wait)
        if found == "true":
            ok = True
            break
    if not ok:
        return "missing: %s never showed %s" % (shot.path, shot.wait)
    if shot.hide:
        # A style rule, not a per-element flag: what the page injects late (the
        # quests companion) must stay out of frame too.
        await s.eval("(function(){var st=document.createElement('style');"
                     "st.textContent=%s.map(function(q){return q+'{display:none!important}'})"
                     ".join('');document.head.appendChild(st);})()" % repr(shot.hide))
    if shot.before and navigate:
        await s.eval(shot.before)
    await asyncio.sleep(shot.settle if navigate else 0.2)
    if names:
        await s.eval(MASK_JS.replace("__NAMES__", json.dumps(names, ensure_ascii=False)))
    if shot.blur:
        # A live preview shows a real report's own pixels, which no alias can
        # reach: make them unreadable rather than pretend they are demo data.
        await s.eval("(function(){var st=document.createElement('style');"
                     "st.textContent=%s.map(function(q){"
                     "return q+'{filter:blur(7px)!important}'}).join('');"
                     "document.head.appendChild(st);})()" % repr(shot.blur))
    params = {"format": "png"}
    if shot.crop:
        x, y, w, h = shot.crop
        params["clip"] = {"x": x, "y": y, "width": w, "height": h, "scale": DPR}
    r = await s.send("Page.captureScreenshot", params)
    png = os.path.join(OUT, shot.name + ".png")
    with open(png, "wb") as f:
        f.write(base64.b64decode((r.get("result") or r)["data"]))
    size = webp(png, os.path.join(OUT, shot.name + ".webp"))
    return "%-14s %6.1f KB  %s" % (shot.name + ".webp", size / 1024.0, shot.note)


async def main() -> int:
    os.makedirs(OUT, exist_ok=True)
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
        # The language is pinned by a cookie, not a browser flag. `--accept-lang` decides
        # only the FIRST request: login sets `op_lang` from that header, and
        # after that the cookie beats the header (users.lang -> cookie -> Accept-Language).
        # So a profile whose cookie is left over from a previous capture shoots the wrong language
        # silently — exactly what happened: half the menu came out in Russian with
        # OP_CHROME_LANG=en-GB. We set what was asked for and do not depend on history.
        await s.eval(
            "document.cookie='op_lang=%s;path=/;max-age=86400'"
            % os.environ.get("OP_CHROME_LANG", "ru").split("-")[0])
        report = await seed_report(s)
        print("demo report %s" % report)
        names = await collect_names(s)
        print("masking %d real name(s)" % len(names))
        for shot in SHOTS:
            if "{report}" in shot.path:
                shot.path = shot.path.replace("{report}", report)
            print(await grab(s, shot, names))
    finally:
        await s.close()
        s.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
