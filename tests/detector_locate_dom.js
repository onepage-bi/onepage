/* op_locate — the controller's JS, run for real in a fake DOM.
 *
 * The code under test lives inside a Python string (`_ANNOTATE_CONTROLLER` in
 * app/services/detector.py), so it is extracted from the source file rather
 * than required: that keeps this harness honest — it tests the exact bytes the
 * server injects into a report, escaping included, not a copy.
 *
 * No jsdom, no dependency: the smallest DOM the controller touches — elements
 * with children, textContent, classList, tagName — plus a window/document with
 * listeners and a parent that records postMessage.
 *
 * Run: node tests/detector_locate_dom.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const FAILURES = [];
function check(label, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + label + (!cond && extra !== undefined ? '  <-- ' + extra : ''));
  if (!cond) FAILURES.push(label);
}

// ── extract the controller from detector.py ──────────────────────────────
const PY = fs.readFileSync(path.join(ROOT, 'app', 'services', 'detector.py'), 'utf8');
const START = '_ANNOTATE_CONTROLLER = """';
const from = PY.indexOf(START);
if (from < 0) { console.log('FAIL cannot find _ANNOTATE_CONTROLLER'); process.exit(1); }
const body = PY.slice(from + START.length);
const raw = body.slice(0, body.indexOf('"""'));
// The Python literal is not raw: every backslash the JS needs is written
// doubled in the source, so undoubling gives back the injected bytes.
const SRC = raw.replace(/\\\\/g, '\\');
const CODE = SRC.slice(SRC.indexOf('>') + 1, SRC.lastIndexOf('</script>'));
check('extracted controller carries op_locate', CODE.indexOf("m.type==='op_locate'") > 0);

// ── fake DOM ─────────────────────────────────────────────────────────────
class ClassList {
  constructor() { this.set = new Set(); }
  add() { for (const c of arguments) if (c) this.set.add(c); }
  remove() { for (const c of arguments) this.set.delete(c); }
  contains(c) { return this.set.has(c); }
}
class El {
  constructor(tag, text) {
    this.tagName = tag;            // as written: 'DIV', 'text' (svg), …
    this.nodeType = 1;
    this.children = [];
    this.parentNode = null;
    this.classList = new ClassList();
    this._text = text || '';
    this.style = { cssText: '' };
    this.id = '';
    this.scrolled = null;
    this.listeners = {};
  }
  get textContent() {
    if (!this.children.length) return this._text;
    return this._text + this.children.map(c => c.textContent).join('');
  }
  set textContent(v) { this.children = []; this._text = String(v); }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter(x => x !== c); return c; }
  getAttribute() { return null; }
  setAttribute() {}
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  getBoundingClientRect() { return { left: 0, top: 0, width: 10, height: 10 }; }
  scrollIntoView(opt) { this.scrolled = opt; }
}
function el(tag, text, kids) {
  const n = new El(tag, text);
  (kids || []).forEach(k => n.appendChild(k));
  return n;
}

const posted = [];
let doc, win, sandbox;
const timers = [];

function boot(bodyEl) {
  doc = {
    documentElement: el('HTML'),
    body: bodyEl,
    head: el('HEAD'),
    listeners: {},
    createElement: (t) => el(t.toUpperCase()),
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
  };
  doc.documentElement.appendChild(doc.head);
  doc.documentElement.appendChild(bodyEl);
  posted.length = 0; timers.length = 0;
  win = {
    listeners: {},
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    parent: { postMessage: (m) => posted.push(m) },
    scrollX: 0, scrollY: 0,
  };
  win.window = win;
  sandbox = {
    window: win, document: doc, Math, Object, String, Number, Array, RegExp, JSON,
    parseFloat, parseInt, isFinite, Date,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (h) => { if (h) timers[h - 1] = null; },
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox, { filename: 'annotate-controller.js' });
}
function locate(msg) {
  (win.listeners.message || []).forEach(fn => fn({ data: msg }));
  return posted[posted.length - 1];
}

// ── a small report ───────────────────────────────────────────────────────
function report() {
  return el('BODY', '', [
    el('H1', 'Sales by region'),
    el('SCRIPT', 'var revenue = "Revenue"; var x = 1234.567;'),
    el('STYLE', '.kpi{color:red} /* Revenue 1 234,6 */'),
    el('DIV', '', [
      el('DIV', '', [el('SPAN', 'Revenue'), el('B', '1 234,6')]),
      el('DIV', '', [el('SPAN', 'Margin'), el('B', '17,2%')]),
    ]),
    el('TABLE', '', [el('TR', '', [el('TH', 'Region'), el('TH', 'Revenue, mln')])]),
    el('svg', '', [el('text', '1 234,6')]),
  ]);
}

