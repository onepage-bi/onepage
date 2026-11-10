/* OnePage — P4.2 folder-change banner (static/js/folder-watch.js).
 *
 * Plan item P4.2: "saved folder handle (IndexedDB) → on viewer open
 * queryPermission → listing → diff (name,size,mtime) against the last
 * observation → banner 'N new / M changed files since the last run — run?'.
 * Works only in-browser, and only for a user who has already run this
 * generator once." (agile-petting-dewdrop.md)
 *
 * No network calls beyond one best-effort GET of the manifest already served
 * by gen-engine.js's own endpoint (see fetchManifestFilters below) — this
 * script otherwise makes no server writes and no new server routes. The
 * main-folder FSA handle is the SAME one gen-engine.js already persists
 * (IndexedDB db `op-gen`, store `handles`, key = dashboard id) — this script
 * only READS it, so there is exactly one handle/permission per dashboard and
 * no second folder picker. queryPermission is called (never
 * requestPermission — that needs a user gesture this script does not have on
 * load) so a stale/denied handle degrades to silence or a "reconnect" hint,
 * never a prompt.
 *
 * "Last observation" here is intentionally self-scoped to THIS browser: the
 * server's true last-known files require a POST …/generator/runs to fetch
 * (that call starts a run and takes the lock — not something a passive page
 * load may do). So this script keeps its own baseline snapshot, written only
 * after a run this same browser completed successfully (gen-engine.js's
 * `op:gen:run-success` event on #gen-engine-root, carrying the exact file
 * list the run saw). Until that has happened once here, there is no baseline
 * to diff against and the banner stays silent — matching the plan's own
 * caveat above.
 */
