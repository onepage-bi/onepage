/* Headless harness for the reference-table half of static/js/gen-engine.js
 * (ADR-16, phase 4).
 *
 * Same idiom as tests/quality_dom.js and tests/explore_dom.js: no jsdom, no
 * new dependency — the smallest DOM the engine actually touches, a stubbed
 * `fetch`, a stubbed `Worker`, an in-memory IndexedDB holding the folder
 * handle (so the run starts without a picker click), and the script loaded
 * with an indirect eval so its IIFE runs against these globals.
 *
 * What it pins:
 *   * the run panel draws one row per `reference[]` entry the manifest
 *     endpoint returns, checked by default, disabled with «reference table not
 *     yet created» when the table does not exist;
 *   * a ticked box makes the run fetch the version's xlsx and hand it to the
 *     worker inside `auxFiles` under `<slug>.xlsx` — that is what satisfies
 *     the manifest's platform input;
 *   * an unticked box fetches nothing and passes nothing;
 *   * the platform workbook NEVER reaches POST …/aux (the server rejects any
 *     name the manifest does not declare as an aux input — plan risk 2) and
 *     never appears in `input_manifest.files`;
 *   * every ref_lookup check spec is handed the table's normalised keys, once
 *     per slug rather than once per file;
 *   * the runs POST carries `input_manifest.reference`.
 *
 * Run: node tests/gen_engine_dom.js   (prints PASS/FAIL lines + ALL PASS)
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

// ── DOM ──────────────────────────────────────────────────────────────
class ClassList {
  constructor() { this.set = new Set(); }
  add() { for (const c of arguments) if (c) this.set.add(c); }
  remove() { for (const c of arguments) this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  get value() { return Array.from(this.set).join(' '); }
}
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parent = null;
    this.attrs = {};
    this.style = {};
    this.dataset = {};
    this.listeners = {};
    this._class = new ClassList();
    this._text = '';
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.checked = false;
    this.selected = false;
    this.type = '';
    this.title = '';
    this.href = '';
  }
  get classList() { return this._class; }
  set className(v) { this._class.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return this._class.value; }
  set textContent(v) { this.children = []; this._text = v === '' ? '' : String(v); }
  get textContent() { return this._text; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter(x => x !== c); return c; }
  insertBefore(n, ref) {
    n.parent = this;
    const i = this.children.indexOf(ref);
    if (i === -1) this.children.push(n); else this.children.splice(i, 0, n);
    return n;
  }
  get firstChild() { return this.children[0] || null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatchEvent(ev) { (this.listeners[ev.type] || []).forEach(f => f.call(this, ev)); return true; }
}
// Text nodes are the shape gen-engine's el() appends for string children.
function textOf(node) {
  if (!node) return '';
  if (node.nodeType === 3) return node.text;
  return (node.textContent || '') + node.children.map(textOf).join('');
}
function findAll(node, pred, out) {
  out = out || [];
  (node.children || []).forEach(c => { if (c.nodeType !== 3) { if (pred(c)) out.push(c); findAll(c, pred, out); } });
  return out;
}

let byIdMap = {};
const document = {
  createElement: t => new El(t),
  getElementById: id => byIdMap[id] || null,
  createTextNode: v => ({ nodeType: 3, text: String(v), children: [] }),
};

// ── stubs the engine boots against ───────────────────────────────────
const DASH_ID = 'dash-ref';
const SLUG = 'nwd_bv_map';

const MANIFEST_RESPONSE = {
  generator: { id: 3, name: 'ref-test' },
  version_number: 2,
  checksum_sha256: 'abc123',
  bundle_url: `/api/dashboards/${DASH_ID}/generator/bundle?v=2`,
  run_lock_ttl_s: 600,
  source_rules: {},
  manifest: {
    name: 'ref-test', engine: 'browser', write_back: false,
    data_scale: { max_files: 50, max_total_bytes: 50e6, max_file_bytes: 20e6 },
    aux_inputs: [],
  },
  inputs: [
    { match: 'SL_*.xlsx', role: 'data', required: true, sheets: [], source: 'folder', table: null, from_required_files: false },
    // `required` is flipped to true by one scenario below: a platform input is
    // never the analyst's to provide, so the pre-run required-file check must
    // ignore it whatever the manifest says — otherwise "without reference table" (a
    // mode ADR-16 requires to keep working) dies with "required input file
    // missing", which nobody can act on.
    { match: `${SLUG}.xlsx`, role: 'reference', required: false, sheets: [], source: 'platform', table: SLUG, from_required_files: false },
  ],
  checks: [
    { id: `ref_lookup:SL_*.xlsx:Orders:BV`, type: 'ref_lookup', match: 'SL_*.xlsx',
      sheet: 'Orders', column: 'BV', table: SLUG, on_fail: 'warn', label: null },
    { id: `ref_lookup:SL_2_*.xlsx:Orders:BV`, type: 'ref_lookup', match: 'SL_2_*.xlsx',
      sheet: 'Orders', column: 'BV', table: SLUG, on_fail: 'warn', label: null },
    { id: 'not_null:SL_*.xlsx:Orders:BV', type: 'not_null', match: 'SL_*.xlsx',
      sheet: 'Orders', column: 'BV', table: null, on_fail: 'warn', label: null },
  ],
  force_deps: ['python-calamine'],
  reference: [
    { slug: SLUG, title: 'BV NWD→CONTOSO', exists: true, current_version: 2,
      allow_pin: true, responsible: 'Alex',
      versions: [{ n: 2, created_at: '2026-08-25T10:00:00', row_count: 12 },
                 { n: 1, created_at: '2026-08-01T10:00:00', row_count: 9 }] },
    { slug: 'missing_map', title: 'missing_map', exists: false, current_version: 0,
      allow_pin: false, responsible: null, versions: [] },
  ],
};

const fetchCalls = [];
let refXlsxStatus = 200;
global.fetch = async (url, opts) => {
  fetchCalls.push({ url: String(url), opts: opts || null });
  const u = String(url);
  if (u.endsWith('/generator/manifest')) return jsonRes(MANIFEST_RESPONSE);
  if (/\/api\/reference\/[^/]+\/versions\/[^/]+\/xlsx$/.test(u)) {
    if (refXlsxStatus !== 200) return { ok: false, status: refXlsxStatus, text: async () => 'boom' };
    return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(128) };
  }
  if (/\/api\/reference\/[^/]+\/versions\/[^/]+\/keys$/.test(u)) {
    return jsonRes({ version: 2, keys: ['a1', 'a2'] });
  }
  if (u.endsWith('/generator/runs') && opts && opts.method === 'POST') {
    return { ok: true, status: 201, json: async () => ({
      run_id: 77, bundle_url: MANIFEST_RESPONSE.bundle_url, heartbeat_every_s: 60,
      observation_ids: {}, last_known: {},
    }) };
  }
  return jsonRes({});
};
function jsonRes(body) { return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) }; }

let lastWorker = null;
class WorkerStub {
  constructor(url) { this.url = url; this.messages = []; lastWorker = this; }
  postMessage(msg) { this.messages.push(msg); }
  terminate() {}
}

const lsStore = {};
global.localStorage = {
  getItem: k => (k in lsStore ? lsStore[k] : null),
  setItem: (k, v) => { lsStore[k] = String(v); },
  removeItem: k => { delete lsStore[k]; },
};

// The folder handle the engine restores from IndexedDB — a granted directory
// with one Excel file, so doRun() reaches the run POST without a picker.
const fileEntry = {
  kind: 'file', name: 'SL_2026.xlsx',
  getFile: async () => ({ size: 2048, lastModified: 1700000000000, arrayBuffer: async () => new ArrayBuffer(8) }),
};
const dirHandle = {
  name: 'SL',
  kind: 'directory',
  queryPermission: async () => 'granted',
  requestPermission: async () => 'granted',
  values: async function* () { yield fileEntry; },
};
// Minimal IndexedDB: one store, one key — enough for idbGet/idbPut.
const idbStore = { [DASH_ID]: dirHandle };
global.indexedDB = {
  open() {
    const req = {};
    setTimeout(() => {
      req.result = {
        transaction(_name, _mode) {
          const t = {
            objectStore() {
              return {
                get(key) { const r = {}; setTimeout(() => { r.result = idbStore[key]; r.onsuccess && r.onsuccess(); }, 0); return r; },
                put(val, key) { idbStore[key] = val; setTimeout(() => { t.oncomplete && t.oncomplete(); }, 0); },
              };
            },
          };
          return t;
        },
      };
      req.onsuccess && req.onsuccess();
    }, 0);
    return req;
  },
};

global.document = document;
global.window = { showDirectoryPicker: async () => dirHandle, prompt: () => null };
// node 24 exposes `navigator` as a getter-only global — define over it.
Object.defineProperty(global, 'navigator', {
  value: { deviceMemory: 8, hardwareConcurrency: 8 }, configurable: true, writable: true,
});
global.Worker = WorkerStub;
global.location = { reload() {}, href: 'http://x/d/' + DASH_ID };

const src = fs.readFileSync(path.join(ROOT, 'static/js/gen-engine.js'), 'utf8');

// One panel per scenario. The engine keeps `running`/`runId` in closure and
// only leaves the running state on a worker 'done' — this harness's worker
// never answers, so each scenario boots its OWN instance instead of pretending
// a run finished. localStorage is deliberately NOT reset between boots: that
// is how the «the panel remembers what I ticked» pin works.
let root = null;
function boot() {
  root = new El('div');
  root.dataset.dashboardId = DASH_ID;
  root.dataset.inputDirs = '[]';
  root.dataset.pyodideBase = '/static/pyodide/';
  root.dataset.assetV = '1';
  byIdMap['gen-engine-root'] = root;
  lastWorker = null;
  (0, eval)(src);
}

// ── helpers over the rendered panel ──────────────────────────────────
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle(n) { for (let i = 0; i < (n || 12); i++) await tick(); }
// The reference row's own checkbox — NOT «every checkbox in the panel»: the
// extra-folders block puts an «allow writing results» box in the panel too,
// and it is rendered before this block.
function refCheckbox(i) {
  const row = refRows()[i];
  return row && findAll(row, n => n.tagName === 'INPUT' && n.type === 'checkbox')[0];
}
function selects() { return findAll(root, n => n.tagName === 'SELECT'); }
function refRows() {
  // The reference block is the only place the panel puts a <label> whose text
  // starts with "Apply reference table".
  return findAll(root, n => n.tagName === 'LABEL' && textOf(n).indexOf('Apply reference table') === 0);
}
function runButton() {
  return findAll(root, n => n.tagName === 'BUTTON' && n.textContent === 'Run')[0];
}
function lastRunPost() {
  const posts = fetchCalls.filter(f => f.url.endsWith('/generator/runs') && f.opts && f.opts.method === 'POST');
  return posts.length ? JSON.parse(posts[posts.length - 1].opts.body) : null;
}
function auxPosts() { return fetchCalls.filter(f => /\/runs\/\d+\/aux$/.test(f.url)); }
function startMsg() { return lastWorker && lastWorker.messages[0]; }
async function bootAndRun() {
  const before = fetchCalls.length;
  boot();
  await settle();
  runButton().dispatchEvent({ type: 'click' });
  await settle(20);
  return fetchCalls.slice(before);
}

(async () => {
  // ── the panel, and a run with the box ticked ───────────────────────
  boot();
  await settle();

  const rows = refRows();
  check('reference: one row per manifest entry', rows.length === 2, String(rows.length));
  check('reference: existing table names its title, current version and date',
    rows[0] && textOf(rows[0]) === 'Apply reference table «BV NWD→CONTOSO» (v2 · 25.08.26)',
    rows[0] && textOf(rows[0]));
  check('reference: an existing filled table is ticked by default',
    refCheckbox(0) && refCheckbox(0).checked === true && refCheckbox(0).disabled === false);
  const missingRow = rows[1] && rows[1].parent;
  check('reference: a table nobody created -> disabled + «reference table not yet created»',
    refCheckbox(1) && refCheckbox(1).disabled === true && refCheckbox(1).checked === false
    && textOf(missingRow).indexOf('reference table not yet created') !== -1,
    missingRow && textOf(missingRow));
  check('reference: allow_pin renders a version picker, newest first, current selected',
    selects().length === 1 && selects()[0].children.length === 2
    && selects()[0].children[0].textContent === 'v2 · 25.08.26'
    && selects()[0].value === '2',
    String(selects().length));

  runButton().dispatchEvent({ type: 'click' });
  await settle(20);

  const xlsxCalls = fetchCalls.filter(f => f.url.indexOf('/xlsx') !== -1);
  check('run: the applied table is fetched as a workbook at its current version',
    xlsxCalls.length === 1 && xlsxCalls[0].url === `/api/reference/${SLUG}/versions/current/xlsx`,
    JSON.stringify(xlsxCalls.map(f => f.url)));

  const msg = startMsg();
  const auxNames = ((msg && msg.auxFiles) || []).map(a => a.name);
  check('run: the platform workbook rides into the worker as <slug>.xlsx',
    auxNames.length === 1 && auxNames[0] === SLUG + '.xlsx', JSON.stringify(auxNames));
  check('run: it carries a buffer, a size and the platform flag',
    msg && msg.auxFiles[0].size === 128 && msg.auxFiles[0].platform === true
    && msg.auxFiles[0].buf && typeof msg.auxFiles[0].mtime_ms === 'number',
    JSON.stringify(msg && msg.auxFiles[0] && Object.keys(msg.auxFiles[0])));

  const body = lastRunPost();
  check('run: input_manifest.reference records the resolved choice',
    body && JSON.stringify(body.input_manifest.reference)
      === JSON.stringify([{ slug: SLUG, version: null, applied: true }]),
    JSON.stringify(body && body.input_manifest.reference));
  check('run: the platform workbook is NOT listed in input_manifest.files',
    body && !body.input_manifest.files.some(f => f.name === SLUG + '.xlsx'),
    JSON.stringify(body && body.input_manifest.files));
  check('run: a table that does not exist is not sent at all',
    body && !body.input_manifest.reference.some(e => e.slug === 'missing_map'));
  check('run: uploadAux never sees the platform workbook (server rejects the name)',
    auxPosts().length === 0, String(auxPosts().length));

  const keysCalls = fetchCalls.filter(f => f.url.indexOf('/keys') !== -1);
  check('run: keys are fetched ONCE per slug, not once per ref_lookup check',
    keysCalls.length === 1 && keysCalls[0].url === `/api/reference/${SLUG}/versions/current/keys`,
    JSON.stringify(keysCalls.map(f => f.url)));
  const specs = (msg && msg.checks) || [];
  check('run: every ref_lookup spec is handed the keys',
    specs.filter(c => c.type === 'ref_lookup').length === 2
    && specs.filter(c => c.type === 'ref_lookup').every(c => JSON.stringify(c.keys) === '["a1","a2"]'),
    JSON.stringify(specs.map(c => [c.type, c.keys])));
  check('run: a non-ref_lookup check is left untouched',
    specs.filter(c => c.type === 'not_null').every(c => c.keys === undefined));

  // ── the observation gate is by DATA, not by engine name ───────────
  // The gate in flushObservations used to read `engineMode === 'agent'`,
  // which silenced the whole queue for any engine that was not the browser.
  // What it must actually ask is whether observations arrived — so a run
  // that DID observe still ships its batch.
  const beforeObs = fetchCalls.length;
  const observed = [];
  for (let i = 0; i < 25; i++) observed.push({ name: 'SL_' + i + '.xlsx', kind: 'parsed', size: 10, mtime_ms: 1 });
  lastWorker.onmessage({ data: { type: 'observe', files: observed } });
  await settle(20);
  check('observations: a batch that arrived is posted',
    fetchCalls.slice(beforeObs).some(f => /\/runs\/\d+\/observations$/.test(f.url)),
    JSON.stringify(fetchCalls.slice(beforeObs).map(f => f.url)));

  // ── untick, run again on a fresh panel ─────────────────────────────
  boot();
  await settle();
  const cb = refCheckbox(0);
  cb.checked = false;
  cb.dispatchEvent({ type: 'change' });
  check('reference: the choice is remembered in localStorage',
    JSON.parse(localStorage.getItem('op-ref:' + DASH_ID))[SLUG].applied === false,
    localStorage.getItem('op-ref:' + DASH_ID));

  const before2 = fetchCalls.length;
  runButton().dispatchEvent({ type: 'click' });
  await settle(20);
  const after2 = fetchCalls.slice(before2);
  check('run: an unticked table fetches no workbook',
    after2.filter(f => f.url.indexOf('/xlsx') !== -1).length === 0,
    JSON.stringify(after2.map(f => f.url)));
  check('run: nothing rides into the worker as auxFiles',
    (startMsg().auxFiles || []).length === 0, JSON.stringify(startMsg().auxFiles));
  const body2 = lastRunPost();
  check('run: the «without the dictionary» decision is still sent',
    body2 && JSON.stringify(body2.input_manifest.reference)
      === JSON.stringify([{ slug: SLUG, version: null, applied: false }]),
    JSON.stringify(body2 && body2.input_manifest.reference));
  check('run: keys are still fetched with the box unticked (the author needs to know)',
    after2.filter(f => f.url.indexOf('/keys') !== -1).length === 1,
    JSON.stringify(after2.map(f => f.url)));

  // ── a fresh panel restores the remembered answer, then pins v1 ─────
  boot();
  await settle();
  check('reference: a reopened panel restores the unticked box from localStorage',
    refCheckbox(0).checked === false);
  const cb3 = refCheckbox(0);
  cb3.checked = true;
  cb3.dispatchEvent({ type: 'change' });
  const sel = selects()[0];
  sel.value = '1';
  sel.dispatchEvent({ type: 'change' });
  check('reference: a pin below current is remembered',
    JSON.parse(localStorage.getItem('op-ref:' + DASH_ID))[SLUG].version === 1,
    localStorage.getItem('op-ref:' + DASH_ID));

  const before3 = fetchCalls.length;
  runButton().dispatchEvent({ type: 'click' });
  await settle(20);
  const after3 = fetchCalls.slice(before3);
  check('run: the pinned version is what is fetched, for the workbook AND the keys',
    after3.some(f => f.url === `/api/reference/${SLUG}/versions/1/xlsx`)
    && after3.some(f => f.url === `/api/reference/${SLUG}/versions/1/keys`),
    JSON.stringify(after3.map(f => f.url)));
  check('run: the pin rides in input_manifest.reference',
    lastRunPost().input_manifest.reference[0].version === 1,
    JSON.stringify(lastRunPost().input_manifest.reference));

  // ── a REQUIRED platform input, with the box unticked ───────────────
  // The workbook the pre-check would look for is the one the server only
  // materialises when the box is ticked, so counting it as required makes the
  // "without reference table" mode unreachable.
  MANIFEST_RESPONSE.inputs[1].required = true;
  lsStore['op-ref:' + DASH_ID] = JSON.stringify({ [SLUG]: { applied: false, version: null } });
  const after5 = await bootAndRun();
  check('run: a REQUIRED platform input does not block the unticked run',
    after5.some(f => f.url.endsWith('/generator/runs')),
    JSON.stringify(after5.map(f => f.url)));
  check('run: the folder input is still required (the guard is not disarmed)',
    MANIFEST_RESPONSE.inputs[0].required === true);
  MANIFEST_RESPONSE.inputs[1].required = false;

  // ── a keys fetch that fails must not leave the PREVIOUS run's keys ──
  // checksDecl is module state read once at init; a stale key set would make
  // the check compare against yesterday's dictionary instead of reporting
  // itself skipped (`no_keys`).
  lsStore['op-ref:' + DASH_ID] = JSON.stringify({ [SLUG]: { applied: true, version: null } });
  await bootAndRun();                       // run A: keys attach normally
  const okSpecs = startMsg().checks.filter(c => c.type === 'ref_lookup');
  check('run A: keys attached', okSpecs.every(c => JSON.stringify(c.keys) === '["a1","a2"]'));
  const realFetch = global.fetch;
  global.fetch = async (url, opts) => {
    if (/\/keys$/.test(String(url))) { fetchCalls.push({ url: String(url), opts: null }); throw new Error('offline'); }
    return realFetch(url, opts);
  };
  await bootAndRun();                       // run B: keys fetch throws
  global.fetch = realFetch;
  const staleSpecs = startMsg().checks.filter(c => c.type === 'ref_lookup');
  check('run B: a failed keys fetch leaves NO stale keys on the spec',
    staleSpecs.length === 2 && staleSpecs.every(c => c.keys === undefined),
    JSON.stringify(staleSpecs.map(c => c.keys)));

  // ── localStorage: a remembered choice survives exists:false ─────────
  lsStore['op-ref:' + DASH_ID] = JSON.stringify({
    [SLUG]: { applied: false, version: null },
    missing_map: { applied: true, version: 3 },
  });
  boot();
  await settle();
  refCheckbox(0).checked = true;
  refCheckbox(0).dispatchEvent({ type: 'change' });   // any save rewrites the blob
  check('reference: a save does not erase the choice of a table that is not usable',
    JSON.parse(lsStore['op-ref:' + DASH_ID]).missing_map
    && JSON.parse(lsStore['op-ref:' + DASH_ID]).missing_map.version === 3,
    lsStore['op-ref:' + DASH_ID]);

  // ── the workbook will not load ─────────────────────────────────────
  delete lsStore['op-ref:' + DASH_ID];
  refXlsxStatus = 500;
  const after4 = await bootAndRun();
  check('run: a workbook that will not load ABORTS the run (never a silent raw-code report)',
    !after4.some(f => f.url.endsWith('/generator/runs')) && lastWorker === null,
    JSON.stringify(after4.map(f => f.url)));
  const errText = findAll(root, n => textOf(n).indexOf('Failed to load reference table') === 0);
  check('run: the failure is spelled out in the panel', errText.length > 0, String(errText.length));

  console.log();
  if (FAILURES.length) { console.log(FAILURES.length + ' FAILED: ' + FAILURES.join(', ')); process.exit(1); }
  console.log('ALL PASS');
  process.exit(0);
})();
