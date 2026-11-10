#!/usr/bin/env python3
"""Screenshots for the Practicum course: real screens, not a description in words.

    OP_STAND=http://127.0.0.1:8766 tools/learn_shots.py

The upload lesson used to be a list of steps in text, and that is not enough:
a person opening the platform for the first time searches with their eyes, not
by reading. The Dev Guide got this right long ago: it shows the real product
(`tools/docs_shots.py`), and the course should do the same.

The script reuses the guide's machinery entirely: the same headless Chrome, the
same dev-bypass sign-in, the same name MASKING (the stand holds real reports and
folders, and they must not get into teaching material) and the same WebP
packaging. Only the frames and the directory differ: `static/img/learn/`.

Frames are shot with a hint: a yellow ring around the element the step talks
about. The script draws the ring itself before capturing, so the picture shows
not "the page in general" but exactly the button to press.
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "tests", "chrome"))

import docs_shots as ds                                    # noqa: E402
from cdp import ChromeSession                              # noqa: E402

OUT = os.path.join(REPO, "static", "img", "learn")
SAMPLE = os.path.join(REPO, "static", "learn", "files", "delta-report.html")
SAMPLE_TITLE = "Lost sales · stores · week"


# The lesson shows the Anonymizer step by step, and for that the page has to be
# fed something. The page itself cannot download the file: it is locked with
# `connect-src 'none'` (its guarantee that nothing goes to the network), and the
# first attempt to capture the screens via fetch() honestly ran into it. So the
# file is put into the input from outside, over CDP (DOM.setFileInputFiles),
# exactly as the browser test does. The file is a teaching one
# (tools/learn_fixture_anon.py): all clients, tax ids and phones are invented.
ANON_DEMO = os.path.join(REPO, "static", "learn", "files", "anon-demo.xlsx")

# Wait until the worker parses the workbook and shows the column table.
WAIT_PARSED = """(function () {
  return document.querySelectorAll('table.anon-cols tbody tr').length > 1 ? 'ok' : '';
})()"""

# Second step for the last frame: run the masking and wait for the result
# panel; the sample for the prompt appears only after it.
RUN_MASK = """(function () {
  var b = document.getElementById('btn-anon-run');
  if (b) b.click();
  return 'ok';
})()"""

WAIT_RESULT = """(function () {
  var b = document.getElementById('anon-result');
  return (b && !b.hidden) ? 'ok' : '';
})()"""


async def feed_anon(s, run: bool = False) -> None:
    """Put the teaching workbook into the file input and wait for it to be parsed."""
    doc = await s.send("DOM.getDocument", {})
    root = (doc.get("result") or doc)["root"]["nodeId"]
    q = await s.send("DOM.querySelector", {"nodeId": root, "selector": "#anon-file-input"})
    node = (q.get("result") or q).get("nodeId")
    if not node:
        return
    await s.send("DOM.setFileInputFiles", {"files": [ANON_DEMO], "nodeId": node})
    for _ in range(80):
        await ds.asyncio.sleep(0.25)
        if await s.eval(WAIT_PARSED):
            break
    if not run:
        return
    await s.eval(RUN_MASK)
    for _ in range(160):
        await ds.asyncio.sleep(0.25)
        if await s.eval(WAIT_RESULT):
            break
    await ds.asyncio.sleep(0.4)


# Step highlight. Drawn over the real element, not added in an editor: if the
# button moves, the ring moves with it.
RING = """(function (sel) {
  var e = document.querySelector(sel);
  if (!e) return 'missing ' + sel;
  var r = e.getBoundingClientRect(), p = 6;
  var d = document.createElement('div');
  d.style.cssText = 'position:fixed;z-index:99999;pointer-events:none;' +
    'border:3px solid #f0a500;border-radius:10px;' +
    'box-shadow:0 0 0 4px rgba(240,165,0,.22);' +
    'left:' + (r.left - p) + 'px;top:' + (r.top - p) + 'px;' +
    'width:' + (r.width + 2 * p) + 'px;height:' + (r.height + 2 * p) + 'px';
  document.body.appendChild(d);
  return 'ok';
})(%s)"""


def ring(selector: str) -> str:
    import json as _j
    return RING % _j.dumps(selector)


# A frame is defined by an element, not by numbers: "show the whole metadata
# card". Numbers would have to be re-tuned after every layout change, and the
# very first capture confirmed it: the crop cut off the field labels.
# Frame = element + margins around it. Margins differ: for a menu item the
# neighbours matter (otherwise it is unclear where to look), for a form card
# only the card itself. A tall narrow frame expands to full screen in the
# lesson, so the menu is taken not whole but around the needed item.
# The fourth element is "down to where": the dock panel spans the full window
# height, and a frame by its border came out as a column of emptiness below the
# relevant spot. The frame is cut at the bottom of the last meaningful element,
# and the top margin is enough for the tab row; otherwise the highlighted tab
# gets cut off.
CROP_OF = {
    "an-1-menu": ('a[href="/anonymizer"], .nav-item[href="/anonymizer"]', 14, 86),
    "an-2-drop": ("#anon-drop", 18, 18),
    "an-3-audit": ("#anon-audit", 14, 14),
    "an-4-columns": ("table.anon-cols", 14, 30),
    # A frame of a single button is a white rectangle about nothing (the owner
    # sent a screenshot of emptiness). Take the whole action row: next to it is
    # the "how many columns will be masked" counter, and together they explain
    # the step.
    "an-5-run": (".anon-actions:has(#btn-anon-run)", 26, 210,
                 ".anon-actions:has(#btn-anon-run)", 26),
    "an-6-result": ("#anon-result", 14, 14),
    "upd-1-data": (".dock-panel[data-tab='data']", 14, 38, "#btn-upload-slots", 14),
    "upd-2-versions": (".dock-panel[data-tab='versions']", 14, 38,
                       ".dock-panel[data-tab='versions'] .version-row:last-child", 14),
    "pres-1-present": (".present-menu", 26, 62, ".present-menu", 26),
    "pres-2-build": (".pres-add", 16, 34, ".pres-add", 16),
    "up-1-menu": ('a[href="/upload"], .nav-item[href="/upload"]', 14, 86),
    "up-2-drop": ("#drop-zone", 18, 18),
    "up-3-meta": (".upload-grid .card", 18, 18),
    "up-4-publish": (".upload-grid .card", 18, 18),
}


async def measure(s, selector: str):
    import json as _j
    box = await s.eval(
        "(function(sel){var e=document.querySelector(sel);if(!e)return '';"
        "var r=e.getBoundingClientRect();"
        "return JSON.stringify({x:r.left,y:r.top,w:r.width,h:r.height});})(%s)"
        % _j.dumps(selector))
    return _j.loads(box) if box else None


async def seed_sample(s) -> str:
    """Upload our own sample report to the stand and return its address.

    The data panel shots are taken on it not to save effort: for a report that
    reads a file the platform shows "Data slots", and for a report with data
    inside, a different section. The lesson must show what the learner will
    see, and they will see their own report, i.e. one like the sample."""
    import json as _j
    await s.navigate(ds.STAND + "/")
    await s.eval(
        "(async function(){var el=document.getElementById('dash-data');"
        "if(!el) return 0; var rows=(JSON.parse(el.textContent||'{}').items)||[];"
        "var n=0; for (var i=0;i<rows.length;i++){ if(rows[i].title!==%s) continue;"
        " await fetch('/api/dashboards/'+rows[i].id,{method:'DELETE'}); n++; }"
        "return n;})()" % _j.dumps(SAMPLE_TITLE), await_promise=True)
    html = open(SAMPLE, encoding="utf-8").read()
    rid = (await s.eval(
        "(async function(){var fd=new FormData();"
        "fd.append('title', %s);"
        "fd.append('description', 'Course sample report: reads the sales file');"
        "fd.append('html_file', new File([%s],'delta-report.html',{type:'text/html'}));"
        "var r=await fetch('/api/dashboards',{method:'POST',body:fd});"
        "var m=/\\/d\\/([0-9a-f-]{36})/.exec(r.url||'');"
        "return m ? m[1] : 'ERR '+r.status;})()"
        % (_j.dumps(SAMPLE_TITLE), _j.dumps(html)), await_promise=True) or "").strip().strip('"')
    if not rid or rid.startswith("ERR"):
        raise SystemExit("could not upload the sample: %s" % rid)
    await s.eval("fetch('/api/dashboards/'+%s,{method:'PATCH',"
                 "headers:{'Content-Type':'application/json'},"
                 "body:JSON.stringify({is_published:true})}).then(function(r){return r.status})"
                 % _j.dumps(rid), await_promise=True)
    # The second version is not for looks: while there is one version there is
    # nothing to restore in the panel, and the frame would show an empty list
    # where the lesson promises a "Restore" button. Upload the same file again.
    await s.eval(
        "(async function(){var fd=new FormData();"
        "fd.append('html_file', new File([%s],'delta-report.html',{type:'text/html'}));"
        "var r=await fetch('/api/dashboards/%s/version',{method:'POST',body:fd});"
        "return r.status;})()" % (_j.dumps(html), rid), await_promise=True)
    return "/d/" + rid


PRES_TITLE = "Lost sales: review for the planning meeting"


async def seed_presentation(s, dash_path: str) -> str:
    """Build a teaching presentation from our own report and return its address.

    The lesson promises that a slide can be a live report, so the slide in the
    screenshot must be a real report too, not an "empty for now" panel."""
    import json as _j
    pid = (await s.eval(
        "(async function(){var r=await fetch('/api/presentations',{method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({title:%s})});"
        "var j=await r.json(); return j.id||('ERR '+r.status);})()"
        % _j.dumps(PRES_TITLE), await_promise=True) or "").strip().strip('"')
    if not pid or pid.startswith("ERR"):
        raise SystemExit("could not create the presentation: %s" % pid)
    await s.eval(
        "(async function(){var r=await fetch('/api/presentations/%s/items',"
        "{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({kind:'dashboard',dashboard_id:%s})});"
        "return r.status;})()" % (pid, _j.dumps(dash_path.rsplit("/", 1)[-1])),
        await_promise=True)
    return "/presentations/" + pid


SHOTS = [
    ds.Shot("up-1-menu", "/?fox=off", wait=".sidebar, nav", height=760, settle=1.6,
            hide=["#op-companion"], blur=[".card iframe", ".preview iframe"],
            before=ring('a[href="/upload"], .nav-item[href="/upload"]'),
            note="step 1: \"Upload\" in the left menu"),
    ds.Shot("up-2-drop", "/upload", wait="#drop-zone", height=760, settle=1.2,
            before=ring("#drop-zone"),
            note="step 2: the file drop frame"),
    ds.Shot("up-3-meta", "/upload", wait="input[name='title']", height=820, settle=1.2,
            before=ring("input[name='title']"),
            note="step 4: title and the publish checkbox"),
    ds.Shot("up-4-publish", "/upload", wait="button[type='submit']", height=820,
            settle=1.2, before=ring("button[type='submit']"),
            note="step 6: the publish button"),
    # ── Anonymizer: six steps, one screen per step ──────────────
    ds.Shot("an-1-menu", "/anonymizer", wait=".nav-item", height=820, settle=1.0,
            before=ring('a[href="/anonymizer"], .nav-item[href="/anonymizer"]'),
            note="step 1: where the Anonymizer is"),
    ds.Shot("an-2-drop", "/anonymizer", wait="#anon-drop", height=760, settle=1.0,
            before=ring("#anon-drop"),
            note="step 2: where to put the file"),
    ds.Shot("an-3-audit", "/anonymizer", wait="#anon-drop", height=1100, settle=1.0,
            note="step 3: what else is in the workbook"),
    ds.Shot("an-4-columns", "/anonymizer", wait="#anon-drop", height=1200, settle=1.0,
            note="step 4: columns, checkboxes and what to replace with"),
    # The action row sits BELOW the long column table, i.e. below the window.
    # The frame is defined in window coordinates, so we first scroll it into
    # view and only then draw the ring and measure; otherwise the frame catches
    # emptiness two screens above (the owner sent exactly such a shot).
    ds.Shot("an-5-run", "/anonymizer", wait="#anon-drop", height=1200, settle=1.0,
            before="(function(){var a=document.querySelector('.anon-actions:has(#btn-anon-run)');"
                   "if(a)a.scrollIntoView({block:'center'});"
                   "var e=document.getElementById('btn-anon-run');if(!e)return 'missing';"
                   "var r=e.getBoundingClientRect(),p=8,d=document.createElement('div');"
                   "d.style.cssText='position:fixed;z-index:99999;pointer-events:none;"
                   "border:3px solid #f0a500;border-radius:10px;"
                   "box-shadow:0 0 0 4px rgba(240,165,0,.22);"
                   "left:'+(r.left-p)+'px;top:'+(r.top-p)+'px;"
                   "width:'+(r.width+2*p)+'px;height:'+(r.height+2*p)+'px';"
                   "document.body.appendChild(d);return 'ok';})()",
            note="step 5: the button that makes the file"),
    ds.Shot("an-6-result", "/anonymizer", wait="#anon-drop", height=1200, settle=1.2,
            note="step 6: the prompt sample and the self-check"),
    ds.Shot("upd-1-data", "{sample}?dock=data", wait=".dock-panel", height=900, settle=3.0,
            before=ring("#btn-upload-slots"),
            note="data update: the Data tab and the upload button"),
    # The action is highlighted, not the tab: since 13.09 uploading a new
    # version moved here from Settings, and the lesson must show exactly that.
    ds.Shot("upd-2-versions", "{sample}?dock=versions", wait=".dock-panel", height=900,
            settle=3.0, before=ring("#form-replace-html button[type='submit']"),
            note="report versions: upload a new one and roll back"),
    # The "Present" menu is closed by default, and a shot of a bare button
    # explains nothing. Open it with the same click the person would make.
    ds.Shot("pres-1-present", "{sample}", wait="#btn-present", height=820, settle=3.0,
            before="(function(){var b=document.getElementById('btn-present');"
                   "if(b) b.click();})()",
            note="presenting: the Present menu on the report itself"),
    ds.Shot("pres-2-build", "{pres}", wait=".pres-add", height=900, settle=2.2,
            before=ring("#pres-add-dash"),
            note="presenting: building a presentation from slides"),
    ds.Shot("up-5-done", "{report}", wait="iframe", height=820, settle=3.5,
            before="(function(){var r=document.getElementById('viewer-root');"
                   "if(r) r.setAttribute('data-dock','closed');})()",
            note="the result: the report at its own address"),
]


async def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    ds.OUT = OUT                       # the same grab(), another directory
    s = ChromeSession()
    s.start()
    await s.connect()
    try:
        await s.navigate(ds.STAND + "/login")
        await s.eval(
            "fetch('/dev-login',{method:'POST',headers:"
            "{'Content-Type':'application/x-www-form-urlencoded'},"
            "body:'person_id=%s'}).then(function(r){return r.status})" % ds.USER,
            await_promise=True)
        report = await ds.seed_report(s)
        sample = await seed_sample(s)
        pres = await seed_presentation(s, sample)
        print("demo report %s · sample %s · presentation %s" % (report, sample, pres))
        names = await ds.collect_names(s)
        print("real names masked: %d" % len(names))
        for shot in SHOTS:
            shot.path = (shot.path.replace("{report}", report)
                         .replace("{sample}", sample).replace("{pres}", pres))
            spec = CROP_OF.get(shot.name)
            if spec:
                sel, padx, pady = spec[0], spec[1], spec[2]
                until = spec[3] if len(spec) > 3 else None
                # The bottom margin is separate: the top needs room for the tab
                # row, while the same 38 px at the bottom became an empty strip
                # that made the frame taller and smaller on display (owner, 13.09).
                padb = spec[4] if len(spec) > 4 else pady
                # one visit to measure, a second for the frame: grab() navigates
                # to the page itself, so it cannot be measured in advance
                await s.send("Emulation.setDeviceMetricsOverride", {
                    "width": shot.width, "height": shot.height,
                    "deviceScaleFactor": ds.DPR, "mobile": False})
                await ds.asyncio.sleep(0)
                await s.navigate(ds.STAND + shot.path)
                await ds.asyncio.sleep(shot.settle)
                # The measurement happens before the frame and on the same page,
                # so whatever the step opens has to be opened here too: a closed
                # menu has zero size, and the frame came out as an empty square.
                if shot.name.startswith("an-") and shot.name not in ("an-1-menu", "an-2-drop"):
                    await feed_anon(s, run=shot.name == "an-6-result")
                if shot.before:
                    await s.eval(shot.before)
                    await ds.asyncio.sleep(0.3)
                box = await measure(s, sel)
                if box:
                    x = max(0, box["x"] - padx)
                    y = max(0, box["y"] - pady)
                    bottom = box["y"] + box["h"]
                    if until:
                        tail = await measure(s, until)
                        if tail:
                            bottom = tail["y"] + tail["h"]
                    shot.crop = (x, y,
                                 min(shot.width - x, box["w"] + 2 * padx),
                                 min(shot.height - y, bottom - y + padb))
            if shot.name.startswith("an-") and shot.name not in ("an-1-menu", "an-2-drop"):
                # grab() navigates to the page itself, so it has to be fed once
                # more, this time for the frame.
                await s.navigate(ds.STAND + shot.path)
                await ds.asyncio.sleep(shot.settle)
                await feed_anon(s, run=shot.name == "an-6-result")
                if shot.before:
                    await s.eval(shot.before)
                    await ds.asyncio.sleep(0.2)
                print(await ds.grab(s, shot, names, navigate=False))
            else:
                print(await ds.grab(s, shot, names))
    finally:
        await s.close()
        s.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
