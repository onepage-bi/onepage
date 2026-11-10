/* Headless harness for static/js/main.js sidebar-collapse UX + base.html
 * flash-of-expanded fix + main.css collapsed-state rule.
 *
 * Same idiom as tests/catalog_dom.js: no jsdom — the smallest DOM main.js
 * actually touches (elements with classList/attrs/dataset, delegated
 * document click listeners) is stubbed, then static/js/main.js is eval'd
 * against it.
 *
 * Run: node tests/sidebar_dom.js   (prints PASS/FAIL lines + ALL PASS)
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

// ── (a) CSS check — no display:none for .sidebar-collapse-btn when collapsed
(function checkCss() {
  const css = fs.readFileSync(path.join(ROOT, 'static/css/main.css'), 'utf8');
  // Find every rule block whose selector mentions BOTH
  // [data-sidebar="collapsed"] and .sidebar-collapse-btn, then check its
  // declaration body for a display:none.
  const ruleRe = /([^{}]+)\{([^{}]*)\}/g;
  let m, offender = null;
  while ((m = ruleRe.exec(css))) {
    const selector = m[1];
    const body = m[2];
    if (selector.includes('[data-sidebar="collapsed"]') && selector.includes('.sidebar-collapse-btn')) {
      if (/display\s*:\s*none/i.test(body)) { offender = selector.trim(); break; }
    }
  }
  check('main.css: collapsed .sidebar-collapse-btn is never display:none', !offender, offender);
})();

// ── (d) base.html check — inline restore script precedes first stylesheet link
(function checkBaseHtml() {
  const html = fs.readFileSync(path.join(ROOT, 'templates/base.html'), 'utf8');
  const scriptIdx = html.indexOf('<script>');
  const linkIdx = html.search(/<link[^>]*rel=["']stylesheet["']/);
  check('base.html: has an inline <script> in <head>', scriptIdx !== -1, 'no inline script found');
  check('base.html: has a stylesheet <link>', linkIdx !== -1, 'no stylesheet link found');
  check('base.html: inline script precedes first stylesheet link',
    scriptIdx !== -1 && linkIdx !== -1 && scriptIdx < linkIdx,
    'script@' + scriptIdx + ' link@' + linkIdx);
  // Sanity: the inline script actually restores op-ui-sidebar from localStorage.
  const scriptBody = html.slice(scriptIdx, html.indexOf('</script>', scriptIdx));
  check('base.html: inline script reads op-ui-sidebar from localStorage',
    /op-ui-/.test(scriptBody) && /localStorage/.test(scriptBody));
})();

// ── DOM stub (minimal — only what main.js touches) ─────────────────────
class ClassList {
  constructor() { this.set = new Set(); }
  add() { for (const c of arguments) if (c) this.set.add(c); }
  remove() { for (const c of arguments) this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  toggle(c, on) { if (on === undefined) on = !this.set.has(c); if (on) this.set.add(c); else this.set.delete(c); return on; }
  get value() { return Array.from(this.set).join(' '); }
}

function camel(s) { return s.replace(/-([a-z])/g, (m, c) => c.toUpperCase()); }

// Parses one simple selector (no combinators) into {tag, id, classes}.
function parseSimple(sel) {
  sel = sel.trim();
  const m = /^([a-zA-Z0-9-]*)((?:[.#][\w-]+)*)$/.exec(sel);
  if (!m) return null;
  const tag = m[1] || null;
  const rest = m[2] || '';
  const id = (/#([\w-]+)/.exec(rest) || [])[1] || null;
  const classes = (rest.match(/\.[\w-]+/g) || []).map(c => c.slice(1));
  return { tag, id, classes };
}

function matchesOne(el, parsed) {
  if (!parsed) return false;
  if (parsed.tag && el.tagName.toLowerCase() !== parsed.tag.toLowerCase()) return false;
  if (parsed.id && el.attrs.id !== parsed.id) return false;
  for (const c of parsed.classes) if (!el.classList.contains(c)) return false;
  return true;
}

function matches(el, selector) {
  return selector.split(',').some(part => matchesOne(el, parseSimple(part)));
}

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parent = null;
    this.attrs = {};
    this.dataset = {};
    this.listeners = {};
    this._class = new ClassList();
    this._text = '';
  }
  get classList() { return this._class; }
  set className(v) { this._class.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return this._class.value; }
  set textContent(v) { this.children = []; this._text = v === '' ? '' : String(v); }
  get textContent() { return this._text; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[camel(k.slice(5))] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  closest(sel) {
    let n = this;
    while (n) {
      if (n.tagName && matches(n, sel)) return n;
      n = n.parent;
    }
    return null;
  }
}

let byIdMap = {};
let docListeners = {};
const documentStub = {
  createElement: t => new El(t),
  getElementById: id => byIdMap[id] || null,
  addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
  dispatchEvent: ev => { (docListeners[ev.type] || []).forEach(f => f.call(documentStub, ev)); return true; },
  // main.js's language switcher looks for [data-lang-switch]; this page has
  // none, so an empty NodeList is the honest answer.
  querySelectorAll: () => [],
};

function mk(tag, attrs, id) {
  const e = new El(tag);
  Object.keys(attrs || {}).forEach(k => e.setAttribute(k, attrs[k]));
  if (attrs && attrs.class) e.className = attrs.class;
  if (id) { e.attrs.id = id; byIdMap[id] = e; }
  return e;
}

// html (documentElement) stub carrying data-sidebar etc.
const htmlEl = new El('html');

// localStorage stub.
const lsStore = {};
const localStorageStub = {
  getItem: k => (k in lsStore ? lsStore[k] : null),
  setItem: (k, v) => { lsStore[k] = String(v); },
};

// window.getSelection stub — empty selection by default.
let selectionText = '';
const windowStub = {
  getSelection: () => ({ toString: () => selectionText }),
};

// ── Build a DOM mirroring templates/components/sidebar.html ────────────
byIdMap = {};
docListeners = {};
const aside = mk('aside', { class: 'sidebar' });
aside.classList.add('sidebar');
const sidebarHead = mk('div', { class: 'sidebar-head' });
aside.appendChild(sidebarHead);
const logo = mk('div', { class: 'sidebar-logo' });
sidebarHead.appendChild(logo);
const collapseBtn = mk('button', { class: 'sidebar-collapse-btn' }, 'sidebar-collapse');
collapseBtn.classList.add('sidebar-collapse-btn');
collapseBtn.setAttribute('data-title-expanded', 'Collapse sidebar  [');
collapseBtn.setAttribute('data-title-collapsed', 'Expand sidebar  [');
sidebarHead.appendChild(collapseBtn);

const nav = mk('nav', { class: 'sidebar-nav' });
aside.appendChild(nav);
const navItem = mk('a', { class: 'nav-item' });
navItem.classList.add('nav-item');
nav.appendChild(navItem);

// A plain "empty area" node inside the sidebar — not interactive, not a
// nav-item/user-chip/logo.
const emptyArea = mk('div', { class: 'sidebar-section-label' });
aside.appendChild(emptyArea);

// ── Load main.js against the stub globals ───────────────────────────────
global.document = documentStub;
document.documentElement = htmlEl;
global.window = windowStub;
window.document = documentStub;
global.localStorage = localStorageStub;

const src = fs.readFileSync(path.join(ROOT, 'static/js/main.js'), 'utf8');
(0, eval)(src);

function click(el) { documentStub.dispatchEvent({ type: 'click', target: el }); }

// ── (b) click on empty sidebar area toggles expanded -> collapsed -> expanded
htmlEl.setAttribute('data-sidebar', 'expanded');
click(emptyArea);
check('click on empty sidebar area toggles expanded -> collapsed',
  htmlEl.getAttribute('data-sidebar') === 'collapsed', htmlEl.getAttribute('data-sidebar'));
click(emptyArea);
check('click on empty sidebar area toggles collapsed -> expanded',
  htmlEl.getAttribute('data-sidebar') === 'expanded', htmlEl.getAttribute('data-sidebar'));

// ── (c) click on a .nav-item does NOT toggle
htmlEl.setAttribute('data-sidebar', 'expanded');
click(navItem);
check('click on a .nav-item does NOT toggle the sidebar',
  htmlEl.getAttribute('data-sidebar') === 'expanded', htmlEl.getAttribute('data-sidebar'));

// ── Extra: #sidebar-collapse button still toggles, and title flips
htmlEl.setAttribute('data-sidebar', 'expanded');
click(collapseBtn);
check('click on #sidebar-collapse toggles expanded -> collapsed',
  htmlEl.getAttribute('data-sidebar') === 'collapsed', htmlEl.getAttribute('data-sidebar'));
check('#sidebar-collapse title flips to "Expand..." when collapsed',
  collapseBtn.getAttribute('title') === 'Expand sidebar  [', collapseBtn.getAttribute('title'));
click(collapseBtn);
check('#sidebar-collapse title flips back to "Collapse..." when expanded',
  collapseBtn.getAttribute('title') === 'Collapse sidebar  [', collapseBtn.getAttribute('title'));

// ── Extra: a text-selection drag ending inside the sidebar does not toggle
htmlEl.setAttribute('data-sidebar', 'expanded');
selectionText = 'some selected text';
click(emptyArea);
check('click with an active text selection does NOT toggle the sidebar',
  htmlEl.getAttribute('data-sidebar') === 'expanded', htmlEl.getAttribute('data-sidebar'));
selectionText = '';

console.log();
if (FAILURES.length) {
  console.log(FAILURES.length + ' FAILED: ' + FAILURES.join(', '));
  process.exit(1);
}
console.log('ALL PASS');
