# OnePage Dashboard Spec — instructions for an LLM (Claude / GPT)

> **How to use this (for humans).** Attach this file to your request in Claude or ChatGPT and add: (1) a description of the data — columns, types, approximate volume, sample rows; (2) which charts/tables/KPIs you need; (3) the layout and any visual preferences. The model must generate a report that honours every contract below. Everything else in this file is addressed to the model.

---

## 0. Role and task

You are an engineer who builds single-page analytical dashboards. Your task is to generate **one self-contained HTML file** (plus, if needed, a script that prepares the data file) for the internal **OnePage** platform. The platform serves the file from cache in microseconds; all perceived load time comes from the size of your file and from your JavaScript. The requirements below are not recommendations but contracts: breaking them makes the report slow, broken or impossible to recolour, and the platform owner sees it in the telemetry.

Targets for the finished report: **≤ 300 KB of HTML**, **full initialisation ≤ 1 s**, no main-thread task longer than 50 ms, zero external network dependencies, zero console errors.

---

## 1. Runtime environment contract (must not be broken)

The report runs in an iframe with the attribute:

```html
sandbox="allow-scripts allow-forms allow-downloads allow-popups allow-modals"
```

`allow-same-origin` is **absent** → the origin is opaque. That gives you some hard facts:

| Environment fact | What it means for your code |
|---|---|
| Cookies are unavailable | No use of `document.cookie` at all |
| `fetch('/api/...')` returns 401 | Data arrives **only** via postMessage (`op_file`, §3) or relative URLs (§4). No requests of your own to the platform API |
| The external network may be closed | **CDNs, Google Fonts and any absolute external URLs are forbidden.** Libraries go inline, minified, or into a slot (§4). Fonts — system stack only |
| The real `localStorage` is blocked, but the platform injects a transparent proxy | You may use `localStorage.getItem/setItem` as usual, but **only for settings ≤ 4 KB** (selected tab, filters). The state is inlined into the document on every render — large volumes bloat it |
| The browser HTTP cache is partitioned per open; the document is served with `no-store` | **Every open of the report pays for the whole transfer again.** File size always matters; "it will be cached after the first time" does not exist here |
| The platform itself injects into `<head>`: the height reporter (op_height) and the annotation, theme and text-edit controllers | **Do not duplicate** the height reporter; do not override `window.postMessage` or `localStorage`; do not register a global `message` handler that swallows other types; do not remove the injected scripts |

### 1.1 postMessage protocol (iframe ↔ parent page)

| Message | Direction | Purpose |
|---|---|---|
| `op_ready {slots}` | iframe → parent | Sent by the platform's injected bridge after load. For the interactive type this is automatic; you do not need to send it |
| `op_file {slot, name, data: ArrayBuffer}` | parent → iframe | Contents of a slot file. **The main data-delivery channel** |
| `op_save_file {slot, name, data}` | iframe → parent | A file the user changed, to be saved (interactive) |
| `op_height {height}` | iframe → parent | Auto height. Already implemented by the platform |
| `op:theme` — a CustomEvent on `window` **inside** the iframe | platform → your code | A theme was applied/changed. You must redraw the charts (§6) |

---

## 2. Report type detection (regex detection — be precise)

The platform decides the type from the **raw text of the file**, including comments and JS string literals:

1. There is a `<script id="bootstrap-snapshot">` → type **snapshot**;
2. otherwise there is an `<input type="file">` → type **interactive**;
3. otherwise → **static**.

Rules:

- Choose the type deliberately and avoid accidental matches: the string `'<input type="file"'` inside your JS code will make the report interactive.
- **Generate snapshot by default**, with an empty inline snapshot and the data in a slot (§3.2) — it is the fastest and most updatable scheme.
- Choose **interactive** only if the user must load/change data files themselves through `<input type="file">` (the platform injects a bridge: files arrive as ordinary `change` events, changes are saved via `op_save_file`).
- **static** — only for reports with no data at all, or with a trivial amount baked into the markup.

---

## 3. Data architecture (the most important decision)

### 3.1 Selection rule

