/* Headless harness for static/js/folder-watch.js (P4.2 folder-change banner).
 *
 * Two layers, same idiom as tests/quality_dom.js / tests/checks_filter.js:
 *  1. Pure diff/scan-filter logic via `require()` (the file exports a node
 *     test hook, mirroring gen-worker.js's `checksFor`/`declaredFor`).
 *  2. Full wiring (IndexedDB handle → permission → scan → banner DOM) via a
 *     minimal in-memory `indexedDB`/`document`/FileSystemDirectoryHandle
 *     stub and `(0, eval)(src)`, exactly like data-quality.js's harness.
 *
 * Run: node tests/folder_watch.js   (prints PASS/FAIL lines + ALL PASS)
 */
'use strict';
require('./_i18n_stub');   // global t/tn — must come before the DOM stub
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const FAILURES = [];
function check(label, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + label + (!cond && extra ? '  <-- ' + extra : ''));
  if (!cond) FAILURES.push(label);
}

// ── Layer 1: pure logic via require() ──────────────────────────────────
const pure = require(path.join(ROOT, 'static/js/folder-watch.js'));

{
  const baseline = pure.toBaseline([
    { name: 'a.xlsx', size: 100, mtime_ms: 1000 },
    { name: 'b.xlsx', size: 200, mtime_ms: 2000 },
  ]);
  check('toBaseline shape', baseline['a.xlsx'][0] === 100 && baseline['a.xlsx'][1] === 1000);

  const current = [
    { name: 'a.xlsx', size: 100, mtime_ms: 1000 },   // unchanged
    { name: 'b.xlsx', size: 250, mtime_ms: 2000 },   // size changed
    { name: 'c.xlsx', size: 50, mtime_ms: 500 },     // new
  ];
  const { added, changed } = pure.diffFiles(current, baseline);
  check('diffFiles: new file detected', added.length === 1 && added[0] === 'c.xlsx', JSON.stringify(added));
  check('diffFiles: changed file detected (size)', changed.length === 1 && changed[0] === 'b.xlsx', JSON.stringify(changed));
  check('diffFiles: unchanged file neither new nor changed', !added.includes('a.xlsx') && !changed.includes('a.xlsx'));

  // mtime-only change also counts as "changed"
  const mtimeChanged = pure.diffFiles(
    [{ name: 'a.xlsx', size: 100, mtime_ms: 9999 }], baseline);
  check('diffFiles: mtime-only change detected', mtimeChanged.changed.length === 1 && mtimeChanged.changed[0] === 'a.xlsx');

  // no baseline entry at all for an empty current list
  check('diffFiles: nothing vs nothing', pure.diffFiles([], {}).added.length === 0 && pure.diffFiles([], {}).changed.length === 0);
}

{
  check('isJunk: dotfile', pure.isJunk('.DS_Store') === true);
  check('isJunk: Excel lock file', pure.isJunk('~$report.xlsx') === true);
  check('isJunk: macOS resource fork', pure.isJunk('._report.xlsx') === true);
  check('isJunk: ordinary name', pure.isJunk('report.xlsx') === false);
  check('isDataFile: xlsx', pure.isDataFile('Report.XLSX') === true);   // case-insensitive
  check('isDataFile: xlsb', pure.isDataFile('data.xlsb') === true);
  check('isDataFile: pdf rejected', pure.isDataFile('notes.pdf') === false);
}

