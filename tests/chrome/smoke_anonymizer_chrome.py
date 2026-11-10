"""Real Anonymizer run in headless Chrome: the masked file must stay usable
for actual work.

This cannot be checked in a fake DOM: all the work happens in a worker on top
of SheetJS, and the question is about the CONTENT of the output workbook. So
this boots a real server, builds a trap workbook and reads the result back
with openpyxl.

What is pinned (each item is a breakage found by hand on 13.09):

  • the mask is ONE-TO-ONE: 60 distinct codes give 60 distinct replacements.
    Before the fix the replacement dictionary was finite and 60 outlets
    silently collapsed into 18 — the file looked intact, but analysis by that
    column was no longer possible;
  • one value is masked IDENTICALLY across the whole workbook: a code on the
    sales sheet and the same code on the warehouse sheet must match, or the two
    sheets can no longer be joined, and reconciling against shipments is an
    analyst's everyday job;
  • the code shape is preserved: a Cyrillic code like "ZR-01" stays two
    letters of the same alphabet, a dash, two digits;
  • numbers and dates are untouched — the masked file must give the same totals;
  • the workbook audit names what will travel with the file: a hidden sheet,
    a named range, an external link, a comment, the author in the properties;
  • the sample for the prompt is built from ALREADY masked rows and prints the
    date as dd.mm.yyyy (in the workbook it sits at UTC midnight — local getters
    shifted it a day back);
  • the self-check names the columns that were left with real values.

Skips cleanly (exit 0, SKIP line) when the Chrome binary is absent.

Run: .venv/bin/python tests/chrome/smoke_anonymizer_chrome.py
"""
import asyncio
import os
import shutil
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
SHOTS = os.path.join(SCRATCH, "chrome-anonymizer")
ADMIN = "admin1"

sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

from cdp import CHROME_BIN, ChromeSession                      # noqa: E402
from smoke_explore_chrome import HttpClient, check, free_port, FAILURES  # noqa: E402

# Cyrillic codes on purpose: the test pins that the mask keeps the code
# shape in the SAME alphabet (Cyrillic letter -> Cyrillic letter).
CODES = ["ЗР-%02d" % i for i in range(1, 61)]


def fixture(path: str) -> None:
    """Trap workbook: 60 codes on two sheets, PII, a hidden sheet, a link."""
    import openpyxl
    from openpyxl.comments import Comment
    from openpyxl.workbook.defined_name import DefinedName
    from datetime import date

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["date", "outlet", "client", "tax_id", "revenue"])
    for i, code in enumerate(CODES):
        ws.append([date(2026, 3, 3), code, "Company %d LLC" % i,
                   "77%08d" % i, 100 + i])
    st = wb.create_sheet("Warehouse")
    st.append(["outlet", "shipped"])
    for i, code in enumerate(CODES):
        st.append([code, 1000 + i])
    ws["G2"] = "='[C:\\exchange\\prices.xlsx]PriceList'!B7"   # external link
    ws["B2"].comment = Comment("check with the department", "Ivanov")
    hid = wb.create_sheet("RefCodes")
    hid.append(["code", "price"])
    hid.append([CODES[0], 120])
    hid.sheet_state = "hidden"
    wb.defined_names.add(DefinedName("PriceList", attr_text="RefCodes!$A$1:$B$2"))
    wb.properties.creator = "Maria Ivanova"
    wb.save(path)


def read(path: str):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    out = {}
    for name in wb.sheetnames:
        ws = wb[name]
        it = ws.iter_rows(values_only=True)
        head = list(next(it))
        out[name] = (head, list(it))
    return out


