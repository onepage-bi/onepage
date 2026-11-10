/* OnePage — browser generator engine (main thread).
 * RFC docs/RFC_GENERATORS_RESEARCH.md Appendix D (Stage 3).
 *
 * Mounts into #gen-engine-root (owner/admin Data dock). Owns the UI, the
 * File-System-Access folder picker + permission lifecycle, the C.6 engine
 * border, the run-lock POST/heartbeat/cancel, and publishing the Worker's
 * output back to the platform. ALL Python compute lives in gen-worker.js —
 * this file never touches Pyodide.
 *
 * Telemetry (D.7) rides the existing perf.js beacon: every op:gen:* measure
 * placed here is observed by perf.js and shipped to /api/perf/client on
 * pagehide. Stage marks live on the main thread only (the Worker ships stage
 * timings via postMessage — perf.js runs in the page, not the Worker).
 */
(function () {
  'use strict';

  // ── Pure observation-payload helpers ────────────────────────────────
  // No DOM/fetch — kept testable from node (tests/gen_engine_observe.js)
  // without the #gen-engine-root early-return below getting in the way.

  // undefined keys drop out of JSON.stringify. `checks`/`profiles` are the
  // heaviest fields (profiles carry up to 5 top values + distinct counts per
  // declared column) — shed alongside `schema` on a 413 so the rest of the
  // run's batches actually fit.
  // A file that HAD a schema keeps an explicit empty one with an error — the
  // server reads an absent `schema` as "unchanged since last run" and would
  // otherwise backfill from an older observation (or, if there is none,
  // leave a permanent gap: six prod files never got a schema because their
  // very first batch was stripped). `undefined` stays `undefined`.
  const STRIPPED_SCHEMA = { sheets: [], error: 'stripped to fit the observation batch cap' };
  function _stripSchema(f) {
    const schema = f.schema === undefined ? undefined : STRIPPED_SCHEMA;
    return Object.assign({}, f, { schema: schema, checks: undefined, profiles: undefined });
  }
  // Same idea for a stage: its profiles are the heavy half, the shape
  // (name/order/rows/columns/inputs/read_files) is what draws the graph.
  function _stripStage(s) { return Object.assign({}, s, { profiles: undefined }); }

  // Shrinks an observations POST body to fit `capBytes`, cheapest field
  // first — mirrors app/api/generators.py `_MAX_OBS_BODY`: the server
  // rejects a bigger body with a flat 413 before it even parses the JSON,
  // and the FINAL batch only ever gets ONE send (obsFinalSent forbids a
  // second), so a 413 on it would silently drop `final:true` and
  // observations.finalize() would never run for the whole run (review
  // finding). Order: (1) send as-is if it already fits; (2) strip
  // schema/checks/profiles from files and profiles from stages — same trade
  // the reactive 413-recovery path already makes; (3) drop stages outright
  // — narration, not load-bearing the way `final:true` itself is. Pure:
  // returns a new {payload, body, stripped} rather than mutating `payload`.
  function shrinkObservationsBody(payload, files, stages, alreadyStripped, capBytes) {
    let p = payload;
    let body = JSON.stringify(p);
    if (new Blob([body]).size <= capBytes) return { payload: p, body: body, stripped: alreadyStripped };
    let stripped = alreadyStripped;
    if (!alreadyStripped) {
      p = Object.assign({}, p, { files: files.map(_stripSchema) });
      if (stages) p.stages = stages.map(_stripStage);
      body = JSON.stringify(p);
      stripped = true;
      if (new Blob([body]).size <= capBytes) return { payload: p, body: body, stripped: stripped };
    }
    if (p.stages) {
      p = Object.assign({}, p);
      delete p.stages;
      body = JSON.stringify(p);
    }
    return { payload: p, body: body, stripped: stripped };
  }

  // ── Phase 9.3: the report index ("the lake") ───────────────────────────
  // After a SUCCESSFUL publish the run uploads the Parquet partitions of the
  // files it just read, so every viewer of the report can ask questions
  // without owning the folder (plan "Phase 9", ADR-14). Everything here is
  // non-fatal by construction: the report is already published when this
  // runs, and a failure only costs the server-side index.
  //
  // Kept out of the closure (like the observation helpers above) so
  // tests/gen_lake_dom.js can drive the whole sequence with a fake worker and
  // a fake fetch instead of a real Pyodide run.

  // Copy of explore.js's matchFilesToCatalog — same rule, by file NAME, one
  // record per catalogue partition (so one workbook with two sheets becomes
  // two records). Deliberately duplicated rather than imported: explore.js is
  // a page controller that mounts /explore's whole UI on load, and the run
  // dock must not pull that in. `dir` rides along here (explore.js hardcodes
  // 0) because a run may read several folders and the buffer has to come
  // from the right handle.
  function matchFilesToCatalog(files, tables) {
    const byName = new Map();
    (files || []).forEach(f => byName.set(f.name, f));
    const matched = [];
    const matchedNames = new Set();
    (tables || []).forEach(tv => {
      (tv.partitions || []).forEach(p => {
        const f = byName.get(p.file);
        if (!f) return;
        matchedNames.add(p.file);
        matched.push({
          dir: f.dir || 0, name: f.name, size: f.size, mtime_ms: f.mtime_ms,
          table_id: tv.table_id, table: tv.name || '', sheet: p.sheet || null,
          partition: p.key || null, header_row: tv.header_row || 0,
        });
      });
    });
    const unmatched = (files || []).filter(f => !matchedNames.has(f.name)).map(f => f.name);
    return { matched, unmatched };
  }

  // The whole step, as one awaitable. `ctx`:
  //   dashId, runId, pyodideBase, lakeEnabled (manifest.lake !== false)
  //   files      — the run's own file list ({name,size,mtime_ms,dir})
  //   readFile   — (rec) -> Promise<ArrayBuffer>, from the SAME handles
  //   send       — (type, payload, transfer) -> Promise<worker reply>
  //   setStatus  — (text) -> void, the run UI's status line
  //   showWarn   — (text) -> void, non-fatal warning
  //   fetch      — window.fetch (injectable for the node test)
  // Returns {skipped} | {ok, parts, bytes} | {error} — never throws.
  async function lakeStep(ctx) {
    const F = ctx.fetch;
    const dash = encodeURIComponent(String(ctx.dashId));
    const warn = (msg) => { try { ctx.showWarn(msg); } catch (e) { /* no UI */ } };
    const status = (msg) => { try { ctx.setStatus(msg); } catch (e) { /* no UI */ } };
    // Manifest opt-out first: it costs no request and it is the switch a
    // report author flips when the folder must not leave the laptop.
    if (!ctx.lakeEnabled) return { skipped: 'manifest' };
    // A generator with no semantics.json has nothing to answer questions
    // WITH, so an index of its folder would be dead weight on the server.
    // 404 here is the normal, silent case — no status, no warning.
    let sem;
    try { sem = await F(`/api/dashboards/${dash}/generator/semantics`); }
    catch (e) { return { skipped: 'semantics-unreachable' }; }
    if (!sem || !sem.ok) return { skipped: 'no-semantics' };

    let catalog;
    try {
      const r = await F('/api/explore/catalog');
      if (!r || !r.ok) return { skipped: 'no-catalog' };
      catalog = await r.json();
    } catch (e) { return { skipped: 'no-catalog' }; }
    const tables = (catalog && catalog.tables) || [];
    const { matched } = matchFilesToCatalog(ctx.files || [], tables);
    if (!matched.length) return { skipped: 'no-partitions' };

    const N = matched.length;
    try {
      status(t('Report index: 0/{p1}', {p1: N}));
      // The worker boots its /explore side lazily and refuses index/probe
      // before it has acknowledged an init.
      // `pyodideBase` matters: the worker's /explore side keeps its own
      // base (the run path's is a different variable) and boots from it.
      await ctx.send('explore:init', { pyodideBase: ctx.pyodideBase || '', catalog: { tables: tables } });
      // The worker owns the key recipe (python `_explore.cache_key`); this
      // page never computes one. `cached[i]` says the partition is already
      // Parquet in OPFS — that file is not read or parsed again.
      const probe = await ctx.send('explore:cache_probe', {
        files: matched.map(r => ({
          name: r.name, size: r.size, mtime_ms: r.mtime_ms,
          sheet: r.sheet, header_row: r.header_row,
        })),
      });
      if (probe && probe.error) throw new Error(probe.error);
      const keys = (probe && probe.keys) || [];
      const cached = (probe && probe.cached) || [];
      const parserVersion = String((probe && probe.parser_version) || '');

      const todo = [];
      for (let i = 0; i < N; i++) if (!cached[i]) todo.push(i);
      let done = N - todo.length;
      status(t('Report index: {p1}/{p2}', {p1: done, p2: N}));
      for (let k = 0; k < todo.length; k++) {
        const rec = matched[todo[k]];
        const isLast = k === todo.length - 1;
        let buf;
        // One file at a time — the worker writes each buffer straight to its
        // FS and drops it, and reading them all up front would hold the whole
        // folder in this tab at once (explore.js's own discipline).
        try { buf = await ctx.readFile(rec); }
        catch (e) { done++; continue; }   // vanished/locked: its key just never uploads
        const res = await ctx.send('explore:index', {
          files: [{
            dir: rec.dir, name: rec.name, size: rec.size, mtime_ms: rec.mtime_ms,
            table: rec.table, table_id: rec.table_id, sheet: rec.sheet,
            partition: rec.partition, header_row: rec.header_row,
            buf: buf, cache_key: keys[todo[k]] || null,
          }],
          final: isLast,
        }, [buf]);
        if (res && res.error) throw new Error(res.error);
        done++;
        status(t('Report index: {p1}/{p2}', {p1: done, p2: N}));
      }

      // Export EVERY key of this run, hits included: a cache hit means the
      // partition is already Parquet, not that the server has it.
      const wanted = [];
      const seenKey = new Set();
      for (let i = 0; i < N; i++) {
        const k = keys[i];
        if (!k || seenKey.has(k)) continue;
        seenKey.add(k);
        wanted.push(k);
      }
      const exported = await ctx.send('explore:cache_export', { keys: wanted });
      if (exported && exported.error) throw new Error(exported.error);
      const parts = (exported && exported.parts) || [];
      if (!parts.length) return { skipped: 'nothing-to-upload' };

      const sizeByKey = new Map();
      parts.forEach(p => sizeByKey.set(p.key, (p.buf && p.buf.byteLength) || 0));

      let bytes = 0;
      for (let i = 0; i < parts.length; i++) {
        const p = parts[i];
        status(t('Report index: {p1}/{p2}', {p1: (i + 1), p2: parts.length}));
        const fd = new FormData();
        fd.append('key', p.key);
        fd.append('file', new Blob([p.buf], { type: 'application/octet-stream' }), p.key + '.parquet');
        const r = await F(`/api/dashboards/${dash}/lake/parts`, { method: 'POST', body: fd });
        if (!r || !r.ok) {
          // First refusal ends the step: a partial upload with no manifest is
          // exactly what the server prunes on the next publish anyway.
          warn(t('Report index not loaded (HTTP {p1}) — published report unaffected.', {p1: ((r && r.status) || '?')}));
          return { error: 'part', status: (r && r.status) || 0, uploaded: i };
        }
        bytes += sizeByKey.get(p.key) || 0;
      }

      const entries = [];
      for (let i = 0; i < N; i++) {
        const key = keys[i];
        if (!key || !sizeByKey.has(key)) continue;   // never listed a key the server does not hold
        const rec = matched[i];
        entries.push({
          name: rec.name, size: rec.size, mtime_ms: rec.mtime_ms,
          sheet: rec.sheet || null, header_row: rec.header_row || 0,
          key: key, table: rec.table || '', partition: rec.partition || null,
          bytes: sizeByKey.get(key) || 0,
        });
      }
      const r = await F(`/api/dashboards/${dash}/lake/manifest`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: ctx.runId, parser_version: parserVersion, files: entries }),
      });
      if (!r || !r.ok) {
        warn(t('Report index not published (HTTP {p1}) — published report unaffected.', {p1: ((r && r.status) || '?')}));
        return { error: 'manifest', status: (r && r.status) || 0 };
      }
      const mb = (bytes / (1024 * 1024)).toFixed(1);
      status(t('Report index loaded ({p1} partitions, {p2} MB)', {p1: entries.length, p2: mb}));
      // The viewer's chat pane may be open already, with a snapshot that
      // predates this upload: tell it (same origin) so it loads the index.
      try {
        const fr = typeof document !== 'undefined' && document.getElementById('discuss-frame');
        if (fr && fr.contentWindow) fr.contentWindow.postMessage({ type: 'op_lake_published', dashboard_id: ctx.dashId }, location.origin);
      } catch (e) { /* no pane */ }
      return { ok: true, parts: entries.length, bytes: bytes };
    } catch (err) {
      warn(t('Report index not built: {p1} — published report unaffected.', {p1: String((err && err.message) || err || '').slice(0, 200)}));
      return { error: 'engine' };
    }
  }

  // A stored extra folder after restore: whatever the permission query said
  // (or threw), the folder STAYS — only `ready` changes. Pruning here is how
  // people's folders "reset by themselves".
  function restoredExtra(it, perm) {
    return { h: it.h, w: !!it.w, ready: perm === 'granted' };
  }
  // Expected folder names (server-observed) that no connected handle covers.
  function missingExpected(expected, connectedNames) {
    const have = new Set(connectedNames || []);
    return (expected || []).filter(n => !have.has(n));
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { shrinkObservationsBody, _stripSchema, _stripStage,
      matchFilesToCatalog, lakeStep, restoredExtra, missingExpected };
  }

  const root = typeof document !== 'undefined' ? document.getElementById('gen-engine-root') : null;
  if (!root) return;                    // non-owner grantee: no dock, nothing to do; also node test harness

  const DASH_ID = root.dataset.dashboardId;
  // The folders this report READS, as the server observed them on the last
  // runs (names only). They are listed whether or not THIS browser has ever
  // picked them: the analyst's job is «Reconnect», never «remember».
  const EXPECTED_DIRS = (function () {
    try { const v = JSON.parse(root.dataset.inputDirs || '[]'); return Array.isArray(v) ? v.map(String) : []; }
    catch (e) { return []; }
  })();
  const PYODIDE_BASE = root.dataset.pyodideBase || '';
  const ASSET_V = root.dataset.assetV || '';
  const FSA_OK = typeof window.showDirectoryPicker === 'function';
  // Chromium remembers the last-picked directory per (origin, picker id) across
  // sessions. The id is deliberately NOT per-dashboard: the case that hurts is
  // opening a COLLEAGUE'S report, where this profile has no handle for that
  // dashboard — and the folder you want is very likely the one you last picked
  // for some other report on the same share. A per-dashboard id would open the
  // picker at the OS default every single time in exactly that case.
  // A path can never be stored (the API exposes none, and no handle can be
  // rebuilt from a string), so steering the picker is all that is available.
  const PICKER_ID = 'opgen';
  // startIn takes a handle and wins over the remembered id. Once this profile
  // has its own handle for THIS dashboard, the picker opens exactly there.
  function pickerOpts(mode, suffix) {
    const o = { mode: mode, id: suffix ? PICKER_ID + suffix : PICKER_ID };
    if (dirHandle) o.startIn = dirHandle;
    return o;
  }

  // ── State ───────────────────────────────────────────────────────────
  let manifest = null, checksum = null, generatorId = null;
  // Data Lineage v2 (B1): the manifest's declared inputs (allow-list + glob
  // required-check) and the server's force-installed deps, both read once at
  // init and left untouched for the life of the page.
  let inputsDecl = [], forceDeps = [], checksDecl = [];
  // ADR-16 reference tables: what the server says exists (from the manifest
  // endpoint) and what this analyst chose to apply for the next run.
  let referenceDecl = [], refChoices = {}, refControls = [];
  let dirHandle = null, folderReady = false;
  let borderAck = false;   // analyst chose "Run anyway" past the scale advice
  // Extra input folders beyond the main one: {h, w, ready}. Main + MAX_EXTRA =
  // up to 8 folders; the C.6 file-count ceiling still governs the file total.
  const MAX_EXTRA = 7;
  let extraDirs = [];
  let slotsEl = null;
  // Which expected folder names are already covered by a connected handle.
  function connectedNames() {
    const names = [];
    if (dirHandle) names.push(dirHandle.name);
    extraDirs.forEach(x => names.push(x.h.name));
    return names;
  }
  // Rows for expected folders that are not on this browser at all: «📁 2026
  // — not connected here [Reconnect]». Reconnect opens the picker; the pick
  // is checked against the expected name before it is accepted.
  function renderSlots() {
    if (!slotsEl) return;
    slotsEl.textContent = '';
    missingExpected(EXPECTED_DIRS, connectedNames()).forEach(name => {
      const row = el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim);display:flex;gap:6px;align-items:center;flex-wrap:wrap' });
      row.appendChild(el('span', { style: 'word-break:break-all', text: '📁 ' + name + ' — not connected in this browser' }));
      const btn = el('button', { class: 'btn xs', type: 'button', text: 'Reconnect' });
      btn.disabled = running;
      btn.addEventListener('click', () => { connectExpected(name); });
      row.appendChild(btn);
      slotsEl.appendChild(row);
    });
  }
  async function connectExpected(name) {
    if (running) return;
    clearError();
    const asMain = !dirHandle;
    let h;
    // A picker id per expected folder: Chrome reopens the dialog where this
    // folder was last picked — the «where was it again?» half of the pain.
    let sfx = 0; for (let i = 0; i < name.length; i++) sfx = (sfx * 31 + name.charCodeAt(i)) >>> 0;
    try { h = await window.showDirectoryPicker(pickerOpts(asMain ? pickMode() : 'read', 'f' + sfx.toString(36))); }
    catch (e) { return; }                 // cancelled
    if (h.name !== name) {
      showError(t('Folder «{p1}» selected, but report reads «{p2}». Choose the matching folder or add it via «+ Add folder…».', {p1: h.name, p2: name}));
      return;
    }
    if (asMain) {
      dirHandle = h;
      await idbPut(DASH_ID, h).catch(() => {});
      btnReconnect.hidden = true;
      setFolder(h.name, true);
    } else {
      extraDirs.push({ h: h, w: false, ready: true });
      await persistExtras();
      renderExtras();
      refreshAddBtn();
    }
    renderSlots();
  }
  let auxDirHandle = null, auxFolderReady = false;   // optional second (aux) folder
  let worker = null, runId = null, heartbeatTimer = null, running = false;
  let currentFiles = [];
  let publishInfo = null;            // publish response; carries server-side warnings
  const seenPhase = new Set();

  // ── Observation queue (Data Lineage v2, B1) ─────────────────────────
  // The worker reports structure/consumption per file as it reads them; this
  // page batches those into POST …/runs/{id}/observations calls so the run
  // itself never waits on the lineage write. observationIds/lastKnown come
  // back from the run-start response and ride into the worker unchanged —
  // this page never interprets them itself.
  let observationIds = {}, lastKnown = {};
  let obsQueue = [], obsSeq = 0, obsTimer = null;
  // Pipeline stages (P4.1) arrive once, with the worker's last 'observe'
  // message, and ride out on the final:true batch — the server replaces the
  // whole set of a run on receipt, so they must travel together.
  let obsStages = [];
  let obsStripSchema = false;   // set true after a 413 — later batches shed the heaviest field
  // Every flush chains onto this promise instead of firing independently —
  // enqueueObservations() (size trigger), the 5s timer, and the final flush
  // in onDone/reportError can all be pending at once, and without a chain a
  // later seq's response can land before an earlier one, or `final` gets
  // posted while a data batch is still in flight (review finding: the
  // server's monotonic-seq/409-dup contract would then drop the earlier
  // batch instead of the intended duplicate). Chaining serializes them in
  // enqueue order, matching the seq numbers we hand out.
  let obsFlushChain = Promise.resolve();
  // onDone always sends the final:true flush before publish (queue's own
  // comment for why); if publish then fails, reportError sends its own
  // final:true too. Without this guard that's a second final for the same
  // run with a fresh seq — not documented as idempotent server-side (review
  // finding), so track it explicitly instead of relying on that.
  let obsFinalSent = false;
  // "This run has a source of observations". Two places raise it:
  //   * startWorker — the Pyodide worker observes by construction, and its final:true
  //     must be sent even if the batches turned out to contain no files;
  //   * enqueueObservations — any other sender that actually
  //     sent something.
  // A run that nobody observed (engine #2, GreenPlum) stays
  // false — and makes not a single empty POST …/observations.
  let obsActive = false;

  // ── Agent engine (#2, GreenPlum) ────────────────────────────────────
  // The agent runs the frozen SQL on the analyst's machine under their personal
  // credential; the page is the conductor (holds the run-lock, publishes). No
  // local folders — data comes from GreenPlum. See docs/RFC_AGENT_GREENPLUM.md.
  const AGENT_PORTS = [47113, 47114, 47115];
  const AGENT_PROTOCOL = 2;   // 2: pairing by approval (/pair/status), token survives restarts
  const AGENT_TOKEN_KEY = 'op-agent-token';
  let engineMode = 'browser';               // resolved from the manifest at init
  let agentBase = null, agentToken = null, agentRunId = null, agentAbort = null;
  let agentChipEl = null;
  const browserOnlyEls = [];                // folder controls hidden in agent mode

  // ── Run location (branch G) ──────────────────────────────────────────
  // The ENGINE is decided by the manifest (browser / agent), the LOCATION by the person, and these
  // are two different axes. `local` is the same agent, but instead of GreenPlum it reads
  // the folders declared in its own folders.ini (K1), and returns the result
  // the same way engine #2 does (K3).
  //
  // The switch is OFFERED only when: the manifest has no `sources`, the agent
  // was found on the right protocol and `GET /folders?dashboard=<id>` returned a section.
  // The default is always `browser`: for someone without an agent NOTHING may
  // change — not one new button, not one new request on the hot path
  // (the probe goes out AFTER folders are restored, at the very end of init).
  const LOCATION_KEY = 'op-gen-loc:' + DASH_ID;
  const SCENARIO_KEY = 'op-gen-scen:' + DASH_ID;
  let runLocation = 'browser';        // 'browser' | 'local'
  let folderInfo = null;              // the agent's last answer to GET /folders
  let folderScenario = '';            // the chosen set; "" — the unnamed one
  let locationRow = null, folderCardEl = null;
  // ── Catch-up (phase 7) ───────────────────────────────────────────────
  // An offline run lives with the agent and is unknown to the platform. The page
  // is the only one that can publish it: the agent has neither the person's session
  // nor the right to publish, and granting it that right would hand
  // a program on someone else's machine the key to the report's versions. So the queue
  // is ASKED for from here, and only with the person's explicit consent.
  let catchupEl = null;
  let catchupRuns = [];              // unpublished runs of this report
  let catchupBusy = false;
  // Soft limits of the `local` mode. The hard C.6 limits (500 files / 500 MB / 35 MB)
  // do NOT apply here: they exist because a browser tab does the computing,
  // not because the data is like that. These two only make a typo like
  // `dir = C:\` get caught before an hour of scanning, and they forbid nothing.
  const LOCAL_SOFT_FILES = 5000;
  const LOCAL_SOFT_BYTES = 20 * 1024 * 1024 * 1024;

  // ── Perf marks / measures (guarded — old browsers just skip) ─────────
  function mark(n) { try { performance.mark(n); } catch (e) { /* noop */ } }
  function measure(n, start, detail) {
    try {
      performance.measure(n, detail ? { start: start, detail: detail } : start);
    } catch (e) { /* missing mark / unsupported detail arg — telemetry is best-effort */ }
  }

  // ── Tiny DOM builder (createElement/textContent only — no innerHTML) ─
  function el(tag, props, kids) {
    const n = document.createElement(tag);
    if (props) for (const k in props) {
      if (k === 'class') n.className = props[k];
      else if (k === 'text') n.textContent = props[k];
      else if (k === 'style') n.style.cssText = props[k];
      else if (k in n) n[k] = props[k];
      else n.setAttribute(k, props[k]);
    }
    (kids || []).forEach(c => {
      if (c == null) return;
      n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return n;
  }

  const fmtMB = b => (b / (1024 * 1024)).toFixed(1);

  // ── UI scaffold ─────────────────────────────────────────────────────
  let refListEl = null;
  let wrap, titleEl, btnChoose, btnReconnect, btnRun, btnCancel, folderEl, limitsEl,
      statusEl, progressEl, diagEl, errEl, warnEl,
      btnAuxChoose, btnAuxReconnect, btnRunAux, auxFolderEl, auxHintEl,
      extraListEl, btnAddExtra, extraWriteCb;

  function buildUI() {
    wrap = el('div', { style: 'margin-top:12px;border-top:1px solid var(--border);padding-top:12px;display:flex;flex-direction:column;gap:8px' });

    titleEl = el('div', { class: 'dock-section-title', text: 'Run generator (browser)' });
    wrap.appendChild(titleEl);

    limitsEl = el('div', { class: 'dock-hint', style: 'margin-bottom:0' });
    wrap.appendChild(limitsEl);

    // Folder row
    btnChoose = el('button', { class: 'btn sm', type: 'button', text: 'Choose folder…' });
    btnReconnect = el('button', { class: 'btn sm', type: 'button', text: 'Reconnect folder', hidden: true });
    const folderRow = el('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;align-items:center' },
      [btnChoose, btnReconnect]);
    wrap.appendChild(folderRow);

    folderEl = el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim);word-break:break-all' });
    wrap.appendChild(folderEl);
    // Expected folders (from the server) that this browser has not connected
    // yet — one row each with its own «Reconnect». Filled by renderSlots().
    slotsEl = el('div', { style: 'display:flex;flex-direction:column;gap:4px' });
    wrap.appendChild(slotsEl);

    // Extra input folders (multi-folder input). One row per folder + an add row
    // whose checkbox decides the picker's read/readwrite mode at click time.
    extraListEl = el('div', { style: 'display:flex;flex-direction:column;gap:4px' });
    wrap.appendChild(extraListEl);
    extraWriteCb = el('input', { type: 'checkbox' });
    btnAddExtra = el('button', { class: 'btn sm', type: 'button', text: '+ Add folder…' });
    const addRow = el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center' },
      [btnAddExtra, el('label', { style: 'display:flex;gap:4px;align-items:center;font-size:11px;color:var(--ink-dim)' },
        [extraWriteCb, 'allow writing results'])]);
    wrap.appendChild(addRow);
    // Folder controls are browser-only: the agent engine hides them (GP data
    // has no local folder). Every OTHER element (run/cancel/status/progress/
    // error/diag) is shared by both engines.
    browserOnlyEls.push(folderRow, folderEl, extraListEl, addRow);

    // Reference tables (ADR-16): one row per platform input, filled by
    // renderReference() once the manifest endpoint has answered.
    refListEl = el('div', { style: 'display:flex;flex-direction:column;gap:4px' });
    wrap.appendChild(refListEl);
    browserOnlyEls.push(refListEl);

    // Run location + the agent's folder card. Both containers are born
    // empty and hidden: until probeLocal() PROVES there is something to
    // offer, the panel looks exactly as it did before this branch.
    locationRow = el('div', { style: 'display:none;flex-direction:column;gap:4px' });
    wrap.appendChild(locationRow);
    folderCardEl = el('div', { style: 'display:none' });
    wrap.appendChild(folderCardEl);
    // The catch-up inset sits NEXT TO the folder card, but NOT in browserOnlyEls:
    // the run is already computed, and it must be publishable regardless of where
    // the person intends to compute next time.
    catchupEl = el('div', { style: 'display:none' });
    wrap.appendChild(catchupEl);

    // "What we will read" sits ABOVE Run: it is a step before the run, not a report on it.
    btnPreview = el('button', { class: 'btn sm', type: 'button', text: t('What we will read…'),
      disabled: true, style: 'width:100%' });
    previewEl = el('div', { style: 'display:none' });
    wrap.appendChild(btnPreview);
    wrap.appendChild(previewEl);
    browserOnlyEls.push(btnPreview, previewEl);

    // Run / Cancel row
    btnRun = el('button', { class: 'btn sm primary', type: 'button', text: 'Run', disabled: true, style: 'width:100%' });
    btnCancel = el('button', { class: 'btn sm danger', type: 'button', text: 'Cancel', hidden: true, style: 'width:100%' });
    wrap.appendChild(btnRun);
    wrap.appendChild(btnCancel);

    // Progress
    statusEl = el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim)' });
    progressEl = el('progress', { style: 'width:100%;height:6px;display:none' });
    wrap.appendChild(statusEl);
    wrap.appendChild(progressEl);

    // Transient warning (e.g. wasm heap pressure)
    warnEl = el('div', { class: 'mono', style: 'font-size:11px;color:var(--warning,#b7791f);display:none' });
    wrap.appendChild(warnEl);

    // Error + diagnosis cards
    errEl = el('div', { class: 'mono', style: 'font-size:11px;color:var(--danger);white-space:pre-wrap;display:none' });
    diagEl = el('div', { style: 'display:none' });
    wrap.appendChild(errEl);
    wrap.appendChild(diagEl);

    root.appendChild(wrap);

    btnChoose.addEventListener('click', chooseFolder);
    btnReconnect.addEventListener('click', reconnect);
    btnAddExtra.addEventListener('click', () => { addExtraFolder().catch(() => { /* picker/idb best-effort */ }); });
    btnPreview.addEventListener('click', () => {
      openPreview().catch(err => reportError('Preview failed', err));
    });
    btnRun.addEventListener('click', () => { doRun(false).catch(err => reportError('Run failed', err)); });
    btnCancel.addEventListener('click', cancelRun);
  }

  // Aux folder block: rendered lazily, only when the manifest declares
  // aux_inputs (0..3). All aux inputs share ONE folder handle; the second run
  // button is one button whose label is author-controlled (may be Russian).
  function buildAuxUI() {
    const auxList = (manifest && manifest.aux_inputs) || [];
    if (!auxList.length) return;

    const block = el('div', { style: 'display:flex;flex-direction:column;gap:8px' });

    auxHintEl = el('div', { class: 'dock-hint', style: 'margin-bottom:0' });
    auxHintEl.textContent = 'Input file' + (auxList.length > 1 ? 's' : '') + ': '
      + auxList.map(a => a.name).join(', ');
    block.appendChild(auxHintEl);

    btnAuxChoose = el('button', { class: 'btn sm', type: 'button', text: 'Choose input folder…' });
    btnAuxReconnect = el('button', { class: 'btn sm', type: 'button', text: 'Reconnect input folder', hidden: true });
    block.appendChild(el('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;align-items:center' },
      [btnAuxChoose, btnAuxReconnect]));

    auxFolderEl = el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim);word-break:break-all' });
    block.appendChild(auxFolderEl);

    btnRunAux = el('button', { class: 'btn sm primary', type: 'button', text: auxList[0].label || 'Run with input', disabled: true, style: 'width:100%' });
    block.appendChild(btnRunAux);

    wrap.insertBefore(block, statusEl);   // right after the main run/cancel row
    // The second button ("run with the analyst's file") is also browser work with
    // a folder via File System Access: in `local` mode the agent takes the files from
    // folders.ini, and showing this block there would offer a choice
    // that cannot be used.
    browserOnlyEls.push(block);

    btnAuxChoose.addEventListener('click', chooseAuxFolder);
    btnAuxReconnect.addEventListener('click', reconnectAux);
    btnRunAux.addEventListener('click', () => { doRun(true).catch(err => reportError('Run failed', err)); });

    refreshAuxRun();
  }

  function setStatus(text, kind) {
    statusEl.textContent = text || '';
    statusEl.style.color = kind === 'ok' ? 'var(--success)'
      : kind === 'err' ? 'var(--danger)'
      : kind === 'accent' ? 'var(--accent)' : 'var(--ink-dim)';
  }
  // `detail` is the generator's own last words (stderr tail from the agent,
  // stack from the worker); `hint` says whose fault it is. Until 2026-09-09
  // the dock showed only "gen_crash: the generator exited with code 1" and the
  // real cause — a SchemaError naming the file and the columns — sat unread
  // in generation_runs.error_detail on the server.
  function showError(msg, detail, hint) {
    errEl.textContent = '';
    errEl.appendChild(document.createTextNode(msg));
    if (hint) {
      const h = el('div', { style: 'margin-top:4px;color:var(--ink-muted)' });
      h.textContent = hint;
      errEl.appendChild(h);
    }
    if (detail && String(detail).trim() && String(detail).trim() !== String(msg).trim()) {
      const d = el('pre', { class: 'gen-err-detail', style: 'margin:6px 0 0;max-height:240px;overflow:auto;white-space:pre-wrap;word-break:break-word;font:inherit;color:var(--ink);background:var(--surface-2);padding:6px 8px;border-radius:4px' });
      d.textContent = String(detail).slice(0, 4000);
      errEl.appendChild(d);
    }
    errEl.style.display = '';
  }
  function clearError() { errEl.textContent = ''; errEl.style.display = 'none'; }
  function showWarn(msg) { warnEl.textContent = msg; warnEl.style.display = ''; }

  // Publish-time diagnostics from the server (genmanifest.publish_warnings).
  // Never fatal — the publish already succeeded — but the report author is the
  // only person who can act on them, and this panel is the only moment they are
  // looking. Shown AFTER resetUI, which does not touch warnEl.
  function showPublishWarnings(list, qualityUrl) {
    if (!list || !list.length) return;
    while (warnEl.firstChild) warnEl.removeChild(warnEl.firstChild);
    warnEl.style.whiteSpace = 'pre-line';
    warnEl.style.display = '';
    const text = list.length > 1 ? list.map(w => '• ' + w).join('\n') : list[0];
    warnEl.appendChild(document.createTextNode(text));
    if (qualityUrl) {
      warnEl.appendChild(el('a', {
        href: qualityUrl, text: t('Open Data Quality'),
        style: 'display:block;margin-top:4px',
      }));
    }
  }
  function clearDiag() { diagEl.textContent = ''; diagEl.style.display = 'none'; }

  function renderLimits() {
    const ds = (manifest && manifest.data_scale) || {};
    limitsEl.textContent =
      `Browser engine works best up to: ≤${ds.max_files} files · ≤${fmtMB(ds.max_total_bytes)} MB total · ≤${fmtMB(ds.max_file_bytes)} MB/file. `
      + (manifest.write_back ? 'Writes results back into the folder.' : 'Read-only access.');
  }

  function setFolder(name, ready) {
    folderReady = !!ready;
    borderAck = false;              // a different folder earns its own warning
    folderEl.textContent = name ? (ready ? '📁 ' + name : '📁 ' + name + ' — permission needed') : '';
    // In `local` mode the browser folder takes no part in the run at all, so
    // its state has no right to disable the button (otherwise the person switched to
    // "run on this computer" and hit "Choose a folder first").
    btnRun.disabled = runLocation === 'local' ? running : (!ready || running);
    btnRun.title = (ready || runLocation === 'local') ? '' : 'Choose a folder first';
    invalidatePreview();   // the list belonged to THE folder that was changed
    refreshPreviewBtn();
    refreshAuxRun();
  }

  function setAuxFolder(name, ready) {
    auxFolderReady = !!ready;
    borderAck = false;              // aux files count toward the border totals
    if (auxFolderEl) auxFolderEl.textContent = name ? (ready ? '📁 ' + name : '📁 ' + name + ' — permission needed') : '';
    refreshAuxRun();
  }

  // Second run needs BOTH folders ready and no run in flight; the MAIN run
  // button is governed by setFolder alone and never gated on the aux folder.
  function refreshAuxRun() {
    if (!btnRunAux) return;
    btnRunAux.disabled = !(folderReady && auxFolderReady) || running;
  }

  function setBusy(busy) {
    running = busy;
    btnRun.hidden = busy;
    btnCancel.hidden = !busy;
    btnChoose.disabled = busy;
    btnReconnect.disabled = busy;
    if (btnRunAux) btnRunAux.hidden = busy;
    if (btnAuxChoose) { btnAuxChoose.disabled = busy; btnAuxReconnect.disabled = busy; }
    refreshAddBtn();
    refreshRefControls();
    refreshPreviewBtn();
    renderExtras();     // rebuild rows so remove/reconnect follow the run state
    if (busy) progressEl.style.display = '';
  }

  function resetUI() {
    setBusy(false);
    // Neither the agent nor the "run on this computer" mode has a browser
    // folder — there must be no gate on it there.
    btnRun.disabled = (engineMode === 'agent' || runLocation === 'local') ? false : !folderReady;
    refreshPreviewBtn();
    refreshAuxRun();
    progressEl.style.display = 'none';
    progressEl.removeAttribute('value');
  }

  // ── Reference tables (ADR-16) ───────────────────────────────────────
  // "Apply reference table" is a RUN parameter, not a report setting: a run
  // that resolved v7 and a run that deliberately ran without the dictionary
  // are both legitimate, and which of them produced a published version has
  // to be answerable months later — so the choice travels with the run
  // (input_manifest.reference) and is written to run_reference_uses.
  // localStorage only remembers the analyst's last answer so they do not
  // re-tick it every morning; it is never the source of truth for a run.
  function refLsKey() { return 'op-ref:' + DASH_ID; }
  function loadRefChoices() {
    try {
      const v = JSON.parse(localStorage.getItem(refLsKey()) || '{}');
      return (v && typeof v === 'object') ? v : {};
    } catch (e) { return {}; }          // private mode / corrupt value
  }
  function saveRefChoices() {
    // MERGE, never replace: a table that is momentarily `exists:false` (deleted,
    // or not created yet) is forced to applied:false in refChoices, and a plain
    // rewrite would use that to erase the analyst's remembered answer the next
    // time they tick ANY other checkbox on the panel.
    const out = loadRefChoices();
    for (const slug in refChoices) {
      const c = refChoices[slug];
      if (!c.usable) continue;
      out[slug] = { applied: c.applied, version: c.version };
    }
    try { localStorage.setItem(refLsKey(), JSON.stringify(out)); } catch (e) { /* private mode */ }
  }
  function refDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const p2 = n => (n < 10 ? '0' : '') + n;
    return p2(d.getDate()) + '.' + p2(d.getMonth() + 1) + '.' + String(d.getFullYear()).slice(2);
  }
  function refreshRefControls() {
    refControls.forEach(c => { c.node.disabled = c.locked || running; });
  }
  function renderReference() {
    if (!refListEl) return;
    refListEl.textContent = '';
    refControls = [];
    const stored = loadRefChoices();
    refChoices = {};
    referenceDecl.forEach(tv => {
      const versions = tv.versions || [];
      // A table nobody has created — or created but never filled — cannot be
      // applied: there is nothing to resolve against. The row still shows, so
      // the analyst sees WHY the generator's dictionary column is raw.
      const usable = !!(tv.exists && tv.current_version > 0);
      const prev = stored[tv.slug] || {};
      const applied = usable ? (prev.applied === undefined ? true : !!prev.applied) : false;
      let version = null;
      if (usable && tv.allow_pin && typeof prev.version === 'number'
          && prev.version !== tv.current_version
          && versions.some(v => v.n === prev.version)) version = prev.version;
      refChoices[tv.slug] = { applied: applied, version: version, exists: !!tv.exists, usable: usable };

      const row = el('div', { style: 'display:flex;gap:6px;align-items:center;flex-wrap:wrap;font-size:11px' });
      const cb = el('input', { type: 'checkbox' });
      cb.checked = applied;
      const label = el('label', { style: 'display:flex;gap:4px;align-items:center;color:var(--ink)' });
      label.appendChild(cb);
      const cur = versions.find(v => v.n === tv.current_version);
      const suffix = usable
        ? ' (v' + tv.current_version + (cur && cur.created_at ? ' · ' + refDate(cur.created_at) : '') + ')'
        : '';
      label.appendChild(document.createTextNode(
        t('Apply reference table «{p1}»{p2}', {p1: tv.title, p2: suffix})));
      row.appendChild(label);
      refControls.push({ node: cb, locked: !usable });
      cb.addEventListener('change', () => {
        refChoices[tv.slug].applied = !!cb.checked;
        saveRefChoices();
      });

      if (!usable) {
        row.appendChild(el('span', {
          style: 'color:var(--ink-dim)',
          text: tv.exists ? t('reference table not yet populated') : t('reference table not yet created'),
        }));
      } else if (tv.allow_pin && versions.length) {
        // Version picking exists only where the owner allowed it: an old
        // version is a deliberate «reproduce last month's numbers», never a
        // default a run can drift into.
        const sel = el('select', { style: 'font-size:11px' });
        versions.forEach(v => {
          const o = el('option', { value: String(v.n), text: 'v' + v.n + (v.created_at ? ' · ' + refDate(v.created_at) : '') });
          if (v.n === (version == null ? tv.current_version : version)) o.selected = true;
          sel.appendChild(o);
        });
        sel.value = String(version == null ? tv.current_version : version);
        sel.addEventListener('change', () => {
          const n = parseInt(sel.value, 10);
          // The current version is stored as «current», not as a pin: tomorrow's
          // import must reach a run that never asked for an old version.
          refChoices[tv.slug].version = (!n || n === tv.current_version) ? null : n;
          saveRefChoices();
        });
        row.appendChild(sel);
        refControls.push({ node: sel, locked: false });
      }
      refListEl.appendChild(row);
    });
    refreshRefControls();
  }

  // What rides in input_manifest.reference: every table that EXISTS, applied
  // or not. The «not applied» rows are the record that says the published
  // numbers were produced without the dictionary on purpose.
  function refBlock() {
    const out = [];
    referenceDecl.forEach(tv => {
      const c = refChoices[tv.slug];
      if (!c || !c.exists) return;
      out.push({ slug: tv.slug, version: c.applied ? c.version : null, applied: !!c.applied });
    });
    return out;
  }

  // ── IndexedDB: persist the directory handle (op-gen / handles / id) ──
  function idbOpen() {
    return new Promise((res, rej) => {
      const req = indexedDB.open('op-gen', 1);
      req.onupgradeneeded = () => { req.result.createObjectStore('handles'); };
      req.onsuccess = () => res(req.result);
      req.onerror = () => rej(req.error);
    });
  }
  async function idbGet(key) {
    const db = await idbOpen();
    return new Promise((res, rej) => {
      const r = db.transaction('handles', 'readonly').objectStore('handles').get(String(key));
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  }
  async function idbPut(key, val) {
    const db = await idbOpen();
    return new Promise((res, rej) => {
      const tv = db.transaction('handles', 'readwrite');
      tv.objectStore('handles').put(val, String(key));
      tv.oncomplete = () => res();
      tv.onerror = () => rej(tv.error);
    });
  }

  const pickMode = () => (manifest && manifest.write_back) ? 'readwrite' : 'read';

  // ── Folder picker + permission lifecycle (S4) ───────────────────────
  async function chooseFolder() {
    clearError();
    try {
      const h = await window.showDirectoryPicker(pickerOpts(pickMode()));
      dirHandle = h;
      await idbPut(DASH_ID, h).catch(() => { /* private mode: keep working this session */ });
      btnReconnect.hidden = true;
      setFolder(h.name, true);
      renderSlots();
    } catch (e) {
      /* AbortError = user cancelled the picker; nothing to report */
    }
  }

  async function reconnect() {
    if (!dirHandle) return;
    clearError();
    try {
      const perm = await dirHandle.requestPermission({ mode: pickMode() });
      if (perm === 'granted') { btnReconnect.hidden = true; setFolder(dirHandle.name, true); }
      else { showError('Folder permission was denied.'); }
    } catch (e) { showError('Could not re-request folder permission.'); }
  }

  async function restoreHandle() {
    let h;
    try { h = await idbGet(DASH_ID); } catch (e) { return; }
    if (!h) return;
    dirHandle = h;
    try {
      const perm = await h.queryPermission({ mode: pickMode() });
      if (perm === 'granted') { setFolder(h.name, true); }
      else { setFolder(h.name, false); btnReconnect.hidden = false; }   // 'prompt' or 'denied': Reconnect asks again
    } catch (e) { setFolder(h.name, false); btnReconnect.hidden = false; }
    renderSlots();
  }

  // Aux folder — read-only (we never write to it), persisted under 'aux:'+id.
  async function chooseAuxFolder() {
    clearError();
    try {
      const h = await window.showDirectoryPicker(pickerOpts('read', 'x'));
      auxDirHandle = h;
      await idbPut('aux:' + DASH_ID, h).catch(() => { /* private mode: session-only */ });
      btnAuxReconnect.hidden = true;
      setAuxFolder(h.name, true);
    } catch (e) {
      /* AbortError = user cancelled the picker */
    }
  }

  async function reconnectAux() {
    if (!auxDirHandle) return;
    clearError();
    try {
      const perm = await auxDirHandle.requestPermission({ mode: 'read' });
      if (perm === 'granted') { btnAuxReconnect.hidden = true; setAuxFolder(auxDirHandle.name, true); }
      else { showError('Input folder permission was denied.'); }
    } catch (e) { showError('Could not re-request input folder permission.'); }
  }

  async function restoreAuxHandle() {
    if (!btnRunAux) return;              // no aux UI on this generator
    let h;
    try { h = await idbGet('aux:' + DASH_ID); } catch (e) { return; }
    if (!h) return;
    auxDirHandle = h;
    try {
      const perm = await h.queryPermission({ mode: 'read' });
      if (perm === 'granted') { setAuxFolder(h.name, true); }
      else if (perm === 'prompt') { setAuxFolder(h.name, false); btnAuxReconnect.hidden = false; }
      else { auxDirHandle = null; }       // 'denied' — force a fresh pick
    } catch (e) { auxDirHandle = null; }
  }

  // ── Extra input folders (multi-folder input) ────────────────────────
  // One IndexedDB entry ('extra:'+id) holds the array of {h, w} plain objects
  // (handles are structured-cloneable). The main handle at key DASH_ID stays
  // untouched, so single-folder generators are byte-for-byte backward compatible.
  async function persistExtras() {
    const arr = extraDirs.map(x => ({ h: x.h, w: x.w }));
    await idbPut('extra:' + DASH_ID, arr).catch(() => { /* private mode: session-only */ });
  }

  function refreshAddBtn() {
    if (btnAddExtra) btnAddExtra.disabled = running || extraDirs.length >= MAX_EXTRA;
  }

  function renderExtras() {
    if (!extraListEl) return;
    extraListEl.textContent = '';
    extraDirs.forEach((x, i) => {
      const row = el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim);display:flex;gap:6px;align-items:center;flex-wrap:wrap' });
      row.appendChild(el('span', { style: 'word-break:break-all', text: '📁 ' + x.h.name + (x.w ? ' ✎' : '') }));
      // Reconnect shown only while the handle needs permission (queryPermission
      // was 'prompt'); ready handles hide it.
      const btnRe = el('button', { class: 'btn xs', type: 'button', text: 'Reconnect', hidden: x.ready });
      btnRe.disabled = running;
      btnRe.addEventListener('click', () => { reconnectExtra(i); });
      row.appendChild(btnRe);
      const btnRm = el('button', { class: 'btn xs', type: 'button', text: '✕' });
      btnRm.disabled = running;
      btnRm.addEventListener('click', () => { removeExtra(i); });
      row.appendChild(btnRm);
      extraListEl.appendChild(row);
    });
  }

  async function addExtraFolder() {
    if (running || extraDirs.length >= MAX_EXTRA) return;
    clearError();
    // The checkbox at click time sets the picker mode: a writable extra receives
    // results in the write-back step.
    const w = !!(extraWriteCb && extraWriteCb.checked);
    let h;
    try { h = await window.showDirectoryPicker(pickerOpts(w ? 'readwrite' : 'read', 'e')); }
    catch (e) { return; }                 // AbortError = user cancelled the picker
    // Duplicate guard: same folder as the main handle or any existing extra.
    try {
      let dup = dirHandle && await h.isSameEntry(dirHandle);
      if (!dup) for (const x of extraDirs) { if (await h.isSameEntry(x.h)) { dup = true; break; } }
      if (dup) { showError('This folder is already in the list.'); return; }
    } catch (e) { /* isSameEntry unsupported — accept the pick */ }
    extraDirs.push({ h: h, w: w, ready: true });
    borderAck = false;              // the totals just changed
    invalidatePreview();
    await persistExtras();
    renderExtras();
    refreshAddBtn();
  }

  async function removeExtra(i) {
    if (running) return;
    extraDirs.splice(i, 1);
    invalidatePreview();
    await persistExtras();
    renderExtras();
    refreshAddBtn();
    renderSlots();
  }

  async function reconnectExtra(i) {
    const x = extraDirs[i];
    if (!x) return;
    clearError();
    try {
      const perm = await x.h.requestPermission({ mode: x.w ? 'readwrite' : 'read' });
      if (perm === 'granted') { x.ready = true; renderExtras(); }
      else { showError('Folder permission was denied.'); }
    } catch (e) { showError('Could not re-request folder permission.'); }
  }

  async function restoreExtraHandles() {
    let arr;
    try { arr = await idbGet('extra:' + DASH_ID); } catch (e) { return; }
    if (!Array.isArray(arr) || !arr.length) return;
    const kept = [];
    for (const it of arr) {
      if (!it || !it.h) continue;
      const w = !!it.w;
      // NEVER drop a stored folder here: a handle that cannot be queried
      // today (browser hiccup, 'denied' after a policy change) stays in the
      // list as «needs permission» and Reconnect gets another chance. Only
      // the ✕ button removes a folder — pruning on restore is how people's
      // folders "reset by themselves" (prod, 2026-08-23).
      let perm = 'prompt';
      try { perm = await it.h.queryPermission({ mode: w ? 'readwrite' : 'read' }); }
      catch (e) { perm = 'prompt'; }
      kept.push(restoredExtra(it, perm));
    }
    extraDirs = kept;
    renderExtras();
    refreshAddBtn();
    renderSlots();
  }

  // ── Exclusion rules (data_sources.exclude_rules) ────────────────────
  // Operational, not authored: they arrive beside the manifest rather than
  // inside it, because the manifest is frozen per version and "this file is
  // junk" is a decision the report's own people revise without shipping code.
  //
  // Patterns match the path RELATIVE to the folder root. The walk is flat
  // today, so that is just the filename — writing the matcher against a
  // relative path anyway means the rules keep their meaning unchanged if the
  // walk ever descends into subfolders.
  let sourceRules = {};        // { folderName: [pattern, …] }
  let sourceAliases = {};      // { folderName: { match: [file name, …] } }
  const _globCache = new Map();

  function globToRe(pat) {
    let re = _globCache.get(pat);
    if (re) return re;
    let out = '';
    for (let i = 0; i < pat.length; i++) {
      const c = pat[i];
      if (c === '*') {
        if (pat[i + 1] === '*') { out += '.*'; i++; }   // ** crosses separators
        else out += '[^/]*';
      } else if (c === '?') {
        out += '[^/]';
      } else {
        out += c.replace(/[.+^${}()|[\]\\]/g, '\\$&');
      }
    }
    // Case-insensitive on purpose: these folders live on Windows shares, where
    // REPORT.XLSX and report.xlsx are the same file.
    re = new RegExp('^' + out + '$', 'i');
    _globCache.set(pat, re);
    return re;
  }

  function excludedBy(rules, relPath) {
    if (!rules || !rules.length) return null;
    for (const pat of rules) {
      if (globToRe(pat).test(relPath)) return pat;
    }
    return null;
  }

  // Size and date of a skipped file — only for the selection screen. During a run
  // (`detail` falsy) the entry stays exactly as it was: an extra
  // `getFile()` on a hundred discarded files is a hundred system calls
  // for a line nobody will read.
  async function describeSkip(rec, entry, detail) {
    if (!detail) return rec;
    try {
      const f = await entry.getFile();
      rec.size = f.size;
      rec.mtime_ms = f.lastModified;
    } catch (e) { /* no permission on the file — show it without a size, but show it */ }
    return rec;
  }

  // ── Enumerate: flat walk + K11 filter (D.6) ─────────────────────────
  // Per-folder walk; the caller unions folders and sorts the combined list.
  // `depth` levels of subfolders are walked when the manifest says
  // `subfolders: N`; a nested file is named `<sub>__<file>` (its real path
  // rides along in `path` for reading) so names stay flat and unique.
  //
  // `detail` is the "what we will read" screen mode: a skipped file additionally
  // gets its size and date (which requires opening a handle — during a run
  // we do NOT do this, so an excluded file still is never opened at all)
  // and, if a rule is the reason, the name of the rule that matched. The "read / don't
  // read" decision is made by the same code as the run: the screen must show
  // exactly what will happen afterwards.
  const SUB_SEP = '__';
  async function enumerate(handle, depth, prefix, acc, detail) {
    const files = (acc && acc.files) || [], skipped = (acc && acc.skipped) || [];
    const rules = sourceRules[handle.name] || null;
    // "THIS file is such-and-such input": Anya renamed the file, the mask did
    // not recognise it, and the run must still happen. An alias NAMES the file by
    // its literal name and only lets it in under an already declared input —
    // it cannot widen the mask, so no fourth semantics of "what was
    // read" appears.
    const aliasMap = sourceAliases[handle.name] || null;
    const aliasOf = (fileName) => {
      if (!aliasMap) return null;
      const want = String(fileName || '').toLowerCase();
      for (const match of Object.keys(aliasMap)) {
        if ((aliasMap[match] || []).some(n => String(n).toLowerCase() === want)) return match;
      }
      return null;
    };
    const maxDepth = depth == null ? Number(manifest && manifest.subfolders) || 0 : depth;
    const pre = prefix || '';
    for await (const entry of handle.values()) {
      if (entry.kind !== 'file') {
        if (maxDepth > 0 && !entry.name.startsWith('.') && entry.name !== '__MACOSX') {
          await enumerate(entry, maxDepth - 1, pre + entry.name + SUB_SEP, { files, skipped }, detail);
        } else {
          skipped.push({ name: pre + entry.name, reason: 'subfolder' });
        }
        continue;
      }
      const base = entry.name;
      const nm = pre + base;
      if (base.startsWith('~$') || base.startsWith('._') || base.startsWith('.') || base === '__MACOSX') {
        skipped.push({ name: nm, reason: 'ignored' }); continue;
      }
      // .xls/.xlsm included on purpose: service workbooks (e.g. the reasons
      // catalog SL_NWD.xls) ride through add_file, which classifies them as
      // "service" and keeps them for finish() — the generator, not the engine,
      // knows which names are data and which are supporting files.
      // Delimited text (.csv/.tsv) rides along with the workbooks: a DWH
      // export of several hundred MB is a text file, and converting it to
      // xlsx by hand just to get past this gate was the analysts' workaround.
      // The worker streams it into the Pyodide FS in chunks (never a whole
      // ArrayBuffer), the lineage probe reads its header through the same
      // sniff/run_checks path (_TextWorkbook), and add_file decides what to
      // do with it exactly as with a workbook.
      const lower = nm.toLowerCase();
      if (!(lower.endsWith('.xlsx') || lower.endsWith('.xlsm') || lower.endsWith('.xls') || lower.endsWith('.xlsb')
            || lower.endsWith('.csv') || lower.endsWith('.tsv'))) {
        skipped.push({ name: nm, reason: 'not an Excel/CSV file' }); continue;
      }
      // Allow-list (Data Lineage v2): when the manifest declares `inputs`, the
      // engine reads ONLY files a pattern admits — legacy manifests (inputsDecl
      // empty) keep the old "every Excel file" behaviour unchanged.
      // Entries the server folded in from `required_files` (from_required_files)
      // are NOT an allow-list: a legacy manifest with only required_files must
      // keep reading every Excel file (regression 2026-08-21: ops-rdc-health
      // got 1 file of 16). They still tag the file's matched_input.
      // Patterns are written against file names; a nested file matches by
      // its base name OR its prefixed name, so `SL_*.xlsx` keeps working.
      let matchedInput = null;
      const found = inputsDecl.find(inp => globToRe(inp.match).test(nm) || (pre && globToRe(inp.match).test(base)));
      if (found) matchedInput = found.match;
      else {
        // The alias is checked BEFORE the rejection and AFTER the mask: first what
        // the author declared, and only if they said nothing — what the person named.
        const named = aliasOf(nm) || (pre ? aliasOf(base) : null);
        if (named) matchedInput = named;
        else if (inputsDecl.some(inp => !inp.from_required_files)) {
          skipped.push(await describeSkip({ name: nm, reason: 'not declared in inputs' }, entry, detail));
          continue;
        }
      }
      // Checked BEFORE getFile(): an excluded file is never opened, so a rule
      // is also the way to keep the engine off a file it must not touch. Scoped
      // by the server to this generator×source now, but still applied AFTER the
      // allow-list — an override can only narrow a declared input, not widen it.
      const hit = excludedBy(rules, nm) || (pre ? excludedBy(rules, base) : null);
      if (hit) {
        skipped.push(await describeSkip(
          { name: nm, reason: 'excluded by rule ' + hit, rule: hit, matched_input: matchedInput },
          entry, detail));
        continue;
      }
      const f = await entry.getFile();
      const rec = { name: nm, size: f.size, mtime_ms: f.lastModified };
      if (pre) rec.path = pre.split(SUB_SEP).filter(Boolean).concat([base]).join('/');
      if (matchedInput) rec.matched_input = matchedInput;
      files.push(rec);
    }
    return { files, skipped };
  }

  // ── C.6 engine border ───────────────────────────────────────────────
  // Two kinds of finding, and only one of them stops a run:
  //   blocking — there is physically nothing to run (no input files);
  //   advisory — the folder is past the measured comfort border (owner
  //              decision 2026-08-20: scale is advice, not a gate). The engine
  //              may still finish; it may also exhaust the tab's memory. The
  //              analyst is shown the numbers and decides.
  function checkBorder(files, totalBytes, maxFile) {
    const ds = (manifest && manifest.data_scale) || {};
    const blocking = [], advisory = [];
    if (files.length === 0) blocking.push('No Excel files found in the selected folder.');
    if (ds.max_files != null && files.length > ds.max_files)
      advisory.push(`${files.length} files is over the recommended ${ds.max_files}.`);
    if (ds.max_total_bytes != null && totalBytes > ds.max_total_bytes)
      advisory.push(`${fmtMB(totalBytes)} MB total is over the recommended ${fmtMB(ds.max_total_bytes)} MB.`);
    if (ds.max_file_bytes != null && maxFile > ds.max_file_bytes)
      advisory.push(`Largest file ${fmtMB(maxFile)} MB is over the recommended ${fmtMB(ds.max_file_bytes)} MB.`);
    const dm = navigator.deviceMemory, hc = navigator.hardwareConcurrency;
    if (dm != null && dm < 2) advisory.push(`This device reports ${dm} GB RAM (browser engine wants ≥2 GB).`);
    if (hc != null && hc < 2) advisory.push(`This device reports ${hc} CPU core (browser engine wants ≥2).`);
    return { blocking, advisory };
  }

  // Shared tail of both border cards: what the folder holds, what was skipped.
  function borderFacts(card, files, totalBytes, skipped) {
    const ds = (manifest && manifest.data_scale) || {};
    card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink)', text: `${files.length} files / ${fmtMB(totalBytes)} MB in folder` }));
    card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim)', text: `recommended ≤${ds.max_files} files / ≤${fmtMB(ds.max_total_bytes)} MB / ≤${fmtMB(ds.max_file_bytes)} MB/file` }));
    if (skipped && skipped.length) {
      card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim);margin-top:2px', text: `Skipped ${skipped.length}: ` + skipped.slice(0, 12).map(s => s.name).join(', ') + (skipped.length > 12 ? '…' : '') }));
    }
  }

  // Hard stop: nothing to run.
  function showDiag(violations, files, totalBytes, skipped) {
    clearDiag();
    const card = el('div', { style: 'border:1px solid var(--danger);border-radius:8px;padding:10px;background:color-mix(in oklab,var(--danger) 8%,transparent);display:flex;flex-direction:column;gap:6px' });
    card.appendChild(el('div', { style: 'font-size:12px;font-weight:600;color:var(--danger)', text: 'Nothing to run in this folder' }));
    violations.forEach(m => card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--danger)', text: '• ' + m })));
    borderFacts(card, files, totalBytes, skipped);
    diagEl.appendChild(card);
    diagEl.style.display = '';
  }

  // Advisory: past the comfort border, but the analyst may proceed. onRun
  // re-enters doRun from the button's own click, so the folder-permission
  // dance still happens inside a user gesture.
  function showBorderWarning(advisory, files, totalBytes, skipped, onRun) {
    clearDiag();
    const card = el('div', { style: 'border:1px solid var(--warning);border-radius:8px;padding:10px;background:color-mix(in oklab,var(--warning) 10%,transparent);display:flex;flex-direction:column;gap:6px' });
    card.appendChild(el('div', { style: 'font-size:12px;font-weight:600;color:var(--warning)', text: 'Larger than the browser engine is measured for' }));
    advisory.forEach(m => card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--warning)', text: '• ' + m })));
    borderFacts(card, files, totalBytes, skipped);
    card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim)', text: 'The run may take much longer, or run the browser tab out of memory. Close other tabs first.' }));
    const btn = el('button', { class: 'btn xs', type: 'button', text: 'Run anyway', style: 'align-self:flex-start;margin-top:2px' });
    btn.addEventListener('click', onRun);
    card.appendChild(btn);
    diagEl.appendChild(card);
    diagEl.style.display = '';
  }

  // Same card idiom as showDiag, for a required aux input the folder lacks.
  function showAuxMissing(name) {
    clearDiag();
    const card = el('div', { style: 'border:1px solid var(--danger);border-radius:8px;padding:10px;background:color-mix(in oklab,var(--danger) 8%,transparent);display:flex;flex-direction:column;gap:6px' });
    card.appendChild(el('div', { style: 'font-size:12px;font-weight:600;color:var(--danger)', text: 'Input file missing' }));
    card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink)', text: `The selected input folder has no ${name}.` }));
    card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim)', text: `Choose a folder that contains ${name} and try again.` }));
    diagEl.appendChild(card);
    diagEl.style.display = '';
  }

  // Same card idiom as showDiag: a basename present in two or more folders.
  function showDupes(dupes) {
    clearDiag();
    const card = el('div', { style: 'border:1px solid var(--danger);border-radius:8px;padding:10px;background:color-mix(in oklab,var(--danger) 8%,transparent);display:flex;flex-direction:column;gap:6px' });
    card.appendChild(el('div', { style: 'font-size:12px;font-weight:600;color:var(--danger)', text: 'Duplicate file names across folders' }));
    dupes.slice(0, 10).forEach(d => card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink)', text: '• ' + d.name + ' — ' + d.folders.join(', ') })));
    if (dupes.length > 10) card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim)', text: '…' }));
    card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim)', text: 'Keep one copy per name, or remove a folder from the list.' }));
    diagEl.appendChild(card);
    diagEl.style.display = '';
  }

  // Same card idiom as showDiag: a manifest-required input not in any folder.
  function showRequiredMissing(missing) {
    clearDiag();
    const card = el('div', { style: 'border:1px solid var(--danger);border-radius:8px;padding:10px;background:color-mix(in oklab,var(--danger) 8%,transparent);display:flex;flex-direction:column;gap:6px' });
    card.appendChild(el('div', { style: 'font-size:12px;font-weight:600;color:var(--danger)', text: 'Required input file missing' }));
    missing.forEach(n => card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink)', text: '• ' + n })));
    card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim)', text: 'Add the folder that contains it (+ Add folder…).' }));
    diagEl.appendChild(card);
    diagEl.style.display = '';
  }

  // ── The "what we will read" screen ───────────────────────────────────
  // Between "connected the folders" and "Run" the person used to see and decide
  // NOTHING: what exactly the engine would read only became clear from the run's result.
  // The screen shows the same list the run will build — the list comes from
  // the same `enumerate()` with the same masks — and lets you untick what is not needed.
  //
  // The decision does NOT live in the browser: an unticked box is an exclusion mask in
  // `input_overrides` on the (generator, folder) pair, the same record that
  // the drawer on /d/{id}/data-sources edits. So it survives the next
  // run, is visible to another person and matches the lineage.
  //
  // None of this loads when the report opens: both the file list and the
  // rules arrive when the button is pressed.
  let previewEl = null, btnPreview = null;
  let folderRules = {};        // {folder name: {source_id, version, exclude_rules}}
  let previewDraft = {};       // {folder name: [mask…]} — a decision not saved yet
  let previewAliases = {};     // {folder name: {mask: [file name, …]}} — the same for "this file is the input"
  const previewPicker = new Set();   // keys of rows where the input picker is open
  let previewOnlyChosen = false;     // show only what will be read
  let previewRows = null;      // [{dir, folder, name, size, mtime_ms, kind, …}]
  let previewDirty = false;

  // The set of folders changed — the list shown is no longer about them.
  function invalidatePreview() {
    previewRows = null;
    previewDraft = {};
    previewAliases = {};
    previewOnlyChosen = false;
    previewDirty = false;
    renderPreview();
  }

  function refreshPreviewBtn() {
    if (!btnPreview) return;
    btnPreview.disabled = !folderReady || running
      || engineMode === 'agent' || runLocation === 'local';
  }

  async function loadFolderRules() {
    let r;
    try { r = await fetch(`/api/dashboards/${DASH_ID}/generator/input-rules`); }
    catch (e) { return false; }
    if (!r.ok) return false;
    let d = null;
    try { d = await r.json(); } catch (e) { return false; }
    folderRules = (d && d.folders) || {};
    // The server is the source of truth: pull the rules into sourceRules so that
    // the run reads exactly what the screen showed, even if the manifest was
    // loaded before someone else set the rule.
    for (const name in folderRules) {
      sourceRules[name] = (folderRules[name].exclude_rules || []).slice();
      sourceAliases[name] = folderRules[name].file_aliases || {};
    }
    return true;
  }

  function draftFor(folder) {
    if (!previewDraft[folder]) previewDraft[folder] = (sourceRules[folder] || []).slice();
    return previewDraft[folder];
  }

  function aliasDraftFor(folder) {
    if (!previewAliases[folder]) {
      const src = sourceAliases[folder] || {};
      const copy = {};
      Object.keys(src).forEach(k => { copy[k] = (src[k] || []).slice(); });
      previewAliases[folder] = copy;
    }
    return previewAliases[folder];
  }

  // Which declared input the person assigned this file to (not saved yet).
  function previewAliasOf(row) {
    const map = previewAliases[row.folder] || sourceAliases[row.folder] || {};
    const want = String(row.name || '').toLowerCase();
    for (const match of Object.keys(map)) {
      if ((map[match] || []).some(n => String(n).toLowerCase() === want)) return match;
    }
    return null;
  }

  // The per-folder rule ceiling of the screen's endpoint (app/api/generators.py).
  // The screen must know it too: hitting a refusal AFTER pressing "Remember
  // this choice" is worse than saying so up front.
  const PREVIEW_MAX_RULES = 1000;

  // "Clear all" in a folder. A rule is a file name, one per file, not a single
  // "*" mask: that would also switch off files that appear in the folder tomorrow, while
  // the person unticks what they see now. globmatch would not accept such a mask anyway.
  function clearFolder(folder, rows) {
    const names = rows.filter(r => r.kind === 'read' || r.kind === 'excluded')
      .map(r => r.name);
    if (names.length > PREVIEW_MAX_RULES) {
      showError(t('The folder holds {p1} files — more than one decision can cover ({p2}).',
                  {p1: names.length, p2: PREVIEW_MAX_RULES}));
      return;
    }
    previewDraft[folder] = names;
    // Inputs assigned in this same folder are cleared too: otherwise "clear all"
    // would leave readable exactly the files the person named themselves, and
    // the "0 of N" counter would lie.
    const map = aliasDraftFor(folder);
    Object.keys(map).forEach(k => { delete map[k]; });
    previewDirty = true;
    renderPreview();
  }

  // "Restore all" — removes ALL rules of the folder, including ones set earlier.
  function restoreFolder(folder) {
    previewDraft[folder] = [];
    previewDirty = true;
    renderPreview();
  }

  function setPreviewAlias(row, match) {
    const map = aliasDraftFor(row.folder);
    // A file is always assigned to EXACTLY one input: remove previous assignments,
    // otherwise one workbook would quietly travel as two inputs at once.
    Object.keys(map).forEach(k => {
      map[k] = (map[k] || []).filter(n => String(n).toLowerCase() !== String(row.name).toLowerCase());
      if (!map[k].length) delete map[k];
    });
    if (match) (map[match] = map[match] || []).push(row.name);
    previewDirty = true;
    renderPreview();
  }

  // The same `excludedBy` that decides the file's fate during the run — the screen has no
  // right to compute it itself, otherwise one day it will lie.
  function previewExcluded(row) {
    return excludedBy(draftFor(row.folder), row.name);
  }

  function previewDate(ms) {
    if (!ms) return '';
    try { return OP_I18N.fmtDate(new Date(ms)); } catch (e) { return ''; }
  }

  async function openPreview() {
    if (running || !folderReady) return;
    clearError();
    setStatus(t('Reading the file list…'), 'accent');
    // Permission is confirmed with the same gesture as during the run.
    try {
      let perm = await dirHandle.queryPermission({ mode: pickMode() });
      if (perm !== 'granted') perm = await dirHandle.requestPermission({ mode: pickMode() });
      if (perm !== 'granted') { showError(t('Folder access is needed.')); setStatus('', ''); return; }
    } catch (e) { showError(t('Folder access is needed.')); setStatus('', ''); return; }
    for (const x of extraDirs) {
      const mode = x.w ? 'readwrite' : 'read';
      try {
        let perm = await x.h.queryPermission({ mode: mode });
        if (perm !== 'granted') perm = await x.h.requestPermission({ mode: mode });
        if (perm !== 'granted') { showError(t('Folder access is needed: {p1}', {p1: x.h.name})); setStatus('', ''); return; }
      } catch (e) { showError(t('Folder access is needed: {p1}', {p1: x.h.name})); setStatus('', ''); return; }
    }

    await loadFolderRules();
    previewDraft = {};
    previewAliases = {};
    previewDirty = false;

    const handles = [dirHandle].concat(extraDirs.map(x => x.h));
    const rows = [];
    for (let i = 0; i < handles.length; i++) {
      const folder = handles[i].name;
      let r;
      try { r = await enumerate(handles[i], undefined, undefined, undefined, true); }
      catch (e) { showError(t('Could not read the folder: {p1}', {p1: folder})); setStatus('', ''); return; }
      r.files.forEach(f => rows.push({
        dir: i, folder: folder, name: f.name, size: f.size, mtime_ms: f.mtime_ms,
        matched_input: f.matched_input || null, kind: 'read',
      }));
      r.skipped.forEach(s => {
        let kind = 'ignored';
        if (s.rule) kind = 'excluded';
        else if (s.reason === 'not declared in inputs') kind = 'unmatched';
        rows.push({
          dir: i, folder: folder, name: s.name, size: s.size, mtime_ms: s.mtime_ms,
          matched_input: s.matched_input || null, kind: kind, reason: s.reason,
        });
      });
    }
    rows.sort((a, b) => (a.dir - b.dir) || a.name.localeCompare(b.name));
    previewRows = rows;
    setStatus('', '');
    renderPreview();
  }

  function renderPreview() {
    if (!previewEl) return;
    // The list is redrawn completely on every tick — and that is
    // intentional: a rule can be wider than one name ("*_old.xlsx"), so
    // the fate of neighbouring files changes along with this one, and EVERYTHING
    // must be shown. But then the screen is obliged to return the person to where they
    // were: with 172 files, resetting the scroll to the top after every click
    // makes the work impossible. We remember both the scroll position and what had
    // focus (otherwise the list "jumps" for keyboard users too).
    const oldList = previewEl.firstChild
      && previewEl.firstChild.querySelector && previewEl.firstChild.querySelector('.gp-list');
    const keepTop = (oldList && oldList.scrollTop) || 0;
    let keepFocus = null;
    try {
      const a = document.activeElement;
      if (a && a.closest && previewEl.contains(a)) {
        const r = a.closest('.gp-row');
        if (r) keepFocus = r.getAttribute('title');
      }
    } catch (e) { /* test stand without activeElement/closest — no harm */ }

    previewEl.textContent = '';
    if (!previewRows) { previewEl.style.display = 'none'; return; }
    previewEl.style.display = '';

    // The layout lives in a dock ~320px wide, so a file row has two
    // levels: the name on one line with truncation, a caption below it. Markup uses
    // classes (.gp-*), not inline styles: the panel must obey
    // the theme tokens, like the rest of the product.
    const card = el('div', { class: 'gp-card' });
    const list = el('div', { class: 'gp-list' });

    let readCount = 0, readBytes = 0, unmatched = 0;
    // folderAll is not "how many rows" but "how many files the folder can give
    // the run at all": an Excel temp file and a .txt in the denominator would mean
    // that "6 of 8" is never reachable. folderOff — those unticked by a rule, and
    // only they give the "restore all" command any meaning.
    let curFolder = null, folderHead = null;
    let folderRead = 0, folderAll = 0, folderOff = 0, folderRows = [], folderShown = 0;
    let shown = 0;
    const closeFolder = () => {
      if (folderHead) {
        const name = curFolder, rows = folderRows;
        folderHead.appendChild(el('span', { class: 'gp-folder-n',
          text: t('{p1} of {p2}', {p1: folderRead, p2: folderAll}) }));
        // Unticking 170 boxes one by one is not work. Only the button that
        // changes something right now is shown: in a fully unticked folder
        // "clear all" is an empty promise.
        if (folderRead > 0) {
          const b = el('button', { class: 'gp-folder-act', type: 'button', text: t('clear all') });
          b.addEventListener('click', () => clearFolder(name, rows));
          folderHead.appendChild(b);
        }
        if (folderOff > 0) {
          const b = el('button', { class: 'gp-folder-act', type: 'button', text: t('restore all') });
          b.addEventListener('click', () => restoreFolder(name));
          folderHead.appendChild(b);
        }
      }
      // A folder with not a single row left in "selected only" mode
      // must not hang around as an empty header.
      if (folderHead && folderShown === 0) list.removeChild(folderHead);
      folderRead = folderAll = folderOff = folderShown = 0;
      folderRows = [];
    };

    previewRows.forEach(row => {
      if (row.folder !== curFolder) {
        closeFolder();
        curFolder = row.folder;
        folderHead = el('div', { class: 'gp-folder' }, [el('span', { text: curFolder })]);
        list.appendChild(folderHead);
      }
      if (row.kind !== 'ignored') folderAll++;
      folderRows.push(row);
      // The count always runs over ALL rows: "2 of 5" must talk about the
      // folder, not about what is currently shown on screen.
      const willRead = (row.kind === 'read' || row.kind === 'excluded')
        ? !previewExcluded(row)
        : (row.kind === 'unmatched' ? !!previewAliasOf(row) : false);
      const visible = !previewOnlyChosen || willRead;
      if (visible) { folderShown++; shown++; }
      const meta = [row.size != null ? t('{p1} MB', {p1: fmtMB(row.size)}) : null,
                    previewDate(row.mtime_ms)].filter(Boolean);

      if (row.kind === 'read' || row.kind === 'excluded') {
        const hit = previewExcluded(row);
        if (!hit) { readCount++; readBytes += row.size || 0; folderRead++; }
        else folderOff++;
        const cb = el('input', { type: 'checkbox' });
        cb.checked = !hit;
        cb.addEventListener('change', () => togglePreviewRow(row, cb.checked));
        const tail = hit
          ? t('not read — rule «{p1}»', {p1: hit})
          : (row.matched_input ? t('pattern {p1}', {p1: row.matched_input}) : t('read'));
        const label = el('label', { class: 'gp-row' + (hit ? ' is-off' : ''), title: row.name }, [
          cb,
          el('span', { class: 'gp-name', text: row.name }),
          el('span', { class: 'gp-meta', text: meta.concat([tail]).join(' · ') }),
        ]);
        if (visible) list.appendChild(label);
        return;
      }

      if (row.kind === 'unmatched') {
        // The file is the right one, the name is wrong. The person names the input right here instead
        // of renaming someone else's file on a network share.
        const named = previewAliasOf(row);
        if (named) { readCount++; readBytes += row.size || 0; folderRead++; }
        else unmatched++;
        const key = row.folder + '\u0000' + row.name;
        // The drop-down is NOT shown for every file in a row: in a folder
        // with a dozen unrecognised names, ten open drop-downs are a wall
        // of controls instead of a file list. Until the person presses
        // "read as…", the row stays a row.
        const open = named || previewPicker.has(key);
        const tail = named ? null : t('matched no pattern');
        const kids = [
          el('span', { class: 'gp-mark', text: named ? '✓' : '!' }),
          el('span', { class: 'gp-name', text: row.name }),
          el('span', { class: 'gp-meta', text: meta.concat(tail ? [tail] : []).join(' · ') }),
        ];
        if (open) {
          const sel = el('select', { class: 'select' });
          sel.appendChild(el('option', { value: '', text: t('do not read') }));
          inputsDecl.filter(i => i.source !== 'platform').forEach(i => {
            const o = el('option', { value: i.match, text: i.match });
            if (named === i.match) o.selected = true;
            sel.appendChild(o);
          });
          sel.addEventListener('change', () => { previewPicker.delete(key); setPreviewAlias(row, sel.value); });
          kids.push(el('div', { class: 'gp-assign' }, [sel]));
        } else {
          const btn = el('button', { class: 'btn xs', type: 'button', text: t('read as…') });
          btn.addEventListener('click', () => { previewPicker.add(key); renderPreview(); });
          kids.push(el('div', { class: 'gp-assign' }, [btn]));
        }
        if (visible) list.appendChild(el('div', { class: 'gp-row ' + (named ? 'is-named' : 'is-warn'), title: row.name }, kids));
        return;
      }

      if (!visible) return;
      list.appendChild(el('div', { class: 'gp-row', title: row.name }, [
        el('span', { class: 'gp-mark', text: '·' }),
        el('span', { class: 'gp-name', text: row.name }),
        el('span', { class: 'gp-meta',
          text: meta.concat([t('skipped: {p1}', {p1: row.reason})]).join(' · ') }),
      ]));
    });
    closeFolder();

    // The total goes in the header, not at the bottom: it is the main number of the screen, and
    // the person should not have to hunt for it under a list of fifty rows.
    card.appendChild(el('div', { class: 'gp-head' }, [
      el('span', { class: 'gp-title', text: t('What the run will read') }),
      el('span', { class: 'pill' + (readCount ? ' success' : ' warning') },
        [el('span', { class: 'dot' }),
         el('span', { text: tn(readCount, '{n} file · {p2} MB|{n} files · {p2} MB',
                               {p2: fmtMB(readBytes)}) })]),
    ]));
    // The "all / selected only" switch is the final check
    // before the run: "what did I tick". It sits ABOVE the list because
    // the person will look at it last, already about to
    // press Run, and hunting for it under a hundred rows is wrong.
    const seg = el('div', { class: 'seg gp-seg' });
    [[false, t('All files')], [true, t('Selected only')]].forEach(pair => {
      const b = el('button', { type: 'button', class: previewOnlyChosen === pair[0] ? 'active' : '',
                               text: pair[1] });
      b.addEventListener('click', () => {
        if (previewOnlyChosen === pair[0]) return;
        previewOnlyChosen = pair[0];
        renderPreview();
      });
      seg.appendChild(b);
    });
    card.appendChild(el('div', { class: 'gp-tools' }, [seg]));

    if (shown === 0) {
      list.appendChild(el('div', { class: 'gp-empty',
        text: previewOnlyChosen
          ? t('Nothing is selected — the run would read no file at all.')
          : t('The connected folders are empty.') }));
    }
    card.appendChild(list);

    const readNames = previewRows.filter(r => (r.kind === 'read' || r.kind === 'excluded') && !previewExcluded(r))
      .map(r => r.name);
    // An input the person has already named a file for is closed — complaining about it
    // would mean arguing with a decision made on this same screen a minute ago.
    const namedInputs = new Set(previewRows.map(r => r.kind === 'unmatched' ? previewAliasOf(r) : null)
      .filter(Boolean));
    const unsat = inputsDecl.filter(i => i.source !== 'platform'
      && !namedInputs.has(i.match)
      && !readNames.some(n => globToRe(i.match).test(n)));
    if (unsat.length || unmatched) {
      const notes = el('div', { class: 'gp-notes' });
      unsat.forEach(i => notes.appendChild(el('div',
        { class: 'gp-note ' + (i.required ? 'is-danger' : 'is-warn') }, [
          el('span', { class: 'gp-mark', text: i.required ? '✕' : '!' }),
          el('span', { text: i.required
            ? t('No file matched the required pattern {p1} — the run will not start.', {p1: i.match})
            : t('No file matched the pattern {p1}.', {p1: i.match}) }),
        ])));
      if (unmatched) {
        notes.appendChild(el('div', { class: 'gp-note' }, [
          el('span', { class: 'gp-mark', text: '?' }),
          el('span', { text: tn(unmatched,
            'One file matched no pattern — it may have been renamed: assign it an input in the list.|{n} files matched no pattern — they may have been renamed: assign them inputs in the list.') }),
        ]));
      }
      card.appendChild(notes);
    }

    const btnSave = el('button', { class: 'btn xs primary', type: 'button',
      text: t('Remember this choice'), disabled: !previewDirty });
    btnSave.addEventListener('click', () => { savePreview().catch(() => { /* the message is already shown */ }); });
    const btnClose = el('button', { class: 'btn xs ghost', type: 'button', text: t('Hide') });
    btnClose.addEventListener('click', () => { previewRows = null; renderPreview(); });
    card.appendChild(el('div', { class: 'gp-foot' }, [btnSave, btnClose]));
    card.appendChild(el('div', { class: 'gp-hint',
      text: t('The choice is remembered for this report and applies to later runs.') }));
    previewEl.appendChild(card);

    // Restore the scroll AFTER inserting into the document: before that the element has no
    // height, and assigning scrollTop is silently reset to zero.
    list.scrollTop = keepTop;
    if (keepFocus) {
      try {
        const again = Array.prototype.slice.call(list.querySelectorAll('.gp-row'))
          .filter(r => r.getAttribute('title') === keepFocus)[0];
        const ctl = again && again.querySelector('input,select,button');
        if (ctl && ctl.focus) ctl.focus();
      } catch (e) { /* same as above */ }
    }
  }

  function togglePreviewRow(row, read) {
    const rules = draftFor(row.folder);
    if (read) {
      // Remove ALL rules that closed this file. A rule can be
      // wider than a name ("*_old.xlsx"), and then others come back along with this
      // file — that is visible right away in the redrawn list, while silently leaving
      // the file excluded after its box was ticked would be a deception.
      previewDraft[row.folder] = rules.filter(p => !globToRe(p).test(row.name));
    } else if (!excludedBy(rules, row.name)) {
      rules.push(row.name);
    }
    previewDirty = true;
    renderPreview();
  }

  function aliasesEqual(a, b) {
    const ka = Object.keys(a || {}).sort(), kb = Object.keys(b || {}).sort();
    if (ka.join('\u0000') !== kb.join('\u0000')) return false;
    return ka.every(k => (a[k] || []).slice().sort().join('\u0000')
                      === ((b || {})[k] || []).slice().sort().join('\u0000'));
  }

  async function savePreview() {
    const touched = {};
    Object.keys(previewDraft).forEach(f => {
      if (previewDraft[f].join('\u0000') !== (sourceRules[f] || []).join('\u0000')) touched[f] = true;
    });
    Object.keys(previewAliases).forEach(f => {
      if (!aliasesEqual(previewAliases[f], sourceAliases[f] || {})) touched[f] = true;
    });
    const folders = Object.keys(touched);
    if (!folders.length) { previewDirty = false; renderPreview(); return; }
    setStatus(t('Saving the choice…'), 'accent');
    for (const folder of folders) {
      const known = folderRules[folder];
      let r;
      try {
        r = await fetch(`/api/dashboards/${DASH_ID}/generator/input-rules`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ folder: folder,
            exclude_rules: previewDraft[folder] || (sourceRules[folder] || []).slice(),
            file_aliases: previewAliases[folder] || null,
            version: known ? known.version : 0 }),
        });
      } catch (e) { showError(t('Could not save the choice — the server is unreachable.')); setStatus('', ''); return; }
      if (r.status === 409) {
        // Someone changed the rules while the screen was open. Silently overwriting their
        // decision is not allowed — re-read and show what it became.
        await loadFolderRules();
        previewDraft = {};
        previewAliases = {};
        previewDirty = false;
        renderPreview();
        showError(t('Someone changed this folder\'s rules — the list shown is current; check it and try again.'));
        setStatus('', '');
        return;
      }
      if (!r.ok) {
        showError(t('Could not save the choice for folder {p1}.', {p1: folder}));
        setStatus('', '');
        return;
      }
      let d = null;
      try { d = await r.json(); } catch (e) { d = null; }
      const saved = (d && d.exclude_rules) || previewDraft[folder] || [];
      const savedAliases = (d && d.file_aliases) || previewAliases[folder] || {};
      sourceRules[folder] = saved.slice();
      sourceAliases[folder] = savedAliases;
      folderRules[folder] = { source_id: d && d.source_id, version: d && d.version,
        exclude_rules: saved.slice(), file_aliases: savedAliases };
    }
    previewDraft = {};
    previewAliases = {};
    previewDirty = false;
    setStatus(t('Choice saved.'), '');
    renderPreview();
  }

  // ── Run location: "run here" / "on this computer" ────────────────────
  // Contract K1 (the agent endpoint) and K3 (what the page sends) live only here;
  // the run then follows the already existing startAgentRun() path.

  function loadLocation() {
    try { return localStorage.getItem(LOCATION_KEY) === 'local' ? 'local' : 'browser'; }
    catch (e) { return 'browser'; }     // private mode — default to the browser
  }
  function saveLocation(v) {
    try { localStorage.setItem(LOCATION_KEY, v); } catch (e) { /* private mode */ }
  }
  function loadScenario() {
    try { return localStorage.getItem(SCENARIO_KEY) || ''; } catch (e) { return ''; }
  }
  function saveScenario(v) {
    try { localStorage.setItem(SCENARIO_KEY, v || ''); } catch (e) { /* private mode */ }
  }

  // K1: GET /folders?dashboard=<id>[&scenario=<name>].
  // Returns {ok:true, data} or {ok:false, reason, scenarios} — an EXPLICIT
  // refusal with a reason, not an empty result: "silently found nothing" is
  // the worst outcome, the person has nothing to fix.
  async function fetchFolders(scenario) {
    if (!agentBase) return { ok: false, reason: 'unreachable' };
    if (!agentToken) return { ok: false, reason: 'no_token' };
    let q = '?dashboard=' + encodeURIComponent(DASH_ID);
    if (scenario) q += '&scenario=' + encodeURIComponent(scenario);
    let r;
    try {
      r = await fetch(`${agentBase}/folders${q}`, {
        headers: { 'Authorization': 'Bearer ' + agentToken }, cache: 'no-store',
      });
    } catch (e) { return { ok: false, reason: 'unreachable' }; }
    if (r.status === 401) return { ok: false, reason: 'no_token' };
    let j = null;
    try { j = await r.json(); } catch (e) { /* not JSON — becomes http_* below */ }
    if (!r.ok || !j || j.ok !== true) {
      return { ok: false, reason: (j && j.error) || ('http_' + r.status),
        scenarios: (j && j.scenarios) || null };
    }
    return { ok: true, data: j };
  }

  function folderErrorText(res) {
    if (res.reason === 'no_section') {
      return t('folders.ini on this computer has no section for this report — add [folders:{p1}] and name the folders.', {p1: DASH_ID});
    }
    if (res.reason === 'no_scenario') {
      return t('folders.ini has no such folder set. Available: {p1}.',
        {p1: (res.scenarios || []).map(s => s || t('unnamed')).join(', ')});
    }
    if (res.reason === 'no_token') return t('Pairing with the agent is needed — press Run again and approve the request.');
    if (res.reason === 'unreachable') return t('The op-agent stopped answering.');
    return t('The agent could not resolve the folders: {p1}', {p1: String(res.reason || '')});
  }

  // The four reasons a folder goes missing in K1, in plain words and without guessing: "the folder does not exist" and
  // "no access" are fixed in completely different ways.
  function missingReasonText(reason) {
    switch (reason) {
      case 'not_found': return t('folder or file not found');
      case 'denied': return t('no access');
      case 'not_a_dir': return t('not a folder');
      case 'no_match': return t('no file matched the mask');
      default: return String(reason || '');
    }
  }

  // The switch. Drawn exactly once — after a successful probe.
  function renderLocation() {
    if (!locationRow) return;
    locationRow.textContent = '';
    const mk = (value, label) => {
      const input = el('input', { type: 'radio', name: 'op-gen-loc-' + DASH_ID, value: value });
      input.checked = runLocation === value;
      input.addEventListener('change', () => {
        if (!input.checked) return;
        runLocation = value;
        saveLocation(value);
        applyLocation();
      });
      return el('label', { style: 'display:flex;gap:6px;align-items:center;font-size:11px;color:var(--ink)' },
        [input, label]);
    };
    locationRow.appendChild(el('div', { class: 'dock-hint', style: 'margin-bottom:0', text: t('Where to run') }));
    locationRow.appendChild(mk('browser', t('Run here, in the browser')));
    locationRow.appendChild(mk('local', t('Run on this computer (op-agent)')));
    locationRow.style.display = 'flex';
    applyLocation();
  }

  // Apply the chosen location to the panel. Requests nothing — only
  // shows/hides what is already built.
  function applyLocation() {
    const local = runLocation === 'local';
    for (const n of browserOnlyEls) n.style.display = local ? 'none' : '';
    if (local) {
      limitsEl.textContent = t('Folders come from folders.ini on this computer and op-agent does the work. The browser tab\'s limits do not apply here.');
      renderFolderCard();
    } else {
      renderLimits();
      folderCardEl.style.display = 'none';
      folderCardEl.textContent = '';
    }
    if (!running) {
      btnRun.disabled = local ? false : !folderReady;
      btnRun.title = (local || folderReady) ? '' : 'Choose a folder first';
    }
  }

  // How many files and bytes EACH entry of the section produced. A file's `dir` is
  // the `entry_index` of the entry that produced it (K1), so the grouping is exact,
  // not "by folder name".
  function perEntryTotals(info) {
    const by = new Map();
    (info.files || []).forEach(f => {
      const k = f.dir || 0;
      const cur = by.get(k) || { count: 0, bytes: 0 };
      cur.count += 1; cur.bytes += (f.size || 0);
      by.set(k, cur);
    });
    return by;
  }

  // The folder card from the agent's answer — a replacement for the browser folder picker.
  function renderFolderCard() {
    if (!folderCardEl) return;
    folderCardEl.textContent = '';
    if (!folderInfo) { folderCardEl.style.display = 'none'; return; }
    const info = folderInfo;
    const card = el('div', { style: 'border:1px solid var(--border);border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:6px' });

    // Folder set: show the choice only when there really are several.
    const scen = info.scenarios || [];
    if (scen.length > 1) {
      const sel = el('select', { class: 'inp sm' });
      scen.forEach(s => {
        const o = el('option', { value: s, text: s || t('main set') });
        if (s === folderScenario) o.selected = true;
        sel.appendChild(o);
      });
      sel.value = folderScenario;
      sel.addEventListener('change', () => {
        folderScenario = sel.value || '';
        saveScenario(folderScenario);
        reloadFolders().catch(() => { /* the text is already shown */ });
      });
      card.appendChild(el('label', { style: 'display:flex;gap:6px;align-items:center;font-size:11px;color:var(--ink-dim)' },
        [t('Folder set'), sel]));
    }

    const totals = perEntryTotals(info);
    (info.entries || []).forEach(e => {
      const tt = totals.get(e.index) || { count: 0, bytes: 0 };
      const row = el('div', { style: 'display:flex;flex-direction:column' });
      // The real path is the main reason the card exists: the
      // browser has no path at all, and "two identical folders of different departments"
      // differ only by it.
      row.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink);word-break:break-all', text: e.raw }));
      row.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim)',
        text: t('{p1} files · {p2} MB', {p1: tt.count, p2: fmtMB(tt.bytes)})
          + ((e.include && e.include.length) ? ' · ' + e.include.join(', ') : '')
          + ((e.exclude && e.exclude.length) ? ' · −' + e.exclude.join(', ') : '') }));
      card.appendChild(row);
    });

    (info.missing || []).forEach(m => {
      card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--danger);word-break:break-all',
        text: '• ' + m.raw + ' — ' + missingReasonText(m.reason) }));
    });

    // The agent cuts the traversal by time and honestly marks the answer `partial`.
    // Not showing that means letting an incomplete list be read as complete:
    // the person decides half the folders are empty and goes to fix the wrong thing.
    if (info.partial) {
      card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--warning,#b7791f);word-break:break-all',
        text: t('The list is incomplete: the agent ran out of its {p1} s budget on {p2}. This is an estimate, not the full list.',
          {p1: info.budget_s, p2: (info.pending || []).join(', ')}) }));
    }

    const tot = info.totals || { count: 0, bytes: 0 };
    card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink);font-weight:600',
      text: t('Total {p1} files · {p2} MB', {p1: tot.count || 0, p2: fmtMB(tot.bytes || 0)}) }));
    if (info.ini) {
      card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim);word-break:break-all', text: info.ini }));
    }

    folderCardEl.appendChild(card);
    folderCardEl.style.display = '';
  }

  // Re-read the folders from the agent (set change, retry before a run).
  async function reloadFolders() {
    const res = await fetchFolders(folderScenario);
    if (!res.ok) { folderInfo = null; renderFolderCard(); showError(folderErrorText(res)); return false; }
    clearError();
    folderInfo = res.data;
    folderScenario = folderInfo.scenario || '';
    renderFolderCard();
    return true;
  }

  // Checks BEFORE a run in `local` mode. Returns {ok} or
  // {ok:false, status} — the card is already drawn here, the status goes up.
  function checkLocalFolders(info) {
    const files = info.files || [];
    // Zero files is a hard refusal: an empty run would publish a report without
    // data, and that would only be noticed from the numbers.
    if (!files.length) {
      const why = (info.missing || []).map(m => m.raw + ' — ' + missingReasonText(m.reason));
      showError(t('Not a single file was found in these folders — the run was not started.'),
        why.join('\n'));
      return { ok: false, status: t('Not started — no files.') };
    }
    // Duplicate names and missing required_files go through the existing
    // diagnostic cards: the refusal is the same wherever the computing happens.
    const dupes = info.duplicates || [];
    if (dupes.length) {
      showDupes(dupes.map(d => ({ name: d.name, folders: d.paths || [] })));
      return { ok: false, status: t('Not started — duplicate file names across folders.') };
    }
    const requiredPatterns = inputsDecl.length
      ? inputsDecl.filter(i => i.required && i.source !== 'platform').map(i => i.match)
      : ((manifest && manifest.required_files) || []);
    const names = files.map(f => f.name);
    // File-name aliases are NOT applied here: the list comes from the agent,
    // which does not know about them yet. Propagating them is separate work; silently
    // pretending they apply here too is not allowed.
    const missing = requiredPatterns.filter(pat => !names.some(n => globToRe(pat).test(n)));
    if (missing.length) {
      showRequiredMissing(missing);
      return { ok: false, status: t('Not started — a required input file is missing.') };
    }
    // A soft warning: not a ban, but a chance to notice `dir = C:\` before
    // the agent spends an hour walking the disk.
    const bytes = (info.totals && info.totals.bytes) || 0;
    if (files.length > LOCAL_SOFT_FILES || bytes > LOCAL_SOFT_BYTES) {
      showWarn(t('That is a lot: {p1} files, {p2} MB. Check that folders.ini names the folder you meant and not a whole drive.',
        {p1: files.length, p2: fmtMB(bytes)}));
    }
    return { ok: true };
  }

  // K3: what the page sends the server for a `local` run.
  function buildLocalInputManifest(info) {
    const files = (info.files || []).map(f => ({
      name: f.name, size: f.size, mtime_ms: f.mtime_ms, dir: f.dir || 0, path: f.path,
    }));
    const roots = (info.roots || []).map(r => ({
      dir: r.dir || 0, root_path: r.root_path, root_id: r.root_id,
    }));
    const bytes = (info.totals && info.totals.bytes) || files.reduce((s, f) => s + (f.size || 0), 0);
    return {
      source: 'folders',
      scenario: info.scenario || '',
      files: files,
      // The platform's source key is the folder NAME, as in the browser.
      // The full path travels alongside in roots[].root_path. Sending the path here would make
      // the same folder create a SECOND DataSource (lookup goes by key_name), and
      // the history of a report run both in the browser and locally would split apart.
      // The agent computes the name (_root_name): parsing D:\ and UNC is that OS's job.
      dirs: roots.map(r => r.name || r.root_path),
      roots: roots,
      total_bytes: bytes,
      count: files.length,
      skipped: [],
    };
  }

  // The probe when the panel loads. Any failure means silence: no agent
  // is normal, not a report error.
  async function probeLocal() {
    if (manifest && manifest.sources) return;       // GreenPlum: the location is not up for discussion
    let h = null;
    try { h = await detectAgent(); } catch (e) { h = null; }
    if (!h || h.protocol !== AGENT_PROTOCOL) return;
    try { agentToken = localStorage.getItem(AGENT_TOKEN_KEY); } catch (e) { agentToken = null; }
    // Pairing silently without a token is not allowed: pairing needs a person at that
    // machine. So we offer nothing — the first time the person goes through it
    // themselves, once they have a token from another report.
    // The catch-up queue is asked FIRST and separately from the folders: the run is already
    // computed, and it must be published even if the folders.ini section was
    // removed since or the network drive dropped off. Tying one question to
    // the other would mean losing finished numbers because of a setting that
    // no longer relates to them.
    await loadCatchup();
    const res = await fetchFolders(loadScenario());
    if (!res.ok) return;
    folderInfo = res.data;
    folderScenario = folderInfo.scenario || '';
    runLocation = loadLocation();
    renderLocation();
  }

  // ── Catch-up: ask, show, publish ─────────────────────────────────────
  // Three agent endpoints (`GET /local-runs`, `GET /local-runs/output`,
  // `POST /local-runs/published`) and NOT ONE new server endpoint: publishing
  // goes through the same `POST …/generator/runs` + `…/publish` used to publish
  // a normal run. There is exactly one difference and it is honest — the start manifest
  // carries an `offline` mark, from which the server writes into the version
  // note that the numbers were collected earlier and without a connection.

  async function fetchLocalRuns() {
    if (!agentBase || !agentToken) return [];
    let r;
    try {
      r = await fetch(`${agentBase}/local-runs?dashboard=${encodeURIComponent(DASH_ID)}`, {
        headers: { 'Authorization': 'Bearer ' + agentToken }, cache: 'no-store',
      });
    } catch (e) { return []; }
    if (!r.ok) return [];
    let j = null;
    try { j = await r.json(); } catch (e) { return []; }
    if (!j || j.ok !== true || !Array.isArray(j.runs)) return [];
    // The agent returns the queue for the whole mirror when it was not asked about
    // a specific report; the filter here guards against someone else's run
    // ending up in THIS report, should the endpoint one day start answering more broadly.
    return j.runs.filter(x => x && String(x.dashboard_id) === String(DASH_ID)
                              && !x.published && x.k != null);
  }

  async function loadCatchup() {
    try { catchupRuns = await fetchLocalRuns(); }
    catch (e) { catchupRuns = []; }
    renderCatchup();
  }

  // The run date in human words. `created_at` is ISO from the agent; if
  // it does not parse, print the string as is rather than inventing "today".
  function catchupWhen(iso) {
    const d = new Date(String(iso || ''));
    if (isNaN(d.getTime())) return String(iso || '');
    return OP_I18N.fmtDate(d, { year: 'numeric', month: 'long', day: 'numeric',
                                hour: '2-digit', minute: '2-digit' });
  }

  function renderCatchup() {
    if (!catchupEl) return;
    catchupEl.textContent = '';
    if (!catchupRuns.length) { catchupEl.style.display = 'none'; return; }
    // The freshest run is the last one computed; it is the one that becomes
    // the new version. The others are shown as a count but not published: two
    // versions in a row from one queue are two versions with the same
    // numbers, and the second only makes the history harder to read.
    const rec = catchupRuns[catchupRuns.length - 1];
    const card = el('div', { style: 'border:1px solid var(--accent);border-radius:8px;padding:10px;background:color-mix(in oklab,var(--accent) 7%,transparent);display:flex;flex-direction:column;gap:6px' });
    card.appendChild(el('div', { style: 'font-size:12px;font-weight:600;color:var(--ink)',
      text: t('There is a report computed on this machine while offline') }));
    card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink)',
      text: t('Built {p1}. The platform does not have it yet.', {p1: catchupWhen(rec.created_at)}) }));
    if (rec.scenario) {
      card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim)',
        text: t('Folder set: {p1}', {p1: rec.scenario}) }));
    }
    if (catchupRuns.length > 1) {
      // Keeping quiet about the others is not allowed: the person computed three times and has the right to know
      // that the last one will be published, not "some one".
      card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--ink-dim)',
        text: t('There are {p1} unpublished runs; the newest one becomes the new version.', {p1: catchupRuns.length}) }));
    }
    if (rec.bridge === 'none') {
      card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--warning,#b7791f)',
        text: t('The local copy had no bridge to the data, but the files themselves will go to the platform — the report assembles fully there.') }));
    }
    if (catchupBusy) {
      card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--accent)',
        text: t('Publishing…') }));
      catchupEl.appendChild(card);
      catchupEl.style.display = '';
      return;
    }
    const row = el('div', { style: 'display:flex;gap:6px' });
    const yes = el('button', { class: 'btn xs primary', type: 'button', text: t('Publish to the platform') });
    yes.addEventListener('click', () => {
      publishCatchup(rec).catch(err => reportError(t('Catch-up failed'), err));
    });
    const no = el('button', { class: 'btn xs', type: 'button', text: t('Not now') });
    // "Not now" hides the INSET, not the run: with the agent it stays
    // unpublished and will ask again the next time the page opens.
    // Marking it published here would mean losing the numbers at the press of
    // a "later" button.
    no.addEventListener('click', () => { catchupRuns = []; renderCatchup(); });
    row.appendChild(yes); row.appendChild(no);
    card.appendChild(row);
    catchupEl.appendChild(card);
    catchupEl.style.display = '';
  }

  async function fetchCatchupOutput(k, name) {
    const q = '?dashboard=' + encodeURIComponent(DASH_ID)
      + '&k=' + encodeURIComponent(String(k))
      + '&name=' + encodeURIComponent(name);
    const r = await fetch(`${agentBase}/local-runs/output${q}`,
      { headers: { 'Authorization': 'Bearer ' + agentToken } });
    if (!r.ok) throw new Error('local output ' + name + ' ' + r.status);
    return await r.arrayBuffer();
  }

  // The order of steps here is not style but a requirement: the run on the platform
  // is created FIRST (it holds the lock and gives the `run_id`, without which there is nowhere
  // to send lineage), the outputs are fetched SECOND, the "published" mark
  // is set on the agent LAST and only after a successful publish — otherwise
  // the run would vanish from the queue without arriving.
  async function publishCatchup(rec) {
    if (running || catchupBusy) return;
    catchupBusy = true;
    renderCatchup();
    clearError(); clearDiag(); showWarn(''); warnEl.style.display = 'none';
    publishInfo = null;
    runId = null;
    obsQueue = []; obsStages = []; obsSeq = 0; obsStripSchema = false;
    obsFlushChain = Promise.resolve(); obsFinalSent = false; obsActive = false;
    mark('op:gen:total:t0');

    // 1. The run on the platform. The start manifest is the very one the agent
    // saved next to the outputs: paths, roots and masks as of the COMPUTATION, not
    // as of today. Rebuilding it now would write into the
    // lineage folders these numbers were never read from.
    const im = Object.assign({}, (rec.input_manifest && typeof rec.input_manifest === 'object')
      ? rec.input_manifest : { source: 'folders', files: [], dirs: [] });
    im.offline = { created_at: rec.created_at || '', k: rec.k };
    setStatus(t('Starting run…'), 'accent');
    let res;
    try {
      res = await fetch(`/api/dashboards/${DASH_ID}/generator/runs`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ engine: 'local', input_manifest: im }),
      });
    } catch (e) {
      catchupBusy = false; renderCatchup();
      showError(t('Cannot reach OnePage server.')); setStatus('', ''); return;
    }
    if (res.status === 409) {
      let d = {}; try { d = (await res.json()).detail || {}; } catch (e) { /* ignore */ }
      catchupBusy = false; renderCatchup();
      showError(t('Another run is already in progress{p1}. Wait for it to finish.', {p1: d.locked_by ? ' (' + d.locked_by + ')' : ''}));
      setStatus('', ''); return;
    }
    if (res.status !== 201) {
      let tv = ''; try { tv = await res.text(); } catch (e) { /* ignore */ }
      catchupBusy = false; renderCatchup();
      showError(t('Run did not start: {p1}{p2}', {p1: res.status, p2: (tv ? ' ' + tv.slice(0, 300) : '')}));
      setStatus('', ''); return;
    }
    const run = await res.json();
    runId = run.run_id;
    setBusy(true);
    startHeartbeat(run.heartbeat_every_s);

    try {
      // 2. Lineage of the offline run. The frames were captured during the computation and
      // cannot be obtained anywhere else — put them in the same queue that
      // a normal run uses, and send them as the final batch before
      // publishing (same as the browser path).
      for (const fr of (rec.observe || [])) {
        if (fr && typeof fr === 'object') enqueueObservations(fr.files || [], fr.stages);
      }

      // 3. The outputs — from the agent, exactly the files the generator wrote.
      setStatus(t('Loading results from agent…'), 'accent');
      const outs = rec.outputs || {};
      const fd = new FormData();
      if (outs.html) {
        const buf = await fetchCatchupOutput(rec.k, outs.html);
        fd.append('html_file', new Blob([buf], { type: 'text/html' }), 'dashboard.html');
      }
      for (const idx in (outs.slots || {})) {
        const name = outs.slots[idx];
        const buf = await fetchCatchupOutput(rec.k, name);
        fd.append('slot_' + idx, new Blob([buf]), name);
      }

      await flushObservations(true);
      setStatus(t('Publishing… {ctx:2}'), 'accent');
      mark('op:gen:pub:t0');
      const pr = await fetch(`/api/dashboards/${DASH_ID}/generator/runs/${runId}/publish`, { method: 'POST', body: fd });
      if (!pr.ok) {
        let tv = ''; try { tv = await pr.text(); } catch (e) { /* ignore */ }
        throw new Error('publish ' + pr.status + (tv ? ' ' + tv.slice(0, 300) : ''));
      }
      try { publishInfo = await pr.json(); } catch (e) { publishInfo = null; }
      measure('op:gen:publish', 'op:gen:pub:t0');
      measure('op:gen:total', 'op:gen:total:t0', { border_decision: 'catchup' });
    } catch (err) {
      catchupBusy = false;
      await reportError(t('Catch-up failed'), err);
      renderCatchup();                 // the inset stays: the run was not published
      return;
    }

    // 4. Only now — the mark on the agent. A failure HERE does not undo
    // the publish (the version is already on the platform), so it does not fail the step but
    // is said out loud: otherwise the person would publish the same numbers a second time.
    let marked = true;
    try {
      const mr = await fetch(`${agentBase}/local-runs/published`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + agentToken },
        body: JSON.stringify({ dashboard: DASH_ID, k: rec.k }),
      });
      marked = mr.ok;
    } catch (e) { marked = false; }

    stopHeartbeat();
    catchupBusy = false;
    catchupRuns = [];
    renderCatchup();
    resetUI();
    setStatus(t('Published ✓'), 'ok');
    showPublishWarnings(publishInfo && publishInfo.warnings, publishInfo && publishInfo.quality_url);
    const card = el('div', { style: 'display:flex;flex-direction:column;gap:6px;margin-top:6px' });
    if (!marked) {
      card.appendChild(el('div', { class: 'mono', style: 'font-size:11px;color:var(--warning,#b7791f)',
        text: t('The report was published, but the agent did not mark the run as published — it will offer the catch-up again. There is no need to publish twice.') }));
    }
    const btnReload = el('button', { class: 'btn xs primary', type: 'button', text: t('Show new report') });
    btnReload.addEventListener('click', () => location.reload());
    card.appendChild(btnReload);
    diagEl.textContent = ''; diagEl.appendChild(card); diagEl.style.display = '';
  }

  // ── Run ──────────────────────────────────────────────────────────────
  // withAux=false → the plain main-folder run (unchanged); withAux=true →
  // the second button: main folder + the aux folder's required input file(s).
  async function doRun(withAux) {
    if (running) return;
    if (engineMode === 'agent') { return startAgentRun(); }   // GP: no folders, own flow
    // "Run on this computer": the same agent path, but in folders mode —
    // the data comes from folders.ini, not from GreenPlum.
    if (runLocation === 'local') { return startAgentRun('folders'); }
    if (!folderReady) return;
    if (withAux && !auxFolderReady) return;
    clearError(); clearDiag(); showWarn(''); warnEl.style.display = 'none';
    publishInfo = null;              // last run's warnings must not survive into this one
    seenPhase.clear();
    // Reset runId BEFORE any early return (permission denial, enumerate/perm
    // errors below) can hit doRun().catch(reportError) — otherwise a failure
    // on THIS run would post its /error and observations{final:true} to the
    // PREVIOUS run's id (review finding).
    runId = null;
    obsQueue = []; obsStages = []; obsSeq = 0; obsStripSchema = false; obsFlushChain = Promise.resolve(); obsFinalSent = false; obsActive = false;   // a new run starts its own observation stream
    mark('op:gen:total:t0');

    // Ensure permission is live (this click is a user gesture).
    try {
      let perm = await dirHandle.queryPermission({ mode: pickMode() });
      if (perm !== 'granted') perm = await dirHandle.requestPermission({ mode: pickMode() });
      if (perm !== 'granted') { showError('Folder permission is required to run.'); return; }
    } catch (e) { showError('Could not access the folder.'); return; }
    if (withAux) {
      try {
        let perm = await auxDirHandle.queryPermission({ mode: 'read' });
        if (perm !== 'granted') perm = await auxDirHandle.requestPermission({ mode: 'read' });
        if (perm !== 'granted') { showError('Input folder permission is required to run.'); return; }
      } catch (e) { showError('Could not access the input folder.'); return; }
    }
    // Extra input folders: same gesture-scoped permission dance as the main
    // folder, each with its own read/readwrite mode. A denial stops the run.
    for (const x of extraDirs) {
      const mode = x.w ? 'readwrite' : 'read';
      try {
        let perm = await x.h.queryPermission({ mode: mode });
        if (perm !== 'granted') perm = await x.h.requestPermission({ mode: mode });
        if (perm !== 'granted') { showError(`Folder permission is required: ${x.h.name}`); return; }
        x.ready = true;
      } catch (e) { showError(`Could not access the folder: ${x.h.name}`); return; }
    }

    // Aux inputs: strict-named files read from the aux folder (buffers kept,
    // streamed to the worker). A missing one stops the run before it starts.
    let auxFiles = [];
    if (withAux) {
      for (const aux of (manifest.aux_inputs || [])) {
        let fh;
        try { fh = await auxDirHandle.getFileHandle(aux.name); }
        catch (e) { showAuxMissing(aux.name); setStatus('Not started — input file missing.', 'err'); return; }
        const f = await fh.getFile();
        auxFiles.push({ name: aux.name, mtime_ms: f.lastModified, size: f.size, buf: await f.arrayBuffer() });
      }
    }

    // Platform reference workbooks (ADR-16): the server renders each applied
    // table into the fixed layout the generator reads (<slug>.xlsx, sheet
    // "data", key first). They join auxFiles BEFORE the dedupe and the
    // required-pattern union below, so the manifest's platform input is
    // satisfied by them exactly the way a folder file would satisfy it — the
    // worker and add_file stay untouched.
    const platformFiles = [];
    for (const tv of refBlock()) {
      if (!tv.applied) continue;
      const n = tv.version == null ? 'current' : String(tv.version);
      let buf;
      try {
        const rr = await fetch(`/api/reference/${encodeURIComponent(tv.slug)}/versions/${n}/xlsx`);
        if (!rr.ok) throw new Error('reference ' + rr.status);
        buf = await rr.arrayBuffer();
      } catch (e) {
        // Never start a run without a dictionary the analyst asked to apply:
        // the report would silently show raw codes and nobody would know the
        // difference until someone compared two versions by hand.
        showError(t('Failed to load reference table «{p1}» — run not started. Try again or uncheck «Apply reference table».', {p1: tv.slug}));
        setStatus('Not started — reference table could not be loaded.', 'err');
        return;
      }
      platformFiles.push({ name: tv.slug + '.xlsx', mtime_ms: Date.now(), size: buf.byteLength, buf: buf, platform: true });
    }
    if (platformFiles.length) auxFiles = platformFiles.concat(auxFiles);

    // ref_lookup specs are handed the CURRENT (or pinned) normalised keys once
    // per slug — never per file: the same 50k-key list would otherwise be
    // fetched for every workbook the check runs over. A table that does not
    // exist leaves `keys` absent and the check reports itself skipped rather
    // than declaring every value unmapped.
    const refKeySlugs = [];
    checksDecl.forEach(c => {
      if (c.type === 'ref_lookup' && c.table && refKeySlugs.indexOf(c.table) < 0) refKeySlugs.push(c.table);
    });
    // Wipe whatever a PREVIOUS run attached: checksDecl is module state read
    // once at init, so a keys fetch that fails on run 2 would otherwise leave
    // run 1's key set in place and the check would compare against a stale
    // dictionary instead of reporting itself skipped (`no_keys`).
    checksDecl.forEach(ch => { if (ch.type === 'ref_lookup') delete ch.keys; });
    for (const slug of refKeySlugs) {
      const c = refChoices[slug];
      const n = (c && c.applied && c.version != null) ? String(c.version) : 'current';
      let keys = null;
      try {
        const rk = await fetch(`/api/reference/${encodeURIComponent(slug)}/versions/${n}/keys`);
        if (rk.ok) {
          const j = await rk.json();
          if (Array.isArray(j.keys)) keys = j.keys;
        }
      } catch (e) { /* no keys -> the check reports no_keys, never a false miss */ }
      if (keys) checksDecl.forEach(ch => { if (ch.type === 'ref_lookup' && ch.table === slug) ch.keys = keys; });
    }

    setStatus('Scanning folder…', 'accent');
    // Union of the main folder (dir 0) and each extra folder (dir 1..N). Every
    // file is tagged with its source dir so the worker reads it from the right
    // handle; the skipped lists are concatenated for the diagnosis card.
    mark('op:gen:enum:t0');
    let files = [], skipped = [];
    for (let i = 0; i <= extraDirs.length; i++) {
      const r = await enumerate(i === 0 ? dirHandle : extraDirs[i - 1].h);
      for (const f of r.files) { f.dir = i; files.push(f); }
      for (const s of r.skipped) { s.dir = i; skipped.push(s); }
    }
    measure('op:gen:enumerate', 'op:gen:enum:t0');

    // The aux copy is authoritative: drop any same-named entry from any folder.
    if (auxFiles.length) {
      const auxNames = new Set(auxFiles.map(a => a.name));
      files = files.filter(f => !auxNames.has(f.name));
    }
    files.sort((a, b) => a.name.localeCompare(b.name));

    // Duplicate basenames across folders: the worker keys files by name, so a
    // name in 2+ folders is ambiguous. One Map pass, stop before the run.
    const dirNames = [dirHandle.name].concat(extraDirs.map(x => x.h.name));
    const byName = new Map();
    for (const f of files) {
      const seen = byName.get(f.name);
      if (seen) seen.push(f.dir); else byName.set(f.name, [f.dir]);
    }
    const dupes = [];
    byName.forEach((dirs, name) => { if (dirs.length > 1) dupes.push({ name: name, folders: dirs.map(d => dirNames[d]) }); });
    if (dupes.length) {
      showDupes(dupes);
      setStatus('Not started — duplicate file names across folders.', 'err');
      return;
    }

    // required_files / required inputs: patterns the generator cannot run
    // without. Matched by glob (case-insensitive, same rule as enumerate's
    // allow-list) against the union of folder names and aux inputs — a
    // required entry needs only ONE match, in any folder. inputsDecl already
    // folds required_files into itself (genmanifest.inputs_for) once the
    // server ships it; until then this falls back to the plain names, still
    // glob-tested so the check stays case-insensitive either way.
    // ADR-16: a `source: "platform"` input is NEVER the analyst's to provide —
    // the server materialises it, and only when "Apply reference table" is on.
    // Counting it as required would make "without reference table" (a mode ADR-16
    // requires to keep working) fail with "required input file missing", which
    // the analyst has no way to satisfy.
    const requiredPatterns = inputsDecl.length
      ? inputsDecl.filter(i => i.required && i.source !== 'platform').map(i => i.match)
      : (manifest.required_files || []);
    const allNames = Array.from(byName.keys()).concat(auxFiles.map(a => a.name));
    // A file let in by an alias closes a required input: its NAME does not fit
    // the mask (that is the whole problem), but matched_input is exactly that mask.
    const aliasedInputs = new Set(files.map(f => f.matched_input).filter(Boolean));
    const missing = requiredPatterns.filter(
      pat => !aliasedInputs.has(pat) && !allNames.some(n => globToRe(pat).test(n)));
    if (missing.length) {
      showRequiredMissing(missing);
      setStatus('Not started — required input file missing.', 'err');
      return;
    }

    // Border counts aux files in files-count / total-bytes / max-file totals.
    const borderFiles = files.concat(auxFiles.map(a => ({ name: a.name, size: a.size, mtime_ms: a.mtime_ms })));
    const totalBytes = borderFiles.reduce((s, f) => s + f.size, 0);
    const maxFile = borderFiles.reduce((m, f) => Math.max(m, f.size), 0);

    const border = checkBorder(borderFiles, totalBytes, maxFile);
    if (border.blocking.length) {
      showDiag(border.blocking, borderFiles, totalBytes, skipped);
      setStatus('Not started — no input files in the folder.', 'err');
      return;
    }
    if (border.advisory.length && !borderAck) {
      showBorderWarning(border.advisory, borderFiles, totalBytes, skipped, () => {
        borderAck = true;
        doRun(withAux);   // re-walks the folder — the click is the gesture
      });
      setStatus('Paused — folder is over the recommended size.', 'err');
      return;
    }

    setStatus('Starting run…', 'accent');
    // `dir` now always rides along (0 for the main folder) — the server-side
    // ingest (A3) needs it on every row to attribute bytes to the right
    // data_source, not just when there is more than one folder. `matched_input`
    // rides too when the allow-list assigned one. Aux entries carry metadata
    // only (no buffers).
    const manifestFiles = files.map(f => {
      const o = { name: f.name, size: f.size, mtime_ms: f.mtime_ms, dir: f.dir || 0 };
      if (f.matched_input) o.matched_input = f.matched_input;
      return o;
    }).concat(auxFiles.filter(a => !a.platform)
      .map(a => ({ name: a.name, size: a.size, mtime_ms: a.mtime_ms, aux: true })));
    const input_manifest = { files: manifestFiles, total_bytes: totalBytes, count: manifestFiles.length, skipped };
    // The dictionaries this run resolves. Platform workbooks are deliberately
    // absent from `files`: they are not the analyst's data, they are the
    // platform's own, and `observations` would otherwise file them against a
    // folder they never came from.
    const refUses = refBlock();
    if (refUses.length) input_manifest.reference = refUses;
    // Always recorded, including single-folder runs: these names are the only
    // durable answer to "which folders does this report read?" for anyone who
    // opens it later. FSA never gives us a path, so the name is all there is.
    input_manifest.dirs = dirNames;
    let res;
    try {
      res = await fetch(`/api/dashboards/${DASH_ID}/generator/runs`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ engine: 'browser', input_manifest }),
      });
    } catch (e) { showError('Could not reach the server to start the run.'); setStatus('', ''); return; }

    if (res.status === 409) {
      let d = {};
      try { d = (await res.json()).detail || {}; } catch (e) { /* ignore */ }
      showError(`Another run is in progress${d.locked_by ? ' by ' + d.locked_by : ''}`
        + `${d.age_s != null ? ' (started ' + d.age_s + 's ago)' : ''}. Try again once it finishes.`);
      setStatus('', '');
      return;
    }
    if (res.status !== 201) {
      let tv = ''; try { tv = await res.text(); } catch (e) { /* ignore */ }
      showError('Run could not start: ' + res.status + (tv ? ' ' + tv.slice(0, 300) : ''));
      setStatus('', '');
      return;
    }

    const run = await res.json();
    runId = run.run_id;
    currentFiles = files;
    // Keyed by dir index (once A3 ships them) — this page never reads them
    // itself, only forwards them into the worker's start message so the
    // observation batches it posts back can be attributed server-side.
    observationIds = run.observation_ids || {};
    lastKnown = run.last_known || {};   // {name: [size, mtime_ms]} — worker skips sniffing an unchanged file
    // Copy aux buffers BEFORE startWorker transfers (detaches) the originals —
    // the provenance upload needs its own bytes. Aux files are small.
    // Platform workbooks are excluded: /aux accepts only names the manifest
    // declares as aux_inputs, and the server already has every byte of a
    // reference version anyway (run_reference_uses records which one).
    const auxUpload = auxFiles.filter(a => !a.platform).map(a => ({ name: a.name, buf: a.buf.slice(0) }));
    startWorker(run.bundle_url, run.heartbeat_every_s, auxFiles);
    if (auxUpload.length) uploadAux(auxUpload);   // non-fatal, fire-and-forget
  }

  // Provenance: store each aux input on the server. Never blocks the run.
  async function uploadAux(items) {
    for (const it of items) {
      try {
        const fd = new FormData();
        fd.append('file', new Blob([it.buf]), it.name);
        const r = await fetch(`/api/dashboards/${DASH_ID}/generator/runs/${runId}/aux`, { method: 'POST', body: fd });
        if (!r.ok) throw new Error('aux ' + r.status);
      } catch (e) {
        showWarn('Could not store the input file on the server — the run continues.');
      }
    }
  }

  // ── Worker lifecycle ────────────────────────────────────────────────
  function startWorker(bundleUrl, everyS, auxFiles) {
    auxFiles = auxFiles || [];
    obsActive = true;      // the worker observes by construction — see obsActive
    setBusy(true);
    startHeartbeat(everyS);
    startObsTimer();
    // type: 'module' is required — Pyodide 314 refuses classic workers, and
    // the worker boots it via dynamic import('pyodide.mjs').
    worker = new Worker(`/static/js/gen-worker.js?v=${encodeURIComponent(ASSET_V)}`, { type: 'module' });
    worker.onmessage = onWorkerMessage;
    worker.onerror = (e) => { reportError('Worker crashed', { message: e.message || 'worker error', stack: (e.filename ? e.filename + ':' + e.lineno : '') }); };
    worker.postMessage({
      type: 'start',
      dirHandles: [dirHandle].concat(extraDirs.map(x => x.h)),   // index 0 = main; f.dir selects
      files: currentFiles,
      auxFiles: auxFiles,         // {name,mtime_ms,size,buf}; buffers transferred below
      manifest: manifest,
      bundleUrl: bundleUrl,
      pyodideBase: PYODIDE_BASE,
      schemaVersion: checksum,    // == checksum_sha256 of this generator version
      generatorId: generatorId,
      inputs: inputsDecl,         // Data Lineage v2: declared inputs, for install_hooks() to match against
      checks: checksDecl,         // Data Quality v1 (P1): manifest checks, filtered per file by matched_input
      lastKnown: lastKnown,       // {name: [size, mtime_ms]} — skip re-sniffing an unchanged file
      forceDeps: forceDeps,       // deps the server force-installs regardless of the manifest (e.g. python-calamine)
    }, auxFiles.map(a => a.buf));
  }

  function onWorkerMessage(e) {
    const m = e.data;
    // Phase 9.3: the same worker also speaks /explore's protocol; those
    // replies are correlated by id and belong to lakeStep, never to the run.
    if (m.type && String(m.type).slice(0, 8) === 'explore:') { onExploreMessage(m); return; }
    if (m.type === 'progress') onProgress(m);
    else if (m.type === 'warning') showWarn(m.msg || 'warning');
    else if (m.type === 'observe') enqueueObservations(m.files || [], m.stages);
    else if (m.type === 'done') onDone(m.outputs || {}, m.gauges || {});
    else if (m.type === 'error') reportError('Run failed', { message: m.message, stack: m.detail });
  }

  // ── Explore channel on the run worker (phase 9.3) ────────────────────
  // The run's worker is also the /explore engine (gen-worker.js runs both,
  // sharing nothing but the boot code), so the report index is built in the
  // process that just read the folder — no second Worker, no second Pyodide.
  // Same id-correlated request/response idiom as explore.js's `send`, minus
  // the timeout: this step runs after the report is already published, so a
  // wedged engine costs nothing but a missing index.
  let exploreSeq = 0;
  const explorePending = new Map();   // id -> resolve
  let exploreReadyResolve = null;

  function onExploreMessage(m) {
    if (m.type === 'explore:ready') {
      if (exploreReadyResolve) { exploreReadyResolve(m); exploreReadyResolve = null; }
      return;
    }
    if (m.type === 'explore:error') {
      const p = m.id != null && explorePending.get(m.id);
      if (p) { explorePending.delete(m.id); p({ error: m.message || 'engine error' }); return; }
      if (exploreReadyResolve) { exploreReadyResolve({ error: m.message || 'engine error' }); exploreReadyResolve = null; }
      return;
    }
    if (m.id != null && explorePending.has(m.id)) {
      const p = explorePending.get(m.id);
      explorePending.delete(m.id);
      p(m);
    }
  }

  function exploreSend(type, payload, transfer) {
    if (!worker) return Promise.resolve({ error: 'engine gone' });
    // 'explore:init' is answered by 'explore:ready', which carries no id.
    if (type === 'explore:init') {
      return new Promise(resolve => {
        exploreReadyResolve = resolve;
        worker.postMessage(Object.assign({ type: type }, payload));
      });
    }
    const id = ++exploreSeq;
    return new Promise(resolve => {
      explorePending.set(id, resolve);
      worker.postMessage(Object.assign({ type: type, id: id }, payload), transfer || []);
    });
  }

  // Read one of the run's OWN files back, from the handle the run used.
  async function lakeReadFile(rec) {
    const handles = [dirHandle].concat(extraDirs.map(x => x.h));
    const h = handles[rec.dir || 0];
    if (!h) throw new Error('folder handle gone');
    const fh = await fileHandleAt(h, rec.path || rec.name);
    const f = await fh.getFile();
    return f.arrayBuffer();
  }
  // Walk `a/b/c.xlsx` from a directory handle.
  async function fileHandleAt(dir, relPath) {
    const parts = String(relPath).split('/').filter(Boolean);
    let d = dir;
    for (let i = 0; i < parts.length - 1; i++) d = await d.getDirectoryHandle(parts[i]);
    return d.getFileHandle(parts[parts.length - 1]);
  }

  function runLakeStep() {
    return lakeStep({
      dashId: DASH_ID,
      runId: runId,
      pyodideBase: PYODIDE_BASE,
      lakeEnabled: !(manifest && manifest.lake === false),
      files: currentFiles,
      readFile: lakeReadFile,
      send: exploreSend,
      // The run is already published — keep that on the line, with the index
      // step's own progress beside it.
      setStatus: (tv) => setStatus('Published ✓ · ' + tv, 'ok'),
      // The publish-time warnings (skipped files) are already on screen by
      // now — append rather than replace, or the index note eats them.
      showWarn: (tv) => showWarn(warnEl.textContent ? warnEl.textContent + ' · ' + tv : tv),
      fetch: (u, o) => fetch(u, o),
    });
  }

  // ── Observation batching (Data Lineage v2, B1) ──────────────────────
  // The worker's 'observe' messages are per-file; batching them keeps the
  // number of round trips independent of file count. Flushed on size (>=25
  // files), on a 5 s timer, and finally (final:true) at run end — worker
  // 'done', the error path, and just before publish all converge here.
  function enqueueObservations(files, stages) {
    if ((files && files.length) || (stages && stages.length)) obsActive = true;
    for (const st of (stages || [])) {
      if (obsStages.length >= 32) break;   // mirrors _lineage.MAX_STAGES and the ingest cap
      obsStages.push(st);
    }
    if (!files || !files.length) return;
    for (const f of files) obsQueue.push(f);
    if (obsQueue.length >= 25) flushObservations(false);
  }

  function startObsTimer() {
    stopObsTimer();
    obsTimer = setInterval(() => { flushObservations(false); }, 5000);
  }
  function stopObsTimer() { if (obsTimer) { clearInterval(obsTimer); obsTimer = null; } }

  // Public entry point: chains onto obsFlushChain instead of firing its own
  // fetch directly, so two triggers that land close together (size trigger
  // vs. the 5s timer, or a final flush racing a still-in-flight batch) run
  // one at a time in call order — matching the seq numbers handed out below.
  // _doFlushObservations never rejects, so the chain itself never breaks.
  function flushObservations(final) {
    // Gate on DATA, not on the engine name. This used to say
    // `if (engineMode === 'agent') return` — because engine #2 (GreenPlum)
    // sends no observations, and an empty POST …/observations {final:true} would be
    // a lie about the run. But as soon as the agent learned to read folders (branch G),
    // that condition started silencing THE observations that actually arrived too:
    // the engine name no longer answers the question "is there anything to send".
    // obsActive answers it — any source of observations raises it.
    if (!obsActive) return obsFlushChain;
    if (final) {
      if (obsFinalSent) return obsFlushChain;   // already sent for this run — see obsFinalSent's own comment
      obsFinalSent = true;
    }
    obsFlushChain = obsFlushChain.then(() => _doFlushObservations(final));
    return obsFlushChain;
  }

  // Mirrors app/api/generators.py `_MAX_OBS_BODY`. See shrinkObservationsBody
  // above for why this is applied proactively rather than left to a 413.
  const OBS_BODY_CAP_BYTES = 2 * 1024 * 1024;

  // Never throws — a lineage write is best-effort and must never block the
  // run it describes. 409 (stale seq) / 413 (batch too big) are logged and
  // the batch is DROPPED, not retried with the same seq (a retry would just
  // repeat whichever problem caused the rejection).
  async function _doFlushObservations(final) {
    if (!runId) { obsQueue.length = 0; return; }
    if (!obsQueue.length && !final) return;
    const files = obsQueue.splice(0, obsQueue.length);
    const payloadFiles = obsStripSchema ? files.map(_stripSchema) : files;
    const payload = { seq: ++obsSeq, files: payloadFiles, final: !!final };
    let stages = null;
    if (final && obsStages.length) {
      stages = obsStages.splice(0, obsStages.length);
      payload.stages = obsStripSchema ? stages.map(_stripStage) : stages;
    }
    const shrunk = shrinkObservationsBody(payload, files, stages, obsStripSchema, OBS_BODY_CAP_BYTES);
    if (shrunk.stripped) obsStripSchema = true;   // carries forward to later batches this run too
    const body = shrunk.body;
    const url = `/api/dashboards/${DASH_ID}/generator/runs/${runId}/observations`;
    try {
      // keepalive lets the request survive a page/tab teardown that follows
      // right on the heels of this call (error report, publish navigation) —
      // but the browser caps keepalive request bodies (~64 KB), so a fat
      // final batch falls back to a plain fetch instead of silently failing.
      const useKeepalive = final && new Blob([body]).size <= 61440;
      const r = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body,
        keepalive: useKeepalive,
      });
      if (r.status === 409 || r.status === 413) {
        console.warn('generator observations batch dropped:', r.status);
        if (r.status === 413) obsStripSchema = true;   // shed the heaviest field for what's left of the run
      }
    } catch (e) { console.warn('generator observations flush failed:', e); }
  }

  // Stage timing: the Worker announces coarse phase starts; the page places
  // the marks/measures (perf.js lives here, not in the Worker).
  function onProgress(m) {
    setStatus(stageLabel(m), 'accent');
    if (m.total && m.total > 0) { progressEl.max = m.total; progressEl.value = m.done || 0; }
    handlePhase(m.stage);
  }

  function stageLabel(m) {
    switch (m.stage) {
      case 'boot': return 'Booting Python runtime…';
      case 'deps': return 'Installing dependencies…';
      case 'bundle': return 'Loading generator bundle…';
      case 'files': return `Reading files ${(m.done || 0) + 1}/${m.total}${m.msg ? ': ' + m.msg : ''}`;
      case 'finishing': return m.msg || 'Finalizing…';
      default: return m.msg || m.stage || 'Working…';
    }
  }

  function handlePhase(stage) {
    const COARSE = ['boot', 'deps', 'bundle', 'files', 'finishing'];
    if (COARSE.indexOf(stage) === -1) return;   // Python fine-grained stage — UI only
    if (seenPhase.has(stage)) return;
    seenPhase.add(stage);
    if (stage === 'boot') { mark('op:gen:mk:boot'); }
    else if (stage === 'deps') { measure('op:gen:boot', 'op:gen:mk:boot'); mark('op:gen:mk:deps'); }
    else if (stage === 'bundle') { measure('op:gen:deps', 'op:gen:mk:deps'); mark('op:gen:mk:bundle'); }
    else if (stage === 'files') { measure('op:gen:bundle', 'op:gen:mk:bundle'); mark('op:gen:mk:run'); }
    // 'finishing' stays inside the run span; op:gen:run is measured on done.
  }

  // ── Completion: publish → best-effort write-back ────────────────────
  // Publish FIRST: the platform copy is the outcome that matters and must
  // never be hostage to a locked local file — sl_unassigned_reasons.xlsx
  // open in Excel makes createWritable() throw NoModificationAllowedError
  // (measured on the first live pilot run). A failed write-back degrades to
  // a download link; the run stays published + success.
  async function onDone(outputs, gauges) {
    stopObsTimer();
    await flushObservations(true);   // final flush BEFORE publish — see the queue's own comment for why
    measure('op:gen:run', 'op:gen:mk:run');
    // Multi-target write-back: the main folder (when the manifest writes back)
    // plus every extra folder the analyst opened for writing. Computed here,
    // BEFORE publish, so outputs_json below can carry the planned target
    // names; the actual write attempt happens further down, after publish.
    const targets = [];
    if (manifest.write_back) targets.push({ h: dirHandle, name: dirHandle.name });
    for (const x of extraDirs) if (x.w) targets.push({ h: x.h, name: x.h.name });
    try {
      setStatus('Publishing…', 'accent');
      mark('op:gen:pub:t0');
      const fd = new FormData();
      if (outputs.html) fd.append('html_file', new Blob([outputs.html], { type: 'text/html' }), 'dashboard.html');
      if (outputs.slots) for (const idx in outputs.slots) {
        const s = outputs.slots[idx];
        fd.append('slot_' + idx, new Blob([s.buf]), s.name);
      }
      // outputs_json: lineage record of what write-back WILL attempt. Publish
      // happens before write-back (comment above this function), so this can
      // only record what's known at publish time — the planned target folder
      // names. `failed` is always [] here; the real per-target outcome is
      // decided in the write-back loop below, after the server already has
      // the row. A known limitation: a target that fails to write still shows
      // here as attempted, not failed. Fixing it needs a second small PATCH
      // after write-back, which is out of scope for this task.
      if (outputs.writeBack) {
        const write_back = [];
        for (const idx in outputs.writeBack) {
          const wb = outputs.writeBack[idx];
          write_back.push({ name: wb.name, size: wb.buf.byteLength, targets: targets.map(tv => tv.name), failed: [] });
        }
        fd.append('outputs_json', JSON.stringify({ write_back }));
      }
      const r = await fetch(`/api/dashboards/${DASH_ID}/generator/runs/${runId}/publish`, { method: 'POST', body: fd });
      if (!r.ok) {
        let tv = ''; try { tv = await r.text(); } catch (e) { /* ignore */ }
        throw new Error('publish ' + r.status + (tv ? ' ' + tv.slice(0, 300) : ''));
      }
      try { publishInfo = await r.json(); } catch (e) { publishInfo = null; }
      measure('op:gen:publish', 'op:gen:pub:t0');
      measure('op:gen:total', 'op:gen:total:t0', {
        files_parsed: gauges.filesParsed, files_cached: gauges.filesCached,
        peak_wasm_bytes: gauges.peakWasmBytes, border_decision: 'browser',
        stages: gauges.stages || 0,
      });
    } catch (err) {
      await reportError('Publish failed', err);
      return;
    }

    stopHeartbeat();
    // Phase 9.3: the report index is built in THIS worker — it is the process
    // that just read the folder, and its /explore side reuses the OPFS
    // Parquet cache. So the terminate moves to the END of this function
    // whenever the step may run; a manifest with `"lake": false` keeps the
    // old timing (and the old memory profile) exactly as it was.
    const lakeWanted = !(manifest && manifest.lake === false);
    if (!lakeWanted && worker) { worker.terminate(); worker = null; }

    // `targets` was already computed above (before publish, for outputs_json).
    // Each output is written to every target; per-target failures are
    // collected here, not fatal.
    const failedByName = {};
    if (targets.length && outputs.writeBack) {
      setStatus('Writing results back…', 'accent');
      for (const idx in outputs.writeBack) {
        const wb = outputs.writeBack[idx];
        for (const tv of targets) {
          try {
            const fh = await tv.h.getFileHandle(wb.name, { create: true });
            const w = await fh.createWritable();
            await w.write(wb.buf);
            await w.close();
          } catch (err) {
            (failedByName[wb.name] || (failedByName[wb.name] = [])).push({ folder: tv.name, err: err });
          }
        }
      }
    }

    // resetUI() also clears the live bug where a write-back failure left the
    // Run button hidden (running was flipped without setBusy(false)).
    resetUI();
    setStatus('Published ✓', 'ok');
    showPublishWarnings(publishInfo && publishInfo.warnings, publishInfo && publishInfo.quality_url);

    // P4.2 folder-change banner (static/js/folder-watch.js): a successful
    // browser-engine run is the only trustworthy "last observation" a
    // no-network client script can diff against later, so hand the file list
    // off as a DOM event instead of adding a coupling import.
    try {
      root.dispatchEvent(new CustomEvent('op:gen:run-success', {
        detail: { files: currentFiles.map(f => ({ name: f.name, size: f.size, mtime_ms: f.mtime_ms })) },
      }));
    } catch (e) { /* CustomEvent unsupported — folder-watch banner just stays stale */ }

    // A run that skipped an unreadable file still publishes — but never
    // silently: the names stay on screen next to the success state.
    const skipped = (gauges && gauges.filesSkipped) || [];
    if (skipped.length) {
      showWarn(t('Skipped {p1} files — {p2}. Report built without them: file changed or disappeared during run.', {p1: skipped.length, p2: skipped.map(s => s.name + ' (' + s.error + ')').join(', ')}));
    }

    // Success panel (no auto-reload): a download for every write-back output —
    // named as the aux input when one maps to it via download_from — plus the
    // reload CTA. Failed folder writes carry the existing hint beside them.
    const auxNameFor = {};
    (manifest.aux_inputs || []).forEach(a => { if (a.download_from) auxNameFor[a.download_from] = a.name; });

    const card = el('div', { style: 'display:flex;flex-direction:column;gap:6px;margin-top:6px' });
    for (const idx in (outputs.writeBack || {})) {
      const wb = outputs.writeBack[idx];
      const dlName = auxNameFor[wb.name] || wb.name;
      const url = URL.createObjectURL(new Blob([wb.buf]));
      const row = el('div', { class: 'mono', style: 'font-size:11px;display:flex;gap:8px;align-items:center;flex-wrap:wrap' });
      const a = el('a', { class: 'btn xs', href: url, text: 'Download ' + dlName });
      a.setAttribute('download', dlName);
      row.appendChild(a);
      const fails = failedByName[wb.name];
      if (fails && fails.length) row.appendChild(el('span', { style: 'color:var(--ink-dim)', text: fails.map(f => f.folder + ': ' + _writeBackHint(f.err)).join('; ') }));
      card.appendChild(row);
    }
    const btnReload = el('button', { class: 'btn xs primary', type: 'button', text: 'Show the new report' });
    btnReload.addEventListener('click', () => location.reload());
    card.appendChild(btnReload);
    diagEl.textContent = '';
    diagEl.appendChild(card);
    diagEl.style.display = '';

    // Phase 9.3, deliberately LAST: publish, write-back and the success panel
    // are all done and on screen, so the report index can only add a status
    // line (and at worst one warning) to an already-finished run. The page
    // does not navigate on success — the "Show the new report" button does —
    // so nothing has to be delayed for this.
    if (lakeWanted) {
      let lake = null;
      try { lake = await runLakeStep(); } catch (e) { /* never fatal */ }
      if (worker) { worker.terminate(); worker = null; }
      if (!lake || !lake.ok) setStatus('Published ✓', 'ok');
    }
  }

  function _writeBackHint(err) {
    const name = (err && err.name) || '';
    if (name === 'NoModificationAllowedError' || name === 'InvalidStateError') {
      return t('file probably open in Excel — close it and run again, or download via button');
    }
    if (name === 'NotAllowedError') {
      return t('no write permission to folder — re-select and grant access');
    }
    return String((err && err.message) || err || '').slice(0, 200);
  }

  // ── Error / cancel ──────────────────────────────────────────────────
  async function reportError(prefix, err) {
    stopObsTimer();
    await flushObservations(true);   // best-effort; never throws — see the queue's own comment
    stopHeartbeat();
    if (worker) { worker.terminate(); worker = null; }
    if (agentAbort) { try { agentAbort.abort(); } catch (e) { /* ignore */ } agentAbort = null; }
    if (agentRunId && agentBase && agentToken) {   // ask the agent to stop its run + free 'busy'
      try { fetch(`${agentBase}/runs/${agentRunId}`, { method: 'DELETE', headers: { 'Authorization': 'Bearer ' + agentToken } }); } catch (e) { /* best-effort */ }
      agentRunId = null;
    }
    if (agentChipEl) setAgentChip('paired-idle', 'error');
    const message = (prefix + ': ' + (err && (err.message || err.detail) || err || 'unknown')).slice(0, 2000);
    const detail = String((err && (err.stack || err.detail || err.message)) || err || '').slice(0, 20000);
    if (runId) {
      try {
        await fetch(`/api/dashboards/${DASH_ID}/generator/runs/${runId}/error`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message, detail }),
        });
      } catch (e) { /* the run will TTL-expire to 'lost' server-side */ }
    }
    running = false;
    setStatus('Run failed.', 'err');
    showError(message, err && err.detail, hintFor(err && err.kind));
    resetUI();
  }

  // Whose fault, in one line — the kinds come from the agent (generator.py /
  // runflow) and are the same words the run history shows.
  function hintFor(kind) {
    if (kind === 'gen_crash' || kind === 'gen_no_result' || kind === 'gen_bad_result') {
      return t('This is an error inside the generator itself; its last lines are below. The platform and the agent worked as expected — fix the input file or the generator code.');
    }
    if (kind === 'gp_config' || kind === 'gp_query' || kind === 'cred_missing') {
      return t('Could not fetch data from GreenPlum: check the connection and the credentials in the agent.');
    }
    return '';
  }

  async function cancelRun() {
    stopObsTimer();
    if (worker) { worker.terminate(); worker = null; }
    if (agentAbort) { try { agentAbort.abort(); } catch (e) { /* ignore */ } agentAbort = null; }
    if (agentRunId && agentBase && agentToken) {
      try { await fetch(`${agentBase}/runs/${agentRunId}`, { method: 'DELETE', headers: { 'Authorization': 'Bearer ' + agentToken } }); } catch (e) { /* ignore */ }
      agentRunId = null;
    }
    stopHeartbeat();
    running = false;
    if (runId) { try { await fetch(`/api/dashboards/${DASH_ID}/generator/runs/${runId}`, { method: 'DELETE' }); } catch (e) { /* ignore */ } }
    if (agentChipEl) setAgentChip('paired-idle', 'canceled');
    setStatus('Canceled.', '');
    resetUI();
  }

  function startHeartbeat(everyS) {
    stopHeartbeat();
    const ms = Math.max(5, everyS || 30) * 1000;
    heartbeatTimer = setInterval(() => {
      fetch(`/api/dashboards/${DASH_ID}/generator/runs/${runId}`, { method: 'PATCH' }).catch(() => { /* ignore */ });
    }, ms);
  }
  function stopHeartbeat() { if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; } }

  // ══ Agent engine (#2 — GreenPlum) ════════════════════════════════════
  // The browser flow above is untouched; everything below runs only when the
  // manifest routes to the agent. Reuses setStatus/progress/error/diag, the
  // OnePage run-lock + heartbeat, and the SAME publish FormData as the browser.

  function chooseEngine() {
    const e = (manifest && manifest.engine) || 'auto';
    if (e === 'agent') return 'agent';
    if (e === 'browser') return 'browser';
    return (manifest && manifest.sources) ? 'agent' : 'browser';  // auto: GP needs the agent
  }

  // Swap the folder UI for an agent chip; enable Run (no folder gate).
  function setupAgentMode() {
    for (const n of browserOnlyEls) n.style.display = 'none';
    titleEl.textContent = 'Run generator (GreenPlum · agent)';
    limitsEl.textContent = 'Runs the frozen SQL on your machine under your personal '
      + 'GreenPlum login. Start the op-agent, then Run.';
    agentChipEl = el('div', { class: 'mono', style: 'font-size:11px;display:flex;gap:6px;align-items:center' });
    wrap.insertBefore(agentChipEl, btnRun);
    setAgentChip('unknown', 'checking for agent…');
    btnRun.disabled = false;
    btnRun.title = '';
    detectAgent().then(h => {
      if (!h) return setAgentChip('not-detected', 'agent not found — start op-agent');
      if (h.protocol !== AGENT_PROTOCOL) return setAgentChip('version-mismatch', h.protocol > AGENT_PROTOCOL ? `agent protocol ${h.protocol} > ${AGENT_PROTOCOL} — update OnePage` : `agent protocol ${h.protocol} < ${AGENT_PROTOCOL} — update the agent`);
      setAgentChip(h.paired ? 'paired-idle' : 'detected-unpaired',
        `agent ${h.version}` + (h.paired ? ' · paired' : ' · not paired'));
    }).catch(() => setAgentChip('not-detected', 'agent not found — start op-agent'));
  }

  function setAgentChip(state, text) {
    if (!agentChipEl) return;
    const color = {
      'paired-idle': 'var(--success)', 'paired-busy': 'var(--accent)',
      'detected-unpaired': 'var(--warning,#b7791f)', 'not-detected': 'var(--danger)',
      'version-mismatch': 'var(--danger)', 'unknown': 'var(--ink-dim)',
    }[state] || 'var(--ink-dim)';
    agentChipEl.textContent = '';
    agentChipEl.appendChild(el('span', { style: `width:8px;height:8px;border-radius:50%;background:${color};display:inline-block` }));
    agentChipEl.appendChild(el('span', { style: 'color:var(--ink-dim)', text: text || state }));
  }

  async function detectAgent() {
    for (const p of AGENT_PORTS) {
      try {
        const r = await fetch(`http://127.0.0.1:${p}/health`, { cache: 'no-store' });
        if (r.ok) {
          const h = await r.json();
          if (h && h.app === 'op-agent') { agentBase = `http://127.0.0.1:${p}`; return h; }
        }
      } catch (e) { /* next port */ }
    }
    agentBase = null;
    return null;
  }

  // Out-of-band pairing — the channel is chosen by the AGENT, not by us:
  //   approve — it has a tray icon and asks the human right there (Windows);
  //   code    — no UI (dev/headless), so it prints a code in its console.
  // Either way the proof is the same: only somebody sitting at that machine can
  // answer. The token now survives an agent restart (DPAPI), so this runs once
  // in the analyst's life rather than every morning.
  async function pairAgent() {
    const rr = await fetch(`${agentBase}/pair/request`, { method: 'POST' });
    if (!rr.ok) throw new Error('pairing request rejected');
    const req = await rr.json();
    const token = req.mode === 'approve'
      ? await awaitApproval(req.request_id, req.expires_in)
      : await redeemCode();
    agentToken = token;
    try { localStorage.setItem(AGENT_TOKEN_KEY, agentToken); } catch (e) { /* private mode */ }
  }

  async function awaitApproval(requestId, expiresIn) {
    setStatus(t('Confirm run in op-agent icon near clock…'), 'accent');
    const deadline = Date.now() + Math.max(15, expiresIn || 300) * 1000;
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 700));
      const r = await fetch(`${agentBase}/pair/status?id=${encodeURIComponent(requestId)}`);
      if (!r.ok) throw new Error('pairing status failed');
      const s = await r.json();
      if (s.state === 'approved' && s.token) return s.token;
      if (s.state === 'denied') throw new Error(t('Run rejected by agent.'));
      if (s.state === 'expired') throw new Error(t('Confirmation not received in time.'));
    }
    throw new Error(t('Confirmation not received in time.'));
  }

  async function redeemCode() {
    const code = window.prompt(t('Pairing code printed in agent window (op-agent). Enter it:'));
    if (!code) throw new Error('pairing canceled');
    const r = await fetch(`${agentBase}/pair`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: String(code).trim() }),
    });
    if (!r.ok) throw new Error('pairing rejected');
    return (await r.json()).token;
  }

  async function sha256Hex(buf) {
    const d = await crypto.subtle.digest('SHA-256', buf);
    return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, '0')).join('');
  }

  const fmtInt = n => (n == null ? '' : OP_I18N.fmtNum(Math.round(n)));

  // `mode`: 'greenplum' (engine #2, as before) or 'folders' (branch G —
  // the same steps, but the agent takes the data from folders.ini, not from GreenPlum).
  // Steps 1–2 (find the agent, pair), 4–6 (bundle, NDJSON, publish) are
  // shared by both — only what goes to the server in POST …/runs differs
  // (K2: engine `local` versus `agent`) and what goes to the agent in `meta` (K3).
  async function startAgentRun(mode) {
    mode = mode || 'greenplum';
    const folders = mode === 'folders';
    if (running) return;
    clearError(); clearDiag(); showWarn(''); warnEl.style.display = 'none';
    // Each run has its own observation stream — otherwise a browser run
    // followed by a run on this computer would inherit obsActive from
    // the previous one and send an empty final:true not on its own behalf.
    obsQueue = []; obsStages = []; obsSeq = 0; obsStripSchema = false;
    obsFlushChain = Promise.resolve(); obsFinalSent = false; obsActive = false;
    publishInfo = null;              // last run's warnings must not survive into this one
    seenPhase.clear();
    mark('op:gen:total:t0');

    // 1. Find the agent.
    setStatus(t('Looking for agent…'), 'accent');
    const health = await detectAgent();
    if (!health) { setAgentChip('not-detected', 'agent not found'); showError(t('Agent op-agent not found at 127.0.0.1 (ports 47113–47115). Is it running?')); setStatus('', ''); return; }
    // The direction of the mismatch matters: the message used to always advise
    // updating the agent — including when it was the page that was outdated (the agent
    // newer than the server), and the person went to fix the wrong thing.
    if (health.protocol !== AGENT_PROTOCOL) {
      setAgentChip('version-mismatch');
      showError(health.protocol > AGENT_PROTOCOL
        ? t('Agent is newer (protocol {p1} vs {p2}). OnePage needs update — contact admin.', {p1: health.protocol, p2: AGENT_PROTOCOL})
        : t('Agent is outdated (protocol {p1}, page needs {p2}). Update op-agent.', {p1: health.protocol, p2: AGENT_PROTOCOL}));
      setStatus('', ''); return;
    }

    // 2. Pair if needed.
    agentToken = null;
    try { agentToken = localStorage.getItem(AGENT_TOKEN_KEY); } catch (e) { /* private mode */ }
    if (!agentToken) {
      // Show the REASON for the refusal: "rejected" and "not confirmed in time" are
      // different situations, and the person must understand what to do next.
      try { await pairAgent(); } catch (e) { setAgentChip('detected-unpaired'); showError(e && e.message ? t('Agent pairing failed. {p1}', {p1: e.message}) : t('Agent pairing failed.')); setStatus('', ''); return; }
    }

    // 2.5 (folders only) Parse the folders AGAIN, on the agent itself: between
    // the probe at page load and the button press the person may have edited
    // folders.ini or disconnected the network drive.
    let inputManifest = { source: 'greenplum', files: [], total_bytes: 0, count: 0 };
    if (folders) {
      setStatus(t('Resolving folders…'), 'accent');
      const fres = await fetchFolders(folderScenario);
      if (!fres.ok) { showError(folderErrorText(fres)); setStatus('', ''); return; }
      folderInfo = fres.data;
      folderScenario = folderInfo.scenario || '';
      renderFolderCard();
      const guard = checkLocalFolders(folderInfo);
      if (!guard.ok) { setStatus(guard.status, 'err'); return; }
      inputManifest = buildLocalInputManifest(folderInfo);
    }

    // 3. Start the OnePage run (holds the run-lock). No local input folders.
    setStatus(t('Starting run…'), 'accent');
    let res;
    try {
      res = await fetch(`/api/dashboards/${DASH_ID}/generator/runs`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        // K2: `local` is "browser/auto without sources, but the agent computes"; for
        // the server it is OBSERVABLE everywhere `agent` is unobservable.
        body: JSON.stringify({ engine: folders ? 'local' : 'agent', input_manifest: inputManifest }),
      });
    } catch (e) { showError(t('Cannot reach OnePage server.')); setStatus('', ''); return; }
    if (res.status === 409) {
      let d = {}; try { d = (await res.json()).detail || {}; } catch (e) { /* ignore */ }
      showError(t('Another run is already in progress{p1}. Wait for it to finish.', {p1: d.locked_by ? ' (' + d.locked_by + ')' : ''}));
      setStatus('', ''); return;
    }
    if (res.status !== 201) {
      let tv = ''; try { tv = await res.text(); } catch (e) { /* ignore */ }
      showError(t('Run did not start: {p1}{p2}', {p1: res.status, p2: (tv ? ' ' + tv.slice(0, 300) : '')}));
      setStatus('', ''); return;
    }
    const run = await res.json();
    runId = run.run_id;
    setBusy(true);
    setAgentChip('paired-busy', 'running…');
    startHeartbeat(run.heartbeat_every_s);

    // 4. Fetch the frozen bundle from OnePage.
    setStatus(t('Loading bundle…'), 'accent');
    let bundleBuf;
    try {
      const br = await fetch(run.bundle_url, { cache: 'no-store' });
      if (!br.ok) throw new Error('bundle ' + br.status);
      bundleBuf = await br.arrayBuffer();
    } catch (e) { await reportError(t('Failed to get bundle'), e); return; }
    const checksumHex = await sha256Hex(bundleBuf);

    // 5. Push the bundle to the agent (multipart bundle + meta).
    const meta = { checksum_sha256: checksumHex, run_id: runId,
      generator: (manifest && manifest.name) || '', version: String((manifest && manifest.version) || '') };
    if (folders) {
      // K3: from these three fields the agent understands that it should read folders, which ones and whose.
      meta.mode = 'folders';
      meta.dashboard_id = String(DASH_ID);
      meta.scenario = folderScenario || '';
    }
    const pushBundle = () => {
      const fd = new FormData();
      fd.append('bundle', new Blob([bundleBuf], { type: 'application/zip' }), 'bundle.zip');
      fd.append('meta', JSON.stringify(meta));
      return fetch(`${agentBase}/runs`, { method: 'POST', headers: { 'Authorization': 'Bearer ' + agentToken }, body: fd });
    };
    let pr;
    try { pr = await pushBundle(); } catch (e) { await reportError(t('Agent unavailable'), e); return; }
    if (pr.status === 401) {                       // stale token (agent restarted) — re-pair once
      try { localStorage.removeItem(AGENT_TOKEN_KEY); } catch (e) { /* ignore */ }
      try { await pairAgent(); pr = await pushBundle(); }
      catch (e) { await reportError(t('Agent pairing required'), e); return; }
    }
    if (pr.status === 409) { await reportError(t('Agent busy with another run'), { message: 'agent busy' }); return; }
    if (pr.status !== 202) {
      let tv = ''; try { tv = await pr.text(); } catch (e) { /* ignore */ }
      await reportError(t('Agent rejected run'), { message: pr.status + (tv ? ' ' + tv.slice(0, 200) : '') });
      return;
    }
    agentRunId = (await pr.json()).agent_run_id;

    // 6. Stream NDJSON progress (fetch().getReader — EventSource can't carry Bearer).
    mark('op:gen:mk:run');
    try { await streamAgentEvents(); }
    catch (e) { if (running) await reportError(t('Agent stream interrupted'), e); }
  }

  async function streamAgentEvents() {
    agentAbort = new AbortController();
    const es = await fetch(`${agentBase}/runs/${agentRunId}/events`,
      { headers: { 'Authorization': 'Bearer ' + agentToken }, signal: agentAbort.signal });
    if (!es.ok || !es.body) throw new Error('events ' + es.status);
    const reader = es.body.getReader();
    const dec = new TextDecoder();
    let buf = '', terminal = false;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
        if (!line || terminal) continue;
        let ev; try { ev = JSON.parse(line); } catch (e) { continue; }
        if (ev.t === 'done' || ev.t === 'error') terminal = true;
        await onAgentEvent(ev);
      }
    }
  }

  function agentStageLabel(stage) {
    switch (stage) {
      case 'received': return t('Agent received bundle…');
      // Branch G: the agent parses folders.ini and lists the files. The stage
      // is separate because on a large network folder it is long, and "Working…"
      // does not tell the person WHAT exactly is taking long.
      case 'resolve': return t('Resolving folders…');
      case 'unpack': return t('Unpacking and verifying sha256…');
      case 'sql': return t('GreenPlum: running queries…');
      case 'generate': return t('Building report…');
      default: return t('Working…');
    }
  }

  async function onAgentEvent(ev) {
    if (ev.t === 'stage') { setStatus(agentStageLabel(ev.stage), 'accent'); return; }
    if (ev.t === 'progress') {
      if (ev.stage === 'copy' && ev.rows_per_s != null) {           // live GP ticker — the «wow»
        setStatus(t('GreenPlum · {p1}: {p2} rows · {p3} rows/s · {p4} MB/s', {p1: ev.output, p2: fmtInt(ev.rows), p3: fmtInt(ev.rows_per_s), p4: ev.mb_per_s}), 'accent');
      } else if (ev.stage === 'copy' && ev.phase === 'done') {
        setStatus(t('GreenPlum · {p1}: done, {p2} rows', {p1: ev.output, p2: fmtInt(ev.rows)}), 'accent');
      } else if (ev.total && ev.total > 0) {                        // generator subprocess progress
        progressEl.max = ev.total; progressEl.value = ev.done || 0;
        setStatus(`${ev.stage}: ${(ev.done || 0)}/${ev.total}${ev.msg ? ' · ' + ev.msg : ''}`, 'accent');
      }
      return;
    }
    if (ev.t === 'warning') { showWarn(ev.msg || 'warning'); return; }
    // Lineage of a native run. The R2 host (`agent/r2host.py`) captures the
    // same frames as the browser worker and sends them here over NDJSON — until this
    // line the page silently threw them away, and a run "on this computer"
    // arrived on the platform without a single read column, although the agent
    // honestly named them. The queue and batches are shared with the browser: they are sent by
    // flushObservations, not by this handler.
    if (ev.t === 'observe') { enqueueObservations(ev.files || [], ev.stages); return; }
    if (ev.t === 'error') {
      await reportError(t('Agent run'), { kind: ev.kind || '', message: (ev.kind ? ev.kind + ': ' : '') + (ev.message || ''), detail: ev.detail || '' });
      return;
    }
    if (ev.t === 'done') { await onAgentDone(ev.outputs || {}); return; }
  }

  async function fetchAgentOutput(name) {
    const r = await fetch(`${agentBase}/runs/${agentRunId}/outputs/${encodeURIComponent(name)}`,
      { headers: { 'Authorization': 'Bearer ' + agentToken } });
    if (!r.ok) throw new Error('output ' + name + ' ' + r.status);
    return await r.arrayBuffer();
  }

  // Pull outputs from the agent, publish via the SAME FormData the browser uses.
  async function onAgentDone(outputs) {
    measure('op:gen:run', 'op:gen:mk:run');
    try {
      // The final batch of observations — BEFORE publishing, in the same order as in
      // the browser path: after publishing the page may go off to
      // reload the report, and the lineage would not get sent in time.
      await flushObservations(true);
      setStatus(t('Loading results from agent…'), 'accent');
      const fd = new FormData();
      if (outputs.html) {
        const buf = await fetchAgentOutput(outputs.html);
        fd.append('html_file', new Blob([buf], { type: 'text/html' }), 'dashboard.html');
      }
      for (const idx in (outputs.slots || {})) {
        const name = outputs.slots[idx];
        const buf = await fetchAgentOutput(name);
        fd.append('slot_' + idx, new Blob([buf]), name);
      }
      setStatus(t('Publishing… {ctx:2}'), 'accent');
      mark('op:gen:pub:t0');
      const r = await fetch(`/api/dashboards/${DASH_ID}/generator/runs/${runId}/publish`, { method: 'POST', body: fd });
      if (!r.ok) {
        let tv = ''; try { tv = await r.text(); } catch (e) { /* ignore */ }
        throw new Error('publish ' + r.status + (tv ? ' ' + tv.slice(0, 300) : ''));
      }
      try { publishInfo = await r.json(); } catch (e) { publishInfo = null; }
      measure('op:gen:publish', 'op:gen:pub:t0');
      measure('op:gen:total', 'op:gen:total:t0', { border_decision: 'agent' });
    } catch (err) { await reportError(t('Publish failed'), err); return; }

    stopHeartbeat();
    agentRunId = null; agentAbort = null;
    resetUI();
    setAgentChip('paired-idle', 'done');
    setStatus(t('Published ✓'), 'ok');
    showPublishWarnings(publishInfo && publishInfo.warnings, publishInfo && publishInfo.quality_url);

    const card = el('div', { style: 'display:flex;flex-direction:column;gap:6px;margin-top:6px' });
    const btnReload = el('button', { class: 'btn xs primary', type: 'button', text: t('Show new report') });
    btnReload.addEventListener('click', () => location.reload());
    card.appendChild(btnReload);
    diagEl.textContent = ''; diagEl.appendChild(card); diagEl.style.display = '';
  }

  // ── Init ─────────────────────────────────────────────────────────────
  async function init() {
    buildUI();
    // Manifest first — it decides the engine (and thus which UI to wire). The
    // FSA gate only matters for the browser engine; the agent needs no folders.
    let data;
    try {
      const r = await fetch(`/api/dashboards/${DASH_ID}/generator/manifest`);
      if (!r.ok) throw new Error('manifest ' + r.status);
      data = await r.json();
    } catch (e) { showError('Could not load the generator manifest.'); return; }
    manifest = data.manifest;
    checksum = data.checksum_sha256;
    sourceRules = data.source_rules || {};
    sourceAliases = data.source_aliases || {};
    inputsDecl = data.inputs || [];       // Data Lineage v2: canonical allow-list (server-normalized, folds required_files in)
    checksDecl = data.checks || [];       // Data Quality v1: canonical checks (genmanifest.checks_for), ids filled
    forceDeps = data.force_deps || [];    // e.g. ["python-calamine"] — installed regardless of the manifest's own deps
    referenceDecl = data.reference || [];  // ADR-16: platform reference tables this generator consumes
    generatorId = data.generator && data.generator.id;
    engineMode = chooseEngine();

    if (engineMode === 'agent') { setupAgentMode(); return; }

    if (!FSA_OK) {
      limitsEl.textContent = 'The browser engine needs a Chromium-based browser (Edge/Chrome) for folder access.';
      btnChoose.disabled = true;
      return;
    }
    renderLimits();
    renderReference();
    buildAuxUI();
    await restoreHandle();
    await restoreExtraHandles();
    renderSlots();
    await restoreAuxHandle();
    // Very last: the agent probe — a network request to 127.0.0.1 that
    // for most people will simply not answer. While it is running, the panel is already
    // fully functional in browser mode.
    try { await probeLocal(); } catch (e) { /* no agent — that is normal */ }
  }

  // P4.2 folder-change banner hook (static/js/folder-watch.js): the banner
  // lives outside this module and has no access to the closured btnRun, so
  // it asks the engine to click its own already-built, already-gated button
  // — reusing the run flow's permission/border checks instead of duplicating
  // them. A no-op when the button isn't ready yet (agent mode, no folder).
  root.opGenTriggerRun = function () {
    if (btnRun && !btnRun.hidden && !btnRun.disabled) btnRun.click();
  };

  init();
})();
