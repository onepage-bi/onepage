/* Headless harness for static/js/main.js's language switcher IIFE
 * (components/lang_switch.html): open/close the menu, pick a language,
 * PUT /api/me/lang, set the op_lang cookie, reload.
 *
 * Same idiom as tests/sidebar_dom.js: no jsdom — a minimal DOM (elements
 * with classList/attrs/dataset/closest, delegated document click listeners)
 * is stubbed, then static/js/main.js is eval'd against it. This file's
 * `closest()` additionally understands attribute selectors and multi-token
 * descendant chains ('[data-lang-switch] .lang-trigger'), which main.js's
 * switcher relies on and sidebar_dom.js's simpler stub does not need.
 *
 * Run: node tests/lang_switch_dom.js
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

// ── DOM stub ─────────────────────────────────────────────────────────────
class ClassList {
  constructor() { this.set = new Set(); }
  add() { for (const c of arguments) if (c) this.set.add(c); }
  remove() { for (const c of arguments) this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  get value() { return Array.from(this.set).join(' '); }
}

function camel(s) { return s.replace(/-([a-z])/g, (m, c) => c.toUpperCase()); }

// Parses one simple selector (tag, #id, .class, [data-attr]) — no combinators.
function parseSimple(sel) {
  sel = sel.trim();
  const attrs = [];
  sel = sel.replace(/\[([\w-]+)\]/g, (m, name) => { attrs.push(name); return ''; });
  const m = /^([a-zA-Z0-9-]*)((?:[.#][\w-]+)*)$/.exec(sel);
  if (!m) return null;
  const tag = m[1] || null;
  const rest = m[2] || '';
  const id = (/#([\w-]+)/.exec(rest) || [])[1] || null;
  const classes = (rest.match(/\.[\w-]+/g) || []).map(c => c.slice(1));
  return { tag, id, classes, attrs };
}

function matchesOne(el, parsed) {
  if (!parsed) return false;
  if (parsed.tag && el.tagName.toLowerCase() !== parsed.tag.toLowerCase()) return false;
  if (parsed.id && el.attrs.id !== parsed.id) return false;
  for (const c of parsed.classes) if (!el.classList.contains(c)) return false;
  for (const a of parsed.attrs) if (!(a in el.attrs)) return false;
  return true;
}

function matches(el, selector) {
  return selector.split(',').some(part => matchesOne(el, parseSimple(part)));
}

// closest() over a (possibly multi-token, descendant-combinator) selector:
// the element must match the last token, and some ancestor must in turn
// satisfy the remaining chain — exactly what '[data-lang-switch] .lang-trigger'
// needs, without pulling in a full CSS engine.
function matchesChain(el, tokens) {
  if (!matchesOne(el, parseSimple(tokens[tokens.length - 1]))) return false;
  if (tokens.length === 1) return true;
  const rest = tokens.slice(0, -1);
  let anc = el.parent;
  while (anc) {
    if (matchesChain(anc, rest)) return true;
    anc = anc.parent;
  }
  return false;
}

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parent = null;
    this.attrs = {};
    this.dataset = {};
    this._class = new ClassList();
    this._text = '';
    this.hidden = false;
  }
  get classList() { return this._class; }
  get parentNode() { return this.parent; }
  set className(v) { this._class.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return this._class.value; }
  set textContent(v) { this.children = []; this._text = v === '' ? '' : String(v); }
  get textContent() { return this._text; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[camel(k.slice(5))] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    (function walk(node) {
      for (const c of node.children) {
        if (matches(c, sel)) out.push(c);
        walk(c);
      }
    })(this);
    return out;
  }
  closest(sel) {
    const tokens = sel.trim().split(/\s+/).filter(Boolean);
    let n = this;
    while (n) {
      if (n.tagName && matchesChain(n, tokens)) return n;
      n = n.parent;
    }
    return null;
  }
}

function mk(tag, attrs, cls) {
  const e = new El(tag);
  Object.keys(attrs || {}).forEach(k => e.setAttribute(k, attrs[k]));
  if (cls) e.className = cls;
  return e;
}

let docListeners = {};
const documentStub = {
  createElement: t => new El(t),
  getElementById: () => null,
  addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
  dispatchEvent: ev => { (docListeners[ev.type] || []).forEach(f => f.call(documentStub, ev)); return true; },
  querySelectorAll: sel => root.querySelectorAll(sel),
  querySelector: sel => root.querySelector(sel),
};

// localStorage / navigator / getSelection stubs main.js touches unconditionally.
const lsStore = {};
const localStorageStub = { getItem: k => (k in lsStore ? lsStore[k] : null), setItem: (k, v) => { lsStore[k] = String(v); } };

let reloaded = false;
let cookieJar = '';
const locationStub = {
  reload: () => { reloaded = true; },
};

const fetchCalls = [];
let fetchImpl = (url, opts) => { fetchCalls.push({ url, opts }); return Promise.resolve({ ok: true, json: () => Promise.resolve({}) }); };

const windowStub = {
  getSelection: () => ({ toString: () => '' }),
  location: locationStub,
  fetch: (...a) => fetchImpl(...a),
};

// ── Build a DOM mirroring components/lang_switch.html, placed in the topbar ──
const htmlEl = new El('html');
htmlEl.setAttribute('lang', 'ru');

const root = new El('div');   // stands in for <body>: everything main.js
                               // walks (children/closest) hangs off this.
// Each language is named in itself (endonym), as app/i18n.py LANG_LABELS does.
const LANGS = [
  { code: 'ru', name: 'Русский', short: 'RU' },
  { code: 'en', name: 'English', short: 'EN' },
  { code: 'zh-HK', name: '繁體中文（香港）', short: '繁' },
];

function buildSwitch(current) {
  const sw = mk('div', { 'data-lang-switch': '', 'data-place': 'topbar' });
  sw.classList.add('lang-switch');
  const trigger = mk('button', {}, 'btn icon sm ghost lang-trigger');
  trigger.setAttribute('aria-expanded', 'false');
  sw.appendChild(trigger);
  const menu = mk('div', {}, 'lang-menu');
  menu.hidden = true;
  sw.appendChild(menu);
  LANGS.forEach(l => {
    const opt = mk('button', { 'data-lang': l.code }, 'lang-opt' + (l.code === current ? ' current' : ''));
    menu.appendChild(opt);
    opt._lang = l.code;
  });
  return { sw, trigger, menu };
}

const { sw, trigger, menu } = buildSwitch('ru');
root.appendChild(sw);

global.document = documentStub;
document.documentElement = htmlEl;
document.cookie = '';
Object.defineProperty(document, 'cookie', {
  get() { return cookieJar; },
  set(v) { cookieJar += (cookieJar ? '; ' : '') + v.split(';')[0]; },
});
global.window = windowStub;
global.fetch = windowStub.fetch;   // main.js calls the bare global, not window.fetch
window.document = documentStub;
window.OP_USER = { id: 'u1' };
global.localStorage = localStorageStub;
Object.defineProperty(global, 'navigator', { value: undefined, configurable: true, writable: true }); // absent, like a stripped-down browser — the
                                 // persist() call must stay caught, not thrown.
global.location = locationStub;
global.CustomEvent = function (type, init) { this.type = type; Object.assign(this, init); };

const src = fs.readFileSync(path.join(ROOT, 'static/js/main.js'), 'utf8');
(0, eval)(src);

function click(el) { documentStub.dispatchEvent({ type: 'click', target: el }); }
function key(k) { (docListeners.keydown || []).forEach(fn => fn({ key: k })); }

(async function main() {
// ── (a) trigger toggles the menu open/closed, and aria-expanded follows ────
check('menu starts closed', menu.hidden === true);
click(trigger);
check('click on the trigger opens the menu', menu.hidden === false);
check('aria-expanded becomes true', trigger.getAttribute('aria-expanded') === 'true');
click(trigger);
check('click on the trigger again closes the menu', menu.hidden === true);
check('aria-expanded becomes false', trigger.getAttribute('aria-expanded') === 'false');

// ── (b) Escape closes an open menu ─────────────────────────────────────────
click(trigger);
check('menu open before Escape', menu.hidden === false);
key('Escape');
check('Escape closes the menu', menu.hidden === true);

// ── (c) picking the CURRENT language is a no-op — no fetch, no reload ─────
htmlEl.setAttribute('lang', 'ru');
click(trigger);
const ruOpt = menu.children.find(c => c._lang === 'ru');
fetchCalls.length = 0; reloaded = false;
click(ruOpt);
check('picking the already-current language does not call fetch', fetchCalls.length === 0);
check('picking the already-current language does not reload', reloaded === false);

// ── (d) picking a DIFFERENT language: PUT /api/me/lang, cookie, reload ────
htmlEl.setAttribute('lang', 'ru');
click(trigger);
const enOpt = menu.children.find(c => c._lang === 'en');
fetchCalls.length = 0; reloaded = false; cookieJar = '';
click(enOpt);
check('picking a different language closes the menu', menu.hidden === true);
await new Promise(r => setImmediate(r));
check('… calls fetch exactly once', fetchCalls.length === 1, fetchCalls.length);
if (fetchCalls.length === 1) {
  const call = fetchCalls[0];
  check('… PUTs /api/me/lang', call.url === '/api/me/lang', call.url);
  check('… method is PUT', call.opts && call.opts.method === 'PUT', call.opts);
  let body = null;
  try { body = JSON.parse(call.opts.body); } catch (e) {}
  check('… body carries {lang: "en"}', body && body.lang === 'en', call.opts && call.opts.body);
}
check('… sets the op_lang cookie', /op_lang=en/.test(cookieJar), cookieJar);
check('… reloads the page', reloaded === true);

// ── (e) anonymous (no window.OP_USER): cookie only, no fetch, still reloads ─
window.OP_USER = null;
htmlEl.setAttribute('lang', 'ru');
click(trigger);
const zhOpt = menu.children.find(c => c._lang === 'zh-HK');
fetchCalls.length = 0; reloaded = false; cookieJar = '';
click(zhOpt);
await new Promise(r => setImmediate(r));
check('anonymous: no fetch call', fetchCalls.length === 0);
check('anonymous: cookie is still set', /op_lang=zh-HK/.test(cookieJar), cookieJar);
check('anonymous: page still reloads', reloaded === true);

// ── (f) clicking outside the switcher closes an open menu ─────────────────
window.OP_USER = { id: 'u1' };
click(trigger);
check('menu open before an outside click', menu.hidden === false);
const outside = mk('div', {}, 'somewhere-else');
root.appendChild(outside);
click(outside);
check('an outside click closes the menu', menu.hidden === true);

console.log('');
if (FAILURES.length) {
  console.log(`FAILURES: ${FAILURES.length}`);
  FAILURES.forEach(f => console.log('  - ' + f));
  process.exit(1);
}
console.log('ALL PASS');
})();
