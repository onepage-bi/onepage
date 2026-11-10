/* Headless harness for static/js/reference.js.
 *
 * Same idiom as tests/catalog_dom.js: no jsdom — the smallest DOM the
 * script actually touches (elements with classes/attrs/dataset/form
 * values/listeners) plus a stubbed `fetch` and a stubbed `confirm`, so both
 * the list page and the detail page are driven and inspected without a
 * server or a browser.
 *
 * Run: node tests/reference_dom.js   (prints PASS/FAIL lines + ALL PASS)
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

// ── DOM stub (same shape as tests/catalog_dom.js) ───────────────────────
class ClassList {
  constructor() { this.set = new Set(); }
  add() { for (const c of arguments) if (c) this.set.add(c); }
  remove() { for (const c of arguments) this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  toggle(c, on) { if (on === undefined) on = !this.set.has(c); if (on) this.set.add(c); else this.set.delete(c); return on; }
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
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.files = null;
  }
  get classList() { return this._class; }
  set className(v) { this._class.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return this._class.value; }
  set textContent(v) { this.children = []; this._text = v === '' ? '' : String(v); }
  get textContent() { return this._text; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter(x => x !== c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[camel(k.slice(5))] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    (this.listeners[ev.type] || []).forEach(f => f.call(this, ev));
    return true;
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
function camel(s) { return s.replace(/-([a-z])/g, (m, c) => c.toUpperCase()); }
function walk(n, fn) { n.children.forEach(c => { fn(c); walk(c, fn); }); }
function collectText(node) {
  if (!node) return '';
  let out = node._text || '';
  (node.children || []).forEach(c => { out += ' ' + collectText(c); });
  return out;
}
function findAll(node, pred) {
  const out = [];
  walk(node, n => { if (pred(n)) out.push(n); });
  return out;
}

let byIdMap = {};
const documentStub = {
  createElement: t => new El(t),
  // reference.js appends bare text nodes between inline elements (a badge
  // after a generator name, the "template" link after a row count).
  createTextNode: v => { const n = new El('#text'); n._text = String(v); return n; },
  getElementById: id => byIdMap[id] || null,
  addEventListener: () => {},
  activeElement: null,
};

function mk(tag, id, attrs) {
  const e = new El(tag);
  Object.keys(attrs || {}).forEach(k => e.setAttribute(k, attrs[k]));
  if (id) { e.attrs.id = id; byIdMap[id] = e; }
  return e;
}

function click(elx) { elx.dispatchEvent({ type: 'click', target: elx }); }
function input(elx) { elx.dispatchEvent({ type: 'input', target: elx }); }

const src = fs.readFileSync(path.join(ROOT, 'static/js/reference.js'), 'utf8');

// ═════════════════════════════════════════════════════════════════════
// Fixture data
// ═════════════════════════════════════════════════════════════════════
const LIST_TABLES = [
  {
    slug: 'nwd_bv_map', title: 'NWD BV → code and name', key_column: 'BV',
    columns: [{ name: 'BV', type: 'text' }, { name: 'Code', type: 'text' }],
    row_count: 42, current_version: 3, allow_pin: true,
    responsible: { id: 5, display_name: 'Ivanov I.I.' },
    consumers: 2, updated_at: '2026-08-20T10:00:00',
    consumer_generators: [{ id: 1, name: 'slreport' }],
    consumer_dashboards: [{ id: 'd1', title: 'SL Report' }],
  },
  // Created from another generator's manifest, never imported into: the row
  // must offer the empty template instead of a bare «0».
  {
    slug: 'rdc_units', title: 'rdc_units', key_column: 'unit',
    columns: [{ name: 'unit', type: 'text' }, { name: 'name', type: 'text' }],
    row_count: 0, current_version: 0, allow_pin: false,
    responsible: null, consumers: 1, updated_at: '2026-08-26T10:00:00',
    consumer_generators: [{ id: 2, name: 'ops-rdc-health' }],
    consumer_dashboards: [{ id: 'd7', title: 'RDC Health' }],
  },
];

const DETAIL = {
  slug: 'nwd_bv_map', title: 'NWD BV → code and name', description: 'pilot mapping',
  key_column: 'BV', columns: [{ name: 'BV', type: 'text' }, { name: 'Code', type: 'text' }],
  current_version: 3, allow_pin: true,
  responsible: { id: 5, display_name: 'Ivanov I.I.' },
  rows: [['BV-1', 'K1'], ['BV-2', 'K2']],
  row_offset: 0, row_limit: 500, row_total: 2,
  versions: [
    { number: 3, row_count: 2, summary: { added: 1, changed: 0, removed: 0 }, note: 'fix', created_by: 'admin1', created_at: '2026-08-20T10:00:00' },
    { number: 2, row_count: 1, summary: { added: 1, changed: 0, removed: 0 }, note: null, created_by: 'admin1', created_at: '2026-08-19T10:00:00' },
  ],
  consumers: [
    { dashboard_id: 'd1', title: 'SL Report', generator_id: 1, generator_name: 'slreport',
      last_run: { run_id: 9, version_number: 2, applied: true, finished_at: '2026-08-19T10:00:00' } },
    { dashboard_id: 'd2', title: 'Never Run Report', generator_id: 1, generator_name: 'slreport', last_run: null },
    // Reads the dictionary only in "ask in plain language" — never in a run.
    { dashboard_id: 'd3', title: 'Chat Only', generator_id: 3, generator_name: 'chatgen',
      via: ['chat'], last_run: null },
  ],
  created_at: '2026-08-01T10:00:00', updated_at: '2026-08-20T10:00:00',
  can_write: true,
};

const DQ_FINDINGS = {
  findings: [
    {
      kind: 'unmapped_values', severity: 'warn', source: 'table:nwd_bv_map',
      generator: 'slreport', dashboard_ids: ['d1'],
      message: 'New values in reference table nwd_bv_map',
      detail: { table: 'nwd_bv_map', values: ['BV-9'], template_url: '/api/reference/nwd_bv_map/template?unmapped=1&keys=BV-9' },
    },
    // A finding for a DIFFERENT table must be filtered out client-side.
    {
      kind: 'unmapped_values', severity: 'warn', source: 'table:other_tbl',
      generator: 'other-gen', dashboard_ids: ['d3'],
      message: 'New values in reference table other_tbl',
      detail: { table: 'other_tbl', values: ['X'] },
    },
    { kind: 'schema_missing_pattern', severity: 'warn', source: 'x', generator: 'g', detail: {} },
  ],
};

// ═════════════════════════════════════════════════════════════════════
// Part 1 — list page (#ref-root)
// ═════════════════════════════════════════════════════════════════════
function buildListDom() {
  byIdMap = {};
  const root = mk('div', 'ref-root', { 'data-tables': JSON.stringify(LIST_TABLES), 'data-can-create': '1' });
  const createBtn = mk('button', 'ref-create-btn');
  const tbody = mk('tbody', 'ref-tbody');
  const emptyEl = mk('div', 'ref-empty'); emptyEl.hidden = true;
  const filterSel = mk('select', 'ref-filter-gen');
  const filterClear = mk('button', 'ref-filter-clear'); filterClear.hidden = true;
  const filterCount = mk('span', 'ref-filter-count');
  const drawer = mk('aside', 'ref-drawer'); drawer.hidden = true;
  const scrim = mk('div', 'ref-scrim'); scrim.hidden = true;
  mk('button', 'ref-drawer-close');

  const slugIn = mk('input', 'ref-c-slug');
  const titleIn = mk('input', 'ref-c-title');
  const descIn = mk('textarea', 'ref-c-desc');
  const respIn = mk('input', 'ref-c-resp');
  const pinCk = mk('input', 'ref-c-pin', { type: 'checkbox' });
  const modeManual = mk('input', 'ref-c-mode-manual', { type: 'radio' }); modeManual.checked = true;
  const modeGen = mk('input', 'ref-c-mode-gen', { type: 'radio' });
  mk('div', 'ref-c-manual-box');
  mk('div', 'ref-c-gen-box').hidden = true;
  mk('div', 'ref-c-columns');
  mk('button', 'ref-c-add-col');
  mk('select', 'ref-c-gen-sel');
  const submitBtn = mk('button', 'ref-c-submit');
  const statusEl = mk('div', 'ref-c-status');

  return { root, createBtn, tbody, emptyEl, drawer, scrim, slugIn, titleIn, descIn, respIn,
    pinCk, submitBtn, statusEl, filterSel, filterClear, filterCount };
}

const listFetchCalls = [];
global.fetch = async (url, opts) => {
  listFetchCalls.push({ url, opts, body: opts && opts.body && typeof opts.body === 'string' ? JSON.parse(opts.body) : null });
  if (url === '/api/generators') return { ok: true, status: 200, json: async () => [{ id: 1, name: 'slreport' }] };
  if (url === '/api/reference' && (!opts || opts.method !== 'POST')) return { ok: true, status: 200, json: async () => LIST_TABLES };
  if (url === '/api/reference' && opts && opts.method === 'POST') {
    return { ok: true, status: 201, json: async () => ({ slug: 'new_tbl', title: 'x' }) };
  }
  return { ok: true, status: 200, json: async () => ({}) };
};

global.document = documentStub;
// The filter reads the query string, then localStorage, and writes both back
// through history.replaceState — all three are stubbed the same minimal way
// the rest of this harness stubs the DOM.
const storeMap = {};
global.localStorage = {
  getItem: k => (k in storeMap ? storeMap[k] : null),
  setItem: (k, v) => { storeMap[k] = String(v); },
  removeItem: k => { delete storeMap[k]; },
};
const replaced = [];
global.window = {
  OP_TOAST: null,
  history: { replaceState: (a, b, url) => replaced.push(url) },
  confirm: () => global.confirm(),
  localStorage: global.localStorage,
};
global.history = global.window.history;
global.location = { href: 'http://x/reference', pathname: '/reference', search: '' };

let L = buildListDom();
(0, eval)(src);

check('list: rows render from baked data-tables', L.tbody.children.length === 2,
  String(L.tbody.children.length));
const row0 = L.tbody.children[0];
check('list: row shows the slug', collectText(row0).includes('nwd_bv_map'), collectText(row0));
check('list: row shows the responsible display_name', collectText(row0).includes('Ivanov'), collectText(row0));
check('list: row shows the version', collectText(row0).includes('v3'), collectText(row0));
check('list: empty state hidden when tables exist', L.emptyEl.hidden === true);

// ── the empty (manifest-created) table offers its template ────────────
const rowEmpty = L.tbody.children[1];
check('list: a table with no version yet links to the empty template',
  collectText(rowEmpty).includes('template'), collectText(rowEmpty));

// ── "Reference tables of this report": filter by generator ───────────────────
check('list: the filter select lists every consuming generator, plus "All"',
  L.filterSel.children.map(o => o.attrs.value !== undefined ? o.attrs.value : o.value)
    .join('|') === '|ops-rdc-health|slreport',
  L.filterSel.children.map(o => collectText(o)).join('|'));
check('list: nothing is filtered out until a generator is chosen',
  L.filterClear.hidden === true && L.filterCount.textContent === '',
  L.filterCount.textContent);

L.filterSel.value = 'ops-rdc-health';
L.filterSel.dispatchEvent({ type: 'change', target: L.filterSel });
check('list: choosing a generator narrows the table to its dictionaries',
  L.tbody.children.length === 1 && collectText(L.tbody.children[0]).includes('rdc_units'),
  String(L.tbody.children.length));
check('list: the filter says how many of how many are shown',
  L.filterCount.textContent === '1 of 2', L.filterCount.textContent);
check('list: the filter is reflected in the URL so the view is linkable',
  replaced[replaced.length - 1] === '/reference?generator=ops-rdc-health',
  String(replaced[replaced.length - 1]));
check('list: the filter is remembered in localStorage',
  JSON.parse(storeMap['op-ref-filter'] || '{}').generator === 'ops-rdc-health',
  storeMap['op-ref-filter']);

click(L.filterClear);
check('list: "Reset" restores every table', L.tbody.children.length === 2,
  String(L.tbody.children.length));
check('list: clearing drops the query and the stored filter',
  replaced[replaced.length - 1] === '/reference' && !('op-ref-filter' in storeMap),
  String(replaced[replaced.length - 1]));

// A link with ?generator=… must win over whatever this browser remembered.
storeMap['op-ref-filter'] = JSON.stringify({ generator: 'slreport' });
global.location = { href: 'http://x/reference?generator=ops-rdc-health',
  pathname: '/reference', search: '?generator=ops-rdc-health' };
L = buildListDom();
(0, eval)(src);
check('list: ?generator= in the URL beats the remembered filter',
  L.tbody.children.length === 1 && collectText(L.tbody.children[0]).includes('rdc_units'),
  collectText(L.tbody));
global.location = { href: 'http://x/reference', pathname: '/reference', search: '' };
delete storeMap['op-ref-filter'];
L = buildListDom();
(0, eval)(src);

// Create submit — validation gate before any network call.
click(L.submitBtn);
check('list: submit with empty slug/title does not create (no reference POST yet)',
  !listFetchCalls.some(c => c.url === '/api/reference' && c.opts && c.opts.method === 'POST'));
check('list: submit with empty fields shows a status message', L.statusEl.textContent.length > 0, L.statusEl.textContent);

L.slugIn.value = 'new_tbl';
L.titleIn.value = 'New table';
// two blank manual column rows exist by default -> still under 2 named columns
click(L.submitBtn);
check('list: submit with unnamed columns still refuses (needs >=2 named columns)',
  !listFetchCalls.some(c => c.url === '/api/reference' && c.opts && c.opts.method === 'POST'));

const colInputs = findAll(byIdMap['ref-c-columns'], n => n.tagName === 'INPUT');
if (colInputs[0]) colInputs[0].value = 'K';
if (colInputs[1]) colInputs[1].value = 'V';
click(L.submitBtn);

function afterCreate() {
  const postCall = listFetchCalls.find(c => c.url === '/api/reference' && c.opts && c.opts.method === 'POST');
  check('list: create posts to /api/reference', !!postCall);
  check('list: create POST uses application/json',
    !!postCall && postCall.opts.headers['Content-Type'] === 'application/json');
  check('list: create POST body carries slug/title/columns',
    !!postCall && postCall.body.slug === 'new_tbl' && postCall.body.title === 'New table'
    && Array.isArray(postCall.body.columns) && postCall.body.columns.length >= 2,
    postCall && JSON.stringify(postCall.body));
  check('list: on success the page navigates to the new table',
    global.location.href === '/reference/new_tbl', global.location.href);

  // ═══════════════════════════════════════════════════════════════════
  // Part 2 — detail page (#refd-root)
  // ═══════════════════════════════════════════════════════════════════
  runDetailPart();
}

// The create handler awaits a fetch promise before navigating — flush the
// microtask queue once before asserting.
Promise.resolve().then(() => Promise.resolve()).then(() => Promise.resolve()).then(afterCreate);

function buildDetailDom() {
  byIdMap = {};
  const root = mk('div', 'refd-root', { 'data-slug': DETAIL.slug, 'data-table': JSON.stringify(DETAIL) });
  mk('span', 'refd-slug-chip');
  mk('h1', 'refd-title');
  mk('div', 'refd-meta');
  const exportBtn = mk('a', 'refd-export-btn', { href: '#' });
  const importBtn = mk('button', 'refd-import-btn');
  const settingsBtn = mk('button', 'refd-settings-btn');
  const consumersEl = mk('div', 'refd-consumers');
  const unmappedEl = mk('div', 'refd-unmapped');
  const searchIn = mk('input', 'refd-search');
  mk('div', 'refd-page-info');
  mk('tr', 'refd-data-thead');
  mk('tbody', 'refd-data-tbody');
  const pagePrev = mk('button', 'refd-page-prev');
  const pageNext = mk('button', 'refd-page-next');
  const versionsTbody = mk('tbody', 'refd-versions-tbody');
  const deleteBtn = mk('button', 'refd-delete-btn');
  const emptyBanner = mk('div', 'refd-empty-banner'); emptyBanner.hidden = true;
  mk('div', 'refd-empty-text');
  mk('a', 'refd-template-btn', { href: '#' });

  const drawer = mk('aside', 'refd-drawer'); drawer.hidden = true;
  const scrim = mk('div', 'refd-scrim'); scrim.hidden = true;
  mk('h2', 'refd-drawer-title');
  const settingsForm = mk('div', 'refd-settings-form'); settingsForm.hidden = true;
  const importForm = mk('div', 'refd-import-form'); importForm.hidden = true;
  mk('button', 'refd-drawer-close');

  mk('input', 'refd-s-title');
  mk('textarea', 'refd-s-desc');
  mk('input', 'refd-s-resp');
  mk('input', 'refd-s-pin', { type: 'checkbox' });
  const sSubmit = mk('button', 'refd-s-submit');
  mk('div', 'refd-s-status');

  const iFile = mk('input', 'refd-i-file', { type: 'file' });
  mk('input', 'refd-i-delete', { type: 'checkbox' });
  mk('input', 'refd-i-note');
  mk('div', 'refd-i-result');
  const iCheck = mk('button', 'refd-i-check');
  const iApply = mk('button', 'refd-i-apply'); iApply.disabled = true;
  mk('div', 'refd-i-status');

  return {
    root, exportBtn, importBtn, settingsBtn, consumersEl, unmappedEl, searchIn,
    pagePrev, pageNext, versionsTbody, drawer, scrim, settingsForm, importForm,
    sSubmit, iFile, iCheck, iApply, deleteBtn, emptyBanner,
  };
}

let detailFetchCalls = [];
let confirmReturn = true;
global.confirm = () => confirmReturn;

function runDetailPart() {
  detailFetchCalls = [];
  global.fetch = async (url, opts) => {
    detailFetchCalls.push({ url, opts, body: opts && opts.body && typeof opts.body === 'string' ? JSON.parse(opts.body) : null });
    if (url === '/api/data-quality') return { ok: true, status: 200, json: async () => DQ_FINDINGS };
    if (String(url).includes('/versions/3/restore')) return { ok: true, status: 200, json: async () => ({ version: 4, row_count: 2 }) };
    if (String(url).includes('/import')) {
      const isDry = opts && opts.body && Array.from(opts.body.entries ? opts.body.entries() : []).some(e => e[0] === 'dry_run' && e[1] === '1');
      return { ok: true, status: 200, json: async () => ({ dry_run: true, added: 1, changed: 0, removed: 0, unchanged: 1, errors: [], duplicates: [], can_commit: true, version: 3 }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };

  global.location = { href: 'http://x/reference/nwd_bv_map', reload: () => { locationReloaded = true; } };
  let locationReloaded = false;

  // ADR-16 addendum, checked FIRST (each build+eval rebinds the whole page,
  // so the DOM the rest of this function drives has to be the last one made):
  // a table created from a manifest and never imported into names the
  // generator it came from and offers the empty template — "rows 0" alone
  // tells nobody what to do.
  const savedVersion = DETAIL.current_version;
  DETAIL.current_version = 0;
  const D0 = buildDetailDom();
  (0, eval)(src);
  check('detail: a manifest-created empty table asks for an Excel',
    D0.emptyBanner.hidden === false
    && byIdMap['refd-empty-text'].textContent.includes('Created from manifest slreport')
    && byIdMap['refd-empty-text'].textContent.includes('rows 0'),
    byIdMap['refd-empty-text'].textContent);
  check('detail: the empty banner links to the blank template',
    byIdMap['refd-template-btn'].href === '/api/reference/nwd_bv_map/template',
    byIdMap['refd-template-btn'].href);
  DETAIL.current_version = savedVersion;

  const D = buildDetailDom();
  (0, eval)(src);

  check('detail: export link points at the current version xlsx',
    D.exportBtn.href === '/api/reference/nwd_bv_map/versions/current/xlsx', D.exportBtn.href);

  check('detail: a populated table shows no "load Excel" banner',
    D.emptyBanner.hidden === true, String(D.emptyBanner.hidden));

  // Delete is destructive: it must ask, and a declined confirm must not call.
  confirmReturn = false;
  click(D.deleteBtn);
  check('detail: delete does NOT call the API when confirm() is declined',
    !detailFetchCalls.some(f => f.opts && f.opts.method === 'DELETE'),
    JSON.stringify(detailFetchCalls.map(f => f.url)));
  confirmReturn = true;
  click(D.deleteBtn);
  const del = detailFetchCalls.find(f => f.opts && f.opts.method === 'DELETE');
  check('detail: an accepted confirm DELETEs this table',
    !!del && del.url === '/api/reference/nwd_bv_map',
    JSON.stringify(detailFetchCalls.map(f => f.url)));

  check('detail: consumer table has every consumer row',
    D.consumersEl.children[0].children[1].children.length === 3,
    collectText(D.consumersEl));
  const consumersText = collectText(D.consumersEl);
  check('detail: applied consumer at an old version shows "recalculation needed" badge',
    consumersText.includes('used v2') && consumersText.includes('recalculation needed'), consumersText);
  const consumerRows = D.consumersEl.children[0].children[1].children;
  check('detail: a chat-only consumer is badged «in chat» and never «hasn\'t run yet»',
    collectText(consumerRows[2]).includes('in chat')
    && collectText(consumerRows[2]).includes('reads in chat')
    && !collectText(consumerRows[2]).includes("hasn't run yet"),
    collectText(consumerRows[2]));
  check('detail: never-run consumer shows "hasn\'t run yet"',
    consumersText.includes("hasn't run yet"), consumersText);

  // Data grid renders the baked rows.
  const dataTbody = byIdMap['refd-data-tbody'];
  check('detail: data grid renders 2 baked rows', dataTbody.children.length === 2, String(dataTbody.children.length));

  // Search filters client-side within the loaded page.
  D.searchIn.value = 'BV-2';
  input(D.searchIn);
  check('detail: search narrows the data grid to the matching key',
    dataTbody.children.length === 1 && collectText(dataTbody).includes('BV-2'), collectText(dataTbody));
  D.searchIn.value = '';
  input(D.searchIn);
  check('detail: clearing search restores all rows', dataTbody.children.length === 2);

  // Versions render with diff pills and a restore button (can_write: true).
  check('detail: versions table has 2 rows', D.versionsTbody.children.length === 2,
    String(D.versionsTbody.children.length));
  const vRow0Text = collectText(D.versionsTbody.children[0]);
  check('detail: version row shows the added-diff pill', vRow0Text.includes('+1'), vRow0Text);

  // Restore: confirm() gate, then POST to the right URL.
  const restoreBtn = findAll(D.versionsTbody, n => n.tagName === 'BUTTON' && n._text === 'Restore')[0];
  check('detail: restore button is present', !!restoreBtn);
  confirmReturn = false;
  if (restoreBtn) click(restoreBtn);
  return Promise.resolve().then(() => Promise.resolve()).then(() => {
    check('detail: restore does NOT POST when confirm() is declined',
      !detailFetchCalls.some(c => String(c.url).includes('/restore')));

    confirmReturn = true;
    if (restoreBtn) click(restoreBtn);
    return Promise.resolve().then(() => Promise.resolve()).then(() => {
      const restoreCall = detailFetchCalls.find(c => String(c.url).includes('/restore'));
      check('detail: restore POSTs to the version-3 restore endpoint after confirm() accepts',
        !!restoreCall && String(restoreCall.url) === '/api/reference/nwd_bv_map/versions/3/restore',
        restoreCall && restoreCall.url);
      check('detail: restore uses POST', !!restoreCall && restoreCall.opts.method === 'POST');

      // ── Unmapped section: async fetch from /api/data-quality, filtered
      //    client-side to this table's kind:unmapped_values findings.
      return Promise.resolve().then(() => Promise.resolve()).then(() => {
        const unmappedText = collectText(D.unmappedEl);
        check('detail: unmapped section shows this table\'s finding',
          unmappedText.includes('New values in reference table nwd_bv_map'), unmappedText);
        check('detail: unmapped section filters out a finding for a different table',
          !unmappedText.includes('other_tbl'), unmappedText);
        const tplLink = findAll(D.unmappedEl, n => n.tagName === 'A' && n.attrs.href && n.attrs.href.includes('template'))[0];
        check('detail: unmapped section links to the value template', !!tplLink,
          JSON.stringify(D.unmappedEl.attrs));

        runImportPart(D);
      });
    });
  });
}

function runImportPart(D) {
  // Open the import drawer, wire the dry-run response, confirm gating.
  const iFile = byIdMap['refd-i-file'];
  const iCheck = byIdMap['refd-i-check'];
  const iApply = byIdMap['refd-i-apply'];

  iFile.files = [{ name: 'data.xlsx' }];

  // ── dry-run WITH errors -> Apply stays disabled ──────────────────
  detailFetchCalls = [];
  global.fetch = async (url, opts) => {
    detailFetchCalls.push({ url, opts });
    if (String(url).includes('/import')) {
      return {
        ok: true, status: 200,
        json: async () => ({ dry_run: true, added: 0, changed: 0, removed: 0, unchanged: 0, errors: [{ row: 1, column: 'K', reason: 'bad' }], duplicates: [], can_commit: false, version: 3 }),
      };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  click(iCheck);
  return Promise.resolve().then(() => Promise.resolve()).then(() => {
    check('import: dry-run with errors leaves Apply disabled', iApply.disabled === true, String(iApply.disabled));
    const resultText = collectText(byIdMap['refd-i-result']);
    check('import: dry-run error row rendered', resultText.includes('bad'), resultText);

    // ── dry-run CLEAN -> Apply enabled -> apply POSTs expected_version ──
    detailFetchCalls = [];
    let sawDryRun = false;
    global.fetch = async (url, opts) => {
      detailFetchCalls.push({ url, opts });
      if (String(url).includes('/import')) {
        if (!sawDryRun) {
          sawDryRun = true;
          return { ok: true, status: 200, json: async () => ({ dry_run: true, added: 1, changed: 0, removed: 0, unchanged: 1, errors: [], duplicates: [], can_commit: true, version: 3 }) };
        }
        return { ok: true, status: 200, json: async () => ({ dry_run: false, added: 1, changed: 0, removed: 0, unchanged: 1, version: 4 }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    };
    click(iCheck);
    return Promise.resolve().then(() => Promise.resolve()).then(() => {
      check('import: clean dry-run enables Apply', iApply.disabled === false, String(iApply.disabled));

      click(iApply);
      return Promise.resolve().then(() => Promise.resolve()).then(() => {
        const commitCall = detailFetchCalls.find(c => String(c.url).includes('/import') && c.opts);
        // find the SECOND import call (the commit one) by inspecting FormData
        const importCalls = detailFetchCalls.filter(c => String(c.url).includes('/import'));
        const commit = importCalls[importCalls.length - 1];
        check('import: apply POSTs to the import endpoint', !!commit);
        const fd = commit && commit.opts && commit.opts.body;
        const entries = fd && fd.entries ? Array.from(fd.entries()) : [];
        const asMap = Object.fromEntries(entries);
        check('import: commit carries dry_run=0', asMap.dry_run === '0', JSON.stringify(asMap));
        check('import: commit carries expected_version from the table\'s current_version',
          asMap.expected_version === '3', JSON.stringify(asMap));

        console.log();
        if (FAILURES.length) {
          console.log(FAILURES.length + ' FAILED: ' + FAILURES.join(', '));
          process.exit(1);
        }
        console.log('ALL PASS');
      });
    });
  });
}

// Minimal FormData polyfill (no external deps) — enough for reference.js's
// usage (append/entries) and for this harness to inspect what was sent.
if (typeof FormData === 'undefined') {
  global.FormData = class {
    constructor() { this._entries = []; }
    append(k, v) { this._entries.push([k, v]); }
    entries() { return this._entries[Symbol.iterator](); }
  };
}
