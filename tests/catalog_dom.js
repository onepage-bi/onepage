/* Headless harness for static/js/catalog.js.
 *
 * Same idiom as tests/quality_dom.js: no jsdom, no new dependency — the
 * smallest DOM the script actually touches (elements with classes,
 * attributes, dataset, form values, listeners) plus a stubbed `fetch` so
 * the read path (tiles/filters/drawer) and one write path (confirm) are
 * driven and inspected without a server.
 *
 * Run: node tests/catalog_dom.js   (prints PASS/FAIL lines + ALL PASS)
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
  toggle(c, on) { if (on === undefined) on = !this.set.has(c); if (on) this.set.add(c); else this.set.delete(c); return on; }
  get value() { return Array.from(this.set).join(' '); }
}
class El {
  constructor(tag, ns) {
    this.tagName = tag.toUpperCase();
    this.ns = ns || null;
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
    this.placeholder = '';
    this.type = '';
    this.min = '';
  }
  get classList() { return this._class; }
  set className(v) { this._class.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return this._class.value; }
  set textContent(v) { this.children = []; this._text = v === '' ? '' : String(v); }
  get textContent() { return this._text; }
  get options() { return this.children.filter(c => c.tagName === 'OPTION'); }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter(x => x !== c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[camel(k.slice(5))] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter(f => f !== fn); }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    (this.listeners[ev.type] || []).forEach(f => f.call(this, ev));
    return true;
  }
  contains(elx) { if (elx === this) return true; return this.children.some(c => c.contains(elx)); }
  focus() { document.activeElement = this; }
  querySelectorAll(sel) {
    const out = [];
    walk(this, n => { if (matches(n, sel)) out.push(n); });
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
function camel(s) { return s.replace(/-([a-z])/g, (m, c) => c.toUpperCase()); }
function walk(n, fn) { n.children.forEach(c => { fn(c); walk(c, fn); }); }
function matches(n, sel) {
  const m = /^([a-zA-Z0-9]*)((?:\[[-a-z]+(?:=["']?[^"'\]]*["']?)?\])*)$/.exec(sel);
  if (!m) return false;
  if (m[1] && n.tagName.toLowerCase() !== m[1].toLowerCase()) return false;
  const groups = m[2].match(/\[[^\]]+\]/g) || [];
  return groups.every(g => {
    const bodyStr = g.slice(1, -1);
    const eq = bodyStr.indexOf('=');
    if (eq === -1) return n.attrs[bodyStr] !== undefined;
    const key = bodyStr.slice(0, eq);
    const val = bodyStr.slice(eq + 1).replace(/^["']|["']$/g, '');
    return n.attrs[key] === val;
  });
}

let byIdMap = {};
let body = new El('div');
let docListeners = {};
const document = {
  createElement: t => new El(t),
  createElementNS: (ns, t) => new El(t, ns),
  getElementById: id => byIdMap[id] || null,
  addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
  dispatchEvent: ev => { ev.target = ev.target || document; (docListeners[ev.type] || []).forEach(f => f.call(document, ev)); return true; },
  querySelectorAll: sel => body.querySelectorAll(sel),
  querySelector: sel => body.querySelector(sel),
  createTextNode: v => ({ nodeType: 3, text: String(v) }),
  activeElement: null,
};

function mk(tag, attrs, id) {
  const e = new El(tag);
  Object.keys(attrs || {}).forEach(k => e.setAttribute(k, attrs[k]));
  if (id) { e.attrs.id = id; byIdMap[id] = e; }
  return e;
}

// ── fixture ──────────────────────────────────────────────────────────
const STATS = { tables: 3, inferred: 1, confirmed: 1, rejected: 1, nontables: 4 };
const TABLES = [
  {
    table_id: 't_aaa000000001', name: 'datasalesorder', display_name: 'DataSalesOrder_06.26.xlsx',
    status: 'inferred', confidence: 0.91, n_files: 3, n_columns: 8, partitions: 3,
    latest_partition: { key: '2026-08', last_seen_at: '2026-08-20T10:00:00' },
    consumers: ['gen-rdcrct', 'gen-ops-rdc-health'], owner: null,
    source: { id: 1, key_name: 'OPS_RDC_Exports' }, stale: false, version: 1,
    first_seen_at: '2026-07-01T00:00:00', last_seen_at: '2026-08-20T10:00:00',
  },
  {
    table_id: 't_bbb000000002', name: 'rdc_data', display_name: 'RDC warehouse stock',
    status: 'confirmed', confidence: 0.77, n_files: 1, n_columns: 7, partitions: 1,
    latest_partition: null, consumers: ['gen-rdcrct'], owner: { id: 5, name: 'ivanov' },
    source: { id: 1, key_name: 'OPS_RDC_Exports' }, stale: false, version: 2,
    first_seen_at: '2026-07-01T00:00:00', last_seen_at: '2026-08-20T10:00:00',
  },
  {
    table_id: 't_ccc000000003', name: 'stock', display_name: 'stock_07.26.xlsx',
    status: 'rejected', confidence: 0.55, n_files: 2, n_columns: 5, partitions: 2,
    latest_partition: { key: '2026-08', last_seen_at: '2026-08-20T10:00:00' },
    consumers: [], owner: null,
    source: { id: 2, key_name: 'Stock_Extra' }, stale: true, version: 3,
    first_seen_at: '2026-06-01T00:00:00', last_seen_at: '2026-07-01T00:00:00',
  },
];
const FIXTURE = { tables: TABLES, stats: STATS };

const DETAIL = {
  table_id: 't_aaa000000001', data_source_id: 1,
  source: { id: 1, key_name: 'OPS_RDC_Exports', label: null },
  name: 'datasalesorder', name_stem: 'datasalesorder', display_name: 'DataSalesOrder_06.26.xlsx',
  status: 'inferred', confidence: 0.91, version: 1, owner: null, confirmed_by: null, confirmed_at: null,
  superseded_by: null, header_row: 0, sheets: ['TDSheet'],
  columns: [{ name: 'item', display: 'Item', present_ratio: 1.0, type: null }],
  core_columns: ['item'], n_files: 3,
  partitions: [
    { key: '2026-08', file: 'DataSalesOrder_08.26.xlsx', sheet: 'TDSheet', rows: 900, size: 5000, mtime_ms: 1000, last_run_id: 3, last_seen_at: '2026-08-20T10:00:00' },
    { key: '2026-06', file: 'DataSalesOrder_06.26.xlsx', sheet: 'TDSheet', rows: 4000, size: 5000, mtime_ms: 900, last_run_id: 1, last_seen_at: '2026-08-20T10:00:00' },
  ],
  consumers: [
    { generator_id: 11, generator_name: 'gen-rdcrct', matched_input: null, last_run_id: 3, legacy: true },
    { generator_id: 12, generator_name: 'gen-ops-rdc-health', matched_input: null, last_run_id: 2, legacy: false },
  ],
  evidence: [{ file: 'DataSalesOrder_06.26.xlsx', sheet: 'TDSheet', source_file_id: 101, run_id: 1, sim: 1.0 }],
  freshness: { newest_mtime_ms: 1000, days_since: 3.2 },
  first_seen_at: '2026-07-01T00:00:00', last_seen_at: '2026-08-20T10:00:00',
  nontables: [{ file: 'Pivot Report.xlsx', sheet: 'Pivot', kind: 'pivot', reason: 'pivot marker', headers: ['Row Labels'] }],
};

const SUGGEST = {
  generator_id: 11, generator_name: 'gen-rdcrct',
  inputs: [{ match: 'DataSalesOrder_*.xlsx', role: 'data', sheets: [{ name: 'TDSheet', columns: ['Item'] }] }],
  inputs_json: '[{"match":"DataSalesOrder_*.xlsx"}]',
  tables: [], sources: [{ data_source_id: 1, key_name: 'OPS_RDC_Exports', label: null, matched: ['a.xlsx'], unmatched: ['b.xlsx'] }],
  matched: ['a.xlsx'], unmatched: ['b.xlsx'],
};

// Builds one fresh copy of the page's DOM skeleton (mirrors
// templates/pages/catalog.html).
function buildDom() {
  byIdMap = {};
  body = new El('div');
  docListeners = {};
  const root = mk('div', {}, 'cat-root');
  body.appendChild(root);
  root.setAttribute('data-tables', JSON.stringify(FIXTURE.tables));
  root.setAttribute('data-stats', JSON.stringify(FIXTURE.stats));

  ['cat-tile-tables', 'cat-tile-confirmed', 'cat-tile-inferred', 'cat-tile-nontables']
    .forEach(id => body.appendChild(mk('div', {}, id)));

  const searchIn = mk('input', {}, 'cat-search');
  body.appendChild(searchIn);
  ['inferred', 'confirmed', 'rejected'].forEach(v => body.appendChild(mk('button', { 'data-cat-status': v })));
  const sourceSel = mk('select', {}, 'cat-source');
  body.appendChild(sourceSel);
  const rebuildBtn = mk('button', {}, 'cat-rebuild');
  body.appendChild(rebuildBtn);
  const tbody = mk('tbody', {}, 'cat-tbody');
  body.appendChild(tbody);
  const emptyEl = mk('div', {}, 'cat-empty');
  emptyEl.hidden = true;
  body.appendChild(emptyEl);

  const scrim = mk('div', {}, 'cat-scrim');
  scrim.hidden = true;
  body.appendChild(scrim);
  const drawer = mk('aside', {}, 'cat-drawer');
  drawer.hidden = true;
  body.appendChild(drawer);
  const drawerHead = mk('header', {});
  drawer.appendChild(drawerHead);
  drawerHead.appendChild(mk('h2', { 'data-drawer-title': '' }));
  drawerHead.appendChild(mk('button', { 'data-drawer-close': '' }));
  const drawerBody = mk('div', { 'data-drawer-body': '' });
  drawer.appendChild(drawerBody);

  return { searchIn, sourceSel, rebuildBtn, tbody, emptyEl, scrim, drawer, drawerBody };
}

// ── fetch stub ───────────────────────────────────────────────────────
const fetchCalls = [];
global.fetch = async (url, opts) => {
  fetchCalls.push({ url, opts, body: opts && opts.body ? JSON.parse(opts.body) : null });
  if (url === '/api/catalog/tables') return { ok: true, status: 200, json: async () => FIXTURE };
  if (url === '/api/catalog/tables/t_aaa000000001') return { ok: true, status: 200, json: async () => DETAIL };
  if (url === '/api/catalog/suggest-inputs/11') return { ok: true, status: 200, json: async () => SUGGEST };
  if (url === '/api/catalog/tables/t_aaa000000001/confirm') return { ok: true, status: 200, json: async () => Object.assign({}, DETAIL, { status: 'confirmed' }) };
  return { ok: true, status: 200, json: async () => ({}) };
};

global.document = document;
global.OP_USER = { role: 'administrator' };
global.location = { pathname: '/catalog', search: '', href: 'http://x/catalog' };
global.history = { replaceState: (a, b, u) => { global.location.href = String(u); } };
global.window = { history: global.history, OP_USER: global.OP_USER };

const src = fs.readFileSync(path.join(ROOT, 'static/js/catalog.js'), 'utf8');

let dom = buildDom();
(0, eval)(src);

function visibleTableIds() {
  return dom.tbody.children.map(tr => tr.dataset.catTable);
}
function click(elx) { elx.dispatchEvent({ type: 'click', target: elx }); }
function change(elx) { elx.dispatchEvent({ type: 'change', target: elx }); }
function input(elx) { elx.dispatchEvent({ type: 'input', target: elx }); }
function collectText(node) {
  if (node && node.nodeType === 3) return node.text || '';
  let out = (node && node._text) || '';
  (node && node.children || []).forEach(c => { out += ' ' + collectText(c); });
  return out;
}
function waitThen(fn) { return new Promise(res => setTimeout(() => res(fn()), 0)); }

setTimeout(() => {
  // 1. Tiles render from stats.
  check('tile: tables', document.getElementById('cat-tile-tables').textContent === '3');
  check('tile: confirmed', document.getElementById('cat-tile-confirmed').textContent === '1');
  check('tile: inferred (need confirmation)', document.getElementById('cat-tile-inferred').textContent === '1');
  check('tile: nontables', document.getElementById('cat-tile-nontables').textContent === '4');

  // 2. All three rows render initially.
  check('all rows render initially', visibleTableIds().length === 3, visibleTableIds().join(','));

  // 3. Source select is populated from the tables list.
  check('source select has both sources',
    dom.sourceSel.options.map(o => o.value).sort().join(',') === ',1,2',
    dom.sourceSel.options.map(o => o.value).join(','));

  // 4. Status chip filter.
  const confirmedChip = document.querySelectorAll('[data-cat-status]').find(b => b.attrs['data-cat-status'] === 'confirmed');
  click(confirmedChip);
  check('status=confirmed filters to the one confirmed table',
    visibleTableIds().length === 1 && visibleTableIds()[0] === 't_bbb000000002',
    visibleTableIds().join(','));
  click(confirmedChip); // toggle back off

  // 5. Source select filter.
  dom.sourceSel.value = '2';
  change(dom.sourceSel);
  check('source filter narrows to Stock_Extra',
    visibleTableIds().length === 1 && visibleTableIds()[0] === 't_ccc000000003',
    visibleTableIds().join(','));
  dom.sourceSel.value = '';
  change(dom.sourceSel);

  // 6. Search filter.
  dom.searchIn.value = 'warehouse';
  input(dom.searchIn);
  check('search matches display_name/name',
    visibleTableIds().length === 1 && visibleTableIds()[0] === 't_bbb000000002',
    visibleTableIds().join(','));
  dom.searchIn.value = '';
  input(dom.searchIn);
  check('clearing search restores all rows', visibleTableIds().length === 3, visibleTableIds().join(','));

  // 7. Stale badge rendered for the stale row.
  const staleRow = dom.tbody.children.find(tr => tr.dataset.catTable === 't_ccc000000003');
  check('stale row carries the stale badge', collectText(staleRow).includes('outdated'));

  // 8. Open the drawer -> fetches detail and renders it.
  const row = dom.tbody.children.find(tr => tr.dataset.catTable === 't_aaa000000001');
  click(row);
  return waitThen(() => {
    check('drawer becomes visible', dom.drawer.hidden === false);
    const titleEl = dom.drawer.querySelector('[data-drawer-title]');
    check('drawer title shows the display_name',
      titleEl && titleEl.textContent === 'DataSalesOrder_06.26.xlsx', titleEl && titleEl.textContent);
    const bodyText = collectText(dom.drawerBody);
    check('drawer shows the source key_name', bodyText.includes('OPS_RDC_Exports'), bodyText.slice(0, 200));
    check('drawer shows columns section', bodyText.includes('Item'), bodyText.slice(0, 400));
    check('drawer shows partitions section', bodyText.includes('2026-08') && bodyText.includes('2026-06'), bodyText.slice(0, 400));
    check('drawer shows the legacy consumer\'s suggest-inputs toggle',
      bodyText.includes('Suggest inputs for «gen-rdcrct»'), bodyText.slice(0, 600));
    check('drawer does NOT offer suggest-inputs for the non-legacy consumer',
      !bodyText.includes('gen-ops-rdc-health»'), bodyText.slice(0, 600));

    // 9. Confirm button posts to the confirm endpoint with the row's version.
    const confirmBtn = dom.drawerBody.querySelectorAll('button').find(b => collectText(b).trim() === 'Confirm');
    check('confirm button is present', !!confirmBtn);
    if (confirmBtn) click(confirmBtn);
    return waitThen(() => {
      const confirmCall = fetchCalls.find(c => c.url === '/api/catalog/tables/t_aaa000000001/confirm');
      check('confirm button POSTs to the confirm endpoint', !!confirmCall, JSON.stringify(fetchCalls.map(c => c.url)));
      check('confirm POST carries the version the drawer loaded',
        !!confirmCall && confirmCall.body && confirmCall.body.version === 1,
        confirmCall && JSON.stringify(confirmCall.body));
      check('confirm POST uses application/json',
        !!confirmCall && confirmCall.opts.headers['Content-Type'] === 'application/json');
      // The confirm handler refetches the whole list on success — the page
      // never called this endpoint before (the initial render comes from
      // baked data-attributes), so exactly one call is the refetch.
      check('confirm success triggers a refetch of the table list',
        fetchCalls.filter(c => c.url === '/api/catalog/tables').length === 1,
        JSON.stringify(fetchCalls.map(c => c.url)));

      console.log();
      if (FAILURES.length) {
        console.log(FAILURES.length + ' FAILED: ' + FAILURES.join(', '));
        process.exit(1);
      }
      console.log('ALL PASS');
    });
  });
}, 0);
