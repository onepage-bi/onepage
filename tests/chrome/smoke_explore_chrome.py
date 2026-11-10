"""Real headless-Chrome end-to-end run for Leg 3 (/explore) — BOTH engines.

Boots a real uvicorn on a throwaway DB, seeds a legacy generator plus two
runs whose lineage observations describe the fixture folder, rebuilds and
confirms the catalog, then drives Chrome through the whole /explore flow:
index the folder in the browser (no stubs anywhere), run a builder query, run
two recipes, export to Excel, save a question and find it after a reload, and
land an NL->SQL answer in the editor without running it.

That flow runs TWICE, against the two engines the page can boot:

  ?engine=1  gen-worker.js — real Pyodide, python-calamine, pandas, DuckDB
  ?engine=2  static/js/explore2/worker.js — the Rust xlsx parser to Arrow and
             two duckdb-wasm instances (a locked query one, an IO one that owns
             the Parquet cache), no Python at all

Every check is shared and labelled `[e1]`/`[e2]`; engine 2 adds five of its own,
for the things engine 1 has no equivalent of — that the v2 worker really
answered, that its query instance is locked, that the second index came out of
Parquet in OPFS, that the ported `sem` views answer a query directly, and that
the engine accounts for its own memory.

Cutting across both engines are THREE provider variants over the same flow
(docs/LLM_PROVIDERS.md), each still running both engines in full:

    (default)                     server → Ollama            `/api/generate`
    --provider deepseek           server → DeepSeek          `/chat/completions`
    --browser                     the PAGE → DeepSeek        prepare/validate on the server

All three point at the same stub; the last one proves the production shape,
where the server has no egress and the browser does.

The FSA folder picker opens a native OS dialog no automation can drive, so
the page carries a documented TEST-ONLY hook (`window.__opExploreTestFiles`,
gated on localhost) that this harness fills with the fixture bytes.

Skips cleanly (exit 0, SKIP lines) when the Chrome binary is absent.

Run: .venv/bin/python tests/chrome/smoke_explore_chrome.py
     .venv/bin/python tests/chrome/smoke_explore_chrome.py --provider deepseek
     .venv/bin/python tests/chrome/smoke_explore_chrome.py --browser
Screenshots: SHOTS (below).
"""
import asyncio
import base64
import glob
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PYTHON = os.environ.get("OP_TEST_PYTHON", sys.executable)
SCRATCH = os.environ.get(
    "OP_TEST_SCRATCH",
    os.path.join(tempfile.gettempdir(), "onepage-chrome-tests"))
SHOTS = os.path.join(SCRATCH, "chrome-explore")
ADMIN = "admin1"

sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

from cdp import CHROME_BIN, ChromeSession           # noqa: E402
import make_explore_fixture                          # noqa: E402
import seed_explore                                  # noqa: E402

FAILURES = []
RESULTS = []

# The stub Ollama always answers with this. It has to be a real SELECT over a
# real table, because the server refuses anything that is not one (422) — that
# refusal is itself part of the contract being exercised. "склад" is the
# fixture's own Cyrillic column header (see make_explore_fixture.py).
STUB_SQL = 'SELECT "склад", COUNT(*) AS n FROM "datasalesorder" GROUP BY "склад"'
STUB_INTENT = json.dumps({"kpi": "realised_share", "dims": ["sklad"], "filters": [],
                          "order": {"by": "value", "dir": "desc"}, "limit": 10,
                          "explanation": "share of realised orders by warehouse"}, ensure_ascii=False)
# Tier 2: the model writes SQL over the report's own semantic views. `lines`
# is the fixture generator's declared view (seed_explore.SEMANTICS), so this
# only runs if the `sem` schema really stands on the indexed partitions.
STUB_SEMANTIC_SQL = ('SELECT "sklad", COUNT(*) AS "n_rows" FROM "lines" '
                     'GROUP BY "sklad" ORDER BY 2 DESC LIMIT 100')


# Fragments of the MODEL PROMPTS built server-side in app/services/nl2sql.py.
# Those prompts are Russian in the app itself (they are not interface text),
# so the stub and the pins below match them verbatim; update both together.
P_VERIFY = "Ты — контролёр"              # build_verify_prompt opening line
P_SEM_SCHEMA = "Схема отчёта:"           # build_semantic_sql_prompt header
P_FIXED_KPI = "Метрика уже выбрана"      # _build_fixed_kpi_prompt
P_SCHEMA_HINT = "отвечай строго JSON по схеме:"   # _SCHEMA_HINT (DeepSeek)

# The questions this harness asks are Russian on purpose: the form matcher,
# period grounding and follow-up matcher are exercised on Russian input.
Q_SHARE = "какая доля реализованных заказов по складам"
# The fixture KPIs' own names (seed_explore.SEMANTICS), Russian there on purpose.
KPI_SHARE = seed_explore.SEMANTICS["kpis"]["realised_share"]["name"]
KPI_AMOUNT = seed_explore.SEMANTICS["kpis"]["order_amount"]["name"]


def no_error(text):
    """No engine/query error in a status line ('Error: …', 'Engine error')."""
    return "error" not in (text or "").lower()


