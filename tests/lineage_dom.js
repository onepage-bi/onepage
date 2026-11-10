/* Headless harness for static/js/lineage.js.
 *
 * There is no jsdom in this repo and no new dependency is worth one test, so
 * this is the smallest DOM the drawing code actually touches: elements with
 * classes, attributes, listeners and a FAKE box model (offsetWidth/Height
 * derived from child count, which is what the real cards vary by).
 *
 * It exists for the three claims that cannot be checked by reading the file:
 * edge contraction when a layer is folded, the writes_back edge staying out of
 * dagre, and 300 nodes laying out inside the 200 ms budget.
 *
 * Run: node tests/lineage_dom.js   (prints PASS/FAIL lines + ALL PASS)
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
// Pointer/wheel/keyboard events lineage.js calls ev.preventDefault() on;
// the existing click/change dispatches in this file get away without one
// because none of their handlers call it.
function evt(type, props) {
  return Object.assign({ type: type, preventDefault: function () {}, stopPropagation: function () {} }, props);
}

// ── DOM ──────────────────────────────────────────────────────────────
class ClassList {
  constructor(el) { this.el = el; this.set = new Set(); }
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
    this._class = new ClassList(this);
    this._text = '';
    this.hidden = false;
    this.tabIndex = -1;
    this.focused = false;
  }
  get classList() { return this._class; }
  set className(v) { this._class.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return this._class.value; }
  set textContent(v) { this.children = []; this._text = v === '' ? '' : String(v); }
  get textContent() { return this._text; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter(x => x !== c); return c; }
  get firstChild() { return this.children[0] || null; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[camel(k.slice(5))] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter(f => f !== fn); }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    (this.listeners[ev.type] || []).forEach(f => f.call(this, ev));
    return true;
  }
  // Enough of the focus/pointer-capture surface for lineage.js's own logic
  // (sectionIsActive, the pointerdown->pointerup drag threshold) to run
  // unmodified; none of it needs real hit-testing or a native activeElement.
  focus() { this.focused = true; if (typeof document !== 'undefined') document.activeElement = this; }
  blur() { this.focused = false; if (typeof document !== 'undefined' && document.activeElement === this) document.activeElement = null; }
  contains(el) { if (el === this) return true; return this.children.some(c => c.contains(el)); }
  setPointerCapture() {}
  releasePointerCapture() {}
  hasPointerCapture() { return true; }
  // The real cards grow with their content; a constant here would hide exactly
  // the overlap this measurement exists to prevent.
  get offsetWidth() { return 168 + Math.min(100, (this._text || '').length * 2 + this.children.length * 8); }
  get offsetHeight() { return 22 + this.children.length * 16; }
  querySelectorAll(sel) {
    const out = [];
    walk(this, n => { if (matches(n, sel)) out.push(n); });
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 900, height: 520 }; }
  get parentElement() { return this.parent; }
}
function camel(s) { return s.replace(/-([a-z])/g, (m, c) => c.toUpperCase()); }
function walk(n, fn) { n.children.forEach(c => { fn(c); walk(c, fn); }); }
function matches(n, sel) {
  const m = /^\[([-a-z]+)\]$/.exec(sel);
  if (m) return n.attrs[m[1]] !== undefined;
  const a = /^\[([-a-z]+)=["']?([^"'\]]+)["']?\]$/.exec(sel);
  if (a) return n.attrs[a[1]] === a[2];
  return false;
}

const docListeners = {};
const document = {
  createElement: t => new El(t),
  createElementNS: (ns, t) => new El(t, ns),
  getElementById: id => byIdMap[id] || null,
  addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
  dispatchEvent: ev => { (docListeners[ev.type] || []).forEach(f => f(ev)); return true; },
  querySelectorAll: sel => section.querySelectorAll(sel),
  querySelector: sel => section.querySelector(sel),
  activeElement: null,
};
const byIdMap = {};

function mk(tag, attrs) {
  const e = new El(tag);
  Object.keys(attrs || {}).forEach(k => e.setAttribute(k, attrs[k]));
  return e;
}
const section = mk('div', {});
section.dataset.lineageDev = '1';
byIdMap.lineage = section;
// #ds-root's data-sources is the per-source annotations payload (P4.2),
// present on the real page (data_sources.html). Starts with no sources at
// all, same as a page with nothing claimed yet; the annotation-scoping
// section below fills it in.
const dsRoot = mk('div', {});
byIdMap['ds-root'] = dsRoot;
const viewport = mk('div', { 'data-lin-viewport': '' });
const world = mk('div', { 'data-lin-world': '' });
const edges = mk('svg', { 'data-lin-edges': '' });
const cards = mk('div', { 'data-lin-cards': '' });
const skel = mk('div', { 'data-lin-skeleton': '' });
const err = mk('div', { 'data-lin-error': '' });
err.appendChild(mk('div', { 'data-lin-msg': '' }));
const zoom = mk('span', { 'data-lin-zoom-pct': '' });
const detail = mk('select', { 'data-lin-detail': '' });
detail.value = 'sheets';
const btnRetry = mk('button', { 'data-lin-act': 'retry' });
const btnFit = mk('button', { 'data-lin-act': 'fit' });
const btnReset = mk('button', { 'data-lin-act': 'reset' });
const btnUnfocus = mk('button', { 'data-lin-act': 'unfocus' });
const btnZoomIn = mk('button', { 'data-lin-zoom': 'in' });
const btnZoomOut = mk('button', { 'data-lin-zoom': 'out' });
const btnZoom100 = mk('button', { 'data-lin-zoom': '100' });
world.appendChild(edges); world.appendChild(cards);
viewport.appendChild(world);
section.appendChild(viewport); section.appendChild(skel); section.appendChild(err);
section.appendChild(zoom); section.appendChild(detail);
section.appendChild(btnRetry); section.appendChild(btnFit); section.appendChild(btnReset);
section.appendChild(btnUnfocus);
section.appendChild(btnZoomIn); section.appendChild(btnZoomOut); section.appendChild(btnZoom100);

class CustomEventShim {
  constructor(type, init) { this.type = type; this.detail = (init || {}).detail; }
}

// ── Globals lineage.js expects ───────────────────────────────────────
let nextGraph = null;              // what the next fetch answers with
global.document = document;
global.CustomEvent = CustomEventShim;
global.location = { href: 'http://x/data-sources' };
global.history = { replaceState: (a, b, u) => { global.location.href = String(u); } };
global.fetch = async () => (nextGraph
  ? { ok: true, status: 200, json: async () => nextGraph }
  : { ok: false, status: 501 });
const debugLines = [];
const realDebug = console.debug;
console.debug = msg => { debugLines.push(String(msg)); };

const win = { document: document };
new Function('window', 'self', 'module', 'exports',
  fs.readFileSync(path.join(ROOT, 'static/js/vendor/dagre.min.js'), 'utf8')
)(win, win, undefined, undefined);
global.dagre = win.dagre;

const SAMPLE = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests/fixtures/lineage_sample.json'), 'utf8'));

// ── Go ───────────────────────────────────────────────────────────────
const src = fs.readFileSync(path.join(ROOT, 'static/js/lineage.js'), 'utf8');
(0, eval)(src);

const shown = () => cards.children.filter(c => c.style.display !== 'none');
const cardFor = id => cards.children.find(c => c.dataset.node === id);
const edgePaths = () => edges.children.filter(c => c.tagName === 'PATH');
const lastLayout = () => debugLines.filter(l => l.startsWith('lineage: laid out')).slice(-1)[0] || '';

setTimeout(() => {
  // 1. The fixture draws, at full detail, every node it declares.
  check('sample fixture renders every node', shown().length === SAMPLE.nodes.length,
    shown().length + ' of ' + SAMPLE.nodes.length);
  check('dev fallback used the fixture (skeleton down)', skel.hidden === true && err.hidden === true);

  // 2. writes_back is drawn, dashed, and did NOT drag the folder rightwards:
  //    src:1 must still sit left of the output that writes back into it.
  const wb = edgePaths().filter(p => (p.attrs.class || '').includes('kind-writes_back'));
  check('writes_back edge is drawn separately', wb.length === 1, String(wb.length));
  check('writes_back edge carries an arrow head', wb[0] && wb[0].attrs['marker-end'] === 'url(#lin-arrow)');
  const xOf = id => parseFloat(/translate\(([-\d.]+)px/.exec(cardFor(id).style.transform)[1]);
  check('writes_back stayed out of the ranking', xOf('src:1') < xOf('out:rdcstock:wb:sl_unassigned_reasons.xlsx'));

  // 3. Folding a layer must not lose the dependency, only what it passes
  //    through: folders detail keeps folder -> generator.
  detail.value = 'folders';
  detail.dispatchEvent({ type: 'change', target: detail });
  const hiddenKinds = shown().length;
  check('detail=folders hides patterns and sheets', hiddenKinds === SAMPLE.nodes.length - 5,
    String(hiddenKinds));
  check('folded edges are contracted, not dropped',
    edgePaths().some(p => (p.attrs.class || '').includes('is-via')));
  const edgeCount = edgePaths().length;
  check('every visible node still has its edges', edgeCount >= 9, String(edgeCount));

  detail.value = 'sheets';
  detail.dispatchEvent({ type: 'change', target: detail });
  check('detail=sheets restores every node', shown().length === SAMPLE.nodes.length);

  // 4. Focus is the upstream+downstream closure of one node, nothing else.
  let opened = null;
  document.addEventListener('ds:open', e => { opened = e.detail; });
  cardFor('src:2').dispatchEvent({ type: 'click', target: cardFor('src:2') });
  check('clicking a folder opens the drawer with kind:name',
    opened && opened.key === 'folder:Reasons_Catalog', opened && opened.key);
  check('focus lights the closure', cardFor('rep:d1').classList.contains('hot'));
  // rep:d2 IS in the closure and must stay lit: the write-back edge makes the
  // OPS report a real downstream of this catalogue. The unrelated one is d3.
  check('focus keeps the write-back closure', cardFor('rep:d2').classList.contains('hot'));
  check('focus dims what is unrelated', !cardFor('rep:d3').classList.contains('hot'));
  check('focus is deep-linkable', global.location.href.includes('focus=src%3A2')
    || global.location.href.includes('focus=src:2'), global.location.href);

  // 5. ds:filter dims everything no surviving source reaches.
  document.dispatchEvent(new CustomEventShim('ds:filter', { detail: { keys: new Set(['folder:Stock_RDC']) } }));
  check('ds:filter keeps the reachable', !cardFor('gen:rdcstock').classList.contains('out'));
  check('ds:filter drops the unreachable', cardFor('gen:dwhreject').classList.contains('out'));
  document.dispatchEvent(new CustomEventShim('ds:filter', {
    detail: { keys: new Set(['folder:Stock_RDC', 'folder:Reasons_Catalog', 'greenplum:gp_dwh']) },
  }));
  check('ds:filter with everything selected is not a filter',
    !cardFor('gen:dwhreject').classList.contains('out'));

  // 6. ds:saved retires the "by name" chip without a reload.
  document.dispatchEvent(new CustomEventShim('ds:saved', {
    detail: { key: 'folder:Reasons_Catalog', source: { declared_path: '\\\\share\\R', label: 'x' } },
  }));
  const chips = cardFor('src:2').querySelectorAll('[class]');
  check('ds:saved repaints the card',
    JSON.stringify(cardFor('src:2').children.map(c => c.className)).includes('lin-card-path'));

  // 7. Sticky focus survives a drag, dies to a jitter-free background click
  //    or Escape; wheel semantics split on the modifier; toolbar/keyboard zoom.
  btnUnfocus.dispatchEvent(evt('click', { target: btnUnfocus }));
  cardFor('src:2').dispatchEvent(evt('click', { target: cardFor('src:2') }));
  check('click focuses the card', cardFor('rep:d1').classList.contains('hot'));
  check('clear-focus button becomes visible once a focus is active', btnUnfocus.hidden === false);

  // The owner's report, verbatim: focus a card, then start panning - the
  // focus must not be the thing that breaks.
  viewport.dispatchEvent(evt('pointerdown', { target: viewport, button: 0, pointerId: 1, clientX: 100, clientY: 100 }));
  viewport.dispatchEvent(evt('pointermove', { target: viewport, pointerId: 1, clientX: 130, clientY: 100 }));
  viewport.dispatchEvent(evt('pointerup', { target: viewport, pointerId: 1, clientX: 130, clientY: 100 }));
  check('a 30px drag after focusing a card keeps the focus',
    cardFor('rep:d1').classList.contains('hot'));

  // A press-and-release that never cleared the 4px threshold is a click, and
  // it started on empty canvas, so it clears the focus.
  viewport.dispatchEvent(evt('pointerdown', { target: viewport, button: 0, pointerId: 2, clientX: 50, clientY: 50 }));
  viewport.dispatchEvent(evt('pointermove', { target: viewport, pointerId: 2, clientX: 52, clientY: 51 }));
  viewport.dispatchEvent(evt('pointerup', { target: viewport, pointerId: 2, clientX: 52, clientY: 51 }));
  check('a <=4px jitter click on background clears focus',
    !cardFor('rep:d1').classList.contains('hot'));
  check('clear-focus button hides itself once focus is gone', btnUnfocus.hidden === true);

  // A cancelled press on the background (browser takes over a touch scroll,
  // among other reasons) must not be mistaken for a click - it never got a
  // pointerup, so it must not clear a focus either.
  cardFor('src:2').dispatchEvent(evt('click', { target: cardFor('src:2') }));
  check('re-focus for the pointercancel check', cardFor('rep:d1').classList.contains('hot'));
  viewport.dispatchEvent(evt('pointerdown', { target: viewport, button: 0, pointerId: 3, clientX: 60, clientY: 60 }));
  viewport.dispatchEvent(evt('pointercancel', { target: viewport, pointerId: 3, clientX: 60, clientY: 60 }));
  check('a cancelled press on background does not clear focus',
    cardFor('rep:d1').classList.contains('hot'));

  // Still focused from the pointercancel check above (that click did not
  // toggle it) - a real click on a tabIndex=0 element moves DOM focus there
  // too, which is what gates Escape below (hover is not enough, on purpose -
  // see lineage.js).
  cardFor('src:2').focus();
  check('re-focus for the Escape check', cardFor('rep:d1').classList.contains('hot'));
  document.dispatchEvent(evt('keydown', { target: cardFor('src:2'), key: 'Escape' }));
  check('Escape clears focus when it lands inside the section',
    !cardFor('rep:d1').classList.contains('hot'));

  // But Escape must NOT clear a focus that survives only via mouse hover with
  // nothing in the section actually focused - that is what let dismissing the
  // drawer (or any other Escape-consuming UI) silently wipe the lineage trace.
  cardFor('src:2').dispatchEvent(evt('click', { target: cardFor('src:2') }));
  cardFor('src:2').focus();
  check('re-focus for the hover-only check', cardFor('rep:d1').classList.contains('hot'));
  cardFor('src:2').blur();
  viewport.dispatchEvent(evt('mouseenter', { target: viewport }));
  document.dispatchEvent(evt('keydown', { target: document.body, key: 'Escape' }));
  check('hover alone does not arm Escape', cardFor('rep:d1').classList.contains('hot'));
  const transBeforeHoverFit = world.style.transform;
  document.dispatchEvent(evt('keydown', { target: viewport, key: '0' }));
  check('hover alone still arms 0/fit', world.style.transform !== transBeforeHoverFit,
    world.style.transform);

  // Ctrl/Cmd+0 (and +/-) are the BROWSER's own zoom shortcuts; the graph must
  // leave them alone instead of also refitting/zooming underneath them.
  const transBeforeCtrlZero = world.style.transform;
  document.dispatchEvent(evt('keydown', { target: viewport, key: '0', ctrlKey: true }));
  check('ctrl+0 does not also refit the graph', world.style.transform === transBeforeCtrlZero,
    world.style.transform);
  const zoomBeforeCtrlPlus = zoom.textContent;
  document.dispatchEvent(evt('keydown', { target: viewport, key: '=', ctrlKey: true }));
  check('ctrl+= does not also zoom the graph', zoom.textContent === zoomBeforeCtrlPlus,
    zoom.textContent);
  document.dispatchEvent(evt('keydown', { target: viewport, key: '0', metaKey: true }));
  check('cmd+0 (metaKey) does not also refit the graph', world.style.transform === transBeforeCtrlZero,
    world.style.transform);
  cardFor('src:2').focus();
  document.dispatchEvent(evt('keydown', { target: cardFor('src:2'), key: 'Escape' })); // clean up for what follows
  check('focus back on the card lets Escape clear it again',
    !cardFor('rep:d1').classList.contains('hot'));

  const zoomBeforePan = zoom.textContent;
  const transBeforePan = world.style.transform;
  viewport.dispatchEvent(evt('wheel', {
    target: viewport, deltaX: 24, deltaY: 15, deltaMode: 0, clientX: 10, clientY: 10,
  }));
  check('plain wheel pans (translate changed)', world.style.transform !== transBeforePan,
    world.style.transform);
  check('plain wheel leaves the zoom % alone', zoom.textContent === zoomBeforePan,
    zoom.textContent + ' vs ' + zoomBeforePan);

  viewport.dispatchEvent(evt('wheel', {
    target: viewport, deltaX: 0, deltaY: -200, deltaMode: 0, ctrlKey: true, clientX: 10, clientY: 10,
  }));
  check('ctrl+wheel zooms (percentage changed)', zoom.textContent !== zoomBeforePan, zoom.textContent);

  // deltaMode 2 (page units) scales by the viewport's own width/height, not
  // one axis borrowing the other's dimension - a page-mode horizontal tick
  // must not move X by the viewport's HEIGHT (900x520 in this harness).
  function txOf(t) { return parseFloat(/translate\(([-\d.]+)px/.exec(t)[1]); }
  const xBeforePageWheel = txOf(world.style.transform);
  viewport.dispatchEvent(evt('wheel', {
    target: viewport, deltaX: 1, deltaY: 0, deltaMode: 2, clientX: 10, clientY: 10,
  }));
  const dxPageWheel = txOf(world.style.transform) - xBeforePageWheel;
  check('deltaMode=2 horizontal pan scales by viewport width, not height',
    Math.abs(dxPageWheel + 900) < 0.01, dxPageWheel);

  const zoomBeforePlus = zoom.textContent;
  document.dispatchEvent(evt('keydown', { target: viewport, key: '+' }));
  check('"+" key zooms in', zoom.textContent !== zoomBeforePlus, zoom.textContent);

  const zoomBeforeBtn = zoom.textContent;
  btnZoomOut.dispatchEvent(evt('click', { target: btnZoomOut }));
  check('toolbar zoom-out button changes scale', zoom.textContent !== zoomBeforeBtn, zoom.textContent);
  btnZoom100.dispatchEvent(evt('click', { target: btnZoom100 }));
  check('toolbar 100% button resets scale to 100%', zoom.textContent === '100%', zoom.textContent);

  // 8. Stages (P4.1): folded by default under the generator, a twisty
  //    expands the chain, file->stage edges are dashed 'read_order', and the
  //    focus closure reaches through them.
  nextGraph = stageGraph();
  btnRetry.dispatchEvent({ type: 'click', target: btnRetry });
  setTimeout(() => {
    check('a stage card does not exist until its generator is expanded',
      !cardFor('stage:1:0') && !cardFor('stage:1:1'));
    const genCard = cardFor('gen:gen1');
    check('the generator card shows a stage-count chip',
      genCard.children[0].children.some(c => (c.className || '').includes('lin-chip')
        && c.textContent === '2 stages'));
    const twisty = genCard.children[0].children.find(c => c.tagName === 'BUTTON');
    check('the generator card has a twisty to expand its stage chain', !!twisty);
    twisty.dispatchEvent(evt('click', { target: twisty }));
    check('expanding the generator reveals its stages',
      !!cardFor('stage:1:0') && !!cardFor('stage:1:1')
      && cardFor('stage:1:0').style.display !== 'none');
    const readOrder = edgePaths().filter(p => (p.attrs.class || '').includes('kind-read_order'));
    check('the file->stage edge is drawn dashed as read_order', readOrder.length === 1,
      String(readOrder.length));
    cardFor('gen:gen1').dispatchEvent(evt('click', { target: cardFor('gen:gen1') }));
    check('focusing the generator lights its whole stage chain',
      cardFor('stage:1:0').classList.contains('hot') && cardFor('stage:1:1').classList.contains('hot'));
    check('focusing the generator lights the report downstream of the chain',
      cardFor('rep:g1').classList.contains('hot'));
    btnUnfocus.dispatchEvent(evt('click', { target: btnUnfocus }));

    // 9. Annotation scoping (P4.2) + baked-description fallback (P4.2 fix,
    //    finding #1): a per-source Annotation belongs to the source it was
    //    written for, never to another source that merely shares the same
    //    (match, sheet, column) - but when the CONNECTED source has no
    //    Annotation row of its own, the graph's own baked description
    //    (P3.6 manifest field, never source-scoped) must still show, not
    //    disappear - see lineage.js's "-- Annotations --".
    dsRoot.dataset.sources = JSON.stringify([
      { source_id: 201, annotations: [] },
      { source_id: 202, annotations: [
        { match: 'Ann_*.xlsx', sheet: 'Sheet1', column: null, description: 'LEAKED' },
      ] },
    ]);
    nextGraph = annGraph();
    btnRetry.dispatchEvent({ type: 'click', target: btnRetry });
    setTimeout(() => {
      const titleOf = card => card.children[0].children.find(
        c => (c.className || '').includes('lin-card-title')).title;
      const sheetId = 'sheet:gen2:Ann_*.xlsx:Sheet1';
      const t1 = titleOf(cardFor(sheetId));
      check('a description on an unrelated (unconnected) source does not leak onto this sheet',
        !t1.includes('LEAKED'), t1);
      check("the connected source's empty annotation list falls back to the graph's baked description",
        t1.includes('Manifest sheet note'), t1);

      dsRoot.dataset.sources = JSON.stringify([
        { source_id: 201, annotations: [
          { match: 'Ann_*.xlsx', sheet: 'Sheet1', column: null, description: 'Real note for A' },
          { match: 'Ann_*.xlsx', sheet: 'Sheet1', column: 'Col1', description: 'Real col note' },
        ] },
        { source_id: 202, annotations: [
          { match: 'Ann_*.xlsx', sheet: 'Sheet1', column: null, description: 'LEAKED' },
        ] },
      ]);
      btnRetry.dispatchEvent({ type: 'click', target: btnRetry });
      setTimeout(() => {
        const sheetCard = cardFor(sheetId);
        const t2 = titleOf(sheetCard);
        check("the sheet's own source annotation overrides the graph's baked description",
          t2.includes('Real note for A') && !t2.includes('LEAKED') && !t2.includes('Manifest sheet note'), t2);
        const colsWrap = sheetCard.children.find(c => (c.className || '').includes('lin-cols'));
        const colTitle = colsWrap.children[0].title;
        check('a column description is scoped to the same source, not any source with the key',
          colTitle.includes('Real col note') && !colTitle.includes('LEAKED_COL')
          && !colTitle.includes('Manifest column note'), colTitle);

        // 9b. Deep link to a stage node (P4.1/P4.2 fix, finding #2): the
        // generator's stage chain starts folded (see build()), so a link
        // into it must unfold the chain and land focus on the stage card,
        // exactly like the existing sheet deep-link case does.
        nextGraph = stageGraph();
        global.location.href = 'http://x/data-sources?focus=stage%3A1%3A0';
        btnRetry.dispatchEvent({ type: 'click', target: btnRetry });
        setTimeout(() => {
          check('deep link ?focus=stage:1:0 unfolds the stage chain and renders the card',
            !!cardFor('stage:1:0') && cardFor('stage:1:0').style.display !== 'none');
          check('deep link ?focus=stage:1:0 focuses that stage card',
            cardFor('stage:1:0').focused === true);
          global.location.href = 'http://x/data-sources';

        // 9c. Switching the detail select does not blow open every staged
        // generator's fold (minor finding): a per-pattern fold IS cleared by
        // "Sheets", a per-generator stage fold is not. Reload plain (no
        // focus param) first so the chain starts folded again, per build()'s
        // default - 9b left it unfolded via the deep link.
        nextGraph = stageGraph();
        btnRetry.dispatchEvent({ type: 'click', target: btnRetry });
        setTimeout(() => {
        detail.value = 'files';
        detail.dispatchEvent({ type: 'change', target: detail });
        detail.value = 'sheets';
        detail.dispatchEvent({ type: 'change', target: detail });
        check('switching back to "Sheets" leaves a staged generator folded',
          cardFor('stage:1:0').style.display === 'none');
        const gTwisty = cardFor('gen:gen1').children[0].children.find(c => c.tagName === 'BUTTON');
        gTwisty.dispatchEvent(evt('click', { target: gTwisty }));
        check('its twisty still expands the chain afterwards',
          cardFor('stage:1:0').style.display !== 'none');

        // 9d. A stage node with no generator name is shown, unattached,
        // rather than permanently swallowed (minor finding).
        const orphan = stageGraph();
        delete orphan.nodes.find(n => n.id === 'stage:1:1').data.generator;
        nextGraph = orphan;
        btnRetry.dispatchEvent({ type: 'click', target: btnRetry });
        setTimeout(() => {
          check('a stage node with no generator name is drawn, not swallowed',
            !!cardFor('stage:1:1') && cardFor('stage:1:1').style.display !== 'none');

        // 9.5. schema_json's per-sheet `reason` / top-level `error` (commit
        // 633eaaa): a sheet with no observed header gets a "header not found"
        // chip carrying the reason in its title, and a pattern whose files
        // failed to open at all gets a muted "N files without a schema" chip.
        nextGraph = schemaGraph();
        btnRetry.dispatchEvent({ type: 'click', target: btnRetry });
        setTimeout(() => {
          const sheetChips = cardFor('sheet:gen3:Bad_*.xlsx:Sheet1')
            .children[0].children.filter(c => (c.className || '').includes('lin-chip'));
          const hdrChip = sheetChips.find(c => c.textContent === 'header not found');
          check('a sheet with header_missing gets the "header not found" chip', !!hdrChip);
          check('the chip title carries the reason',
            !!hdrChip && hdrChip.title === 'no header in first 20 rows',
            hdrChip && hdrChip.title);

          const patChips = cardFor('pat:gen3:Bad_*.xlsx')
            .children[0].children.filter(c => (c.className || '').includes('lin-chip'));
          const errChip = patChips.find(c => (c.textContent || '').includes('without a schema'));
          check('a pattern with schema_errors gets a "N files without a schema" chip', !!errChip);
          check('the schema-error chip text names the file count and the error',
            !!errChip && errChip.textContent === '2 files without a schema: workbook is corrupt',
            errChip && errChip.textContent);
          check('the schema-error chip is muted, not a warning',
            !!errChip && (errChip.className || '').includes('is-muted'), errChip && errChip.className);
          check('the schema-error chip title carries the full error text',
            !!errChip && errChip.title === '2 file(s) without schema: workbook is corrupt',
            errChip && errChip.title);

          const okSheetChips = cardFor('sheet:gen3:Bad_*.xlsx:Good')
            .children[0].children.filter(c => (c.className || '').includes('lin-chip'));
          check('a sheet with columns observed does NOT get the header_missing chip',
            !okSheetChips.some(c => c.textContent === 'header not found'));

        // 10. The budget: 300 nodes under 200 ms.
        nextGraph = bigGraph(300);
        btnRetry.dispatchEvent({ type: 'click', target: btnRetry });
        setTimeout(() => {
          // Above 12 patterns the graph opens with its sheets folded; that IS
          // the default under test, so the budget is measured fully expanded.
          const folded = parseInt(/laid out (\d+) nodes/.exec(lastLayout())[1], 10);
          check('a wide graph opens folded', folded < 300, String(folded));
          detail.value = 'sheets';
          detail.dispatchEvent({ type: 'change', target: detail });
          const line = lastLayout();
          const ms = parseFloat(/in ([\d.]+) ms/.exec(line)[1]);
          const n = parseInt(/laid out (\d+) nodes/.exec(line)[1], 10);
          check('300-node graph laid out', n === 300, String(n) + ' / ' + line);
          // The budget is a property of the hardware, not the code: the CI
          // runner is three times slower than a laptop, and a strict threshold
          // went red there without finding any regression. The multiplier is
          // OP_TEST_TIME_FACTOR; the measurement is printed in full either way.
          var budget = 200 * (parseFloat(process.env.OP_TEST_TIME_FACTOR) || 1);
          check('300 nodes under the ' + budget + ' ms budget', ms < budget, line);
          realDebug('  ' + line);

          console.log(FAILURES.length ? 'FAILURES: ' + FAILURES.join(', ') : 'ALL PASS');
          process.exit(FAILURES.length ? 1 : 0);
        }, 0);
        }, 0);
        }, 0);
        }, 0);
        }, 0);
      }, 0);
    }, 0);
  }, 0);
}, 0);

/* A generator whose latest run reported a two-step onepage.stage() chain
   (P4.1): one folder feeding one pattern feeding one sheet feeding stage 0
   ("load"), stage 0 feeding stage 1 ("clean") by call order (no declared
   `inputs`), stage 1 feeding the generator, publishing one report. */