| Data volume | Scheme |
|---|---|
| ≤ ~200 KB of JSON | Inline: `<script id="bootstrap-snapshot" type="application/json">{ …data… }</script>`. The server substitutes the current JSON there on every render; the data is available synchronously: `JSON.parse(document.getElementById('bootstrap-snapshot').textContent)` |
| More | **Data slot** (mandatory scheme for large data): in the HTML, an empty `<script id="bootstrap-snapshot" type="application/json">{}</script>`; the data goes in a separate pre-compressed file that the person uploads to "slot 0". After the report loads, the parent sends `op_file` with an `ArrayBuffer` by itself |

### 3.2 Why data is never embedded in the HTML in large blocks

- The document travels over the wire on **every** open (§1). A slot file is served with a strong ETag — reopening costs a 304.
- Updating the data = replacing the file in the same slot through the platform UI, without regenerating the HTML. The file name stays the same on update; **no `data_v2.bin`** — the platform invalidates the cache itself by mtime.
- **Base64 blobs in the HTML are forbidden.** The server compresses responses (brotli/zstd/gzip), but before compressing it takes a sample from the middle of the body: if the sample is incompressible (and base64 is incompressible), **the whole document goes out uncompressed**. One megabyte of base64 blob deprives all the surrounding markup of compression too. The same applies to data: URI images and fonts.

### 3.3 Data file format: binary columnar (reference)

For datasets from ~50k values, `JSON.parse` is forbidden (hundreds of ms of main-thread blocking, 3–5× peak memory). Use a columnar binary format with zero-copy typed-array access. Reference specification **OPB1** (implement both sides):

```
[0..3]   magic "OPB1" (ASCII)
[4..7]   uint32 LE: length of the JSON header H
[8..8+H) JSON header (utf-8), then zero padding to a multiple of 8 bytes
[data]   data section: columns back to back, each aligned to 8 bytes

Header: {
  "rows": N,
  "cols": [ {"name": "...", "type": "f64"|"i32", "offset": <bytes from the start of data>}, ... ],
  "dicts": { "<string column name>": ["value0", "value1", ...] }
}
String columns are encoded as an i32 column of indices into their own dictionary in "dicts".
```

**Python encoder** (give it to the user next to the HTML if the data is large):

```python
import json, struct

def encode_opb1(rows: int, cols: list[tuple[str, str, bytes]], dicts: dict) -> bytes:
    # cols: [(name, "f64"|"i32", raw_little_endian_bytes)]
    data, meta, off = bytearray(), [], 0
    for name, typ, raw in cols:
        pad = (-off) % 8
        data += b"\0" * pad; off += pad
        meta.append({"name": name, "type": typ, "offset": off})
        data += raw; off += len(raw)
    header = json.dumps({"rows": rows, "cols": meta, "dicts": dicts},
                        ensure_ascii=False).encode()
    hpad = (-(8 + len(header))) % 8
    return b"OPB1" + struct.pack("<I", len(header)) + header + b"\0" * hpad + bytes(data)

# Numeric columns: struct.pack(f"<{n}d", *values) for f64, f"<{n}i" for i32.
# String columns: dictionary uniq = sorted(set(vals)); indices -> i32; dictionary -> dicts[name].
# Compress the result: brotli.compress(payload, quality=11) -> file data.bin.br
```

**JS decoder** (embed it in the report):

```js
function decodeOPB1(buf) {
  const u8 = new Uint8Array(buf), dv = new DataView(buf);
  if (String.fromCharCode(u8[0], u8[1], u8[2], u8[3]) !== 'OPB1') throw new Error('bad magic');
  const hlen = dv.getUint32(4, true);
  const header = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + hlen)));
  const base = 8 + hlen + ((-(8 + hlen)) % 8 + 8) % 8;   // padding to a multiple of 8
  const cols = {};
  for (const c of header.cols)
    cols[c.name] = c.type === 'f64'
      ? new Float64Array(buf, base + c.offset, header.rows)   // zero-copy view
      : new Int32Array(buf, base + c.offset, header.rows);
  return { rows: header.rows, cols, dicts: header.dicts,
           str: (col, i) => header.dicts[col][cols[col][i]] };
}
```

Compress the file to `data.bin.br` (brotli −q11) or `data.bin.gz` (gzip −9): the platform serves it with the correct `Content-Encoding`, the browser decompresses it transparently, and `op_file` delivers an already decompressed ArrayBuffer. A pre-compressed file is strictly better than a raw one: the platform compresses a raw file itself, but the first visit after every replacement pays for that time.

