---
name: onepage-testing
description: How to run and extend every OnePage test layer — python smokes, node DOM harnesses, engine2 suite, the Chrome CDP harness — and the contracts they pin. Load before touching static/js, templates, or tests.
---

# Testing: Python, Node, Engine2, Chrome

## Python smoke tests

**Interpreter:** `.venv/bin/python` (python3.12; system 3.14 breaks SQLAlchemy 2.0.36).

**Run all:** `for t in tests/smoke_*.py; do .venv/bin/python "$t" || exit 1; done`

Each test prints `PASS`/`FAIL` lines, ends with `ALL PASS` or exit 1. No pytest — hand-written harness.

**Boot idiom** (see `tests/smoke_quality.py` lines 20-60):

```python
import os, sys, tempfile
TMP = tempfile.mkdtemp(prefix="op-test-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",        # dev-login enabled
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",                 # no startup preload
})
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient
from app.main import app

FAILURES = []
def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)

client = TestClient(app)  # runs lifespan (creates tables, seeds admin)
client.post("/dev-login", data={"person_id": "testuser"})  # session cookie
```

## Node DOM harnesses

No jsdom, no external dependencies — test by importing the module directly and stubbing the smallest DOM it touches.

**Files:** `tests/catalog_dom.js`, `tests/explore_dom.js`

**Run:** `for j in tests/*.js; do node "$j" || exit 1; done`

**Idiom** (see `tests/explore_dom.js` line 162–229):

- `ClassList`, `El` mock classes (no real DOM)
- `mk(tag, attrs, id)` creates mock elements, populates `byIdMap` by id
- `buildDom()` function constructs the minimal tree
- `lastWorker.respond(msg)` stubs `new Worker()`
- `global.fetch` stub returns test data
- Import the script: `const EXPLORE = require(path.join(ROOT, 'static', 'js', 'explore.js'))`
- Call pure functions directly: `EXPLORE.buildSql({...})` returns SQL strings
- Check results with `check(label, cond, extra)`

**Key mock objects:**

- `document_`: `createElement()`, `getElementById()`, `querySelectorAll()`, `body`
- `byIdMap`: global id → element map for `getElementById()`
- `docListeners` / `listeners[type]`: event dispatch via `dispatchEvent()`

## Engine2 test suite

Browser engine v2 (xlsx-arrow → duckdb-wasm), tested in Node.

**Run:** `npm run test:engine2` = `node --test "tests/engine2/*.test.mjs"`

**Result:** 43 tests (as of 2026-08-22), all pass = gate before flipping default.

## Chrome DevTools Protocol harness

Real Chrome over raw-websocket CDP — no Selenium/Playwright. Skips cleanly (exit 0) when
`CHROME_BIN` (tests/chrome/cdp.py) is absent.

**Files:** `tests/chrome/cdp.py` (`ChromeSession(port, user_data_dir, download_dir)`:
`start/connect/navigate/eval(expr, await_promise)/click/screenshot/stop`, collects
`console_errors`), `tests/chrome/smoke_explore_chrome.py` (the only Chrome test; drives
`/explore`), `tests/chrome/seed_explore.py` (`seed(c)` — generator bundle with
`SEMANTICS`, a generated dashboard, two runs, catalog rebuild), `make_explore_fixture.py`.