(function () {
  'use strict';

  const DB_NAME = 'op-gen';           // gen-engine.js's own db — read-only from here
  const STORE = 'handles';
  const WATCH_DB = 'op-gen-watch';    // this script's own baseline snapshots
  const WATCH_STORE = 'snapshots';

  // ── IndexedDB helpers ────────────────────────────────────────────────
  function idbOpen(name, store) {
    return new Promise((res, rej) => {
      let req;
      try { req = indexedDB.open(name, 1); } catch (e) { rej(e); return; }
      req.onupgradeneeded = () => { req.result.createObjectStore(store); };
      req.onsuccess = () => res(req.result);
      req.onerror = () => rej(req.error);
    });
  }
  async function idbGet(dbName, store, key) {
    const db = await idbOpen(dbName, store);
    return new Promise((res, rej) => {
      const r = db.transaction(store, 'readonly').objectStore(store).get(String(key));
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  }
  async function idbPut(dbName, store, key, val) {
    const db = await idbOpen(dbName, store);
    return new Promise((res, rej) => {
      const tv = db.transaction(store, 'readwrite');
      tv.objectStore(store).put(val, String(key));
      tv.oncomplete = () => res();
      tv.onerror = () => rej(tv.error);
    });
  }

  // ── Glob matching — same algorithm as gen-engine.js's globToRe/excludedBy,
  // duplicated here (no shared module between the two scripts) so the two
  // engines that read from the SAME folder agree on what "reads" a file. ──
  const _globCache = new Map();
  function globToRe(pat) {
    let re = _globCache.get(pat);
    if (re) return re;
    let out = '';
    for (let i = 0; i < pat.length; i++) {
      const c = pat[i];
      if (c === '*') {
        if (pat[i + 1] === '*') { out += '.*'; i++; }
        else out += '[^/]*';
      } else if (c === '?') {
        out += '[^/]';
      } else {
        out += c.replace(/[.+^${}()|[\]\\]/g, '\\$&');
      }
    }
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

  // ── Manifest filters — best-effort GET of the same endpoint gen-engine.js
  // reads at init (`/api/dashboards/{id}/generator/manifest`). Gives this
  // script the declared-inputs allow-list, per-source exclusion rules and
  // aux input names gen-engine.js's enumerate() applies, plus write_back (to
  // know which permission mode a run actually needs). No new server route;
  // any failure (network, 403 for a non-can_update_data viewer, old browser
  // with no `fetch`) just means less precise filtering, never a throw. ──
  async function fetchManifestFilters(dashId) {
    if (typeof fetch !== 'function') return null;
    try {
      const r = await fetch('/api/dashboards/' + encodeURIComponent(dashId) + '/generator/manifest',
        { credentials: 'same-origin' });
      if (!r || !r.ok) return null;
      const data = await r.json();
      const inputsDecl = Array.isArray(data.inputs) ? data.inputs : [];
      const rules = (data.source_rules && typeof data.source_rules === 'object') ? data.source_rules : {};
      const auxNames = new Set(((data.manifest && data.manifest.aux_inputs) || [])
        .map(a => a && a.name).filter(Boolean));
      const writeBack = !!(data.manifest && data.manifest.write_back);
      return { inputsDecl: inputsDecl, rules: rules, auxNames: auxNames, writeBack: writeBack };
    } catch (e) { return null; }
  }

  // ── Scan: K11 junk/extension filter (always) plus, when `filters` is
  // available, the same three additional filters gen-engine.js's enumerate()
  // applies — declared-inputs allow-list, this folder's exclusion rules, aux
  // input names — so a file the engine deliberately ignores is never reported
  // as a "new file". Without `filters` (fetch failed) this falls back to the
  // old junk/extension-only scan rather than refusing to render at all. ──
  function isJunk(name) {
    return name.startsWith('~$') || name.startsWith('._') || name.startsWith('.') || name === '__MACOSX';
  }
  function isDataFile(name) {
    const lower = name.toLowerCase();
    return lower.endsWith('.xlsx') || lower.endsWith('.xlsm') || lower.endsWith('.xls') || lower.endsWith('.xlsb');
  }
  async function scanFolder(handle, filters) {
    const inputsDecl = (filters && filters.inputsDecl) || [];
    const rules = (filters && filters.rules && filters.rules[handle.name]) || null;
    const auxNames = (filters && filters.auxNames) || null;
    const files = [];
    for await (const entry of handle.values()) {
      if (entry.kind !== 'file') continue;
      if (isJunk(entry.name) || !isDataFile(entry.name)) continue;
      const nm = entry.name;
      // Same rule as gen-engine.js enumerate(): required_files-derived entries
      // never form an allow-list on their own.
      if (inputsDecl.some(inp => inp && !inp.from_required_files)
          && !inputsDecl.some(inp => inp && globToRe(inp.match).test(nm))) continue;
      if (excludedBy(rules, nm)) continue;
      if (auxNames && auxNames.has(nm)) continue;
      let f;
      try { f = await entry.getFile(); } catch (e) { continue; }   // unreadable — skip, not fatal
      files.push({ name: nm, size: f.size, mtime_ms: f.lastModified });
    }
    return files;
  }

  // ── Pure diff: (name,size,mtime) against a baseline map ─────────────
  // baseline: {name: [size, mtime_ms]} (same shape as the engine's own
  // `lastKnown`, for a single mental model of "what did we see last time").
  function diffFiles(current, baseline) {
    const added = [], changed = [];
    for (const f of current) {
      const prev = baseline && baseline[f.name];
      if (!prev) { added.push(f.name); continue; }
      if (prev[0] !== f.size || prev[1] !== f.mtime_ms) changed.push(f.name);
    }
    return { added: added, changed: changed };
  }
  function toBaseline(files) {
    const m = {};
    files.forEach(f => { m[f.name] = [f.size, f.mtime_ms]; });
    return m;
  }

  // ── Tiny DOM builder (textContent only — no innerHTML) ───────────────
  function el(tag, props, kids) {
    const n = document.createElement(tag);
    if (props) for (const k in props) {
      if (k === 'class') n.className = props[k];
      else if (k === 'text') n.textContent = props[k];
      else if (k === 'style') n.style.cssText = props[k];
      else if (k in n) n[k] = props[k];
      else n.setAttribute(k, props[k]);
    }
    (kids || []).forEach(c => { if (c != null) n.appendChild(c); });
    return n;
  }

  // Russian plural form index: 0 = one (1, 21, 31…), 1 = few (2-4, 22-24…),
  // 2 = many (0, 5-20, 25-30…) — the standard mod10/mod100 rule. Applied to
  // BOTH the noun ("file") and the two adjectives, because the previous
  // singular/plural binary was wrong for any count ending in 1 above 20 (e.g.
  // 21) and for the 11-14 range (which is always "many", never "few").
  // The mod10/mod100 rule this function used to carry now lives in
  // static/js/i18n.js, behind tn() — one implementation for every language.
  function pluralNote(n, m) {
    const parts = [];
    if (n) parts.push(tn(n, '{n} new|{n} new'));
    if (m) parts.push(tn(m, '{n} changed|{n} changed'));
    if (!parts.length) return t('No new or changed files in folder since last run.');
    // The noun agrees with the TOTAL, and the whole sentence is one key so a
    // translation can order it its own way.
    return tn(n + m,
      '{what} file in the folder since the last run — run it?|{what} files in the folder since the last run — run it?',
      {what: parts.join(' / ')});
  }

  // ── Banner rendering ─────────────────────────────────────────────────
  function renderChangeBanner(container, added, changed, onRun, onDismiss) {
    container.textContent = '';
    const card = el('div', { class: 'folder-banner' });
    card.appendChild(el('div', { class: 'folder-banner-text', text: pluralNote(added.length, changed.length) }));
    const actions = el('div', { class: 'folder-banner-actions' });
    const btnRun = el('button', { class: 'btn xs primary', type: 'button', text: t('Run') });
    btnRun.addEventListener('click', onRun);
    const btnDismiss = el('button', { class: 'btn xs ghost', type: 'button', text: t('Hide') });
    btnDismiss.addEventListener('click', onDismiss);
    actions.appendChild(btnRun);
    actions.appendChild(btnDismiss);
    card.appendChild(actions);
    container.appendChild(card);
    container.hidden = false;
  }

  function renderReconnectHint(container, onReconnect) {
    container.textContent = '';
    const card = el('div', { class: 'folder-banner folder-banner-subtle' });
    const link = el('button', { class: 'btn xs ghost', type: 'button', text: t('Reconnect folder') });
    link.addEventListener('click', onReconnect);
    card.appendChild(link);
    container.appendChild(card);
    container.hidden = false;
  }

  function clearBanner(container) {
    container.textContent = '';
    container.hidden = true;
  }

  // ── Wire-up ───────────────────────────────────────────────────────────
  // dashId: the dashboard id the persisted handle and baseline are keyed by.
  // container: the banner's mount element (viewer.html: #folder-watch-banner).
  // successSource: the element gen-engine.js dispatches `op:gen:run-success`
  // on (viewer.html: #gen-engine-root) — kept as a parameter, not a fixed
  // getElementById, so the DOM test harness can supply a stub.
  async function initFolderWatch(dashId, container, successSource) {
    if (!container || !dashId) return;
    if (typeof indexedDB === 'undefined') return;

    // Every future successful run (this browser) becomes the new baseline —
    // wired regardless of whether a handle currently resolves, so a run
    // started via "Choose folder…" today still seeds tomorrow's diff.
    if (successSource) {
      successSource.addEventListener('op:gen:run-success', (ev) => {
        const files = (ev && ev.detail && ev.detail.files) || [];
        idbPut(WATCH_DB, WATCH_STORE, dashId, toBaseline(files)).catch(() => { /* private mode: session-only */ });
        clearBanner(container);   // this run just accounted for the change
      });
    }

    let handle;
    try { handle = await idbGet(DB_NAME, STORE, dashId); } catch (e) { return; }
    if (!handle) return;   // no folder ever chosen in this browser — nothing to watch

    // No baseline recorded here yet (plan's own caveat) — nothing to compare
    // against, so stay silent even if the handle also needs reconnecting.
    // Checked BEFORE permission so a user who picked a folder but never
    // finished a run doesn't get a reconnect card duplicating gen-engine's
    // own (gen-engine.js renders its own #gen-engine-root reconnect button
    // from the very same handle a few rows below in the same dock section).
    let baseline;
    try { baseline = await idbGet(WATCH_DB, WATCH_STORE, dashId); } catch (e) { baseline = null; }
    if (!baseline) { clearBanner(container); return; }

    const filters = await fetchManifestFilters(dashId);
    // The permission MODE a run actually needs — matches gen-engine.js's own
    // `pickMode()` (readwrite when the manifest writes results back). Asking
    // for less than that here would let a reconnect through this banner
    // report success while gen-engine's Run button (which needs readwrite)
    // stays disabled.
    const mode = (filters && filters.writeBack) ? 'readwrite' : 'read';

    let perm;
    try { perm = await handle.queryPermission({ mode: mode }); }
    catch (e) { return; }   // handle unusable (e.g. cross-browser-profile clone) — stay silent

    if (perm !== 'granted') {
      // Never requestPermission() without a user gesture — show a subtle
      // reconnect link instead. The click IS the gesture the reconnect needs.
      renderReconnectHint(container, async () => {
        try {
          const p = await handle.requestPermission({ mode: mode });
          if (p === 'granted') {
            // gen-engine.js owns the Run button's enabled state and only
            // re-syncs it from its own restoreHandle(), which already ran
            // once at page load and won't run again on its own — without a
            // reload, "Run" below would click a button gen-engine
            // still believes is folder-less. Reload lets both scripts pick
            // up the fresh grant from the same handle consistently.
            location.reload();
          } else clearBanner(container);
        } catch (e) { clearBanner(container); }
      });
      return;
    }

    await checkAndRender();

    async function checkAndRender() {
      let current;
      try { current = await scanFolder(handle, filters); } catch (e) { clearBanner(container); return; }

      const { added, changed } = diffFiles(current, baseline);
      if (!added.length && !changed.length) { clearBanner(container); return; }

      renderChangeBanner(container, added, changed,
        () => {
          if (successSource && typeof successSource.opGenTriggerRun === 'function') {
            successSource.opGenTriggerRun();
          }
        },
        () => clearBanner(container));
    }
  }

  function boot() {
    const container = document.getElementById('folder-watch-banner');
    if (!container) return;   // non-owner grantee, or no generator on this dashboard
    const dashId = container.dataset.dashboardId;
    const successSource = document.getElementById('gen-engine-root');
    initFolderWatch(dashId, container, successSource).catch(() => { /* best-effort — banner stays hidden */ });
  }

  // Guarded: a `require()` of this file for unit tests (no `document` global
  // yet) must not throw — the DOM-level test instead sets up `global.document`
  // before requiring/eval'ing the source, same idiom as tests/quality_dom.js.
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', boot);
    } else {
      boot();
    }
  }

  // Node test hook only (tests/folder_watch.js) — `document`/`indexedDB`
  // don't exist under plain node; this never runs in the browser.
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      diffFiles, toBaseline, isJunk, isDataFile, pluralNote, initFolderWatch,
      globToRe, excludedBy, scanFolder, fetchManifestFilters,
    };
  }
})();
