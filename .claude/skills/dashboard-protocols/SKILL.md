---
name: dashboard-protocols
description: Full postMessage contract between the dashboard iframe and the parent viewer — op_ready/op_file/op_save_file/op_height/op_save_ls/op_save_snapshot, plus the Annotate, Theme/Colors and Edit-text controller protocols and the screenshot constraint. Load when working on viewer.js, share.html, the injected controllers in services/detector.py, or anything that crosses the iframe boundary.
---

# Dashboard ↔ viewer postMessage protocols

The dashboard runs in an iframe sandboxed **without** `allow-same-origin`, so it
has an opaque origin and cannot reach the parent's cookies, storage, or the API.
Everything crossing that boundary goes through `postMessage`, and this file is
the contract.

Two invariants also live in the root `CLAUDE.md` because they are safety rules,
not reference material: text overrides are applied via `textContent` and never
`innerHTML`, and `share.html` deliberately has no write handler, so share-link
visitors can never write.

## Core protocol

- `op_ready` — iframe → parent on load; carries `slots` array
- `op_file` — parent → iframe; `{slot, name, data: ArrayBuffer}`
- `op_save_file` — iframe → parent; `{slot, name, data: ArrayBuffer}`; parent PUTs to server if user has `can_update_data`
- `op_height` — iframe → parent; content height for iframe auto-resize
- `op_save_ls` — iframe → parent; localStorage proxy state; parent PUTs if `can_update_data`
- `op_save_snapshot` — iframe → parent; full snapshot object from an in-page-editable **snapshot** dashboard (e.g. the HR roadmap); `viewer.js` PUTs it (as `snapshot.json`) to `PUT /api/dashboards/{id}/snapshot` if `CAN_UPDATE_DATA`, so edits persist across reloads. Owner/admin only — `share.html` has no handler, so share-link visitors can never write. The endpoint also re-checks `can_update_dashboard_data` + `dash_type == snapshot` + valid JSON.

## Annotate protocol

Owner/admin "Annotate" feature; dormant controller injected via
`inject_annotate` into every render.

- `op_annotate_on` / `op_annotate_off` — parent → iframe; enable/disable element picking (hover highlight + click capture)
- `op_pick` — iframe → parent; `{annotation: {id, selector, tag, classes, elId, text, html, styles, rect}}`
- `op_annotate_remove {id}` / `op_annotate_clear` — parent → iframe; drop numbered pins

The Annotate UI (viewer.js `ANNOT` module + dock "Annotate" tab) collects pins, lets the user describe a change per pin, and builds a copy-paste prompt (optionally including the raw source from `GET /api/dashboards/{id}/source`) for any external LLM. The source is fetched **lazily** — first Annotate open (`setPicking`), checkbox toggle, or prompt build — never on plain viewer opens (an eager fetch used to pull the full document, tens of MB for giants, on every `/d/{id}` visit just for a size label). Nothing is sent off-server — the prompt is copied to the clipboard for manual paste.

## Locate protocol ("Show in report")

Same injected controller as Annotate (`inject_annotate`), one extra command —
it works for every viewer, no picking mode, and needs no new iframe rights.

- `op_locate {id, q, kpi, value, labels[]}` — parent → report iframe. Finds the place that shows a value or a metric and flashes it: `scrollIntoView({block:'center', behavior:'smooth'})` + class `op-located` (2px `currentColor` outline, 6px radius, fades over 1.6 s, no animation under `prefers-reduced-motion`), removed after 2 s.
- `op_located {id, ok, tag, text}` — report iframe → parent. `text` is the first 60 chars of the found node; `ok:false` (with `tag`/`text` empty) means nothing matched — the chat must say so rather than pretend.

Search order over leaf elements, headings, `th` and `caption` (`script`/`style`/
`noscript`/`template`/`iframe`/`canvas`/media subtrees skipped, SVG `text` kept,
20 000 elements max so a multi-megabyte report cannot hang): **(1)** `value` — as a
folded substring, and as a number, so `1 234,6` in the report matches `1234.567`
from the answer (each numeric token in the text is parsed with either separator
convention and compared to the target rounded to that token's decimals; a unit
or currency around the value — «99,99 %», «$1 234» — is stripped; a share is
also tried ×100 and ÷100). **A bare number is never enough**: among the cells
that carry it, the one whose CONTEXT — its table row plus the header of its
column (outside tables: the three nearest containers) — mentions the most of
`kpi` + `labels[]` wins; when several cells carry the number and none carries
any of the context, the answer is `ok:false` rather than the first «100,0%»
on the page (owner, 2026-09-07). **(2)** without a value: `kpi` + `labels[]` —
case-insensitive, whitespace-collapsed substring, longest label wins, shorter
node wins on a tie. `q` (the question) is carried for
context but never searched — a whole sentence never occurs in a cell.

The chat lives in a *different*, same-origin iframe (`/explore?…&mode=chat&embed=1`)
and cannot reach the sandboxed report window at all. `viewer.js` (`chatLocateRelay`)
is the only bridge: chat → parent `{type:'op_chat_locate', id, q, kpi, value, labels}`
(origin- and source-checked, every field coerced to a ≤200-char string, ≤20 labels)
→ report `op_locate`; report `op_located` → chat
`{type:'op_chat_located', id, ok, tag, text, reason}`. No answer within 1.5 s ⇒
`{ok:false, reason:'timeout'}`; a miss ⇒ `reason:'not_found'`.