**How it boots:** a REAL uvicorn subprocess (`smoke_explore_chrome.py` `main()`, ~line 760):
`subprocess.Popen([PYTHON, "-m", "uvicorn", "app.main:app", "--port", port], env=…)` with
`OP_DB_PATH/OP_UPLOADS_DIR/OP_PERF_DIR` in a temp dir, `OP_DEV_BYPASS_AUTH=true`,
`OP_INITIAL_ADMIN=admin1`, `OP_CACHE_WARM=0`, **`OP_SAML_SP_BASE_URL=http://localhost:{port}`
(required — OriginGuard rejects the page's POSTs otherwise)**; waits on `/health`.
`PYTHON` = `OP_TEST_PYTHON` env or `.venv/bin/python`.
Seeding uses an in-file `HttpClient` (`.post/.get`) → `/dev-login` → `seed_explore.seed(c)`.
The page logs in via `fetch('/dev-login', {method:'POST', headers:{'Content-Type':
'application/x-www-form-urlencoded'}, body:'person_id=admin1'})` executed with `chrome.eval`.
The FSA folder picker is a native dialog, so the page reads fixture bytes from the
localhost-only hook `window.__opExploreTestFiles`.

**Run:** `$PY tests/chrome/smoke_explore_chrome.py` · `--provider deepseek` (server path over
a local OpenAI-compatible stub) · `--browser` (page → stub with a real CORS preflight).
`flow(chrome, base, engine, …)` runs ONCE PER ENGINE (`?engine=1` Pyodide, `?engine=2`
duckdb-wasm); labels are prefixed `[e1]`/`[e2]`. Results 2026-08-22: 137 / 139 / 143 checks.
Run variants sequentially — they share Chrome's debugging port. In zsh, pass flags as
separate words (`$v` is NOT word-split).

**Helpers:** `ck(label, cond, extra="")` / `check(...)` push into `RESULTS`/`FAILURES`;
`wait_for(chrome, js_expr, timeout, every)` polls a JS expression until truthy;
`clear_grid(chrome)`, `ask_in_page(chrome, question)` → `{sql, status, sqlStatus, rows, cols,
badge}`, `run_sql_in_page(chrome, sql)`; `shot(name)` → scratchpad `chrome-explore/`.
A `_Stub` HTTP server plays the model (`_Stub.prompts`, `_Stub.paths`, `_Stub.intent_answer`
for the tier-2 «not one of these metrics» case).

**Console-error discipline:** the last check asserts zero console errors for the whole
session. The intent route's 422 is the routine signal to tier 2 and Chrome logs every 4xx
fetch — the tier-2 block asserts the 422 is the only noise in its own window and deletes
just those entries (`del chrome.console_errors[err_before:]`); never waive 4xx globally.
Export check accepts only `*.xlsx` from the download dir (Chrome may drop `downloads.html`).

**Adding a check:** extend `flow()` with `ck(...)` after a `wait_for`; assertion values
come from `await chrome.eval("<js>")`. A new page (home, viewer) needs the same boot +
seed scaffolding — clone `main()` into a sibling file next to `cdp.py`.

**Contract enforcement:** every `#ex-*` id and data-attribute is referenced by
`static/js/explore.js`, `tests/explore_dom.js buildDom()` AND the Chrome harness:

```bash
grep -o "getElementById('ex-[a-z0-9-]*')" static/js/explore.js | sort -u
grep -o "\[data-[a-z-]*" static/js/explore.js | sort -u
```

Data-attributes that must survive: `[data-recipe] [data-kpi] [data-dim] [data-time]
[data-metric-run] [data-metric-status] [data-metric-order] [data-metric-limit]
[data-question-id]`; classes `.ex-recipe-btn(.active) .ex-saved-btn .ex-b-col-chip.active
.ex-check-item.sev-* .ex-badge.is-incomplete th.sorted-asc/-desc`.

**Rules:** move nodes, never rename ids; add new wrappers to `buildDom()` in the same
commit; the `[hidden]{display:none!important}` rule inside the inline `<style>` of
`templates/pages/explore.html` must survive; `#ex-check-btn` is enabled by a 300 ms poll
of `state.indexed` (`folderCheckFeature`), not by a DOM event; IndexedDB `op-gen/handles`
is shared with gen-engine.js (explore uses key prefix `explore:`).

## Pre-release checklist

Before any patch:

1. `for t in tests/smoke_*.py; do python "$t" || exit 1; done` — all python smokes
2. `for j in tests/*.js; do node "$j" || exit 1; done` — all node DOM harnesses
3. `npm run test:engine2` — 43 engine2 tests
4. `tests/chrome/smoke_explore_chrome.py` — 3 variants × 2 engines (default Ollama, `--provider deepseek`, `--browser`)
5. `$PY tools/real_smoke.py --dir <folder with real workbooks> --glob 'SL_*.xlsx' --sheet Заказы --table sl_zakazy --semantics <bundle.zip> --generator slreport` — 14/14 KPI ready, none over budget (`--max-kpi-ms 2000`)
6. `$PY tools/intent_eval.py --no-llm --generator slreport --questions tools/intent_eval_slreport.json --semantics <bundle> --data-dir <folder with real workbooks> …` — ≥ 18/20
7. `$PY tools/perf_baseline.py` — `db_n` and `serve_us` unchanged against docs/PERF_DECISIONS.md "Baseline measurements 2026-08-22"
8. Zero console errors on all flows

**Contracts pinned:** `tests/smoke_migration.py` (no-op second boot), `tests/smoke_xss_escaping.py` (user text via `textContent` only).