For data up to a few MB, pre-compressed JSON (`data.json.br`) is acceptable — `op_file` then delivers an ArrayBuffer with JSON: `JSON.parse(new TextDecoder().decode(buf))`, but only if the decompressed size is ≤ ~2 MB.

---

## 4. Relative resources (slots as the report's file system)

The document lives at `/render/{id}/`, so relative URLs work: `<script src="chart.min.js">` is served from the slot file whose original name is `chart.min.js` (pre-compressed `chart.min.js.br` / `.gz` are fine — the platform handles Content-Encoding and MIME).

- Put a large library (> ~150 KB minified) in a slot — the HTML stays light and the library gets ETag/304. Small ones go inline.
- Every relative URL for which the user does not upload a slot is a 404 in the platform's error log under the owner's name. **List for the user, in your answer, every file that must be uploaded as a slot, with exact names.**
- Links to `/static/...`, `/api/...` and any absolute platform paths are forbidden.

---

## 5. Execution budgets (JS discipline)

A real anti-case from the platform: a report with a 1 ms server time and a 140 KB transfer took 2.0–3.5 s to load on every open, because it built the whole DOM in one synchronous pass. Do not repeat it:

| Budget | Value |
|---|---|
| One synchronous main-thread task | ≤ 50 ms (target ≤ 16 ms) |
| DOM nodes in the initial render | ≤ 3,000 |
| Points on an SVG/DOM chart | ≤ 1,000; more — **canvas only** |
| Full `load` | ≤ 1 s |

Mandatory techniques:

1. **Skeleton instantly, heavy work frame by frame.** Render only the shell synchronously (KPI tiles, headings, empty containers); charts in `requestAnimationFrame`; tables in chunks:

```js
function renderTableChunked(tbody, rows, renderRow, chunk = 200) {
  let i = 0;
  (function next() {
    const frag = document.createDocumentFragment();
    for (const end = Math.min(i + chunk, rows.length); i < end; i++)
      frag.appendChild(renderRow(rows[i]));
    tbody.appendChild(frag);
    if (i < rows.length) setTimeout(next, 0);       // yield the main thread
  })();
}
```

2. **Large tables (> ~2,000 rows) — virtualisation**: only the visible rows plus a buffer are rendered, in a fixed-height scroll container, recalculated on `scroll` with `requestAnimationFrame` throttling. Never output tens of thousands of `<tr>`.
3. **Charts**: a canvas library (Chart.js / uPlot / ECharts-canvas); initial-render animations off (`animation:false`); decimate series to ~2× the canvas width in pixels (min/max decimation preserves the shape). One library for the whole report.
4. **Layout thrashing is forbidden**: do not interleave layout reads (`offsetHeight`, `getBoundingClientRect`) with style writes in a loop — all reads first, then all writes.
5. **Heavy aggregations** (> 50 ms) go to a Web Worker created inline (external worker files are unavailable in the sandbox):

```js
const worker = new Worker(URL.createObjectURL(new Blob([workerCode], { type: 'text/javascript' })));
```

6. **Memory**: keep the data in typed arrays; filters materialise only the displayed subset; do not hold the raw buffer, the parsed copy and the filtered copy at the same time; release intermediate structures after init (`= null`); on rebuild, remove handlers and `disconnect()` observers.
7. **No timer-driven network requests** (`setInterval(fetch)`), no endless retries: the data is static between slot replacements, and every failed request lands in the platform's error log under the owner's name.

---

## 6. Theming (mandatory integration with the Colors feature)

The platform lets the owner recolour the report from the UI; the saved theme is applied automatically for every viewer. The contract:

1. **Every colour used is a CSS custom property on `:root`** with a meaningful name:

```css
:root {
  --bg: #0f1420;  --surface: #171e2e;  --text: #e8ecf4;  --muted: #8b93a7;
  --accent: #4f8cff;  --good: #34c98e;  --bad: #ff6b6b;
  --series-1: #4f8cff;  --series-2: #34c98e;  --series-3: #f2b64c;  /* one variable per series */
}
```

Platform limits: ≤ 200 variables, ≤ 400 remap pairs. The rest of the CSS must contain no colour literals — only `var(--…)`.

2. **Canvas charts take their colours from the same variables** and redraw on the theme event:

```js
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function applyThemeToChart(chart) {
  chart.data.datasets.forEach((d, i) => {
    d.borderColor = cssVar(`--series-${i + 1}`);
    d.backgroundColor = cssVar(`--series-${i + 1}`) + '33';
  });
  chart.options.scales.x.ticks.color = chart.options.scales.y.ticks.color = cssVar('--muted');
  chart.update('none');                       // no animation
}
window.addEventListener('op:theme', () => charts.forEach(applyThemeToChart));
```

3. Do not "bake" colours where the theme controller cannot reach them: raster images, canvas gradients, colour callback functions.

In addition (the Edit text feature): make headings, captions and legends **leaf** elements (text with no nested tags — `<span>`, `<h3>`, `<td>`) with stable semantic ids (`#kpi-revenue`, `#chart-title-headcount`) — the owner can then edit the wording from the UI, and annotations survive regeneration of the report.

---

## 7. Fault tolerance (the report must never break silently)

1. **Every wait has a visible state.** Until `op_file` arrives — a skeleton / "Loading data…". A 15 s timeout → a clear message ("The data file has not been uploaded — contact the report owner"), not an eternal spinner. A missing slot on a freshly created report is a normal situation.
2. **Section isolation**: each section's init runs in its own `try/catch`; a failed chart shows an error banner in its own container, and the other sections keep working.
3. **Idempotence**: `op_file` for the same slot may arrive again (the user replaced the file) — init must rebuild the view without duplicating handlers or DOM (teardown first, then build).
4. **Zero console errors** on the happy path, and degradation without exceptions on empty/corrupt data (check the magic and the lengths when decoding).

---

## 8. Forbidden (summary list)

- External URLs: CDNs, Google Fonts, analytics, images over http(s).
- `fetch`/`XHR` to `/api/*` and absolute platform paths; any timer-driven network polling.
- Data > 200 KB, or base64/data: URI blobs inside the HTML.
- `JSON.parse` of strings > ~2 MB; parsing data on the main thread without chunking.
- SVG/DOM charts with > 1,000 points; DOM > 3,000 nodes in the initial render; tables without virtualisation/chunking.
- Synchronous JS chunks > 50 ms; initial-render chart animations.
- Colour literals outside `:root` variables; baking colours into rasters/gradients.
- Web fonts; use `font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`.
- Overriding `postMessage`/`localStorage`; a global `message` handler that swallows other messages; your own height reporter.
- `<input type="file">` or the string `bootstrap-snapshot` in the file unless the corresponding type was chosen deliberately (§2).
- Versioned data file names (`data_v2.bin`) — an update is always a replacement in the same slot.
- localStorage proxy > 4 KB.

---

## 9. Format of your answer (what you must deliver)

1. **The complete HTML file as one code block** — no placeholders and no "// rest of the code", ready to upload as is.
2. If the data is large — a **Python script that prepares** the data file (the OPB1 encoder from §3.3, or pre-compressed JSON) for the user's specific columns.
3. **Upload instructions** (3–5 lines): which file goes into which slot, with exact file names (including libraries moved into slots, §4).
4. **Self-check list** — before delivering the answer, check and explicitly confirm every item:

```
[ ] report type chosen deliberately; no accidental regex-detection matches (§2)
[ ] data: inline ≤ 200 KB OR empty bootstrap-snapshot + slot with a pre-compressed file (§3)
[ ] no base64/data: URI blobs and no external URLs in the HTML; system fonts (§8)
[ ] HTML ≤ 300 KB; initial DOM ≤ 3000 nodes (§5)
[ ] skeleton renders synchronously and instantly; charts in rAF; tables chunked/virtualised (§5)
[ ] canvas charts for > 1000 points, init animations off, decimation in place (§5)
[ ] all colours are :root variables; the op:theme handler redraws the charts (§6)
[ ] skeleton before data, 15 s timeout with a message, try/catch per section, repeated op_file is idempotent (§7)
[ ] headings/captions are leaf elements with stable ids (§6)
[ ] every slot file is listed for the user with its exact name (§9.3)
```

---