async def run(chrome, base, src, downloads):
    import json as _json
    import re

    await chrome.send("Page.setDownloadBehavior",
                      {"behavior": "allow", "downloadPath": downloads})
    await chrome.send("Network.enable", {})
    # The worker loads from a versioned URL, but in a test the cache only gets in the way.
    await chrome.send("Network.setCacheDisabled", {"cacheDisabled": True})
    await chrome.navigate(f"{base}/anonymizer", wait_ms=800)

    doc = await chrome.send("DOM.getDocument", {})
    root = (doc.get("result") or doc)["root"]["nodeId"]
    q = await chrome.send("DOM.querySelector",
                          {"nodeId": root, "selector": "#anon-file-input"})
    await chrome.send("DOM.setFileInputFiles",
                      {"files": [src], "nodeId": (q.get("result") or q)["nodeId"]})
    for _ in range(60):
        await asyncio.sleep(0.5)
        n = await chrome.eval("document.querySelectorAll('table.anon-cols tbody tr').length")
        if int(n or 0) > 1:
            break
    check("file parsed and columns shown", int(n or 0) > 1, str(n))

    audit = str(await chrome.eval(
        "var b=document.getElementById('anon-audit'); b && !b.hidden ? b.innerText : ''"))
    for what, needle in (("a hidden sheet", "RefCodes"),
                         ("a named range", "PriceList"),
                         ("an external link", "prices.xlsx"),
                         ("the workbook author", "Ivanova")):
        check("audit names " + what, needle in audit, audit[:200])
    await chrome.screenshot(os.path.join(SHOTS, "01_audit.png"))

    # Nothing is ticked by hand: the defaults are under test too. The tool must
    # pick high-uniqueness text columns and PII by itself and leave numbers and
    # dates alone — otherwise the masked file will not give the same totals.
    picked = str(await chrome.eval(
        "JSON.stringify([].filter.call(document.querySelectorAll('table.anon-cols tbody tr'),"
        "function(tr){return tr.querySelector('input.anon-mask').checked;})"
        ".map(function(tr){return tr.querySelectorAll('td')[1].innerText.trim();}))"))
    check("text columns and PII are ticked by default, numbers and dates are not",
          "outlet" in picked and "client" in picked and "tax_id" in picked
          and "revenue" not in picked and "date" not in picked, picked)
    await chrome.eval("document.getElementById('btn-anon-run').click()")
    got = None
    for _ in range(120):
        await asyncio.sleep(0.5)
        files = [f for f in os.listdir(downloads) if f.endswith(".xlsx") and "masked" in f]
        if files:
            got = os.path.join(downloads, files[0])
            break
    check("masked file saved", bool(got), str(os.listdir(downloads)))
    if not got:
        return
    await asyncio.sleep(0.8)

    sample = str(await chrome.eval(
        "(document.querySelector('.anon-sample')||{}).innerText||''"))
    check("prompt sample built", sample.count("\n") >= 3, sample[:120])
    check("sample date is dd.mm.yyyy, not shifted by a day",
          bool(re.search(r"^03\.03\.2026;", sample.split("\n")[1])), sample.split("\n")[1][:60])
    result = str(await chrome.eval("document.getElementById('anon-result').innerText"))
    check("self-check reported", "Self-check" in result, result[:120])
    await chrome.screenshot(os.path.join(SHOTS, "02_result.png"))

    # ── workbook content ────────────────────────────────────────────────
    src_data, out_data = read(src), read(got)
    h0, r0 = src_data["Sales"]
    h1, r1 = out_data["Sales"]
    check("rows and columns intact", len(r0) == len(r1) and h0 == h1,
          f"{len(r0)}/{len(r1)}")

    i = h0.index("outlet")
    pairs = {a[i]: b[i] for a, b in zip(r0, r1)}
    check("60 codes gave 60 DISTINCT replacements (no entities merged)",
          len(set(pairs.values())) == 60, str(len(set(pairs.values()))))
    check("code shape preserved: two letters, a dash, two digits",
          # Cyrillic range on purpose: the replacement keeps the code's alphabet.
          all(re.fullmatch(r"[А-ЯЁA-Z]{2}-\d{2}", v or "") for v in pairs.values()),
          str(sorted(pairs.values())[:3]))

    hs, rs = out_data["Warehouse"]
    j = hs.index("outlet")
    hs0, rs0 = src_data["Warehouse"]
    j0 = hs0.index("outlet")
    linked = all(pairs.get(a[j0]) == b[j] for a, b in zip(rs0, rs))
    check("the same code is masked identically on both sheets", linked)

    k = h0.index("revenue")
    check("numbers untouched", all(a[k] == b[k] for a, b in zip(r0, r1)))
    d = h0.index("date")
    check("dates untouched", all(a[d] == b[d] for a, b in zip(r0, r1)))
    n = h0.index("tax_id")
    check("tax ID replaced but still shaped like a tax ID",
          all(str(b[n]) != str(a[n]) and re.fullmatch(r"\d{10}", str(b[n]))
              for a, b in zip(r0, r1)),
          str([r1[0][n], r0[0][n]]))


async def main():
    if not CHROME_BIN:
        print("SKIP: Chrome binary not found")
        return
    try:
        import openpyxl                                        # noqa: F401
    except Exception:
        print("SKIP: openpyxl not installed")
        return
    os.makedirs(SHOTS, exist_ok=True)
    work = tempfile.mkdtemp(prefix="op-anon-")
    downloads = os.path.join(work, "dl")
    os.makedirs(downloads, exist_ok=True)
    src = os.path.join(work, "tricky.xlsx")
    fixture(src)

    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="op-anon-data-")
    env = dict(os.environ)
    env.update({
        "OP_DB_PATH": os.path.join(data_dir, "onepage.db"),
        "OP_UPLOADS_DIR": os.path.join(data_dir, "uploads"),
        "OP_PERF_DIR": os.path.join(data_dir, "perf"),
        "OP_DEV_BYPASS_AUTH": "true",
        "OP_INITIAL_ADMIN": ADMIN,
        "OP_CACHE_WARM": "0",
        "OP_TRUSTED_ORIGINS": f"http://127.0.0.1:{port},http://localhost:{port}",
    })
    server = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    chrome_dir = tempfile.mkdtemp(prefix="op-anon-profile-")
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
        check("server answers /health", up)
        if not up:
            print((server.stdout.read() if server.stdout else "")[-3000:])
            return
        HttpClient(port).post("/dev-login", data={"person_id": ADMIN})
        chrome.start()
        await chrome.connect()
        base = f"http://localhost:{port}"
        await chrome.navigate(f"{base}/dev-login", wait_ms=400)
        await chrome.eval(
            "(async()=>{const r=await fetch('/dev-login',{method:'POST',"
            "headers:{'Content-Type':'application/x-www-form-urlencoded'},"
            "body:'person_id=" + ADMIN + "'});return r.status;})()", await_promise=True)
        chrome.console_errors.clear()
        await run(chrome, base, src, downloads)
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
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(data_dir, ignore_errors=True)

    print("")
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}):")
        for f in FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