{
  // Default language (en, no catalogue): the English key renders as is.
  const msg1 = pure.pluralNote(1, 0);
  const msg2 = pure.pluralNote(0, 2);
  const msg3 = pure.pluralNote(2, 3);
  check('pluralNote: singular new only', msg1 === '1 new file in the folder since the last run — run it?', msg1);
  check('pluralNote: plural changed only', msg2.indexOf('2 changed files') !== -1, msg2);
  check('pluralNote: both counts present', msg3.indexOf('2 new') !== -1 && msg3.indexOf('3 changed') !== -1, msg3);
  const msg21en = pure.pluralNote(0, 21);
  check('pluralNote (en): 21 uses the plural form',
    msg21en === '21 changed files in the folder since the last run — run it?', msg21en);
  const msg0 = pure.pluralNote(0, 0);
  check('pluralNote: zero total has no doubled space and states nothing changed',
    msg0 === 'No new or changed files in folder since last run.', msg0);
}
{
  // Russian through the real catalogue: the full mod10/mod100 rule, not the
  // old singular/plural binary, applied to the noun AND the adjectives.
  // The expected strings below are Russian on purpose: they come from ru.json.
  const I18N = globalThis.OP_I18N;
  const RU = JSON.parse(fs.readFileSync(path.join(ROOT, 'app', 'locales', 'ru.json'), 'utf8'));
  I18N.init('ru', RU);
  try {
    const msg1 = pure.pluralNote(1, 0);
    check('pluralNote (ru): singular new only', msg1 === 'В папке 1 новый файл с прошлого прогона — запустить?', msg1);
    const msg2 = pure.pluralNote(0, 2);
    check('pluralNote (ru): plural changed only', msg2.indexOf('2 изменённых') !== -1, msg2);
    const msg3 = pure.pluralNote(2, 3);
    check('pluralNote (ru): both counts present', msg3.indexOf('2 новых') !== -1 && msg3.indexOf('3 изменённых') !== -1, msg3);
    const msg21 = pure.pluralNote(0, 21);
    check('pluralNote (ru): 21 (ends in 1, not 11) uses singular forms',
      msg21 === 'В папке 21 изменённый файл с прошлого прогона — запустить?', msg21);
    const msg22 = pure.pluralNote(22, 0);
    check('pluralNote (ru): 22 (few range) uses the few form of "file"',
      msg22 === 'В папке 22 новых файла с прошлого прогона — запустить?', msg22);
    const msg11 = pure.pluralNote(11, 0);
    check('pluralNote (ru): 11 (many range, not one) uses the many form of "file"',
      msg11 === 'В папке 11 новых файлов с прошлого прогона — запустить?', msg11);
    const msg0 = pure.pluralNote(0, 0);
    check('pluralNote (ru): zero total states nothing changed',
      msg0 === 'В папке нет новых или изменённых файлов с прошлого прогона.', msg0);
  } finally {
    I18N.init('en', {});
  }
}

// ── Filter parity with gen-engine.js's enumerate(): allow-list, exclusion
// rules, aux-name drop ───────────────────────────────────────────────────
{
  check('globToRe: glob pattern matches', pure.globToRe('K11_*.xlsx').test('K11_2026.xlsx') === true);
  check('globToRe: non-matching name rejected', pure.globToRe('K11_*.xlsx').test('notes.xlsx') === false);
  check('globToRe: case-insensitive', pure.globToRe('report.xlsx').test('REPORT.XLSX') === true);
  check('excludedBy: no rules -> never excluded', pure.excludedBy(null, 'a.xlsx') === null);
  check('excludedBy: matching rule returns the pattern', pure.excludedBy(['a.*'], 'a.xlsx') === 'a.*');
  check('excludedBy: non-matching rule -> null', pure.excludedBy(['z.*'], 'a.xlsx') === null);
}