## 10. Reference skeleton (use it as the basis)

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Report</title>
<style>
  :root {
    --bg:#0f1420; --surface:#171e2e; --text:#e8ecf4; --muted:#8b93a7;
    --accent:#4f8cff; --series-1:#4f8cff; --series-2:#34c98e; --bad:#ff6b6b;
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:16px; background:var(--bg); color:var(--text);
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  .card { background:var(--surface); border-radius:10px; padding:16px; margin-bottom:16px; }
  .err  { color:var(--bad); padding:24px; text-align:center; }
  .skeleton { opacity:.45; }
</style>
</head>
<body>
  <div id="kpi" class="card skeleton">Loading data…</div>
  <div class="card"><h3 id="chart-title">Trend</h3><canvas id="chart" height="320"></canvas></div>
  <div class="card"><table id="tbl"><thead id="tbl-head"></thead><tbody id="tbl-body"></tbody></table></div>

  <!-- snapshot type; the data arrives in slot 0 -->
  <script id="bootstrap-snapshot" type="application/json">{}</script>

  <script>/* minified canvas charting library INLINE, or as a slot: <script src="chart.min.js"> */</script>

  <script>
  'use strict';
  (function () {
    let inited = false, chart = null;
    const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

    const failTimer = setTimeout(() => {                       // §7.1
      if (!inited) document.getElementById('kpi').innerHTML =
        '<div class="err">The data file has not been uploaded — contact the report owner.</div>';
    }, 15000);

    window.addEventListener('message', e => {
      const m = e.data || {};
      if (m.type === 'op_file' && m.slot === 0) {              // §1.1
        clearTimeout(failTimer);
        teardown();                                            // §7.3: idempotence
        try { init(m.data); inited = true; }
        catch (err) { showFatal(err); }
      }
    });

    function init(buf) {
      const d = decodeOPB1(buf);                               // §3.3: zero-copy typed arrays
      section('kpi',   () => renderKpi(d));                    // skeleton — synchronous
      requestAnimationFrame(() =>                              // charts — next frame
        section('chart', () => { chart = renderChart(d); }));
      section('tbl',   () => renderTableChunked(               // table — in chunks of 200
        document.getElementById('tbl-body'), buildRows(d), renderRow));
    }

    function section(id, fn) {                                 // §7.2: section isolation
      try { fn(); document.getElementById(id).classList.remove('skeleton'); }
      catch (err) {
        document.getElementById(id).innerHTML =
          '<div class="err">Section error: ' + String(err.message || err) + '</div>';
      }
    }

    window.addEventListener('op:theme', () => {                // §6.2
      if (chart) applyThemeToChart(chart);
    });

    /* decodeOPB1 — from §3.3; renderTableChunked — from §5.1;
       renderKpi / renderChart / buildRows / renderRow / applyThemeToChart /
       teardown / showFatal — implement for the user's task */
  })();
  </script>
</body>
</html>
```

---

## Appendix A. What happens to the file on the platform (for reference, to understand)

- Upload: one HTML ≤ 50 MB (hard limit, HTTP 413); the type is detected per §2; for a snapshot with an empty inline snapshot the form offers a "Data file — slot 0" field.
- Render `/render/{id}/`: the platform injects its controllers into `<head>`, caches the result in memory (invalidation is automatic, by the mtime of any constituent) and serves warm requests in single-digit microseconds through a pure-ASGI fast lane; the body is compressed with brotli(5)/zstd(6)/gzip(6) according to Accept-Encoding, with a compressibility probe (middle of the body, 64 KB, 95% threshold).
- Slots: `GET /render/{id}/files/{N}` and relative URLs; a strong stat-based ETag (a repeat is a 304 at the cost of one stat); `*.br`/`*.gz` are served with their Content-Encoding as is and decompressed on the server for clients without the codec; replacing a file — `PUT /api/dashboards/{id}/files/{N}` from the viewer's Data panel.
- Telemetry: the server writes `Server-Timing` into every response; the browser ttfb/fcp/load of every view and all 4xx/5xx of the report are visible to the administrator (`/admin/perf`, `/admin/errors`) together with the owner.
- A report heavier than 1.5 MB loses its live preview in the dashboard grid (a placeholder is drawn).

## Appendix B. Acceptance criteria after upload (for humans)

```
[ ] DevTools → Network: the document went out with content-encoding br/gzip; Size ≤ ~300 KB
[ ] second open: load ≤ 1 s (the first one may pay for cache warm-up)
[ ] DevTools → Performance: no tasks > 200 ms; the skeleton is visible in the first frame
[ ] offline test: after the data loads, disconnect the network — the report keeps working
[ ] /admin/errors: no rows for the dashboard after the test opens
[ ] changing the theme in Colors recolours both the markup and the charts
```
