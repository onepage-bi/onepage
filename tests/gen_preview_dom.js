/* The «what we will read» screen before Run (static/js/gen-engine.js).
 *
 * Same technique as tests/gen_engine_dom.js: no jsdom, a minimal DOM,
 * stubbed fetch/Worker/IndexedDB and the module loaded with an indirect eval.
 *
 * What is pinned:
 *   * opening the panel does NOT ask for the rules — the list and the rules
 *     arrive on a button press (the /d/{id} hot path stays untouched);
 *   * the screen lists the files of ALL connected folders with size, date and
 *     the mask each file matched;
 *   * a file that matched no mask is named out loud — that is exactly the
 *     «someone renamed it wrong» case the engine otherwise keeps silent about;
 *   * a server-side exclusion rule is visible on screen BEFORE the run — the
 *     screen shows what the run will do;
 *   * an unticked box is saved as an exclusion mask in input_overrides
 *     (PUT with the version received from GET) and the same run applies it;
 *   * a 409 (someone else edited the rules) never silently overwrites their decision;
 *   * an unmatched required mask is named before the start.
 *
 * Run: node tests/gen_preview_dom.js
 */
'use strict';
require('./_i18n_stub');   // global t/tn — strictly before the DOM stub
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
    this.type = '';
    this.title = '';
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
  // As in a real browser: a reflected attribute (title) can be set as a
  // property and read with getAttribute. Without this the check «did we come
  // back to the same row» would silently check nothing.
  getAttribute(k) {
    if (k in this.attrs) return this.attrs[k];
    return (this[k] !== undefined && this[k] !== '') ? this[k] : null;
  }
  matches(sel) {
    return String(sel).split(',').map(x => x.trim()).filter(Boolean).some(part =>
      part[0] === '.' ? this._class.contains(part.slice(1))
                      : this.tagName === part.toUpperCase());
  }
  querySelectorAll(sel) { return findAll(this, n => n.matches && n.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) {
    let n = this;
    while (n) { if (n.matches && n.matches(sel)) return n; n = n.parent; }
    return null;
  }
  contains(n) { while (n) { if (n === this) return true; n = n.parent; } return false; }
  focus() { ACTIVE = this; }
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

let ACTIVE = null;
const byIdMap = {};
const document = {
  createElement: t => new El(t),
  get activeElement() { return ACTIVE; },
  getElementById: id => byIdMap[id] || null,
  createTextNode: v => ({ nodeType: 3, text: String(v), children: [] }),
};

// ── what the engine is loaded against ────────────────────────────────
const DASH_ID = 'dash-prev';
const MTIME = Date.UTC(2026, 8, 30, 9, 0, 0);

const MANIFEST_RESPONSE = {
  generator: { id: 7, name: 'ops-rdc-health' },
  version_number: 37,
  checksum_sha256: 'deadbeef',
  bundle_url: `/api/dashboards/${DASH_ID}/generator/bundle?v=37`,
  run_lock_ttl_s: 600,
  source_rules: {},
  manifest: {
    name: 'ops-rdc-health', engine: 'browser', write_back: false,
    data_scale: { max_files: 500, max_total_bytes: 500e6, max_file_bytes: 35e6 },
    aux_inputs: [],
  },
  inputs: [
    { match: 'OPS_*.xlsx', role: 'data', required: true, sheets: [], source: 'folder', table: null, from_required_files: false },
    { match: 'Tower_*.xlsx', role: 'data', required: false, sheets: [], source: 'folder', table: null, from_required_files: false },
  ],
  checks: [],
  force_deps: ['python-calamine'],
  reference: [],
};

// Rules the server returns: one folder, one rule, version 2.
let RULES_RESPONSE = { generator_id: 7, folders: {
  OPS: { source_id: 5, version: 2, exclude_rules: ['*_old.xlsx'] },
} };
let putStatus = 200;

const fetchCalls = [];
function jsonRes(body, status) {
  return { ok: (status || 200) < 400, status: status || 200,
           json: async () => body, text: async () => JSON.stringify(body) };
}
global.fetch = async (url, opts) => {
  const u = String(url);
  fetchCalls.push({ url: u, opts: opts || null });
  if (u.endsWith('/generator/manifest')) return jsonRes(MANIFEST_RESPONSE);
  if (u.endsWith('/generator/input-rules') && (!opts || !opts.method || opts.method === 'GET')) {
    return jsonRes(RULES_RESPONSE);
  }
  if (u.endsWith('/generator/input-rules') && opts && opts.method === 'PUT') {
    const body = JSON.parse(opts.body);
    if (putStatus === 409) {
      return jsonRes({ detail: { detail: 'version conflict',
        current: { exclude_rules: ['*_old.xlsx', 'someone_else.xlsx'], version: 9 } } }, 409);
    }
    const known = RULES_RESPONSE.folders[body.folder];
    const next = { source_id: (known && known.source_id) || 11,
                   version: ((known && known.version) || 0) + 1,
                   exclude_rules: body.exclude_rules };
    RULES_RESPONSE.folders[body.folder] = next;
    return jsonRes(Object.assign({ ok: true, folder: body.folder }, next));
  }
  if (u.endsWith('/generator/runs') && opts && opts.method === 'POST') {
    return jsonRes({ run_id: 90, bundle_url: MANIFEST_RESPONSE.bundle_url,
      heartbeat_every_s: 60, observation_ids: {}, last_known: {} }, 201);
  }
  return jsonRes({});
};

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

function fileEntry(name, size) {
  return { kind: 'file', name: name,
    getFile: async () => ({ size: size, lastModified: MTIME,
      arrayBuffer: async () => new ArrayBuffer(8) }) };
}
// A folder of exactly the shape the owner complained about: the right file, its
// «(1)» copy, last month under a rule, a draft with a wrong name, and junk.
const FILES = [
  fileEntry('OPS_RDC_Health.xlsx', 2 * 1024 * 1024),
  fileEntry('OPS_RDC_Health (1).xlsx', 2 * 1024 * 1024),
  fileEntry('OPS_old.xlsx', 1024 * 1024),
  fileEntry('tower report 09.xlsx', 3 * 1024 * 1024),
  fileEntry('~$OPS_RDC_Health.xlsx', 162),
  fileEntry('notes.txt', 10),
];
const dirHandle = {
  name: 'OPS', kind: 'directory',
  queryPermission: async () => 'granted',
  requestPermission: async () => 'granted',
  values: async function* () { for (const f of FILES) yield f; },
};
const idbStore = { [DASH_ID]: dirHandle };
global.indexedDB = {
  open() {
    const req = {};
    setTimeout(() => {
      req.result = {
        transaction() {
          const tr = {
            objectStore() {
              return {
                get(key) { const r = {}; setTimeout(() => { r.result = idbStore[key]; r.onsuccess && r.onsuccess(); }, 0); return r; },
                put(val, key) { idbStore[key] = val; setTimeout(() => { tr.oncomplete && tr.oncomplete(); }, 0); },
              };
            },
          };
          return tr;
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
global.Worker = WorkerStub;
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
  lastWorker = null;
  (0, eval)(src);
}

const tick = () => new Promise(r => setTimeout(r, 0));
async function settle(n) { for (let i = 0; i < (n || 14); i++) await tick(); }

function button(label) {
  return findAll(root, n => n.tagName === 'BUTTON' && n.textContent === label)[0];
}
// A file row is a label whose text starts with the file name. The «allow
// writing results» label never contains a file name.
function fileRow(name) {
  return findAll(root, n => n.tagName === 'LABEL' && textOf(n).indexOf(name) === 0)[0];
}
function rowCheckbox(name) {
  const r = fileRow(name);
  return r && findAll(r, n => n.tagName === 'INPUT' && n.type === 'checkbox')[0];
}
function panelText() { return textOf(root); }
// A row of a file that matched no mask: a plain DIV holding a picker of the
// declared inputs.
function aliasRow(name) {
  return findAll(root, n => n.tagName === 'DIV' && n.classList.contains('gp-row')
    && textOf(n).indexOf(name) !== -1)[0];
}
function assignButton(name) {
  const r = aliasRow(name);
  return r && findAll(r, n => n.tagName === 'BUTTON')[0];
}
function aliasSelect(name) {
  // A file row without a mask: .gp-row with an exclamation mark, the name and
  // an input picker. Match by the name occurring anywhere, not at the start:
  // the status icon comes first in the row.
  const row = findAll(root, n => n.tagName === 'DIV'
    && n.classList.contains('gp-row')
    && textOf(n).indexOf(name) !== -1
    && findAll(n, x => x.tagName === 'SELECT').length)[0];
  return row && findAll(row, n => n.tagName === 'SELECT')[0];
}
function lastPut() {
  const puts = fetchCalls.filter(f => f.opts && f.opts.method === 'PUT');
  return puts.length ? JSON.parse(puts[puts.length - 1].opts.body) : null;
}
function lastRunPost() {
  const posts = fetchCalls.filter(f => f.url.endsWith('/generator/runs') && f.opts && f.opts.method === 'POST');
  return posts.length ? JSON.parse(posts[posts.length - 1].opts.body) : null;
}

(async () => {
  boot();
  await settle();

  // ── hot path: nobody asks for the rules before the press ───────────
  check('boot: the panel does not fetch the rules when the report opens',
    fetchCalls.filter(f => f.url.indexOf('/input-rules') !== -1).length === 0,
    JSON.stringify(fetchCalls.map(f => f.url)));
  check('boot: the «What we will read…» button exists and is enabled once the folder is ready',
    button('What we will read…') && button('What we will read…').disabled === false);

  button('What we will read…').dispatchEvent({ type: 'click' });
  await settle(25);

  check('open: the rules are requested once',
    fetchCalls.filter(f => f.url.indexOf('/input-rules') !== -1).length === 1,
    JSON.stringify(fetchCalls.map(f => f.url)));

  // ── list ───────────────────────────────────────────────────────────
  check('list: a file that matched a mask is shown with it',
    fileRow('OPS_RDC_Health.xlsx') && textOf(fileRow('OPS_RDC_Health.xlsx')).indexOf('pattern OPS_*.xlsx') !== -1,
    fileRow('OPS_RDC_Health.xlsx') && textOf(fileRow('OPS_RDC_Health.xlsx')));
  check('list: the file has a size and a date',
    textOf(fileRow('OPS_RDC_Health.xlsx')).indexOf('2.0 MB') !== -1
    && textOf(fileRow('OPS_RDC_Health.xlsx')).indexOf('30/09/2026') !== -1,
    textOf(fileRow('OPS_RDC_Health.xlsx')));
  check('list: the «(1)» copy is read too and is visible',
    rowCheckbox('OPS_RDC_Health (1).xlsx') && rowCheckbox('OPS_RDC_Health (1).xlsx').checked === true);
  check('list: the server rule is visible BEFORE the run — the file is unticked',
    rowCheckbox('OPS_old.xlsx') && rowCheckbox('OPS_old.xlsx').checked === false
    && textOf(fileRow('OPS_old.xlsx')).indexOf('rule «*_old.xlsx»') !== -1,
    fileRow('OPS_old.xlsx') && textOf(fileRow('OPS_old.xlsx')));
  check('list: a file matching no mask is named out loud, without a checkbox',
    panelText().indexOf('tower report 09.xlsx') !== -1
    && panelText().indexOf('matched no pattern') !== -1
    && !fileRow('tower report 09.xlsx'),
    panelText().slice(0, 400));
  check('list: the total counts only what will be read',
    panelText().indexOf('2 files · 4.0 MB') !== -1, panelText());

  // ── the second problem: the name does not match a declared input ───
  check('masks: a mask nothing matched is named before the start',
    panelText().indexOf('No file matched the pattern Tower_*.xlsx') !== -1,
    panelText());
  check('masks: it says the cause may be a rename',
    panelText().indexOf('it may have been renamed') !== -1, panelText());

  // ── untick the extras and remember────────────────────────────────────
  const cb = rowCheckbox('OPS_RDC_Health (1).xlsx');
  cb.checked = false;
  cb.dispatchEvent({ type: 'change' });
  check('choice: an unticked file is immediately marked «not read»',
    panelText().indexOf('OPS_RDC_Health (1).xlsx') !== -1
    && rowCheckbox('OPS_RDC_Health (1).xlsx').checked === false);
  check('choice: the total is recomputed without it',
    panelText().indexOf('1 file · 2.0 MB') !== -1, panelText());

  button('Remember this choice').dispatchEvent({ type: 'click' });
  await settle(20);
  const put = lastPut();
  check('save: the rule went to the server as a file name, on top of the existing one',
    put && put.folder === 'OPS'
    && JSON.stringify(put.exclude_rules) === JSON.stringify(['*_old.xlsx', 'OPS_RDC_Health (1).xlsx']),
    JSON.stringify(put));
  check('save: the version is taken from GET (optimistic locking)',
    put && put.version === 2, JSON.stringify(put));

  // ── the run reads exactly what the screen showed ───────────────────
  button('Run').dispatchEvent({ type: 'click' });
  await settle(25);
  const body = lastRunPost();
  const names = body ? body.input_manifest.files.map(f => f.name) : [];
  check('run: reads exactly what the screen showed',
    JSON.stringify(names) === JSON.stringify(['OPS_RDC_Health.xlsx']), JSON.stringify(names));
  check('run: the unticked file is listed in skipped as excluded by a rule',
    body && body.input_manifest.skipped.some(s => s.name === 'OPS_RDC_Health (1).xlsx'
      && String(s.reason).indexOf('excluded by rule') === 0),
    JSON.stringify(body && body.input_manifest.skipped));

  // ── 409: someone else's decision is not silently overwritten──────────────────────
  putStatus = 409;
  boot();
  await settle();
  button('What we will read…').dispatchEvent({ type: 'click' });
  await settle(25);
  const cb2 = rowCheckbox('OPS_RDC_Health.xlsx');
  cb2.checked = false;
  cb2.dispatchEvent({ type: 'change' });
  button('Remember this choice').dispatchEvent({ type: 'click' });
  await settle(25);
  check('409: the person is told the rules have changed',
    panelText().indexOf('Someone changed') !== -1, panelText().slice(0, 300));
  check('409: the fresh list is shown, the other decision is not overwritten',
    rowCheckbox('OPS_RDC_Health.xlsx') && rowCheckbox('OPS_RDC_Health.xlsx').checked === true,
    String(rowCheckbox('OPS_RDC_Health.xlsx') && rowCheckbox('OPS_RDC_Health.xlsx').checked));

  // ── required mask: the screen predicts the run's refusal ──────────
  // The owner's second problem: the file exists but its name is wrong — the
  // engine silently skips it as «not declared», and the run fails on the
  // required input. The screen must say so BEFORE Run is pressed.
  putStatus = 200;
  MANIFEST_RESPONSE.inputs[1].required = true;
  boot();
  await settle();
  button('What we will read…').dispatchEvent({ type: 'click' });
  await settle(25);
  check('required mask: the screen says the run will not start',
    panelText().indexOf('No file matched the required pattern Tower_*.xlsx — the run will not start') !== -1,
    panelText());
  const beforeRun = fetchCalls.length;
  button('Run').dispatchEvent({ type: 'click' });
  await settle(25);
  check('required mask: the run really did not start — the screen told the truth',
    !fetchCalls.slice(beforeRun).some(f => f.url.endsWith('/generator/runs')),
    JSON.stringify(fetchCalls.slice(beforeRun).map(f => f.url)));

  // ── «clear all» in a folder ──────────────────────────────────────
  // The owner on a folder with 171 exports: unticking one by one is impossible.
  // The rule is a file name, one per file, NOT the mask «*»: that would also
  // switch off files that appear tomorrow, and globmatch rejects it.
  putStatus = 200;
  RULES_RESPONSE = { generator_id: 7, folders: {
    OPS: { source_id: 5, version: 2, exclude_rules: [], file_aliases: {} },
  } };
  boot();
  await settle();
  button('What we will read…').dispatchEvent({ type: 'click' });
  await settle(25);
  check('folder: there is a «clear all» command', !!button('clear all'), panelText().slice(0, 200));
  check('folder: while everything is ticked, «restore all» is not offered',
    !button('restore all'), panelText().slice(0, 200));

  button('clear all').dispatchEvent({ type: 'click' });
  await settle(15);
  check('folder: after the command no tick is left',
    ['OPS_RDC_Health.xlsx', 'OPS_RDC_Health (1).xlsx', 'OPS_old.xlsx']
      .every(n => rowCheckbox(n) && rowCheckbox(n).checked === false),
    panelText().slice(0, 300));
  // The denominator is how many files the folder CAN give the run at all: the
  // Excel temp file and the .txt are not in it, otherwise «4 of 6» is never reachable.
  check('folder: the folder counter is zero and does not count junk',
    panelText().indexOf('0 of 4') !== -1, panelText().slice(0, 300));
  check('folder: «restore all» is offered now', !!button('restore all'));

  button('Remember this choice').dispatchEvent({ type: 'click' });
  await settle(25);
  const bulk = lastPut();
  check('folder: file NAMES were sent, not the mask «*»',
    bulk && bulk.exclude_rules.indexOf('*') === -1
      && bulk.exclude_rules.indexOf('OPS_RDC_Health.xlsx') !== -1
      && bulk.exclude_rules.length === 3,
    JSON.stringify(bulk && bulk.exclude_rules));

  button('restore all').dispatchEvent({ type: 'click' });
  await settle(15);
  check('folder: «restore all» brings the ticks back',
    rowCheckbox('OPS_RDC_Health.xlsx') && rowCheckbox('OPS_RDC_Health.xlsx').checked === true,
    panelText().slice(0, 300));

  // ── final review: «selected only» ────────────────────────────────
  // «I want to see in the end what I have clicked» and «let me remove all
  // the extras» are one request: before Run a person wants to see exactly
  // what goes into the run, and nothing else.
  putStatus = 200;
  RULES_RESPONSE = { generator_id: 7, folders: {
    OPS: { source_id: 5, version: 2, exclude_rules: ['*_old.xlsx'], file_aliases: {} },
  } };
  boot();
  await settle();
  button('What we will read…').dispatchEvent({ type: 'click' });
  await settle(25);
  check('review: by default everything is visible, unticked included',
    panelText().indexOf('OPS_old.xlsx') !== -1 && panelText().indexOf('notes.txt') !== -1,
    panelText().slice(0, 300));

  button('Selected only').dispatchEvent({ type: 'click' });
  await settle(15);
  check('review: what a rule unticked is hidden',
    panelText().indexOf('OPS_old.xlsx') === -1, panelText().slice(0, 400));
  check('review: junk and unrecognised names are hidden',
    panelText().indexOf('notes.txt') === -1
    && panelText().indexOf('tower report 09.xlsx') === -1,
    panelText().slice(0, 400));
  check('review: what will be read remains',
    panelText().indexOf('OPS_RDC_Health.xlsx') !== -1, panelText().slice(0, 400));
  check('review: the folder counter still covers the whole folder, not what is shown',
    panelText().indexOf('2 of 4') !== -1, panelText().slice(0, 400));

  button('All files').dispatchEvent({ type: 'click' });
  await settle(15);
  check('review: back to the full list',
    panelText().indexOf('OPS_old.xlsx') !== -1, panelText().slice(0, 300));

  // An empty selection must say so in words, not with blank space.
  button('clear all').dispatchEvent({ type: 'click' });
  await settle(15);
  button('Selected only').dispatchEvent({ type: 'click' });
  await settle(15);
  check('review: an empty selection is named out loud',
    panelText().indexOf('Nothing is selected') !== -1, panelText().slice(0, 300));

  // ── scroll position survives a checkbox click ─────────────────────
  // On 172 files (the owner's real case) re-rendering the list threw the
  // person back to the top after every click. The re-render stays full — a
  // rule is wider than one name and neighbouring rows must update — but the
  // screen must put the person back where they were.
  putStatus = 200;
  RULES_RESPONSE = { generator_id: 7, folders: {
    OPS: { source_id: 5, version: 2, exclude_rules: [], file_aliases: {} },
  } };
  boot();
  await settle();
  button('What we will read…').dispatchEvent({ type: 'click' });
  await settle(25);
  const listBefore = findAll(root, n => n.classList && n.classList.contains('gp-list'))[0];
  check('scroll: the list is found', !!listBefore);
  listBefore.scrollTop = 420;
  const cbScroll = rowCheckbox('OPS_RDC_Health.xlsx');
  cbScroll.focus();
  cbScroll.checked = false;
  cbScroll.dispatchEvent({ type: 'change' });
  await settle(15);
  const listAfter = findAll(root, n => n.classList && n.classList.contains('gp-list'))[0];
  check('scroll: after the click the list is re-rendered (a new node)',
    listAfter && listAfter !== listBefore);
  check('scroll: the person was not thrown back to the top',
    listAfter && listAfter.scrollTop === 420, String(listAfter && listAfter.scrollTop));
  check('scroll: focus returned to the same row',
    document.activeElement
      && document.activeElement.closest('.gp-row')
      && document.activeElement.closest('.gp-row').getAttribute('title') === 'OPS_RDC_Health.xlsx',
    document.activeElement && document.activeElement.closest('.gp-row')
      && document.activeElement.closest('.gp-row').getAttribute('title'));

  // ── «this file IS that input» ────────────────────────────────────
  // The second problem in full: the right file, the wrong name. A person
  // should not rename someone else's file on a network share — they name the
  // input right here, and the run must read the file under that mask.
  putStatus = 200;
  MANIFEST_RESPONSE.inputs[1].required = true;
  RULES_RESPONSE = { generator_id: 7, folders: {
    OPS: { source_id: 5, version: 2, exclude_rules: [], file_aliases: {} },
  } };
  boot();
  await settle();
  button('What we will read…').dispatchEvent({ type: 'click' });
  await settle(25);

  // The input list does not hang on every file: first a button, and only
  // then the picker. Otherwise a folder with a dozen unrecognised names turns
  // into a wall of drop-downs.
  check('alias: no drop-down before the press',
    !aliasSelect('tower report 09.xlsx'), panelText().slice(0, 300));
  const assign = assignButton('tower report 09.xlsx');
  check('alias: a file without a mask has a «read as…» button',
    !!assign && assign.textContent === 'read as…',
    assign && assign.textContent);
  assign.dispatchEvent({ type: 'click' });
  await settle(10);
  const sel = aliasSelect('tower report 09.xlsx');
  check('alias: the button opens the input picker', !!sel,
    panelText().slice(0, 400));
  check('alias: the picker lists the declared inputs',
    sel && findAll(sel, n => n.tagName === 'OPTION').some(o => o.value === 'Tower_*.xlsx'),
    sel && findAll(sel, n => n.tagName === 'OPTION').map(o => o.value).join(','));

  sel.value = 'Tower_*.xlsx';
  sel.dispatchEvent({ type: 'change' });
  await settle(15);
  check('alias: the required mask no longer counts as empty',
    panelText().indexOf('the run will not start') === -1, panelText().slice(0, 400));

  button('Remember this choice').dispatchEvent({ type: 'click' });
  await settle(25);
  const putBody = lastPut();
  check('alias: goes to the server under its mask',
    putBody && putBody.file_aliases
      && JSON.stringify(putBody.file_aliases['Tower_*.xlsx']) === JSON.stringify(['tower report 09.xlsx']),
    JSON.stringify(putBody));

  const beforeAlias = fetchCalls.length;
  button('Run').dispatchEvent({ type: 'click' });
  await settle(30);
  check('alias: the run started — the required input is covered by the named file',
    fetchCalls.slice(beforeAlias).some(f => f.url.endsWith('/generator/runs')),
    JSON.stringify(fetchCalls.slice(beforeAlias).map(f => f.url)));
  const aliasBody = lastRunPost();
  const named = aliasBody && aliasBody.input_manifest.files
    .filter(f => f.name === 'tower report 09.xlsx')[0];
  check('alias: the file is read and tagged with that very mask',
    named && named.matched_input === 'Tower_*.xlsx',
    JSON.stringify(aliasBody && aliasBody.input_manifest.files));

  console.log();
  if (FAILURES.length) { console.log(FAILURES.length + ' FAILED: ' + FAILURES.join(', ')); process.exit(1); }
  console.log('ALL PASS');
  process.exit(0);
})();