def check(label, cond, extra=""):
    line = ("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else "")
    print(line, flush=True)
    RESULTS.append(line)
    if not cond:
        FAILURES.append(label)


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── a requests-ish shim so seed_explore.py can drive the live server ───────
class Resp:
    def __init__(self, status, raw):
        self.status_code = status
        self._raw = raw

    @property
    def text(self):
        return self._raw.decode("utf-8", "replace")

    def json(self):
        return json.loads(self._raw)


class HttpClient:
    """Just enough of TestClient for seed_explore: .post/.get with json= or
    files=, carrying the session cookie."""

    def __init__(self, port):
        self.port = port
        self.cookie = None

    def _req(self, method, path, body=None, headers=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = dict(headers or {})
        if self.cookie:
            h["Cookie"] = self.cookie
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        raw = r.read()
        sc = r.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0]
        conn.close()
        return Resp(r.status, raw)

    def get(self, path):
        return self._req("GET", path)

    def put(self, path, json=None):
        import json as _json
        return self._req("PUT", path, _json.dumps(json or {}).encode(),
                         {"Content-Type": "application/json"})

    def post(self, path, json=None, data=None, files=None, follow_redirects=True):
        import json as _json
        if files is not None:
            boundary = "----opsmoke"
            parts = []
            for field, (fname, content, ctype) in files.items():
                parts.append(
                    (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                     f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n").encode() + content + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            return self._req("POST", path, b"".join(parts),
                             {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        if json is not None:
            return self._req("POST", path, _json.dumps(json).encode(),
                             {"Content-Type": "application/json"})
        if data is not None:
            body = "&".join(f"{k}={v}" for k, v in data.items()).encode()
            return self._req("POST", path, body,
                             {"Content-Type": "application/x-www-form-urlencoded"})
        return self._req("POST", path, b"", {"Content-Length": "0"})


# ── the tiny model stub OP_LLM_ENDPOINT points at ─────────────────────────
# Speaks BOTH shapes on purpose, so one stub covers all three variants of
# this harness: Ollama's `/api/generate` (default run), and DeepSeek's
# OpenAI-compatible `/chat/completions` — called either by the server
# (`--provider deepseek`) or, in `--browser` mode, by the PAGE itself, which
# is why it also answers a CORS preflight.
class _Stub(BaseHTTPRequestHandler):
    prompts = []
    paths = []
    # Set by the tier-2 case: the model declines the metric form («not one of
    # these metrics»), which is exactly what routes the question to tier 2.
    intent_answer = None

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Access-Control-Allow-Headers", "authorization,content-type")
        self.send_header("Access-Control-Allow-Methods", "POST,OPTIONS")

    def do_OPTIONS(self):
        _Stub.paths.append("OPTIONS " + self.path)
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        _Stub.paths.append(self.path)
        chat = self.path.rstrip("/").endswith("/chat/completions")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        if chat:
            msgs = payload.get("messages") or []
            _Stub.prompts.append(next((m.get("content", "") for m in reversed(msgs)
                                       if m.get("role") == "user"), ""))
        else:
            _Stub.prompts.append(payload.get("prompt", ""))
        # Three prompt shapes, told apart by what they end with and what they
        # describe: the intent form (ADR-13) ends with "JSON:"; tier 2's
        # semantic-SQL prompt ends with "SQL:" and opens with the report-schema
        # header (P_SEM_SCHEMA); the bare nl2sql prompt ends with "SQL:" over
        # the raw catalogue schema.
        text = _Stub.prompts[-1]
        is_intent = "JSON:" in text and "SQL:" not in text.rsplit("JSON:", 1)[-1]
        if text.startswith(P_VERIFY):
            # ADR-15: the advisory verify pass — the stub always approves,
            # so every flow below still ends in an answer.
            answer = '{"ok": true}'
        elif is_intent:
            answer = _Stub.intent_answer or STUB_INTENT
        elif P_SEM_SCHEMA in text:
            answer = STUB_SEMANTIC_SQL
        else:
            answer = STUB_SQL
        if chat:
            body = json.dumps({"id": "stub", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": answer},
                 "finish_reason": "stop"}]}).encode()
        else:
            body = json.dumps({"model": "stub", "response": answer, "done": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_stub():
    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def parse_args():
    """Three variants over ONE flow (docs/LLM_PROVIDERS.md), see module
    docstring. All three run both engines."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="ollama", choices=["ollama", "deepseek"])
    ap.add_argument("--browser", action="store_true",
                    help="OP_LLM_BROWSER=true: the page calls the provider itself")
    return ap.parse_args()


# ── page helpers ──────────────────────────────────────────────────────────
async def wait_for(chrome, expr, timeout=180, every=0.5):
    """Poll a JS expression until it is truthy. Booting Pyodide + DuckDB +
    pandas from a cold cache is slow enough that a fixed sleep either flakes
    or wastes a minute on every run."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = await chrome.eval(expr)
        if last:
            return last
        await asyncio.sleep(every)
    return last


async def clear_grid(chrome):
    await chrome.eval(
        "(function(){document.getElementById('ex-sql').value='';"
        "document.getElementById('ex-result-tbody').textContent='';"
        "document.getElementById('ex-result-thead').textContent='';"
        "document.getElementById('ex-nl-status').textContent='';})()")


async def ask_in_page(chrome, question, timeout=180):
    """Type a question, press "Ask", wait for the status line to settle,
    then report what the PAGE ended up with — the SQL, the status and, above
    all, how many rows the grid holds. One-shot means those rows are there
    with no further click, so counting them IS the assertion."""
    await chrome.eval(
        "(function(){document.getElementById('ex-nl').value=" + json.dumps(question) + ";"
        "document.getElementById('ex-nl-status').textContent='';"
        "document.getElementById('ex-nl-btn').click();})()")
    await wait_for(chrome,
                   "(function(){const s=document.getElementById('ex-nl-status').textContent;"
                   "const q=document.getElementById('ex-sql-status').textContent;"
                   "return (s && !/^Asking/.test(s) && q!=='Running…') ? s : '';})()",
                   timeout=timeout)
    return await chrome.eval(
        "(function(){return {sql: document.getElementById('ex-sql').value,"
        "status: document.getElementById('ex-nl-status').textContent,"
        "sqlStatus: document.getElementById('ex-sql-status').textContent,"
        "rows: document.querySelectorAll('#ex-result-tbody tr').length,"
        "cols: [...document.querySelectorAll('#ex-result-thead th')].map(th=>th.textContent),"
        "badge: document.getElementById('ex-result-badge').textContent};})()")


async def run_sql_in_page(chrome, sql, timeout=120):
    await chrome.eval(
        "(function(){const t=document.getElementById('ex-sql');"
        f"t.value={json.dumps(sql)};"
        "document.getElementById('ex-result-tbody').textContent='';"
        "document.getElementById('ex-run-btn').click();})()")
    await wait_for(chrome,
                   "(function(){const s=document.getElementById('ex-sql-status').textContent;"
                   "return s!=='Running…';})()", timeout=timeout)
    return await chrome.eval(
        "(function(){return {"
        "rows: [...document.querySelectorAll('#ex-result-tbody tr')]"
        ".map(tr=>[...tr.children].map(td=>td.textContent)),"
        "cols: [...document.querySelectorAll('#ex-result-thead th')].map(th=>th.textContent),"
        "badge: document.getElementById('ex-result-badge').textContent,"
        "status: document.getElementById('ex-sql-status').textContent};})()")


async def flow(chrome, base, engine, names, fixture, downloads, c, provider, browser):
    """The whole /explore session, once, against ONE engine.

    Everything here is written against the PAGE — ids, buttons, rendered text —
    never against an engine's internals, which is exactly the claim being
    tested: `explore.js` picks a worker URL and nothing else changes. The four
    engine-2-only checks are marked as such; they assert things engine 1 has no
    equivalent of (a second duckdb-wasm instance owning a Parquet cache, its
    own memory accounting).
    """
    def ck(label, cond, extra=""):
        check(f"[e{engine}] {label}", cond, extra)

    def shot(name):
        return os.path.join(SHOTS, f"e{engine}_{name}")

    url = f"{base}/explore?engine={engine}"
    saved_title = f"Order changes (v{engine})"

    # Its OWN download directory. Both engines name the export `explore.xlsx`
    # and headless Chrome overwrites rather than uniquifies, so a shared folder
    # makes the second run's file invisible to a "what is new here?" poll.
    downloads = os.path.join(downloads, f"e{engine}")
    os.makedirs(downloads, exist_ok=True)
    try:
        await chrome.send("Browser.setDownloadBehavior",
                          {"behavior": "allow", "downloadPath": downloads})
    except Exception:
        await chrome.send("Page.setDownloadBehavior",
                          {"behavior": "allow", "downloadPath": downloads})

    chrome.console_errors.clear()
    await chrome.navigate(url, wait_ms=1200)
    ck("page: /explore renders #ex-root", await chrome.eval("!!document.getElementById('ex-root')") is True)
    n_tables = await chrome.eval(
        "JSON.parse(document.getElementById('ex-root').dataset.catalog).tables.length")
    ck("page: the catalog snapshot is baked into the page", n_tables == 3, str(n_tables))
    opts = await chrome.eval(
        "[...document.getElementById('ex-b-table').options].map(o=>o.textContent)")
    ck("page: the table picker lists all three tables",
       isinstance(opts, list) and len(opts) == 4, str(opts))
    ck("page: 'Pin as tile' is present but disabled (out of scope this phase)",
       await chrome.eval("document.getElementById('ex-pin-btn').disabled") is True)
    await chrome.screenshot(shot("01_explore_loaded.png"))

    # ── connect + index through the test-only hook ─────────────────────
    payload = []
    for n in names:
        with open(os.path.join(fixture, n), "rb") as fh:
            payload.append({"name": n, "b64": base64.b64encode(fh.read()).decode(),
                            "mtime_ms": int(os.path.getmtime(os.path.join(fixture, n)) * 1000)})
    await chrome.eval(
        "(function(){const raw=" + json.dumps(payload) + ";"
        "window.__opExploreTestFiles = raw.map(f=>{"
        "const bin=atob(f.b64); const u8=new Uint8Array(bin.length);"
        "for(let i=0;i<bin.length;i++) u8[i]=bin.charCodeAt(i);"
        "return {name:f.name, buf:u8.buffer, mtime_ms:f.mtime_ms};});"
        "return window.__opExploreTestFiles.length;})()")
    await chrome.click("#ex-connect-btn")
    await asyncio.sleep(0.3)
    ck("connect: the folder reads as connected",
       await chrome.eval("document.getElementById('ex-index-btn').disabled") is False)

    t_index = time.time()
    await chrome.click("#ex-index-btn")
    summary = await wait_for(
        chrome,
        "(function(){const s=document.getElementById('ex-summary').textContent;"
        "return /\\btables?\\b/.test(s) ? s : '';})()", timeout=300)
    index_s = time.time() - t_index
    ck("index: header shows 'N tables · M partitions · data as of <date>'",
       bool(summary) and "3 tables" in summary and "5 partitions" in summary
       and "data as of" in summary, str(summary))
    await chrome.screenshot(shot("02_indexed.png"))

    # Table picker after indexing: the connected folder's tables come
    # first. Here the fixture folder backs ALL three catalogue tables, so
    # the "Other catalogue tables" group must be absent, not empty — the
    # grouped case is pinned in tests/explore_dom.js.
    picker = await chrome.eval(
        "(function(){const s=document.getElementById('ex-b-table');return {"
        "groups:[...s.children].filter(c=>c.tagName==='OPTGROUP').map(g=>g.label),"
        "top:[...s.children].filter(c=>c.tagName==='OPTION').map(o=>o.textContent),"
        "all:[...s.options].length};})()")
    ck("picker: every table of the connected folder sits at the top level",
       (picker or {}).get("groups") == [] and len((picker or {}).get("top") or []) == 4,
       json.dumps(picker, ensure_ascii=False))
    told = await chrome.eval("window.__opExploreIndexed || null") or {}
    print(f"      [e{engine}] first index {index_s:.1f}s  "
          f"stats={json.dumps(told.get('stats') or {}, ensure_ascii=False)}  "
          f"cache={json.dumps(told.get('cache') or {}, ensure_ascii=False)}  "
          f"timings={json.dumps(told.get('timings') or {}, ensure_ascii=False)}", flush=True)

    if engine == 2:
        # ENGINE-2 ONLY: prove the v2 worker is the one that answered, not the
        # Pyodide one behind a flag that silently did nothing.
        ck("v2: the engine-2 worker answered (stats.engine === 2)",
           (told.get("stats") or {}).get("engine") == 2, json.dumps(told.get("stats")))
        ck("v2: the query instance really is locked (enable_external_access=false)",
           (told.get("stats") or {}).get("lockdown") is True, json.dumps(told.get("stats")))
        # ENGINE-2 ONLY: memory. v1 reports Pyodide's wasm heap; v2 reports
        # DuckDB's own accounting, which is the honest "how close to the tab
        # ceiling" number for an engine with no CPython in it.
        peak = told.get("peakWasmBytes") or 0
        mem = (told.get("stats") or {}).get("memoryUsage")
        ck("v2: peak wasm/DuckDB memory is reported, not guessed",
           isinstance(peak, (int, float)) and peak > 0 and bool(mem),
           f"peak={peak} memoryUsage={mem} limit={(told.get('stats') or {}).get('memoryLimit')}")
        print(f"      [e2] duckdb memory: peak {peak} bytes, "
              f"in use {mem} of {(told.get('stats') or {}).get('memoryLimit')}", flush=True)

    # ── second index: every partition must come from the Parquet cache in
    #    OPFS — no workbook is read again ───────────────────────────────
    t_cache = time.time()
    await chrome.click("#ex-index-btn")
    summary2 = await wait_for(
        chrome,
        "(function(){const s=document.getElementById('ex-summary').textContent;"
        "return /from cache/.test(s) || /not read/.test(s) ? s : '';})()", timeout=120)
    cache_s = time.time() - t_cache
    ck("cache: the second index is served from the Parquet cache ('from cache: 5')",
       "from cache: 5" in (summary2 or ""), str(summary2))
    ck("cache: and it is fast (< 30 s for 5 partitions)", cache_s < 30, f"{cache_s:.1f}s")
    summary = summary2 or summary
    if engine == 2:
        # ENGINE-2 ONLY: WHERE that Parquet lives. `opfs` = DuckDB wrote
        # straight into origin-private storage through registerOPFSFileName;
        # `opfs-js` = OPFS is there but DuckDB would not drive it, so the bytes
        # went through a buffer. Either survives the tab; `memory`/`off` does
        # not, and a cache that silently forgets is the thing to catch here.
        told2 = await chrome.eval("window.__opExploreIndexed || null") or {}
        mode = ((told2.get("cache") or {}).get("mode"))
        ck("v2: the Parquet cache is backed by OPFS, not by worker memory",
           mode in ("opfs", "opfs-js"),
           f"mode={mode} err={(told2.get('cache') or {}).get('error')}")
        print(f"      [e2] parquet cache: mode={mode} "
              f"files={(told2.get('cache') or {}).get('files')} "
              f"bytes={(told2.get('cache') or {}).get('bytes')} "
              f"second index {cache_s:.1f}s", flush=True)

    # ── phase 9.2: the cache is PORTABLE — export one partition's Parquet
    #    and import it back under a NEW key, in the real engine ──────────
    # This is the whole point of 9.2: the bytes one user's browser parsed can
    # be handed to another user's browser, which then never opens the source
    # workbooks. Here the round trip happens in one tab (export → import under
    # a key nothing was ever parsed under), which exercises exactly the code
    # the server manifest will drive in 9.4 — including the engine's own
    # validation of bytes it did not produce.
    port = await chrome.eval(
        "(async function(){try{"
        "const api=window.OP_EXPLORE, st=window.__opExploreState;"
        "const keys=((st&&st.cacheKeys)||[]).slice(0,1);"
        "if(!keys.length) return {err:'no cache keys on the state'};"
        "const exp=await api.cacheExport(keys);"
        "const p=((exp&&exp.parts)||[])[0];"
        "if(!p) return {err:'no parts', got:JSON.stringify(exp).slice(0,200)};"
        "const n=p.buf.byteLength;"
        "const head=new TextDecoder().decode(new Uint8Array(p.buf.slice(0,4)));"
        "const imp=await api.cacheImport('a'.repeat(40), p.buf);"
        "const bad=await api.cacheImport('b'.repeat(40), new Uint8Array([1,2,3,4,5]).buffer);"
        "return {n:n, head:head, key:keys[0], pv:st.parserVersion, imp:imp, bad:bad};"
        "}catch(e){return {err:String(e&&e.message||e)};}})()", await_promise=True) or {}
    ck("portable cache: the index left its partition keys on the page state",
       not port.get("err"), json.dumps(port, ensure_ascii=False)[:300])
    ck("portable cache: the probe reported the engine's parser version",
       bool(port.get("pv")), str(port.get("pv")))
    ck("portable cache: cache_export hands back real Parquet bytes",
       port.get("head") == "PAR1" and (port.get("n") or 0) > 0,
       f"head={port.get('head')} bytes={port.get('n')}")
    ck("portable cache: cache_import accepts them under a NEW key",
       (port.get("imp") or {}).get("ok") is True
       and (port.get("imp") or {}).get("bytes") == port.get("n"),
       json.dumps(port.get("imp"), ensure_ascii=False))
    ck("portable cache: garbage bytes are refused, not cached",
       (port.get("bad") or {}).get("ok") is False and (port.get("bad") or {}).get("error"),
       json.dumps(port.get("bad"), ensure_ascii=False))
    print(f"      [e{engine}] portable cache: {port.get('n')} bytes exported for "
          f"{str(port.get('key'))[:12]}…, parser v{port.get('pv')}", flush=True)

    # ── phase 9.4 (publish): this browser's partitions become the report's
    #    SERVER index — the three calls a generator run makes at the end ──
    # Everything here goes through the REAL /lake API from the page, with the
    # page's own session: export the keys the index left on the state, POST
    # each partition as FormData, then PUT the manifest that names them. The
    # load half runs at the end of this flow, after a reload.
    pub = await chrome.eval(
        "(async function(){try{"
        "const api=window.OP_EXPLORE, st=window.__opExploreState;"
        "const card=document.querySelector('#ex-report-strip .ex-report-card');"
        "if(!card) return {err:'no report card on the strip'};"
        "const dash=card.getAttribute('data-dashboard-id');"
        "const keys=((st&&st.cacheKeys)||[]).slice();"
        "if(!keys.length) return {err:'no cache keys on the state'};"
        "const DATA=JSON.parse(document.getElementById('ex-root').dataset.catalog);"
        "const m=api.matchFilesToCatalog(st.lastFiles, DATA.tables).matched;"
        "const exp=await api.cacheExport(keys);"
        "const byKey={}; ((exp&&exp.parts)||[]).forEach(function(p){byKey[p.key]=p.buf;});"
        "const files=[]; let up=0, bytes=0;"
        "for(let i=0;i<m.length;i++){"
        "  const k=keys[i], buf=byKey[k];"
        "  if(!buf) continue;"
        "  const fd=new FormData();"
        "  fd.append('key', k);"
        "  fd.append('file', new Blob([buf],{type:'application/octet-stream'}), k+'.parquet');"
        "  const r=await fetch('/api/dashboards/'+dash+'/lake/parts',"
        "    {method:'POST', body:fd, credentials:'same-origin'});"
        "  if(!r.ok) return {err:'POST part -> '+r.status};"
        "  up++; bytes+=buf.byteLength;"
        "  const t=(DATA.tables||[]).find(function(x){return x.table_id===m[i].table_id;});"
        "  files.push({name:m[i].name, size:m[i].size, mtime_ms:m[i].mtime_ms,"
        "    sheet:m[i].sheet, header_row:m[i].header_row||0, key:k,"
        "    table:(t&&t.name)||'', partition:m[i].partition, bytes:buf.byteLength});"
        "}"
        "const r2=await fetch('/api/dashboards/'+dash+'/lake/manifest',"
        "  {method:'PUT', headers:{'Content-Type':'application/json'}, credentials:'same-origin',"
        "   body:JSON.stringify({run_id:1, parser_version:st.parserVersion, files:files})});"
        "if(!r2.ok) return {err:'PUT manifest -> '+r2.status, body:(await r2.text()).slice(0,200)};"
        "const man=await r2.json();"
        "return {dash:dash, uploaded:up, bytes:bytes, files:(man.files||[]).length,"
        "        run:man.run_id, pv:man.parser_version};"
        "}catch(e){return {err:String(e&&e.message||e)};}})()", await_promise=True) or {}
    ck("lake publish: every indexed partition uploaded through the real /lake API",
       not pub.get("err") and (pub.get("uploaded") or 0) == 5,
       json.dumps(pub, ensure_ascii=False)[:300])
    ck("lake publish: the manifest the server stored names them all",
       pub.get("files") == pub.get("uploaded") and pub.get("run") == 1
       and pub.get("pv") == port.get("pv"),
       json.dumps(pub, ensure_ascii=False)[:300])
    dash_id = pub.get("dash")
    print(f"      [e{engine}] lake: {pub.get('uploaded')} partitions / "
          f"{pub.get('bytes')} bytes published for dashboard {dash_id}", flush=True)

    # ── one-shot "Ask" (ADR-13), while the folder is indexed ───────────
    # The fixture generator ships semantics, so "Ask" goes through the
    # intent route: the metric is chosen (deterministically by the form
    # matcher, or by the model), the worker's `intent_sql` builds the SQL
    # from the frozen definition, and the page SHOWS it and RUNS it.
    ck("intent: the summary reported both KPIs ready on this folder",
       "metrics: explore-fixture-gen (2)" in (summary or ""), str(summary))

    # (a) Form-first (H0) + one shot: the question quotes a KPI's question
    #     form, so the METRIC is settled without a model; the model is then
    #     asked for the CUT only (`fixed_kpi`), and the answer runs.
    await clear_grid(chrome)
    stub_before = len(_Stub.prompts)
    got = await ask_in_page(chrome, Q_SHARE)
    ck("one-shot: 'Ask' alone fills the grid — no second click anywhere",
       (got or {}).get("rows", 0) > 0, json.dumps(got, ensure_ascii=False)[:400])
    # A trend question over the same metric: two periods side by side per cut and a
    # delta, worst first — never a flat LIMIT-truncated list (prod 2026-08-23).
    await clear_grid(chrome)
    tr = await ask_in_page(chrome, "по какому складу отрицательный тренд доли реализованных заказов за последние два дня?")
    tr_head = await chrome.eval(
        "JSON.stringify({title:(document.getElementById('ex-answer-title')||{}).textContent||'',"
        " cols:[...document.querySelectorAll('#ex-result-thead th')].map(function(t){return t.textContent;}),"
        " rows:document.querySelectorAll('#ex-result-tbody tr').length})")
    tr_head = json.loads(tr_head or "{}")
    ck("trend: the answer is the two-period card (title says 'Trend', a Δ column, one row per cut)",
       tr_head.get("title", "").startswith("Trend") and "Δ" in (tr_head.get("cols") or [])
       and tr_head.get("rows", 0) >= 1, json.dumps(tr_head, ensure_ascii=False)[:300])
    await chrome.screenshot(shot("02e_trend.png"))
    ck("trend: the status names the worst cut or says there is no decline",
       ("The largest decrease" in (tr or {}).get("status", "")) or ("There is no decrease" in (tr or {}).get("status", "")),
       json.dumps(tr, ensure_ascii=False)[:300])
    await clear_grid(chrome)
    got = await ask_in_page(chrome, Q_SHARE)
    ck("one-shot: the status is an ANSWER line naming the metric and the cut",
       (got or {}).get("status", "").startswith(f'Answer for metric "{KPI_SHARE}"')
       and "dimension: sklad" in got.get("status", ""), str(got.get("status")))
    ck("one-shot: the SQL shown is the DETERMINISTIC one, built from the KPI's own cells",
       (got or {}).get("sql", "").startswith("WITH c AS") and "FROM lines" in got["sql"]
       and '"sklad"' in got["sql"], str(got.get("sql"))[:300])
    cut_prompt = next((p for p in reversed(_Stub.prompts)
                       if not p.startswith(P_VERIFY)), "")
    ck("form-first: the metric itself was NOT guessed — the model only got the cut prompt",
       len(_Stub.prompts) > stub_before
       and P_FIXED_KPI in cut_prompt
       and "order_amount" not in cut_prompt,
       cut_prompt[:300])
    ck("form-first: and the fixed-KPI prompt still carries no row data",
       "SO-001" not in cut_prompt and "SUM(CASE" not in cut_prompt,
       cut_prompt[:300])
    ck("one-shot: one row per warehouse came back over the real indexed partitions",
       (got or {}).get("rows", 0) == 2 and (got or {}).get("cols") and got["cols"][0] == "sklad",
       json.dumps(got, ensure_ascii=False)[:300])
    ck("one-shot: the badge credits the underlying partitions",
       "partition" in ((got or {}).get("badge") or ""), str((got or {}).get("badge")))

    # ── screen A: the answer card, in a real browser ───────────────────
    # The title is the answer's own first sentence, "how it is calculated" spells
    # the ratio out over the row that actually came back, and the SQL row
    # stays shut because nothing here was written by a model.
    card = await chrome.eval(
        "(function(){var t=document.getElementById('ex-answer-title');"
        "var c=document.getElementById('ex-how-calc');"
        "var d=document.getElementById('ex-how-def');"
        "var r=document.getElementById('ex-row-sql');"
        "var n=document.querySelectorAll('#ex-reports .ex-report-card').length;"
        "return JSON.stringify({title:(t&&t.textContent)||'',calc:(c&&c.textContent)||'',"
        "def:(d&&d.textContent)||'',open:!!(r&&r.open),cards:n});})()")
    card = json.loads(card or "{}")
    ck("answer card: #ex-answer-title carries the answer line",
       bool((card.get("title") or "").strip())
       and card["title"].startswith(f'Answer for metric "{KPI_SHARE}"'),
       str(card.get("title")))
    ck("answer card: 'how it is calculated' spells the ratio out as n / d = value",
       " / " in (card.get("calc") or "") and "=" in (card.get("calc") or ""),
       str(card.get("calc")))
    ck("answer card: 'SQL of this answer' is CLOSED after a deterministic answer",
       card.get("open") is False, str(card.get("open")))
    ck("reports: the strip rendered a card for the generator-backed dashboard",
       (card.get("cards") or 0) >= 1, str(card.get("cards")))
    await chrome.screenshot(shot("02a_oneshot_form.png"))

    # ── "Chat" (phase 3), in a real browser with a real IndexedDB ──────
    # The answer above became a thread record on its own. Everything here is
    # driven through the page's own ids: the toggle, the bubble, "Expand",
    # and a follow-up that must be answered with NO model call at all.
    await chrome.click("#ex-chat-btn")
    shown = await wait_for(chrome,
        "(function(){var c=document.getElementById('ex-chat');"
        "return (c && !c.hidden) ? 1 : 0;})()", timeout=20)
    ck("chat: #ex-chat-btn shows the chat", bool(shown), str(shown))
    hidden = await chrome.eval(
        "(function(){return ['ex-reports','ex-ask','ex-answer','ex-rows']"
        ".map(function(i){var e=document.getElementById(i);return e?(e.hidden?1:0):-1;}).join(',');})()")
    ck("chat: every screen-A section is hidden while the chat is up",
       hidden == "1,1,1,1", str(hidden))
    bubbles = await wait_for(chrome,
        "document.querySelectorAll('#ex-chat-thread .ex-bub').length", timeout=20)
    ck("chat: the answer given above is already a bubble in the thread",
       (bubbles or 0) >= 1, str(bubbles))
    count = await chrome.eval(
        "(document.getElementById('ex-chat-count')||{}).textContent||'0'")
    ck("chat: #ex-chat-count shows the thread length", int(count or 0) >= 1, str(count))
    first_q = await chrome.eval(
        "(function(){var b=document.querySelector('#ex-chat-thread .ex-bub .ex-bub-q');"
        "return b?b.textContent:'';})()")
    ck("chat: the bubble carries the question that produced it",
       Q_SHARE in (first_q or ""), str(first_q))

    # A deterministic follow-up. The fixture's KPI is cut by sklad/client/day,
    # so "by day" is the granularity it actually HAS — "by month" would be
    # a cut this metric cannot make and is correctly NOT a client-side match.
    # The follow-up is Russian on purpose (the client-side follow-up matcher).
    stub_before = len(_Stub.prompts)
    await chrome.eval(
        "(function(){document.getElementById('ex-chat-input').value='а по дням?';"
        "document.getElementById('ex-chat-send').click();})()")
    grew = await wait_for(chrome,
        "document.querySelectorAll('#ex-chat-thread .ex-bub').length > %d ? "
        "document.querySelectorAll('#ex-chat-thread .ex-bub').length : 0" % (bubbles or 1),
        timeout=120)
    ck("chat follow-up: 'and by day?' produced a new bubble", bool(grew), str(grew))
    ck("chat follow-up: it was answered with NO model call — the stub was never hit",
       len(_Stub.prompts) == stub_before, f"{stub_before} -> {len(_Stub.prompts)}")
    fu_sql = await chrome.eval("document.getElementById('ex-sql').value")
    ck("chat follow-up: the SQL is the deterministic one, cut by day",
       (fu_sql or "").startswith("WITH c AS") and '"day"' in (fu_sql or ""), str(fu_sql)[:300])
    await chrome.screenshot(shot("02c_chat.png"))

    # "Expand →" puts the stored answer back on screen A, addressably.
    await chrome.eval(
        "document.querySelector('#ex-chat-thread .ex-bub [data-act=\"expand\"]').click()")
    back = await wait_for(chrome,
        "(function(){var a=document.getElementById('ex-answer');var c=document.getElementById('ex-chat');"
        "return (a && !a.hidden && c && c.hidden && /#q=\\d+/.test(location.hash)) ? location.hash : '';})()",
        timeout=20)
    ck("chat: 'Expand →' returns to screen A at a #q=N url", bool(back), str(back))
    nav = await chrome.eval(
        "(function(){var n=document.getElementById('ex-chat-nav');return n && !n.hidden ? 1 : 0;})()")
    ck("chat: screen A grows the '← Back to chat' bar while it shows a stored record", bool(nav), str(nav))
    rows_back = await chrome.eval("document.querySelectorAll('#ex-result-tbody tr').length")
    ck("chat: the stored rows were rehydrated, not re-run", (rows_back or 0) > 0, str(rows_back))
    await chrome.eval("document.getElementById('ex-chat-back').click()")
    await wait_for(chrome,
        "(function(){var c=document.getElementById('ex-chat');return c && !c.hidden ? 1 : 0;})()",
        timeout=20)
    # Back to screen A for everything that follows — the rest of this flow is
    # written against it and must find it exactly as it was.
    await chrome.click("#ex-chat-btn")
    await wait_for(chrome,
        "(function(){var a=document.getElementById('ex-ask');return a && !a.hidden ? 1 : 0;})()",
        timeout=20)

    # ── phase 4.5: "why <number>?" asked COLD, in a real browser ──────
    # The number the operator types is one they read in the report, so the
    # value it must be found by is taken the same way: the KPI's own Grand
    # Total is run through the metrics panel first, and the number the GRID
    # PRINTS (thin-space thousands and all) is what gets asked about.
    #
    # The model is never involved on this route — cell values never reach the
    # server, so `_Stub.prompts` must not grow by a single entry.
    await clear_grid(chrome)
    await chrome.eval(
        "(function(){var b=document.querySelector('#ex-metric-list [data-kpi=\"order_amount\"]');"
        "if(b) b.click();})()")
    await wait_for(chrome,
        "(function(){var r=document.querySelector('#ex-metric-form [data-metric-run]');"
        "return r ? 1 : 0;})()", timeout=20)
    await chrome.eval(
        "(function(){document.querySelectorAll('#ex-metric-form input[type=checkbox][data-dim]')"
        ".forEach(function(c){c.checked=false;});"
        "document.querySelector('#ex-metric-form [data-metric-run]').click();})()")
    await wait_for(chrome,
        "(function(){return document.querySelectorAll('#ex-result-tbody tr').length;})()",
        timeout=120)
    gt_text = await chrome.eval(
        "(function(){var th=[...document.querySelectorAll('#ex-result-thead th')]"
        ".map(function(h){return h.textContent;});"
        "var i=th.indexOf('value'); if(i<0) i=th.length-1;"
        "var tr=document.querySelector('#ex-result-tbody tr');"
        "return tr ? tr.children[i].textContent : '';})()")
    ck("value lookup: the fixture KPI really produced a Grand Total to ask about",
       bool((gt_text or "").strip()), repr(gt_text))

    # Cold means cold: no open answer, no thread to search, no cached totals —
    # so the lookup has to sweep the ready KPIs through the worker.
    await chrome.eval(
        "(function(){var s=window.__opExploreState;"
        "s.openAnswer=null; s.gtCache=null; s.historyProvider=null;})()")
    # The value route writes its own "Looking for the number …" progress line, which
    # `ask_in_page`'s settle condition does not know about — wait it out here
    # rather than reading a status that is still mid-search.
    async def _settled_status():
        return await wait_for(chrome,
            "(function(){var s=document.getElementById('ex-nl-status').textContent;"
            "return (s && !/^(Asking|Looking for the number)/.test(s)) ? s : '';})()", timeout=180)

    stub_before = len(_Stub.prompts)
    got = await ask_in_page(chrome, "почему %s?" % (gt_text or "").strip())
    got = dict(got or {}, status=await _settled_status())
    ck("value lookup: a number the report really prints is found and answered by the why-card",
       (got or {}).get("status", "").startswith("Why:")
       and KPI_AMOUNT in (got or {}).get("status", ""),
       str((got or {}).get("status"))[:300])
    ck("value lookup: the model was never called — the question never left the browser",
       len(_Stub.prompts) == stub_before, f"{stub_before} -> {len(_Stub.prompts)}")
    await chrome.screenshot(shot("02d_value_lookup.png"))

    # A number that is nowhere in the folder: an honest status, and still no
    # model call. The chips beside it are the way out that needs no model.
    await chrome.eval(
        "(function(){var s=window.__opExploreState;"
        "s.openAnswer=null; s.gtCache=null; s.historyProvider=null;})()")
    stub_before = len(_Stub.prompts)
    got = await ask_in_page(chrome, "почему 999999,9?")
    got = dict(got or {}, status=await _settled_status())
    ck("value lookup: a number that is not in the data says so, in plain words",
       (got or {}).get("status", "")
       == "The number 999999,9 was not found in the current data — clarify the metric",
       str((got or {}).get("status"))[:300])
    ck("value lookup: the 'not found' path did not touch the model either",
       len(_Stub.prompts) == stub_before, f"{stub_before} -> {len(_Stub.prompts)}")
    chips = await chrome.eval(
        "(function(){var c=document.getElementById('ex-value-chips');"
        "return JSON.stringify({hidden:!!(c&&c.hidden),n:c?c.children.length:0});})()")
    chips = json.loads(chips or "{}")
    ck("value lookup: the metric chips are offered instead of a dead end",
       chips.get("hidden") is False and (chips.get("n") or 0) >= 2, json.dumps(chips))
    await chrome.eval(
        "(function(){var s=window.__opExploreState; s.openAnswer=null; s.gtCache=null;})()")

    # (b) Model path: a question no form matches confidently goes to the
    #     (stub) model with the definitions prompt — and also runs.
    await clear_grid(chrome)
    stub_before = len(_Stub.prompts)
    got = await ask_in_page(chrome, "что там по складам и клиентам за июль?")
    nl_sql = (got or {}).get("sql") or ""
    ck("intent: deterministic SQL landed in the editor (WITH c AS … over the KPI cells)",
       nl_sql.startswith("WITH c AS") and "FROM lines" in nl_sql and '"sklad"' in nl_sql, str(nl_sql)[:300])
    ck("intent (model path): it RAN in one shot too",
       (got or {}).get("rows", 0) > 0, json.dumps(got, ensure_ascii=False)[:300])
    nl_status = (got or {}).get("status") or ""
    ck("intent: status names the metric that answered",
       KPI_SHARE in nl_status, str(nl_status))
    ck("intent: the stub really was called", len(_Stub.prompts) > stub_before, str(len(_Stub.prompts)))
    # Which SIDE called it, and in which shape — the whole point of the
    # provider work. The stub records every path it served.
    prompt = _Stub.prompts[-1] if _Stub.prompts else ""
    paths = [p for p in _Stub.paths if not p.startswith("OPTIONS ")]
    if provider == "deepseek":
        ck("provider: the model was reached over the OpenAI-compatible /chat/completions",
           bool(paths) and paths[-1].endswith("/chat/completions"), str(paths[-3:]))
        ck("provider: the DeepSeek prompt carries the schema line JSON mode does not enforce",
           P_SCHEMA_HINT in prompt, prompt[-200:])
    else:
        ck("provider: the model was reached over Ollama's /api/generate",
           bool(paths) and paths[-1].endswith("/api/generate"), str(paths[-3:]))
    if browser:
        # The page did it: prepare/validate on the server, the model call
        # from the browser to the stub's own origin (CORS preflight and all).
        audit = c.get("/api/admin/audit-log?limit=50")
        body = audit.text if audit.status_code == 200 else ""
        ck("browser path: the server audited a validate call made via the browser",
           '"via": "browser"' in body or '"via":"browser"' in body, body[-300:])
        ck("browser path: the browser's cross-origin call was preflighted",
           any(p.startswith("OPTIONS ") for p in _Stub.paths), str(_Stub.paths[-4:]))
    # ADR-15: the LAST stub prompt may be the verifier's — these pins are
    # about the INTENT prompt, so take the last non-controller one.
    prompt = next((p for p in reversed(_Stub.prompts)
                   if not p.startswith(P_VERIFY)), "")
    ck("intent: a verifier pass followed the intent (ADR-15)",
       any(p.startswith(P_VERIFY) for p in _Stub.prompts), str(len(_Stub.prompts)))
    ck("intent: the prompt is the definitions form (KPI keys + dims), not a schema dump",
       "realised_share" in prompt and "sklad, client, day" in prompt and "datasalesorder" not in prompt, prompt[:400])
    ck("intent: the prompt carries NO row data and NO KPI SQL",
       "SO-001" not in prompt and "SO-007" not in prompt and "SUM(CASE" not in prompt, prompt[:400])
    # Run the shown SQL: the `sem` views must stand on the real indexed
    # partitions inside the browser's DuckDB (search_path, typed casts,
    # UNION BY NAME) and the badge must still credit those partitions.
    res = await run_sql_in_page(chrome, nl_sql)
    ck("intent: the SQL runs over the sem views in the browser (no error)",
       res and no_error(res.get("status")), str(res)[:300])
    ck("intent: ratio result has sklad + value columns and both warehouses",
       res and res.get("cols") and res["cols"][0] == "sklad" and res["cols"][-1] == "value"
       and len(res.get("rows") or []) == 2, str(res)[:300])
    ck("intent: the badge credits the underlying partitions (K of N partitions)",
       "partition" in ((res or {}).get("badge") or ""), str((res or {}).get("badge")))
    await chrome.screenshot(shot("02b_intent.png"))

    # ── tier 2: no KPI fits, so the model writes ONE SELECT over the
    #    report's own semantic views. Model-written SQL, hence the visible
    #    label — and still run, because both guards already passed it.
    await clear_grid(chrome)
    _Stub.intent_answer = json.dumps(
        {"kpi": None, "explanation": "this is not one of the report's metrics"}, ensure_ascii=False)
    stub_before = len(_Stub.prompts)
    err_before = len(chrome.console_errors)
    try:
        got = await ask_in_page(chrome, "сколько строк по каждому складу в отчёте?")
    finally:
        _Stub.intent_answer = None
    t2_prompt = _Stub.prompts[-1] if _Stub.prompts else ""
    ck("tier 2: the model got the report's SEMANTIC schema, not the raw catalogue",
       len(_Stub.prompts) > stub_before and P_SEM_SCHEMA in t2_prompt
       and '"lines"' in t2_prompt and "datasalesorder" not in t2_prompt, t2_prompt[:400])
    ck("tier 2: the schema prompt carries the columns with their types and descriptions",
       '"sklad" (text)' in t2_prompt and "Orders (DataSalesOrder)" in t2_prompt, t2_prompt[:800])
    ck("tier 2: and still NO row data — the boundary holds on this tier too",
       "SO-001" not in t2_prompt and "SO-007" not in t2_prompt and "Northgate" not in t2_prompt,
       t2_prompt[:600])
    ck("tier 2: the model-written SQL landed in the editor",
       'FROM "lines"' in ((got or {}).get("sql") or ""), str(got.get("sql"))[:200])
    ck("tier 2: it is LABELLED as model-written, so the operator knows to check it",
       (got or {}).get("status", "").startswith(
           "SQL was written by the model from the report's semantics — check it"), str(got.get("status")))
    ck("tier 2: and it RAN over the real sem views — one row per warehouse, no error",
       (got or {}).get("rows") == 2 and no_error((got or {}).get("sqlStatus")),
       json.dumps(got, ensure_ascii=False)[:400])
    # Model-written SQL is the one case where the SQL row opens itself: the
    # label asks the operator to check the query, so the query must be on
    # screen without another click.
    t2_open = await chrome.eval(
        "(function(){var r=document.getElementById('ex-row-sql');return !!(r&&r.open);})()")
    ck("answer card: 'SQL of this answer' OPENS for the tier-2 model-written SQL",
       t2_open is True, str(t2_open))
    # The intent route's 422 IS the routing signal to tier 2, and Chrome logs
    # every 4xx fetch as a console error. Assert the 422 is the ONLY noise,
    # then drop it — the zero-console-errors check at the end stays real.
    t2_errs = [e for e in chrome.console_errors[err_before:] if e]
    ck("tier 2: the only console noise is the expected 422 that routed the question here",
       all("422" in e for e in t2_errs), " | ".join(t2_errs[:3]))
    del chrome.console_errors[err_before:]
    await chrome.screenshot(shot("02d_tier2.png"))
    await clear_grid(chrome)

    if engine == 2:
        # ENGINE-2 ONLY: the `sem` schema answering a query written straight
        # against it — the semantic layer is a port (semantics.js), not the
        # same Python code behind a flag, so "the metrics panel worked" is not
        # by itself proof the views exist and are typed.
        sem_res = await run_sql_in_page(
            chrome, 'SELECT count(*) AS n, count(DISTINCT "sklad") AS s FROM lines')
        ck("v2: the ported sem views answer a direct query over `lines`",
           sem_res and no_error(sem_res.get("status"))
           and sem_res.get("rows") and int(sem_res["rows"][0][1]) == 2, str(sem_res)[:300])

    # ── metrics panel: the deterministic path, no model ───────────────
    n_metrics = await chrome.eval("document.querySelectorAll('#ex-metric-list .ex-recipe-btn').length")
    ck("metrics: both ready KPIs are listed as buttons", n_metrics == 2, str(n_metrics))
    await chrome.eval("document.querySelector('#ex-metric-list .ex-recipe-btn').click()")
    await asyncio.sleep(0.2)
    await chrome.eval(
        "(function(){const cb=document.querySelector('#ex-metric-form input[data-dim=sklad]');"
        "if(cb){cb.checked=true;} document.querySelector('#ex-metric-form [data-metric-run]').click();})()")
    st = await wait_for(chrome,
                        "(function(){const s=(document.querySelector('#ex-metric-form [data-metric-status]')||{}).textContent||'';"
                        "return /Done|Failed|not ready|rror/.test(s) ? s : '';})()", timeout=120)
    ck("metrics: 'Calculate' ran end to end", "Done" in (st or ""), str(st))
    m_sql = await chrome.eval("document.getElementById('ex-sql').value")
    m_rows = await chrome.eval("document.querySelectorAll('#ex-result-tbody tr').length")
    ck("metrics: deterministic SQL shown and one row per warehouse returned",
       (m_sql or "").startswith("WITH c AS") and m_rows == 2, f"rows={m_rows} sql={str(m_sql)[:120]}")
    await chrome.screenshot(shot("02c_metrics.png"))
    # Leave the editor and grid as the builder cases below expect them.
    await chrome.eval(
        "(function(){document.getElementById('ex-sql').value='';"
        "document.getElementById('ex-result-tbody').textContent='';"
        "document.getElementById('ex-result-thead').textContent='';"
        "document.getElementById('ex-result-badge').textContent='';})()")

    # ── builder query: GROUP BY warehouse, SUM(amount) ─────────────────
    # (the identifiers are the fixture's lower-cased Cyrillic headers)
    await chrome.eval(
        "(function(){"
        "const t=document.getElementById('ex-b-table');"
        "const opt=[...t.options].find(o=>o.textContent.toLowerCase().includes('datasalesorder'));"
        "t.value=opt.value; t.dispatchEvent(new Event('change'));"
        "const g=document.getElementById('ex-b-groupby');"
        "[...g.options].forEach(o=>{o.selected = o.value==='склад';});"
        "g.dispatchEvent(new Event('change'));"
        "const fn=document.getElementById('ex-b-agg-fn'); fn.value='SUM';"
        "fn.dispatchEvent(new Event('change'));"
        "const col=document.getElementById('ex-b-agg-col'); col.value='сумма';"
        "col.dispatchEvent(new Event('change'));"
        "return document.getElementById('ex-sql').value;})()")
    built = await chrome.eval("document.getElementById('ex-sql').value")
    ck("builder: the SQL preview reflects the picks",
       'GROUP BY "склад"' in (built or "") and 'SUM("сумма")' in (built or ""), str(built))
    await chrome.click("#ex-run-btn")
    await wait_for(chrome, "document.querySelectorAll('#ex-result-tbody tr').length > 0", timeout=180)
    got = await chrome.eval(
        "(function(){return {rows:[...document.querySelectorAll('#ex-result-tbody tr')]"
        ".map(tr=>[...tr.children].map(td=>td.textContent)),"
        "badge:document.getElementById('ex-result-badge').textContent,"
        "ms:document.getElementById('ex-result-ms').textContent};})()")
    rows = {r[0]: r[1] for r in (got or {}).get("rows", [])}
    ck("builder: two warehouse buckets over both partitions",
       sorted(rows) == ["Northgate", "Southport"], str(got))
    ck("builder: Northgate sums to 20500 (8000 in 07.26 + 12500 in 08.26)",
       str(rows.get("Northgate", "")).replace(",", "").startswith("20500"), str(rows))
    ck("builder: Southport sums to 23000 (13000 + 10000)",
       str(rows.get("Southport", "")).replace(",", "").startswith("23000"), str(rows))
    ck("builder: badge reads '2 of 2 partitions · data as of …'",
       "2 of 2 partitions" in (got or {}).get("badge", "")
       and "data as of" in (got or {}).get("badge", ""), str(got.get("badge")))
    ck("builder: elapsed ms rendered", "ms" in (got or {}).get("ms", ""), str(got.get("ms")))
    await chrome.screenshot(shot("03_builder_result.png"))

    # ── recipe "Reconcile": orders with no realisation ─────────────────
    await chrome.eval(
        "(function(){[...document.querySelectorAll('[data-recipe]')]"
        ".find(b=>b.dataset.recipe==='reconcile').click();})()")
    await asyncio.sleep(0.2)
    await chrome.eval(
        "(function(){const f=document.getElementById('ex-recipe-form');"
        "const [tA,kA,pA,tB,kB,pB,go]=f.children;"
        "const pick=(s,v)=>{s.value=v; s.dispatchEvent(new Event('change'));};"
        "const idOf=n=>JSON.parse(document.getElementById('ex-root').dataset.catalog)"
        ".tables.find(t=>t.name===n).table_id;"
        "pick(tA, idOf('datasalesorder')); pick(tB, idOf('datarealisation'));"
        "pick(kA,'номер'); pick(kB,'номер заказа'); go.click();})()")
    await asyncio.sleep(0.2)
    recipe_sql = await chrome.eval("document.getElementById('ex-sql').value")
    ck("recipe 'Reconcile': generated an anti-join over both tables",
       "LEFT JOIN" in (recipe_sql or "") and '"datarealisation"' in (recipe_sql or ""),
       str(recipe_sql))
    res = await run_sql_in_page(chrome, recipe_sql)
    vals = [r[0] for r in res["rows"]]
    ck("recipe 'Reconcile': exactly one order has no realisation (SO-007)",
       vals == ["SO-007"], json.dumps(res, ensure_ascii=False)[:400])
    ck("recipe 'Reconcile': badge counts all four partitions of the two tables",
       "4 of 4 partitions" in res["badge"], res["badge"])
    await chrome.screenshot(shot("04_recipe_reconcile.png"))

    # ── recipe "What changed": 07.26 vs 08.26 ─────────────────────────
    await chrome.eval(
        "(function(){[...document.querySelectorAll('[data-recipe]')]"
        ".find(b=>b.dataset.recipe==='changed').click();})()")
    await asyncio.sleep(0.2)
    await chrome.eval(
        "(function(){const f=document.getElementById('ex-recipe-form');"
        "const [t,k,pA,pB,go]=f.children;"
        "const pick=(s,v)=>{s.value=v; s.dispatchEvent(new Event('change'));};"
        "const idOf=n=>JSON.parse(document.getElementById('ex-root').dataset.catalog)"
        ".tables.find(t2=>t2.name===n).table_id;"
        "pick(t, idOf('datasalesorder')); pick(k,'номер');"
        "pick(pA,'2026-07'); pick(pB,'2026-08'); go.click();})()")
    await asyncio.sleep(0.2)
    changed_sql = await chrome.eval("document.getElementById('ex-sql').value")
    ck("recipe 'What changed': compares the two chosen partitions",
       "'2026-07'" in (changed_sql or "") and "'2026-08'" in (changed_sql or ""),
       str(changed_sql))
    res = await run_sql_in_page(chrome, changed_sql)
    buckets = {}
    for r in res["rows"]:
        buckets[r[1]] = buckets.get(r[1], 0) + 1
    ck("recipe 'What changed': 1 added (SO-007)", buckets.get("added") == 1, str(res["rows"]))
    ck("recipe 'What changed': 3 removed (SO-001..003 gone in 08.26)",
       buckets.get("removed") == 3, str(res["rows"]))
    ck("recipe 'What changed': 1 changed (SO-005 amount 5000 -> 5500)",
       buckets.get("changed") == 1, str(res["rows"]))
    await chrome.screenshot(shot("05_recipe_changed.png"))

    # ── export to Excel ───────────────────────────────────────────────
    # v1 exports through openpyxl inside Pyodide; v2 has no Python and DuckDB
    # has no xlsx writer, so `explore2/xlsx.js` builds the workbook by hand.
    # The check is the same either way: openpyxl must be able to open it.
    before = set(os.listdir(downloads))
    await chrome.click("#ex-export-btn")
    got_file = None
    deadline = time.time() + 240
    while time.time() < deadline:
        now = set(os.listdir(downloads))
        # Chrome occasionally drops a `downloads.html` (its own downloads
        # page) into the directory; only a workbook counts as our export.
        fresh = [f for f in now - before if f.endswith(".xlsx")]
        if fresh:
            got_file = fresh[0]
            break
        await asyncio.sleep(0.5)
    ck("export: an .xlsx landed in the download folder",
       bool(got_file) and got_file.endswith(".xlsx"),
       str(sorted(os.listdir(downloads))))
    if got_file:
        size = os.path.getsize(os.path.join(downloads, got_file))
        ck("export: the file is a non-trivial workbook", size > 1500, f"{got_file} {size} bytes")
        try:
            from openpyxl import load_workbook
            wb = load_workbook(os.path.join(downloads, got_file))
            ws = wb.active
            ck("export: the workbook holds the same rows as the grid",
               ws.max_row == len(res["rows"]) + 1, f"{ws.max_row} vs {len(res['rows'])}+1")
        except Exception as e:
            ck("export: the workbook holds the same rows as the grid", False, str(e))
    await chrome.screenshot(shot("06_after_export.png"))

    # ── save a question, then find it after a reload ──────────────────
    await chrome.eval(
        "(function(){document.getElementById('ex-save-title').value=" + json.dumps(saved_title) + ";"
        "document.getElementById('ex-save-btn').click();})()")
    # Wait for THIS question, not merely for a non-empty list: engine 2 runs
    # second and the sidebar already holds engine 1's.
    ck("save: the question appears in the sidebar right away",
       bool(await wait_for(
           chrome,
           "document.getElementById('ex-saved-list').textContent"
           ".includes(" + json.dumps(saved_title) + ")", timeout=30)))
    await chrome.navigate(url, wait_ms=1500)
    after = await wait_for(
        chrome,
        "(function(){const l=document.getElementById('ex-saved-list');"
        "return l.children.length ? l.textContent : '';})()", timeout=30)
    ck("save: the question is still there after a full page reload",
       saved_title in (after or ""), str(after))
    # Engine 2 runs after engine 1, so the list holds both — click OURS by name
    # rather than trusting the order.
    loaded = await chrome.eval(
        "(function(){const b=[...document.querySelectorAll('#ex-saved-list .ex-saved-btn')]"
        ".find(x=>x.textContent.includes(" + json.dumps(saved_title) + "));"
        "if(!b) return ''; b.click(); return document.getElementById('ex-sql').value;})()")
    ck("save: clicking the saved question restores its SQL",
       "FULL OUTER JOIN" in (loaded or ""), str(loaded)[:200])
    await chrome.screenshot(shot("07_saved_question.png"))

    # ── NL -> SQL fallback: after the reload nothing is indexed, so no
    #    semantics are "ready" and "Ask" takes the bare nl2sql path ──────
    rows_before = await chrome.eval("document.querySelectorAll('#ex-result-tbody tr').length")
    stub_before = len(_Stub.prompts)
    await chrome.eval(
        "(function(){document.getElementById('ex-nl').value="
        "'сколько заказов по каждому складу'; document.getElementById('ex-nl-btn').click();})()")
    await wait_for(chrome,
                   "(function(){const s=document.getElementById('ex-nl-status').textContent;"
                   "return s && !/^Asking/.test(s) ? s : '';})()", timeout=60)
    nl_sql = await chrome.eval("document.getElementById('ex-sql').value")
    ck("nl2sql fallback: the model's SQL landed in the editor", nl_sql == STUB_SQL, str(nl_sql))
    rows_after = await chrome.eval("document.querySelectorAll('#ex-result-tbody tr').length")
    ck("nl2sql fallback: it was NOT auto-run (the grid is untouched)",
       rows_after == rows_before, f"{rows_before} -> {rows_after}")
    ck("nl2sql fallback: the stub really was called", len(_Stub.prompts) > stub_before, str(len(_Stub.prompts)))
    prompt = _Stub.prompts[-1] if _Stub.prompts else ""
    ck("nl2sql fallback: the prompt carries the schema (table + column names)",
       "datasalesorder" in prompt and "склад" in prompt, prompt[:300])  # Cyrillic header
    ck("nl2sql fallback: the prompt carries NO row data (no order numbers)",
       "SO-001" not in prompt and "SO-007" not in prompt, prompt[:400])
    await chrome.screenshot(shot("08_nl2sql.png"))

    # ── phase 9.4 (load): the report answered with NO folder at all ────
    # The other half of the publish above, and the whole promise of the
    # server lake: a browser that has never seen the share folder pulls the
    # run's own Parquet down and answers a metric question over it. The test
    # hook (`window.__opExploreTestFiles`) is deliberately NOT set here — if
    # anything in the page reached for a folder, there is none.
    #
    # This origin's Parquet cache is wiped first: with the partitions still
    # in OPFS every key would be a hit and the download path — the thing
    # being tested — would never run.
    wiped = await chrome.eval(
        "(async function(){try{"
        "const root=await navigator.storage.getDirectory(); const out=[];"
        "for (const d of ['op-explore-cache','op-explore2-cache']) {"
        "  try { await root.removeEntry(d, {recursive:true}); out.push(d); } catch(e) {}"
        "}"
        "return out.join(',');}catch(e){return 'ERR '+String(e&&e.message||e);}})()",
        await_promise=True)
    ck("server index: this origin's Parquet cache was wiped before the test",
       isinstance(wiped, str) and not wiped.startswith("ERR"), str(wiped))

    await chrome.navigate(url, wait_ms=1200)
    # ADR-14b made server-side execution the default: this block tests the
    # DOWNLOAD fallback, so the flag is flipped off for it (read dynamically).
    await chrome.eval("document.getElementById('ex-root').dataset.lakeExec='0'")
    # Count the partition downloads the page makes, from the page itself —
    # "only the misses travel" is otherwise invisible from out here.
    await chrome.eval(
        "(function(){window.__opLakeGets=0; const of=window.fetch;"
        "window.fetch=function(u){ if(String(u).indexOf('/lake/parts/')>=0)"
        " window.__opLakeGets++; return of.apply(this, arguments); }; return 1;})()")
    ck("server index: no folder is connected on a freshly loaded page",
       await chrome.eval(
           "(function(){var s=window.__opExploreState;"
           "return !!(s && s.dirHandle===null && !window.__opExploreTestFiles);})()") is True)
    load_txt = await wait_for(
        chrome,
        "(function(){var b=document.querySelector('#ex-report-strip .ex-report-load');"
        "return b ? b.parentNode.textContent : '';})()", timeout=30)
    ck("server index: the card advertises the published index, its size and its date",
       "index on the server" in (load_txt or "") and "5 partitions" in (load_txt or "")
       and "data as of" in (load_txt or ""), str(load_txt))

    t_srv = time.time()
    await chrome.click("#ex-report-strip .ex-report-load")
    srv_indexed = await wait_for(
        chrome,
        "(function(){var s=window.__opExploreState; return (s&&s.indexed)?1:0;})()",
        timeout=300)
    srv_s = time.time() - t_srv
    ck("server index: 'Load index' indexes the report with no folder",
       bool(srv_indexed), str(srv_indexed))
    srv = await chrome.eval(
        "(function(){var s=window.__opExploreState;"
        "return JSON.stringify({source:s.source, dir:s.dirHandle, run:s.serverRun,"
        " parts:s.serverParts, gets:window.__opLakeGets,"
        " summary:document.getElementById('ex-summary').textContent,"
        " card:(document.querySelector('#ex-report-strip .ex-report-card')||{}).textContent||'',"
        " indexBtn:document.getElementById('ex-index-btn').disabled});})()")
    srv = json.loads(srv or "{}")
    ck("server index: the page says the rows came from the SERVER, with no folder",
       srv.get("source") == "server" and srv.get("dir") is None,
       json.dumps(srv, ensure_ascii=False)[:300])
    ck("server index: every partition was downloaded (the cache was empty)",
       srv.get("gets") == 5, str(srv.get("gets")))
    ck("server index: the summary names the run and the partitions it stands on",
       "index from server" in (srv.get("summary") or "")
       and "(run 1)" in (srv.get("summary") or "")
       and "5 partitions" in (srv.get("summary") or ""), str(srv.get("summary")))
    ck("server index: the card says the index came from the server",
       "index from the server · 5 partitions" in (srv.get("card") or ""), str(srv.get("card")))
    ck("server index: 'Index' stays disabled — there is no folder to re-read",
       srv.get("indexBtn") is True, str(srv.get("indexBtn")))
    print(f"      [e{engine}] server index: {srv.get('gets')} partitions downloaded, "
          f"indexed in {srv_s:.1f}s", flush=True)
    await chrome.screenshot(shot("08a_server_index.png"))

    # The point of all of it: a metric question, answered over an index this
    # browser never built, with rows on screen.
    await clear_grid(chrome)
    got_srv = await ask_in_page(chrome, Q_SHARE)
    ck("server index: a metric question is answered over it, with rows",
       (got_srv or {}).get("rows", 0) > 0, json.dumps(got_srv, ensure_ascii=False)[:400])
    ck("server index: the answer stands on real partitions (the badge names them)",
       "partition" in ((got_srv or {}).get("badge") or ""), str((got_srv or {}).get("badge")))

    # A second "Load index" must not re-download what OPFS now holds.
    await chrome.navigate(url, wait_ms=1200)
    await chrome.eval("document.getElementById('ex-root').dataset.lakeExec='0'")
    await chrome.eval(
        "(function(){window.__opLakeGets=0; const of=window.fetch;"
        "window.fetch=function(u){ if(String(u).indexOf('/lake/parts/')>=0)"
        " window.__opLakeGets++; return of.apply(this, arguments); }; return 1;})()")
    await wait_for(chrome,
                   "document.querySelectorAll('#ex-report-strip .ex-report-load').length",
                   timeout=30)
    await chrome.click("#ex-report-strip .ex-report-load")
    await wait_for(chrome,
                   "(function(){var s=window.__opExploreState; return (s&&s.indexed)?1:0;})()",
                   timeout=300)
    again = await chrome.eval("window.__opLakeGets")
    ck("server index: a second load re-uses this browser's cache — nothing travels",
       again == 0, str(again))

    # ── ADR-14b: server-side execution — the same lake, zero downloads ──
    # Default flag: "Load index" now activates `POST …/lake/exec`
    # (native duckdb over the published Parquet) instead of pulling
    # partitions into the browser. The question is answered end-to-end by
    # the REAL server instance built from the REAL uploaded parts.
    if engine == 1:
        await chrome.navigate(url, wait_ms=1200)
        await chrome.eval(
            "(function(){window.__opLakeGets=0; const of=window.fetch;"
            "window.fetch=function(u){ if(String(u).indexOf('/lake/parts/')>=0)"
            " window.__opLakeGets++; return of.apply(this, arguments); }; return 1;})()")
        await wait_for(chrome,
                       "document.querySelectorAll('#ex-report-strip .ex-report-load').length",
                       timeout=30)
        t_exec = time.time()
        await chrome.click("#ex-report-strip .ex-report-load")
        await wait_for(chrome,
                       "(function(){var s=window.__opExploreState; return (s&&s.indexed)?1:0;})()",
                       timeout=120)
        exec_s = time.time() - t_exec
        ex = await chrome.eval(
            "(function(){var s=window.__opExploreState;"
            "return JSON.stringify({exec:s.exec, dash:s.execDash, source:s.source,"
            " gets:window.__opLakeGets, parts:s.serverParts,"
            " summary:document.getElementById('ex-summary').textContent});})()")
        ex = json.loads(ex or "{}")
        ck("server exec: the report activates in server mode — not one partition travels",
           ex.get("exec") == "server" and ex.get("gets") == 0 and ex.get("parts") == 5,
           json.dumps(ex, ensure_ascii=False)[:300])
        ck("server exec: the summary says the server computes",
           "calculated by server" in (ex.get("summary") or ""), str(ex.get("summary")))
        await clear_grid(chrome)
        got_ex = await ask_in_page(chrome, Q_SHARE)
        ck("server exec: a metric question is answered BY THE SERVER, with rows",
           (got_ex or {}).get("rows", 0) > 0, json.dumps(got_ex, ensure_ascii=False)[:400])
        ck("server exec: provenance still names real partitions",
           "partition" in ((got_ex or {}).get("badge") or ""), str((got_ex or {}).get("badge")))
        print(f"      [e{engine}] server exec: activated in {exec_s:.1f}s, "
              f"0 partitions downloaded", flush=True)
        await chrome.screenshot(shot("08c_server_exec.png"))

    # ── phase 9.4: the viewer's pane indexes itself, zero clicks ───────
    # The SAME page in embed mode (`?report=…&mode=chat&embed=1` — what the
    # viewer puts in its iframe). Nobody there will press "Load index",
    # and no folder handle was ever stored for this report, so the published
    # index has to be picked up on load or the pane cannot answer at all.
    # Engine 1 only: the pane takes the server default engine, and engine 2's
    # manifest is written by a different parser version on purpose.
    if engine == 1:
        await chrome.navigate(
            f"{base}/explore?report={dash_id}&mode=chat&embed=1", wait_ms=1200)
        auto = await wait_for(
            chrome,
            "(function(){var s=window.__opExploreState;"
            "return (s&&s.indexed&&s.source==='server')?1:0;})()", timeout=300)
        ck("viewer pane: an embedded report indexes itself from the server, no clicks",
           bool(auto), str(auto))
        # The head follows explore.js's state on its own 700 ms poll, so it
        # is a beat behind the state above — wait for the pill, not for time.
        await wait_for(chrome,
                       "(function(){var e=document.getElementById('ex-chat-folder');"
                       "return (e && /index from server/.test(e.textContent)) ? 1 : 0;})()",
                       timeout=20)
        pane = await chrome.eval(
            "(function(){var s=window.__opExploreState;"
            "return JSON.stringify({dir:s.dirHandle, parts:s.serverParts,"
            " pill:(document.getElementById('ex-chat-folder')||{}).textContent||'',"
            " btn:(document.getElementById('ex-chat-folder-btn')||{}).hidden});})()")
        pane = json.loads(pane or "{}")
        ck("viewer pane: it did so with no folder connected",
           pane.get("dir") is None and pane.get("parts") == 5,
           json.dumps(pane, ensure_ascii=False)[:200])
        ck("viewer pane: the chat head says 'index from server' and offers no button",
           "index from server" in (pane.get("pill") or "") and pane.get("btn") is True,
           json.dumps(pane, ensure_ascii=False)[:200])
        await chrome.screenshot(shot("08b_embed_server_index.png"))

        # The pane was open BEFORE the run published the index (prod
        # 2026-08-23: its snapshot said «no index», so it asked for a folder
        # forever). The viewer posts `op_lake_published` into the frame on
        # publish; the pane must forget the stale «no index» and load.
        # Simulated from inside the pane: stale snapshot + dropped index,
        # then the same message the viewer sends (same origin).
        await chrome.eval(
            "(function(){var s=window.__opExploreState;"
            "var api=window.OP_EXPLORE; var rep=(api.reports||[])[0];"
            "if(rep){rep.lake=false;} s.serverManifests={}; s.indexed=false; s.source=null;"
            "window.postMessage({type:'op_lake_published', dashboard_id:rep&&rep.dashboard_id}, location.origin);"
            "return 1;})()")
        again = await wait_for(
            chrome,
            "(function(){var s=window.__opExploreState;"
            "return (s&&s.indexed&&s.source==='server')?1:0;})()", timeout=120)
        ck("viewer pane: a pane opened before publish loads the index on the viewer's op_lake_published message",
           bool(again), str(again))
        # And with NO index at all the pane never asks for a folder — it says
        # what is actually missing (a run), with nothing to press.
        await chrome.eval(
            "(function(){var s=window.__opExploreState; var api=window.OP_EXPLORE;"
            "var rep=(api.reports||[])[0]; s.serverManifests={}; if(rep) s.serverManifests[String(rep.generator_id)]=false;"
            "s.indexed=false; s.source=null; s.dirHandle=null; s.lastKill=null; return 1;})()")
        await wait_for(chrome,
                       "(function(){var e=document.getElementById('ex-chat-folder');"
                       "return (e && /recalculate the report/.test(e.textContent)) ? 1 : 0;})()", timeout=20)
        pill = await chrome.eval(
            "JSON.stringify({t:(document.getElementById('ex-chat-folder')||{}).textContent||'',"
            " b:(document.getElementById('ex-chat-folder-btn')||{}).hidden})")
        pill = json.loads(pill or "{}")
        ck("viewer pane: without any index it asks for a RUN, never for a folder",
           "recalculate the report" in (pill.get("t") or "") and "folder" not in (pill.get("t") or "").lower() and pill.get("b") is True,
           json.dumps(pill, ensure_ascii=False))
        await chrome.navigate(url, wait_ms=800)

    # Leave the report as it was found: the rest of the session (and the next
    # engine) must not silently inherit an index it did not publish.
    gone = await chrome.eval(
        "(async function(){const r=await fetch('/api/dashboards/'+"
        + json.dumps(str(dash_id)) +
        "+'/lake',{method:'DELETE',credentials:'same-origin'});return r.status;})()",
        await_promise=True)
    ck("server index: the published lake is removed again (owner DELETE)",
       gone == 200, str(gone))

    # ── the whole session, zero console errors ────────────────────────
    # The card's «is there an index?» probe is a plain GET whose 404 IS the
    # answer «nothing published for this report», and Chrome logs every 4xx.
    # Same discipline as the tier-2 422 above: name exactly those entries,
    # assert they are what they claim to be, and drop only them.
    # The snapshot's `lake` flag (a stat per card on the server) tells the
    # page which reports HAVE an index, so a report without one is never
    # asked for its manifest — no 404 noise at all, by construction.
    lake404 = [e for e in chrome.console_errors if "/lake/manifest" in e]
    ck("lake probe: reports without an index are never probed (no /lake/manifest 404)",
       not lake404, " | ".join(lake404[:3]))
    errs = [e for e in chrome.console_errors if e]
    ck("chrome: zero console errors across the whole /explore session",
       len(errs) == 0, " | ".join(errs[:5]))


async def viewer_chat(chrome, base, dash_id):
    """The "Chat" pane beside an open report (`/d/{id}`, phase 4).

    Everything is asserted through the PAGE: the launcher, the grid attribute,
    the iframe's own src and — reaching into a SAME-ORIGIN frame, which is the
    whole point of the design — the chat view inside it. The pane's laziness
    (no iframe until it is opened, no iframe after it is closed) is the claim
    that keeps a plain /d/{id} as cheap as it always was, so it is checked in
    both directions.
    """
    def ck(label, cond, extra=""):
        check("[viewer] " + label, cond, extra)

    chrome.console_errors.clear()
    # The pane is a grid column that YIELDS when the window cannot hold the
    # report floor (560) + chat (440) + dock (360) at once — at the default
    # 1280 it measures ~136px with the dock open. Pin a viewport where the
    # numbers below are about the chat alone (same as smoke_viewer_chat_chrome).
    await chrome.send("Emulation.setDeviceMetricsOverride",
                      {"width": 1600, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    await chrome.navigate(f"{base}/d/{dash_id}", wait_ms=1200)
    ck("/d/{id} renders the 'Chat' launcher (FAB)",
       await chrome.eval("!!document.getElementById('discuss-fab')") is True)
    ck("a plain /d/{id} has NO chat iframe (lazy src)",
       await chrome.eval("!document.getElementById('discuss-frame')") is True)
    ck("the report starts with the widget closed",
       await chrome.eval("document.getElementById('discuss-pane').hidden") is True)

    # The count contract: `{type:'op_chat_count', n}` posted to the parent at
    # location.origin, and nothing else. Hook it BEFORE the frame exists.
    await chrome.eval(
        "(function(){window.__opChatMsgs=[];window.addEventListener('message',function(e){"
        "if(e.origin===location.origin&&e.data&&e.data.type==='op_chat_count')"
        "window.__opChatMsgs.push(e.data);});return 1;})()")

    # P1: the pane is a GRID COLUMN — it pushes the report aside instead of
    # covering it. Measure the report iframe before and after.
    w_before = await chrome.eval(
        "document.getElementById('dash-frame').getBoundingClientRect().width")
    await chrome.eval("document.getElementById('discuss-fab').click()")
    ck("clicking the launcher opens the pane",
       await chrome.eval("!document.getElementById('discuss-pane').hidden") is True)
    ck("the viewer grid switches to its open shape",
       (await chrome.eval(
           "document.getElementById('viewer-root').getAttribute('data-chat')")) == "open")
    w_after = await wait_for(
        chrome,
        "(function(){var b=document.getElementById('dash-frame')"
        ".getBoundingClientRect().width;return b<%d?b:0;})()" % int(w_before or 0),
        timeout=5)
    ck("the report is PUSHED, not covered: #dash-frame gets narrower",
       bool(w_after) and w_after < w_before, "%s -> %s" % (w_before, w_after))
    ck("the pane is a column of the grid, not fixed chrome",
       (await chrome.eval(
           "getComputedStyle(document.getElementById('discuss-pane')).position")) == "relative")
    ck("the column is exactly --chat-w wide",
       abs((await chrome.eval(
           "document.getElementById('discuss-pane').getBoundingClientRect().width")) - 440) < 2,
       str(await chrome.eval(
           "document.getElementById('discuss-pane').getBoundingClientRect().width")))
    src = await chrome.eval(
        "(document.getElementById('discuss-frame')||{}).src||''")
    ck("the iframe now points at the embedded /explore",
       "embed=1" in (src or "") and "mode=chat" in (src or "") and str(dash_id) in (src or ""),
       str(src))

    inner = await wait_for(
        chrome,
        "(function(){var f=document.getElementById('discuss-frame');"
        "if(!f||!f.contentDocument)return '';"
        "var s=f.contentDocument.getElementById('ex-chat');"
        "return s&&!s.hidden?'shown':'';})()", timeout=45)
    ck("the frame is same-origin and its 'Chat' view is open", inner == "shown", str(inner))
    ck("the embedded page renders no sidebar",
       (await chrome.eval(
           "(function(){var f=document.getElementById('discuss-frame');"
           "return !!f&&f.contentDocument&&!f.contentDocument.querySelector('.sidebar');})()")) is True)
    ck("the embedded page carries the report on #ex-root",
       (await chrome.eval(
           "(function(){var f=document.getElementById('discuss-frame');"
           "return f&&f.contentDocument?"
           "(f.contentDocument.getElementById('ex-root').dataset.report||''):'';})()")) == str(dash_id))

    msgs = await wait_for(chrome, "window.__opChatMsgs.length", timeout=30)
    ck("the frame posted op_chat_count to the parent", bool(msgs), str(msgs))
    await chrome.screenshot(os.path.join(SHOTS, "09_viewer_chat.png"))
    ck("the launcher's count is a plain number",
       re.fullmatch(r"\d+", (await chrome.eval(
           "document.getElementById('discuss-count').textContent.trim()")) or "") is not None)
    # "Wider" is half the window; Esc closes and hands the focus back.
    # "wider" = 50vw only when the dock does not compete for the same width:
    # the chat track is minmax(0, --chat-w) and yields to the report floor
    # (560) and the dock. Close the dock first, as smoke_viewer_chat_chrome does.
    await chrome.eval("(function(){var v=document.getElementById('viewer-root');"
                      "if(v&&v.getAttribute('data-dock')!=='closed')"
                      "document.getElementById('dock-toggle').click();})()")
    await asyncio.sleep(0.4)
    await chrome.eval("document.getElementById('discuss-wide').click()")
    wide_ok = await wait_for(
        chrome,
        "(function(){var w=document.getElementById('discuss-pane')"
        ".getBoundingClientRect().width;"
        "var v=document.getElementById('viewer-root').getBoundingClientRect().width;"
        "return Math.abs(w-Math.round(v*0.5))<3?1:0;})()", timeout=5)
    ck("'wider' takes half the viewer (the window minus the sidebar)", bool(wide_ok),
       str(await chrome.eval(
           "document.getElementById('discuss-pane').getBoundingClientRect().width")))
    await chrome.eval("document.getElementById('discuss-wide').click()")
    ck("the big close button is really there and labelled",
       (await chrome.eval(
           "(document.getElementById('discuss-close')||{}).textContent||''")).strip() != "")

    # Reload with the widget open: the state is restored AND the iframe is
    # recreated lazily on that restore.
    await chrome.navigate(f"{base}/d/{dash_id}", wait_ms=1500)
    ck("the open state survives a reload",
       await chrome.eval("!document.getElementById('discuss-pane').hidden") is True)
    ck("the restore does not touch the report's own load: the column is open "
       "before the chat frame exists",
       (await chrome.eval(
           "document.getElementById('viewer-root').getAttribute('data-chat')")) == "open")
    ck("the restore re-creates the iframe, after the report has painted",
       bool(await wait_for(
           chrome,
           "((document.getElementById('discuss-frame')||{}).src||'')"
           ".indexOf('embed=1')>=0?1:0", timeout=20)))

    await chrome.eval("document.getElementById('discuss-fab').click()")
    ck("the launcher closes the widget",
       await chrome.eval("document.getElementById('discuss-pane').hidden") is True)
    # Closing KEEPS the frame (hidden): dropping it threw a built index
    # away, and every return to the chat rebuilt it for ~30 s. The worker
    # unloads only after 10 quiet minutes in the closed state.
    ck("closing KEEPS the iframe (the index survives close → open)",
       await chrome.eval("!!document.getElementById('discuss-frame')") is True)

    await chrome.navigate(f"{base}/d/{dash_id}", wait_ms=1000)
    ck("closed is persisted too", await chrome.eval(
        "document.getElementById('discuss-pane').hidden") is True)
    # Present mode keeps the chat reachable (owner 2026-08-23).
    ck("present mode keeps the launcher visible",
       (await chrome.eval(
           "(function(){document.body.classList.add('op-present');"
           "var v=getComputedStyle(document.getElementById('discuss-fab')).display!=='none';"
           "document.body.classList.remove('op-present');return v;})()")) is True)

    # The embedded pane probes the report's server index too (phase 9.4) —
    # nothing is published by now, and that 404 is the answer, not an error.
    lake404 = [e for e in chrome.console_errors if "/lake/manifest" in e]
    ck("lake probe: the pane never probes a report without an index (no 404)",
       not lake404, " | ".join(lake404[:3]))
    errs = [e for e in chrome.console_errors if e]
    ck("zero console errors across the viewer chat session",
       len(errs) == 0, " | ".join(errs[:5]))


async def main():
    args = parse_args()
    os.makedirs(SHOTS, exist_ok=True)
    if not os.path.exists(CHROME_BIN):
        print(f"SKIP chrome binary not found at {CHROME_BIN} — /explore e2e not run")
        return

    tmp = tempfile.mkdtemp(prefix="op-explore-chrome-")
    fixture = os.path.join(SCRATCH, "explore-fixture")
    make_explore_fixture.build(fixture)
    names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(fixture, "*.xlsx")))
    check("fixture: five workbooks built", len(names) == 5, str(names))

    stub_srv, stub_port = start_stub()
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
        "OP_LLM_ENDPOINT": f"http://127.0.0.1:{stub_port}",
        "OP_LLM_MODEL": "stub-model",
        "OP_SAML_SP_BASE_URL": f"http://localhost:{port}",
    })
    provider = "deepseek" if (args.provider == "deepseek" or args.browser) else "ollama"
    if provider == "deepseek":
        env.update({"OP_LLM_PROVIDER": "deepseek", "OP_LLM_API_KEY": "sk-stub-server", "OP_LLM_ACCESS_ADMINS": ADMIN})
    if args.browser:
        # Production shape: the server holds a key for the page and never
        # calls the provider itself. `llm-config` hands the page the stub's
        # URL, so the page's own fetch (cross-origin, hence the stub's CORS
        # headers) is what reaches the model.
        env.update({"OP_LLM_BROWSER": "true", "OP_LLM_BROWSER_KEY": "sk-stub-browser"})
    print(f"variant: provider={provider} browser={bool(args.browser)} stub=http://127.0.0.1:{stub_port}",
          flush=True)
    server = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    downloads = os.path.join(tmp, "downloads")
    os.makedirs(downloads, exist_ok=True)
    chrome_dir = tempfile.mkdtemp(prefix="op-explore-profile-")
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

        # ── seed: legacy generator + 2 runs + rebuild + confirm ───────────
        c = HttpClient(port)
        c.post("/dev-login", data={"person_id": ADMIN})
        info = seed_explore.seed(c)
        # DeepSeek is allowlisted per user (llm_access): the harness admin
        # grants HIMSELF through the real admin API, exactly as the owner
        # would — otherwise the model is never called and one-shot degrades
        # to the form-first path.
        if provider == "deepseek":
            r = c.put(f"/api/admin/llm/access/{ADMIN}", json={})
            check("seed: harness admin granted DeepSeek access via /api/admin/llm/access",
                  r.status_code == 200, r.text[:200])
        check("seed: catalog rebuild produced 3 tables", info["rebuild"].get("tables") == 3,
              json.dumps(info["rebuild"]))
        check("seed: every inferred table confirmed via the real API",
              len(info["confirmed"]) == 3 and all(s == 200 for _n, s in info["confirmed"]),
              str(info["confirmed"]))

        snap = c.get("/api/explore/catalog").json()
        by_name = {t["name"]: t for t in snap["tables"]}
        check("api: GET /api/explore/catalog returns the three tables",
              sorted(by_name) == ["datarealisation", "datasalesorder", "real_forecast"],
              str(sorted(by_name)))
        check("api: DataSalesOrder carries header_row=2 (2-row title block)",
              by_name.get("datasalesorder", {}).get("header_row") == 2,
              str(by_name.get("datasalesorder", {}).get("header_row")))
        check("api: DataSalesOrder has both monthly partitions",
              sorted(p["key"] for p in by_name["datasalesorder"]["partitions"]) == ["2026-07", "2026-08"],
              str(by_name["datasalesorder"]["partitions"]))

        # ── Chrome ────────────────────────────────────────────────────────
        chrome.start()
        await chrome.connect()
        try:
            await chrome.send("Browser.setDownloadBehavior",
                              {"behavior": "allow", "downloadPath": downloads})
        except Exception:
            await chrome.send("Page.setDownloadBehavior",
                              {"behavior": "allow", "downloadPath": downloads})
        base = f"http://localhost:{port}"

        await chrome.navigate(f"{base}/dev-login", wait_ms=400)
        r = await chrome.eval(
            "(async()=>{const r=await fetch('/dev-login',{method:'POST',"
            "headers:{'Content-Type':'application/x-www-form-urlencoded'},"
            f"body:'person_id={ADMIN}',credentials:'same-origin'}});return r.status;}})()",
            await_promise=True)
        check("chrome: dev-login POST from the page succeeded", r in (200, 302, 303), str(r))

        # ── the same flow, once per engine ────────────────────────────────
        # Engine 1 first: it is the shipped path, and a failure there means the
        # harness itself is wrong before v2 is even asked a question. The two
        # runs share one browser profile on purpose — the Parquet caches are
        # different OPFS directories (`op-explore-cache` vs
        # `op-explore2-cache`), so neither engine can be handed the other's
        # partitions by accident, and the saved questions are named per engine.
        for engine in (1, 2):
            print("\n" + "─" * 20 + f" engine {engine} " + "─" * 40, flush=True)
            await flow(chrome, base, engine, names, fixture, downloads, c, provider, args.browser)

        # ── the viewer's "Chat" pane (phase 4) ────────────────────────────
        print("\n" + "─" * 20 + " viewer chat pane " + "─" * 36, flush=True)
        await viewer_chat(chrome, base, info["dashboard"])

    finally:
        try:
            await chrome.close()
        except Exception:
            pass
        chrome.stop()
        stub_srv.shutdown()
        server.terminate()
        try:
            server.wait(timeout=8)
        except Exception:
            server.kill()

    print("\n" + "=" * 70)
    print(f"{len(RESULTS) - len(FAILURES)}/{len(RESULTS)} checks passed")
    if FAILURES:
        print("\nFAILED:")
        for f in FAILURES:
            print(" -", f)
    print("=" * 70)
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    asyncio.run(main())
