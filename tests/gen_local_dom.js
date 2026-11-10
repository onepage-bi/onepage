/* Headless harness for the «compute on this computer» half of
 * static/js/gen-engine.js (track G, contracts K1 and K3).
 *
 * Same idiom as tests/gen_engine_dom.js: no jsdom, no new dependency — the
 * smallest DOM the engine actually touches, a stubbed `fetch` that answers
 * both OnePage and the loopback agent, and the script loaded with an
 * indirect eval so its IIFE runs against these globals.
 *
 * What it pins:
 *   * no agent → the panel is EXACTLY what it was: no switch, no new rows
 *     (the whole point of «no agent, nothing changes»);
 *   * agent + a folders.ini section → a two-way switch, default `browser`,
 *     remembered per report in localStorage;
 *   * in `local` mode the browser folder controls disappear and the folder
 *     card from the agent's own answer appears — real path, file count,
 *     volume, the four missing reasons in plain words, the total;
 *   * the run goes through startAgentRun(): POST …/generator/runs carries
 *     `engine: "local"` and the K3 input_manifest (files with `path`/`dir`,
 *     `roots`, `dirs`, `scenario`), and the multipart `meta` handed to the
 *     agent carries mode/dashboard_id/scenario;
 *   * C.6 does not apply (6000 files is a WARNING, not a refusal), but zero
 *     files and duplicate basenames are hard refusals;
 *   * the observation gate is by data, not by engine name.
 *
 * Run: node tests/gen_local_dom.js   (prints PASS/FAIL lines + ALL PASS)
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
    this.max = 0;
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

// ── stubs ────────────────────────────────────────────────────────────
const DASH_ID = 'dash-loc';
const AGENT = 'http://127.0.0.1:47113';

const MANIFEST_RESPONSE = {
  generator: { id: 9, name: 'folders-test' },
  version_number: 3,
  checksum_sha256: 'cafe',
  bundle_url: `/api/dashboards/${DASH_ID}/generator/bundle?v=3`,
  run_lock_ttl_s: 600,
  source_rules: {},
  manifest: {
    name: 'folders-test', engine: 'browser', write_back: false,
    data_scale: { max_files: 500, max_total_bytes: 500e6, max_file_bytes: 35e6 },
    aux_inputs: [],
  },
  inputs: [{ match: 'SL_*.xlsx', role: 'data', required: true, sheets: [], source: 'folder', table: null, from_required_files: false }],
  checks: [],
  force_deps: [],
  reference: [],
};

// The agent's own answer — the K1 shape, verbatim.
function foldersOk(over) {
  return Object.assign({
    ok: true,
    ini: 'C:\\op-agent\\folders.ini',
    scenario: '',
    scenarios: ['', 'without-irkutsk'],
    entries: [
      { index: 0, kind: 'dir', raw: 'D:\\Exports', include: ['SL_*.xlsx'], exclude: [] },
      { index: 1, kind: 'dir', raw: '\\\\share\\reasons', include: [], exclude: [] },
    ],
    files: [
      { name: 'SL_01.xlsx', size: 1000, mtime_ms: 1760000000000, path: 'D:\\Exports\\SL_01.xlsx', dir: 0 },
      { name: 'SL_02.xlsx', size: 3000, mtime_ms: 1760000000001, path: 'D:\\Exports\\SL_02.xlsx', dir: 0 },
      { name: 'reasons.xlsx', size: 500, mtime_ms: 1760000000002, path: '\\\\share\\reasons\\reasons.xlsx', dir: 1 },
    ],
    roots: [
      { dir: 0, root_path: 'D:\\Exports', root_id: '0123456789abcdef' },
      { dir: 1, root_path: '\\\\share\\reasons', root_id: 'fedcba9876543210' },
    ],
    missing: [{ raw: 'E:\\Archive', reason: 'not_found' }, { raw: 'F:\\Restricted', reason: 'denied' }],
    duplicates: [],
    totals: { count: 3, bytes: 4500 },
  }, over || {});
}

let agentAlive = true;
let foldersPayload = foldersOk();
let foldersStatus = 200;
const fetchCalls = [];

// Catch-up queue (phase 7): what the agent answers on GET /local-runs. Empty
// by default — the vast majority of page opens land on a report nobody has
// computed offline, and it must look exactly as before.
let eventLines = [];                 // NDJSON frames that /events will return
const enc = new TextEncoder();
let localRunsPayload = { ok: true, runs: [] };
let markPublishedOk = true;
function localRun(over) {
  return Object.assign({
    dashboard_id: DASH_ID, k: 1, created_at: '2026-10-11T19:04:00',
    based_on_server_version: 7, published: false, bridge: 'borrowed',
    scenario: '', note: 'built offline 2026-10-11, published 2026-10-19',
    generator: { id: 9, name: 'folders-test', version: '2.2.25' },
    outputs: { html: 'dashboard.html', slots: { '0': 'data0.bin' } },
    input_manifest: {
      source: 'folders', scenario: '', count: 2, total_bytes: 4000,
      dirs: ['Exports'],
      files: [{ name: 'SL_01.xlsx', size: 1000, dir: 0, path: 'D:\\Exports\\SL_01.xlsx' }],
      roots: [{ dir: 0, root_path: 'D:\\Exports', root_id: '0123456789abcdef' }],
    },
    observe: [{ t: 'observe', files: [{ dir: 0, name: 'SL_01.xlsx', kind: 'parsed',
                                        schema: { sheets: [] }, consumed: ['outlet'] }] }],
  }, over || {});
}

function jsonRes(body, status) {
  return { ok: (status || 200) < 400, status: status || 200, json: async () => body, text: async () => JSON.stringify(body) };
}

global.fetch = async (url, opts) => {
  const u = String(url);
  fetchCalls.push({ url: u, opts: opts || null });
  if (u === AGENT + '/health') {
    if (!agentAlive) throw new Error('refused');
    return jsonRes({ app: 'op-agent', protocol: 2, version: '1.4.0', paired: true });
  }
  if (u.indexOf('127.0.0.1') !== -1 && u.indexOf('/health') !== -1) throw new Error('refused');
  if (u.indexOf(AGENT + '/folders') === 0) return jsonRes(foldersPayload, foldersStatus);
  if (u.indexOf(AGENT + '/local-runs/output') === 0) {
    return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(16) };
  }
  if (u === AGENT + '/local-runs/published') return jsonRes({ ok: markPublishedOk }, markPublishedOk ? 200 : 404);
  if (u.indexOf(AGENT + '/local-runs') === 0) return jsonRes(localRunsPayload);
  if (u.indexOf(AGENT + '/runs') === 0 && /\/events$/.test(u)) {
    // The NDJSON stream: by default it closes immediately, so startAgentRun
    // returns and the harness can inspect what was sent without faking a
    // whole run. `eventLines` lets one test feed real frames instead.
    const lines = eventLines.slice();
    eventLines = [];
    let i = 0;
    return { ok: true, status: 200, body: { getReader: () => ({
      read: async () => (i < lines.length
        ? { done: false, value: enc.encode(JSON.stringify(lines[i++]) + '\n') }
        : { done: true }),
    }) } };
  }
  if (u === AGENT + '/runs' && opts && opts.method === 'POST') {
    return jsonRes({ agent_run_id: 'ar-1' }, 202);
  }
  if (/\/runs\/[^/]+\/outputs\//.test(u)) {
    return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(16) };
  }
  if (u.endsWith('/generator/manifest')) return jsonRes(MANIFEST_RESPONSE);
  if (u.endsWith('/generator/runs') && opts && opts.method === 'POST') {
    return { ok: true, status: 201, json: async () => ({
      run_id: 501, bundle_url: MANIFEST_RESPONSE.bundle_url, heartbeat_every_s: 60,
      observation_ids: {}, last_known: {},
    }) };
  }
  if (u.indexOf('/generator/bundle') !== -1) {
    return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(64) };
  }
  return jsonRes({});
};

// A pairing token already in hand: pairing needs a human at that machine, so
// the panel never starts one by itself — it probes with the token it has.
const lsStore = { 'op-agent-token': 'tok-1' };
global.localStorage = {
  getItem: k => (k in lsStore ? lsStore[k] : null),
  setItem: (k, v) => { lsStore[k] = String(v); },
  removeItem: k => { delete lsStore[k]; },
};

// A granted browser folder, so the browser half of the panel is fully alive
// and «the switch hid it» is a real observation rather than «it was never
// built».
const dirHandle = {
  name: 'SL', kind: 'directory',
  queryPermission: async () => 'granted',
  requestPermission: async () => 'granted',
  values: async function* () {},
};
const idbStore = { [DASH_ID]: dirHandle };
global.indexedDB = {
  open() {
    const req = {};
    setTimeout(() => {
      req.result = {
        transaction() {
          const tx = {
            objectStore() {
              return {
                get(key) { const r = {}; setTimeout(() => { r.result = idbStore[key]; r.onsuccess && r.onsuccess(); }, 0); return r; },
                put(val, key) { idbStore[key] = val; setTimeout(() => { tx.oncomplete && tx.oncomplete(); }, 0); },
              };
            },
          };
          return tx;
        },
      };
      req.onsuccess && req.onsuccess();
    }, 0);
    return req;
  },
};

global.document = document;
global.window = { showDirectoryPicker: async () => dirHandle, prompt: () => null };
Object.defineProperty(global, 'navigator', {
  value: { deviceMemory: 8, hardwareConcurrency: 8 }, configurable: true, writable: true,
});
global.Worker = class { constructor() {} postMessage() {} terminate() {} };
global.location = { reload() {}, href: 'http://x/d/' + DASH_ID };

const src = fs.readFileSync(path.join(ROOT, 'static/js/gen-engine.js'), 'utf8');

let root = null;
function boot() {
  root = new El('div');
  root.dataset.dashboardId = DASH_ID;
  root.dataset.inputDirs = '[]';
  root.dataset.pyodideBase = '/static/pyodide/';
  root.dataset.assetV = '1';
  byIdMap['gen-engine-root'] = root;
  (0, eval)(src);
}

const tick = () => new Promise(r => setTimeout(r, 0));
async function settle(n) { for (let i = 0; i < (n || 25); i++) await tick(); }

function radios() { return findAll(root, n => n.tagName === 'INPUT' && n.type === 'radio'); }
function locLabels() {
  return findAll(root, n => n.tagName === 'LABEL' && textOf(n).indexOf('Run ') === 0).map(textOf);
}
function runButton() { return findAll(root, n => n.tagName === 'BUTTON' && n.textContent === 'Run')[0]; }
function chooseButton() { return findAll(root, n => n.tagName === 'BUTTON' && n.textContent === 'Choose folder…')[0]; }
function panelText() { return textOf(root); }
function runPosts() {
  return fetchCalls.filter(f => f.url.endsWith('/generator/runs') && f.opts && f.opts.method === 'POST');
}
function lastRunBody() {
  const p = runPosts();
  return p.length ? JSON.parse(p[p.length - 1].opts.body) : null;
}
function agentPushes() { return fetchCalls.filter(f => f.url === AGENT + '/runs' && f.opts && f.opts.method === 'POST'); }
function pickLocal() {
  const r = radios().filter(n => n.value === 'local')[0];
  r.checked = true;
  radios().filter(n => n.value === 'browser').forEach(n => { n.checked = false; });
  r.dispatchEvent({ type: 'change' });
}

(async () => {
  // ── 1. No agent at all: the panel must be untouched ────────────────
  agentAlive = false;
  boot();
  await settle();
  check('no agent: no location switch is offered', radios().length === 0, String(radios().length));
  check('no agent: the browser folder picker is still there and visible',
    chooseButton() && chooseButton().parent.style.display !== 'none');
  check('no agent: not one word about op-agent on the panel',
    panelText().indexOf('op-agent') === -1);

  // ── 2. Agent + a section: the switch appears, default is browser ───
  agentAlive = true;
  boot();
  await settle();
  check('agent: GET /folders is asked with this dashboard',
    fetchCalls.some(f => f.url === AGENT + '/folders?dashboard=' + DASH_ID),
    JSON.stringify(fetchCalls.filter(f => f.url.indexOf('/folders') !== -1).map(f => f.url)));
  check('agent: a two-way switch is offered', radios().length === 2, String(radios().length));
  check('agent: the two choices are named in plain words',
    JSON.stringify(locLabels()) === JSON.stringify(['Run here, in the browser', 'Run on this computer (op-agent)']),
    JSON.stringify(locLabels()));
  check('agent: the default is the browser — nothing changes by itself',
    radios()[0].checked === true && radios()[1].checked === false);
  check('agent: the folder card stays hidden while the browser is chosen',
    panelText().indexOf('D:\\Exports') === -1);
  check('agent: the browser folder picker is still visible in browser mode',
    chooseButton().parent.style.display !== 'none');

  // ── 3. Switch to «on this computer»: card in, browser controls out ─
  pickLocal();
  check('local: the browser folder controls are hidden',
    chooseButton().parent.style.display === 'none');
  const txt = panelText();
  check('local: every entry is listed by its REAL path',
    txt.indexOf('D:\\Exports') !== -1 && txt.indexOf('\\\\share\\reasons') !== -1, txt);
  check('local: each entry carries its own file count and volume',
    txt.indexOf('2 files · 0.0 MB') !== -1 && txt.indexOf('1 files · 0.0 MB') !== -1, txt);
  check('local: the include mask of an entry is shown', txt.indexOf('SL_*.xlsx') !== -1);
  check('local: a missing folder says WHICH of the four reasons it is',
    txt.indexOf('E:\\Archive — folder or file not found') !== -1
    && txt.indexOf('F:\\Restricted — no access') !== -1, txt);
  check('local: the total is spelled out', txt.indexOf('Total 3 files') !== -1, txt);
  check('local: the folders.ini that answered is named', txt.indexOf('C:\\op-agent\\folders.ini') !== -1);
  check('local: the scenario picker appears when there is more than one set',
    findAll(root, n => n.tagName === 'SELECT').length === 1);
  check('local: Run is enabled although no browser folder is connected',
    runButton().disabled === false);
  check('local: the choice is remembered for this report',
    lsStore['op-gen-loc:' + DASH_ID] === 'local', lsStore['op-gen-loc:' + DASH_ID]);

  // ── 4. A reopened panel comes back in local mode ───────────────────
  boot();
  await settle();
  check('restore: a reopened panel is in local mode',
    radios()[1].checked === true && chooseButton().parent.style.display === 'none');

  // ── 5. The run: K2 engine + K3 manifest + K3 meta ──────────────────
  const before = runPosts().length;
  runButton().dispatchEvent({ type: 'click' });
  await settle(40);
  check('run: exactly one run was started', runPosts().length === before + 1);
  const body = lastRunBody();
  check('run: engine is "local" (K2)', body && body.engine === 'local', JSON.stringify(body && body.engine));
  const im = (body && body.input_manifest) || {};
  check('run: input_manifest.source is "folders" and the scenario rides along',
    im.source === 'folders' && im.scenario === '', JSON.stringify([im.source, im.scenario]));
  check('run: every file carries name/size/mtime_ms/dir/path',
    im.files && im.files.length === 3
    && im.files[0].path === 'D:\\Exports\\SL_01.xlsx' && im.files[0].dir === 0
    && im.files[2].dir === 1 && typeof im.files[2].mtime_ms === 'number',
    JSON.stringify(im.files));
  check('run: roots carry dir/root_path/root_id',
    JSON.stringify(im.roots) === JSON.stringify([
      { dir: 0, root_path: 'D:\\Exports', root_id: '0123456789abcdef' },
      { dir: 1, root_path: '\\\\share\\reasons', root_id: 'fedcba9876543210' }]),
    JSON.stringify(im.roots));
  check('run: dirs are the root paths', JSON.stringify(im.dirs) === JSON.stringify(['D:\\Exports', '\\\\share\\reasons']));
  check('run: the totals are the agent\'s own', im.count === 3 && im.total_bytes === 4500,
    JSON.stringify([im.count, im.total_bytes]));
  check('run: folders are re-resolved at click time, not reused from the probe',
    fetchCalls.filter(f => f.url.indexOf(AGENT + '/folders') === 0).length >= 2);

  const push = agentPushes()[agentPushes().length - 1];
  check('run: the bundle is pushed to the agent', !!push);
  const meta = push && JSON.parse(await push.opts.body.get('meta'));
  check('run: meta says mode/dashboard_id/scenario (K3)',
    meta && meta.mode === 'folders' && meta.dashboard_id === DASH_ID && meta.scenario === '',
    JSON.stringify(meta));
  check('run: meta still carries the checksum and run_id the agent engine needs',
    meta && meta.run_id === 501 && typeof meta.checksum_sha256 === 'string' && meta.checksum_sha256.length === 64);

  // The observation gate is by DATA, not by engine name: nobody observed this
  // run (no Pyodide worker, and the agent files its own batches), so the page
  // must not post an empty final:true in its name.
  check('observations: a run nobody observed posts nothing',
    !fetchCalls.some(f => /\/observations$/.test(f.url)),
    JSON.stringify(fetchCalls.filter(f => /observations/.test(f.url)).map(f => f.url)));

  // ── 5b. Lineage of a native run ────────────────────────────────────
  // The R2 host sends `observe` frames over the same NDJSON as stages and
  // progress. Before this check the page silently dropped them, and a run
  // «on this computer» reached the platform without a single column read,
  // although the agent had named them honestly.
  boot();
  await settle();
  pickLocal();
  await settle();
  eventLines = [
    { t: 'stage', stage: 'generate' },
    { t: 'observe', files: [{ dir: 0, name: 'SL_01.xlsx', kind: 'parsed',
                              schema: { sheets: [{ name: 'Sheet1' }] }, consumed: ['outlet'] }] },
    { t: 'done', outputs: { html: 'dashboard.html', slots: {} } },
  ];
  const beforeObs = fetchCalls.filter(f => /\/observations$/.test(f.url)).length;
  runButton().dispatchEvent({ type: 'click' });
  await settle(60);
  const nativeObs = fetchCalls.filter(f => /\/observations$/.test(f.url));
  check('observations: the agent observe frames reach the platform',
    nativeObs.length === beforeObs + 1
    && JSON.parse(nativeObs[nativeObs.length - 1].opts.body).files[0].name === 'SL_01.xlsx',
    JSON.stringify(nativeObs.map(f => f.opts.body)));
  check('observations: the final batch goes out BEFORE publishing',
    (() => {
      const o = nativeObs[nativeObs.length - 1];
      const at = fetchCalls.indexOf(o);
      const pub = fetchCalls.findIndex((f, i) => i > at && /\/publish$/.test(f.url));
      return JSON.parse(o.opts.body).final === true && pub > at;
    })());

  // ── 6. C.6 does not apply here, but zero files does ────────────────
  // 6000 files / 30 GB would be a hard stop in the tab. On this computer it
  // is only a hint that `dir = C:\` may be a typo.
  const many = [];
  for (let i = 0; i < 6000; i++) many.push({ name: 'f' + i + '.xlsx', size: 1024, mtime_ms: 1, path: 'D:\\x\\f' + i + '.xlsx', dir: 0 });
  many.push({ name: 'SL_01.xlsx', size: 1024, mtime_ms: 1, path: 'D:\\x\\SL_01.xlsx', dir: 0 });
  foldersPayload = foldersOk({ files: many, missing: [], totals: { count: many.length, bytes: many.length * 1024 } });
  boot();
  await settle();
  const before6 = runPosts().length;
  runButton().dispatchEvent({ type: 'click' });
  await settle(40);
  check('limits: 6001 files does NOT block the run (C.6 is a tab limit, not a data limit)',
    runPosts().length === before6 + 1, String(runPosts().length - before6));
  check('limits: but it warns, so a `dir = C:\\` typo is caught before the hour',
    panelText().indexOf('That is a lot') !== -1, panelText().slice(0, 300));

  // ── 7. Zero files: a hard refusal, never a silent empty run ────────
  foldersPayload = foldersOk({ files: [], roots: [], totals: { count: 0, bytes: 0 } });
  boot();
  await settle();
  const before7 = runPosts().length;
  runButton().dispatchEvent({ type: 'click' });
  await settle(40);
  check('empty: no run is started at all', runPosts().length === before7, String(runPosts().length - before7));
  check('empty: the refusal names the reason per folder',
    panelText().indexOf('Not a single file was found in these folders') !== -1
    && panelText().indexOf('no access') !== -1, panelText().slice(0, 400));

  // ── 8. Duplicates and required files reuse the existing cards ──────
  foldersPayload = foldersOk({
    duplicates: [{ name: 'SL_01.xlsx', paths: ['D:\\a\\SL_01.xlsx', 'D:\\b\\SL_01.xlsx'] }],
  });
  boot();
  await settle();
  const before8 = runPosts().length;
  runButton().dispatchEvent({ type: 'click' });
  await settle(40);
  check('dupes: the run is refused', runPosts().length === before8);
  check('dupes: through the EXISTING diagnosis card, not a new one',
    panelText().indexOf('Duplicate file names across folders') !== -1
    && panelText().indexOf('D:\\a\\SL_01.xlsx, D:\\b\\SL_01.xlsx') !== -1, panelText().slice(0, 400));

  foldersPayload = foldersOk({
    files: [{ name: 'other.xlsx', size: 10, mtime_ms: 1, path: 'D:\\x\\other.xlsx', dir: 0 }],
    duplicates: [], missing: [], totals: { count: 1, bytes: 10 },
  });
  boot();
  await settle();
  const before9 = runPosts().length;
  runButton().dispatchEvent({ type: 'click' });
  await settle(40);
  check('required: a manifest-required input nobody has stops the run',
    runPosts().length === before9
    && panelText().indexOf('Required input file missing') !== -1, panelText().slice(0, 400));

  // ── 8b. A budget-truncated answer must not read as a full one ──────
  foldersPayload = foldersOk({ partial: true, budget_s: 5, pending: ['\\\\share\\reasons'] });
  boot();
  await settle();
  check('partial: the card says the list is incomplete and names what was not walked',
    panelText().indexOf('The list is incomplete') !== -1 && panelText().indexOf('its 5 s budget') !== -1,
    panelText().slice(0, 400));

  // ── 9. folders.ini without a section for this report ───────────────
  foldersPayload = { ok: false, error: 'no_section' };
  foldersStatus = 404;
  boot();
  await settle();
  check('no_section: nothing is offered — the switch never appears',
    radios().length === 0, String(radios().length));
  foldersStatus = 200;
  foldersPayload = foldersOk();

  // ── 10. Catch-up: a run was computed offline and awaits publishing ──
  // Publishing silently without asking is not allowed: a publish creates a NEW
  // report version that everyone sees — that is a person's decision.
  localRunsPayload = { ok: true, runs: [] };
  boot();
  await settle();
  check('catchup: empty queue — the panel says nothing about catch-up',
    panelText().indexOf('while offline') === -1, panelText().slice(0, 200));

  localRunsPayload = { ok: true, runs: [localRun()] };
  const beforeAsk = runPosts().length;
  boot();
  await settle();
  check('catchup: the queue is requested from the agent for THIS report',
    fetchCalls.some(f => f.url === AGENT + '/local-runs?dashboard=' + DASH_ID),
    JSON.stringify(fetchCalls.filter(f => /local-runs/.test(f.url)).map(f => f.url)));
  check('catchup: the person is asked an explicit question, not shown a fact',
    panelText().indexOf('computed on this machine while offline') !== -1
    && panelText().indexOf('Publish to the platform') !== -1, panelText().slice(0, 500));
  check('catchup: the computation date is named, not today',
    panelText().indexOf('2026') !== -1, panelText().slice(0, 500));
  check('catchup: nothing starts without consent',
    runPosts().length === beforeAsk, String(runPosts().length - beforeAsk));

  const btnLater = findAll(root, n => n.tagName === 'BUTTON' && n.textContent === 'Not now')[0];
  check('catchup: there is a decline, not only consent', !!btnLater);
  btnLater.dispatchEvent({ type: 'click' });
  await settle();
  check('catchup: «not now» hides the callout and does NOT mark the run published',
    panelText().indexOf('Publish to the platform') === -1
    && !fetchCalls.some(f => f.url === AGENT + '/local-runs/published'),
    panelText().slice(0, 200));

  // Consent: the whole path from the run to the mark at the agent.
  localRunsPayload = { ok: true, runs: [localRun()] };
  boot();
  await settle();
  const beforeC = runPosts().length;
  findAll(root, n => n.tagName === 'BUTTON' && n.textContent === 'Publish to the platform')[0]
    .dispatchEvent({ type: 'click' });
  await settle(60);
  check('catchup: exactly one run is created on the platform',
    runPosts().length === beforeC + 1, String(runPosts().length - beforeC));
  const cbody = lastRunBody();
  check('catchup: starts as local', cbody && cbody.engine === 'local', JSON.stringify(cbody && cbody.engine));
  check('catchup: the start manifest is the run SAVED input, not a rebuilt one',
    cbody && cbody.input_manifest && cbody.input_manifest.files.length === 1
    && cbody.input_manifest.files[0].path === 'D:\\Exports\\SL_01.xlsx',
    JSON.stringify(cbody && cbody.input_manifest && cbody.input_manifest.files));
  check('catchup: the manifest carries the offline mark with the computation date',
    cbody && cbody.input_manifest.offline
    && cbody.input_manifest.offline.created_at === '2026-10-11T19:04:00'
    && cbody.input_manifest.offline.k === 1,
    JSON.stringify(cbody && cbody.input_manifest.offline));
  check('catchup: outputs are fetched from the agent by inventory (document and slot)',
    fetchCalls.some(f => /\/local-runs\/output.*name=dashboard\.html/.test(f.url))
    && fetchCalls.some(f => /\/local-runs\/output.*name=data0\.bin/.test(f.url)),
    JSON.stringify(fetchCalls.filter(f => /local-runs\/output/.test(f.url)).map(f => f.url)));
  // Count ONLY what went out after consent: fetchCalls accumulates across all
  // harness runs, and «exactly one» over the whole log would be false.
  const since = fetchCalls.slice(fetchCalls.indexOf(runPosts()[runPosts().length - 1]));
  const obs = since.filter(f => /\/observations$/.test(f.url));
  check('catchup: the offline run lineage reached the platform',
    obs.length === 1 && JSON.parse(obs[0].opts.body).files[0].name === 'SL_01.xlsx',
    JSON.stringify(obs.map(f => f.opts.body)));
  const pubs = since.filter(f => /\/publish$/.test(f.url));
  check('catchup: published via the same POST …/publish, no new endpoints',
    pubs.length === 1, String(pubs.length));
  check('catchup: lineage went out BEFORE publishing',
    since.indexOf(obs[0]) < since.indexOf(pubs[0]));
  const marks = since.filter(f => f.url === AGENT + '/local-runs/published');
  check('catchup: the agent is told «published» — and only AFTER a successful publish',
    marks.length === 1 && since.indexOf(marks[0]) > since.indexOf(pubs[0]),
    String(marks.length));
  check('catchup: the mark names the run by number, not «latest»',
    marks.length === 1 && JSON.parse(marks[0].opts.body).k === 1
    && JSON.parse(marks[0].opts.body).dashboard === DASH_ID,
    marks.length ? marks[0].opts.body : '');
  check('catchup: the callout is gone, the panel says «Published»',
    panelText().indexOf('Publish to the platform') === -1
    && panelText().indexOf('Published') !== -1, panelText().slice(0, 300));

  // The agent rejected the mark: the report is ALREADY published, and that is
  // said out loud — otherwise the person would publish the same numbers twice.
  markPublishedOk = false;
  localRunsPayload = { ok: true, runs: [localRun()] };
  boot();
  await settle();
  findAll(root, n => n.tagName === 'BUTTON' && n.textContent === 'Publish to the platform')[0]
    .dispatchEvent({ type: 'click' });
  await settle(60);
  check('catchup: a failed mark does not undo the publish and is named out loud',
    panelText().indexOf('Published') !== -1
    && panelText().indexOf('it will offer the catch-up again') !== -1, panelText().slice(0, 600));
  markPublishedOk = true;
  localRunsPayload = { ok: true, runs: [] };

  console.log();
  if (FAILURES.length) { console.log(FAILURES.length + ' FAILED: ' + FAILURES.join(', ')); process.exit(1); }
  console.log('ALL PASS');
  process.exit(0);
})();
