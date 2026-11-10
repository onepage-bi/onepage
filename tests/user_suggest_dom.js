// static/js/user-suggest.js under a stub DOM: the dropdown must actually
// open (review 2026-08-23 blocker: render() used to clear `items` before its
// own length check, so no box was ever appended).
'use strict';
require('./_i18n_stub');   // global t/tn — must come before the DOM stub
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const FAILURES = [];
function check(label, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + label + (!cond && extra ? '  <-- ' + extra : ''));
  if (!cond) FAILURES.push(label);
}

class El {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {};
    this.style = {}; this.classList = { toggle() {}, contains() { return false; } };
    this.textContent = ''; this.value = ''; this.offsetTop = 10; this.offsetHeight = 28;
    this.parentElement = null; this.attrs = {};
  }
  appendChild(c) { c.parentElement = this; this.children.push(c); return c; }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(x => x !== this); }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatchEvent(e) { (this.listeners[e.type] || []).forEach(fn => fn(e)); return true; }
  setAttribute(k, v) { this.attrs[k] = v; }
  closest() { return null; }
  focus() {}
  querySelectorAll(sel) {
    const out = [];
    const walk = n => n.children.forEach(c => { if (sel === '.user-suggest-row' && c.className === 'user-suggest-row') out.push(c); walk(c); });
    walk(this); return out;
  }
}
const input = new El('INPUT');
const wrap = new El('DIV');
wrap.appendChild(input);

global.Event = function (type, opts) { this.type = type; this.bubbles = opts && opts.bubbles; };
global.document = {
  createElement: tag => new El(tag.toUpperCase()),
  querySelectorAll: sel => (sel === 'input[data-user-suggest]' ? [input] : []),
};
global.fetch = async () => ({ ok: true, json: async () => ({ items: [
  { person_id: 'ivanov1', name_ru: 'Ivanov, Ivan', name_en: 'Ivan Ivanov', dept: 'Logistics' },
  { person_id: 'petrova2', name_ru: 'Petrova, Maria', name_en: 'Maria Petrova', dept: 'Finance' },
] }) });

require(path.join(ROOT, 'static', 'js', 'user-suggest.js'));

(async () => {
  input.value = 'iva';
  input.dispatchEvent({ type: 'input' });
  await new Promise(r => setTimeout(r, 260));   // debounce 200 ms + fetch tick
  const box = wrap.children.find(c => c.className === 'user-suggest-box');
  check('typing ≥2 chars opens the dropdown box', !!box, 'no .user-suggest-box appended');
  check('the box holds one row per suggestion', !!box && box.children.length === 2,
    box && String(box.children.length));
  check('rows are textContent, not markup', !!box && box.children[0].children[0].textContent === 'Ivanov, Ivan');
  input.dispatchEvent({ type: 'keydown', key: 'ArrowDown', preventDefault() {} });
  input.dispatchEvent({ type: 'keydown', key: 'Enter', preventDefault() {} });
  check('ArrowDown + Enter picks the first row into the input as person_id', input.value === 'ivanov1', input.value);
  check('picking closes the box', !wrap.children.some(c => c.className === 'user-suggest-box'));

  console.log();
  if (FAILURES.length) { console.log(FAILURES.length + ' FAILED'); process.exit(1); }
  console.log('ALL PASS');
})();
