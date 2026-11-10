/* Headless harness for static/js/data-quality.js.
 *
 * Same idiom as tests/lineage_dom.js: no jsdom, no new dependency — the
 * smallest DOM the script actually touches (elements with classes,
 * attributes, dataset, form values, listeners) plus a stubbed `fetch` so
 * every mutation path (ack, unack, accept, alias, thresholds) is driven and
 * inspected without a server. Namespaced (SVG) elements refuse direct
 * property assignment on their read-only IDL attributes (className,
 * viewBox, width, height, points, cx, cy, r), exactly as real SVGElements
 * do under 'use strict' — this is what caught the drawer-never-opens bug
 * (data-quality.js's el() used to write `n[k] = v` for those), and keeps
 * catching it if it comes back.
 *
 * Run: node tests/quality_dom.js   (prints PASS/FAIL lines + ALL PASS)
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
    this.href = '';
    this.min = '';
    this.max = '';
    this.name = '';
  }
  get classList() { return this._class; }
  set className(v) {
    // Real SVGElement.className (and viewBox/width/height/points/cx/cy/r,
    // handled below) is a read-only IDL attribute — `svgEl.className = x`
    // throws a TypeError under 'use strict' in every browser. Mirror that
    // here so a regression that assigns SVG properties instead of calling
    // setAttribute (the data-quality.js drawer-never-opens bug) fails this
    // harness instead of passing it silently.
    if (this.ns) throw new TypeError('Cannot set property className of #<SVGElement> which has only a getter');
    this._class.set = new Set(String(v).split(/\s+/).filter(Boolean));
  }
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
  contains(el) { if (el === this) return true; return this.children.some(c => c.contains(el)); }
  focus() { document.activeElement = this; }
  // Real SVG elements expose these as read-only IDL getters (SVGAnimatedRect,
  // SVGAnimatedLength, SVGPointList, ...) — `'viewBox' in svgEl` is true, so
  // buggy code that does `n[k] = v` when `k in n` (see data-quality.js's old
  // el()) takes the property-assignment branch and throws. Mirror the
  // existence + the throw, or the harness would take the (harmless here)
  // setAttribute fallback and never notice.
  get width() { return {}; }
  set width(v) { if (this.ns) throw new TypeError("Cannot set property width of #<SVGElement> which has only a getter"); }
  get height() { return {}; }
  set height(v) { if (this.ns) throw new TypeError("Cannot set property height of #<SVGElement> which has only a getter"); }
  get viewBox() { return {}; }
  set viewBox(v) { if (this.ns) throw new TypeError("Cannot set property viewBox of #<SVGElement> which has only a getter"); }
  get points() { return {}; }
  set points(v) { if (this.ns) throw new TypeError("Cannot set property points of #<SVGElement> which has only a getter"); }
  get cx() { return {}; }
  set cx(v) { if (this.ns) throw new TypeError("Cannot set property cx of #<SVGElement> which has only a getter"); }
  get cy() { return {}; }
  set cy(v) { if (this.ns) throw new TypeError("Cannot set property cy of #<SVGElement> which has only a getter"); }
  get r() { return {}; }
  set r(v) { if (this.ns) throw new TypeError("Cannot set property r of #<SVGElement> which has only a getter"); }
  querySelectorAll(sel) {
    const out = [];
    walk(this, n => { if (matches(n, sel)) out.push(n); });
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
function camel(s) { return s.replace(/-([a-z])/g, (m, c) => c.toUpperCase()); }
function walk(n, fn) { n.children.forEach(c => { fn(c); walk(c, fn); }); }
// Supports a plain tag ("button"), "[attr]"/"[attr=val]", or a tag with
// bracket groups ("tr[data-dq-finding]") — everything data-quality.js and
// this harness's own lookups ever ask for.
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

const FIXTURE = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests/fixtures/quality_sample.json'), 'utf8'));

// Builds one fresh copy of the page's DOM skeleton (mirrors
// templates/pages/data_quality.html) and returns the handles the assertions
// need. Called once up front and once more for the URL-prefill test, which
// needs a page that has never rendered yet.
function buildDom() {
  byIdMap = {};
  body = new El('div');
  docListeners = {};
  const root = mk('div', {}, 'dq-root');
  body.appendChild(root);
  root.setAttribute('data-findings', JSON.stringify(FIXTURE.findings));
  root.setAttribute('data-sources', JSON.stringify(FIXTURE.sources));
  root.setAttribute('data-history', JSON.stringify(FIXTURE.history));
  root.setAttribute('data-stats', JSON.stringify(FIXTURE.stats));
  root.setAttribute('data-pending', JSON.stringify(FIXTURE.pending_values));

  ['dq-tile-ok', 'dq-tile-warn', 'dq-tile-error', 'dq-tile-open', 'dq-tile-pending', 'dq-tile-stale']
    .forEach(id => body.appendChild(mk('div', {}, id)));

  const searchIn = mk('input', {}, 'dq-search');
  body.appendChild(searchIn);
  ['error', 'warn', 'info'].forEach(v => body.appendChild(mk('button', { 'data-dq-sev': v })));
  ['schema', 'freshness', 'checks', 'drift', 'other'].forEach(v => body.appendChild(mk('button', { 'data-dq-group': v })));
  const sourceSel = mk('select', {}, 'dq-source');
  body.appendChild(sourceSel);
  const genSel = mk('select', {}, 'dq-generator');
  body.appendChild(genSel);
  const ackedToggle = mk('input', { type: 'checkbox' }, 'dq-show-acked');
  body.appendChild(ackedToggle);
  const tbody = mk('tbody', {}, 'dq-tbody');
  body.appendChild(tbody);
  const emptyEl = mk('div', {}, 'dq-empty');
  emptyEl.hidden = true;
  body.appendChild(emptyEl);
  const thresholdsPanel = mk('div', {}, 'dq-thresholds');
  thresholdsPanel.hidden = true;
  body.appendChild(thresholdsPanel);

  const scrim = mk('div', {}, 'dq-scrim');
  scrim.hidden = true;
  body.appendChild(scrim);
  const drawer = mk('aside', {}, 'dq-drawer');
  drawer.hidden = true;
  body.appendChild(drawer);
  const drawerHead = mk('header', {});
  drawer.appendChild(drawerHead);
  drawerHead.appendChild(mk('h2', { 'data-drawer-title': '' }));
  drawerHead.appendChild(mk('button', { 'data-drawer-close': '' }));
  const drawerBody = mk('div', { 'data-drawer-body': '' });
  drawer.appendChild(drawerBody);

  return { searchIn, sourceSel, genSel, ackedToggle, tbody, emptyEl, thresholdsPanel, scrim, drawer, drawerBody };
}

// ── fetch stub ───────────────────────────────────────────────────────
const fetchCalls = [];
let fetchQueue = [];   // [{matcher, status, json}] consumed FIFO per matching URL
function queueResponse(matcher, status, jsonBody) {
  fetchQueue.push({ matcher, status, json: jsonBody });
}
global.fetch = async (url, opts) => {
  fetchCalls.push({ url, opts, body: opts && opts.body ? JSON.parse(opts.body) : null });
  const idx = fetchQueue.findIndex(q =>
    (typeof q.matcher === 'string' ? q.matcher === url : q.matcher.test(url)));
  if (idx !== -1) {
    const q = fetchQueue.splice(idx, 1)[0];
    return { ok: q.status < 300, status: q.status, json: async () => q.json };
  }
  if (url === '/api/data-quality') {
    return { ok: true, status: 200, json: async () => FIXTURE };
  }
  return { ok: true, status: 200, json: async () => ({}) };
};

global.document = document;
global.location = { pathname: '/data-quality', search: '', href: 'http://x/data-quality' };
global.history = { replaceState: (a, b, u) => { global.location.href = String(u); } };
global.window = { history: global.history };

const src = fs.readFileSync(path.join(ROOT, 'static/js/data-quality.js'), 'utf8');

let dom = buildDom();
(0, eval)(src);

function visibleFindingIds() {
  return dom.tbody.children.map(tr => tr.dataset.dqFinding);
}
function click(el) { el.dispatchEvent({ type: 'click', target: el }); }
function change(el) { el.dispatchEvent({ type: 'change', target: el }); }
function input(el) { el.dispatchEvent({ type: 'input', target: el }); }
function collectText(node) {
  // A plain document.createTextNode() result (nodeType 3, {text}) has no
  // .children — el()'s appendChild(kids) and renderEmptyState's own
  // direct createTextNode() both hang these off a parent, so walking a
  // subtree has to handle a bare text node, not just El instances.
  if (node && node.nodeType === 3) return node.text || '';
  let out = (node && node._text) || '';
  (node && node.children || []).forEach(c => { out += ' ' + collectText(c); });
  return out;
}
function waitThen(fn) { return new Promise(res => setTimeout(() => res(fn()), 0)); }

setTimeout(() => {
  // 1. Tiles render from stats; acked findings hidden by default.
  check('tile: sources_ok', document.getElementById('dq-tile-ok').textContent === '1');
  check('tile: sources_warn', document.getElementById('dq-tile-warn').textContent === '1');
  check('tile: sources_error', document.getElementById('dq-tile-error').textContent === '1');
  check('tile: open findings', document.getElementById('dq-tile-open').textContent === '13');
  check('tile: pending values', document.getElementById('dq-tile-pending').textContent === '1');
  check('tile: stale sources', document.getElementById('dq-tile-stale').textContent === '1');
  check('default view hides the acked finding',
    !visibleFindingIds().includes('f04filestale00004'), visibleFindingIds().join(','));
  check('default view shows every other finding', visibleFindingIds().length === 13,
    String(visibleFindingIds().length));

  // 2. Severity chip filter.
  const errChip = document.querySelectorAll('[data-dq-sev]').find(b => b.attrs['data-dq-sev'] === 'error');
  click(errChip);
  check('severity=error filters to the one open error finding',
    visibleFindingIds().length === 1 && visibleFindingIds()[0] === 'f08reqfilegone008',
    visibleFindingIds().join(','));
  click(errChip); // toggle back off

  // 3. Kind-group chip filter.
  const driftChip = document.querySelectorAll('[data-dq-group]').find(b => b.attrs['data-dq-group'] === 'drift');
  click(driftChip);
  check('kind group=drift shows new_values and unmapped_values',
    visibleFindingIds().length === 2 && visibleFindingIds().includes('f07newvalues00007')
      && visibleFindingIds().includes('f13unmapped000013'),
    visibleFindingIds().join(','));
  click(driftChip);

  // 4. Search.
  dom.searchIn.value = 'Comment';
  input(dom.searchIn);
  check('search filters by column name',
    visibleFindingIds().length === 1 && visibleFindingIds()[0] === 'f10deaddecl00010',
    visibleFindingIds().join(','));
  dom.searchIn.value = '';
  input(dom.searchIn);

  // 5. Acked toggle.
  dom.ackedToggle.checked = true;
  change(dom.ackedToggle);
  check('show-acked reveals the acked finding too', visibleFindingIds().length === 14,
    String(visibleFindingIds().length));
  const ackedRow = dom.tbody.children.find(tr => tr.dataset.dqFinding === 'f04filestale00004');
  check('acked row carries the dimming class', ackedRow && ackedRow.classList.contains('is-acked'));
  dom.ackedToggle.checked = false;
  change(dom.ackedToggle);

  // 6. Source / generator selects.
  check('source select is populated from the sources list',
    dom.sourceSel.options.some(o => o.value === '2' && o.textContent === 'Branches_Data'));
  dom.sourceSel.value = '2';
  change(dom.sourceSel);
  check('source filter narrows to that source’s open findings', visibleFindingIds().length === 4,
    String(visibleFindingIds().length));
  check('thresholds panel appears for a selected source', dom.thresholdsPanel.hidden === false);
  dom.sourceSel.value = '';
  change(dom.sourceSel);

  dom.genSel.value = 'rdcstock';
  change(dom.genSel);
  check('generator filter narrows to that generator’s open findings', visibleFindingIds().length === 6,
    String(visibleFindingIds().length));
  dom.genSel.value = '';
  change(dom.genSel);

  // 7. Empty state.
  dom.searchIn.value = 'no such thing anywhere';
  input(dom.searchIn);
  check('empty state shows when nothing matches', dom.emptyEl.hidden === false);
  dom.searchIn.value = '';
  input(dom.searchIn);
  check('empty state hides once results return', dom.emptyEl.hidden === true);

  // 8. Drawer: check_fail detail.
  const checkFailRow = dom.tbody.children.find(tr => tr.dataset.dqFinding === 'f06checkfail00006');
  click(checkFailRow);
  check('drawer opens (not hidden)', dom.drawer.hidden === false);
  const drawerText1 = collectText(dom.drawerBody);
  check('check_fail drawer shows count/failed chips',
    drawerText1.includes('count: 40') && drawerText1.includes('failed: 5'), drawerText1);
  check('check_fail drawer lists both examples',
    drawerText1.includes('31.13.2024') && drawerText1.includes('—'), drawerText1);

  // 8a. Sampled check_fail: row and drawer both carry the "Sample" cue.
  check('sampled check_fail row carries the is-sampled class',
    checkFailRow.classList.contains('is-sampled'));
  const rowText = collectText(checkFailRow);
  check('sampled check_fail row shows a "Sample" badge', rowText.includes('Sample'), rowText);
  check('sampled check_fail drawer explains the count is a sample, not the whole file',
    drawerText1.includes('Sample') && drawerText1.includes('40'), drawerText1);

  // 8b. Drawer: the ADR-16 reference-table kinds render without throwing and
  // put every link and every value in as text — a strict-mode throw inside
  // openDrawer is exactly the class of bug this harness exists to catch.
  const unmappedRow = dom.tbody.children.find(tr => tr.dataset.dqFinding === 'f13unmapped000013');
  click(unmappedRow);
  check('unmapped_values drawer opens', dom.drawer.hidden === false);
  const unmappedText = collectText(dom.drawerBody);
  check('unmapped_values drawer lists the missing values',
    unmappedText.includes('1001') && unmappedText.includes('1003'), unmappedText);
  check('unmapped_values drawer names the responsible person',
    unmappedText.includes('Alex N.'), unmappedText);
  const unmappedLinks = dom.drawerBody.querySelectorAll('a').map(a => a.attrs.href || a.href);
  check('unmapped_values drawer links to the reference page and the template',
    unmappedLinks.includes('/reference/nwd_bv') &&
    unmappedLinks.some(h => String(h).indexOf('/api/reference/nwd_bv/template?unmapped=1') === 0),
    JSON.stringify(unmappedLinks));

  const rerunRow = dom.tbody.children.find(tr => tr.dataset.dqFinding === 'f14rerunneeded0014');
  click(rerunRow);
  const rerunText = collectText(dom.drawerBody);
  check('rerun_needed drawer states the used and the current version',
    rerunText.includes('v6') && rerunText.includes('v7'), rerunText);
  const rerunLinks = dom.drawerBody.querySelectorAll('a').map(a => a.attrs.href || a.href);
  check('rerun_needed drawer links to the report', rerunLinks.includes('/d/d3'),
    JSON.stringify(rerunLinks));

  // 9. Drawer: new_values detail + accept POST body + 409 refetch.
  const newValRow = dom.tbody.children.find(tr => tr.dataset.dqFinding === 'f07newvalues00007');
  click(newValRow);
  const checkboxes = dom.drawerBody.querySelectorAll('input');
  check('new_values drawer lists both pending values as checked checkboxes',
    checkboxes.length === 2 && checkboxes.every(c => c.checked === true), String(checkboxes.length));
  const acceptBtn = dom.drawerBody.querySelectorAll('button').find(b => b._text === 'Accept selected');
  check('accept button is present', !!acceptBtn);
  fetchCalls.length = 0;
  click(acceptBtn);

  return waitThen(() => {
    const post = fetchCalls.find(c => c.url === '/api/data-sources/2/baselines/20');
    check('accept posts to the pending entry’s generator_id endpoint', !!post, JSON.stringify(fetchCalls));
    check('accept body carries match/sheet/column/accept/version', post && post.body &&
      post.body.match === 'Branch_*.xlsx' && post.body.sheet === 'Sheet1' &&
      post.body.column === 'Region' && post.body.version === 3 &&
      post.body.accept.length === 2 && post.body.accept.includes('South-2') && post.body.accept.includes('Coast'),
      JSON.stringify(post && post.body));

    // 409 path: queue a conflict for the same endpoint, click again, expect a refetch.
    click(newValRow);
    const acceptBtn2 = dom.drawerBody.querySelectorAll('button').find(b => b._text === 'Accept selected');
    queueResponse('/api/data-sources/2/baselines/20', 409, {});
    fetchCalls.length = 0;
    click(acceptBtn2);

    return waitThen(() => {
      check('a 409 on accept triggers a refetch of /api/data-quality',
        fetchCalls.some(c => c.url === '/api/data-quality'), JSON.stringify(fetchCalls));

      // 10. Drawer: schema_missing_column alias flow.
      const aliasRow = dom.tbody.children.find(tr => tr.dataset.dqFinding === 'f01schemacol0001');
      click(aliasRow);
      const aliasBtn = dom.drawerBody.querySelectorAll('button')
        .find(b => (b._text || '').includes('as alias of'));
      check('schema_missing_column drawer offers the alias button', !!aliasBtn);
      queueResponse('/api/data-sources/1/overrides', 200, [{
        generator_id: 10, generator_name: 'rdcstock', exclude_rules: [],
        column_aliases: {}, version: 2, updated_at: null,
      }]);
      queueResponse('/api/data-sources/1/overrides/10', 200, {
        ok: true, generator_id: 10, data_source_id: 1,
        exclude_rules: [], column_aliases: { 'Stock_*.xlsx/Sheet1': { SKU_Discription: 'SKU Description' } },
        version: 3,
      });
      fetchCalls.length = 0;
      click(aliasBtn);

      return waitThen(() => waitThen(() => {
        const put = fetchCalls.find(c => c.url === '/api/data-sources/1/overrides/10');
        check('alias flow PUTs the extended column_aliases with the read version', !!put &&
          put.body.version === 2 &&
          put.body.column_aliases['Stock_*.xlsx/Sheet1'].SKU_Discription === 'SKU Description',
          JSON.stringify(put && put.body));

        // 11. Sparklines: N points for a rows series with a 5-run history.
        const rowsRow = dom.tbody.children.find(tr => tr.dataset.dqFinding === 'f05rowsdrop000005');
        click(rowsRow);
        const polylines = dom.drawerBody.querySelectorAll('polyline');
        check('sparkline(s) drawn for a finding with history', polylines.length >= 1,
          String(polylines.length));
        const pointCounts = polylines.map(p => (p.attrs.points || '').trim().split(/\s+/).length);
        check('a 5-run history series draws a 5-point sparkline', pointCounts.includes(5),
          JSON.stringify(pointCounts));

        // 12. Ack controls: schema kinds don't get the (permanently-live)
        // "until next run" button; other kinds do, and it POSTs the right body.
        const aliasRowAck = dom.tbody.children.find(tr => tr.dataset.dqFinding === 'f01schemacol0001');
        click(aliasRowAck);
        const schemaBtns = dom.drawerBody.querySelectorAll('button').map(b => b._text);
        check('schema finding does not offer "until next run"', !schemaBtns.includes('until next run'),
          JSON.stringify(schemaBtns));
        check('schema finding still offers dated acks', schemaBtns.includes('7 days') && schemaBtns.includes('forever'),
          JSON.stringify(schemaBtns));

        const ackRow = dom.tbody.children.find(tr => tr.dataset.dqFinding === 'f08reqfilegone008');
        ackRow.focus();   // simulate the keyboard-Enter open path (line ~200)
        click(ackRow);
        const untilBtn = dom.drawerBody.querySelectorAll('button').find(b => b._text === 'until next run');
        check('non-schema finding offers "until next run"', !!untilBtn);
        untilBtn.focus();   // simulate a real click's implicit focus-the-button
        fetchCalls.length = 0;
        click(untilBtn);

        return waitThen(() => waitThen(() => {
          const ackPost = fetchCalls.find(c => c.url === '/api/data-quality/acks');
          check('ack POSTs finding_id/source_id/until_run_id', !!ackPost &&
            ackPost.body.finding_id === 'f08reqfilegone008' && ackPost.body.source_id === 2 &&
            ackPost.body.until_run_id === 210 && ackPost.body.until_at === null,
            JSON.stringify(ackPost && ackPost.body));
          check('ack refetches the snapshot', fetchCalls.some(c => c.url === '/api/data-quality'),
            JSON.stringify(fetchCalls));
          // refetch() re-opens the drawer for the SAME finding right after the
          // ack, with document.activeElement still pointing at the "until
          // next run" button the click just focused (real-browser behaviour).
          // The return-focus target must stay the ROW that originally opened
          // the drawer, not get reassigned to that button — which the
          // re-render is about to tear out of the DOM, making Escape's later
          // focus() a no-op and dropping the keyboard user to <body>.
          document.dispatchEvent({ type: 'keydown', key: 'Escape' });
          check('Escape after an ack-triggered drawer re-open returns focus to the '
            + 'original opener, not the (now-destroyed) button that triggered the ack',
            document.activeElement === ackRow, String(document.activeElement));

          // 13. Unacknowledge DELETEs the ack row.
          dom.ackedToggle.checked = true;
          change(dom.ackedToggle);
          const ackedRow2 = dom.tbody.children.find(tr => tr.dataset.dqFinding === 'f04filestale00004');
          click(ackedRow2);
          const unackBtn = dom.drawerBody.querySelectorAll('button').find(b => b._text === 'Unacknowledge');
          check('acked finding offers Unacknowledge', !!unackBtn);
          fetchCalls.length = 0;
          click(unackBtn);

          return waitThen(() => waitThen(() => {
            const del = fetchCalls.find(c => c.url === '/api/data-quality/acks/501');
            check('unacknowledge DELETEs the ack id from the finding', !!del && del.opts.method === 'DELETE',
              JSON.stringify(fetchCalls));
            dom.ackedToggle.checked = false;
            change(dom.ackedToggle);

            // 14. Thresholds: blank stale field on a null-column source shows
            // the installation default as a placeholder, not a baked-in value;
            // saving sends the row's version for the optimistic-lock check.
            dom.sourceSel.value = '1';
            change(dom.sourceSel);
            const staleIn = dom.thresholdsPanel.querySelectorAll('input').find(i => i.min === '1' && i.max === '');
            check('null stale_after_days renders blank with the default as placeholder',
              !!staleIn && staleIn.value === '' && staleIn.placeholder === '7',
              JSON.stringify(staleIn && { value: staleIn.value, placeholder: staleIn.placeholder }));

            dom.sourceSel.value = '2';
            change(dom.sourceSel);
            const dropIn = dom.thresholdsPanel.querySelectorAll('input').find(i => i.max === '100');
            dropIn.value = '75';
            const saveBtn = dom.thresholdsPanel.querySelectorAll('button').find(b => b._text === 'Save thresholds');
            fetchCalls.length = 0;
            click(saveBtn);

            return waitThen(() => waitThen(() => {
              const put = fetchCalls.find(c => c.url === '/api/data-sources' && c.opts.method === 'PUT');
              check('thresholds PUT carries the row version for optimistic locking',
                !!put && put.body.version === 3 && put.body.kind === 'folder' &&
                put.body.key_name === 'Branches_Data' && put.body.thresholds.drop_pct === 75,
                JSON.stringify(put && put.body));
              dom.sourceSel.value = '';
              change(dom.sourceSel);

              // 15. Escape and the scrim both close the drawer.
              click(ackRow);
              check('drawer is open before Escape', dom.drawer.hidden === false);
              document.dispatchEvent({ type: 'keydown', key: 'Escape' });
              check('Escape closes the drawer', dom.drawer.hidden === true);

              click(ackRow);
              click(dom.scrim);
              check('clicking the scrim closes the drawer', dom.drawer.hidden === true);

        // 16. URL params pre-apply on a fresh load.
        global.location = {
          pathname: '/data-quality',
          search: '?source_id=2&generator=branches&finding=f07newvalues00007',
          href: 'http://x/data-quality?source_id=2&generator=branches&finding=f07newvalues00007',
        };
        dom = buildDom();
        fetchCalls.length = 0;
        (0, eval)(src);

        return waitThen(() => {
          check('URL source_id pre-applies to the select', dom.sourceSel.value === '2', dom.sourceSel.value);
          check('URL generator pre-applies to the select', dom.genSel.value === 'branches', dom.genSel.value);
          check('URL finding= opens the drawer', dom.drawer.hidden === false);

        // 17. The viewer's "Data Quality" link (?generator=X) can point at a
        // generator with ZERO findings — genSel never gets an <option> for
        // it (built only from generators that HAVE findings), so this is
        // the one case that can't be reached by clicking the select itself.
        global.location = {
          pathname: '/data-quality',
          search: '?generator=nosuchgen',
          href: 'http://x/data-quality?generator=nosuchgen',
        };
        dom = buildDom();
        fetchCalls.length = 0;
        (0, eval)(src);

        return waitThen(() => {
          check('empty state shows for a generator with no findings',
                dom.emptyEl.hidden === false);
          const emptyText = collectText(dom.emptyEl);
          check('empty state names the generator from the URL',
                emptyText.includes('nosuchgen'), emptyText);
          check('empty state names the generator but is not the generic "no filter matches" copy',
                !emptyText.includes('No findings match this filter'), emptyText);
          check('empty state is English, matching the rest of the page',
                emptyText.includes('No findings for generator'), emptyText);
          const clearLink = dom.emptyEl.children.find(c => c.tagName === 'BUTTON');
          check('empty state carries a clear-filter control', !!clearLink);
          if (clearLink) click(clearLink);
          check('clearing the filter drops the generator select back to empty',
                dom.genSel.value === '', dom.genSel.value);
          check('clearing the filter brings findings back',
                dom.tbody.children.length > 0, dom.tbody.children.length);
          check('the generic empty-state text is back once the filter is generic again '
                + '(source filter still narrows to zero, no viewer link involved)',
                (() => {
                  dom.sourceSel.value = '999999';
                  change(dom.sourceSel);
                  return collectText(dom.emptyEl).includes('No findings match this filter');
                })());

          console.log();
          if (FAILURES.length) {
            console.log(FAILURES.length + ' FAILED: ' + FAILURES.join(', '));
            process.exit(1);
          }
          console.log('ALL PASS');
        });
        });
              }));
            }));
          }));
      }));
    });
  });
}, 0);
