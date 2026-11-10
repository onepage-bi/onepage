"""Real headless-Chrome run for P1: the "Chat about the report" pane PUSHES the report.

The claim the owner asked for cannot be checked in a fake DOM — it is a
geometry claim about a CSS grid. So this harness boots a real uvicorn on a
throwaway DB, seeds one generated report (server-side only — no Pyodide, no
folder indexing, so it runs in seconds and offline) and measures the page:

  • a plain /d/{id} has no chat iframe at all (the hot path);
  • opening the pane makes #dash-frame NARROWER — the report is pushed, not
    covered — and the pane is a grid column, not fixed chrome;
  • the grip resizes within 340…62vw, a double-click resets to 440;
  • "wider" is half the window, Escape closes and the focus lands on the FAB;
  • the geometry survives a reload, and the frame is still created only after
    the report has painted;
  • at 700px wide there is no width to give away: the pane becomes an overlay
    and the grip disappears;
  • present mode keeps the pane, and still does not cover the report.

Skips cleanly (exit 0, SKIP line) when the Chrome binary is absent.

Run: .venv/bin/python tests/chrome/smoke_viewer_chat_chrome.py
Screenshots: <scratch>/chrome-viewer-chat/
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PYTHON = os.environ.get("OP_TEST_PYTHON", sys.executable)
SCRATCH = os.environ.get(
    "OP_TEST_SCRATCH",
    os.path.join(tempfile.gettempdir(), "onepage-chrome-tests"))
SHOTS = os.path.join(SCRATCH, "chrome-viewer-chat")
ADMIN = "admin1"

sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

from cdp import CHROME_BIN, ChromeSession                     # noqa: E402
import seed_explore                                            # noqa: E402
from smoke_explore_chrome import HttpClient, check, free_port, FAILURES, wait_for  # noqa: E402


async def geometry(chrome):
    return await chrome.eval(
        "(function(){var r=document.getElementById('viewer-root');"
        "var p=document.getElementById('discuss-pane');"
        "var f=document.getElementById('dash-frame');"
        "return JSON.stringify({chat:r.getAttribute('data-chat'),"
        "avail:Math.round(r.getBoundingClientRect().width),"
        "dock:r.getAttribute('data-dock'),"
        "w:Math.round(p.getBoundingClientRect().width),"
        "report:Math.round(f.getBoundingClientRect().width),"
        "pos:getComputedStyle(p).position,"
        "grip:getComputedStyle(document.getElementById('discuss-grip')).display,"
        "hidden:p.hidden,frame:!!document.getElementById('discuss-frame')});})()")


async def drag_grip(chrome, dx):
    """Drag the grip dx pixels (negative = to the left = wider)."""
    await chrome.eval(
        "(function(){var g=document.getElementById('discuss-grip');"
        "var b=g.getBoundingClientRect();var x=b.left+2,y=b.top+b.height/2;"
        "function pe(t,cx){var e=new PointerEvent(t,{bubbles:true,cancelable:true,"
        "clientX:cx,clientY:y,pointerId:1,isPrimary:true});g.dispatchEvent(e);}"
        "pe('pointerdown',x);pe('pointermove',x+(%d));pe('pointerup',x+(%d));return 1;})()"
        % (int(dx), int(dx)))


async def run(chrome, base, dash_id):
    import json as _json
    # A fixed viewport: every width below is a number, not a guess. 1600 is
    # wide enough for 560 (report floor) + 440 (chat) + 360 (dock) at once.
    await chrome.send("Emulation.setDeviceMetricsOverride",
                      {"width": 1600, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    await chrome.navigate(f"{base}/d/{dash_id}", wait_ms=1200)
    # The dock is the chat's rival for the same width; close it so the numbers
    # below are about the chat alone. Its share is checked at the end.
    await chrome.eval("document.getElementById('dock-toggle').click()")
    await asyncio.sleep(0.5)
    g = _json.loads(await geometry(chrome))
    check("a plain /d/{id}: the pane is closed and there is NO chat iframe",
          g["chat"] == "closed" and g["hidden"] is True and g["frame"] is False, _json.dumps(g))
    report_full = g["report"]
    check("a plain /d/{id}: the report has the whole width", report_full > 600, str(report_full))

    await chrome.eval("document.getElementById('discuss-fab').click()")
    await wait_for(chrome, "document.getElementById('viewer-root')"
                           ".getAttribute('data-chat')==='open'?1:0", timeout=5)
    await asyncio.sleep(0.5)      # the .28s column transition
    g = _json.loads(await geometry(chrome))
    check("opening the pane PUSHES the report: #dash-frame gets narrower by the pane's width",
          abs((report_full - g["report"]) - 440) <= 4, f'{report_full} -> {g["report"]}')
    check("the pane is a grid column, not fixed chrome", g["pos"] == "relative", g["pos"])
    check("the default column is 440px", abs(g["w"] - 440) <= 2, str(g["w"]))
    check("the report keeps well over its 560px floor at 1600px",
          g["report"] >= 560, str(g["report"]))
    check("opening builds the /explore frame",
          "embed=1" in (await chrome.eval("(document.getElementById('discuss-frame')||{}).src||''")))
    await chrome.screenshot(os.path.join(SHOTS, "01_pane_open.png"))

    # ── the grip ──────────────────────────────────────────────────────
    await drag_grip(chrome, -120)
    await asyncio.sleep(0.2)
    w = _json.loads(await geometry(chrome))["w"]
    check("dragging the grip left widens the pane by the drag distance",
          abs(w - 560) <= 4, str(w))
    await drag_grip(chrome, 4000)
    await asyncio.sleep(0.2)
    w = _json.loads(await geometry(chrome))["w"]
    check("the grip cannot make the pane narrower than 340", abs(w - 340) <= 4, str(w))
    await chrome.eval(
        "document.getElementById('discuss-grip')"
        ".dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))")
    await asyncio.sleep(0.4)
    w = _json.loads(await geometry(chrome))["w"]
    check("a double-click on the grip resets the width to 440", abs(w - 440) <= 2, str(w))

    # ── "wider" ────────────────────────────────────────────────────────
    await chrome.eval("document.getElementById('discuss-wide').click()")
    await asyncio.sleep(0.45)

    w = _json.loads(await geometry(chrome))["w"]
    half = await chrome.eval(
        "Math.round(document.getElementById('viewer-root')"
        ".getBoundingClientRect().width*0.5)")
    check("'wider' takes exactly half the viewer", abs(w - half) <= 3, f"{w} vs {half}")
    await chrome.screenshot(os.path.join(SHOTS, "02_pane_wide.png"))
    await chrome.eval("document.getElementById('discuss-wide').click()")
    await asyncio.sleep(0.4)

    # ── Escape ────────────────────────────────────────────────────────
    await chrome.eval(
        "document.body.dispatchEvent(new KeyboardEvent('keydown',"
        "{key:'Escape',bubbles:true}))")
    await asyncio.sleep(0.4)
    g = _json.loads(await geometry(chrome))
    check("Escape closes the pane and gives the report its width back",
          g["chat"] == "closed" and g["report"] >= report_full - 2, _json.dumps(g))
    check("the focus lands on the launcher",
          (await chrome.eval("document.activeElement&&document.activeElement.id")) == "discuss-fab")
    check("closing KEEPS the frame (the built index survives close → open)",
          g["frame"] is True)

    # ── persistence + the hot path ────────────────────────────────────
    await chrome.eval("document.getElementById('discuss-fab').click()")
    await asyncio.sleep(0.4)
    await drag_grip(chrome, -80)
    await asyncio.sleep(0.3)
    stored = await chrome.eval(
        "localStorage.getItem('op-viewer-chat:'+DASHBOARD_ID)")
    check("the geometry is persisted as {open,w,wide}",
          '"open":true' in (stored or "") and '"w":520' in (stored or ""), str(stored))

    await chrome.navigate(f"{base}/d/{dash_id}", wait_ms=700)
    g = _json.loads(await geometry(chrome))
    stored2 = await chrome.eval("localStorage.getItem('op-viewer-chat:'+DASHBOARD_ID)")
    check("a reload restores the pane open, at the width it can have",
          g["chat"] == "open" and g["w"] >= 340, _json.dumps(g))
    check("the stored 520 survives the reload even when the re-opened dock "
          "leaves less room for it",
          '"w":520' in (stored2 or ""), str(stored2))
    check("the re-opened dock takes its share off the chat, not off the report",
          g["dock"] != "open" or g["w"] == g["avail"] - 360 - 560,
          _json.dumps(g))
    check("the frame is (re)created on a restore",
          bool(await wait_for(chrome, "document.getElementById('discuss-frame')?1:0", timeout=20)))
    order = await chrome.eval(
        "(function(){var r=performance.getEntriesByType('resource');"
        "var ex=r.filter(function(e){return e.name.indexOf('/explore?')>=0;})[0];"
        "var rep=r.filter(function(e){return e.name.indexOf('/render/')>=0;})[0];"
        "if(!ex||!rep)return -1;return ex.startTime>=rep.responseEnd?1:0;})()")
    check("hot path: the /explore frame is requested only AFTER the report's own "
          "document is in — a report load costs no chat request",
          order == 1, str(order))

    # ── the dock is the chat's rival for the width ────────────────────
    # The reload above left the dock open, so this is the constrained case.
    await chrome.eval("document.getElementById('discuss-wide').click()")
    await asyncio.sleep(0.5)
    with_dock = _json.loads(await geometry(chrome))
    check("with the dock open, 'wider' is capped by the report's 560px floor",
          with_dock["dock"] == "open" and with_dock["w"] == with_dock["avail"] - 360 - 560,
          _json.dumps(with_dock))
    await chrome.eval("document.getElementById('dock-toggle').click()")
    await asyncio.sleep(0.9)
    no_dock = _json.loads(await geometry(chrome))
    check("closing the dock really hands its width to the chat",
          no_dock["dock"] == "closed" and no_dock["w"] > with_dock["w"],
          _json.dumps(no_dock))
    check("… and the report never drops under its floor either way",
          with_dock["report"] >= 526 and no_dock["report"] >= 526,
          f'{with_dock["report"]} / {no_dock["report"]}')
    await chrome.eval("document.getElementById('discuss-wide').click()")
    await asyncio.sleep(0.45)

    # ── narrow window: an overlay, no grip ────────────────────────────
    await chrome.send("Emulation.setDeviceMetricsOverride",
                      {"width": 700, "height": 800, "deviceScaleFactor": 1, "mobile": False})
    await asyncio.sleep(0.6)
    open_g = _json.loads(await geometry(chrome))
    check("under 760px the pane becomes an overlay", open_g["pos"] == "absolute", open_g["pos"])
    check("… covering the whole viewer, not a slice of it",
          abs(open_g["w"] - open_g["avail"]) <= 2, _json.dumps(open_g))
    check("… the grip is gone (there is nothing left to give away)",
          open_g["grip"] == "none", open_g["grip"])
    await chrome.eval("document.getElementById('discuss-fab').click()")
    await asyncio.sleep(0.5)
    closed_g = _json.loads(await geometry(chrome))
    check("… and it covers the report instead of squeezing it: the report is "
          "the same width open or closed",
          abs(open_g["report"] - closed_g["report"]) <= 2,
          f'{open_g["report"]} vs {closed_g["report"]}')
    await chrome.eval("document.getElementById('discuss-fab').click()")
    await asyncio.sleep(0.4)
    await chrome.screenshot(os.path.join(SHOTS, "03_pane_overlay.png"))
    await chrome.send("Emulation.setDeviceMetricsOverride",
                      {"width": 1600, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    await asyncio.sleep(0.5)

    # ── present mode ──────────────────────────────────────────────────
    await chrome.eval("document.body.classList.add('op-present')")
    await asyncio.sleep(0.4)
    pres = _json.loads(await chrome.eval(
        "(function(){var p=document.getElementById('discuss-pane');"
        "var m=document.querySelector('.v-main');"
        "return JSON.stringify({vis:getComputedStyle(p).display!=='none',"
        "w:Math.round(p.getBoundingClientRect().width),"
        "right:Math.round(innerWidth-m.getBoundingClientRect().right)});})()"))
    check("present mode keeps the pane visible (questions get asked mid-show)",
          pres["vis"] is True and pres["w"] > 300, _json.dumps(pres))
    check("… and the report's fixed layer stops where the pane starts",
          abs(pres["right"] - pres["w"]) <= 2, _json.dumps(pres))
    await chrome.screenshot(os.path.join(SHOTS, "04_present.png"))
    await chrome.eval("document.body.classList.remove('op-present')")

    errs = [e for e in chrome.console_errors if e]
    check("zero console errors across the pane session", not errs, " | ".join(errs[:5]))


async def main():
    os.makedirs(SHOTS, exist_ok=True)
    if not os.path.exists(CHROME_BIN):
        print(f"SKIP chrome binary not found at {CHROME_BIN} — viewer chat e2e not run")
        return
    tmp = tempfile.mkdtemp(prefix="op-viewer-chat-")
    port = free_port()
    env = os.environ.copy()
    env.update({
        "OP_DB_PATH": f"{tmp}/onepage.db",
        "OP_UPLOADS_DIR": f"{tmp}/uploads",
        "OP_PERF_DIR": f"{tmp}/perflog",
        "OP_DEV_BYPASS_AUTH": "true",
        "OP_INITIAL_ADMIN": ADMIN,
        "OP_CACHE_WARM": "0",
        "OP_ACCESS_LOG": "false",
        "OP_EXPLORE_ENABLED": "true",
        "OP_SAML_SP_BASE_URL": f"http://localhost:{port}",
    })
    server = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    chrome_dir = tempfile.mkdtemp(prefix="op-viewer-chat-profile-")
    chrome = ChromeSession(port=free_port(), user_data_dir=chrome_dir)
    try:
        up = False
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                    if r.status == 200:
                        up = True
                        break
            except Exception:
                time.sleep(0.3)
        check("server: /health answers 200", up)
        if not up:
            print((server.stdout.read() if server.stdout else "")[-4000:])
            return
        c = HttpClient(port)
        c.post("/dev-login", data={"person_id": ADMIN})
        info = seed_explore.seed(c, confirm=False)
        dash_id = info["dashboard"]
        check("seed: one generated report exists", bool(dash_id), str(dash_id))

        chrome.start()
        await chrome.connect()
        base = f"http://localhost:{port}"
        await chrome.navigate(f"{base}/dev-login", wait_ms=400)
        r = await chrome.eval(
            "(async()=>{const r=await fetch('/dev-login',{method:'POST',"
            "headers:{'Content-Type':'application/x-www-form-urlencoded'},"
            "body:'person_id=" + ADMIN + "'});return r.status;})()", await_promise=True)
        check("chrome: dev-login POST from the page succeeded", r in (200, 302, 303), str(r))
        chrome.console_errors.clear()
        await run(chrome, base, dash_id)
    finally:
        try:
            await chrome.close()
        except Exception:
            pass
        chrome.stop()
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()

    print("")
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}):")
        for f in FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