function stageGraph() {
  return {
    meta: {},
    nodes: [
      { id: 'src:s1', layer: 0, kind: 'folder', label: 'FolderS', data: { source_id: 301, files: 3 } },
      { id: 'pat:gen1:Data_*.xlsx', layer: 1, kind: 'pattern', label: 'Data_*.xlsx',
        data: { role: 'data', matched_files: 3 } },
      { id: 'sheet:gen1:Data_*.xlsx:Sheet1', layer: 2, kind: 'sheet', label: 'Sheet1',
        data: { columns: [{ name: 'A', declared: true, observed: true, consumed: true }] } },
      { id: 'stage:1:0', layer: 2.5, kind: 'stage', label: 'load',
        data: { generator: 'gen1', order: 0, rows: 100, columns: ['A', 'B'], inputs: null, ms: 12 } },
      { id: 'stage:1:1', layer: 2.5, kind: 'stage', label: 'clean',
        data: { generator: 'gen1', order: 1, rows: 90, columns: ['A', 'B', 'C'], inputs: null, ms: 8 } },
      { id: 'gen:gen1', layer: 3, kind: 'generator', label: 'gen1',
        data: { version: 1, engine: 'browser', stages: 2 } },
      { id: 'out:gen1:html', layer: 4, kind: 'output', label: 'dashboard.html', data: { slot: null } },
      { id: 'rep:g1', layer: 5, kind: 'report', label: 'Report G1',
        data: { dashboard_id: 'dg1', status: 'success' } },
    ],
    edges: [
      { from: 'src:s1', to: 'pat:gen1:Data_*.xlsx', kind: 'matches' },
      { from: 'pat:gen1:Data_*.xlsx', to: 'sheet:gen1:Data_*.xlsx:Sheet1', kind: 'contains' },
      { from: 'sheet:gen1:Data_*.xlsx:Sheet1', to: 'stage:1:0', kind: 'read_order' },
      { from: 'stage:1:0', to: 'stage:1:1', kind: 'feeds' },
      { from: 'stage:1:1', to: 'gen:gen1', kind: 'reads' },
      { from: 'gen:gen1', to: 'out:gen1:html', kind: 'writes' },
      { from: 'out:gen1:html', to: 'rep:g1', kind: 'publishes' },
    ],
  };
}