// ── value match ──────────────────────────────────────────────────────────
boot(report());
let r = locate({ type: 'op_locate', id: 'a1', q: 'what is the revenue?', kpi: 'Revenue', value: '1 234,6' });
check('value: answers op_located', r && r.type === 'op_located');
check('value: id echoed', r.id === 'a1');
check('value: ok', r.ok === true);
check('value: the leaf holding the number is the hit', r.tag === 'b', r.tag);
check('value: text is the found text', r.text === '1 234,6', r.text);
check('value: the hit got the flash class',
  (function () { let f = null; (function walk(n) { if (n.classList && n.classList.contains('op-located')) f = n; (n.children || []).forEach(walk); })(doc.body); return f && f.textContent === '1 234,6'; })());
check('value: the hit was scrolled to centre',
  (function () { let f = null; (function walk(n) { if (n.scrolled) f = n; (n.children || []).forEach(walk); })(doc.body); return f && f.scrolled.block === 'center' && f.scrolled.behavior === 'smooth'; })());
check('value: a <style> element is injected once for the flash',
  doc.head.children.filter(c => c.tagName === 'STYLE').length === 1);

// unformatted number from the answer finds the formatted one in the report
boot(report());
r = locate({ type: 'op_locate', id: 2, kpi: 'Revenue', value: 1234.567 });
check('value: 1234.567 matches the rendered 1 234,6', r.ok === true && r.text === '1 234,6', r.text);
check('value: a numeric id is echoed as given', r.id === 2);

boot(report());
r = locate({ type: 'op_locate', id: 'p', kpi: 'Margin', value: '17,2%' });
check('value: a percent string matches verbatim', r.ok === true && r.text === '17,2%', r.text);
// The chat sends the DISPLAYED value — unit and all — while the report may
// round it one digit shorter: «99,99 %» from the chat is the cell «100,0%».
boot(report());
r = locate({ type: 'op_locate', id: 'p2', kpi: 'SL', value: '17,24 %' });
check('value: «17,24 %» with a unit and a space still finds the cell «17,2%»', r.ok === true && r.text === '17,2%', JSON.stringify(r));
boot(report());
r = locate({ type: 'op_locate', id: 'p3', kpi: 'SL', value: 0.1724 });
check('value: a share 0.1724 finds the same cell printed as a percent', r.ok === true && r.text === '17,2%', JSON.stringify(r));
boot(report());
r = locate({ type: 'op_locate', id: 'p4', kpi: 'Revenue', value: '$1 234,6' });
check('value: a currency prefix is not part of the number', r.ok === true && r.text === '1 234,6', JSON.stringify(r));

// ── context: the SAME number in many cells; the row and the header decide ──
function grid() {
  return el('DIV', '', [
    el('TABLE', '', [
      el('THEAD', '', [
        el('TR', '', [el('TH', 'Period'), el('TH', 'SL')]),
        el('TR', '', [el('TH', 'Month'), el('TH', 'Week'), el('TH', 'Daisy'), el('TH', 'Cornflower')]),
      ]),
      el('TBODY', '', [
        el('TR', '', [el('TD', 'June'), el('TD', 'W22.2026'), el('TD', '100,0%'), el('TD', '99,1%')]),
        el('TR', '', [el('TD', 'June'), el('TD', 'W23.2026'), el('TD', '100,0%'), el('TD', '100,0 %')]),
        el('TR', '', [el('TD', 'MTD'), el('TD', ''), el('TD', '100,0%'), el('TD', '100,0%')]),
      ]),
    ]),
  ]);
}
boot(grid());
r = locate({ type: 'op_locate', id: 'g1', kpi: 'SL', value: '99,99 %', labels: ['Cornflower', '23', 'W23'] });
check('context: among four «100,0%» cells the one in row W23 under header Cornflower wins',
  r.ok === true && r.text === '100,0 %', JSON.stringify(r));
boot(grid());
r = locate({ type: 'op_locate', id: 'g2', kpi: 'SL', value: '100,0%', labels: ['Daisy', 'W22'] });
check('context: … and row W22 under Daisy is a different cell', r.ok === true && r.text === '100,0%' && r.tag === 'td', JSON.stringify(r));
boot(grid());
r = locate({ type: 'op_locate', id: 'g3', kpi: 'SL', value: '100,0%', labels: ['Dandelion', 'W25'] });
check('context: several cells carry the number, none carries the context → honest «not found»',
  r.ok === false, JSON.stringify(r));