async function scanFolderFilterTests() {
  {
    const handle = makeHandle([
      { name: 'K11_2026.xlsx', size: 10, mtime_ms: 1 },
      { name: 'notes.xlsx', size: 5, mtime_ms: 2 },
    ], { name: 'K11' });
    const filters = { inputsDecl: [{ match: 'K11_*.xlsx' }], rules: {}, auxNames: new Set() };
    const files = await pure.scanFolder(handle, filters);
    check('scanFolder: allow-list drops a file no declared input admits',
      files.length === 1 && files[0].name === 'K11_2026.xlsx', JSON.stringify(files));
  }
  {
    // Regression 2026-08-21 (ops-rdc-health): required_files-derived entries
    // alone must NOT act as an allow-list — the legacy engine reads everything.
    const handle = makeHandle([
      { name: 'RDC Data.xlsx', size: 10, mtime_ms: 1 },
      { name: 'real_forecast_SOP7.xlsx', size: 5, mtime_ms: 2 },
    ], { name: 'OPS' });
    const filters = { inputsDecl: [{ match: 'RDC Data.xlsx', required: true, from_required_files: true }], rules: {}, auxNames: new Set() };
    const files = await pure.scanFolder(handle, filters);
    check('scanFolder: required_files-only inputs do not form an allow-list',
      files.length === 2, JSON.stringify(files));
  }
  {
    const handle = makeHandle([
      { name: 'K11_2026.xlsx', size: 10, mtime_ms: 1 },
      { name: 'private.xlsx', size: 5, mtime_ms: 2 },
    ], { name: 'K11' });
    const filters = { inputsDecl: [], rules: { K11: ['private.*'] }, auxNames: new Set() };
    const files = await pure.scanFolder(handle, filters);
    check('scanFolder: source exclusion rule drops the excluded file',
      files.length === 1 && files[0].name === 'K11_2026.xlsx', JSON.stringify(files));
  }
  {
    const handle = makeHandle([
      { name: 'K11_2026.xlsx', size: 10, mtime_ms: 1 },
      { name: 'reasons.xls', size: 5, mtime_ms: 2 },
    ], { name: 'K11' });
    const filters = { inputsDecl: [], rules: {}, auxNames: new Set(['reasons.xls']) };
    const files = await pure.scanFolder(handle, filters);
    check('scanFolder: aux input name is dropped',
      files.length === 1 && files[0].name === 'K11_2026.xlsx', JSON.stringify(files));
  }
  {
    const handle = makeHandle([{ name: 'a.xlsx', size: 10, mtime_ms: 1 }]);
    const files = await pure.scanFolder(handle, null);
    check('scanFolder: no filters (fetch failed) falls back to junk/extension scan only',
      files.length === 1 && files[0].name === 'a.xlsx', JSON.stringify(files));
  }
}

// ── Layer 2: DOM wiring via eval'd source in a stubbed environment ─────
class Ev {
  constructor(type, opts) { this.type = type; this.detail = opts && opts.detail; }
}
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.attrs = {};
    this.listeners = {};
    this.hidden = false;
    this._class = new Set();
    this._text = '';
    this.style = {};
  }
  get classList() {
    const self = this;
    return {
      add: (...c) => c.forEach(x => self._class.add(x)),
      contains: (c) => self._class.has(c),
    };
  }
  set className(v) { this._class = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return Array.from(this._class).join(' '); }
  set textContent(v) { this.children = []; this._text = v === '' ? '' : String(v); }
  get textContent() {
    if (this.children.length) return this.children.map(c => c.textContent).join('');
    return this._text;
  }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatchEvent(ev) { (this.listeners[ev.type] || []).forEach(f => f.call(this, ev)); return true; }
}
function makeDocument() {
  const registry = {};
  const doc = {
    readyState: 'complete',
    createElement: (tag) => new El(tag),
    getElementById: (id) => registry[id] || null,
    addEventListener: () => {},
    _register: (id, el) => { registry[id] = el; },
  };
  return doc;
}