/* One folder (source_id 201) feeding one pattern/sheet for gen2. The graph's
   OWN baked description/column-description ("Manifest sheet note"/"Manifest
   column note") is what the server bakes from the P3.6 manifest author's own
   field - always safe to show, never source-scoped - so it must survive as a
   fallback when source 201 (the one actually connected) has no Annotation
   row of its own (P4.2 finding #1). Source 202's conflicting entries stand
   in for a DIFFERENT, unconnected source that happens to share the same
   match/sheet/column; the annotation-scoping section checks 201's own real
   annotation, once it exists, overrides both the baked value and 202. */
function annGraph() {
  return {
    meta: {},
    nodes: [
      { id: 'src:A', layer: 0, kind: 'folder', label: 'FolderA', data: { source_id: 201, files: 1 } },
      { id: 'pat:gen2:Ann_*.xlsx', layer: 1, kind: 'pattern', label: 'Ann_*.xlsx',
        data: { role: 'data', matched_files: 1 } },
      { id: 'sheet:gen2:Ann_*.xlsx:Sheet1', layer: 2, kind: 'sheet', label: 'Sheet1', data: {
        description: 'Manifest sheet note',
        columns: [{ name: 'Col1', declared: true, observed: true, consumed: true, description: 'Manifest column note' }],
      } },
      { id: 'gen:gen2', layer: 3, kind: 'generator', label: 'gen2', data: { version: 1, engine: 'browser' } },
    ],
    edges: [
      { from: 'src:A', to: 'pat:gen2:Ann_*.xlsx', kind: 'matches' },
      { from: 'pat:gen2:Ann_*.xlsx', to: 'sheet:gen2:Ann_*.xlsx:Sheet1', kind: 'contains' },
      { from: 'sheet:gen2:Ann_*.xlsx:Sheet1', to: 'gen:gen2', kind: 'reads' },
    ],
  };
}

