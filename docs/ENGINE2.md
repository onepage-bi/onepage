# /explore engine v2 — a browser query engine without Python

Workstream H1, 2026-08-22. **Wired into `/explore` behind a flag and proven in
a real Chrome.** `?engine=2` (or `OP_EXPLORE_ENGINE=2`, default `1`) runs the
whole page on v2; the page picks a worker URL and changes nothing else.
`static/py/_explore.py` is untouched, `gen-worker.js` is untouched, and `app/`
gained exactly one setting. Reverting is still `OP_EXPLORE_ENGINE=1` plus one
`rm` of `static/js/explore2/`.

**The browser run, in one line:** both engines pass the same 109-check Chrome
harness; v2 indexes the fixture folder in **1.5 s against v1's 3.5 s**, its
Parquet cache lives in **real OPFS through DuckDB's own `opfs://` writer**
(second index 0.5 s), and on the real SL folder it holds 53 226 rows in
**41.2 MiB of DuckDB memory and 12 MB of JS heap** — three orders of magnitude
below the 2.75 GB tab ceiling. Details in [The browser run](#the-browser-run).

## The question

Today `/explore` reads a workbook like this:

```
xlsx bytes → Pyodide (wasm CPython, ~200 MB heap, single thread)
           → python-calamine  → pandas frame (python objects, per-cell)
           → DuckDB's pandas scan (wasm DuckDB, also inside CPython)
           → view
```

Three of those four boxes exist only to host the interpreter. The proposal:

```
xlsx bytes → engine/xlsx-arrow  (Rust calamine, 1.15 MB wasm) → Arrow IPC
           → duckdb-wasm insertArrowFromIPCStream               → native table
           → UNION ALL BY NAME view      → Parquet in OPFS, written by DuckDB
```

Same parser (calamine — python-calamine is a binding to it), same database
(DuckDB), same *semantics*. One fewer runtime.

## What was built

| path | what it is |
|---|---|
| `engine/xlsx-arrow/` | Rust crate: `parse_sheet(bytes, sheet, header_row) -> Arrow IPC`, `sheet_names(bytes)`. Column naming is a line-by-line port of `_explore._norm_col` + `_frame_columns` (`src/norm.rs`); cell typing reproduces `_coerce_time_cols` + `_coerce_mixed_cols` in one pass (`src/sheet.rs`). |
| `engine/xlsx-arrow/build.sh` | builds it to `static/xlsx-arrow/` (gitignored). |
| `deploy/fetch-duckdb-wasm.py` | vendors `@duckdb/duckdb-wasm@1.32.0` (mvp+eh+coi bundles, workers, wasm, node target) into `static/duckdb-wasm/`, **and** mirrors the DuckDB extension repository into `static/duckdb-ext/`. Both gitignored, like `static/pyodide/`. |
| `static/js/explore2/engine.js` | the engine. One ES module, runs in a Web Worker, on the main thread, and in Node. |
| `static/js/explore2/worker.js` | the **worker**: the same postMessage protocol `explore.js` speaks to `gen-worker.js`'s explore mode. This is the swap. |
| `static/js/explore2/guard.js` | `guard_sql` **verbatim** — `_scan`, `_statements`, both forbidden tables. |
| `static/js/explore2/semantics.js` | ADR-13: `_materialize_semantics`, `_sem_cast`, `semantics_status`, `intent_sql`. |
| `static/js/explore2/cache.js` | the Parquet partition cache: a second DuckDB instance + OPFS. |
| `static/js/explore2/xlsx.js` | "Export to Excel" without openpyxl — a ~200-line xlsx writer. |
| `tools/bench_engine2.mjs` | benchmark, v2. |
| `tools/bench_engine1.py` | benchmark, shipped engine, CPython. |
| `tools/bench_engine1_pyodide.mjs` | benchmark, shipped engine's parse step, **under Pyodide** — the apples-to-apples number. |
| `tools/compare_engines.py` | column-by-column schema diff between the two engines. |
| `tests/engine2/*.test.mjs` | 43 Node tests: parser edge cases, union + type conflict, Parquet round-trip, the guard against `smoke_explore_py.py`'s own cases, the semantic layer against `test_semantics`', and a proof that the self-hosted extension mirror is really used. |
| `tests/chrome/smoke_explore_chrome.py` | the end-to-end harness, now run **twice** — `?engine=1` and `?engine=2` — over one shared set of checks. |

## Numbers

Four real 1C exports (`SL_07/08/09/23.xlsx`, sheet "Orders" (column names below are given in English translation), 53 226 rows ×
28–30 columns, 6.5 MB of xlsx). MacBook, Node 24, DuckDB v1.4.3 (wasm eh),
best of 3 for queries.

### Indexing

| | v1 CPython (lower bound) | v1 **Pyodide** (what users run) | **v2** |
|---|---|---|---|
| parse + coerce | — | **2438 ms** | **871 ms** |
| index total (parse + into DuckDB + union) | 1502 ms | not measurable¹ | **959 ms** |
| per file | 357–400 ms | 548–650 ms | 212–274 ms |
| throughput | 4.2 MB/s | 2.6 MB/s | **7.4 MB/s** |
| resident data | 71.8 MB pandas frames | ~45 MB frames² | **33.9 MB** in DuckDB |
| process RSS | 321 MB | 468 MB | 746 MB³ |

¹ DuckDB is **not** in the vendored Pyodide subset (`deploy/fetch-pyodide.py`
packages micropip, pandas, python-calamine, xlrd), so the Pyodide script
measures the parse half only — which is where the time goes.
² per-file frames as pandas 3.0.2 reports them inside Pyodide.
³ Node's own RSS, dominated by the 33 MB DuckDB module and the file buffers;
DuckDB's own accounting (`pragma_database_size`) says 33.9 MiB for the data.
Not comparable to the Python numbers as a memory verdict — the comparable
figure is the "resident data" row.

**The honest headline: 2438 → 871 ms on the parse step, 2.8×, comparing the
same work in the same kind of runtime.** The 1502 ms CPython column is a
*lower bound* on the current engine and must not be used as the baseline: it is
native CPython, not wasm.

### Queries

| query | v1 CPython | v2 |
|---|---|---|
| `count(*)` per `__partition` | 19.0 ms | **3.6 ms** |
| `SUM(numeric) GROUP BY text` (top 20) | 20.7 ms | **3.7 ms** |
| filter on the date column | 23.7 ms | **2.3 ms** |

v1's query path carries a `fetchmany` loop, an interrupt timer and a
`count(*)` wrapper that v2's prototype does not, so part of this gap is
plumbing rather than engine. The 5× is real but not all of it is DuckDB.

### Parquet (the partition cache)

| | v1 CPython | v2 |
|---|---|---|
| `COPY` 53 226 rows → Parquet | 109 ms | 195 ms |
| read back | 1.4 ms | 80 ms |
| size | 0.4 MB (17× smaller than the xlsx) | same file, same size |

v2 is slower here and that is expected: it is DuckDB compiled to wasm writing
through a wasm filesystem shim, against native DuckDB writing to a real disk.
Both are fast enough — 195 ms to cache 6.5 MB of workbooks means a re-opened
`/explore` skips ~2.4 s of Pyodide parsing.

### Boot and download

| | v1 | v2 |
|---|---|---|
| runtime boot | Pyodide 1.56 s + packages (Node, warm disk) | 0.52 s |
| bytes on disk | 21.4 MB (`static/pyodide/`) | 37.5 MB |
| bytes on the wire | ~21 MB (wheels are already compressed) | **8.7 MB gzip / 7.1 MB brotli** |

The raw size looks worse and the wire size is better: `duckdb-eh.wasm` is
32.7 MB raw but 5.9 MB brotli, and the Pyodide subset is mostly `.whl` files
that cannot be compressed again. Serve `static/duckdb-wasm/` with the same
`CachedStaticFiles` `immutable` treatment as `static/pyodide/`.

## Semantic equivalence — what is proven, and one real divergence

`tools/compare_engines.py` indexes all four files through both engines and
diffs the resulting DuckDB schema.

**Column names: 32 of 32 identical.** Including the hard cases —
`customer` / `customer__2` and `sales organisation` /
`sales organisation__2` (duplicate headers), the reserved-name guard, and
blank headers becoming `col_N`. This is the part that had to be exact: the
query builder, the semantic layer and every saved query write SQL against
these names.

**Column types: 7 of 32 differ, and every one of them traces to a pandas
inference step v2 does not have.**

| column | v1 | v2 | why |
|---|---|---|---|
| `week` | DOUBLE | VARCHAR | the cells are the **strings** `'07.2026'`. pandas parses them as floats — week `07.2026` becomes the number `7.2026`. **v2 is right and v1 is silently lossy.** |
| `customer`, `material recipient`, `sales organisation` | BIGINT | VARCHAR | the cells are strings like `'1001781221'`; pandas converts all-numeric string columns to int64. Convenient, but it drops any leading zero and it is not what the sheet says. |
| `planned delivery date (blc console)` | TIMESTAMP | VARCHAR | the column mixes real dates with the literal text `'NA'`. pandas treats `'NA'` as missing, so the column becomes datetime and the `'NA'` rows vanish into NULL. v2 keeps the text, so the column stays VARCHAR. |
| `order`, `actual` | DOUBLE | BIGINT | pandas promotes an int column to float as soon as one value is missing. v2 types per column from the cells, so an all-integral column stays integral. |

This is a **migration risk, not a bug**: a saved query doing
`sum("order, pcs")` keeps working, but one doing `WHERE week > 7` or
`WHERE "planned delivery date (blc console)" >= DATE '2026-01-01'` would break.
Two ways forward, to decide before integration:

1. **Ship v2's typing and migrate the queries.** More faithful to the sheet;
   `'07.2026'` stops being a number. Needs a pass over saved queries and over
   `semantics.json` type declarations, which is exactly what the semantic
   layer's `_sem_cast` exists to absorb.
2. **Add a `pandasCompat` flag to the parser** reproducing the three
   inference rules (numeric strings → numbers, `NA`/`N/A`/`NaN` → null,
   int+null → float). ~40 lines in `sheet.rs`. Keeps every existing query
   working and keeps `week = 7.2026`.

Recommendation: (1), with (2) available as a rollback switch, because (2)
preserves a data-corrupting behaviour.

## What duckdb-wasm cannot do

### Extensions are NOT in the wasm binary

The finding that would have broken the corporate deploy quietly:

```
SELECT extension_name, loaded, installed, install_mode FROM duckdb_extensions();
→ parquet: not loaded, NOT installed, NOT_INSTALLED
```

`parquet` is **not** statically linked into `duckdb-{mvp,eh}.wasm` 1.32.0.
Only `core_functions` is. The first `COPY … (FORMAT PARQUET)` auto-installs it
from `https://extensions.duckdb.org/v1.4.3/wasm_eh/parquet.duckdb_extension.wasm`
— a CDN fetch, from a Worker, at query time. On a laptop behind the
SSL-inspecting proxy that surfaces as "the partition cache does not work", with
no network error anywhere the user can see.

Handled: `deploy/fetch-duckdb-wasm.py` mirrors the repository layout under
`static/duckdb-ext/`, and `createEngine()` sets
`custom_extension_repository` to `{base}duckdb-ext` (no trailing slash — DuckDB
appends `/v{version}/{platform}/{name}.duckdb_extension.wasm` itself).
`tests/engine2/selfhost.test.mjs` serves that directory from a throwaway
localhost server and asserts the request lands on it.

Two consequences to remember:
* the mirror is keyed by the **DuckDB engine version inside the wasm build**
  (`SELECT version()` → `v1.4.3`), not by the npm version (1.32.0). Bumping
  `@duckdb/duckdb-wasm` means re-checking `DUCKDB_VERSION` in the fetch script;
* extensions we did not mirror (`icu`, `spatial`, `httpfs`, `json` beyond what
  is vendored) simply are not available. `httpfs` is a feature we do not want
  anyway — see the guard below.

### Threads need cross-origin isolation

The `coi` bundle (threads + SharedArrayBuffer) is vendored but never selected,
because `selectBundle()` correctly refuses it unless the page is cross-origin
isolated. To enable it the **document** must be served with:

```
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Embedder-Policy: require-corp
```

and every subresource must then be either same-origin with

```
Cross-Origin-Resource-Policy: same-origin
```

or CORS-enabled. nginx:

```nginx
location / {
    add_header Cross-Origin-Opener-Policy   same-origin   always;
    add_header Cross-Origin-Embedder-Policy require-corp  always;
}
location /static/ {
    add_header Cross-Origin-Resource-Policy same-origin   always;
}
```

uvicorn/FastAPI (if there is no nginx in front) — a small ASGI middleware
setting the same three headers, registered outside `RenderFastLane` so the warm
render path still skips it.

**Do not turn this on without checking the dashboards.** OnePage renders
user-uploaded dashboards in a sandboxed iframe *without* `allow-same-origin`,
and dashboards may pull their own images or fonts. Under
`COEP: require-corp` every one of those subresources must carry CORP or CORS
headers, and any that does not simply fails to load. The measured win is not
worth a fleet of broken dashboards: DuckDB's threads help a multi-second scan,
and the queries above run in 2–4 ms single-threaded. **Recommendation: stay
un-isolated, keep shipping the `eh` bundle, revisit only if a real workload
turns out to be scan-bound.**

### The read-only guard

`_explore.py` opens DuckDB with `enable_external_access=false` as the belt to
`guard_sql`'s braces. In duckdb-wasm that setting **does** exist and **does**
work — measured:

```
SET enable_external_access=false;
SELECT * FROM read_csv('/etc/hosts');
→ Permission Error: file system operations are disabled by configuration
```

But it is one-way (DuckDB refuses to re-enable it) and it also blocks the
extension installer, so `toParquet`/`fromParquet` die with
`Extension Autoloading Error`. The same trade-off the shipped engine hit — it
answered with a second, IO-only connection (`_explore._io_conn`), and
`tools/bench_engine1.py` had to do the same to measure a COPY at all.

`createEngine({ lockdown: true })` sets it and records whether it took
(`engine.lockdown`). **The worker takes the first option and ships it on**:
`explore2/worker.js` boots the query engine with `lockdown: true`, and
`explore2/cache.js` boots a SECOND `AsyncDuckDB` — unlocked, IO only — that
owns the Parquet cache, mirroring `_explore._io_conn`. The Chrome harness
asserts `stats.lockdown === true` on every engine-2 run, so a build where the
setting silently failed is a failing test, not a quiet loss of the belt.

The two instances share no catalogue (duckdb-wasm has no in-process handle to
pass), so partitions cross between them as **Arrow**, in both directions:

* write: `indexFile({keepIpc: true})` hands back a copy of the same Arrow IPC
  it fed the query instance, and the cache inserts that into its own instance
  before `COPY … (FORMAT PARQUET, COMPRESSION ZSTD)`. The copy is needed
  because `insertArrowFromIPCStream` **transfers** the buffer — after the call
  the original is detached, byteLength 0;
* read: the IO instance runs `read_parquet` and the resulting Arrow table goes
  into the query instance via `insertArrowTable`. The rows never touch a file
  the query instance could open, which is the point of locking it.

The prototype's `guardSql()` was a *concept* port. It is now
`static/js/explore2/guard.js` — `_scan`, `_statements`,
`_FORBIDDEN_KEYWORDS`, `_FORBIDDEN_CALLABLE` copied line for line, including
the two-scan trick (identifiers MASKED for the keyword table, READABLE for the
callable one, so `"read_csv"('/etc/passwd')` cannot hide while a column named
`"delete"` stays queryable). `tests/engine2/guard.test.mjs` runs
`smoke_explore_py.py::test_guard_accepts` / `test_guard_rejects` case for case.

### Memory

DuckDB reports its own accounting: 33.9 MiB for 53 226 rows × 30 columns,
against a default `memory_limit` of 3.1 GiB (wasm derives it from the
heap). The engine accepts `memoryLimit: '512MB'` to match
`_explore.MEMORY_LIMIT`. Two structural differences from the Pyodide path:

* the workbook bytes are consumed and dropped at `indexFile()` — after
  `insertArrowFromIPCStream` the rows live in DuckDB's heap, so the caller can
  release its `ArrayBuffer` immediately (the same O(1) discipline as the run
  path). There is no `_FRAMES` dict keeping pandas objects alive;
* DuckDB can spill; a pandas frame cannot.

The tab ceiling is now measured — see below.

## The browser run

Headless Chrome 
(`tests/chrome/smoke_explore_chrome.py`, 2026-08-22), the whole `/explore`
session run twice over one shared set of checks: **109/109 pass, engine 1 and
engine 2, zero console errors in either.** Same page, same buttons, same
assertions on the same rendered text — the only difference is `?engine=`.

### What the flag proved

| | engine 1 (Pyodide) | engine 2 |
|---|---|---|
| first index, 5 fixture workbooks | **3.5 s** | **1.5 s** |
| of which parse | 1082 ms | **5 ms** |
| of which load into DuckDB | 188 ms | 15 ms |
| second index (Parquet cache) | ~2 s | **0.5 s** |
| Parquet cache backing | OPFS via Pyodide's `mountNativeFS` | **OPFS via DuckDB's own `opfs://`** |
| resident data (engine's own accounting) | not exposed | 5.6 MiB DuckDB |

The parse column is not a typo and not a fair fight either: 5 fixture rows per
sheet means engine 1 is paying for `pd.read_excel`'s fixed cost, not for the
data. The honest scaled number is the SL one below.

### OPFS Parquet: yes, natively

**`registerOPFSFileName` + `opfs://` works.** This is the branch docs said had
*never executed* anywhere. `cache.js` probes it once at boot by writing and
reading back a one-row Parquet at `opfs://op-explore2-cache/_probe.parquet`,
and reports the result as `cache.mode`, which the page prints in its summary
line whenever it is not the good one:

* **`opfs`** — what Chrome actually does. DuckDB writes straight into
  origin-private storage; the bytes never enter JS. Measured: 5 partitions,
  6 458 bytes of Parquet, second index 0.5 s, and the harness asserts it.
* `opfs-js` — fallback if a browser has OPFS but DuckDB will not drive it: the
  COPY goes to a wasm-memory buffer and we persist it with the plain OPFS API.
  Same durability, one copy through JS. Never taken in Chrome.
* `memory` / `off` — no OPFS at all. The cache lives for the life of the worker
  and the harness FAILS on it, because a cache that silently forgets is exactly
  the thing this check exists to catch.

One caveat found while probing: duckdb-wasm's OPFS layer only registers a file
handle when the file already has a non-zero size, which is why the probe writes
before it reads and why `put()` re-registers per key rather than once.

### Memory, on the real folder

`SL_07/08/09/23.xlsx` — 6.5 MB, 53 226 rows × 28–30 columns — indexed through
`engine.js` in a real Chrome tab (not the fixture, not Node):

| | |
|---|---|
| boot (parser wasm + duckdb-wasm, cold HTTP, self-hosted) | **982 ms** |
| index all four files | **661 ms** (Node: 635 ms — a tab is not slower) |
| DuckDB's own accounting | **41.2 MiB** |
| JS heap (`Performance.getMetrics`) | 0.7 → **12.1 MB** |
| query `count(*) GROUP BY __partition` | 12.1 ms first run |

**Against the 2.75 GB tab ceiling that killed the SL generator's legacy path,
53 MB of resident data for 6.5 MB of workbooks is not close to anything.** The
Chrome-process RSS delta (~700 MB) is *not* the number to quote: it is the
whole browser, it includes the 33 MB wasm module and the base64 payload the
measurement script itself injected, and the page path never does that.

`memoryLimit` is set to `512MB` by the worker to match `_explore.MEMORY_LIMIT`;
DuckDB reports it back as `488.2 MiB` and will spill rather than die, which a
pandas frame cannot.

## Still v1-only, or still unproven

* **"Check folder".** `_folder_check.py` needs the Pyodide engine, so
  `explore:check` on v2 answers `{findings: [], note}` and the page shows the
  note. A folder check that reads "no findings" because it never ran would be
  worse than one that says so.
* **The in-DuckDB interrupt timer.** duckdb-wasm has `cancelSent()` but nothing
  that pre-empts a statement which never yields a batch, so v2 enforces the
  30 s deadline BETWEEN RecordBatches — the same place `_explore.run_sql`
  enforces its fetch deadline — and the page's existing `killWorker` remains
  the hard stop. `_arm_interrupt`'s `threading.Timer` was already a documented
  no-op in the browser, so this is not a regression.
* **Export fidelity.** `explore2/xlsx.js` writes every cell as `inlineStr` or a
  bare number: no shared strings, no styles, no number formats, and a timestamp
  exports as its ISO text rather than a real Excel date. openpyxl opens the
  result and the harness checks the row count matches the grid, but a v1 export
  round-trips dates and this one does not.
* **No Pyodide-vs-v2 number for the DuckDB half**, because DuckDB is not in
  the vendored Pyodide subset. Only the parse step is compared like-for-like.
* **No `.xls` / `.xlsb` check.** calamine supports them; nothing tested them.
* **No concurrency.** One query at a time, same as v1.
* `casefold()` in Rust covers `ß`, `ẞ`, `ς`, `ﬁ`, `ﬂ` and otherwise uses
  `to_lowercase()`. Python's full Unicode casefold has more cases; none of them
  can plausibly reach a 1C header, but the divergence is real.
* Mixed columns render a datetime as `2026-07-01T10:30:00` (ISO 8601 with
  `T`); pandas' `str()` would have written a space. If a saved query does a
  string comparison against such a value it will not match.

### The self-hosting bug that only a browser could find

`dist/duckdb-browser.mjs` does `import * as arrow from "apache-arrow"` — a
**bare specifier**. Node resolves it through `node_modules`, which is why every
`tools/bench_engine2.mjs` run and all 43 Node tests passed while the file could
not be loaded by a browser at all:

```
Engine error: Failed to resolve module specifier "apache-arrow".
```

Import maps are document-scoped, so a module **Worker** cannot be handed one.
`deploy/fetch-duckdb-wasm.py` therefore also vendors the arrow ESM tree
(`static/arrow-esm/`, 1 MB, gitignored) and rewrites three specifiers:

| in | from | to |
|---|---|---|
| `duckdb-browser.mjs` | `"apache-arrow"` (×3) | `../arrow-esm/apache-arrow/Arrow.dom.mjs` |
| `apache-arrow/**.mjs` | `"flatbuffers"` (×40) | the vendored flatbuffers ESM |
| `apache-arrow/**.mjs` | `"tslib"` (×17) | the vendored tslib ESM |

`bin/` (the arrow2csv CLI, which wants `node:fs` and `command-line-args`) is
left behind, and nothing `Arrow.dom.mjs` reaches imports `io/node/`, so no
`node:` specifier survives into the browser. **The lesson generalises: a Node
test suite cannot prove self-hosting.** Only the Chrome harness can.

## Integration plan

Behind a flag, in four steps, each independently revertible.
**Steps 0–2 are done; 3 and 4 are not.**

**Step 0 — build wiring (no user-visible change). DONE.**
`deploy/fetch-duckdb-wasm.py` and `engine/xlsx-arrow/build.sh` join
`tools/make-bundle.sh` next to `fetch-pyodide.py`. Both outputs are gitignored.
The Rust toolchain becomes a build-host requirement; if that is unacceptable,
check the built `static/xlsx-arrow/` into a release artefact instead — it is
1.2 MB.

**Step 1 — `OP_EXPLORE_ENGINE` (default `1`). DONE.**
`static/js/explore2/worker.js` speaks the same postMessage protocol
`explore.js` already speaks to `gen-worker.js`'s explore mode, so the page's
whole change is:

```js
const WORKER_URL = (ENGINE === 2 ? '/static/js/explore2/worker.js'
                                 : '/static/js/gen-worker.js') + '?v=' + ASSET_V;
```

`?engine=1|2` on the URL beats the server default, so an operator tries v2 in
one tab and falls back by dropping the query string. `app/` gained exactly one
field (`Settings.explore_engine`); the template already had `settings` as a
Jinja global, so no route changed.

**Step 2 — port the parts the prototype skipped. DONE**, in the order the plan
gave because each is a correctness gate:
* `guard_sql` verbatim → `explore2/guard.js` + `tests/engine2/guard.test.mjs`;
* query deadline → between RecordBatches (see "Still v1-only" on the interrupt);
* `_materialize_semantics` / `intent_sql` → `explore2/semantics.js` +
  `tests/engine2/semantics.test.mjs`, mirroring `test_semantics`' cases;
* the OPFS Parquet cache with
  `cacheKey(name, size, mtime, sheet, header_row, PARSER_VERSION)` and the
  2 GB LRU → `explore2/cache.js`;
* `tables()`/`stats()` in the shapes the page already renders, partition badge
  (`_referenced` / `_context`) included.

**Step 3 — decide the typing question. NOT DONE, and it is the gate.** v2
typing + query migration, or `pandasCompat`; run `tools/compare_engines.py`
over every catalogue table that has partitions in the wild, not just SL. The
7-of-32 type divergence in the section above is unchanged by any of this work
and is the one thing that can break a saved query.

**Step 4 — flip the default. NOT DONE.** `OP_EXPLORE_ENGINE=1` stays the
default and the rollback. "Check folder" must reach v2 first, or the flip
takes a feature away. The Pyodide subset stays vendored regardless: the report
generators need it and that is not changing.

## Build and run

```bash
# one-time toolchain
rustup target add wasm32-unknown-unknown
cargo install wasm-bindgen-cli --locked     # must match Cargo.lock's wasm-bindgen

# build the Excel parser  -> static/xlsx-arrow/  (gitignored)
engine/xlsx-arrow/build.sh

# vendor duckdb-wasm + the extension mirror + the arrow ESM the browser needs
#   -> static/duckdb-wasm/, static/duckdb-ext/, static/arrow-esm/   (gitignored)
python3 deploy/fetch-duckdb-wasm.py

# node dev kit (tests/benchmarks only — apache-arrow for the Node duckdb target)
npm install

# tests
cargo test --manifest-path engine/xlsx-arrow/Cargo.toml     # 9 Rust tests
node --test "tests/engine2/*.test.mjs"                      # 43 Node tests
.venv/bin/python tests/chrome/smoke_explore_chrome.py       # 109 checks, both engines

# benchmarks (SL files are company data — never committed)
node tools/bench_engine2.mjs        --dir ~/data/SL
node tools/bench_engine1_pyodide.mjs --dir ~/data/SL   # needs deploy/fetch-pyodide.py
.venv/bin/python tools/bench_engine1.py --dir ~/data/SL \
    --explore /path/to/_explore.py
.venv/bin/python tools/compare_engines.py --dir ~/data/SL \
    --explore /path/to/_explore.py

# regenerate the synthetic test fixtures (committed, tiny, no company data)
.venv/bin/python tests/engine2/make_fixtures.py
```

Pinned versions: `@duckdb/duckdb-wasm` **1.32.0** (DuckDB engine **v1.4.3**),
`apache-arrow` **17.0.0** + `flatbuffers` 24.12.23 + `tslib` 2.8.1 (the browser
ESM tree), `calamine` 0.36, `arrow` 59, `wasm-bindgen` 0.2.127.

Try it: `/explore?engine=2` on a dev server, or `OP_EXPLORE_ENGINE=2` to make
it the default for everyone. `?engine=1` always gets the shipped engine back.