**Both sides time out, and they must stay in this order.** `viewer.js` answers
the chat itself after **1.5 s** of silence from the report (`chatLocateRelay`,
one timer per request id, an answer clears it — the id is answered exactly
once). The chat frame keeps its own **2 s** timer (`LOC_TIMEOUT_MS` in
`explore-chat.js`, overridable via `window.__opChatLocTimeout` for the node
harness) for the one case the protocol cannot see: a parent that never
answers at all — an old viewer without the relay, or a torn-down pane. The
chat's timer must stay the longer of the two, otherwise the honest
`reason:'not_found'` from the relay loses the race against the chat's own
`'timeout'` and the button says "the report did not answer — try again" about a report that
answered.

**What `labels[]` actually is.** The chat sends the label part of the answer's
**first 8 rows** — for each row, the values of every column except the value
column and the service ones, joined with « · » (`rowLabel` in
`explore-chat.js`) — **plus the filter values the question named** (the
intent's `filters[].value`, e.g. «Magnit», «23», and for a week filter also
«W23», the way reports print weeks), ≤20 labels in all. That is the ONLY data from the answer that crosses into
the report frame, alongside `value` (the first row's number, formatted) and
`kpi` (the metric's display name). **The SQL and the result set never cross
it** — not the full row list, not the query text, not the provenance. The
report frame is a stranger's document: everything it receives it can read.

## Theme / Colors protocol

Owner/admin "Colors" feature; controller injected via `inject_theme` into every
render.

- The controller is seeded with the saved theme (`window.__OP_THEME__`) and **auto-applies it on load for every viewer** (the published report is recoloured for everyone).
- `op_theme_scan` — parent → iframe; collect changeable colours + colour custom-properties.
- `op_theme_palette` — iframe → parent; `{colors:[{value,count}], vars:[{name,value}]}` (values are canvas-normalised, e.g. `#rrggbb`).
- `op_theme_apply {theme:{vars,remap}}` — parent → iframe; live preview. `vars` overrides `:root` custom properties; `remap` maps a normalised old colour → new colour, applied by rewriting accessible stylesheet rules, inline `style`, and SVG `fill`/`stroke`. The controller caches an original baseline and restores it before each apply, so re-previews are idempotent.
- **Chart.js / Plotly:** the controller also recolours canvas charts best-effort via their API — it walks `chart.config` / `gd.data`+`gd.layout` for colour-keyed string/array slots (`*Color`, `fill`, `stroke`, `bgcolor`), remaps them and calls `chart.update('none')` / `Plotly.react(...)`. These chart colours are also surfaced in the palette scan. Gradients, colour callbacks, `colorscale`, baked images and CDN stylesheets are skipped.
- **`op:theme` event:** after every apply the controller dispatches `window` CustomEvent `op:theme` with `{detail:{vars,remap}}`. Dashboard authors who drive chart colours from CSS variables can listen for it and redraw for perfect chart theming (documented in the Dev Guide `/docs#colors`).
- **Colour inspector:** `op_theme_pick_on`/`op_theme_pick_off` (parent → iframe) toggle a hover-highlight + click mode; on click the controller posts `op_theme_picked {colors:[normalised…], tag}` with the element's computed colours, and the THEME module highlights (`.theme-row.hit`) and scrolls to the matching palette/variable rows. Picking auto-stops when the Colors tab is left or the dock is closed.

The Colors UI (viewer.js `THEME` module + dock "Colors" tab, plus the `/themes` page → `/d/{id}?dock=colors`) offers per-colour pickers, theme-variable pickers, and one-click presets (Corporate/Dark/Light — mapped by nearest luminance). Themes persist per-version as `theme.json` (validated server-side: every value must be a colour, var names `--[\w-]+`, so the injected `<style>`/seed can't carry a CSS/script injection). "Download theme.json" exports the raw theme so it can be handed to an LLM alongside the source (download current version) when regenerating the report.

## Edit text protocol

Owner/admin "Text" feature; controller injected via `inject_textedit` into every
render.

- The controller is seeded with saved overrides (`window.__OP_TEXT__`, a `{selector: text}` map) and **auto-applies them for every viewer** on DOMContentLoaded/load + delays (idempotent — only leaf elements with no child tags are touched; an `ORIG` baseline restores removed selectors).
- `op_text_edit_on` / `op_text_edit_off` — parent → iframe; toggle edit mode. In edit mode, hovering highlights leaf text elements; clicking one makes it `contentEditable`; Enter confirms, Esc cancels.
- `op_text_edited {selector, text, original}` — iframe → parent; emitted when an edit is confirmed. The `TEXT` module collects them into a `{selector: text}` map.
- `op_text_apply {edits}` — parent → iframe; live preview / reconcile (restores selectors removed from the map).
- Overrides are applied via **`textContent`** (never `innerHTML`), and the seed escapes every `<` in the JSON to a unicode escape, so user text can neither inject HTML into the dashboard nor break out of the seed `<script>`. Persisted per-version as `text.json` (`{"edits": {...}}`; selectors/length capped server-side). Charts and JS-generated text aren't editable.

## Screenshots

html2canvas cannot run inside the dashboard iframe — the sandbox has no `allow-same-origin`, so its opaque origin breaks html2canvas's clone-iframe (and `foreignObject` taints the canvas). Instead, "Capture annotated screenshot" uses the parent-side Screen Capture API (`getDisplayMedia({preferCurrentTab:true})`), grabs one frame, and crops it to the `#dash-frame` rect. The numbered pins (drawn inside the iframe) are visible in the capture, so a single image conveys which element is which. Requires a one-time share-permission prompt and a secure (HTTPS) context.