// ── Fake IndexedDB: in-memory, same {open/onupgradeneeded/get/put} shape
// the source code actually calls. ──────────────────────────────────────
function makeFakeIndexedDB() {
  const stores = {};   // dbName -> { storeName -> Map }
  function ensure(dbName, storeName) {
    stores[dbName] = stores[dbName] || {};
    stores[dbName][storeName] = stores[dbName][storeName] || new Map();
    return stores[dbName][storeName];
  }
  return {
    _stores: stores,
    open(name, version) {
      const req = { result: null, onupgradeneeded: null, onsuccess: null, onerror: null };
      setTimeout(() => {
        const fakeDb = {
          createObjectStore(storeName) { ensure(name, storeName); },
          transaction(storeName) {
            const map = ensure(name, storeName);
            const t = {
              objectStore() {
                return {
                  get(key) {
                    const r = { onsuccess: null, onerror: null, result: undefined };
                    setTimeout(() => { r.result = map.get(key); if (r.onsuccess) r.onsuccess(); }, 0);
                    return r;
                  },
                  put(val, key) { map.set(key, val); },
                };
              },
              oncomplete: null,
              onerror: null,
              get _map() { return map; },
            };
            // put() runs synchronously above (matches the source's own
            // fire-and-forget usage); oncomplete still fires on a tick so
            // idbPut()'s promise resolves like the real IDB transaction does.
            setTimeout(() => { if (t.oncomplete) t.oncomplete(); }, 0);
            return t;
          },
        };
        req.result = fakeDb;
        if (req.onupgradeneeded) req.onupgradeneeded();
        if (req.onsuccess) req.onsuccess();
      }, 0);
      return req;
    },
  };
}
// The put() path uses `t.oncomplete` — wire it through a microtask so
// idbPut's promise actually resolves in our fake transaction.
function patchPutCompletion(fakeIdb) {
  const origOpen = fakeIdb.open.bind(fakeIdb);
  fakeIdb.open = function (name, version) {
    const req = origOpen(name, version);
    return req;
  };
}

// A fake FileSystemDirectoryHandle: `.values()` async-iterates fake file
// entries, `.getFile()` returns {size, lastModified}, `.queryPermission()`
// and `.requestPermission()` are scripted per test. `.name` matters once
// exclusion rules are keyed by folder basename, same as gen-engine.js.
function makeHandle(files, opts) {
  opts = opts || {};
  let permState = opts.perm || 'granted';
  const requestCalls = [];
  return {
    name: opts.name || 'TestFolder',
    async *values() {
      for (const f of files) {
        yield {
          kind: 'file',
          name: f.name,
          async getFile() { return { size: f.size, lastModified: f.mtime_ms }; },
        };
      }
    },
    async queryPermission(opts2) { return permState; },
    async requestPermission(opts2) {
      requestCalls.push(opts2 && opts2.mode);
      permState = opts.requestResult || 'granted';
      return permState;
    },
    _requestCalls: requestCalls,
  };
}

// Default fetch stub answers `{ok:false}` — fetchManifestFilters() then
// returns null and scanFolder() falls back to the plain junk/extension scan,
// so any scenario that doesn't care about manifest filters is unaffected.
function runDomScenario(label, setup, opts) {
  opts = opts || {};
  const doc = makeDocument();
  const banner = new El('div');
  banner.hidden = true;
  banner.dataset.dashboardId = '42';
  doc._register('folder-watch-banner', banner);
  const successSource = new El('div');
  doc._register('gen-engine-root', successSource);

  const idb = makeFakeIndexedDB();
  patchPutCompletion(idb);

  const reloadCalls = [];
  global.document = doc;
  global.indexedDB = idb;
  global.window = {};
  global.CustomEvent = Ev;
  global.location = opts.location || { reload: () => { reloadCalls.push(true); } };
  global.fetch = opts.fetch || (async () => ({ ok: false }));

  const src = fs.readFileSync(path.join(ROOT, 'static/js/folder-watch.js'), 'utf8');
  // Fresh module scope per scenario (own closures) — eval in a function
  // wrapper so repeated evals don't collide on top-level `const` redeclare.
  (0, eval)('(function(){' + src + '})()');

  return setup({ doc, banner, successSource, idb, reloadCalls });
}
function fetchOk(json) {
  return async () => ({ ok: true, json: async () => json });
}

async function idbSeed(idb, dbName, storeName, key, val) {
  return new Promise((res) => {
    const req = idb.open(dbName, 1);
    req.onupgradeneeded = () => {};
    req.onsuccess = () => {
      const t = req.result.transaction(storeName);
      t.objectStore().put(val, key);
      setTimeout(res, 0);
    };
  });
}