/* A workspace-shaped graph: folders feeding patterns feeding sheets feeding
   generators feeding outputs feeding reports, in the proportions the real one
   has (many patterns per folder, few generators). */
/* One pattern with two sheets: 'Sheet1' whose header build_graph could never
   find (header_missing on the sheet node) and 'Good' whose header read fine
   (no chip) - plus the pattern's own schema_errors tally from two files that
   would not open at all. Mirrors what app/services/lineage.py's
   _add_sheet_nodes / the pat_data schema_errors block emit from
   static/py/_lineage.py sniff()'s per-sheet "reason" and top-level "error". */
function schemaGraph() {
  return {
    meta: {},
    nodes: [
      { id: 'src:s3', layer: 0, kind: 'folder', label: 'FolderS3', data: { source_id: 303, files: 2 } },
      { id: 'pat:gen3:Bad_*.xlsx', layer: 1, kind: 'pattern', label: 'Bad_*.xlsx',
        data: { role: 'data', matched_files: 2,
                schema_errors: { 'workbook is corrupt': 2 } } },
      { id: 'sheet:gen3:Bad_*.xlsx:Sheet1', layer: 2, kind: 'sheet', label: 'Sheet1',
        data: { columns: [], header_missing: true, reason: 'no header in first 20 rows' } },
      { id: 'sheet:gen3:Bad_*.xlsx:Good', layer: 2, kind: 'sheet', label: 'Good',
        data: { columns: [{ name: 'A', declared: true, observed: true, consumed: true }] } },
      { id: 'gen:gen3', layer: 3, kind: 'generator', label: 'gen3', data: { version: 1, engine: 'browser' } },
      { id: 'out:gen3:html', layer: 4, kind: 'output', label: 'dashboard.html', data: { slot: null } },
      { id: 'rep:g3', layer: 5, kind: 'report', label: 'Report G3', data: { dashboard_id: 'dg3', status: 'success' } },
    ],
    edges: [
      { from: 'src:s3', to: 'pat:gen3:Bad_*.xlsx', kind: 'matches' },
      { from: 'pat:gen3:Bad_*.xlsx', to: 'sheet:gen3:Bad_*.xlsx:Sheet1', kind: 'contains' },
      { from: 'pat:gen3:Bad_*.xlsx', to: 'sheet:gen3:Bad_*.xlsx:Good', kind: 'contains' },
      { from: 'sheet:gen3:Bad_*.xlsx:Sheet1', to: 'gen:gen3', kind: 'reads' },
      { from: 'sheet:gen3:Bad_*.xlsx:Good', to: 'gen:gen3', kind: 'reads' },
      { from: 'gen:gen3', to: 'out:gen3:html', kind: 'writes' },
      { from: 'out:gen3:html', to: 'rep:g3', kind: 'publishes' },
    ],
  };
}

