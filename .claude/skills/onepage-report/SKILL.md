---
name: onepage-report
description: How to write a report that OnePage can host — one self-contained HTML file that takes its data over the platform's postMessage bridge, runs inside a sandboxed iframe with no network, and keeps working when opened from disk. Load whenever asked to create, generate, fix or review a report/dashboard for OnePage; also usable standalone — copy this folder into any project's .claude/skills/.
---

# Writing a report for OnePage

A OnePage report is **one HTML file**. The platform serves it inside an iframe
sandboxed without `allow-same-origin`, pushes its data in over `postMessage`,
and the same file must still open from a disk with no server at all. Every rule
below follows from those three facts; the engineering guide
(`REPORT_GUIDE.md`, §1–§2) explains why, and §11 holds a complete working
skeleton to start from.

## Five things that break a report outright

Check these before anything else — each one has shipped, and each one looks
like "the platform is broken":

| Check | Otherwise |
|---|---|
| No `localStorage` access | `SecurityError` on load — white screen for everyone |
| No `fetch` to APIs, no external resources (CDN, Google Fonts) | Does not open for part of the intranet |
| Data is NOT inlined in the HTML — it arrives through a slot | The whole document travels on every open, uncompressible |
| After `load`, post `op_ready` to the parent | Data never arrives — "Loading…" forever |
| Fluid layout from 560 px | Horizontal scroll the moment the chat pane opens |

## The hard requirements (put these in the prompt, verbatim)

```
Generate a single-page HTML report for the OnePage platform. Hard requirements:
1. It runs in a sandboxed iframe WITHOUT allow-same-origin: no fetch to /api,
   no cookies; do NOT touch localStorage (it throws SecurityError here) —
   keep state in JS variables.
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
    chat's "show in report" button works on text.
11. No absolute links to the platform host: the same file opens from a desktop
    mirror at a local address. Assets by relative names.
12. The code must survive data newer than itself: a version rollback restores
    the old HTML while the slot files stay current. An unexpected column is a
    clear message, not an exception.
```

## Report types, and the one constraint that is enforced

- **interactive** — reads a data file through a slot (`op_file`). The normal
  choice: the data is uploaded separately and refreshed without touching the HTML.
- **snapshot** — the numbers are inside the HTML. Fine for a small, static page.
- A report that will use **row-level security** must be `interactive` with an
  EMPTY `bootstrap-snapshot`: the platform refuses to publish otherwise,
  because inlined rows would reach every viewer unfiltered.

## Anti-patterns the platform punishes

| Anti-pattern | What happens | Do instead |
|---|---|---|
| Data / base64 inside the HTML | Uncompressible document, full size on every open | Data slot (§4) |
| `JSON.parse` of a multi-megabyte string | Hundreds of ms frozen, 3–5× memory peak | Binary columnar + typed arrays |
| Whole render in one synchronous pass | 2–3.5 s load | Frame first, heavy parts in chunks (§6.1) |
| Tens of thousands of DOM nodes | Scroll freezes, platform controllers slow down | Virtualise / chunk (§6.2) |
| SVG chart with 100k points | Seconds of layout | Canvas + decimation (§6.3) |
| `<script src="https://cdn…">`, Google Fonts | Does not open for part of the intranet | Everything inline or in a slot (§7) |
| `setInterval(fetch)` / endless retries | A 401 storm with your name in `/admin/errors` | Data through `op_file` only |
| Colours hard-coded all over the file | The Colors feature recolours only part of it | `:root` variables + `op:theme` (§8.1) |
| Slot report that never sends `op_ready` | Data never arrives for anyone | Send it after `load`, repeat at 2 s / 5 s |
| Spinner forever when data is missing | "The report hangs" | Timeout + an honest state (§9.1) |
| `localStorage` in a snapshot/static report | `SecurityError` on load for everyone | State in JS variables |
| Fixed width 1000–1200 px | Horizontal scroll once the chat opens | Fluid layout, lower bound 560 px |
| A KPI that exists only as canvas pixels | "Show in report" cannot find it | Keep the value as text too |
| Absolute links to the production host | Broken in the desktop mirror | Relative asset names |
| Versioned data file names (`data_v2.bin`) | Dead 404 links, manual invalidation | Replace the file under the same name |

## After the file is written

1. Upload it at `/upload` (or the Upload section of the workspace). The
   platform detects the type from the markup.
2. For an `interactive` report, upload the data file into slot 0 on the
   report's Data panel; the report receives it through `op_file`.
3. Check `/admin/errors` and `/admin/perf` after the first opens: a slow or
   broken report is listed there with its owner's name — that is the contract.

For the full reasoning, budgets and the measured numbers behind every rule,
read `REPORT_GUIDE.md` (the generated `REPORT_GUIDE.html` is the same text).