(async () => {
  await scanFolderFilterTests();

  // Scenario A: no handle ever saved → banner stays hidden, no throw.
  await runDomScenario('no handle', async ({ banner }) => {
    await new Promise((res) => setTimeout(res, 20));
    check('no saved handle: banner stays hidden', banner.hidden === true);
  });

  // Scenario B: handle saved, permission granted, but no baseline snapshot
  // yet (first time this browser has ever seen this dashboard) → silent.
  await runDomScenario('handle, no baseline', async ({ banner, idb }) => {
    const handle = makeHandle([{ name: 'a.xlsx', size: 10, mtime_ms: 1 }], { perm: 'granted' });
    await idbSeed(idb, 'op-gen', 'handles', '42', handle);
    await new Promise((res) => setTimeout(res, 30));
    check('handle without baseline: banner stays hidden', banner.hidden === true);
  });

  // Scenario C: handle + baseline + a new file since last run → banner shows,
  // with the right added/changed counts and a working Run button that calls
  // through to root.opGenTriggerRun.
  await runDomScenario('handle + baseline + diff', async ({ banner, idb, successSource }) => {
    const handle = makeHandle([
      { name: 'a.xlsx', size: 10, mtime_ms: 1 },
      { name: 'b.xlsx', size: 99, mtime_ms: 2 },   // new since baseline
    ], { perm: 'granted' });
    await idbSeed(idb, 'op-gen', 'handles', '42', handle);
    await idbSeed(idb, 'op-gen-watch', 'snapshots', '42', { 'a.xlsx': [10, 1] });
    await new Promise((res) => setTimeout(res, 40));
    check('diff present: banner visible', banner.hidden === false);
    check('diff present: banner text mentions the new file', banner.textContent.indexOf('1 new') !== -1, banner.textContent);

    let triggered = false;
    successSource.opGenTriggerRun = () => { triggered = true; };
    const runBtn = banner.children[0].children[1].children[0];   // card > actions > run button
    check('run button is the primary action', runBtn.className.indexOf('primary') !== -1, runBtn.className);
    runBtn.dispatchEvent({ type: 'click' });
    check('run button calls opGenTriggerRun', triggered === true);
  });

  // Scenario D: handle + baseline + NO diff → banner stays hidden.
  await runDomScenario('handle + baseline, unchanged', async ({ banner, idb }) => {
    const handle = makeHandle([{ name: 'a.xlsx', size: 10, mtime_ms: 1 }], { perm: 'granted' });
    await idbSeed(idb, 'op-gen', 'handles', '42', handle);
    await idbSeed(idb, 'op-gen-watch', 'snapshots', '42', { 'a.xlsx': [10, 1] });
    await new Promise((res) => setTimeout(res, 40));
    check('no diff: banner stays hidden', banner.hidden === true);
  });

  // Scenario E: handle present but permission not granted → subtle reconnect
  // hint only, no directory scan attempted (queryPermission alone decides).
  await runDomScenario('handle, permission prompt', async ({ banner, idb }) => {
    const handle = makeHandle([{ name: 'a.xlsx', size: 10, mtime_ms: 1 }], { perm: 'prompt' });
    await idbSeed(idb, 'op-gen', 'handles', '42', handle);
    await idbSeed(idb, 'op-gen-watch', 'snapshots', '42', { 'a.xlsx': [10, 1] });
    await new Promise((res) => setTimeout(res, 40));
    check('permission not granted: reconnect hint shown', banner.hidden === false);
    check('permission not granted: subtle style, no run button', banner.textContent.indexOf('Reconnect') !== -1, banner.textContent);
  });

  // Scenario F: a run-success event rewrites the baseline and clears the banner.
  await runDomScenario('run-success updates baseline', async ({ banner, idb, successSource }) => {
    const handle = makeHandle([
      { name: 'a.xlsx', size: 10, mtime_ms: 1 },
      { name: 'b.xlsx', size: 99, mtime_ms: 2 },
    ], { perm: 'granted' });
    await idbSeed(idb, 'op-gen', 'handles', '42', handle);
    await idbSeed(idb, 'op-gen-watch', 'snapshots', '42', { 'a.xlsx': [10, 1] });
    await new Promise((res) => setTimeout(res, 40));
    check('pre-event: banner visible', banner.hidden === false);

    successSource.dispatchEvent(new Ev('op:gen:run-success', {
      detail: { files: [{ name: 'a.xlsx', size: 10, mtime_ms: 1 }, { name: 'b.xlsx', size: 99, mtime_ms: 2 }] },
    }));
    await new Promise((res) => setTimeout(res, 20));
    check('post-event: banner cleared', banner.hidden === true);

    const saved = idb._stores['op-gen-watch'] && idb._stores['op-gen-watch'].snapshots.get('42');
    check('post-event: baseline persisted with both files', saved && saved['b.xlsx'] && saved['b.xlsx'][0] === 99, JSON.stringify(saved));
  });

  // Scenario G: handle present, permission NOT granted, but NO baseline yet
  // (user picked a folder, never finished a run) → banner stays completely
  // silent, matching the plan's own caveat, instead of showing a reconnect
  // card that duplicates gen-engine.js's own reconnect button.
  await runDomScenario('handle, permission prompt, no baseline', async ({ banner, idb }) => {
    const handle = makeHandle([{ name: 'a.xlsx', size: 10, mtime_ms: 1 }], { perm: 'prompt' });
    await idbSeed(idb, 'op-gen', 'handles', '42', handle);
    await new Promise((res) => setTimeout(res, 30));
    check('no baseline: reconnect hint suppressed', banner.hidden === true);
  });

  // Scenario H: a file the manifest's declared inputs never admit (e.g. a
  // colleague's notes.xlsx next to the K11_*.xlsx the generator reads) must
  // never be reported as "new" — this is the failing input from the finding:
  // without allow-list filtering, this file nags on every single page load
  // forever because a run never adds it to the baseline either.
  await runDomScenario('handle + baseline, allow-list drops undeclared file', async ({ banner, idb }) => {
    const handle = makeHandle([
      { name: 'K11_2026.xlsx', size: 10, mtime_ms: 1 },
      { name: 'notes.xlsx', size: 5, mtime_ms: 2 },
    ], { perm: 'granted', name: 'K11' });
    await idbSeed(idb, 'op-gen', 'handles', '42', handle);
    await idbSeed(idb, 'op-gen-watch', 'snapshots', '42', { 'K11_2026.xlsx': [10, 1] });
    await new Promise((res) => setTimeout(res, 40));
    check('undeclared file never reported as new', banner.hidden === true);
  }, { fetch: fetchOk({ inputs: [{ match: 'K11_*.xlsx' }], source_rules: {}, manifest: {} }) });

  // Scenario I: reconnecting through the banner (a) requests the SAME
  // permission mode gen-engine.js's own pickMode() needs (readwrite when the
  // manifest writes results back — requesting plain 'read' would leave the
  // engine's own Run button, which checks readwrite, still disabled) and
  // (b) reloads the page on success so gen-engine.js's restoreHandle() runs
  // again and re-syncs its Run button instead of leaving it a silent no-op.
  await runDomScenario('reconnect requests write_back mode and reloads', async ({ banner, idb, reloadCalls }) => {
    const handle = makeHandle([{ name: 'a.xlsx', size: 10, mtime_ms: 1 }], { perm: 'prompt', requestResult: 'granted' });
    await idbSeed(idb, 'op-gen', 'handles', '42', handle);
    await idbSeed(idb, 'op-gen-watch', 'snapshots', '42', { 'a.xlsx': [10, 1] });
    await new Promise((res) => setTimeout(res, 40));
    check('reconnect hint shown', banner.hidden === false);

    const reconnectBtn = banner.children[0].children[0];   // subtle card > link button
    reconnectBtn.dispatchEvent({ type: 'click' });
    await new Promise((res) => setTimeout(res, 20));

    check('requestPermission asked for readwrite (manifest.write_back)',
      handle._requestCalls.indexOf('readwrite') !== -1, JSON.stringify(handle._requestCalls));
    check('successful reconnect reloads the page', reloadCalls.length === 1);
  }, { fetch: fetchOk({ inputs: [], source_rules: {}, manifest: { write_back: true } }) });

  console.log(FAILURES.length === 0 ? 'ALL PASS' : (FAILURES.length + ' FAILURE(S)'));
  process.exit(FAILURES.length === 0 ? 0 : 1);
})();