function bigGraph(total) {
  const nodes = [], edges = [];
  let i = 0;
  const push = (id, layer, kind, label, data) => { nodes.push({ id, layer, kind, label, data: data || {} }); i++; };
  const nf = 12, ng = 10;
  for (let f = 0; f < nf; f++) {
    push('src:' + f, 0, 'folder', 'Folder_' + f, { files: 140, bytes: 5e8, data_age_days: f % 30, shared: f % 3 === 0 });
  }
  for (let g = 0; g < ng; g++) {
    push('gen:' + g, 3, 'generator', 'gen' + g, { version: 2, engine: 'browser' });
    push('out:' + g, 4, 'output', 'dashboard.html', { slot: null });
    push('rep:' + g, 5, 'report', 'Report ' + g, { dashboard_id: 'd' + g, status: 'success' });
    edges.push({ from: 'gen:' + g, to: 'out:' + g, kind: 'writes' });
    edges.push({ from: 'out:' + g, to: 'rep:' + g, kind: 'publishes' });
  }
  let p = 0;
  while (i < total) {
    const g = p % ng, f = p % nf;
    push('pat:' + p, 1, 'pattern', 'File_' + p + '_*.xlsx', { role: 'data', matched_files: 40 });
    edges.push({ from: 'src:' + f, to: 'pat:' + p, kind: 'matches' });
    if (i < total) {
      push('sheet:' + p, 2, 'sheet', 'Sheet' + p, {
        columns: Array.from({ length: 8 }, (_, c) => ({
          name: 'Col_' + c, declared: true, observed: c !== 3, consumed: c < 2 ? true : null,
        })),
      });
      edges.push({ from: 'pat:' + p, to: 'sheet:' + p, kind: 'contains' });
      edges.push({ from: 'sheet:' + p, to: 'gen:' + g, kind: 'reads' });
    }
    p++;
  }
  return { meta: {}, nodes: nodes.slice(0, total), edges: edges };
}
