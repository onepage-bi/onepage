# OnePage — Engineering guide to building reports

**Who it is for.** People who write reports, and the models that generate
them. This document is canonical: if a report breaks the contracts described
here, it is the report that will be slow or broken, not the platform, and it
will show up in `/admin/errors` and `/admin/perf` under its owner's name.

---

## If you are writing a report right now

Three steps; everything else explains why they matter.

**1. Give the model [the block from §13](#13-prompt-block-for-an-llm-copy-it-when-generating-a-report).**
It is short and holds every hard requirement at once. Paste it into the
request whole — that block alone prevents nine mistakes out of ten.

**2. Check the five things that break a report outright.** Each one has
happened more than once, and each one looks like "the platform is broken":

| Check | Otherwise |
|---|---|
| No access to `localStorage` | `SecurityError` on load — a white screen for everyone |
| No `fetch` to APIs and no external links (CDN, Google Fonts) | The report will not open for half the people on the intranet |
| The data is NOT inside the HTML but in a slot | The whole document travels on every open |
| `op_ready` is posted after `load` | The data never arrives — "Loading…" forever |
| The layout is fluid, from 560 px | Horizontal scroll the moment someone opens the chat |

**3. Go through [the anti-pattern table (§12)](#12-anti-pattern-summary-table)** —
it is built to be skimmed right before you publish.

A ready-made skeleton of a working report is in [§11](#11-skeleton-of-a-correct-report-snapshot--data-slot).

---

## Where to find what

The foundations, without which nothing works:

- [§1. How the platform serves a report](#1-how-the-platform-serves-a-report-the-model-every-rule-follows-from) — the model every rule follows from
- [§2. The sandbox and its limits](#2-runtime-environment-the-sandboxed-iframe-and-its-limits) — what a report physically cannot do
- [§4. Where the data lives](#4-data-architecture-three-report-types-and-the-separation-rule) — three report types and the separation rule

How to make it fast:

- [§3. Budgets](#3-budgets) — how many bytes and milliseconds you have
- [§5. Compression](#5-compression-what-exactly-the-server-does-and-how-to-write-for-it) — what the server does and how to write for it
- [§6. JavaScript discipline](#6-javascript-discipline-initialisation-in-the-browser) — why a report "hangs" and how to cure it
- [§7. Slots and relative resources](#7-slots-and-relative-resources)

How not to break platform features:

- [§8. Colors, Text, Annotate, Locate and width](#8-compatibility-with-platform-features-colors--text--annotate)
- [§9. Stability](#9-stability-a-report-must-not-be-able-to-fail-silently) — a report must not fail silently
- [§11b. Where else your report opens](#11b-where-else-your-report-opens-new-in-autumn-2026) — single-file download, mirror, password

Reference:

- [§10. How to measure](#10-how-to-measure-the-tools-are-already-built-in)
- [§11. Skeleton of a correct report](#11-skeleton-of-a-correct-report-snapshot--data-slot)
- [§12. Anti-pattern table](#12-anti-pattern-summary-table)
- [§13. Prompt block for an LLM](#13-prompt-block-for-an-llm-copy-it-when-generating-a-report)

---

## 1. How the platform serves a report (the model every rule follows from)

**Why this document exists at all.** The server side delivers a warm report
in a fraction of a millisecond: production telemetry of 20 July 2026 — isolated
serve p50 = **207 µs**, p99 = 326 µs, total server time ≈ 0.3 ms. Of the "time
to pixels", the server accounts for **less than a tenth of a percent**.
Everything else is your file: how many bytes travel over the wire and for how
many milliseconds your JavaScript blocks the main thread. A report that
"hangs" is always its content, and the rest of this guide explains how not to
write one.


The full pipeline from upload to pixels:

```
Upload (.html ≤ 50 MB, otherwise 413)
  └─ detect_type() by regex:  <script id="bootstrap-snapshot"> → snapshot
                              <input type="file">              → interactive
                              otherwise                        → static
  └─ storage: data/uploads/{id}/v{n}/page.html (+ slots, snapshot.json, theme.json, text.json)

Render /render/{id}/  (the document the iframe loads)
  └─ injections into <head> (once, the result is cached):
       height-reporter, annotate, theme and textedit controllers,
       for interactive — bootstrap (postMessage bridge + localStorage proxy),
       for snapshot    — fresh JSON inside <script id="bootstrap-snapshot">
  └─ in-process LRU cache (budget OP_CACHE_MAX_MB, default 256 MB):
       cell = {raw, br, zstd, gzip}; key = (id, version, fingerprint from mtime)
       any change (replaced HTML, slot, theme, text) = a new key
  └─ fast lane (pure ASGI): a warm authenticated GET with no query string
       is served from a pre-built response, bypassing the whole framework:
       ~0.2–0.6 ms in production (1–3 µs in an isolated dev microbenchmark)
  └─ warm-up: the 20 newest + the 5 heaviest reports are pre-rendered and
       br-compressed at startup and after every change (2 s debounce)

Browser
  └─ sandboxed iframe without allow-same-origin (see §2)
  └─ YOUR code: HTML parsing, script execution, DOM construction, painting
```

From this follows the budget equation a report has to be designed against:

```
T(visible report) = T_server (≈ 0.3 ms, a platform constant)
                  + network RTT (~40–60 ms on the corporate network, physics)
                  + size_wire / bandwidth      ← set by YOUR size and compressibility
                  + T_parse(HTML) + T_init(JS) ← set by YOUR code
```

Only the last two lines are the author's to optimise. They are exactly what produces the spread from 150 ms to 3.5 s on real reports served by the same server.

**Critical:** the render document is served with `Cache-Control: no-store`, and the sandboxed iframe has an opaque origin, so its HTTP cache is partitioned per navigation. The practical consequence: **you pay for the wire on every single open of the report**. "It downloads once and gets cached" does not apply here — size always matters.

---

## 2. Runtime environment: the sandboxed iframe and its limits

The report runs in an iframe with the attribute:

```html
sandbox="allow-scripts allow-forms allow-downloads allow-popups allow-modals"
```

`allow-same-origin` is **absent on purpose** (isolation from the platform's cookies). The consequences the author must account for:

| Limit | Consequence for the report's code |
|---|---|
| Opaque origin | `document.cookie` is empty; platform sessions are unreachable — this is protection, not a bug |
| No access to `/api/*` | `fetch('/api/...')` from the iframe returns 401 (cookies are not sent). More than that: **`fetch()` does not work from the iframe at all**, not even for relative URLs — the opaque origin makes every request cross-origin, the platform sends no CORS headers, and reading the response is blocked. Data arrives **only** over postMessage (`op_file`); relative URLs `/render/{id}/…` work only as tag resources: `<script src>`, `<img>`, `<link>` |
| The real `localStorage` is blocked (any access throws `SecurityError`) | **For interactive only** the platform injects a proxy: reads come from `window.__OP_LS__`, writes go to the parent (`op_save_ls`); plain `localStorage.setItem/getItem` works transparently. **In snapshot/static the proxy is NOT injected** — do not touch `localStorage` at all (any access only inside `try/catch`); keep state in JS variables and persist it through the snapshot |
| Partitioned HTTP cache | Repeat views do not reuse the browser cache (see §1) |
| `html2canvas` and the like do not work | Screenshots are taken by the parent through the Screen Capture API — do not try to capture the canvas from inside |
| External network (CDN) | The platform is on the intranet: `cdn.jsdelivr.net`, Google Fonts and the like may be unreachable for some users. **Every library goes inline in the HTML or into a slot** (see §7) |

### 2.1 The postMessage protocol (iframe ↔ parent)

| Message | Direction | Purpose |
|---|---|---|
| `op_ready {slots}` | iframe → parent | "I have loaded" — **the parent sends slot files (`op_file`) only in reply to this message.** For interactive the injected bridge sends it (on `load`); **a snapshot/static report with slot data must send it itself** — otherwise the data never arrives. The `slots` field is informational: the parent hands out its own server-side list of slots |
| `op_file {slot, name, data:ArrayBuffer}` | parent → iframe | The content of a slot file (zero-copy) |
| `op_save_file {slot, name, data}` | iframe → parent | A changed file to save (persisted when the viewer has the `can_update_data` right) |
| `op_height {height}` | iframe → parent | Automatic iframe height. **Already injected by the platform** — do not add your own reporter |
| `op_save_ls {data}` | iframe → parent | Persists the localStorage proxy |
| `op_save_snapshot {data}` | iframe → parent | A full snapshot from a snapshot report edited in-page: the object in the `data` field is saved as the version's `snapshot.json` (owner/admin only; silently ignored for every other viewer) |
| `op:theme` (CustomEvent on `window`) | platform → your code | A theme has been applied — redraw the charts (see §8) |

Rules: do not install a global `message` interceptor that swallows other types; tag your own messages with a `type` prefix of your own; do not answer unknown types — the platform's controllers (annotate/theme/textedit) live on the same channel.

---

## 3. Budgets

The targets are derived from the platform's telemetry (`/admin/perf` collects ttfb / fcp / load for every view):

| Metric | Target | Acceptable | Problem | Hard limit |
|---|---|---|---|---|
| HTML document (raw, with every inline library) | ≤ 300 KB | ≤ 1.5 MB | > 5 MB | 50 MB (HTTP 413) |
| Data (in a separate slot, pre-compressed) | ≤ 1 MB wire | ≤ 5 MB wire | > 10 MB wire | — |
| `load` (full initialisation in the browser) | ≤ 500 ms | ≤ 1 s | > 2 s | — |
| Main-thread block by one piece of JS | ≤ 16 ms | ≤ 50 ms | > 200 ms | — |
| DOM nodes on first render | ≤ 1,500 | ≤ 3,000 | > 10,000 | — |
| localStorage state (through the proxy; interactive only) | ≤ 4 KB | ≤ 32 KB | > 100 KB | — |

Notes on the thresholds:

- **1.5 MB** is `OP_PREVIEW_MAX_BYTES`: a heavier report gets no live preview in the dashboard grid (a placeholder is drawn instead of a mini-iframe). It is the first visible penalty for weight.
- **localStorage** (interactive type only; in snapshot/static any access to it throws — §2) is injected into the render's `<head>` as `window.__OP_LS__ = {…}` — bloated state makes the document itself heavier and rotates the cache key on every save. Keep settings there (the selected tab, filters), not data.
- The 50 ms main-thread threshold comes from the RAIL model: anything longer and the user sees scrolling and clicks freeze.

**The real incidents these numbers come from** (all from this platform's perf logs):

| Incident | Server | Browser | Diagnosis |
|---|---|---|---|
| Report `a0099a9a`, 2.6 MB HTML, 140 KB wire | ttfb 34–47 ms | **load 2.0–3.5 s on every open** | Synchronous JS initialisation: the whole render in one piece on the main thread |
| "The giant", 38 MB of source | ~1 ms (warm) | seconds of transfer | Data inside the HTML instead of a slot |
| Interactive report with a 10.8 MB (raw) slot | +255 ms of compression on every view (cached now, but the first visit pays) | waiting | The slot was not pre-compressed (`.br`/`.gz`) |
| 15 MB slot that brotli only gets down to 12 MB (base64-like content) | hdr 24 ms | **4.85 s** of transfer on a fast network | Poorly compressible data plus volume: a binary columnar format is needed (§4.3) |
| Snapshot report with 6–12 MB slots | µs | `op:ready_to_data` **17–22 s** | The weight of the data: wire plus decoding. Slow-link arithmetic: 12 MB over a 2 Mbit/s VPN ≈ 50 s, 26 MB identity over 256 kbit/s ≈ **14 minutes of "loading" on every open** — that is how legends like "the report takes 20 minutes to load" are born |

---

## 4. Data architecture: three report types and the separation rule

### 4.1 Type detection: a regex over the raw HTML

The type is detected automatically, in order of priority:

1. There is a `<script id="bootstrap-snapshot">` → **snapshot**;
2. otherwise there is an `<input type="file">` → **interactive**;
3. otherwise → **static**.

The regex searches the whole raw file, **including comments and JS string literals**. A stray string `'<input type="file"'` in a templating helper inside your script will make the report interactive. Do not keep these patterns in the file unless you mean the corresponding type.

**A fourth type appeared in October, and it is NOT detected from the markup.** A navigator (`dash_type = "page"`) is a page of links to reports, Power BI and network folders; it is set by the Navigators section at upload time, not by the regex, because it is indistinguishable from a static page. One thing matters for the author: such a page gets a link interceptor injected when it is served. Every `click` on an `a[href]` and every `window.open` of yours goes up to the parent as a message, and the shell opens the tab. This is not done out of spite: the report iframe lives without `allow-same-origin`, and a tab opened from INSIDE it inherits the sandbox and the opaque origin — the browser gives it no cookies, and any system with a sign-in shows a white screen. Write links in a navigator as usual; there is nothing to implement, but do not intercept clicks yourself either.

Two consequences for interactive: slot N is addressed as **the N-th `<input type="file">` in DOM order** (the platform bridge literally does `querySelectorAll('input[type="file"]')[slot]`) — do not reorder inputs between versions, or the data lands in the wrong input; and the inputs must exist **statically in the markup** — an input created from JS after load is invisible to detection over the raw HTML.

### 4.2 The rule: code apart, data apart

The single decision with the biggest effect on a report's performance is **where the data lives**:

| Data volume | Scheme | Mechanics |
|---|---|---|
| ≤ ~200 KB JSON | Inline snapshot: `<script id="bootstrap-snapshot" type="application/json">{…}</script>` | On every render the server substitutes the current JSON inside the tag (`PUT /api/dashboards/{id}/snapshot` updates it without re-uploading the HTML). The data is available synchronously, with zero network requests |
| More | **Data slot**: an empty inline snapshot `{}` + the data file in slot 0 | At upload the creation page shows a "Data file — slot 0" field (if the inline snapshot is empty); in the viewer's Data panel the file is replaced through `PUT /api/dashboards/{id}/files/0`. The parent downloads the slot and sends `op_file` with an ArrayBuffer **only in reply to your `op_ready`** (§2.1). A report that never posts `op_ready` waits for its data forever — mistake number one in hand-made snapshot reports |
| The user changes the data | **interactive**: one `<input type="file">` per file | The platform injects a bridge: files arrive as ordinary `change` events; changes the user saves leave through `op_save_file` |

**If row-level security (RLS) is attached to the report, the choice in the table is made for you: interactive only, and only with an EMPTY inline snapshot.** The rows of such a report are filtered for each viewer when the slot is served; data baked into the HTML would reach everyone identically, and the platform checks this — a non-empty `bootstrap-snapshot` in an RLS report is refused at publication. The same fact tells you what such a report will not have: share links, single-file download, and a copy in the desktop mirror.

Why the data belongs in a slot and not in the HTML:

- The HTML document is rebuilt and travels over the wire **on every open** (`no-store`, §1). A slot is served with a strong stat-based `ETag` — a repeat load is a `304` at the cost of one `stat()`. This works because the slot is downloaded by **the parent page** (and passed in via `op_file`): its HTTP cache is an ordinary one, unlike the iframe's partitioned cache.
- Updating data by replacing the slot **does not touch** the HTML: the report's code stays stable, human review is possible, and the diff means something.
- Replacing a slot automatically rotates the render cache key and (for raw files) the key of the compressed-variant cache — freshness is guaranteed by the platform, the author does not have to think about it.

### 4.3 Data format: binary columnar beats JSON

For datasets of hundreds of thousands of values and up there is a hierarchy (from measurements on the real SL report):

1. **Binary columnar (SoA) + string dictionaries**, a `*.bin.br` or `*.bin.gz` file. Numeric columns are contiguous `Float64Array`/`Int32Array` regions; repeated strings are a dictionary plus an index column. Decoding = creating typed-array views over the ArrayBuffer from `op_file` — **zero copies, zero `JSON.parse`**. `JSON.parse` of a 10-megabyte string means hundreds of milliseconds of main-thread blocking and a 3–5× memory peak.
2. JSON pre-compressed into `*.json.br`/`*.json.gz` — acceptable up to a few MB.
3. Bare JSON inside the HTML — only for the "inline" volumes from the table above.
4. **Base64 blobs inside the HTML are a forbidden pattern.** Base64 adds +33% to the volume and, more importantly, is statistically incompressible: the server's probe (see §5) recognises such a body as incompressible and serves it as **identity**, i.e. at full size over the wire on every open.

Do the pre-compression on your side while preparing the data:

```bash
brotli -q 11 -o data.bin.br data.bin      # best ratio
# or
gzip -9 -k data.bin                        # universal fallback
```

The platform serves `*.br`/`*.gz` slots as they are, with the correct `Content-Encoding` (the browser decompresses transparently), and decompresses on the server itself for a client that lacks the codec. Raw (uncompressed) slots are compressed by the platform on the fly and the variant is cached, but the first visit after each file replacement pays for the compression — a pre-compressed file is always strictly better.

---

## 5. Compression: what exactly the server does and how to write for it

The server negotiates the codec from `Accept-Encoding`: **brotli(5) → zstd(6) → gzip(6) → identity**. Before compressing it takes a probe: the middle 64 KB of the body are run through zlib-1, and if the output is ≥ 95% of the input, the body is declared incompressible and goes out as identity (compressing garbage wastes CPU and time). Bodies ≤ 1 KB are not compressed at all.

Consequences for the author:

- **Text (HTML/JS/JSON/CSV) compresses 5–20×.** A report of 1.5 MB of markup and inline libraries really travels as ~200–300 KB of br. That is a normal, healthy structure.
- **Incompressible content (base64, PNG/JPEG/fonts embedded as data: URIs, already compressed data) sends the WHOLE document as identity** — the probe is taken from the middle of the body, which is usually where the blob sits. One embedded megabyte screenshot denies compression to the markup around it too.
- Images and binary resources go **into slots** (§7), not data: URIs. SVG (text, it compresses) can be inline.
- Minifying JS/CSS gives a moderate gain on top of brotli (~10–20%), but noticeably speeds up **parsing** in the browser — minify the libraries; the report's own code can stay readable.

---

## 6. JavaScript discipline: initialisation in the browser

This section is about incident `a0099a9a`: the server answered in 1 ms, the wire carried 140 KB, and yet the user waited 2–3.5 seconds on every open, because the whole report was built in a single synchronous pass. The rules below are how to avoid that.

### 6.1 The frame at once, the heavy parts frame by frame

The user must see the report's shell (headings, KPI tiles, axes) in the very first frame, and the expensive nodes should arrive in portions:

```js
initFromBuffer(buf) {
  renderShell();                                   // synchronous: KPIs, layout, the table "skeleton"
  requestAnimationFrame(() => renderCharts());     // charts — on the next frame
  renderTableChunked(rows, 200);                   // table — in portions of 200 rows
}

function renderTableChunked(rows, n) {
  let i = 0;
  (function next() {
    const frag = document.createDocumentFragment();
    for (const end = Math.min(i + n, rows.length); i < end; i++)
      frag.appendChild(renderRow(rows[i]));
    tbody.appendChild(frag);
    if (i < rows.length) setTimeout(next, 0);      // yield the main thread
  })();
}
```

No synchronous piece may run longer than 50 ms. Check it in DevTools → Performance: long yellow "Evaluate/Function call" blocks at startup are your candidates for chunking.

### 6.2 The DOM is the most expensive data structure in the browser

- First render — no more than ~1,500–3,000 nodes. A table of 50,000 rows means **virtualisation** (only the visible rows plus a buffer are rendered) or, at the very least, pagination/chunking; never 50,000 `<tr>` at once.
- Insert through a `DocumentFragment` or a single `innerHTML` on the container, not node by node in a loop.
- Do not interleave layout reads (`offsetHeight`, `getBoundingClientRect`) with style writes in a loop — that forces a synchronous reflow on every iteration (layout thrashing). All the reads first, then all the writes.
- Do not build the layout off `100vh`/the window height: the iframe auto-grows to its content via `op_height` (the reporter measures `scrollHeight`), and a viewport-dependent height produces either a growth loop or clipping. Let the content set the height.
- Remember: after load, the platform's injected controllers walk your DOM (annotate/theme/textedit scan the elements). A giant DOM slows them down too.

### 6.3 Charts

- **> ~1,000 points — canvas only** (Chart.js, the ECharts canvas renderer, uPlot). SVG/DOM renderers create a node per point and die on large series.
- Decimation: one point per pixel of width is the limit of useful density; aggregate a series down to ~2× the canvas width (min/max decimation preserves the shape).
- Turn off first-paint animations (`animation: false` / `chart.update('none')`) — on heavy data that is hundreds of milliseconds of pure penalty.
- One charting library per report. Chart.js + ECharts + D3 in one file is megabytes of parsing for the sake of taste.

### 6.4 Computation

- Aggregations/filters over large arrays — in chunks through `setTimeout(0)`/`requestIdleCallback` (the safe default), or in a **Web Worker** — inline only, through `new Worker(URL.createObjectURL(new Blob([code])))`: external worker files are unreachable in the sandbox, and a blob worker in an opaque origin does work in current Chrome/Firefox, but check it in the target browser before making it load-bearing.
- Keep the data in typed arrays and materialise only the filtered subset for rendering. Do not hold the raw buffer + a parsed copy + a filtered copy at the same time — that is triple the memory; once the buffer is decoded, release the intermediate structures (`= null`).
- `applyFilters()` must recompute the view without re-reading or re-parsing the source.

### 6.5 Network and external dependencies

- **No CDNs** (§2): libraries go minified and inline in `<head>`/before `</body>`, or into a slot with a relative `src` (§7).
- **No web fonts**: `font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`. A font is a blocking resource plus FOIT/FOUT; for an internal report the system stack is indistinguishable.
- **No polling** (`setInterval(fetch, …)`): a report's data is static between slot replacements, and `fetch` from the iframe does not work at all (§2) — polling only hammers the server with a stream of 401s (`/admin/errors` filters them out as auth noise, but they heat the perf logs, and the client's battery even more).
- No endless retries. A retry is at most 2–3 attempts with exponential backoff, then a visible error state (§9).

---

## 7. Slots and relative resources

The render document canonically lives at `/render/{id}/` (with the slash), so **relative URLs inside the report work**: `<script src="app.js">` resolves to `/render/{id}/app.js`, and the platform serves it from the slot file whose **original name** matches (`app.js`; pre-compressed `app.js.br` / `app.js.gz` are allowed too — for a client without the codec the server decompresses itself). The MIME type is set correctly (including `.js` — Chromium ORB will not block it).

In practice:

- A large library can be moved into a slot (`chart.min.js.br`) and included with `<script src="chart.min.js">` — the document and its cache cell become light. But have no illusions about repeat visits: this request is made **from inside** the sandboxed iframe, whose HTTP cache is partitioned (§1), so the library travels over the wire on every open — just as a separate compressed response. Between opens only the data the parent downloads and passes in via `op_file` is cached (§4.2).
- A resource is addressed by **a single path segment**: `<script src="app.js">` works, `<script src="js/app.js">` does not (the `/render/{id}/{asset}` route does not accept subdirectories; slot names are flat). Keep relative links flat.
- **Do not version file names** (`data_v2.bin`): an update = replacing the file **in the same slot** through the Data panel. The platform invalidates by itself (stat key: mtime+size); `?v=` suffixes are unnecessary and harmful.
- Do not reference the platform's absolute paths (`/static/...`, `/api/...`) — the first is not for reports, the second is closed in the sandbox.
- A report that is missing a file (a 404 on a relative resource) is fixed by uploading that file into a slot, with no change to the HTML. But do not rely on it: ship the report complete from the start; every 404 is visible in `/admin/errors`.

---

## 8. Compatibility with platform features (Colors / Text / Annotate)

Controllers are injected into every render. A report written with their contracts in mind gets features for free; one written against them breaks them.

### 8.1 Colors

The owner can recolour a report from the UI; the saved theme is **applied automatically for every viewer**. The controller can rewrite CSS rules, inline styles, SVG `fill`/`stroke` and, best-effort, Chart.js/Plotly configs. For the recolouring to be perfect:

- **Every colour goes through CSS custom properties on `:root`** (`--bg`, `--accent`, `--series-1`…). Variables are shown in the UI as a separate list and are overridden more reliably than literal remapping (limits: 200 variables, 400 remap pairs — the excess is silently dropped).
- **Feed the charts colours from those same variables** (`getComputedStyle(document.documentElement).getPropertyValue('--series-1')`) and subscribe to the theme event:

```js
window.addEventListener('op:theme', () => {
  chart.data.datasets.forEach((d, i) => d.borderColor = cssVar('--series-' + (i + 1)));
  chart.update('none');
});
```

- Do not "bake" colours into raster images and canvas gradients — the controller will not recolour them.

### 8.2 Text editing (Text)

Only **leaf elements** (text nodes with no nested tags) are editable; edits are applied through `textContent` by CSS selector. Consequences:

- Make headings, captions and legends separate leaf elements (`<span>`, `<td>`, `<h3>` without nesting) with **stable** ids/classes — a selector that breaks when the report is regenerated loses its edit.
- Text that your JS generates after load is not always overwritten by the overrides — static captions win.

### 8.3 Annotate

Pins are anchored to the elements' CSS selectors. Stable, semantic ids (`#kpi-revenue`, `#chart-headcount`) keep annotations alive between versions of the report.

---

### 8.4 "Show in report" (Locate)

A chat pane lives next to the report; an answer in it has a "Show in report" button. There is nothing to implement — the `op_locate` command is handled by the injected controller. But it helps the author to know HOW it searches: **the search runs over the `textContent` of leaf elements** (including `th` and `caption`), the match is outlined for a second and a half, and nothing else is done to the document — the report belongs to someone else.

There is exactly one consequence, and it is about markup: **a number drawn in a `<canvas>` cannot be found.** If a KPI exists only as pixels, the button will honestly answer "not found". Keep the value as text as well — at least in a caption next to it.

### 8.5 Width: the chat takes a column

An open chat turns the viewer's layout into three columns, and the report gets `minmax(560px, 1fr)` — it shrinks rather than being covered. **A report with a fixed width of 1000–1200 px gets a horizontal scroll once the chat is open.** On screens narrower than 760 px the chat becomes a full-screen overlay and takes no width.

The rule is the same as it always was; it is simply tested more often now: the layout is fluid, no `min-width` on containers, tables get their own `overflow-x: auto`. The lower bound worth designing for is **560 px**.

---

## 9. Stability: a report must not be able to fail silently

1. **Every asynchronous wait has a visible state.** Until `op_file` arrives — a skeleton/"Loading data…"; on timeout (15 s is sensible) — an explicit message "The data file has not been loaded — contact the owner", not an endless spinner. Remember: for a new report the slot may not have been uploaded yet — that is a normal situation. And check the delivery chain: `op_file` comes only in reply to your `op_ready` (§2.1); send it on `load` and repeat it 1–2 times a few seconds apart if the data has not arrived (insurance against the race "a small report loaded before the parent attached its listener") — but not in an endless loop: every `op_ready` makes the parent download all the slots again.
2. **Per-section error isolation.** Each section's initialisation goes in its own `try/catch`; a failed chart shows an error plate in its own container while the other sections live on. One error in `init()` without isolation = a white screen for the whole report.
3. **Idempotent re-initialisation.** `op_file` for a slot may arrive again (the user of an interactive report replaced the file). `initFromBuffer` must be able to rebuild the view without duplicating handlers and DOM.
4. **Do not touch what is not yours.** Do not override `window.postMessage`, do not install a `message` handler with `stopImmediatePropagation`, do not remove the platform's injected `<script>` tags, do not override `localStorage` (it is already proxied).
5. **Memory.** A report may live in an open tab for hours: unsubscribe handlers when rebuilding, do not accumulate closures over large buffers, `disconnect()` a `ResizeObserver`/`IntersectionObserver` before recreating it.
6. **Every 4xx/5xx request from your report (except 401 — those are filtered out as auth noise) lands in `/admin/errors`**, grouped by dashboard, with the owner's name, the top failing paths and the time. A typical row there is a 404 on a relative resource someone forgot to upload into a slot. A clean report = no row in that table.

---

## 10. How to measure (the tools are already built in)

> **How to measure so that people believe you.** The platform numbers in this
> document are not eyeballed: they were taken on a dedicated stand under a
> protocol with validity gates (`errors=0`, rate deviation ≤ ±1%,
> `send_lag p99 < 100 µs`, `steal < 0.5%`), three accepted runs per cell, with
> the expected result pre-registered BEFORE the run. The decisions taken on
> the results are in `docs/PERF_DECISIONS.md`. If you are measuring your own
> report and want the result to count as a fact rather than an impression,
> use the same scheme: fix the expectation in advance, run three times, and
> compare only comparable quantities.


| Tool | What it shows | How to read it |
|---|---|---|
| `Server-Timing` | Server time (`app;dur`) and database time (`db;dur`) of every response | DevTools → Network → select the request → Timing tab. If `app;dur` is microseconds or a few ms and the user still finds it slow, the problem is in the report |
| `/admin/perf` | p50/p95/p99 per route, server µs metrics, **browser ttfb/fcp/load for each report**, the slowest requests | Open it after a few views of the report. `load` is your main metric |
| DevTools → Performance | The breakdown of your 2 seconds of init: parsing, scripts, layout, paint | Record the report opening; long tasks (> 50 ms, red triangles) are the targets for §6.1 |
| DevTools → Network | `size` (wire) vs `content` (raw): whether compression works; the response's `content-encoding` | If the document went out as `identity` while its content is text, look for a base64 blob (§5) |
| `performance.mark/measure` inside the report | Your own init milestones | Visible in a Performance recording (they do not reach the shared telemetry from the sandbox — this is your local tool) |

**Acceptance criteria before publishing** (all checkable in 5 minutes):

```
[ ] document wire size ≤ 300 KB (Network, Size column), content-encoding: br/gzip
[ ] data in a slot, pre-compressed (*.br/*.gz), or inline ≤ 200 KB
[ ] load ≤ 1 s on the second open (the first may pay for the warm-up)
[ ] Performance recording: no tasks > 200 ms, the frame is visible in the first frame
[ ] network switched off after load: the report works (no hidden CDNs)
[ ] /admin/errors: no rows for your dashboard after the test opens
[ ] colours through :root variables; op:theme redraws the charts
[ ] a report with slot data posts op_ready on load (otherwise op_file never comes)
[ ] an empty/missing slot shows a clear state, not an endless spinner
```

---

## 11. Skeleton of a correct report (snapshot + data slot)

A minimal frame that honours every contract in this document:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Report: sales by region</title>
<style>
  :root {                       /* §8.1: every colour as a variable */
    --bg: #0f1420;  --surface: #171e2e;  --text: #e8ecf4;
    --accent: #4f8cff;  --series-1: #4f8cff;  --series-2: #34c98e;
  }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; } /* §6.5: no web fonts */
  .card { background:var(--surface); border-radius:10px; padding:16px; }
  .err  { color:#ff6b6b; padding:24px; }
</style>
</head>
<body>
  <div id="kpi" class="card">Loading data…</div>   <!-- §9.1: a visible state -->
  <div class="card"><canvas id="chart" height="320"></canvas></div>
  <div class="card"><table id="tbl"><tbody></tbody></table></div>

  <!-- §4.2: an empty inline snapshot => snapshot type, the data comes in slot 0 -->
  <script id="bootstrap-snapshot" type="application/json">{}</script>

  <script>/* minified chart library INLINE (§6.5) or in a slot (§7) */</script>

  <script>
  'use strict';
  (function () {
    var inited = false, failTimer = setTimeout(function () {      // §9.1: a timeout instead of an endless spinner
      if (!inited) document.getElementById('kpi').innerHTML =
        '<div class="err">The data file has not been loaded — contact the report owner.</div>';
    }, 15000);

    // §2.1: op_file comes ONLY in reply to op_ready. Send it on load and
    // repeat twice — insurance against a race with the parent's listener.
    function announce() { window.parent.postMessage({ type: 'op_ready', slots: [0] }, '*'); }
    if (document.readyState === 'complete') { announce(); }
    else { window.addEventListener('load', announce); }
    setTimeout(function () { if (!inited) announce(); }, 2000);
    setTimeout(function () { if (!inited) announce(); }, 5000);

    window.addEventListener('message', function (e) {
      var m = e.data || {};
      if (m.type === 'op_file' && m.slot === 0) {                 // §2.1: the data arrives as op_file
        clearTimeout(failTimer);
        try { init(m.data); inited = true; }                      // §9.2/9.3: isolation + idempotency
        catch (err) { showError(err); }
      }
    });

    function init(buf) {
      var view = decodeColumns(buf);          // §4.3: typed arrays over the ArrayBuffer, no JSON.parse
      renderKpi(view);                        // §6.1: the frame — synchronous and immediate
      requestAnimationFrame(function () { renderChart(view); });   // charts — on the next frame
      renderTableChunked(view, 200);          // table — in portions, the main thread stays free
    }

    window.addEventListener('op:theme', function () {              // §8.1: live chart recolouring
      if (window.__chart) { applyCssVarColors(window.__chart); window.__chart.update('none'); }
    });

    /* decodeColumns / renderKpi / renderChart / renderTableChunked / showError — per §§4.3, 6.1–6.3 */
  })();
  </script>
</body>
</html>
```

At upload, one file goes with it: `sales.bin.br` (columnar format, brotli −q11) in slot 0.

---

## 11b. Where else your report opens (new in autumn 2026)

Until the autumn a report lived in one place — the viewer. Now there are four places, and the SAME `op_ready` → `op_file` contract works in all of them. There is nothing to add; there is only something not to break.

### 11b.1 As a single file, data included

The "Download current version" button used to hand out only `dashboard.html` — so an interactive report opened empty. Now there is a single-file download: the platform takes the same document the viewer serves, embeds the slot bytes as base64, and appends before `</body>` a bridge that sends your report the very same `op_file` on `load`.

**The protocol does not change — the sender does**, and that is exactly why a report written according to §4.2 downloads without a single edit. Worth knowing:

- `gzip` is decompressed by the browser, `brotli` by the bridge — the browser has no brotli decompressor. The file name says nothing about the encoding; go by `Content-Encoding`.
- A slot the document itself references (`.js`, `.css`) is spliced into the markup as an asset rather than sent as a message.
- The ceiling is 64 MB for the finished file; base64 adds a third, and the document is opened in memory whole.
- An RLS report has no such button at all, and neither does a "highly confidential" one: such a file opens without a password and lives on by itself.

### 11b.2 Desktop mirror

Published reports are synchronised to a person's machine and open there while the server is down — from a local `http://127.0.0.1`, not from `file://`. The reason is the same sandbox: a `file:` page cannot download a slot, and an iframe with an opaque origin gets neither assets nor subresources.

For the author this means: **no absolute links to the production host** inside the report. Relative asset names (§7) work in the mirror just as they do on the platform; `https://onepage.example.com/...` does not. Reports with RLS never get into the mirror.

### 11b.3 Behind a password

A report can have a second lock on top of the access list. While that mode is on, the report has the following switched off: outside links, single-file download, the copy in the mirror, the source, and the chat. Otherwise the report is ordinary — there is no need to write it differently; just do not promise users a share link to such a report.

### 11b.4 A version rollback takes the data from the CURRENT version

The "Restore" button in the Versions panel puts the old HTML back as a **new** version, moving forward, and copies the slot files from the current one. So a rollback brings back the layout and the formulas, but not last year's numbers. The practical conclusion for the author: the report's code must survive data newer than itself — not crash on a column it did not expect, but say so (§9.2).

---

## 12. Anti-pattern summary table

| Anti-pattern | How it is punished | Do instead |
|---|---|---|
| Data/base64 inside the HTML | An incompressible document, identity, full size over the wire on **every** open | A data slot, pre-compressed (§4) |
| `JSON.parse` of a multi-megabyte string | Hundreds of ms of freeze + a 3–5× memory peak | Binary columnar format + typed arrays (§4.3) |
| The whole render in one synchronous pass | load 2–3.5 s (incident `a0099a9a`) | The frame at once, heavy parts in chunks (§6.1) |
| Tens of thousands of DOM nodes | Scroll freezes, slow platform controllers | Virtualisation/chunking (§6.2) |
| An SVG chart with 100k points | Seconds of layout | Canvas + decimation (§6.3) |
| `<script src="https://cdn…">`, Google Fonts | The report does not open for some intranet users | Everything inline or in a slot (§6.5, §7) |
| `setInterval(fetch)` / endless retries | A 401 storm, a row in `/admin/errors` with your name | Data through op_file; at most 3 retries (§6.5, §9) |
| Colours hard-coded as literals all over the file | The Colors feature recolours only part of it | `:root` variables + `op:theme` (§8.1) |
| A snapshot report with a slot that never posts `op_ready` | The data never arrives — "Loading…" forever for every viewer | `op_ready` on `load` + 1–2 repeats (§2.1, §11) |
| An endless spinner when the data is missing | "The report is stuck", as the user sees it | Timeout + a clear state (§9.1) |
| Megabytes in the localStorage proxy (interactive) | A bloated document, cache rotation at every sneeze | ≤ 4 KB of settings (§3) |
| `localStorage` in a snapshot/static report | `SecurityError` on load — the report crashes for everyone | State in JS; persist via the snapshot (§2) |
| A fixed width of 1000–1200 px | Horizontal scroll as soon as the viewer opens the chat | A fluid layout, lower bound 560 px (§8.5) |
| A KPI that exists only as pixels in a `<canvas>` | The "Show in report" button cannot find it | Keep the value as text too (§8.4) |
| Absolute links to the production host inside the report | The report opens broken in the desktop mirror | Relative asset names (§7, §11b.2) |
| A non-empty `bootstrap-snapshot` in a report with RLS | Publication is refused; otherwise the rows would reach everyone | interactive + an empty snapshot (§4.2) |
| Versioned data file names (`data_v2.bin`) | Dead 404 links, manual invalidation | Replace the file in the same slot (§7) |

---

## 13. Prompt block for an LLM (copy it when generating a report)

```
Generate a single-page HTML report for the OnePage platform. Hard requirements:
1. It runs in a sandboxed iframe WITHOUT allow-same-origin: no fetch to /api,
   no cookies; do NOT touch localStorage (in this environment it throws
   SecurityError) — keep state in JS variables.
2. No external resources: no CDN, no Google Fonts. Libraries go inline,
   minified. Fonts: the system-ui stack.
3. Do NOT embed the data in the HTML. Insert an empty
   <script id="bootstrap-snapshot" type="application/json">{}</script>
   and receive data by message: window.addEventListener('message', e =>
   e.data.type === 'op_file' && e.data.slot === 0 → init(e.data.data /*ArrayBuffer*/)).
   MANDATORY: after load, send window.parent.postMessage({type:'op_ready',
   slots:[0]}, '*') and repeat at 2 s and 5 s if no data came — the platform
   sends op_file ONLY in reply to op_ready.
   Data format: binary columnar (typed arrays) or pre-compressed JSON. No base64 blobs.
4. HTML ≤ 300 KB. Initialisation: the frame (KPIs, layout) synchronously and at
   once; charts in requestAnimationFrame; tables in chunks of 200 rows via
   setTimeout(0); no synchronous JS piece longer than 50 ms; initial DOM ≤ 3000 nodes.
5. Charts: canvas (not SVG) above 1000 points, initial animations off,
   series decimated to ~2× the canvas width.
6. All colours as CSS custom properties on :root (--bg, --accent, --series-N);
   charts read colours from those variables and redraw on the window event
   'op:theme' (chart.update('none')).
7. Resilience: a skeleton until data arrives; 15 s timeout → a visible error
   message; every section's init in its own try/catch; a repeated op_file for
   the slot rebuilds the report idempotently.
8. Do not intercept message events globally, do not override
   localStorage/postMessage, do not send network requests on a timer.
9. Fluid layout that works from 560 px wide: a chat pane opens next to the
   report and takes a column. No fixed widths or min-width on containers; wide
   tables in their own container with overflow-x: auto.
10. Every key number must exist as TEXT, not only as pixels in a canvas: the
    chat's "Show in report" button works on text.
11. No absolute links to the platform host: the same file opens from a desktop
    mirror at a local address. Assets by relative names.
12. The code must survive data newer than itself: a version rollback restores
    the old HTML while the slot files stay current. An unexpected column is a
    clear message, not an exception.
```

---

*This document is based on the platform's measured behaviour (perf telemetry from `/admin/perf`, incidents of June–July 2026). Server-side contracts: `app/api/files.py` (render cache, slots), `app/services/encoding.py` (compression), `app/services/detector.py` (detection, injections, Locate and the navigator link interceptor), `app/services/standalone.py` (single-file download), `app/services/rls.py` (row-level security), `app/middleware.py` (fast lane). User documentation on report types and uploading is in the application, on the `/docs` page.*

*Revised 2026-11-03. Previous revision: 2026-07-20; since then reports have gained the chat with its "Show in report" button, single-file download, the desktop mirror, the password mode, row-level security and a fourth page type — all of it added in §4.1, §4.2, §8.4–8.5 and §11b. If you change the platform in a way that alters one of these contracts, update this file too: a discrepancy here costs more than one in the code, because people and models write reports from it.*