boot(report());
r = locate({ type: 'op_locate', id: 'g4', kpi: 'Revenue', value: '1 234,6', labels: ['Nobody'] });
check('context: a single cell with the number is still found even when the labels miss', r.ok === true, JSON.stringify(r));

// ── label match (no value, or a value nobody shows) ──────────────────────
boot(report());
r = locate({ type: 'op_locate', id: 'l1', kpi: 'revenue', value: '' });
check('label: case-insensitive kpi hit', r.ok === true, JSON.stringify(r));
check('label: the shortest node wins over the wordy header', r.text === 'Revenue', r.text);

boot(report());
r = locate({ type: 'op_locate', id: 'l2', kpi: '', value: '', labels: ['no such thing', 'Margin'] });
check('label: labels[] is searched too', r.ok === true && r.text === 'Margin', r.text);

boot(report());
r = locate({ type: 'op_locate', id: 'l3', kpi: 'Sales   by\nregion', value: '' });
check('label: whitespace is collapsed on both sides',
  r.ok === true && r.tag === 'h1', r.tag);

// value misses, label saves it
boot(report());
r = locate({ type: 'op_locate', id: 'l4', kpi: 'Margin', value: '999 999' });
check('value miss falls through to the label', r.ok === true && r.text === 'Margin', r.text);

// ── honest miss ──────────────────────────────────────────────────────────
boot(report());
r = locate({ type: 'op_locate', id: 'n1', kpi: 'Stock turnover', value: '424242' });
check('miss: ok is false', r.ok === false);
check('miss: tag and text are empty', r.tag === '' && r.text === '');
check('miss: nothing is flashed',
  (function () { let f = false; (function walk(n) { if (n.classList && n.classList.contains('op-located')) f = true; (n.children || []).forEach(walk); })(doc.body); return !f; })());

boot(report());
r = locate({ type: 'op_locate', id: 'n2' });
check('miss: an empty command is answered, not thrown', r.ok === false && r.id === 'n2');
boot(report());
r = locate({ type: 'op_locate', id: {}, kpi: {}, value: [], labels: 'none' });
check('miss: junk types are coerced away', r.ok === false && r.id === '');

// script/style text must never be the hit
boot(el('BODY', '', [el('SCRIPT', 'UniqueStringXyz'), el('STYLE', 'UniqueStringXyz')]));
r = locate({ type: 'op_locate', id: 's1', kpi: 'UniqueStringXyz', value: '' });
check('script/style text is not searched', r.ok === false, JSON.stringify(r));

// ── node budget ──────────────────────────────────────────────────────────
const many = [];
for (let i = 0; i < 25000; i++) many.push(el('DIV', 'row ' + i));
many.push(el('DIV', 'NeedleInHaystack'));          // past the 20 000 budget
boot(el('BODY', '', many));
r = locate({ type: 'op_locate', id: 'b1', kpi: 'NeedleInHaystack', value: '' });
check('budget: a huge report is answered, not hung', r && r.type === 'op_located');
check('budget: the sweep stops at the cap (needle beyond it is not found)', r.ok === false);
boot(el('BODY', '', many.slice(0, 100).concat([el('DIV', 'NeedleInHaystack')])));
r = locate({ type: 'op_locate', id: 'b2', kpi: 'NeedleInHaystack', value: '' });
check('budget: within the cap the same needle is found', r.ok === true, JSON.stringify(r));

// ── the flash is temporary ───────────────────────────────────────────────
boot(report());
locate({ type: 'op_locate', id: 'f1', kpi: 'Margin', value: '' });
const pending = timers.filter(Boolean);
check('flash: a removal timer is armed for 2s',
  pending.length === 1 && pending[0].ms === 2000, JSON.stringify(pending.map(p => p.ms)));
pending[0].fn();
check('flash: the class is gone after the timer',
  (function () { let f = false; (function walk(n) { if (n.classList && n.classList.contains('op-located')) f = true; (n.children || []).forEach(walk); })(doc.body); return !f; })());

// other commands still work (no regression in the annotate protocol)
boot(report());
(win.listeners.message || []).forEach(fn => fn({ data: { type: 'op_annotate_on' } }));
check('annotate protocol untouched: op_annotate_on posts nothing back', posted.length === 0);

console.log();
if (FAILURES.length) { console.log('FAILED: ' + FAILURES.join(', ')); process.exit(1); }
console.log('ALL PASS');
